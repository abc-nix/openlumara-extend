"""pdfgeom - structural evidence from a PDF text layer. pypdf only, no core.

Why this module exists: the repairs done by textfix (and, later, by the
series profiles) need two facts that no extractor puts in its plain text:

  * a word hyphenated across a line break ("perse-" / "guido") is ONE word,
    not two, and the break is proof enough - no vocabulary check needed;
  * a paragraph begins where the leading jumps (a first-line indent or a gap
    bigger than the line spacing). A column break is NOT a paragraph.

Everything here is read from the page itself. Every helper returns None when
it cannot prove what it was asked about (scans with no text layer, encrypted
files, unreadable pages); callers MUST then do nothing. Same contract as
textfix: repair only on proof, never on a guess.

Reading order is approximated: lines are grouped per page, columns are
clustered by left edge, and within a page lines are ordered by (column, y).
That is enough for the repairs above on this kind of two-column layout; it is
deliberately NOT a general layout analyser.
"""
import hashlib
import re
from io import BytesIO

_CACHE = {}
_CACHE_MAX = 4

# last word of a line, only when the line ends with a hyphen
_WORD_END = re.compile(r"([^\W\d_][^\W\d_'-]*)-$")
# first word of a line
_WORD_START = re.compile(r"^([^\W\d_][^\W\d_'’-]*)")
_SOFT_HYPHEN = "\u00ad"          # how this series writes a hyphenation break

# paratext that can sit between a hyphenated line and its continuation: the
# InDesign stamp ("... .indd 87 11/10/17 11:18") and the bare page number
_PARATEXT = re.compile(r"(\.indd\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b|^\d{1,4}$)")


def _strip_break(line):
    """Text before the hyphenation mark, or None if the line ends without one.

    Two encodings seen in the wild: a plain "-" (Cursiva U4) and U+00AD, the
    invisible soft hyphen (the exercises), which the extractors drop and
    replace with a space - that is the "perse guido" artefact."""
    s = line.rstrip()
    if s.endswith(("-", _SOFT_HYPHEN)) and len(s) > 1:
        return s[:-1]
    return None


# -------------------------
#   small helpers
# -------------------------
def _key(data):
    return hashlib.sha1(data).digest()


def _put(key, value):
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[key] = value
    return value


def _mul(m1, m2):
    """m1 x m2 for 2x3 affine matrices (a,b,c,d,e,f)."""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (a1 * a2 + b1 * c2, a1 * b2 + b1 * d2,
            c1 * a2 + d1 * c2, c1 * b2 + d1 * d2,
            e1 * a2 + f1 * c2 + e2, e1 * b2 + f1 * d2 + f2)


def _reader(data):
    try:
        from pypdf import PdfReader
        r = PdfReader(BytesIO(data))
        if getattr(r, "is_encrypted", False) and not r.decrypt(""):
            return None
        return r
    except Exception:
        return None


# -------------------------
#   text layer
# -------------------------
def raw_text(data):
    """Whole text layer as pypdf reads it (pages joined with a newline)."""
    k = ("raw", _key(data))
    if k in _CACHE:
        return _CACHE[k]
    r = _reader(data)
    if r is None:
        return _put(k, None)
    parts = []
    try:
        for page in r.pages:
            parts.append(page.extract_text() or "")
    except Exception:
        return _put(k, None)
    text = "\n".join(parts)
    return _put(k, text if text.strip() else None)


def lines(data):
    """[(page_index, line_text)]: non-empty lines, in pypdf's order."""
    k = ("lines", _key(data))
    if k in _CACHE:
        return _CACHE[k]
    r = _reader(data)
    if r is None:
        return _put(k, None)
    out = []
    try:
        for pno, page in enumerate(r.pages):
            for line in (page.extract_text() or "").split("\n"):
                line = line.strip()
                if line:
                    out.append((pno, line))
    except Exception:
        return _put(k, None)
    return _put(k, out or None)


def chunks(data):
    """[(page, y, x, text, size)] per text-showing chunk, in draw order.

    Positions are device space: the text matrix is multiplied by the graphics
    matrix, because InDesign wraps the body in q ... cm ... Q (same reason as
    extras/_pdfcolor)."""
    k = ("chunks", _key(data))
    if k in _CACHE:
        return _CACHE[k]
    r = _reader(data)
    if r is None:
        return _put(k, None)
    out = []
    for pno, page in enumerate(r.pages):
        rows = []

        def vis(text, cm, tm, font_dict, font_size, _rows=rows):
            if not text or not text.strip():
                return
            dev = _mul(tm, cm)
            try:
                size = float(font_size or 0)
            except Exception:
                size = 0.0
            _rows.append((round(dev[5], 2), round(dev[4], 2), text, round(size, 2)))

        try:
            page.extract_text(visitor_text=vis)
        except Exception:
            return _put(k, None)
        out.extend((pno, y, x, t, s) for (y, x, t, s) in rows)
    return _put(k, out or None)


def _column_centers(xs):
    """Left edge of every column: the first x of each cluster."""
    centers = []
    for x in sorted(xs):
        if not centers or x - centers[-1] > 25.0:
            centers.append(x)
    return centers


def _column_of(x0, centers):
    """Column a line belongs to; an indented first line stays in its column."""
    idx = 0
    for i, c in enumerate(centers):
        if x0 + 2.0 >= c:
            idx = i
    return idx


def line_boxes(data):
    """[(page, y, x0, text, size)] one entry per visual line.

    Chunks belong to the same line when their device y agrees within 2 pt; the
    line is ordered by x. Lines are sorted per page by (column, y): the column
    is the rounded left edge, so a two-column page never interleaves."""
    k = ("boxes", _key(data))
    if k in _CACHE:
        return _CACHE[k]
    ch = chunks(data)
    if not ch:
        return _put(k, None)
    per_page = {}
    for pno, y, x, text, size in ch:
        per_page.setdefault(pno, []).append((y, x, text, size))
    out = []
    for pno, rows in per_page.items():
        rows.sort(key=lambda r: (-r[0], r[1]))
        cur, cur_y = [], None
        for y, x, text, size in rows:
            if cur_y is None or abs(y - cur_y) <= 2.0:
                cur.append((x, text, size))
                cur_y = y if cur_y is None else cur_y
                continue
            out.append(_line(pno, cur, cur_y))
            cur, cur_y = [(x, text, size)], y
        if cur:
            out.append(_line(pno, cur, cur_y))
    # columns first, then y inside each column
    ordered = []
    for pno in sorted({b[0] for b in out}):
        page = [b for b in out if b[0] == pno]
        centers = _column_centers([b[2] for b in page])
        page.sort(key=lambda b: (_column_of(b[2], centers), -b[1]))
        ordered.extend(page)
    return _put(k, ordered or None)


def _line(pno, parts, y):
    parts.sort(key=lambda p: p[0])
    text = "".join(p[1] for p in parts).strip()
    x0 = min(p[0] for p in parts)
    sizes = [p[2] for p in parts if p[2]]
    size = max(sizes) if sizes else 0.0
    return (pno, y, x0, text, size)


# -------------------------
#   proofs
# -------------------------
# words that can open a page or a sentence on their own; a page-end hyphen
# followed by one of them is a suspended dash, not a hyphenation
_SENTENCE_OPENERS = frozenset((
    "de", "la", "el", "los", "las", "lo", "un", "una", "unos", "unas",
    "que", "y", "e", "o", "u", "en", "con", "por", "para", "del", "al",
    "se", "su", "sus", "es", "son", "no", "si", "sí", "ya", "pero",
    "este", "esta", "estos", "estas", "ese", "esa", "aquel", "aquella",
    "como", "más", "menos", "entre", "cuando", "donde", "porque",
))


def _next_body(ls, i, limit=3):
    """Index of the next line that is not paratext (stamp, page number)."""
    for j in range(i + 1, min(len(ls), i + 1 + limit)):
        if not _PARATEXT.search(ls[j][1]):
            return j
    return None


def hyphen_pairs(data, cross_page=False):
    """set of (left, right): the PDF proves `left` + "-" + `right`.

    Strict on purpose: the line ends on a hyphen (or on U+00AD) and the next
    one starts with a lowercase letter. A capital after the hyphen is left
    alone - that is a compound or a proper name ("vasco-\nFrancés"), not a
    hyphenation. With cross_page, a page boundary also counts, unless the
    right half is a word that can open a sentence by itself ("-" + "de" is a
    suspended dash)."""
    k = ("hyphens", _key(data), bool(cross_page))
    if k in _CACHE:
        return _CACHE[k]
    ls = lines(data)
    if not ls:
        return _put(k, None)
    pairs = set()
    for i in range(len(ls) - 1):
        p0, a = ls[i]
        head = _strip_break(a)
        if head is None:
            continue
        j = _next_body(ls, i)
        if j is None:
            continue
        p1, b = ls[j]
        if p0 != p1 and not cross_page:
            continue
        m, n = _WORD_END.search(head + "-"), _WORD_START.match(b)
        if not m or not n:
            continue
        left, right = m.group(1), n.group(1)
        if not (right[:1].islower() and len(left) >= 2 and len(right) >= 2):
            continue
        if p0 != p1 and right.lower() in _SENTENCE_OPENERS:
            continue                    # suspended dash at a page end
        pairs.add((left, right))
    return _put(k, pairs or None)


def leading(data):
    """Median vertical step between consecutive lines of a page, in points."""
    k = ("leading", _key(data))
    if k in _CACHE:
        return _CACHE[k]
    boxes = line_boxes(data)
    if not boxes:
        return _put(k, None)
    centers = {}
    for pno in {b[0] for b in boxes}:
        centers[pno] = _column_centers([b[2] for b in boxes if b[0] == pno])
    steps = []
    prev = None
    for pno, y, x0, text, size in boxes:
        col = _column_of(x0, centers.get(pno) or [x0])
        if prev is not None and prev[0] == pno and prev[1] == col:
            d = prev[2] - y
            if 0 < d < 60:
                steps.append(round(d, 1))
        prev = (pno, col, y)
    if not steps:
        return _put(k, None)
    steps.sort()
    return _put(k, steps[len(steps) // 2])


def paragraph_starts(data, gap_factor=1.35, indent_min=6.0, indent_max=40.0):
    """[(page, y, x0, first_60_chars)] where a paragraph plausibly begins.

    Two proofs only, both after a full-width line of the same column: a first
    line indented indent_min..indent_max points from the column's left edge
    (this series: ~20 pt) or a vertical step bigger than gap_factor x leading
    (the Cursiva units). A column change is explicitly NOT a paragraph start,
    and neither is a list item (its previous line is indented too)."""
    k = ("paragraphs", _key(data), round(gap_factor, 2),
         round(indent_min, 2), round(indent_max, 2))
    if k in _CACHE:
        return _CACHE[k]
    boxes = line_boxes(data)
    lead = leading(data)
    if not boxes or not lead:
        return _put(k, None)
    centers = {}
    for pno in {b[0] for b in boxes}:
        centers[pno] = _column_centers([b[2] for b in boxes if b[0] == pno])
    out = []
    prev = None                             # (page, column, y, x0, text)
    for pno, y, x0, text, size in boxes:
        col = _column_of(x0, centers.get(pno) or [x0])
        margin = (centers.get(pno) or [x0])[col]
        if prev is not None and prev[0] == pno and prev[1] == col:
            at_margin = abs(prev[3] - margin) <= 2.0
            ind = x0 - margin
            # a gap or an indent only starts a paragraph after a FULL-WIDTH
            # line: list items are indented too and must not be split apart
            if at_margin:
                if (indent_min <= ind <= indent_max):
                    out.append((pno, y, x0, text[:60]))
                elif ind <= 2.0 and (prev[2] - y) > gap_factor * lead:
                    out.append((pno, y, x0, text[:60]))
        prev = (pno, col, y, x0, text)
    return _put(k, out or None)

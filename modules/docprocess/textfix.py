"""textfix - repair the small, repetitive damage converters do to running text.

Everything here is EVIDENCE-BASED from the document itself, never a word list.
The converter mangles what pypdf reads cleanly, so the PDF's own text layer is
the reference we repair against (scans simply have none: those rules are then
skipped, the rest still applies).

1. SPACE BEFORE PUNCTUATION.  Rebuilding inline emphasis emits "**reseña** ,"
   because the comma lived in another font run. We drop the space only when it
   sits between a closing emphasis marker and the punctuation (the artifact's
   exact signature); ordinary text is untouched.

2. PARAGRAPHS SPLIT AT A STYLE BOUNDARY.  A run change mid-sentence can cut a
   paragraph in two ("...Si atendemos a la" / "**perspectiva y enfoque** de
   cada texto:"). Two blocks are merged only when the first ends in a letter
   and the second starts lowercase or with emphasis, so headings, list items
   and tables never merge.

3. HYPHENATION DAMAGE.  Line-end hyphens arrive mangled three ways:
       "correccio nes"        (hyphen became a space)
       "contrata lostítulos"  (word boundary lost)
       "reseña ycrítica"      (same, glued to a preceding conjunction)
   A token is split only at a point where the PDF itself proves the two halves
   are adjacent words (bigram check, so "oeste" cannot become "o este"), and a
   pair of words is joined only when the first half is NOT a word on its own
   and the union IS ("con tacto" stays).

4. GLUED LIST GROUPS.  A bullet that starts a new line in the PDF gets appended
   to the previous item ("...la literatura. → Si atendemos..."). An arrow after
   a sentence end becomes its own list item again.

Code fences are never touched. Everything returns text unchanged on any doubt.
"""
import re

_WORD = re.compile(r"[^\W\d_]{2,}")
_FENCE = re.compile(r"^\s*(```|~~~)")
_PUNCT_AFTER_MARKER = re.compile(r"(\*{1,3}|_{1,3})[ \t]+([,.;:»)])")
_GLUED_ARROW = re.compile(r"(?<=[.:;!?])[ \t]*(→|➔|➤)[ \t]+")
_LOOSE_PUNCT = re.compile(r"(?<=\*)[ \t]+(?=[,.])")
_TERMINAL = ".!?:;»"
# a word hyphenated across a line break, split INSIDE an emphasis run:
#   "**importancia significa** - **tiva en el mercado**"
#   "**profundi** - **dad**"
_EMPH_HYPHEN = re.compile(r"(\*{1,3})([^*]*?)([^\W\d_]+)\1[ \t]*-[ \t]*"
                          r"\1([^\W\d_]+)([^*]*?)\1")


def _reference(pdf_bytes):
    """(vocab, bigrams) from the PDF text layer, or (None, None)."""
    try:
        from io import BytesIO
        import pypdf
        r = pypdf.PdfReader(BytesIO(pdf_bytes))
        if r.is_encrypted and not r.decrypt(""):
            return None, None, None
        raw = "\n".join((p.extract_text() or "") for p in r.pages)
    except Exception:
        return None, None, None
    if not raw.strip():
        return None, None, None
    flat = re.sub(r"\s+", " ", raw)
    joined = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", raw)   # "incorpo-" + "ración"
    low = joined.lower()
    vocab = set(_WORD.findall(low))
    # Adjacent pairs must be built from the TOKEN LIST, not with findall:
    # a non-overlapping scan consumes the shared word ("y el subgénero"
    # swallowed "el", so ("el","subgénero") never existed). One-letter first
    # halves are kept because "reseña y crítica" is the proof for "ycrítica".
    tokens = re.findall(r"[^\W\d_]+", low)
    bigrams = set(zip(tokens, tokens[1:]))
    return vocab, bigrams, flat


def _split_unknown_compound(token, vocab, bigrams):
    """'lostítulos' -> 'los títulos' only where the PDF itself says so."""
    low = token.lower()
    if low in vocab or len(low) < 4:
        return None
    for i in range(1, len(low) - 1):
        a, b = low[:i], low[i:]
        if len(b) < 2 or (a, b) not in bigrams:
            continue
        if b in vocab and (a in vocab or len(a) == 1):
            return token[:i] + " " + token[i:]
    return None


def _rejoin_emphasis_break(text, vocab):
    """Rejoin a word hyphenated across a line break inside/around emphasis.

    Both halves are proven by the PDF vocabulary, so a genuine two-word dash
    ("amor - odio") is never touched."""
    n = 0

    def repl(m):
        nonlocal n
        mk, head, a, b, tail = m.groups()
        if (a + b).lower() in vocab:
            n += 1
            return f"{mk}{head}{a}{b}{tail}{mk}"
        return m.group(0)

    return _EMPH_HYPHEN.sub(repl, text), n


def _join_fragments(text, vocab):
    """'correccio nes' -> 'correcciones': first half is not a word, union is.

    With a hyphen between them ("significa - tiva") the union is proof enough
    even when the first half is a real word: real compounds are written
    without spaces around the hyphen."""
    out, pos, n = [], 0, 0
    for m in re.finditer(r"\b([^\W\d_]{2,})([ \t]+-[ \t]+|[ \t]+)([^\W\d_]{2,})\b",
                         text):
        a, sep, b = m.group(1), m.group(2), m.group(3)
        union = (a + b).lower()
        hyphen = "-" in sep
        if union in vocab and (hyphen or a.lower() not in vocab):
            out.append(text[pos:m.start()])
            out.append(a + b)
            pos = m.end()
            n += 1
    out.append(text[pos:])
    return "".join(out), n


def _merge_split_paragraphs(text):
    """Undo blocks cut at a style boundary; never touches headings or lists."""
    blocks = text.split("\n\n")
    out, merged = [], 0
    for blk in blocks:
        if out:
            prev = out[-1]
            ps = prev.rstrip()
            ns = blk.lstrip()
            starts_cont = bool(re.match(r"^(\*{1,3}[a-záéíóúüñ«“\"¿¡]|[a-záéíóúüñ«“\"¿¡])", ns))
            # A trailing "," or ";" is a page-break split unless the next block
            # is itself a list item: element lists DO end with a comma or a
            # semicolon. A trailing ":" only merges into a lowercase
            # continuation - a colon followed by a capital usually announces a
            # list or a new paragraph, not a split sentence.
            # real enumerators only: "(a)", "b)", "1." / "2)" and bullets.
            # NOT a short word plus a period - that is ordinary prose
            # ("mano. Abrir los ojos...") and must not block a merge.
            # A straight quote is a continuation too: this pass runs BEFORE
            # _typographic_quotes, so a split in front of a quoted sentence
            # still carries " and would otherwise stay unmerged forever.
            next_enum = bool(re.match(r"^(?:\(?[a-zA-Z]\)\s|\d+[.)]\s|[-•·–—]\s)", ns))
            if ps.endswith(":"):
                prev_open = starts_cont
            elif ps.endswith((",", ";")):
                prev_open = not next_enum
            else:
                prev_open = ps[-1:].isalpha() and starts_cont
            blocky = ("#", "|", ">")
            prev_blocky = (ps.startswith(blocky) or ps.startswith(("- ", "* "))) \
                and not ps.startswith("- → ")
            next_blocky = (ns.startswith(blocky) or ns.startswith(("- ", "* "))
                           or ns.startswith("```") or next_enum)
            if prev_open and ps and not prev_blocky and not next_blocky:
                out[-1] = ps + " " + ns
                merged += 1
                continue
        out.append(blk)
    return "\n\n".join(out), merged


def _typographic_quotes(block, flat=None):
    """Straight `"` -> “ ” for a block whose quotes balance.

    Only applied when the block has an EVEN number of straight quotes, so a
    quote left open in one paragraph and closed in the next is never
    half-converted. Apostrophes are untouched (l'écrivain must stay).

    When the PDF text is available we copy the EXACT shapes from the passage
    itself (that is what keeps a quote nested inside another one correct);
    alternation is only the fallback."""
    n = block.count('"')
    if n == 0 or n % 2:
        return block, 0
    shapes = _reference_quotes(block, n, flat) if flat else None
    if not shapes:
        shapes, opening = [], True
        for _ in range(n):
            shapes.append("\u201c" if opening else "\u201d")
            opening = not opening
    it = iter(shapes)
    return "".join(next(it) if ch == '"' else ch for ch in block), n


def _reference_quotes(block, n, flat):
    """Quote shapes, in order, from the matching passage of the PDF text."""
    plain = re.sub(r"\*+", "", block)
    plain = re.sub(r"\s+", " ", plain).strip()
    # the md has straight quotes where the PDF has curly ones: drop them from
    # the needle or the passage would never be found
    needle = re.sub(r"[\"\u201c\u201d]", "", plain)[:40]
    if len(needle) < 15:
        return None
    pos = flat.find(needle)
    if pos < 0:
        pos = flat.lower().find(needle.lower())
    if pos < 0:
        return None
    start = pos
    if plain.startswith('"'):
        # the block opens with a quote, which sits just BEFORE the needle:
        # take it too, or every shape after it would be shifted by one
        j = pos - 1
        while j >= 0 and flat[j] == " ":
            j -= 1
        if j >= 0 and flat[j] in "\u201c\u201d\"":
            start = j
    found, i = [], start
    while i < len(flat) and len(found) < n and i - pos < 4000:
        if flat[i] in "\u201c\u201d":
            found.append(flat[i])
        elif flat[i] == '"':
            found.append("\u201c")
        i += 1
    return found if len(found) == n else None


def repair(text, pdf_bytes=None, log=lambda m: None):
    """Apply the four repairs. Safe to call on any markdown (no-op on doubt)."""
    if not text:
        return text
    vocab, bigrams, flat = _reference(pdf_bytes) if pdf_bytes else (None, None, None)

    # split on code fences; only prose segments are repaired
    parts = re.split(r"(^\s*(?:```|~~~).*?^\s*(?:```|~~~)\s*$)",
                     text, flags=re.S | re.M)
    counts = {"punct": 0, "arrow": 0, "merge": 0, "split": 0, "join": 0,
              "emphjoin": 0, "quotes": 0}

    for idx, seg in enumerate(parts):
        if idx % 2 == 1:            # fenced code: keep byte-identical
            continue
        if not seg:
            continue

        new = _PUNCT_AFTER_MARKER.sub(r"\1\2", seg)
        new = _LOOSE_PUNCT.sub("", new)
        if new != seg:
            counts["punct"] = 1

        counts["arrow"] += len(_GLUED_ARROW.findall(new))
        new = _GLUED_ARROW.sub(lambda m: "\n\n- " + m.group(1) + " ", new)

        new, nm = _merge_split_paragraphs(new)
        counts["merge"] += nm

        blocks = []
        for blk in new.split("\n\n"):
            if blk.lstrip().startswith(("-", "#", "|", ">", "*")):
                blocks.append(blk)          # structural lines keep raw quotes
                continue
            blk2, nq = _typographic_quotes(blk, flat)
            counts["quotes"] += nq
            blocks.append(blk2)
        new = "\n\n".join(blocks)

        if vocab:
            new, ne = _rejoin_emphasis_break(new, vocab)
            counts["emphjoin"] += ne
            toks_out = []
            for tok in _WORD.findall(new):
                rep = _split_unknown_compound(tok, vocab, bigrams)
                toks_out.append((tok, rep)) if rep else None
            for tok, rep in toks_out:
                new = new.replace(tok, rep, 1)
                counts["split"] += 1
            new, nj = _join_fragments(new, vocab)
            counts["join"] += nj
        parts[idx] = new

    result = "".join(parts)
    if any(counts.values()):
        log("textfix: " + ", ".join(f"{k}={v}" for k, v in counts.items() if v))
    return result

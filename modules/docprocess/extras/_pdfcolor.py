"""_pdfcolor - which text runs are painted in a non-black fill colour.

Why not pypdf's text extraction: it drops colour entirely. Why not decode the
strings ourselves: half of this series' body is CID/Identity-H, so we would need
every font's ToUnicode map. Instead we line up the two lists by POSITION: the
content stream says "at device-y=NNN the fill colour was blue", pypdf's visitor
says "at y=NNN this text was drawn". Matching needs the page CTM, because
InDesign wraps the body in `q ... cm ... Q`; ignoring it collapses every y into
the transformed frame and nothing matches.

Only used by series profiles that care about colour (see cursiva.py); the
generic converter never imports this.
"""
import re

_OP = re.compile(
    r"(?P<g>(?:[\d.]+\s+){1}g)\s|"
    r"(?P<rg>(?:[\d.]+\s+){3}rg)\s|"
    r"(?P<k>(?:[\d.]+\s+){4}k)\s|"
    r"(?P<scn>(?:[\d.]+\s+){1,4}scn)\s|"
    r"(?P<cm>(?:[-\d.]+\s+){6}cm)\s|"
    r"(?P<Tf>/[^\s/]+\s+[-\d.]+\s+Tf)\s|"
    r"(?P<Tm>(?:[-\d.]+\s+){6}Tm)\s|"
    r"(?P<Td>(?:[-\d.]+\s+){2}Td)\s|(?P<TD>(?:[-\d.]+\s+){2}TD)\s|"
    r"(?P<TL>[-\d.]+\s+TL)\s|(?P<Ts>[-\d.]+\s+Ts)\s|"
    r"(?P<Tstar>T\*)\s|(?P<q>\bq\b)\s|(?P<Q>\bQ\b)\s|"
    r"(?P<tj>\[[^\]]*\]\s*TJ)|(?P<tjs>\([^)]*\)\s*Tj)|"
    r"(?P<quote>'|(?:\d+\s+)?\")"
)


def _num(s):
    return [float(x) for x in re.findall(r"[-\d.]+", s)]


def _is_black(rgb):
    return all(c <= 0.02 for c in rgb)


def _mul(m1, m2):
    """m1 x m2 for 2x3 affine matrices (a,b,c,d,e,f)."""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (a1 * a2 + b1 * c2, a1 * b2 + b1 * d2,
            c1 * a2 + d1 * c2, c1 * b2 + d1 * d2,
            e1 * a2 + f1 * c2 + e2, e1 * b2 + f1 * d2 + f2)


def run_colors(data):
    """-> {page_index: [(device_y, (r,g,b)), ...]} in draw order."""
    from io import BytesIO
    import pypdf
    r = pypdf.PdfReader(BytesIO(data))
    if r.is_encrypted and not r.decrypt(""):
        return {}
    out = {}
    for pno, page in enumerate(r.pages):
        try:
            txt = page.get_contents().get_data().decode("latin-1")
        except Exception:
            continue
        colour = (0.0, 0.0, 0.0)
        ctm = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
        stack = []
        tlm = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)      # text line matrix
        leading = 0.0
        tfs = 1.0                                  # current font size (Tf)
        marks = []
        for m in _OP.finditer(txt):
            k = m.lastgroup
            g = m.group(0)
            if k == "g":
                v = _num(g)[0]
                colour = (v, v, v)
            elif k == "rg":
                colour = tuple(_num(g))
            elif k == "k":
                c, mm, yy, kk = _num(g)
                colour = (1 - min(1, c + kk), 1 - min(1, mm + kk), 1 - min(1, yy + kk))
            elif k == "scn":
                v = _num(g)
                if len(v) == 1:
                    colour = (v[0],) * 3
                elif len(v) == 3:
                    colour = tuple(v)
                elif len(v) == 4:
                    c, mm, yy, kk = v
                    colour = (1 - min(1, c + kk), 1 - min(1, mm + kk), 1 - min(1, yy + kk))
            elif k == "q":
                stack.append(ctm)
            elif k == "Q":
                ctm = stack.pop() if stack else (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
            elif k == "cm":
                ctm = _mul(tuple(_num(g)), ctm)
            elif k == "Tf":
                tfs = _num(g)[0] or 1.0
            elif k == "Tm":
                tlm = tuple(_num(g))
            elif k in ("Td", "TD"):
                tx, ty = _num(g)
                # Td translates the line matrix in TEXT space: the offset is
                # scaled by the matrix itself (InDesign writes "1 Tf" and a
                # "9 0 0 9 ... Tm"), so 1.333 text units = 12 device points.
                tlm = (tlm[0], tlm[1], tlm[2], tlm[3],
                       tlm[4] + tx * tlm[0], tlm[5] + ty * tlm[3])
                if k == "TD":
                    leading = -ty * tlm[3]
            elif k == "TL":
                leading = _num(g)[0] * tlm[3]
            elif k == "Tstar":
                tlm = (tlm[0], tlm[1], tlm[2], tlm[3], tlm[4], tlm[5] - leading)
            elif k in ("tj", "tjs", "quote"):
                dev = _mul(tlm, ctm)
                marks.append((round(dev[5], 2), colour))
        out[pno] = marks
    return out


def colored_mask(data, tolerance=1.5):
    """-> (flat_text, mask) where mask[i] is True if char i was non-black.

    Character-level truth instead of word guessing: a paragraph is "coloured"
    only if nearly every one of its characters came from a coloured run. That
    is what separates a full quotation block from a bibliography entry whose
    title alone is blue."""
    colors = run_colors(data)
    if not colors:
        return "", []
    from io import BytesIO
    import pypdf
    r = pypdf.PdfReader(BytesIO(data))
    if r.is_encrypted and not r.decrypt(""):
        return "", []
    flat, mask = [], []

    for pno, page in enumerate(r.pages):
        marks = colors.get(pno) or []
        if not marks:
            continue
        # A LINE CAN HOLD BOTH BLACK AND BLUE RUNS: consume the marks in draw
        # order instead of asking "which colour does this y have", or a black
        # chunk sitting next to a blue one inherits the blue.
        used = [False] * len(marks)

        def vis(text, cm, tm, fd, fs, _marks=marks, _used=used):
            if not text:
                return
            yy = tm[5]
            coloured = False
            for idx, (y, c) in enumerate(_marks):
                if _used[idx] or abs(y - yy) > tolerance:
                    continue
                _used[idx] = True
                coloured = not _is_black(c)
                break
            t = re.sub(r"\s+", " ", text)
            if not t:
                return
            if flat and flat[-1] != "":
                flat.append(" " if not t.startswith(" ") else "")
                mask.append(coloured)
            flat.append(t)
            mask.extend([coloured] * len(t))

        try:
            page.extract_text(visitor_text=vis)
        except Exception:
            continue
        flat.append(" ")
        mask.append(False)
    return "".join(flat), mask


def colored_text(data, tolerance=1.5):
    """Concatenate the text drawn in a non-black colour, in reading order."""
    colors = run_colors(data)
    if not colors:
        return ""
    from io import BytesIO
    import pypdf
    r = pypdf.PdfReader(BytesIO(data))
    if r.is_encrypted and not r.decrypt(""):
        return ""
    parts = []
    for pno, page in enumerate(r.pages):
        marks = colors.get(pno) or []
        if not marks:
            continue
        keep = []

        def vis(text, cm, tm, fd, fs, _marks=marks, _keep=keep):
            if not text or not text.strip():
                return
            yy = tm[5]
            near = [c for (y, c) in _marks if abs(y - yy) <= tolerance]
            if near and not _is_black(near[-1]):
                _keep.append(text)

        try:
            page.extract_text(visitor_text=vis)
        except Exception:
            continue
        parts.append("".join(keep))
    return re.sub(r"\s+", " ", "".join(parts))

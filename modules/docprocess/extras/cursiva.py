"""cursiva - quirks of the "Cursiva / LECTOR EDITORIAL" course series.

NOT part of the generic converter. Everything here comes from looking at the
real documents, and none of it is safe to apply elsewhere:

  - the cover page (book title, "UNIDAD x", chapter title) and the legal
    notice are paratext, not content;
  - every page carries a running header ("EDITORIAL LECTOR EDITORIAL 1.2 ... CURSIVA")
    and a bare page number, which the converter emits as body text;
  - section titles are all-caps bold runs, not markdown headings;
  - bullet markers are arrows, and the converter sometimes glues the next
    bullet to the end of the previous item.

matches() is deliberately strict: InDesign-produced PDFs of this series always
carry the Montserrat + DMSans-9pt font pair and the Adobe PDF Library producer.
"""
import re

NAME = "cursiva"
STAGE = "post"

_HEADER = re.compile(r"^EDITORIAL\b.{0,80}\bCURSIVA$")
_PAGENUM = re.compile(r"^-\s*\d+\s*-$")
_COPYRIGHT = re.compile(r"^©.*Cursiva.*$")
_LEGAL = re.compile(r"^No se permite la reproducción total o parcial", re.I)
# ALL-CAPS bold run -> real heading: **LA RESEÑA Y LA CRÍTICA**
_CAPS_BOLD = re.compile(r"^\*\*([A-ZÁÉÍÓÚÜÑ0-9][A-ZÁÉÍÓÚÜÑ0-9 \.,;:¡!?¿()\-—«»]{3,})\*\*$")
_UNIT = re.compile(r"^##\s+UNIDAD\s+([0-9][0-9.]*)\s*$")
_ARROW_LINE = re.compile(r"^→\s*")


def matches(ctx):
    """Only this series: InDesign + Adobe PDF Library + the two font families."""
    if not ctx or not ctx.get("fonts"):
        return False
    fonts = " ".join(ctx["fonts"])
    return ("Montserrat-" in fonts and "DMSans-9pt" in fonts
            and "Adobe PDF Library" in ctx.get("producer", "")
            and "InDesign" in ctx.get("creator", ""))


def _is_noise(line):
    s = line.strip()
    return bool(_HEADER.match(s) or _PAGENUM.match(s)
                or _COPYRIGHT.match(s) or _LEGAL.match(s))


def _promote_caps_heading(line):
    m = _CAPS_BOLD.match(line.strip())
    return f"## {m.group(1).strip()}" if m else line


def _normalise_title(lines):
    """'# BOOK' + '## UNIDAD 1.2' + '## Chapter' -> '# UNIDAD 1.2 - Chapter'."""
    unit_idx = next((i for i, l in enumerate(lines) if _UNIT.match(l.strip())), None)
    if unit_idx is None:
        return lines
    unit = _UNIT.match(lines[unit_idx].strip()).group(1)
    topic = ""
    for j in range(unit_idx + 1, min(unit_idx + 4, len(lines))):
        if lines[j].startswith("## "):
            topic = lines[j][3:].strip()
            break
    out = list(lines)
    for idx in sorted([i for i in (0, unit_idx) if i < len(out)], reverse=True):
        del out[idx]
    if topic:
        k = next((i for i, l in enumerate(out) if l.startswith("## ") and
                  l[3:].strip() == topic), None)
        if k is not None:
            del out[k]
        out.insert(0, f"# UNIDAD {unit} - {topic}")
    else:
        out.insert(0, f"# UNIDAD {unit}")
    return out


def _norm(s):
    s = re.sub(r"\*+", "", s)
    s = re.sub(r"[“”\"«»]", "", s)
    return re.sub(r"\s+", " ", s).strip().lower()


COLOUR_COVERAGE = 0.85        # a quoted paragraph is coloured almost end to end


def _flat_with_mask(pdf_bytes):
    """Normalise the coloured mask the same way _norm() normalises the md."""
    from . import _pdfcolor
    raw, mask = _pdfcolor.colored_mask(pdf_bytes)
    chars, flags, prev_space = [], [], True
    for ch, m in zip(raw, mask):
        if ch in "“”\"«»*":
            continue
        if ch.isspace():
            if prev_space:
                continue
            chars.append(" ")
            flags.append(m)
            prev_space = True
        else:
            chars.append(ch.lower())
            flags.append(m)
            prev_space = False
    return "".join(chars), flags


def _mark_coloured_quotes(text, pdf_bytes):
    """Wrap paragraphs painted in a non-black colour in '> '.

    The decision is made per CHARACTER (see _pdfcolor.colored_mask): a
    paragraph counts as a quotation only if >= 85% of its characters came from
    coloured runs. Extremes alone are not enough - a bibliography entry whose
    title is blue would otherwise qualify."""
    try:
        flat, flags = _flat_with_mask(pdf_bytes)
    except Exception:
        return text
    if len(flat) < 200:
        return text
    out, n = [], 0
    for blk in text.split("\n\n"):
        body = " ".join(l for l in blk.split("\n") if not l.lstrip().startswith(("#", ">", "-")))
        needle = _norm(body)
        if len(needle) < 60:
            out.append(blk)
            continue
        head, tail = needle[:50], needle[-50:]
        i = flat.find(head)
        j = flat.find(tail, i) if i >= 0 else -1
        if i < 0 or j < 0:
            out.append(blk)
            continue
        span = flags[i:j + len(tail)]
        if span and sum(span) / len(span) >= COLOUR_COVERAGE:
            out.append("\n".join("> " + l if l.strip() else l for l in blk.split("\n")))
            n += 1
        else:
            out.append(blk)
    return "\n\n".join(out)


def transform(markdown, ctx):
    """Strip paratext, promote all-caps titles, normalise arrow bullets."""
    lines = []
    for line in markdown.split("\n"):
        if _is_noise(line):
            continue
        lines.append(_promote_caps_heading(line))

    # the converter can emit the same heading twice (page header picked up as
    # a heading); collapse only IDENTICAL adjacent headings
    deduped, last_heading = [], ""
    for line in lines:
        s = line.strip()
        if s.startswith("#"):
            if s == last_heading:          # blank lines may sit between them
                continue
            last_heading = s
        deduped.append(line)
    lines = deduped

    lines = _normalise_title(lines)

    # a heading whose second line came out as a lowercase bold run belongs to
    # the same heading: "## La no ficción: qué es y cómo" + "**debemos evaluar**"
    joined = []
    for line in lines:
        m = re.match(r"^\*\*([a-záéíóúüñ][^*]*)\*\*$", line.strip())
        k = next((i for i in range(len(joined) - 1, -1, -1) if joined[i].strip()), None)
        if (m and k is not None and joined[k].startswith("## ")
                and not joined[k].rstrip().endswith((".", ":", "?", "!"))):
            joined[k] = joined[k].rstrip() + " " + m.group(1).strip()
            del joined[k + 1:]              # drop the blank lines between them
            continue
        joined.append(line)
    lines = joined

    out = [_ARROW_LINE.sub("- → ", line) for line in lines]

    # squeeze the blank-line runs the removals leave behind
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text).strip("\n") + "\n"

    if ctx.get("pdf_bytes"):
        text = _mark_coloured_quotes(text, ctx["pdf_bytes"])
    return text

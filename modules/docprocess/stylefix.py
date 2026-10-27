"""stylefix - make a PDF tell the truth about bold/italic before extraction.

WHY THIS EXISTS
---------------
xberg decides emphasis from the font NAME and the descriptor weight. Publisher
PDFs made with InDesign + variable fonts embed several *instances* of the same
family (regular / heavier / italic) but keep the SAME PostScript name
("DMSans-9ptRegular") and the SAME descriptor (FontWeight 400, StemV 84):
only the glyph outlines differ. xberg therefore cannot tell emphasis runs from
body text and prints them plain. Proven on three Cursiva units: the in-body
bold runs and the blue "italic" titles were all lost; genuine Montserrat-Bold
(weight 700) survived.

WHAT WE DO
----------
1. For every embedded TrueType program, measure things the metadata cannot
   fake: INK (filled area of a few glyphs, normalised by em^2) and SLANT
   (geometric slope of a stem glyph, plus post.italicAngle).
2. Compare against the median ink of all programs in the document (a per-page
   reference is unstable: two near-identical regular instances are 1-2% apart).
   Emphasis sits at ~1.45x here, so a 1.15 threshold has a wide margin.
3. Rewrite ONLY the lying metadata: /BaseFont ...Regular -> ...Bold, plus
   /FontWeight 700 and the ForceBold flag. xberg's own name-based logic then
   labels the runs correctly, with no change to xberg.

SAFETY
------
Never breaks a conversion: any failure (non-PDF, encrypted without the empty
user password, unparsable font, exotic CFF program) returns the ORIGINAL bytes
untouched. Scans without fonts are skipped. Italic programs here are already
named *Italic*, so only bold names are patched by default.
"""
import hashlib
import statistics
import struct
from io import BytesIO

SAMPLE_CHARS = "onaeilmr"      # body letters, strong ink signal
STEM_CHARS = "lI"              # near-vertical stems: slant probe
BOLD_RATIO = 1.15              # measured: emphasis ~1.41-1.50x the median
SLANT_ITALIC = 0.06            # radians-ish slope of a stem
ALREADY_BOLD = ("bold", "black", "heavy", "semibold", "demibold")


# --------------------------------------------------------------------------
#  Minimal TrueType reader (no third-party deps)
# --------------------------------------------------------------------------
def _u16(b, o): return struct.unpack(">H", b[o:o + 2])[0]
def _s16(b, o): return struct.unpack(">h", b[o:o + 2])[0]
def _u32(b, o): return struct.unpack(">I", b[o:o + 4])[0]


def _tables(data):
    if data[:4] == b"ttcf":
        raise ValueError("TTC not supported")
    if data[:4] not in (b"\x00\x01\x00\x00", b"true", b"OTTO"):
        raise ValueError("not a sfnt")
    n = _u16(data, 4)
    out = {}
    for i in range(n):
        rec = 12 + i * 16
        out[data[rec:rec + 4]] = (_u32(data, rec + 8), _u32(data, rec + 12))
    return out


def _cmap_lookup(data, t, code):
    """char code -> glyph id, honouring the font's own (subset) cmap.

    Subset fonts renumber glyphs, so every metric must be keyed by CHARACTER;
    that is what makes two different subsets comparable."""
    if b"cmap" not in t:
        return None
    off = t[b"cmap"][0]
    ntab = _u16(data, off + 2)
    best = None
    for i in range(ntab):
        rec = off + 4 + i * 8
        sub = _u32(data, rec + 4)
        fmt = _u16(data, off + sub)
        if fmt == 4 and best is None:
            best = off + sub
        elif fmt in (0, 6) and best is None:
            best = off + sub
    if best is None:
        return None
    fmt = _u16(data, best)
    if fmt == 0:
        return data[best + 6 + code] or None if code < 256 else None
    if fmt == 6:
        first, cnt = _u16(data, best + 6), _u16(data, best + 8)
        if first <= code < first + cnt:
            return _u16(data, best + 10 + (code - first) * 2) or None
        return None
    if fmt != 4:
        return None
    segx2 = _u16(data, best + 6)
    seg = segx2 // 2
    end = best + 14
    start = end + segx2 + 2
    delta = start + segx2
    rng = delta + segx2
    for i in range(seg):
        e = _u16(data, end + i * 2)
        if code > e:
            continue
        s = _u16(data, start + i * 2)
        if code < s:
            return None
        d = _s16(data, delta + i * 2)
        ro = _u16(data, rng + i * 2)
        if ro == 0:
            return ((code + d) & 0xFFFF) or None
        idx = rng + i * 2 + ro + (code - s) * 2
        if idx + 1 >= len(data):
            return None
        g = _u16(data, idx)
        return ((g + d) & 0xFFFF) or None
    return None


def _glyph_span(data, t, gid):
    if b"loca" not in t or b"glyf" not in t:
        return None
    long_loca = _s16(data, t[b"head"][0] + 50) == 1
    loca = t[b"loca"][0]
    if long_loca:
        a, b = _u32(data, loca + gid * 4), _u32(data, loca + gid * 4 + 4)
    else:
        a = _u16(data, loca + gid * 2) * 2
        b = _u16(data, loca + gid * 2 + 2) * 2
    if a == b:
        return None
    return t[b"glyf"][0] + a, b - a


def _contours(data, goff):
    n = _s16(data, goff)
    if n < 0:
        return None                     # composite glyph: skipped by design
    p = goff + 10
    ends = [_u16(data, p + i * 2) for i in range(n)]
    p += n * 2
    p += 2 + _u16(data, p)              # skip instructions
    npts = (ends[-1] + 1) if ends else 0
    flags = []
    while len(flags) < npts:
        f = data[p]; p += 1
        flags.append(f)
        if f & 0x08:                    # REPEAT
            flags.extend([f] * data[p]); p += 1
    flags = flags[:npts]
    xs, cur = [], 0
    for f in flags:
        if f & 0x02:                    # X_SHORT: 1 byte, sign from bit 4
            v = data[p]; p += 1
            dx = v if (f & 0x10) else -v
        elif f & 0x10:                  # X_IS_SAME
            dx = 0
        else:
            dx = _s16(data, p); p += 2
        cur += dx
        xs.append(cur)
    ys, cur = [], 0
    for f in flags:
        if f & 0x04:
            v = data[p]; p += 1
            dy = v if (f & 0x20) else -v
        elif f & 0x20:
            dy = 0
        else:
            dy = _s16(data, p); p += 2
        cur += dy
        ys.append(cur)
    out, s = [], 0
    for e in ends:
        out.append([(xs[i], ys[i], flags[i] & 1) for i in range(s, e + 1)])
        s = e + 1
    return out


def _flatten(contour):
    """TrueType quadratic contour -> polygon (sampled)."""
    if len(contour) < 2:
        return []
    k = next((i for i, p in enumerate(contour) if p[2]), None)
    if k is None:
        a, b = contour[0], contour[1]
        contour = [((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, 1)] + contour
        k = 0
    seq = contour[k:] + contour[:k]
    pts, cur, i = [seq[0][:2]], seq[0][:2], 1
    while i <= len(seq):
        p = seq[i % len(seq)]
        if p[2]:
            pts.append(p[:2]); cur = p[:2]; i += 1
        else:
            nxt = seq[(i + 1) % len(seq)]
            if not nxt[2]:
                mid = ((p[0] + nxt[0]) / 2, (p[1] + nxt[1]) / 2)
                for t in (0.33, 0.66):
                    pts.append(((1 - t) ** 2 * cur[0] + 2 * (1 - t) * t * p[0] + t * t * mid[0],
                                (1 - t) ** 2 * cur[1] + 2 * (1 - t) * t * p[1] + t * t * mid[1]))
                pts.append(mid); cur = mid; i += 1
            else:
                for t in (0.33, 0.66):
                    pts.append(((1 - t) ** 2 * cur[0] + 2 * (1 - t) * t * p[0] + t * t * nxt[0],
                                (1 - t) ** 2 * cur[1] + 2 * (1 - t) * t * p[1] + t * t * nxt[1]))
                cur = nxt[:2]; pts.append(cur); i += 2
    return pts


def _signed_area(poly):
    s = 0.0
    for i in range(len(poly)):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % len(poly)]
        s += x1 * y2 - x2 * y1
    return s / 2.0


def _glyph_ink(contours):
    """True ink: outer area minus counters (TrueType winding cancels holes)."""
    areas = [_signed_area(_flatten(c)) for c in contours]
    signed, naive = abs(sum(areas)), sum(abs(a) for a in areas)
    return signed if signed > 0.2 * naive else naive


def fingerprint(data):
    """Per-character ink, stem slant, and the (untrustworthy) declared metrics."""
    t = _tables(data)
    upem = _u16(data, t[b"head"][0] + 18) or 1000
    per_char, slant = {}, None
    for ch in SAMPLE_CHARS:
        gid = _cmap_lookup(data, t, ord(ch))
        span = _glyph_span(data, t, gid) if gid is not None else None
        cs = _contours(data, span[0]) if span else None
        if cs:
            per_char[ch] = _glyph_ink(cs) / (upem ** 2)
    for ch in STEM_CHARS:
        gid = _cmap_lookup(data, t, ord(ch))
        span = _glyph_span(data, t, gid) if gid is not None else None
        cs = _contours(data, span[0]) if span else None
        if not cs:
            continue
        pts = [p for c in cs for p in c]
        ys = [p[1] for p in pts]
        y0, y1 = min(ys), max(ys)
        h = y1 - y0
        if h <= 0:
            continue
        top = [p[0] for p in pts if p[1] > y0 + 0.7 * h]
        bot = [p[0] for p in pts if p[1] < y0 + 0.3 * h]
        if top and bot:
            slant = (sum(top) / len(top) - sum(bot) / len(bot)) / (0.4 * h)
            break
    post_angle = None
    if b"post" in t:
        post_angle = struct.unpack(">i", data[t[b"post"][0] + 4:t[b"post"][0] + 8])[0] / 65536.0
    return {"per_char": per_char, "slant": slant, "post_angle": post_angle,
            "has_glyf": b"glyf" in t}


def _is_italic(fp):
    if fp["post_angle"] is not None and abs(fp["post_angle"]) > 0.5:
        return True
    return fp["slant"] is not None and abs(fp["slant"]) > SLANT_ITALIC


# --------------------------------------------------------------------------
#  PDF side
# --------------------------------------------------------------------------
def _font_program(font):
    d = font.get("/FontDescriptor")
    d = d.get_object() if d else {}
    if not d and font.get("/Subtype") == "/Type0":
        desc = font.get("/DescendantFonts")
        if desc:
            d0 = desc[0].get_object().get("/FontDescriptor")
            d = d0.get_object() if d0 else {}
    for k in ("/FontFile2", "/FontFile3", "/FontFile"):
        ff = d.get(k)
        if ff:
            return ff.get_object().get_data()
    return None


def honest_styles(data, log=lambda m: None, patch_italic=False):
    """Return PDF bytes whose font metadata no longer lies about weight/style."""
    try:
        from pypdf import PdfReader, PdfWriter
        from pypdf.generic import NameObject, NumberObject
    except Exception as e:                                   # pragma: no cover
        log(f"stylefix: pypdf unavailable ({e})")
        return data
    try:
        r = PdfReader(BytesIO(data))
        if r.is_encrypted and not r.decrypt(""):
            log("stylefix: encrypted, cannot patch in place - using raw bytes")
            return data

        # 1. fingerprint every embedded program (cached by program bytes)
        cache, entries = {}, []          # entries: (page_idx, res_name, fp, idnum)
        for i, page in enumerate(r.pages):
            fonts = page.get("/Resources", {}).get("/Font") or {}
            for n, ref in fonts.items():
                f = ref.get_object()
                prog = _font_program(f)
                if not prog:
                    continue
                key = hashlib.md5(prog).hexdigest()
                if key not in cache:
                    try:
                        cache[key] = fingerprint(prog)
                    except Exception:
                        cache[key] = None
                fp = cache[key]
                if not fp or not fp["per_char"]:
                    continue
                entries.append((i, n, fp, str(f.get("/BaseFont")), ref.idnum))
        if not entries:
            log("stylefix: no embedded TrueType programs - nothing to classify")
            return data

        # 2. document-wide baseline (chars present in every program)
        common = set.intersection(*(set(e[2]["per_char"]) for e in entries))
        inks = [statistics.mean(e[2]["per_char"][c] for c in (common or e[2]["per_char"]))
                for e in entries]
        med = statistics.median(inks)
        if not med:
            return data

        # 3. verdicts
        bold, italic = [], []
        for i, n, fp, declared, idnum in entries:
            chars = common or set(fp["per_char"])
            ratio = statistics.mean(fp["per_char"][c] for c in chars) / med
            low = declared.lower()
            if ratio >= BOLD_RATIO and not any(b in low for b in ALREADY_BOLD):
                bold.append((i, n, declared, idnum, ratio))
            elif patch_italic and _is_italic(fp) and "italic" not in low and "oblique" not in low:
                italic.append((i, n, declared, idnum))
        if not bold and not italic:
            log(f"stylefix: nothing to patch (median ink {med:.4f})")
            return data

        # 4. rewrite: same document, only the lying metadata changes.
        # Keep the document information dictionary: pypdf writes its own
        # Producer otherwise, which destroys the only cheap way to identify a
        # publishing workflow (and would break series profiles that match on it).
        w = PdfWriter()
        for p in r.pages:
            w.add_page(p)
        try:
            if r.metadata:
                w.add_metadata({str(k): str(v) for k, v in r.metadata.items() if v is not None})
        except Exception as e:
            log(f"stylefix: metadata could not be preserved ({e})")
        done, patched = set(), 0
        for i, n, declared, idnum, ratio in bold:
            if idnum in done:
                continue
            done.add(idnum)
            try:
                font = w.pages[i]["/Resources"]["/Font"][n].get_object()
                new = declared.replace("Regular", "Bold")
                font[NameObject("/BaseFont")] = NameObject(new)
                fd = font.get("/FontDescriptor")
                if fd is not None:
                    fd = fd.get_object()
                    fd[NameObject("/FontWeight")] = NumberObject(700)
                    fd[NameObject("/Flags")] = NumberObject(int(fd.get("/Flags", 0)) | (1 << 18))
                patched += 1
            except Exception as e:
                log(f"stylefix: could not patch {n} on page {i+1} ({e})")
        for i, n, declared, idnum in italic:
            if idnum in done:
                continue
            done.add(idnum)
            try:
                font = w.pages[i]["/Resources"]["/Font"][n].get_object()
                font[NameObject("/BaseFont")] = NameObject(declared.replace("Regular", "Italic"))
                patched += 1
            except Exception as e:
                log(f"stylefix: could not patch italic {n} ({e})")

        if not patched:
            return data
        buf = BytesIO()
        w.write(buf)
        log(f"stylefix: {patched} font program(s) relabelled "
            f"(median ink {med:.4f}, bold >= x{BOLD_RATIO})")
        return buf.getvalue()
    except Exception as e:
        log(f"stylefix: skipped ({type(e).__name__}: {str(e)[:120]})")
        return data

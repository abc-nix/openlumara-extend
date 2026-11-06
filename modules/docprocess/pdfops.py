"""pdfops - PDF structure operations for docprocess. pypdf only (core dep)."""
import math
import re
from io import BytesIO

_LIVE_BYTES_PER_PX = 12.0  # observed 11.6-12.0 in xberg SecurityErrors
_RASTER_LIMIT_DEFAULT = 100 * 1024 * 1024


def audit_pdf(data, npages=None):
    """Compare MediaBox vs CTM-at-Do vs image px per page.

    Returns (rows, ok). Fail-CLOSED by design: unreadable page, no CTM match
    or coverage < 0.98 -> not ok. (A silent fallback once false-passed a
    cropped PDF and cost a whole debugging saga; never fail open.)
    Note: cov<0.98 is EXPECTED on cropbox-normalized pages - audit raw bytes.
    """
    from pypdf import PdfReader
    rows, ok = [], True
    try:
        r = PdfReader(BytesIO(data))
    except Exception as e:
        return ([{"page": 0, "verdict": f"UNREADABLE: {e}"}], False)
    if getattr(r, "is_encrypted", False):
        # pypdf needs its "cryptography" extra for AES; xberg decrypts natively.
        return ([{"page": 0, "verdict": "ENCRYPTED-audit skipped"}], True)
    for i, p in enumerate(r.pages):
        if npages and i >= npages:
            break
        try:
            mb = p["/MediaBox"]
            cw, ch = float(mb[2]) - float(mb[0]), float(mb[3]) - float(mb[1])
            c = p.get_contents()
            cdata = (b"" if c is None else
                     c.get_data() if hasattr(c, "get_data")
                     else b"".join(x.get_object().get_data() for x in c))
            for k, xo in (p["/Resources"].get("/XObject") or {}).items():
                o = xo.get_object()
                if o.get("/Subtype") != "/Image":
                    continue
                W, H = int(o["/Width"]), int(o["/Height"])
                # allow graphics-state ops between cm and /Name Do (Canon: 'cm 1 g /x Do')
                m = re.search(rb"([-\d.]+) ([-\d.]+) ([-\d.]+) ([-\d.]+) "
                              rb"([-\d.]+) ([-\d.]+) cm\b[^D]{0,64}/" + k[1:].encode(),
                              cdata)
                if not m:
                    rows.append({"page": i + 1, "verdict": "NO-CTM-MATCH", "ok": False})
                    ok = False
                    continue
                a, b, cc, d = (float(m.group(j)) for j in range(1, 5))
                sx, sy = math.hypot(a, b), math.hypot(cc, d)  # CTM a/d ARE drawn pt-size
                cov = min(1.0, cw / sx) * min(1.0, ch / sy)
                good = cov >= 0.98
                rows.append({"page": i + 1, "box": f"{cw:.0f}x{ch:.0f}pt",
                             "img": f"{W}x{H}", "drawn": f"{sx:.0f}x{sy:.0f}pt",
                             "cov": round(cov, 3), "dpi": round(72 * W / max(sx, 1e-9)),
                             "ok": good})
                ok = ok and good
        except Exception as e:
            rows.append({"page": i + 1, "verdict": f"AUDIT-ERROR {e}", "ok": False})
            ok = False
    return rows, ok


def normalize_cropbox(data, log=lambda m: None):
    """MediaBox := CropBox for copier spread scans (xberg rasterizes MediaBox
    and ignores CropBox). Any error/encryption -> original bytes untouched."""
    try:
        from pypdf import PdfReader, PdfWriter
        from pypdf.generic import NameObject

        reader = PdfReader(BytesIO(data))
        if getattr(reader, "is_encrypted", False):
            return data

        def dims(box):
            return [float(x) for x in box] if box else None

        it = iter(reader.pages)
        first = next(it, None)
        if first is None:
            return data
        if dims(first.get("/CropBox")) == dims(first.get("/MediaBox")) or not first.get("/CropBox"):
            return data
        writer = PdfWriter()
        # pypdf's writer defaults /Producer to "pypdf" and drops /Creator, and
        # the series profiles identify a document by producer/creator: keep the
        # original metadata or every matches() silently turns false after a
        # cropbox trim.
        try:
            meta = {k: v for k, v in (reader.metadata or {}).items()
                    if isinstance(v, str)}
            if meta:
                writer.add_metadata(meta)
        except Exception:
            pass
        pages = [first] + list(it)
        fixed = 0
        for p in pages:
            crop = p.get("/CropBox")
            if crop and dims(crop) != dims(p.get("/MediaBox")):
                p[NameObject("/MediaBox")] = crop
                p[NameObject("/CropBox")] = crop
                fixed += 1
            writer.add_page(p)
        if not fixed:
            return data
        out = BytesIO()
        writer.write(out)
        log(f"cropbox: {fixed} pages trimmed to visible area")
        return out.getvalue()
    except Exception:
        return data


def raster_plan(data, base_dpi=300, budget_mb=100, log=lambda m: None):
    """Compute worst-page raster cost before extraction. xberg caps the
    DECODED working set of a single page raster at SecurityLimits
    .max_content_size (default 100MB); live bytes ~= 12 * box_px^2.
    `budget_mb` (module setting max_page_raster_mb) IS that cap as chosen:
      - needs fit in 0.96*budget -> keep base dpi
      - over -> lower dpi (never below 150) until it fits the budget
      - would drop below 150 -> safety valve: raise cap to exact need * 1.15
        so extraction never hard-fails
      - budget != default 100 -> set the xberg cap explicitly (up OR down)
    Returns None for unreadable PDFs (xberg may still handle them)."""
    from pypdf import PdfReader
    try:
        r = PdfReader(BytesIO(data))
    except Exception:
        return None
    if getattr(r, "is_encrypted", False):
        # pypdf cannot touch encrypted page objects (no cryptography extra installed);
        # extraction itself still works - xberg decrypts natively.
        log("raster plan skipped (encrypted PDF)")
        return None
    worst_px, worst_page = 0.0, 0
    for i, p in enumerate(r.pages):
        try:
            mb = p["/MediaBox"]
            pt2 = (float(mb[2]) - float(mb[0])) * (float(mb[3]) - float(mb[1]))
            px = pt2 * (base_dpi / 72.0) ** 2
            if px > worst_px:
                worst_px, worst_page = px, i + 1
        except Exception:
            continue
    live = worst_px * _LIVE_BYTES_PER_PX
    budget = max(1.0, float(budget_mb)) * 1048576
    custom = abs(float(budget_mb) - _RASTER_LIMIT_DEFAULT / 1048576) > 1e-9
    out = {"worst_page": worst_page, "worst_px": int(worst_px),
           "live_bytes_at_base": int(live), "dpi": base_dpi, "limit_set": 0}
    if live <= 0.96 * budget:
        if custom:
            out["limit_set"] = int(budget)  # enforce the user's cap (up or down)
            log(f"raster plan: worst p{worst_page} {int(live/1e6)}MB fits "
                f"{float(budget_mb):g}MB budget")
        else:
            log(f"raster plan: worst p{worst_page} fits default 100MB limit")
        return out
    budget_px = 0.96 * budget / _LIVE_BYTES_PER_PX
    dpi_alt = int(base_dpi * math.sqrt(budget_px / worst_px))
    if dpi_alt >= 150:
        out["dpi"] = dpi_alt
        if custom:
            need_alt = live * (dpi_alt / base_dpi) ** 2
            out["limit_set"] = int(max(budget, need_alt * 1.02))
        log(f"raster plan: worst p{worst_page} needs {int(live/1e6)}MB; "
            f"lowering target_dpi {base_dpi}->{dpi_alt} to fit "
            f"{float(budget_mb):g}MB")
        return out
    need = int(live * 1.15)
    log(f"raster plan: worst p{worst_page} needs {int(live/1e6)}MB; "
        f"raising SecurityLimits to {need >> 20}MB")
    out["limit_set"] = need
    return out

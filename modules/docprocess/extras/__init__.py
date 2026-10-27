"""extras - optional, document-series-specific profiles for docprocess.

This package is deliberately NOT part of the generic converter. It holds the
quirks of one publisher or one course series (running headers, cover pages,
coloured citation blocks, bullet conventions...) that cannot be generalised
and must not ship with the public module. Remove the folder - or set the
`extras` setting to false - and docprocess behaves exactly as before.

A profile is any module inside this package that exposes:

    NAME = "cursiva"                  stable id, shown in the log
    STAGE = "post"                    optional: "pre" (raw converter text) or
                                      "post" (after the generic repairs)
    def matches(ctx) -> bool          must be cheap AND strict: it decides
                                      whether this document belongs to the
                                      series. Never "return True".
    def transform(markdown, ctx) -> str

`ctx` is a plain dict: pdf_bytes, pdf_text, producer, creator, fonts, config.
Profiles are isolated: an import error, a raising matches() or a broken
transform() is logged and skipped, never propagated.
"""
import importlib
import pkgutil

PROFILES = []


def document_info(pdf_bytes):
    """Cheap identification of a PDF: producer/creator and embedded font names.

    Used by matches(); returns None when the document cannot be read or has no
    text layer, which is exactly the case where series profiles must not act."""
    try:
        from io import BytesIO
        import pypdf
        r = pypdf.PdfReader(BytesIO(pdf_bytes))
        if r.is_encrypted and not r.decrypt(""):
            return None
        meta = r.metadata or {}
        fonts = set()
        for page in r.pages:
            for _n, ref in (page.get("/Resources", {}).get("/Font") or {}).items():
                try:
                    fonts.add(str(ref.get_object().get("/BaseFont", ""))[7:])
                except Exception:
                    pass
        # NOTE: no text extraction here on purpose - matches() must stay cheap.
        # An empty font set is the scan/no-text-layer signal a profile needs.
        return {"producer": str(meta.get("/Producer", "")),
                "creator": str(meta.get("/Creator", "")),
                "fonts": sorted(f for f in fonts if f),
                "pages": len(r.pages)}
    except Exception:
        return None


def load(log=lambda m: None):
    """Import every profile submodule of this package and keep the valid ones."""
    global PROFILES
    found = []
    for m in pkgutil.iter_modules(__path__):
        if m.name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"{__name__}.{m.name}")
        except Exception as e:
            log(f"extras: profile '{m.name}' could not be imported ({e})")
            continue
        if all(hasattr(mod, a) for a in ("NAME", "matches", "transform")):
            found.append(mod)
        else:
            log(f"extras: '{m.name}' ignored (needs NAME, matches and transform)")
    PROFILES = found
    if found:
        log("extras: loaded " + ", ".join(p.NAME for p in found))
    return found


def apply(stage, text, ctx, log=lambda m: None, profiles=None):
    """Run every profile for this stage whose matches() accepts the document."""
    for p in (PROFILES if profiles is None else profiles):
        if getattr(p, "STAGE", "post") != stage:
            continue
        try:
            if not p.matches(ctx):
                continue
        except Exception as e:
            log(f"extras: {p.NAME} matches() failed ({e}) - skipped")
            continue
        try:
            new = p.transform(text, ctx)
            if isinstance(new, str) and new.strip():
                text = new
            log(f"extras: {p.NAME} applied")
        except Exception as e:
            log(f"extras: {p.NAME} failed ({type(e).__name__}: {str(e)[:120]}) - left untouched")
    return text

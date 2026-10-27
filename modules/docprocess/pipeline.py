"""pipeline - extraction engine for docprocess. stdlib + xberg only, NO core
imports (log= callbacks) so offline test harnesses can import it directly."""
import asyncio
import time

QUALITY_FLOOR = 0.75
PADDLE_SEC_PER_PAGE = 4.8   # measured Oct 15: Ortografia 29pg 138.8s
VLM_SEC_PER_PAGE = 3.5      # measured Oct 15: max-res 3.4s/pg warm
VLM_MODEL_LOAD_SEC = 10.0

GENERIC_PROMPT = ("Extract all text from this scanned page as Markdown. "
                  "Preserve layout order; render tables as markdown pipe tables.")


def vlm_prompt_for(model, custom=""):
    """PaddleOCR-VL-1.5 needs the canonical 'OCR:' (generic prompt loops it);
    GLM-OCR and others take the markdown-extraction prompt."""
    if custom:
        return custom
    m = (model or "").lower()
    if "paddle" in m and "1.5" in m:
        return "OCR:"
    return GENERIC_PROMPT


def _ocr_tier_config(mode, languages):
    from xberg import (OcrConfig, OcrPipelineConfig, OcrPipelineStage,
                       OcrQualityThresholds, PaddleOcrConfig, TesseractConfig)
    if mode == "off":
        return OcrConfig(enabled=False)
    if mode == "paddle":
        stage = OcrPipelineStage(
            backend="paddleocr", priority=1,
            paddle_ocr_settings=PaddleOcrConfig(language="latin",
                                                enable_table_detection=True))
    else:  # tesseract
        stage = OcrPipelineStage(
            backend="tesseract", priority=1, language=list(languages),
            tesseract_config=TesseractConfig(enable_table_detection=False))
    return OcrConfig(language=list(languages),
                     pipeline=OcrPipelineConfig(
                         stages=[stage],
                         quality_thresholds=OcrQualityThresholds()))


def build_config(mode, timeout=240, languages=("spa",), force_ocr=False,
                 layout_tables=True, vlm=None, images=None, plan=None):
    """mode: off|tesseract|paddle|vlm. All xberg config classes are FROZEN
    dataclasses: constructor kwargs only, never attribute assignment.
    images: dict(target_dpi, max_px, extract, placeholders) ->
    ImageExtractionConfig governing the page rasters xberg produces."""
    from xberg import ExtractionConfig
    if mode == "vlm":
        from xberg import LlmConfig, OcrConfig
        ocr = OcrConfig(
            backend="vlm",
            vlm_config=LlmConfig(model=vlm["model"], base_url=vlm["base_url"],
                                 max_tokens=int(vlm.get("max_tokens", 4000)),
                                 timeout_secs=180),
            vlm_prompt=vlm_prompt_for(vlm["model"], vlm.get("prompt", "")))
        cfg_kwargs = dict(output_format="markdown",
                          extraction_timeout_secs=int(timeout), ocr=ocr,
                          force_ocr=force_ocr)
    else:
        cfg_kwargs = dict(output_format="markdown",
                          extraction_timeout_secs=int(timeout),
                          force_ocr=force_ocr, ocr=_ocr_tier_config(mode, languages))
        if mode == "paddle" and layout_tables:
            from xberg import LayoutDetectionConfig
            cfg_kwargs["layout"] = LayoutDetectionConfig(strategy="always",
                                                         table_model="tatr")
    if images:
        from xberg import ImageExtractionConfig
        ikw = dict(target_dpi=int(images.get("target_dpi", 300)),
                   auto_adjust_dpi=bool(images.get("auto_adjust", False)),
                   extract_images=bool(images.get("extract", True)),
                   inject_placeholders=bool(images.get("placeholders", True)))
        if images.get("max_px"):
            ikw["max_image_dimension"] = int(images["max_px"])
        cfg_kwargs["images"] = ImageExtractionConfig(**ikw)
    if plan and plan.get("limit_set"):
        from xberg import SecurityLimits
        cfg_kwargs["security_limits"] = SecurityLimits(
            max_content_size=plan["limit_set"])
    return ExtractionConfig(**cfg_kwargs)


def summarize(doc, t0):
    usage = []
    for u in (getattr(doc, "llm_usage", None) or []):
        usage.append({"in": getattr(u, "input_tokens", 0) or 0,
                      "out": getattr(u, "output_tokens", 0) or 0,
                      "stop": str(getattr(u, "finish_reason", None)
                                  or getattr(u, "stop_reason", None) or "?")})
    conf = getattr(doc, "extraction_confidence", None)
    return {"markdown": str(getattr(doc, "content", "") or ""),
            "chars": len(str(getattr(doc, "content", "") or "")),
            "method": str(getattr(doc, "extraction_method", "?")),
            "quality": float(getattr(doc, "quality_score", 0) or 0),
            "coverage": getattr(conf, "text_coverage", None),
            "ocr_failures": len(list(getattr(doc, "ocr_page_failures", None) or [])),
            "usage": usage,
            "secs": round(time.monotonic() - t0, 2)}


async def extract_pdf(data, mode, timeout, languages, force_ocr,
                      vlm=None, images=None, plan=None, layout_tables=True,
                      log=lambda m: None):
    from xberg import ExtractInput, extract
    inp = ExtractInput(kind="bytes", bytes=data, mime_type="application/pdf")
    cfg = build_config(mode, timeout, languages, force_ocr, layout_tables,
                       vlm, images, plan)
    t0 = time.monotonic()
    out = await asyncio.wait_for(extract(inp, cfg), timeout=timeout + 5)
    docs = getattr(out, "results", None) or []
    if not docs:
        raise RuntimeError(f"xberg returned no documents (mode={mode})")
    res = summarize(docs[0], t0)
    log(f"tier {mode}: {res['chars']}ch method={res['method']} "
        f"q={res['quality']:.3f} {res['secs']}s")
    return res


def vlm_watchdog(usage, log=lambda m: None):
    """'length' stops = cut off mid-page (small ctx) or looping model - the
    only machine-readable truncation signal xberg exposes."""
    if not usage:
        return
    stops = {}
    for u in usage:
        stops[u["stop"]] = stops.get(u["stop"], 0) + 1
    tok = sum(u["in"] + u["out"] for u in usage)
    log(f"vlm usage: {len(usage)} calls, {tok} tokens, stops={stops}")
    if stops.get("length"):
        log(f"vlm WATCHDOG: {stops['length']}/{len(usage)} pages hit the output "
            f"cap -> TRUNCATED. Raise the model's context (-c 32768 in "
            f"llama-swap) or lower vlm_max_px/target_dpi.")


async def extract_ladder(data, timeout, languages, force_ocr,
                         vlm=None, images=None, plan=None, log=lambda m: None):
    """auto: tesseract tier -> quality floor -> (vlm if configured & budget)
    -> paddle+layout tier. Returns best summary dict found."""
    t_start = time.monotonic()
    best = await extract_pdf(data, "tesseract", timeout, languages, force_ocr,
                             log=log)
    if best["quality"] >= QUALITY_FLOOR:
        return best
    try:
        from xberg import pdf_page_count
        pages = int(pdf_page_count(data))
    except Exception:
        pages = 0
    remaining = timeout - (time.monotonic() - t_start)

    if vlm and vlm.get("model"):
        needed = pages * VLM_SEC_PER_PAGE + VLM_MODEL_LOAD_SEC
        if needed > remaining:
            log(f"vlm tier SKIPPED: est {needed:.0f}s > remaining {remaining:.0f}s")
        else:
            try:
                log(f"escalating to vlm ({vlm['model']})")
                v = await extract_pdf(data, "vlm", remaining, languages,
                                      True, vlm=vlm, images=images, plan=plan,
                                      log=log)
                vlm_watchdog(v["usage"], log)
                if v["chars"]:
                    return v
            except Exception as e:
                log(f"vlm tier failed: {type(e).__name__}: {str(e)[:200]}")
            remaining = timeout - (time.monotonic() - t_start)

    needed = max(1, pages) * PADDLE_SEC_PER_PAGE
    if needed > remaining:
        log(f"paddle tier SKIPPED: est {needed:.0f}s > remaining {remaining:.0f}s")
        return best
    log(f"escalating to paddle+layout ({remaining:.0f}s budget, {pages} pages)")
    try:
        return await extract_pdf(data, "paddle", remaining, languages, force_ocr,
                                 log=log)
    except Exception as e:
        log(f"paddle tier failed: {type(e).__name__}: {str(e)[:200]}")
        return best


def fetch_model_groups(url):
    """Shared fetch+classify for the vlm_models tool (and later the webui
    route). Uses llama.cpp architecture.input_modalities when present;
    models without modality metadata go to 'unknown', never silently hidden.
    Raises ValueError on bad URL / unreachable endpoint."""
    import json
    import urllib.request
    url = (url or "").strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise ValueError("vlm_base_url must start with http:// or https://")
    try:
        with urllib.request.urlopen(url + "/models", timeout=8) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:
        raise ValueError(f"could not reach {url}/models: {e}")
    entries = [e for e in (payload.get("data") or payload.get("models") or [])
               if isinstance(e, dict) and (e.get("id") or e.get("name"))]

    def mods(e, key):
        arch = e.get("architecture")
        v = arch.get(key) if isinstance(arch, dict) else None
        return [str(m).lower() for m in v] if isinstance(v, list) else None

    groups = {"vision": [], "unknown": [], "text": []}
    seen = set()
    for e in sorted(entries, key=lambda e: str(e.get("id") or e.get("name")).lower()):
        mid = str(e.get("id") or e.get("name"))
        if mid in seen:
            continue
        seen.add(mid)
        status = (e.get("status") or {}).get("value") if isinstance(e.get("status"), dict) else None
        item = {"id": mid,
                "ctx": e.get("context_window") or e.get("context_length"),
                "loaded": status in ("loaded", "sleeping"),
                "modalities": mods(e, "input_modalities")}
        inm = item["modalities"]
        if inm and "image" in inm:
            groups["vision"].append(item)
        elif inm:
            groups["text"].append(item)
        else:
            groups["unknown"].append(item)
    return url, groups

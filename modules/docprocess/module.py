"""docprocess - document conversion module for OpenLumara.

Thin contract surface: settings, AI tools, job store. All logic lives in
pdfops.py / pipeline.py / htmlmd.py (core-free, offline-testable).
"""
import asyncio
import time

import core

from . import pdfops, pipeline, stylefix, textfix

# Series-specific profiles live in ./extras/, which is NOT part of the public
# module: if the folder is missing the import fails and nothing happens.
try:
    from . import extras as _extras
except Exception:                                   # pragma: no cover
    _extras = None
from .htmlmd import html_tables_to_md

# webui extension routes (@_ui_route -> /api/ext/docprocess/...). Guarded so
# the offline core-stub harnesses (tmp/docprocess) keep working without a webui.
try:
    import channels.webui.api as webui
except Exception:  # pragma: no cover
    webui = None


def _ui_route(path, method="GET"):
    if webui is None:
        def _noop(func):
            return func
        return _noop
    return webui.route(path, method)


_CONVERT_FORMATS = {"markdown": ".md", "plain": ".txt"}
_MODES = ("auto", "tesseract", "paddle", "vlm", "off")


class DocProcess(core.module.Module):
    """
    Converts PDFs and most document formats to clean markdown, with automatic
    OCR that escalates from Tesseract to PaddleOCR to your vision model when
    quality drops. Also pre-extracts PDFs uploaded in chat.
    """

    # pip deps auto-installed by OpenLumara (everything else is bundled or core)
    dependencies = ["xberg==1.3.9"]

    settings = {
        "enabled": {
            "type": "boolean", "default": True,
            "description": "Instant kill-switch for the conversion tools and PDF upload takeover. Takes effect without a restart; fully disabling the module (needs restart) is done in the module list.",
        },
        "ocr_mode": {
            "type": "text", "default": "auto",
            "description": "auto|tesseract|paddle|vlm|off. auto escalates by quality.",
        },
        "ocr_languages": {
            "type": "text", "default": "spa",
            "description": "Comma-separated Tesseract language codes (e.g. spa,eng).",
        },
        "force_ocr": {
            "type": "boolean", "default": False,
            "description": "Ignore embedded text layer; OCR every page.",
        },
        "extras": {
            "type": "boolean", "default": True,
            "description": "Load document-series profiles from the optional ./extras/ package (quirks of a specific publisher that cannot be generalised, e.g. coloured citation blocks or cover pages). Absent or disabled = the module behaves as the generic converter.",
        },
        "fix_text": {
            "type": "boolean", "default": True,
            "description": "Repair converter text damage: space before punctuation left by emphasis runs, paragraphs cut at a style boundary, hyphenation breakage (word halves rejoined or split using the PDF's own text layer as proof) and list bullets glued to the previous item.",
        },
        "fix_styles": {
            "type": "boolean", "default": True,
            "description": "Repair lying font metadata before extraction (InDesign PDFs embed bold/italic instances under the same name with FontWeight 400, so xberg prints them plain). Classifies each embedded program by its outlines (ink + slant) and relabels only those; needs a text layer with embedded fonts, so scans are skipped.",
        },
        "layout_tables": {
            "type": "boolean", "default": True,
            "description": "PaddleOCR tier: detect table layout and output real markdown tables.",
        },
        "html_tables": {
            "type": "text", "default": "convert",
            "description": "convert|keep: turn HTML tables in VLM output into pipe tables.",
        },
        "vlm_base_url": {
            "type": "text", "default": "",
            "description": "OpenAI-compatible endpoint root for vision models (http://host:port/v1).",
        },
        "vlm_model": {
            "type": "text", "default": "",
            "description": "Vision model id for the vlm tier; empty = tier off.",
        },
        "vlm_prompt": {
            "type": "text", "default": "",
            "description": "Per-page prompt; empty = automatic per model.",
        },
        "vlm_max_px": {
            "type": "number", "default": 0,
            "description": "Cap image long edge for vlm (0=off; ~1800 for small-context servers).",
        },
        "max_page_raster_mb": {
            "type": "number", "default": 100,
            "description": "Memory budget per page raster. Raise for better image quality on very large scans.",
        },
        "max_size_mb": {
            "type": "number", "default": 25,
            "description": "Refuse input files larger than this many MB (0 = no limit).",
        },
        "timeout_seconds": {
            "type": "number", "default": 240,
            "description": "Max seconds per document before the job fails.",
        },
        "debug": {
            "type": "boolean", "default": False,
            "description": "Extra OCR-ladder detail in tool results and logs.",
        },
    }

    # -------------------------
    #   EVENT HANDLERS
    # -------------------------

    async def on_ready(self):
        """Diagnostic log: xberg bindings must be importable; otherwise every
        PDF deliberately falls through to the built-in extractor."""
        try:
            import xberg  # noqa: F401
            try:
                import importlib.metadata as md
                ver = md.version("xberg")
            except Exception:
                ver = "unknown version"
            self.channel.log(self.name, f"ready - xberg bindings loaded ({ver})")
        except Exception as e:
            self.channel.log(self.name,
                             f"xberg is NOT importable ({type(e).__name__}: {e}). "
                             "PDFs will use the built-in extractor.")

    # -------------------------
    #   AI TOOLS
    # -------------------------

    @core.module.tool
    async def convert(self, input_path: str, output_path: str = "",
                      to: str = "markdown", ocr_mode: str = "",
                      include_content: bool = False):
        """Convert a document (PDF, images, Office, EPUB...) to text.

        Args:
            input_path: File to convert (~ is expanded).
            output_path: File to write. Empty = next to input with .md/.txt. The default target is never silently overwritten.
            to: Output text format: markdown|plain.
            ocr_mode: Override the ocr_mode setting for this call: auto|tesseract|paddle|vlm|off (empty = setting).
            include_content: Also return the extracted text inline (conversions can be huge).
        """
        import os
        if not self.config.get("enabled"):
            return self.result("docprocess is disabled", success=False)
        mode = (str(ocr_mode).strip() or str(self.config.get("ocr_mode") or "auto"))
        fmt = str(to).strip().lower()
        if fmt not in _CONVERT_FORMATS:
            return self.result(f"convert: 'to' must be one of {sorted(_CONVERT_FORMATS)}", success=False)
        if mode not in _MODES:
            return self.result(f"convert: ocr_mode must be one of {_MODES} (got {mode!r})", success=False)

        path = os.path.abspath(os.path.expanduser(str(input_path).strip()))
        if not os.path.isfile(path):
            return self.result(f"convert: input_path is not an existing file: {path}", success=False)
        limit = float(self.config.get("max_size_mb") or 0)
        size = os.path.getsize(path)
        if limit > 0 and size > limit * 1048576:
            return self.result(f"convert: file is {size / 1048576:.1f} MB, over max_size_mb={limit:g}", success=False)

        with open(path, "rb") as f:
            head = f.read(1024)
        is_pdf = head[:4] == b"%PDF" or b"%PDF" in head

        default_out = os.path.splitext(path)[0] + _CONVERT_FORMATS[fmt]
        out = os.path.abspath(os.path.expanduser(str(output_path).strip())) if output_path else default_out
        if out == path:
            return self.result("convert: output would overwrite the input file; choose a different output_path", success=False)
        if not output_path and os.path.exists(out):
            return self.result(f"convert: default output already exists: {out}. Pass an explicit output_path to overwrite it or pick another name.", success=False)

        vlm = self._resolve_vlm_config()
        if mode == "vlm" and not vlm:
            return self.result("convert: ocr_mode=vlm needs vlm_model set in this module's settings", success=False)

        pages = 0
        if is_pdf:
            try:
                from xberg import pdf_page_count
                with open(path, "rb") as f:
                    pages = int(pdf_page_count(f.read()))
            except Exception:
                pages = 0
        if pages:
            est = (pages * (pipeline.VLM_SEC_PER_PAGE if mode == "vlm"
                            else 1.2 + pipeline.PADDLE_SEC_PER_PAGE)
                   + (pipeline.VLM_MODEL_LOAD_SEC if mode == "vlm" else 0))
        else:
            est = None

        self._job_seq = getattr(self, "_job_seq", 0) + 1
        job = {"id": f"c{self._job_seq}", "input": path, "output": out,
               "mode": mode, "format": fmt, "is_pdf": is_pdf,
               "include_content": bool(include_content), "status": "running",
               "started": time.monotonic(), "pages": pages, "est": est,
               "log": [], "chars": 0, "method": "?", "quality": 0.0,
               "secs": None, "error": "", "content": ""}
        self._jobs = getattr(self, "_jobs", {})
        self._jobs[job["id"]] = job
        task = asyncio.create_task(self._run_conversion(job))
        job["task"] = task
        self._prune_jobs()

        # Return BEFORE the core tool timeout can cancel us; long jobs keep
        # running server-side and are polled via convert_status.
        try:
            tv = float(core.config.get("core", "tool_timeout", default=10.0))
        except Exception:
            tv = 10.0
        grace = max(1.0, tv - 1.5)
        await asyncio.wait({task}, timeout=grace)

        if job["status"] == "running":
            return self.result(
                f"Conversion of {os.path.basename(path)} is still running "
                f"(job {job['id']}) - poll docprocess_convert_status(job_id=\"{job['id']}\"). "
                f"Output target: {out}")
        return self._format_conversion_result(job)

    @core.module.tool
    async def convert_status(self, job_id: str = ""):
        """Report convert jobs: running progress, finished summaries or errors.

        Args:
            job_id: Job id from convert. Empty = one line per known job.
        """
        self._prune_jobs()
        if not str(job_id).strip():
            if not self._jobs:
                return self.result("no conversion jobs recorded (jobs live in memory until the server restarts)")
            return self.result("\n".join(self._status_line(j) for j in self._jobs.values()))
        job = self._jobs.get(str(job_id).strip())
        if not job:
            return self.result(f"convert_status: no such job {job_id!r} (empty job_id lists all)", success=False)
        if job["status"] != "running":
            return self._format_conversion_result(job)
        line = self._status_line(job)
        if job["log"]:
            line += "\nlog tail: " + " | ".join(job["log"][-3:])
        return self.result(line)

    @core.module.tool
    async def vlm_models(self, vlm_base_url: str = "", show_all: bool = False):
        """List VISION-capable models of an OpenAI-compatible endpoint, grouped by modality.

        Args:
            vlm_base_url: API root like http://host:port/v1. Empty = this module's vlm_base_url setting.
            show_all: Also list text-only and unknown-modality models (default False).
        """
        url = str(vlm_base_url or "").strip() or str(self.config.get("vlm_base_url") or "")
        try:
            url, groups = pipeline.fetch_model_groups(url)
        except ValueError as e:
            return self.result(f"vlm_models: {e}", success=False)

        def fmt(bucket):
            return "\n".join(
                f"{m['id']} (ctx={m['ctx']}{', loaded now' if m['loaded'] else ''})"
                for m in bucket)

        parts = []
        if groups["vision"]:
            parts.append(f"VISION-capable ({len(groups['vision'])}):\n" + fmt(groups["vision"]))
        if groups["unknown"]:
            parts.append(f"modality UNKNOWN, verify before use ({len(groups['unknown'])}):\n"
                         + fmt(groups["unknown"]))
        if show_all and groups["text"]:
            parts.append(f"text-only ({len(groups['text'])}):\n" + fmt(groups["text"]))
        elif groups["text"]:
            parts.append(f"({len(groups['text'])} text-only hidden - pass show_all=True to see them)")
        if not parts:
            return self.result(f"no models listed at {url}/models")
        return self.result("\n\n".join(parts))

    # -------------------------
    #   WEBUI EXTENSION ROUTES (settings-page VLM model combo)
    # -------------------------

    @_ui_route("vlm_models")
    async def _route_vlm_models(self, body=None, query=None):
        """GET /api/ext/docprocess/vlm_models - structured JSON for the VLM
        model combo grafted onto this module's settings page. ValueError from
        the shared classifier is caught and returned as success=False (the
        webui envelope surfaces it; the combo shows it in its refresh tooltip).
        """
        try:
            url, groups = pipeline.fetch_model_groups(
                str(self.config.get("vlm_base_url") or ""))
        except ValueError as e:
            return {"error": str(e), "success": False}
        return {
            "url": url,
            "configured_model": str(self.config.get("vlm_model") or ""),
            "groups": groups,
            "success": True,
        }

    # -------------------------
    #   JOB MACHINERY (proven v10 pattern)
    # -------------------------

    def _status_line(self, job):
        import os
        elapsed = time.monotonic() - job["started"]
        arrow = f"[{job['id']}] {os.path.basename(job['input'])} -> {os.path.basename(job['output'])}"
        if job["status"] == "running":
            est_txt = f", est ~{job['est']:.0f}s" if job["est"] else ""
            return f"{arrow}: running {elapsed:.0f}s{est_txt}"
        if job["status"] == "error":
            return f"{arrow}: ERROR after {job['secs'] or elapsed:.0f}s - {job['error'][:120]}"
        return f"{arrow}: done in {job['secs'] or elapsed:.0f}s - {job['chars']} chars, method={job['method']}, quality={job['quality']:.2f}"

    def _format_conversion_result(self, job):
        import os
        name = os.path.basename(job["input"])
        if job["status"] == "error":
            return self.result(f"Conversion of {name} FAILED after {job['secs'] or 0:.1f}s: {job['error']}", success=False)
        line = (f"Converted {name} -> {job['output']} | {job['chars']} chars | method={job['method']} "
                f"quality={job['quality']:.2f} | {job['secs']:.1f}s | mode={job['mode']}")
        notable = [l for l in job["log"]
                   if any(k in l for k in ("tier", "SKIPPED", "WATCHDOG",
                                           "vlm usage", "cropbox", "raster plan", "audit",
                                           "stylefix", "textfix", "extras"))]
        if notable:
            line += "\n" + "\n".join(notable[-5:])
        if job["include_content"]:
            line += f"\n\n----- extracted content ({job['chars']} chars) -----\n{job['content']}"
        return self.result(line)

    def _prune_jobs(self, keep=25, max_age=3 * 3600):
        now = time.monotonic()
        for jid in [k for k, j in self._jobs.items()
                    if j["status"] != "running" and now - j["started"] > max_age]:
            del self._jobs[jid]
        if len(self._jobs) > keep:
            for jid in sorted(self._jobs, key=lambda k: self._jobs[k]["started"])[: len(self._jobs) - keep]:
                if self._jobs[jid]["status"] != "running":
                    del self._jobs[jid]

    async def _run_conversion(self, job):
        import os
        log = job["log"].append
        try:
            with open(job["input"], "rb") as f:
                data = f.read()
            timeout = float(self.config.get("timeout_seconds") or 240)
            langs = tuple(p.strip() for p in
                          str(self.config.get("ocr_languages") or "spa").split(",") if p.strip())
            force_ocr = bool(self.config.get("force_ocr"))

            if job["is_pdf"]:
                # audit RAW bytes: after CropBox-trim the full spread image is
                # still drawn larger than the viewport (cov<0.98 EXPECTED)
                rows, ok = pdfops.audit_pdf(data, npages=3)
                data = pdfops.normalize_cropbox(data, log=log)
                if self.config.get("fix_styles", True):
                    data = stylefix.honest_styles(data, log=log)
                if not ok:
                    log(f"audit: structure check FAILED (first pages): {rows[:3]}")
                elif self.config.get("debug"):
                    log(f"audit ok: {rows[:2]}")
                images = None
                plan = None
                if job["mode"] in ("auto", "vlm"):
                    vlm = self._resolve_vlm_config()
                    images = {"target_dpi": 300,
                              "max_px": int(self.config.get("vlm_max_px") or 0),
                              "extract": False, "placeholders": False}
                    plan = pdfops.raster_plan(
                        data, images["target_dpi"],
                        float(self.config.get("max_page_raster_mb") or 100), log)
                    if plan and plan["dpi"] != 300:
                        images["target_dpi"] = plan["dpi"]
                    mode = "vlm" if job["mode"] == "vlm" else None
                    if job["mode"] == "vlm":
                        res = await pipeline.extract_pdf(
                            data, "vlm", timeout, langs, True,
                            vlm=vlm, images=images, plan=plan, log=log)
                        pipeline.vlm_watchdog(res["usage"], log)
                    else:
                        res = await pipeline.extract_ladder(
                            data, timeout, langs, force_ocr,
                            vlm=vlm, images=images, plan=plan, log=log)
                else:
                    res = await pipeline.extract_pdf(
                        data, job["mode"], timeout, langs, force_ocr,
                        layout_tables=bool(self.config.get("layout_tables")),
                        log=log)
            else:
                import mimetypes
                tier = job["mode"] if job["mode"] in ("tesseract", "paddle", "off") else "tesseract"
                if job["mode"] in ("auto", "vlm"):
                    log(f"ladder/vlm modes are PDF-only; this document uses the '{tier}' tier")
                from xberg import ExtractInput, extract
                inp = ExtractInput(kind="bytes", bytes=data,
                                   filename=os.path.basename(job["input"]),
                                   mime_type=mimetypes.guess_type(job["input"])[0])
                cfg = pipeline.build_config(tier, timeout, langs, force_ocr)
                import asyncio as aio
                out_obj = await aio.wait_for(extract(inp, cfg), timeout=timeout)
                docs = getattr(out_obj, "results", None) or []
                res = pipeline.summarize(docs[0], time.monotonic()) if docs else None

            if not res or not res["markdown"].strip():
                job["status"], job["error"] = "error", "extraction produced no content"
                return

            content = html_tables_to_md(
                res["markdown"].strip(),
                mode=str(self.config.get("html_tables") or "convert"))
            if self.config.get("fix_text", True):
                content = textfix.repair(content,
                                         pdf_bytes=data if job["is_pdf"] else None,
                                         log=log)
            content = self._apply_extras(content, data, job["is_pdf"], log)

            os.makedirs(os.path.dirname(job["output"]) or ".", exist_ok=True)
            with open(job["output"], "w", encoding="utf-8") as f:
                f.write(content + "\n")
            job["chars"] = len(content)
            job["content"] = content
            job["method"] = res["method"]
            job["quality"] = res["quality"]
            job["status"] = "done"
        except asyncio.CancelledError:
            job["status"], job["error"] = "error", "job cancelled (server restart?)"
            raise
        except asyncio.TimeoutError:
            job["status"], job["error"] = "error", (
                f"extraction exceeded timeout_seconds={self.config.get('timeout_seconds')}")
        except Exception as e:
            job["status"], job["error"] = "error", f"{type(e).__name__}: {str(e)[:300]}"
        finally:
            job["secs"] = time.monotonic() - job["started"]

    # -------------------------
    #   UPLOADED-FILE HOOK (core: Channel._module_file_to_block)
    # -------------------------

    async def on_file_to_block(self, filename: str, file_data: bytes):
        """Take over uploaded PDFs: xberg extraction (ladder modes) instead of
        core's pypdf text dump. Return None on anything non-PDF, oversized,
        or failed -> built-in extractor handles it untouched."""
        if not self.config.get("enabled"):
            return None
        if not (file_data[:4] == b"%PDF" or b"%PDF" in file_data[:1024]):
            return None
        limit = float(self.config.get("max_size_mb") or 0)
        if limit > 0 and len(file_data) > limit * 1048576:
            self.channel.log(self.name, f"'{filename}': {len(file_data)/1048576:.1f}MB over max_size_mb, using built-in extractor")
            return None

        debug = bool(self.config.get("debug"))
        log = (lambda m: self.channel.log(self.name, m)) if debug else (lambda m: None)
        timeout = float(self.config.get("timeout_seconds") or 240)
        langs = tuple(p.strip() for p in
                      str(self.config.get("ocr_languages") or "spa").split(",") if p.strip())
        mode = str(self.config.get("ocr_mode") or "auto")

        try:
            # audit RAW bytes (post-trim cov<0.98 is the expected trim state)
            rows, ok = pdfops.audit_pdf(file_data, npages=3)
            data = pdfops.normalize_cropbox(file_data, log=log)
            if self.config.get("fix_styles", True):
                data = stylefix.honest_styles(data, log=log)
            if not ok:
                log(f"audit: structure check FAILED (first pages): {rows[:3]}")
            images, plan, res = None, None, None
            if mode in ("auto", "vlm"):
                vlm = self._resolve_vlm_config()
                images = {"target_dpi": 300,
                          "max_px": int(self.config.get("vlm_max_px") or 0),
                          "extract": False, "placeholders": False}
                plan = pdfops.raster_plan(
                    data, images["target_dpi"],
                    float(self.config.get("max_page_raster_mb") or 100), log)
                if plan and plan["dpi"] != 300:
                    images["target_dpi"] = plan["dpi"]
                if mode == "vlm":
                    if not vlm:
                        self.channel.log(self.name, "ocr_mode=vlm but no vlm_model configured - falling back to built-in extractor")
                        return None
                    res = await pipeline.extract_pdf(data, "vlm", timeout, langs,
                                                     True, vlm=vlm, images=images,
                                                     plan=plan, log=log)
                    pipeline.vlm_watchdog(res["usage"], log)
                else:
                    res = await pipeline.extract_ladder(data, timeout, langs,
                                                       bool(self.config.get("force_ocr")),
                                                       vlm=vlm, images=images,
                                                       plan=plan, log=log)
            else:
                res = await pipeline.extract_pdf(
                    data, mode, timeout, langs, bool(self.config.get("force_ocr")),
                    layout_tables=bool(self.config.get("layout_tables")), log=log)

            content = html_tables_to_md(res["markdown"].strip(),
                                        str(self.config.get("html_tables") or "convert"))
            if content and self.config.get("fix_text", True):
                content = textfix.repair(content, pdf_bytes=data, log=log)
            if content:
                content = self._apply_extras(content, data, True, log)
            if not content:
                self.channel.log(self.name, f"'{filename}': extraction produced no content, using built-in extractor")
                return None
            self.channel.log(self.name, f"'{filename}': method={res['method']} quality={res['quality']:.2f} {res['secs']}s mode={mode}")
            return {"type": "text", "text": f"File: {filename}\n\n```markdown\n{content}\n```"}
        except asyncio.TimeoutError:
            self.channel.log(self.name, f"'{filename}': timed out after {timeout}s, using built-in extractor")
            return None
        except Exception as e:
            self.channel.log(self.name, f"'{filename}': {type(e).__name__}: {str(e)[:200]} - using built-in extractor")
            return None

    # -------------------------
    #   INTERNALS
    # -------------------------

    def _extras_profiles(self, log):
        """Profiles from ./extras/, loaded once per process.

        Empty list when the folder is absent (public module) or disabled."""
        if _extras is None or not self.config.get("extras", True):
            return []
        cached = getattr(self, "_profile_cache", None)
        if cached is None:
            cached = _extras.load(log=log)
            self._profile_cache = cached
        return cached

    def _apply_extras(self, content, data, is_pdf, log):
        """Let series profiles reshape the markdown. Never raises."""
        profiles = self._extras_profiles(log)
        if not profiles or not is_pdf:
            return content
        info = _extras.document_info(data)
        if info is None:
            return content
        ctx = dict(info, pdf_bytes=data, config=self.config)
        return _extras.apply("post", content, ctx, log=log, profiles=profiles)

    def _resolve_vlm_config(self):
        """vlm settings -> pipeline dict, or None when the tier is off.
        vlm_base_url is a literal endpoint on purpose (no core config coupling)."""
        model = str(self.config.get("vlm_model") or "").strip()
        if not model:
            return None
        base_url = str(self.config.get("vlm_base_url") or "").strip()
        if not base_url:
            self.channel.log(self.name, "vlm_model set but vlm_base_url empty - vlm tier disabled")
            return None
        return {"model": model, "base_url": base_url,
                "prompt": str(self.config.get("vlm_prompt") or "").strip(),
                "max_tokens": 4000}

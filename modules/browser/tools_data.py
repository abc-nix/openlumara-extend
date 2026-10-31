"""Data tools: console and network buffers, images, cookies, raw CDP and tabs.

A mixin; see tools_session.py for why module.py copies these into Browser.
"""

import json
import os

import core

from . import page
from .cdp import CDPError
from .util import (MAX_URL_LEN, filter_console, filter_requests, normalize_url,
                   tail_entries, unique_images, untrusted)


class DataTools:
    """What the AI reads out of the browser."""

    @core.module.tool
    async def console(self, count: int = 20, level: str = "info",
                      types: str = None, clear: bool = False):
        """Returns recent console messages, errors, and uncaught exceptions from all tabs.

        Debug/verbose chatter is hidden by default; the result says how many
        entries it looked at and hid instead of dropping them silently.

        Args:
            count: How many of the most recent matching entries (0 = none).
            level: Severity floor: debug, info (default), warning or error.
            types: Only these message types, e.g. 'error, warning'.
            clear: Clear the buffer after reading.
        """
        await self._ensure_connection()
        everything = list(self._console_log)
        if clear:
            self._console_log.clear()
        if not everything:
            return self.result("No console output recorded.")
        matched, hidden = filter_console(everything, level, types)
        shown = tail_entries(matched, count)
        data = {"messages": shown, "shown": len(shown), "matched": len(matched),
                "total": len(everything), "hidden": hidden, "level": level}
        return self.result(untrusted(data, self._page_source()))

    @core.module.tool
    async def network_requests(self, count: int = 20, filter: str = None,
                               types: str = None, static: bool = False,
                               clear: bool = False):
        """Returns recent network requests (method + URL + status). Requires enable_network_monitoring.

        Successful static resources are hidden unless `static` is on; the result
        says how many were hidden, so nothing disappears silently.

        Args:
            count: How many of the most recent entries to look at (0 = none).
            filter: Only URLs matching this text or regular expression.
            types: Only these resource types, e.g. 'xhr, document'.
            static: Also list successful static resources (images, scripts, fonts).
            clear: Clear the buffer after reading.
        """
        if not self.config.get("enable_network_monitoring", False):
            return self.result(
                "enable_network_monitoring is off. Enable it and restart.",
                success=False,
            )
        await self._ensure_connection()
        everything = list(self._network_log)
        if clear:
            self._network_log.clear()
        if not everything:
            return self.result("No network requests recorded.")
        # the filters see the whole buffer: a page's own requests are the
        # oldest, and taking the last `count` first hid them from every filter
        matched, hidden = filter_requests(everything, filter, types,
                                          skip_static=not static)
        shown = tail_entries(matched, count)
        data = {"requests": shown, "shown": len(shown), "matched": len(matched),
                "total": len(everything), "hidden": hidden}
        return self.result(untrusted(data, self._page_source()))

    @core.module.tool
    async def get_images(self, min_size: int = 48, limit: int = 50):
        """Lists images on the current page with their src, alt text and pixel size.

        Images narrower and shorter than min_size are skipped and repeated src
        values are dropped; more than limit results are cut, with the whole
        list saved to a file.

        Args:
            min_size: Skip images smaller than this many px on both sides (0 = keep all).
            limit: Max images to return (default 50).
        """
        sid = await self._sid()
        try:
            min_size = max(0, int(min_size))
        except (TypeError, ValueError):
            min_size = 48
        try:
            limit = max(1, int(limit))
        except (TypeError, ValueError):
            limit = 50
        entries = await page.collect_images(self._cdp, sid, min_size)
        images = unique_images(entries, MAX_URL_LEN)
        if not images:
            return self.result([])
        shown = images[:limit]
        omitted = max(0, len(images) - len(shown))
        data = {"images": shown, "total": len(images),
                "returned": len(shown), "omitted": omitted}
        if omitted:
            path = os.path.join(self._snapshots_dir,
                                page.new_filename("images", "json"))
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(images, f, ensure_ascii=False)
                data["full_path"] = path
            except OSError as e:
                self.log("browser", f"get_images: {core.detail_error(e)}")
        return self.result(untrusted(data, self._page_source()))

    @core.module.tool
    async def cookies(self, domain: str = None):
        """Returns the cookies of the current tab's site (or of a specific domain), including their values.

        Args:
            domain: Optional domain to look up instead of the current tab (e.g. 'example.com').
        """
        sid = await self._sid()
        if domain:
            urls = [f"https://{str(domain).strip().strip('/')}/"]
        else:
            try:
                res = await self._cdp.cmd("Runtime.evaluate", {
                    "expression": "location.href",
                    "returnByValue": True,
                }, sid)
                url = res.get("result", {}).get("value")
            except Exception:
                url = None
            if not (isinstance(url, str) and url.startswith("http")):
                # no silent example.com fallback: that looked like "this site
                # has no cookies" when nothing was queried for the real site
                return self.result(
                    "This tab is not http(s). Pass a domain (e.g. "
                    "domain='example.com').", success=False,
                )
            urls = [url]
        res = await self._cdp.cmd("Network.getCookies", {"urls": urls}, sid)
        cookies = res.get("cookies", [])
        return self.result([
            {k: c.get(k) for k in ("name", "value", "domain", "path",
                                   "httpOnly", "secure", "expires")}
            for c in cookies
        ])

    @core.module.tool
    async def cdp(self, method: str, params: dict = None, use_current_tab: bool = True):
        """Raw CDP passthrough. Escape hatch for anything not covered by other tools (requires allow_raw_cdp).

        Args:
            method: CDP method name (e.g. 'Page.captureSnapshot').
            params: CDP method parameters as a JSON object.
            use_current_tab: Send on the current tab session (True) or browser session (False).
        """
        if not self.config.get("allow_raw_cdp", False):
            return self.result(
                "Raw CDP is disabled. Enable the allow_raw_cdp setting to use it.",
                success=False,
            )
        sid = await self._sid() if use_current_tab else None
        res = await self._cdp.cmd(method, params or {}, sid)
        return self.result(res)

    @core.module.tool
    async def list_tabs(self):
        """Lists all open tabs (pages) in the controlled browser."""
        await self._ensure_connection()
        targets = await page.list_page_targets(self._cdp)
        # current = the tab this module drives (the word Playwright MCP uses);
        # active = the tab the user is looking at (chrome.tabs.Tab's selected
        # tab). They differ as soon as the user clicks another tab.
        return self.result(await self._tab_entries(targets))

    @core.module.tool
    async def new_tab(self, url: str = "about:blank"):
        """Opens a new tab and makes it the current tab. A schemeless URL gets https:// prepended, exactly like navigate.

        Args:
            url: URL to open in the new tab (default about:blank).
        """
        await self._ensure_connection()
        url = normalize_url(url)
        res = await self._cdp.cmd("Target.createTarget", {"url": url})
        tid = res.get("targetId")
        await self._attach_target(tid)
        await self._cdp.cmd("Target.activateTarget", {"targetId": tid})
        self._current_target = tid
        return self.result(f"Opened new tab: {url or 'about:blank'}")

    @core.module.tool
    async def switch_tab(self, target: str):
        """Switches the current tab by id from list_tabs or by its zero-based index in that same list.

        Args:
            target: Tab id or index (e.g. '0' for the first tab; a bare number is accepted too).
        """
        await self._ensure_connection()
        # tool arguments arrive uncoerced (core passes JSON straight through),
        # so a model that answers with a number would otherwise crash on
        # target.isdigit()
        target = str(target)
        targets = await page.list_page_targets(self._cdp)
        if not targets:
            return self.result("No tabs open.", success=False)
        tid = page.find_target(targets, target)
        if tid is None:
            return self.result(
                f"Tab '{target}' not found. Use list_tabs to see available tabs.",
                success=False,
            )
        await self._attach_target(tid)
        await self._cdp.cmd("Target.activateTarget", {"targetId": tid})
        self._current_target = tid
        return self.result(f"Switched to tab {target}.")

    @core.module.tool
    async def close_tab(self, target: str = None):
        """Closes a tab (default: the current tab) and switches to another one.

        Args:
            target: Tab id or index to close (default: current tab; a bare number is accepted).
        """
        await self._ensure_connection()
        target = None if target is None else str(target)
        targets = await page.list_page_targets(self._cdp)
        tid = self._current_target if target is None else page.find_target(targets, target)
        if tid is None:
            return self.result("Tab not found.", success=False)
        await self._cdp.cmd("Target.closeTarget", {"targetId": tid})
        self._attached.pop(tid, None)
        if self._current_target == tid:
            self._current_target = None
            remaining = [t for t in await page.list_page_targets(self._cdp) if t.get("targetId") != tid]
            if remaining:
                await self._attach_target(remaining[0]["targetId"])
                self._current_target = remaining[0]["targetId"]
        return self.result("Tab closed.")

    @core.module.tool
    async def handle_dialog(self, accept: bool = True, prompt_text: str = None):
        """Responds to a pending JavaScript dialog (alert/confirm/prompt).

        Args:
            accept: True to accept, False to dismiss.
            prompt_text: Text to provide for prompt() dialogs (optional).
        """
        await self._ensure_connection()
        # the current tab's dialog; a dialog from another tab is only
        # taken when it is the sole one open (otherwise which one is answered
        # would be a guess)
        key = self._current_target
        entry = self._pending_dialogs.get(key)
        if entry is None and len(self._pending_dialogs) == 1:
            key, entry = next(iter(self._pending_dialogs.items()))
        if entry is None:
            others = [k for k in self._pending_dialogs if k != key]
            if others:
                return self.result(
                    f"Another tab has this dialog ({len(others)}). Switch to "
                    f"it and retry.", success=False,
                )
            return self.result("No pending dialog.", success=False)
        params = {"accept": bool(accept)}
        if prompt_text is not None:
            params["promptText"] = str(prompt_text)
        sid = entry.get("sessionId") or self._current_session()
        try:
            await self._cdp.cmd("Page.handleJavaScriptDialog", params, sid)
        except CDPError:
            # dialog vanished between the opening event and this call (user
            # clicked it, auto-accept raced us): self-heal instead of
            # leaking a raw CDP error to the model
            self._pending_dialogs.pop(key, None)
            return self.result("No dialog is showing (already handled?).",
                               success=False)
        self._pending_dialogs.pop(key, None)
        return self.result(f"Dialog {'accepted' if accept else 'dismissed'}.")


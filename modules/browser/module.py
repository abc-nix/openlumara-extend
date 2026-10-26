"""
Browser Module for OpenLumara
=====================================

Controls a real Chrome/Chromium (or Brave) browser window via the Chrome
DevTools Protocol (CDP), giving the AI the usual browser tools: open,
navigate, control (click/type/press/scroll), snapshot (accessibility tree),
screenshot, console/network inspection, tabs, cookies, and a raw CDP
escape hatch.

Pure Python: only depends on aiohttp (same dependency web_reader uses).
No Node.js, no browser downloads, no MCP server. The module attaches to a
browser it launches itself (dedicated profile, visible window by default)
or to one you started manually with --remote-debugging-port.

File layout (step-6 split, 2026-10-07; contract in README.md):
- module.py: the Browser class — settings, lifecycle, webui routes and ALL
  AI tools (thin bodies).
- cdp.py: transport & process layer (_CDP client, binary/port/profile
  discovery, launching, endpoint probing).
- page.py: page/DOM helpers (AX-tree walker, element refs, screenshots,
  grid overlay, metrics). Helpers are stateless: the class owns all state.

Setup (for the user):
1. Enable this module (it is auto-detected after a restart).
2. Optional: log in to sites once through the browser the module controls;
   the dedicated profile persists cookies on disk under data/browser/.
3. Screenshots are saved to data/browser/screenshots/.
"""

import core
import asyncio
import base64
import json
import os
import urllib.parse
from collections import deque
from datetime import datetime

import aiohttp

# -- AI GENERATED CODE (Qwen3.8-Flash-Next) :: (2026-10-07)
# webui extension routes: zero-dependency marker (channels/webui/api.py
# contract). channels/ has no __init__.py (namespace packages) and api.py
# imports nothing, so this import has no side effects even if the webui
# channel were disabled.
import channels.webui.api as webui

from . import cdp, page
from .cdp import CDPClient, CDPError, CDP_CALL_TIMEOUT, DEFAULT_PORT
from .page import KEY_MAP, DEFAULT_SNAPSHOT_CHARS

# network-log URL display cap: data: URLs and analytics query strings can be
# many KB and would bloat the 500-entry deque and the model's context
MAX_URL_LEN = 200


# ---------------------------------------------------------------------------
# The Module
# ---------------------------------------------------------------------------

class Browser(core.module.Module):
    """
    Gives the AI its own Chrome/Chromium window to browse the web with: open
    pages, click, type, screenshot, read what is on screen. Runs in a
    dedicated profile with persistent logins, visible by default to watch it work.
    """

    # NOTE: intentionally NOT marked unsafe so it shows up in the WebUI
    # module list (unsafe modules are hidden when webui.show_unsafe_settings
    # is false). This is a local, user-owned module controlling the user's
    # own browser. If you want the safety warning back, set this to True.
    unsafe = False

    # aiohttp is needed here for the CDP WebSocket connection (core's h11
    # http_client has no websocket support); declared so the auto-installer
    # guarantees it (idempotent if already installed).
    dependencies = ["aiohttp"]

    settings = {
        "browser_binary": {
            "type": "select",
            "description": "Which browser to launch. 'Auto-detect' uses the first it finds installed: Chrome, Chromium, Brave.",
            "default": "auto",
            "options": {
                "auto": "Auto-detect (chrome, chromium, brave)",
                "google-chrome": "Google Chrome",
                "chromium": "Chromium",
                "brave-browser": "Brave Browser",
                "custom": "Custom path (set the path below)",
            },
        },
        "custom_binary_path": {
            "type": "string",
            "description": "Full path to the browser, used when 'Custom path' is selected above.",
            "default": "",
        },
        "port": {
            "type": "number",
            "description": "Port used to talk to the browser. Only change it to attach to a browser you started yourself with --remote-debugging-port.",
            "default": DEFAULT_PORT,
        },
        "user_data_dir": {
            "type": "string",
            "description": "Profile folder of the controlled browser. Empty = data/browser/profile (persistent logins, separate from your own browsing). Absolute path = use an existing profile.",
            "default": "",
        },
        "headless": {
            "type": "boolean",
            "description": "Run the browser headless (invisible). Off by default so you can watch the AI work.",
            "default": False,
        },
        "window_size": {
            "type": "string",
            "description": "Browser window size as WIDTH,HEIGHT (e.g. 1280,800).",
            "default": "1280,800",
        },
        "auto_launch": {
            "type": "boolean",
            "description": "Launch the browser automatically the first time it is needed. Turn off to attach to a browser you start yourself.",
            "default": True,
        },
        "auto_reconnect": {
            "type": "boolean",
            "description": "Reconnect or relaunch automatically if the browser closes or the connection drops.",
            "default": True,
        },
        "close_browser_on_shutdown": {
            "type": "boolean",
            "description": "Close the browser when OpenLumara shuts down. Never closes a browser you started yourself.",
            "default": True,
        },
        "snapshot_max_chars": {
            "type": "number",
            "description": "Max characters of a page snapshot shown inline. Longer ones are saved to a file and the AI gets the path.",
            "default": DEFAULT_SNAPSHOT_CHARS,
        },
        "save_full_snapshots": {
            "type": "boolean",
            "description": "Save every untruncated page snapshot to data/browser/snapshots/.",
            "default": True,
        },
        "auto_accept_dialogs": {
            "type": "boolean",
            "description": "Accept website pop-ups automatically. Turn off to handle them with the handle_dialog tool instead.",
            "default": True,
        },
        "enable_network_monitoring": {
            "type": "boolean",
            "description": "Lets the network_requests tool see the requests a page makes. Slight overhead, off by default.",
            "default": False,
        },
        "allow_raw_cdp": {
            "type": "boolean",
            "description": "Let the AI send raw Chrome DevTools Protocol commands directly. Advanced, off by default.",
            "default": False,
        },
    }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def on_ready(self):
        """Initialize state, directories, and the HTTP session. Browser is launched lazily."""
        self._http = None
        self._cdp = None
        self._owned_proc = None
        self._current_target = None
        self._attached = {}          # targetId -> sessionId
        self._refs = {}              # "@e1" -> backendDOMNodeId (from last snapshot)
        self._console_log = deque(maxlen=500)
        self._network_log = deque(maxlen=500)
        # requestId -> the in-flight entry dict inside _network_log, so
        # responseReceived/loadingFinished/loadingFailed can annotate it
        self._net_pending = {}
        self._pending_dialog = None
        self._load_event = asyncio.Event()
        self._lock = asyncio.Lock()
        self._browser_info = None
        self._headless_override = None   # session-only mode flip (restart flow)
        self._started = False
        self._screenshots_dir = core.get_data_path("browser/screenshots")
        self._snapshots_dir = core.get_data_path("browser/snapshots")
        os.makedirs(self._screenshots_dir, exist_ok=True)
        os.makedirs(self._snapshots_dir, exist_ok=True)
        self._http = aiohttp.ClientSession()
        self._started = True
        self.log("browser", "Ready (browser will launch on first use)")

    async def on_shutdown(self):
        """Close the CDP connection and, only if we launched it, the browser."""
        if self._cdp:
            try:
                await self._cdp.close()
            except Exception:
                pass
            self._cdp = None
        if self._http:
            try:
                await self._http.close()
            except Exception:
                pass
            self._http = None
        if self._owned_proc and self.config.get("close_browser_on_shutdown", True):
            try:
                self._owned_proc.terminate()
                await asyncio.wait_for(self._owned_proc.wait(), timeout=3)
            except Exception:
                try:
                    self._owned_proc.kill()
                except Exception:
                    pass
        self._owned_proc = None

    async def on_background(self):
        """Supervisor: keep the connection alive / reconnect if needed."""
        while True:
            await asyncio.sleep(10)
            if not self._started:
                continue
            try:
                if self._cdp and self._cdp.ws and self._cdp.ws.closed:
                    if self.config.get("auto_reconnect", True):
                        self.log("browser", "Connection lost, reconnecting..")
                        await self._ensure_connection()
            except Exception as e:
                self.log("browser", f"Supervisor: {core.detail_error(e)}")

    # ------------------------------------------------------------------
    # Connection management (glue between state and cdp.py helpers)
    # ------------------------------------------------------------------

    def _profile_dir(self):
        return cdp.profile_dir(self.config)

    def _port(self):
        return cdp.port_of(self.config)

    def _find_binary(self):
        return cdp.find_binary(self.config)

    async def _probe(self, tries=1, delay=0.5):
        """Return /json/version dict if a CDP endpoint is up, else None."""
        return await cdp.probe(self._http, self._port(), tries=tries, delay=delay)

    async def _launch_browser(self):
        proc, info = await cdp.launch_browser(
            self.config, self.log, self._http,
            headless=self._headless_override,
        )
        self._owned_proc = proc
        return info

    def _effective_headless(self):
        """Headless mode the next launch (or the running one) uses."""
        if self._headless_override is not None:
            return self._headless_override
        return bool(self.config.get("headless", False))

    def _is_connected(self):
        """True if we have a live CDP connection and an attached tab."""
        return bool(self._cdp and self._cdp.ws and not self._cdp.ws.closed
                    and self._current_target)

    async def _ensure_connection(self):
        """Make sure we have a live CDP connection + an attached tab."""
        if self._is_connected():
            return True
        async with self._lock:
            if self._is_connected():
                return True
            info = await self._probe()
            if info is None:
                if self.config.get("auto_launch", True):
                    info = await self._launch_browser()
                else:
                    raise RuntimeError(
                        "No browser is reachable on "
                        f"127.0.0.1:{self._port()}. Start Chrome/Chromium with "
                        "--remote-debugging-port=<port> and a dedicated "
                        "--user-data-dir, or enable the auto_launch setting."
                    )
            self._browser_info = info
            # a fresh connection invalidates any previous tab sessions
            self._attached = {}
            self._current_target = None
            self._refs = {}
            self._net_pending.clear()
            self._cdp = CDPClient(
                self._http, info["webSocketDebuggerUrl"],
                self._on_cdp_event, self.log,
            )
            await self._cdp.connect()
            await self._cdp.cmd("Target.setDiscoverTargets", {"discover": True})
            await self._select_default_target()
            return True

    def _current_session(self):
        sid = self._attached.get(self._current_target)
        if sid is None:
            raise RuntimeError("No tab is attached. Call new_tab or navigate first.")
        return sid

    async def _select_default_target(self):
        """Attach to the active page tab, or create one if none exist."""
        targets = await page.list_page_targets(self._cdp)
        if not targets:
            res = await self._cdp.cmd("Target.createTarget", {"url": "about:blank"})
            tid = res["targetId"]
        else:
            tid = targets[0]["targetId"]  # list is ordered, active first
        await self._attach_target(tid)
        self._current_target = tid

    async def _attach_target(self, target_id):
        if target_id in self._attached:
            return
        res = await self._cdp.cmd("Target.attachToTarget",
                                  {"targetId": target_id, "flatten": True})
        sid = res["sessionId"]
        self._attached[target_id] = sid
        # Enable the domains we need for this tab
        await self._cdp.cmd("Page.enable", {}, sid)
        await self._cdp.cmd("Runtime.enable", {}, sid)
        await self._cdp.cmd("Log.enable", {}, sid)
        await self._cdp.cmd("Accessibility.enable", {}, sid)
        if self.config.get("enable_network_monitoring", False):
            await self._cdp.cmd("Network.enable", {}, sid)

    def _on_cdp_event(self, data):
        """Synchronous dispatch of CDP events (called from the reader task)."""
        method = data.get("method")
        params = data.get("params", {}) or {}
        sid = data.get("sessionId")

        if method == "Page.loadEventFired" and sid == self._attached.get(self._current_target):
            self._load_event.set()
        elif method == "Runtime.consoleAPICalled":
            args = params.get("args", [])
            text = " ".join(
                str(a.get("value", a.get("description", ""))) for a in args
            )
            self._console_log.append({
                "type": params.get("type", "log"),
                "text": text,
                "timestamp": params.get("timestamp"),
            })
        elif method == "Runtime.exceptionThrown":
            details = params.get("exceptionDetails", {})
            exc = details.get("exception", {})
            text = exc.get("description") or details.get("text", "exception")
            self._console_log.append({
                "type": "exception",
                "text": str(text),
                "timestamp": details.get("timestamp"),
            })
        elif method == "Log.entryAdded":
            entry = params.get("entry", {})
            self._console_log.append({
                "type": entry.get("level", "log"),
                "text": entry.get("text", ""),
                "timestamp": entry.get("timestamp"),
            })
        elif method == "Network.requestWillBeSent":
            if self.config.get("enable_network_monitoring", False):
                req = params.get("request", {})
                url = req.get("url", "")
                if len(url) > MAX_URL_LEN:
                    url = url[:MAX_URL_LEN - 3] + "..."
                entry = {
                    "method": req.get("method", "?"),
                    "url": url,
                    "timestamp": params.get("timestamp"),
                    "status": None,  # annotated by the events below
                }
                self._network_log.append(entry)
                rid = params.get("requestId")
                if rid:
                    # keep the entry reachable by requestId; on redirects CDP
                    # reuses the id, the older entry keeps its 3xx status
                    if len(self._net_pending) > 1000:
                        self._net_pending.clear()
                    self._net_pending[rid] = entry
        elif method == "Network.responseReceived":
            entry = self._net_pending.get(params.get("requestId"))
            if entry is not None:
                entry["status"] = (params.get("response") or {}).get("status")
        elif method == "Network.loadingFinished":
            entry = self._net_pending.pop(params.get("requestId"), None)
            if entry is not None and entry.get("timestamp"):
                entry["duration"] = round(
                    params.get("timestamp", 0) - entry["timestamp"], 2)
        elif method == "Network.loadingFailed":
            entry = self._net_pending.pop(params.get("requestId"), None)
            if entry is not None:
                entry["status"] = "FAILED"
                entry["error"] = ("canceled" if params.get("canceled")
                                  else params.get("errorText", "failed"))
        elif method == "Page.javascriptDialogOpening":
            self._pending_dialog = {
                "type": params.get("type", "alert"),
                "message": params.get("message", ""),
                "defaultPrompt": params.get("defaultPrompt", ""),
                "url": params.get("url", ""),
                "sessionId": sid,
            }
            if self.config.get("auto_accept_dialogs", True):
                asyncio.create_task(self._auto_accept_dialog(sid))
        elif method == "Page.javascriptDialogClosed":
            # authoritative signal from Chrome that a dialog was resolved
            # (auto-accept, user click, or handle_dialog): never leave a
            # stale entry that would send handle_dialog to a dialog-less tab
            self._pending_dialog = None

    async def _auto_accept_dialog(self, sid):
        try:
            await self._cdp.cmd("Page.handleJavaScriptDialog",
                                {"accept": True}, sid)
            if (self._pending_dialog
                    and self._pending_dialog.get("sessionId") == sid):
                self._pending_dialog = None
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Restart headless <-> visible: state capture & restore
    # ------------------------------------------------------------------

    async def _capture_state(self):
        """Snapshot open http tabs (url/scroll/form values), active tab and
        window bounds. Password fields are never captured (page.py); history
        stacks cannot be captured (no CDP API). A sanitized copy is saved to
        data/browser/last_state.json (crash-recovery aid, future use)."""
        targets = await page.list_page_targets(self._cdp)
        tabs = []
        active_index = None
        for i, t in enumerate(targets):
            tid = t["targetId"]
            url = t.get("url", "")
            if tid == self._current_target:
                active_index = i
            if not (url.startswith("http") or url.startswith("about:blank")):
                continue  # chrome://, devtools://, extensions: not reopenable
            await self._attach_target(tid)
            sid = self._attached.get(tid)
            state = await page.capture_page_state(self._cdp, sid) if sid else None
            tabs.append({"url": url, "title": t.get("title", ""), "state": state})
        window = await page.get_window_bounds(
            self._cdp, self._attached.get(self._current_target))
        snapshot = {"tabs": tabs, "active_index": active_index or 0,
                    "window": window,
                    "captured_at": datetime.now().isoformat(timespec="seconds")}
        try:
            path = os.path.join(core.get_data_path("browser"), "last_state.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False)
        except Exception:
            pass
        return snapshot

    async def _restore_state(self, snapshot):
        """Recreate tabs and re-inject per-tab state. Best-effort per tab."""
        tabs = snapshot.get("tabs") or []
        new_ids = []
        for t in tabs:
            try:
                res = await self._cdp.cmd("Target.createTarget", {"url": t["url"]})
                tid = res["targetId"]
                new_ids.append(tid)
                await self._attach_target(tid)
                sid = self._attached.get(tid)
                if sid:
                    await page.wait_page_ready(self._cdp, sid)
                    await page.restore_page_state(self._cdp, sid, t.get("state"))
            except Exception as e:
                self.log("browser", f"restore tab failed ({t.get('url','?')}): "
                                    f"{core.detail_error(e)}")
        restored = 0
        if new_ids:
            idx = snapshot.get("active_index") or 0
            tid = new_ids[min(idx, len(new_ids) - 1)]
            try:
                await self._attach_target(tid)
                await self._cdp.cmd("Target.activateTarget", {"targetId": tid})
                self._current_target = tid
                if not self._effective_headless():
                    await page.set_window_bounds(
                        self._cdp, self._attached.get(tid), snapshot.get("window"))
            except Exception:
                pass
            restored = len(new_ids)
        return restored

    # ------------------------------------------------------------------
    # AI Tools
    # NOTE: tool_loader scans type(module).__dict__ without MRO — tools
    # must be defined directly on this class (a mixin/base silently loses
    # them). See user_modules/browser/README.md before any file split.
    # ------------------------------------------------------------------

    @core.module.tool
    async def open(self):
        """Ensures the browser is running and connected (launches it if needed). Returns status info."""
        await self._ensure_connection()
        info = self._browser_info or await self._probe()
        return self.result({
            "status": "connected",
            "browser": info.get("Browser", "unknown") if info else "unknown",
            "url": info.get("webSocketDebuggerUrl", "") if info else "",
            "tabs": len(await page.list_page_targets(self._cdp)),
        })

    @core.module.tool
    async def close(self):
        """Closes the browser only if this module launched it; otherwise just disconnects."""
        if self._owned_proc:
            try:
                self._owned_proc.terminate()
                await asyncio.wait_for(self._owned_proc.wait(), timeout=3)
            except Exception:
                try:
                    self._owned_proc.kill()
                except Exception:
                    pass
            self._owned_proc = None
            if self._cdp:
                await self._cdp.close()
                self._cdp = None
            return self.result("Browser closed (module-launched browser).")
        if self._cdp:
            await self._cdp.close()
            self._cdp = None
        return self.result(
            "Disconnected. The browser was not started by this module, so it "
            "was left running."
        )

    @core.module.tool
    async def status(self):
        """Reports connection state, current tab, open tabs, and whether the module owns the browser process."""
        await self._ensure_connection()
        targets = await page.list_page_targets(self._cdp)
        current = None
        for t in targets:
            if t.get("targetId") == self._current_target:
                current = {"title": t.get("title", ""), "url": t.get("url", "")}
                break
        return self.result({
            "connected": True,
            "owned_process": self._owned_proc is not None,
            "current_tab": current,
            "tabs": [
                {"id": t.get("targetId"), "title": t.get("title", ""),
                 "url": t.get("url", ""), "active": t.get("attached", False)}
                for t in targets
            ],
        })

    # ------------------------------------------------------------------
    # WebUI extension routes (browser panel in the sidebar).
    # Contract (channels/webui/api.py): handlers are async with signature
    # (self, body=None, query=None) and PRIVATE (underscore-prefixed), so
    # the tool loader never registers them as AI tools. The forwarder in
    # channels/webui/channel.py exposes them at /api/ext/browser/<path>,
    # unwraps a returned self.result() dict into the standard api envelope
    # and turns exceptions into {success: false}.
    # -- AI GENERATED CODE (Qwen3.8-Flash-Next) :: (2026-10-07)
    # ------------------------------------------------------------------

    @webui.route("status")
    async def _route_ui_status(self, body=None, query=None):
        """Passive status for the webui panel: NEVER launches or attaches.

        The public status() tool deliberately auto-launches the browser via
        _ensure_connection; the UI polls this route instead, so merely
        loading a page or refreshing the modal can never spawn a browser.
        Returns raw data (the forwarder wraps it in the api envelope).
        """
        if not self._started or self._http is None:
            return {"connected": False, "browser_running": False,
                    "owned_process": False, "browser": None,
                    "current_tab": None, "tab_count": None, "headless": None}

        connected = self._is_connected()
        browser = None
        current_tab = None
        tab_count = None

        if connected:
            targets = await page.list_page_targets(self._cdp)
            tab_count = len(targets)
            for t in targets:
                if t.get("targetId") == self._current_target:
                    current_tab = {"title": t.get("title", ""),
                                   "url": t.get("url", "")}
                    break
            if self._browser_info:
                browser = self._browser_info.get("Browser")
            browser_running = True
        else:
            # single try against the CDP port; a refusal just means closed
            info = await self._probe()
            browser_running = info is not None
            if info:
                browser = info.get("Browser")

        return {
            "connected": connected,
            "browser_running": browser_running,
            "owned_process": self._owned_proc is not None,
            "browser": browser,
            "current_tab": current_tab,
            "tab_count": tab_count,
            # effective headless mode: known when we launched/own it,
            # unknown (None) for an external browser we merely attached to
            "headless": self._effective_headless() if connected else None,
        }

    @webui.route("open", method="POST")
    async def _route_ui_open(self, body=None, query=None):
        """Forward straight to the public open() tool (launches + attaches).
        Its self.result() dict is unwrapped by the forwarder; an exception
        becomes {success: false} instead of killing the request."""
        return await self.open()

    @webui.route("close", method="POST")
    async def _route_ui_close(self, body=None, query=None):
        """Forward to close(): only terminates the module-launched browser;
        an externally started one is merely disconnected."""
        return await self.close()

    @webui.route("restart", method="POST")
    async def _route_ui_restart(self, body=None, query=None):
        """Restart the module-launched browser in the opposite headless mode,
        restoring open (http/about) tabs, scroll positions and non-secret
        form values, plus the window position when going back to visible.

        Limits (inherent, see README): navigation history stacks cannot be
        restored (no CDP API), sessionStorage/downloads/dialogs are lost,
        password fields are deliberately never captured. The mode flip is
        session-scoped (the `headless` setting in settings stays the
        permanent source of truth). External browsers are refused: we never
        kill a browser we did not start."""
        if self._owned_proc is None:
            return self.result(
                "Cannot restart: the running browser was not launched by "
                "this module (external). Close it yourself and reopen from "
                "the panel for the other mode.",
                success=False,
            )
        if not self._is_connected():
            await self._ensure_connection()
        snapshot = await self._capture_state()
        new_mode = not self._effective_headless()
        self._headless_override = new_mode
        await self.close()
        await asyncio.sleep(0.5)  # let the port die before relaunch
        await self._ensure_connection()
        try:
            restored = await self._restore_state(snapshot)
        except Exception as e:
            restored = 0
            self.log("browser", f"restart restore failed: {core.detail_error(e)}")
        return self.result({
            "status": "restarted",
            "headless": new_mode,
            "tabs": len(snapshot.get("tabs") or []),
            "tabs_restored": restored,
            "note": "history stack and sessionStorage cannot be restored (CDP limit)",
        })

    @webui.route("shot")
    async def _route_ui_shot(self, body=None, query=None):
        """Hand a saved screenshot to the tool-card view as JSON base64.

        The view shrinks it with css and the browser's "open image in new tab"
        shows the full-resolution file. Addressed by file name only, and
        resolved inside the screenshots dir, so the route can't read elsewhere.
        """
        name = os.path.basename(str((query or {}).get("file") or ""))
        root = os.path.realpath(self._screenshots_dir)
        path = os.path.realpath(os.path.join(root, name))
        if not name or not path.startswith(root + os.sep) or not os.path.isfile(path):
            return self.result("no such screenshot", success=False)
        with open(path, "rb") as f:
            data = base64.b64encode(f.read()).decode("ascii")
        mime = "image/jpeg" if name.lower().endswith((".jpg", ".jpeg")) else "image/png"
        return {"mime": mime, "data": data}

    @core.module.tool
    async def navigate(self, url: str, wait_seconds: int = 8):
        """Navigates the current tab to a URL. Prepends https:// if no scheme is given.

        Args:
            url: The URL to navigate to (e.g. https://example.com).
            wait_seconds: Seconds to wait for the page load event.
        """
        await self._ensure_connection()
        url = url.strip()
        parsed = urllib.parse.urlparse(url)
        # urlparse treats "localhost:3000" as scheme "localhost", so only
        # accept real schemes and prepend https:// otherwise
        if parsed.scheme not in ("http", "https", "file", "about", "data", "chrome"):
            url = "https://" + url
        sid = self._current_session()
        self._load_event.clear()
        res = await self._cdp.cmd("Page.navigate", {"url": url}, sid, timeout=CDP_CALL_TIMEOUT)
        if res.get("errorText"):
            return self.result(f"Navigation failed: {res['errorText']}", success=False)
        loaded = True
        try:
            await asyncio.wait_for(self._load_event.wait(), timeout=min(wait_seconds, 12))
        except asyncio.TimeoutError:
            loaded = False
        return self.result({
            "url": url,
            "loaded": loaded,
            "note": "Page load event not received within the wait window (page may still be loading)." if not loaded else None,
        })

    @core.module.tool
    async def snapshot(self, max_chars: int = None):
        """Returns the page's accessibility tree as text; interactive elements get refs like @e1. Long snapshots are saved to a file and the path returned.

        Args:
            max_chars: Max characters to return inline (default: snapshot_max_chars setting).
        """
        await self._ensure_connection()
        sid = self._current_session()
        limit = max_chars or int(self.config.get("snapshot_max_chars", default=15000))
        ax = await self._cdp.cmd("Accessibility.getFullAXTree", {}, sid, timeout=20)
        nodes = ax.get("nodes", [])
        if not nodes:
            return self.result("Accessibility tree is empty.", success=False)

        lines, refs = page.walk_ax_tree(nodes)
        self._refs = refs

        title, current_url = "", ""
        try:
            info = await self._cdp.cmd("Runtime.evaluate", {
                "expression": "JSON.stringify({title: document.title, url: location.href})",
                "returnByValue": True,
            }, sid)
            parsed_info = json.loads(info.get("result", {}).get("value", "{}"))
            title = parsed_info.get("title", "")
            current_url = parsed_info.get("url", "")
        except Exception:
            pass

        body = "\n".join(lines)
        snapshot = f"URL: {current_url}\nTitle: {title}\n\n{body}"
        truncated = False
        full_path = None
        if len(snapshot) > limit:
            truncated = True
            if self.config.get("save_full_snapshots", True):
                full_path = os.path.join(
                    self._snapshots_dir, page.new_filename("snapshot", "txt"))
                with open(full_path, "w", encoding="utf-8") as f:
                    f.write(snapshot)
            snapshot = snapshot[:limit]
            snapshot += f"\n\n[truncated - full snapshot saved to {full_path}]" if full_path else "\n\n[truncated]"

        return self.result({
            "snapshot": snapshot,
            "url": current_url,
            "title": title,
            "truncated": truncated,
            "full_path": full_path,
        })

    @core.module.tool
    async def click(self, target: str):
        """Clicks an element by ref (@e1) or CSS selector; scrolls it into view and sends real mouse events.

        Args:
            target: Element ref (e.g. @e3) or CSS selector (e.g. '#submit').
        """
        await self._ensure_connection()
        sid = self._current_session()
        handle = await page.resolve_target(self._cdp, sid, self._refs, target)
        try:
            await self._cdp.cmd("DOM.scrollIntoViewIfNeeded", handle, sid)
        except Exception:
            pass
        try:
            box = await self._cdp.cmd("DOM.getBoxModel", handle, sid)
            quad = box.get("model", {}).get("border", [])
            if quad and len(quad) >= 8:
                xs = [quad[i] for i in range(0, 8, 2)]
                ys = [quad[i] for i in range(1, 8, 2)]
                cx = int(sum(xs) / 4)
                cy = int(sum(ys) / 4)
                for etype in ("mousePressed", "mouseReleased"):
                    await self._cdp.cmd("Input.dispatchMouseEvent", {
                        "type": etype, "x": cx, "y": cy,
                        "button": "left", "clickCount": 1,
                    }, sid)
                return self.result(f"Clicked {target} at ({cx}, {cy}).")
        except Exception:
            pass
        # Fallback: JS click
        object_id = await page.resolve_object_id(self._cdp, handle, sid)
        if not object_id:
            return self.result(f"Could not resolve element '{target}'.", success=False)
        await page.call_on(self._cdp, object_id, "function(){ this.click(); }", sid)
        return self.result(f"Clicked {target} (via JS).")

    @core.module.tool
    async def type_text(self, target: str, text: str):
        """Types text into an element (input/textarea/contenteditable): focuses, clears, then types.

        Args:
            target: Element ref (e.g. @e5) or CSS selector.
            text: The text to type.
        """
        await self._ensure_connection()
        sid = self._current_session()
        handle = await page.resolve_target(self._cdp, sid, self._refs, target)
        object_id = await page.resolve_object_id(self._cdp, handle, sid)
        if not object_id:
            return self.result(f"Could not resolve element '{target}'.", success=False)
        await page.call_on(
            self._cdp,
            object_id,
            "function(){ this.scrollIntoView({block:'center'}); this.focus();"
            " if (typeof this.select === 'function') { this.select(); } }",
            sid,
        )
        # clear selected content
        for ktype in ("keyDown", "keyUp"):
            await self._cdp.cmd("Input.dispatchKeyEvent", {
                "type": ktype, "key": "Backspace", "code": "Backspace",
                "windowsVirtualKeyCode": 8, "nativeVirtualKeyCode": 8,
            }, sid)
        await self._cdp.cmd("Input.insertText", {"text": text}, sid)
        return self.result(f"Typed {len(text)} characters into {target}.")

    @core.module.tool
    async def press(self, key: str):
        """Presses a keyboard key in the current tab (enter, tab, escape, arrows, f5, ...).

        Args:
            key: The key name (lowercase, e.g. 'enter').
        """
        await self._ensure_connection()
        sid = self._current_session()
        key = key.strip().lower()
        if key == "f5":
            await self._cdp.cmd("Page.reload", {}, sid)
            return self.result("Reloaded the page (F5).")
        if key not in KEY_MAP:
            return self.result(
                f"Unsupported key '{key}'. Supported: {', '.join(sorted(KEY_MAP))}, f5.",
                success=False,
            )
        vk, key_str, code = KEY_MAP[key]
        for ktype in ("keyDown", "keyUp"):
            params = {"type": ktype, "key": key_str, "code": code,
                      "windowsVirtualKeyCode": vk,
                      "nativeVirtualKeyCode": vk}
            # character keys need the `text` payload on keyDown or Chrome
            # never reaches the keypress stage -> no implicit form submit
            # on Enter, no space character insertion (probe: tmp/enter_probe.py)
            if ktype == "keyDown" and key in ("enter", "space"):
                params["text"] = "\r" if key == "enter" else " "
                params["unmodifiedText"] = params["text"]
            await self._cdp.cmd("Input.dispatchKeyEvent", params, sid)
        return self.result(f"Pressed {key}.")

    @core.module.tool
    async def scroll(self, direction: str = "down", amount: int = 300):
        """Scrolls the page in the given direction.

        Args:
            direction: 'down' or 'up'.
            amount: Pixels to scroll (default 300).
        """
        await self._ensure_connection()
        sid = self._current_session()
        delta = int(amount)
        if direction == "up":
            delta = -delta
        await self._cdp.cmd("Runtime.evaluate", {
            "expression": f"window.scrollBy({{top: {delta}, behavior: 'auto'}})",
            "returnByValue": True,
        }, sid)
        return self.result(f"Scrolled {direction} by {abs(delta)}px.")

    @core.module.tool
    async def back(self):
        """Navigates back to the previous page in the current tab's history."""
        await self._ensure_connection()
        sid = self._current_session()
        hist = await self._cdp.cmd("Page.getNavigationHistory", {}, sid)
        idx = hist.get("currentIndex", 0)
        entries = hist.get("entries", [])
        if idx <= 0 or not entries:
            return self.result("No previous page in history.", success=False)
        entry_id = entries[idx - 1].get("id")
        await self._cdp.cmd("Page.navigateToHistoryEntry", {"entryId": entry_id}, sid)
        return self.result("Navigated back.")

    @core.module.tool
    async def screenshot(self, full_page: bool = False, include_image: bool = True, grid=None,
                         format: str = "png", quality: int = 80):
        """Takes a screenshot of the current tab (PNG/JPEG) and returns the path plus an inline image.

        Args:
            full_page: Capture the whole page instead of the viewport.
            include_image: Deliver the screenshot to the model as an image message.
            grid: Labeled grid overlay. True = 100px spacing, int = custom px.
            format: 'png' (default) or 'jpeg'.
            quality: JPEG quality 0-100 (default 80).
        """
        await self._ensure_connection()
        # normalize format
        fmt = "jpeg" if format in ("jpeg", "jpg") else ("png" if format == "png" else None)
        if fmt is None:
            return self.result(
                f"Unsupported screenshot format '{format}'. Use 'png' or 'jpeg'.", success=False)
        grid_spacing = page.normalize_grid(grid)
        grid_id = None
        try:
            if grid_spacing:
                grid_id = await page.inject_grid(self._cdp, self._current_session(),
                                                 grid_spacing, full_page=full_page, log=self.log)
            path, raw = await page.capture_screenshot(
                self._cdp, self._current_session(), self._screenshots_dir,
                full_page=full_page, fmt=fmt, quality=quality)
        finally:
            await page.remove_grid(self._cdp, self._current_session(), grid_id)
        metrics = await page.viewport_metrics(self._cdp, self._current_session())
        px_w, px_h = page.image_size(raw, fmt)
        if metrics:
            if full_page:
                region = {
                    "x": 0, "y": 0,
                    "width": int(metrics.get("docW") or metrics.get("innerWidth") or px_w or 0),
                    "height": int(metrics.get("docH") or metrics.get("innerHeight") or px_h or 0),
                }
            else:
                region = {
                    "x": int(metrics.get("scrollX") or 0),
                    "y": int(metrics.get("scrollY") or 0),
                    "width": int(metrics.get("innerWidth") or px_w or 0),
                    "height": int(metrics.get("innerHeight") or px_h or 0),
                }
        else:
            region = None
        # the tool result stays plain text (path + geometry): the image can't
        # travel in a `tool` message, so it is added to context as a user
        # message instead - the inference API only accepts images in user
        # turns. adding it directly avoids channel.send() inside a tool call,
        # which deadlocks the channel's stream chain.
        meta = {
            "path": path,
            "file": os.path.basename(path),
            "bytes": len(raw),
            "full_page": bool(full_page),
            "region": region,
            "format": fmt,
            "hint": "Screenshot image is attached. Coordinates: doc_x = shot_x + region.x, doc_y = shot_y + region.y.",
        }
        if fmt == "jpeg":
            meta["quality"] = max(0, min(100, int(quality)))
        if grid_spacing:
            meta["grid"] = {"spacing": grid_spacing, "applied": bool(grid_id)}

        if include_image:
            mime = "image/jpeg" if fmt == "jpeg" else "image/png"
            await self.channel.context.chat.messages.add({
                "role": "user",
                "content": [{
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,"
                                         + base64.b64encode(raw).decode("ascii")},
                }],
                "_metadata": {"internal": "screenshot", "path": path},
            })

        return self.result(meta)

    # NOTE: a `vision` tool was removed. It used to capture a screenshot and
    # route the PNG bytes through self.channel.send(files={filename: raw}) so
    # the vision model could analyze it, but it caused a nested channel.send
    # deadlock. If screenshots alone are not enough in the future, re-add it:
    # capture a screenshot, send the bytes via channel.send(files=...), and
    # return the model's description as text.

    @core.module.tool
    async def evaluate(self, expression: str):
        """Evaluates JavaScript in the current tab and returns the result.

        Args:
            expression: JavaScript expression to evaluate (e.g. 'document.title').
        """
        await self._ensure_connection()
        sid = self._current_session()
        res = await self._cdp.cmd("Runtime.evaluate", {
            "expression": expression,
            "awaitPromise": True,
            "returnByValue": True,
            "userGesture": True,
        }, sid, timeout=CDP_CALL_TIMEOUT)
        if res.get("exceptionDetails"):
            exc = res["exceptionDetails"].get("exception", {})
            return self.result(
                exc.get("description") or res["exceptionDetails"].get("text", "JS error"),
                success=False,
            )
        result = res.get("result", {})
        if "value" in result:
            return self.result(result["value"])
        return self.result(result.get("description", "undefined"))

    @core.module.tool
    async def console(self, count: int = 20, clear: bool = False):
        """Returns recent console messages, errors, and uncaught exceptions.

        Args:
            count: How many recent entries to return.
            clear: Clear the buffer after reading.
        """
        await self._ensure_connection()
        entries = list(self._console_log)[-int(count):]
        if clear:
            self._console_log.clear()
        return self.result(entries if entries else "No console output recorded.")

    @core.module.tool
    async def network_requests(self, count: int = 20, clear: bool = False):
        """Returns recent network requests (method + URL + status). Requires enable_network_monitoring.

        Args:
            count: How many recent entries to return.
            clear: Clear the buffer after reading.
        """
        if not self.config.get("enable_network_monitoring", False):
            return self.result(
                "Network monitoring is disabled. Enable the "
                "enable_network_monitoring setting and restart to use this.",
                success=False,
            )
        await self._ensure_connection()
        entries = list(self._network_log)[-int(count):]
        if clear:
            self._network_log.clear()
        return self.result(entries if entries else "No network requests recorded.")

    @core.module.tool
    async def get_images(self):
        """Lists all images on the current page with their src and alt text."""
        await self._ensure_connection()
        sid = self._current_session()
        res = await self._cdp.cmd("Runtime.evaluate", {
            "expression": (
                "Array.from(document.images).map(i => ({src: i.src, alt: i.alt}))"
            ),
            "returnByValue": True,
        }, sid)
        return self.result(res.get("result", {}).get("value", []))

    @core.module.tool
    async def cookies(self, domain: str = None):
        """Returns cookies for the current tab's domain (or a specific domain).

        Args:
            domain: Optional domain to filter cookies for (e.g. 'example.com').
        """
        await self._ensure_connection()
        sid = self._current_session()
        if domain:
            urls = [f"https://{domain}/"]
        else:
            try:
                res = await self._cdp.cmd("Runtime.evaluate", {
                    "expression": "location.href",
                    "returnByValue": True,
                }, sid)
                url = res.get("result", {}).get("value")
                urls = [url] if isinstance(url, str) and url.startswith("http") else ["https://example.com/"]
            except Exception:
                urls = ["https://example.com/"]
        res = await self._cdp.cmd("Network.getCookies", {"urls": urls}, sid)
        cookies = res.get("cookies", [])
        return self.result([
            {k: c.get(k) for k in ("name", "value", "domain", "path",
                                   "httpOnly", "secure", "expires")}
            for c in cookies
        ])

    @core.module.tool
    async def list_tabs(self):
        """Lists all open tabs (pages) in the controlled browser."""
        await self._ensure_connection()
        targets = await page.list_page_targets(self._cdp)
        return self.result([
            {
                "id": t.get("targetId"),
                "title": t.get("title", ""),
                "url": t.get("url", ""),
                "current": t.get("targetId") == self._current_target,
            }
            for t in targets
        ])

    @core.module.tool
    async def new_tab(self, url: str = "about:blank"):
        """Opens a new tab and makes it the current tab.

        Args:
            url: URL to open in the new tab (default about:blank).
        """
        await self._ensure_connection()
        res = await self._cdp.cmd("Target.createTarget", {"url": url or "about:blank"})
        tid = res.get("targetId")
        await self._attach_target(tid)
        await self._cdp.cmd("Target.activateTarget", {"targetId": tid})
        self._current_target = tid
        return self.result(f"Opened new tab: {url or 'about:blank'}")

    @core.module.tool
    async def switch_tab(self, target: str):
        """Switches the current tab by id from list_tabs or zero-based index.

        Args:
            target: Tab id or index (e.g. '0' for the first tab).
        """
        await self._ensure_connection()
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
            target: Tab id or index to close (default: current tab).
        """
        await self._ensure_connection()
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
        if not self._pending_dialog:
            return self.result("No pending dialog.", success=False)
        params = {"accept": bool(accept)}
        if prompt_text is not None:
            params["promptText"] = str(prompt_text)
        sid = self._pending_dialog.get("sessionId") or self._current_session()
        try:
            await self._cdp.cmd("Page.handleJavaScriptDialog", params, sid)
        except CDPError:
            # dialog vanished between the opening event and this call (user
            # clicked it, auto-accept raced us): self-heal instead of
            # leaking a raw CDP error to the model
            self._pending_dialog = None
            return self.result("No dialog is showing (already handled?).",
                               success=False)
        self._pending_dialog = None
        return self.result(f"Dialog {'accepted' if accept else 'dismissed'}.")

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
        await self._ensure_connection()
        sid = self._current_session() if use_current_tab else None
        res = await self._cdp.cmd(method, params or {}, sid, timeout=CDP_CALL_TIMEOUT)
        return self.result(res)

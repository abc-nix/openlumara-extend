"""Browser module: a real Chrome/Chromium (or Brave) driven over the DevTools
Protocol.

File map: module.py holds the Browser class (settings, lifecycle, webui routes,
every AI tool); cdp.py the transport and process layer; page.py the page/DOM
helpers; util.py the pure helpers. Only aiohttp is required, and only the
class keeps state. Profile and screenshots live under data/browser/.
"""

import core
import asyncio
import base64
import functools
import json
import os
import time
from collections import deque
from datetime import datetime

import aiohttp

# -- AI GENERATED CODE (Qwen3.8-Flash-Next) :: (2026-10-07)
# webui extension routes: zero-dependency marker (channels/webui/api.py
# contract). channels/ has no __init__.py (namespace packages) and api.py
# imports nothing, so this import has no side effects even if the webui
# channel were disabled.
import channels.webui.api as webui

# A module reload re-executes this file only: cdp/page/util stay in sys.modules,
# so edits to those three need a full server restart.
from . import cdp, page
from .cdp import CDPClient, CDPError, DEFAULT_PORT
from .page import KEY_MAP, DEFAULT_SNAPSHOT_CHARS
from .util import (normalize_url, tail_entries, grid_fits, capture_fits,
                   first_capture_window, tool_budget, CALL_BUDGET, drop_target,
                   headless_from_version, timeout_text, rejected_top_level_await,
                   wrap_await, restorable_url, captured_active_index)

# network-log URL display cap: data: URLs and analytics query strings can be
# many KB and would bloat the 500-entry deque and the model's context
MAX_URL_LEN = 200

# ---------------------------------------------------------------------------
# The Module
# ---------------------------------------------------------------------------

class Browser(core.module.Module):
    """
    Gives your AI its own Chrome/Chromium window to browse the web: open
    pages, click, type, screenshot and read what is on screen. It runs in a
    dedicated profile with persistent logins, visible by default so you can
    watch it work.
    """

    # category groups this module with the other web-facing ones in the
    # settings list (without it the UI files it under "Other")
    category = "web"

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
            # "text" is the type the settings UI renders for plain strings
            # ("string" is accepted in the schema docs but has no renderer)
            "type": "text",
            "description": "Full path to the browser binary. Only used when 'Custom path' is selected above.",
            "default": "",
            "depends": {"browser_binary": "custom"},
        },
        "port": {
            "type": "number",
            "description": "Port used to talk to the browser. If a browser is already listening on it (for example one you started with --remote-debugging-port), the module attaches to that browser instead of launching a new one.",
            "default": DEFAULT_PORT,
            "min": 1024,
            "max": 65535,
        },
        "user_data_dir": {
            "type": "text",
            "description": "Profile folder of the controlled browser. Empty = data/browser/profile (persistent logins, separate from your own browsing). An absolute path uses an existing profile instead - **pointing it at your everyday browser gives the AI access to your logged-in sessions.**",
            "default": "",
        },
        "headless": {
            "type": "boolean",
            "description": "Run the browser headless (invisible). Off by default so you can watch the AI work.",
            "default": False,
        },
        "window_size": {
            "type": "text",
            "description": "Browser window size as WIDTH,HEIGHT (e.g. 1280,800). Only applies when the module launches a visible browser.",
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
            "description": "Max characters of a page snapshot shown inline. Longer snapshots are cut and, when 'save_full_snapshots' is on, the full text is saved to a file and the AI gets the path.",
            "default": DEFAULT_SNAPSHOT_CHARS,
            "min": 500,
        },
        "full_page_max_pixels": {
            "type": "number",
            "description": "Largest full-page screenshot to attempt, in megapixels (width x height of the capture). Chrome paints the whole page no matter how the capture is clipped, and the capture and the grid both cost time per pixel, so bigger pages are refused with their real size instead of stalling into a timeout. 25 MPx is a ~1200 px wide page at ~20000 px tall; a 40000 px article does not fit.",
            "default": 25,
            "min": 1,
        },
        "save_full_snapshots": {
            "type": "boolean",
            "description": "Save every untruncated page snapshot to data/browser/snapshots/.",
            "default": True,
        },
        "auto_accept_dialogs": {
            "type": "boolean",
            "description": "Automatically accept JavaScript dialogs (alert, confirm, prompt) so they never block the page. Turn off to decide with the handle_dialog tool instead. Note that accepting a confirm dialog can trigger irreversible actions on some sites.",
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
        self._owned_pid = None       # adopted from the pid file after a reload
        self._current_target = None
        self._attached = {}          # targetId -> sessionId
        # refs belong to the tab that was snapshotted, not to the module: a
        # snapshot of one tab must not invalidate another tab's "@e1" refs.
        self._refs_by_tab = {}       # targetId -> {"@e1": backendDOMNodeId}
        self._console_log = deque(maxlen=500)
        self._network_log = deque(maxlen=500)
        self._target_url = {}        # targetId -> last known URL
        # requestId -> the in-flight entry dict inside _network_log, so
        # responseReceived/loadingFinished/loadingFailed can annotate it
        self._net_pending = {}
        self._pending_dialogs = {}   # targetId -> pending dialog
        self._load_event = asyncio.Event()
        self._lock = asyncio.Lock()
        self._browser_info = None
        self._headless_override = None   # session-only mode flip (restart flow)
        self._launched_headless = None   # mode the running browser was started with
        self._started = False
        self._screenshots_dir = core.get_data_path("browser/screenshots")
        self._snapshots_dir = core.get_data_path("browser/snapshots")
        os.makedirs(self._screenshots_dir, exist_ok=True)
        os.makedirs(self._snapshots_dir, exist_ok=True)
        self._http = aiohttp.ClientSession()
        self._started = True
        self._adopt_pidfile()
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
        if self._owns_browser() and self.config.get("close_browser_on_shutdown", True):
            await self._stop_owned()
        # when the setting says "leave it running", the pid file stays: the
        # next start adopts the browser instead of calling it external

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

    def _pidfile(self):
        return os.path.join(core.get_data_path("browser"), "browser.pid")

    def _adopt_pidfile(self):
        """Recover ownership of a browser launched by an earlier module load.

        A reload re-executes module.py, so `_owned_proc` is gone and the module
        would report (and treat) its own browser as external. The pid file is
        only trusted when the process is alive *and* its command line still
        names this module's profile and port - a reused pid must never be
        killed.
        """
        path = self._pidfile()
        recorded = cdp.read_pidfile(path)
        if not recorded:
            return
        if cdp.process_owns_browser(recorded["pid"], self._port(),
                                    cdp.profile_dir(self.config)):
            self._owned_pid = recorded["pid"]
            self.log("browser", f"Adopted browser pid {recorded['pid']}")
        else:
            cdp.clear_pidfile(path)

    def _owns_browser(self):
        """True if the running browser was started by this module."""
        return self._owned_proc is not None or self._owned_pid is not None

    async def _stop_owned(self):
        """Stop the browser this module started and forget it.

        One path for close, shutdown and restart, for both kinds of ownership:
        the asyncio process when this run launched it, the adopted pid after a
        reload (checked again before signalling - pids get reused).
        """
        proc, pid = self._owned_proc, self._owned_pid
        self._owned_proc = None
        self._owned_pid = None
        cdp.clear_pidfile(self._pidfile())
        if proc is not None:
            await cdp.stop_process(proc)
            return True
        if pid is not None and cdp.process_owns_browser(
                pid, self._port(), cdp.profile_dir(self.config)):
            return await cdp.stop_pid(pid)
        return False

    async def _probe(self, tries=1, delay=0.5):
        """Return /json/version dict if a CDP endpoint is up, else None."""
        return await cdp.probe(self._http, self._port(), tries=tries, delay=delay)

    async def _wait_port_down(self, timeout=5.0):
        """Wait until nothing answers on the CDP port."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await self._probe() is None:
                return True
            await asyncio.sleep(0.2)
        return False

    async def _launch_browser(self):
        # waiting for the endpoint is part of the tool call: bound by what is
        # left of the budget, not by cdp's own 20 x 0.5 s
        budget = self._budget()
        # recorded here, not read from the setting later: the setting says what
        # the *next* launch would use, and it can change while this one runs
        launched_headless = self._effective_headless()
        proc, info = await cdp.launch_browser(
            self.config, self.log, self._http,
            headless=self._headless_override,
            deadline=time.monotonic() + budget.left(),
        )
        self._launched_headless = launched_headless
        self._owned_proc = proc
        self._owned_pid = proc.pid
        # remembered on disk, because a module reload loses _owned_proc
        cdp.write_pidfile(self._pidfile(), proc.pid, self._port(),
                          cdp.profile_dir(self.config))
        return info

    def _effective_headless(self):
        """Headless mode the next launch (or the running one) uses."""
        if self._headless_override is not None:
            return self._headless_override
        return bool(self.config.get("headless", False))

    def _headless_actual(self):
        """Headless mode of the browser that is actually running.

        The setting only predicts the next launch and says nothing about a
        browser we merely attached to, so ask the browser itself. Note that
        modern Chrome's `--headless=new` keeps "Chrome/x" in the Browser field
        and only admits it in the User-Agent, which is why
        util.headless_from_version reads both.
        None means genuinely unknown.
        """
        actual = headless_from_version(self._browser_info)
        if actual is None and self._owns_browser():
            # the product string was unreadable: answer with the mode we
            # launched with, not with the current setting - that one predicts
            # the next launch and says nothing about this process
            return self._launched_headless
        return actual

    def _full_page_max_pixels(self):
        """Pixel-area cap for full-page captures, in pixels.

        Area, not height: the cost (rasterising, PNG encoding, the grid) grows
        with pixels, and a height cap refuses a tall-but-narrow page that would
        have fitted while accepting a short-but-huge one. No upper clamp: the
        tool budget is the real limit, and the timeout message names it.
        """
        try:
            mpx = float(self.config.get("full_page_max_pixels", 25))
        except (TypeError, ValueError):
            mpx = 25.0
        return int(max(1.0, mpx) * 1_000_000)

    def _capture_failed_text(self, background=False):
        """Message for a capture that outlived its window.

        A background tab is painted from scratch just for the capture, so that
        case names the command that fixes it.
        """
        if background:
            return ("Screenshot timed out. This tab is not active: switch_tab to "
                    "activate it, or raise the Tool Timeout.")
        return ("Screenshot timed out. The page was still painting: reload the tab "
                "(f5) and retry, or raise the Tool Timeout.")

    def _tool_timeout(self):
        """The framework's per-tool-call ceiling (core.tool_timeout).

        Read per call, not cached: a settings save takes effect without a
        restart. core reads it the same way and cancels the call at it.
        """
        try:
            return max(1.0, float(core.config.get("core", "tool_timeout", default=10.0)))
        except (TypeError, ValueError):
            return 10.0

    def _budget(self):
        """Deadline for the tool call that is running; see util.Budget.

        _guarded opens it at tool entry (so the deadline covers Chrome launch
        and every pre-CDP step, and every command shares one clock). Tools
        invoked directly - the webui routes - get a fresh one here.
        """
        budget = CALL_BUDGET.get()
        return budget if budget is not None else self._new_budget()

    def _new_budget(self):
        """One deadline per tool call: core.tool_timeout minus a reserve."""
        return tool_budget(self._tool_timeout())

    def _tab_refs(self):
        """Refs from the last snapshot of the tab being driven.

        Empty for a tab that was never snapshotted *or* whose document has
        since changed; `resolve_target` then answers "Unknown ref …, take a
        fresh snapshot first", which is the honest answer in both cases.
        """
        return self._refs_by_tab.get(self._current_target, {})

    def _target_of_session(self, sid):
        """targetId for a CDP session id, or None.

        CDP events name the session, never the tab: the reverse lookup is the
        only way an entry can say which tab it came from.
        """
        for tid, session in self._attached.items():
            if session == sid:
                return tid
        return None

    def _page_of_session(self, sid):
        """The URL that tab is on, as last reported by Chrome (None if unknown)."""
        tid = self._target_of_session(sid)
        return self._target_url.get(tid) if tid else None

    async def _active_target_ids(self, targets=None):
        """targetIds of the tab(s) the user is looking at right now.

        A probe is ~1 ms per tab (attach without domain enables, one
        evaluate, detach), cheap enough to answer on every call. Caching it
        for a couple of seconds was tried and went wrong: list_tabs named the
        tab from before the last new_tab, which is exactly the case this
        field exists for. Pass `targets` when the caller already fetched them.
        """
        if targets is None:
            targets = await page.list_page_targets(self._cdp)
        return set(await page.active_targets(self._cdp, targets, self._attached))

    def _is_connected(self):
        """True if we have a live CDP connection and an attached tab."""
        return bool(self._cdp and self._cdp.ws and not self._cdp.ws.closed
                    and self._current_target)

    def _forget_browser_state(self):
        """Drop everything tied to the browser connection we are leaving.

        Called before a new CDP client is built. The console and network buffers
        belong to the browser that produced them: after a relaunch they would
        answer console()/network_requests() with the dead browser's entries.
        """
        self._attached = {}
        self._current_target = None
        self._refs_by_tab.clear()
        self._pending_dialogs.clear()   # a new connection has no dialogs
        self._net_pending.clear()
        self._console_log.clear()
        self._network_log.clear()

    async def _ensure_connection(self):
        """Make sure we have a live CDP connection + an attached tab."""
        if self._is_connected():
            if self._current_target in self._attached:
                return True
            # the tab we were driving died while the connection stayed up.
            # Keep the connection and pick another live tab - never hand out
            # the dead session id, which would answer -32001 on every call.
            await self._select_default_target()
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
                        f"No browser on 127.0.0.1:{self._port()}. Enable "
                        f"auto_launch, or start Chrome with "
                        f"--remote-debugging-port={self._port()}."
                    )
            self._browser_info = info
            self._forget_browser_state()
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
        if sid is not None:
            return sid
        if self._current_target:
            # we still name a target but its session is gone: the tab was
            # closed or crashed. Say so instead of letting CDP answer -32001
            raise RuntimeError(
                "That tab is gone (closed or crashed). Call list_tabs, then "
                "switch_tab or new_tab."
            )
        raise RuntimeError("No tab is attached. Call new_tab or navigate first.")

    async def _select_default_target(self):
        """Attach to the active page tab, or create one if none exist."""
        targets = await page.list_page_targets(self._cdp)
        if not targets:
            res = await self._cdp.cmd("Target.createTarget", {"url": "about:blank"})
            tid = res["targetId"]
        else:
            tid = targets[0]["targetId"]  # CDP order: the tab that came first
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

        if method in ("Target.targetCreated", "Target.targetInfoChanged"):
            # events carry a sessionId, not an URL. Track the URL each tab
            # is on so a log entry can say which page it came from instead of
            # inheriting whatever the current tab shows now.
            info = params.get("targetInfo", {}) or {}
            tid = info.get("targetId")
            if tid and info.get("type") == "page":
                url = str(info.get("url", ""))
                self._target_url[tid] = (url[:MAX_URL_LEN - 3] + "..."
                                         if len(url) > MAX_URL_LEN else url)
            return
        if method in ("Target.targetDestroyed", "Target.targetCrashed"):
            # a closed tab has to stop being _current_target and lose its
            # session id, or every later call addresses a dead session
            tid = params.get("targetId")
            self._target_url.pop(tid, None)
            keep = drop_target(self._attached, self._current_target, tid)
            self._refs_by_tab.pop(tid, None)   # refs belonged to that document
            self._pending_dialogs.pop(tid, None)
            if keep != self._current_target:
                self._load_event.clear()
            self._current_target = keep
            return
        if method == "Page.loadEventFired" and sid == self._attached.get(self._current_target):
            self._load_event.set()
        elif method == "Page.frameNavigated":
            # a new document invalidates that tab's refs. Kept, they
            # would resolve to backend node ids of a document that is gone -
            # the same numbers, a different element, no error at all.
            frame = params.get("frame", {}) or {}
            tid = self._target_of_session(sid)
            if tid and not frame.get("parentId"):
                self._refs_by_tab.pop(tid, None)
                url = str(frame.get("url") or "")
                if url:
                    self._target_url[tid] = (url[:MAX_URL_LEN - 3] + "..."
                                             if len(url) > MAX_URL_LEN else url)
        elif method == "Runtime.consoleAPICalled":
            args = params.get("args", [])
            text = " ".join(
                str(a.get("value", a.get("description", ""))) for a in args
            )
            self._console_log.append({
                "type": params.get("type", "log"),
                "text": text,
                "timestamp": params.get("timestamp"),
                "tab": self._target_of_session(sid),
                "page": self._page_of_session(sid),
            })
        elif method == "Runtime.exceptionThrown":
            details = params.get("exceptionDetails", {})
            exc = details.get("exception", {})
            text = exc.get("description") or details.get("text", "exception")
            self._console_log.append({
                "type": "exception",
                "text": str(text),
                "timestamp": details.get("timestamp"),
                "tab": self._target_of_session(sid),
                "page": self._page_of_session(sid),
            })
        elif method == "Log.entryAdded":
            entry = params.get("entry", {})
            self._console_log.append({
                "type": entry.get("level", "log"),
                "text": entry.get("text", ""),
                "timestamp": entry.get("timestamp"),
                "tab": self._target_of_session(sid),
                # a log entry names its own source document; fall back to the
                # tab's last known URL when it does not
                "page": entry.get("url") or self._page_of_session(sid),
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
                    "tab": self._target_of_session(sid),
                    "page": self._page_of_session(sid),
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
            # one slot per tab. A single slot meant a dialog in one tab
            # could be handled on another tab's session, and a second dialog
            # silently replaced the first.
            self._pending_dialogs[self._target_of_session(sid) or sid] = {
                "type": params.get("type", "alert"),
                "message": params.get("message", ""),
                "defaultPrompt": params.get("defaultPrompt", ""),
                "url": params.get("url", ""),
                "sessionId": sid,
                "targetId": self._target_of_session(sid),
            }
            if self.config.get("auto_accept_dialogs", True):
                asyncio.create_task(self._auto_accept_dialog(sid))
        elif method == "Page.javascriptDialogClosed":
            # authoritative signal from Chrome that a dialog was resolved
            # (auto-accept, user click, or handle_dialog): never leave a
            # stale entry that would send handle_dialog to a dialog-less tab
            self._pending_dialogs.pop(self._target_of_session(sid) or sid, None)

    async def _auto_accept_dialog(self, sid):
        try:
            await self._cdp.cmd("Page.handleJavaScriptDialog",
                                {"accept": True}, sid)
            entry = self._pending_dialogs.get(self._target_of_session(sid) or sid)
            if entry is not None and entry.get("sessionId") == sid:
                self._pending_dialogs.pop(self._target_of_session(sid) or sid, None)
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
        active_hits = []             # (targetId, index in the captured list)
        for t in targets:
            tid = t["targetId"]
            url = t.get("url", "")
            if not restorable_url(url):
                continue  # chrome://, devtools://, extensions: not reopenable
            await self._attach_target(tid)
            sid = self._attached.get(tid)
            if sid and await page.is_active_tab(self._cdp, sid):
                active_hits.append((tid, len(tabs)))
            state = await page.capture_page_state(self._cdp, sid) if sid else None
            tabs.append({"url": url, "title": t.get("title", ""), "state": state})
        # Restore the tab the user is looking at, not the one this module
        # happens to drive - they differ as soon as the user clicks another
        # tab. Several visible tabs mean several windows: the one this module
        # was driving wins when it is among them.
        # the index belongs to the captured list, never the target list -
        # skipped chrome:// and devtools tabs still occupy a slot in Chrome's
        # own order.
        active_index = next((i for tid, i in active_hits
                             if tid == self._current_target), None)
        if active_index is None and active_hits:
            active_index = active_hits[0][1]
        if active_index is None:
            active_index = captured_active_index(targets, self._current_target)
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
    # NOTE: tool_loader scans type(module).__dict__ without MRO, so tools must
    # be defined directly on this class - a mixin or base class silently loses
    # every one of them.
    # ------------------------------------------------------------------

    @core.module.tool
    async def open(self):
        """Starts the browser if it isn't running and connects to it. Use this before the other browser tools when the browser may be closed.

        Returns the browser name, its debug endpoint (ws_url) and the number of open tabs.
        """
        await self._ensure_connection()
        info = self._browser_info or await self._probe()
        return self.result({
            "status": "connected",
            "browser": info.get("Browser", "unknown") if info else "unknown",
            "ws_url": info.get("webSocketDebuggerUrl", "") if info else "",
            "tabs": len(await page.list_page_targets(self._cdp)),
        })

    @core.module.tool
    async def close(self):
        """Closes the browser only if this module launched it; otherwise just disconnects."""
        if self._owns_browser():
            await self._stop_owned()
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
        """Starts the browser if needed (auto_launch), then reports connection state, the current tab, all open tabs and whether the module launched the process."""
        await self._ensure_connection()
        targets = await page.list_page_targets(self._cdp)
        current = None
        active = None
        active_ids = await self._active_target_ids(targets)
        for t in targets:
            info = {"title": t.get("title", ""), "url": t.get("url", "")}
            if t.get("targetId") == self._current_target:
                current = info
            if active is None and t.get("targetId") in active_ids:
                active = info
        return self.result({
            "connected": True,
            "owned_process": self._owns_browser(),
            # what the running browser really is (not what the setting says),
            # and the one timeout every tool call shares - the number to raise
            # when a capture says it ran out of time
            "headless": self._headless_actual(),
            "tool_timeout": self._tool_timeout(),
            "current_tab": current,
            # the tab the user is looking at, read from the page itself
            # (document.visibilityState); None when the window is minimised.
            # It equals current_tab until the user clicks another tab.
            "active_tab": active,
            "tabs": [
                # `current` is the tab this module drives, not CDP's
                # targetInfo.attached (which only says "a debugger session is
                # attached", true for every tab we have attached to)
                {"id": t.get("targetId"), "title": t.get("title", ""),
                 "url": t.get("url", ""),
                 "current": t.get("targetId") == self._current_target,
                 "active": t.get("targetId") in active_ids}
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
                    "current_tab": None, "active_tab": None,
                    "tab_count": None, "headless": None}

        connected = self._is_connected()
        browser = None
        current_tab = None
        active_tab = None
        tab_count = None

        if connected:
            targets = await page.list_page_targets(self._cdp)
            tab_count = len(targets)
            active_ids = await self._active_target_ids(targets)
            for t in targets:
                info = {"title": t.get("title", ""), "url": t.get("url", "")}
                if t.get("targetId") == self._current_target:
                    current_tab = info
                if active_tab is None and t.get("targetId") in active_ids:
                    active_tab = info
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
            "owned_process": self._owns_browser(),
            "browser": browser,
            "current_tab": current_tab,
            # the tab the user is looking at; equals current_tab until they
            # click another one, None when the window is minimised
            "active_tab": active_tab,
            "tab_count": tab_count,
            # the running browser's real mode (the same answer the status tool
            # gives), unknown (None) when it cannot be read at all
            "headless": self._headless_actual() if connected else None,
            # the ceiling every tool call gets; long captures are sized to it,
            # so this is the number to raise if a capture says it ran out of time
            "tool_timeout": self._tool_timeout(),
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

        Limits (inherent): navigation history stacks cannot be
        restored (no CDP API), sessionStorage/downloads/dialogs are lost,
        password fields are deliberately never captured. The mode flip is
        session-scoped (the `headless` setting in settings stays the
        permanent source of truth). External browsers are refused: we never
        kill a browser we did not start."""
        if not self._owns_browser():
            return self.result(
                "Not launched by this module: close it yourself, then reopen "
                "from the panel.", success=False,
            )
        if not self._is_connected():
            await self._ensure_connection()
        snapshot = await self._capture_state()
        new_mode = not self._effective_headless()
        self._headless_override = new_mode
        await self.close()
        # a fixed 0.5 s sleep raced the real shutdown - if the port was
        # still held by the dying browser (or grabbed by something else), the
        # relaunch attached to a stranger. Wait until nothing answers.
        if not await self._wait_port_down():
            return self.result(
                "Restart aborted: the old browser still holds the debug port. "
                "Close it and reopen.", success=False,
            )
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
        budget = self._budget()
        await self._ensure_connection()
        url = normalize_url(url)
        sid = self._current_session()
        try:
            wait = max(0, int(wait_seconds))
        except (TypeError, ValueError):
            wait = 8
        self._load_event.clear()
        res = await self._cdp.cmd("Page.navigate", {"url": url}, sid)
        if res.get("errorText"):
            return self.result(f"Navigation failed: {res['errorText']}", success=False)
        loaded = True
        window = 0.0
        try:
            # the load wait shares the same deadline: it can only use the time
            # the command above did not, never a number of its own
            window = min(wait, budget.left())
            await asyncio.wait_for(self._load_event.wait(), timeout=window)
        except asyncio.TimeoutError:
            loaded = False
        out = {"url": url, "loaded": loaded}
        if not loaded:
            # omit the key instead of carrying "note": null into every result
            if window < wait:
                out["note"] = ("Page load not seen before the call ran out; it may "
                               "still be loading.")
            else:
                out["note"] = ("Page load not seen within the wait window; it may "
                               "still be loading.")
        return self.result(out)

    @core.module.tool
    async def snapshot(self, max_chars: int = None):
        """Returns the page's accessibility tree as text; interactive elements get refs like @e1. Long snapshots are saved to a file and the path returned.

        Args:
            max_chars: Max characters to return inline (default: snapshot_max_chars setting).
        """
        await self._ensure_connection()
        sid = self._current_session()
        limit = max_chars or int(self.config.get("snapshot_max_chars", default=15000))
        # one big command on a long page: bounded by the tool call, not by a
        # module-private number
        ax = await self._cdp.cmd("Accessibility.getFullAXTree", {}, sid)
        nodes = ax.get("nodes", [])
        if not nodes:
            return self.result("Accessibility tree is empty.", success=False)

        lines, refs = page.walk_ax_tree(nodes)
        self._refs_by_tab[self._current_target] = refs

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
        handle = await page.resolve_target(self._cdp, sid, self._tab_refs(), target)
        try:
            await self._cdp.cmd("DOM.scrollIntoViewIfNeeded", handle, sid)
        except Exception:
            pass
        # The mouse click lands on the box centre, so both the box and where it
        # sits decide whether a click can land at all: hidden elements have no
        # box, and off-canvas ones sit outside the viewport.
        cx = cy = None
        try:
            box = await self._cdp.cmd("DOM.getBoxModel", handle, sid)
            quad = box.get("model", {}).get("border", [])
        except Exception:
            quad = []
        if len(quad) >= 8:
            xs = [quad[i] for i in range(0, 8, 2)]
            ys = [quad[i] for i in range(1, 8, 2)]
            cx, cy = int(sum(xs) / 4), int(sum(ys) / 4)

        reason = "no layout box (hidden?)"
        if cx is not None:
            size = await page.viewport_size(self._cdp, sid)
            if size and 0 <= cx < size[0] and 0 <= cy < size[1]:
                try:
                    for etype in ("mousePressed", "mouseReleased"):
                        await self._cdp.cmd("Input.dispatchMouseEvent", {
                            "type": etype, "x": cx, "y": cy,
                            "button": "left", "clickCount": 1,
                        }, sid)
                    return self.result(f"Clicked {target} at ({cx}, {cy}).")
                except Exception:
                    reason = f"mouse click failed at ({cx}, {cy})"
            else:
                reason = f"off-screen at ({cx}, {cy})"

        # Fallback: JS click, which bypasses hit-testing - say when it is used
        object_id = await page.resolve_object_id(self._cdp, handle, sid)
        if not object_id:
            return self.result(f"Could not resolve element '{target}'.", success=False)
        await page.call_on(self._cdp, object_id, "function(){ this.click(); }", sid)
        return self.result(f"Clicked {target} via JS - {reason}.")

    @core.module.tool
    async def type_text(self, target: str, text: str):
        """Types text into an element (input/textarea/contenteditable): focuses, clears, then types.

        Args:
            target: Element ref (e.g. @e5) or CSS selector.
            text: The text to type.
        """
        await self._ensure_connection()
        sid = self._current_session()
        handle = await page.resolve_target(self._cdp, sid, self._tab_refs(), target)
        object_id = await page.resolve_object_id(self._cdp, handle, sid)
        if not object_id:
            return self.result(f"Could not resolve element '{target}'.", success=False)
        # contenteditable has no select(): select the contents with a range, so
        # the Backspace below deletes the old text instead of leaving it in
        # front of what gets typed.
        await page.call_on(
            self._cdp,
            object_id,
            "function(){ this.scrollIntoView({block:'center'}); this.focus();"
            " if (typeof this.select === 'function') { this.select(); return; }"
            " if (this.isContentEditable) {"
            "   var r = document.createRange(); r.selectNodeContents(this);"
            "   var s = window.getSelection(); s.removeAllRanges(); s.addRange(r);"
            " } }",
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
            # on Enter, no space character insertion
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
            full_page: Capture the whole page instead of the viewport. Captures larger than full_page_max_pixels are refused with their real size.
            include_image: Deliver the screenshot to the model as an image message.
            grid: Labeled grid drawn on the image. True = 100px spacing, int = custom px. Requires format='png'.
            format: 'png' (default) or 'jpeg'.
            quality: JPEG quality 0-100 (default 80).
        """
        # taken BEFORE anything else: launching Chrome (1-3s), the metrics read,
        # the capture and the grid all have to fit inside core.tool_timeout
        budget = self._budget()
        await self._ensure_connection()
        sid = self._current_session()
        fmt = "jpeg" if format in ("jpeg", "jpg") else ("png" if format == "png" else None)
        if fmt is None:
            return self.result(
                f"Unsupported screenshot format '{format}'. Use 'png' or 'jpeg'.", success=False)
        try:
            quality = max(0, min(100, int(quality)))
        except (TypeError, ValueError):
            quality = 80
        grid_spacing = page.normalize_grid(grid)
        if grid_spacing and fmt != "png":
            # 1px lines do not survive JPEG, and decoding JPEG would need an
            # imaging library: ask for png instead of silently changing format
            return self.result(
                "A grid needs format='png' (JPEG is lossy).", success=False,
            )

        # metrics BEFORE the capture: they decide whether a full-page capture
        # is feasible, in the raster size Chrome will actually paint
        metrics = await page.viewport_metrics(self._cdp, sid) or {}
        shot_w = int(metrics.get("docWpx") or 0)
        shot_h = int(metrics.get("docHpx") or 0)
        max_px = self._full_page_max_pixels()
        if full_page and not metrics:
            # No metrics means the cap below cannot be checked. Refusing is the
            # honest answer: an unmeasurably large page would spend the whole
            # tool timeout and still return nothing (viewport captures don't
            # depend on the cap, so they stay allowed).
            return self.result(
                "Cannot read the page size, so full_page is refused. Use a "
                "viewport capture.", success=False,
            )
        fits, mpx = capture_fits(shot_w, shot_h, max_px)
        if full_page and not fits:
            # captureBeyondViewport paints the whole page whatever `clip` says,
            # so an oversized capture only stalls into a timeout: refuse up
            # front, with the real size.
            return self.result(
                f"Page is {mpx:.1f} Mpx; full_page is capped at "
                f"{max_px // 1_000_000} Mpx (full_page_max_pixels). Use viewport "
                f"captures or raise it.", success=False,
            )
        scale = float(metrics.get("devicePixelRatio") or 1.0)
        # `region` is in image pixels, the grid labels in document pixels
        doc_origin_x = int(metrics.get("scrollX") or 0)
        doc_origin_y = int(metrics.get("scrollY") or 0)
        origin_x = 0 if full_page else int(round(doc_origin_x * scale))
        origin_y = 0 if full_page else int(round(doc_origin_y * scale))

        # Read before the capture: this only explains a timeout, and the answer
        # must already be there when the capture gives up. None = could not
        # read it (a stalled page hangs this evaluate too), so then no claim is
        # made about the cause.
        background = await page.visibility_state(self._cdp, sid, timeout=1.0) == "hidden"

        # a full-page capture gets part of the call, not all of it: if
        # Chrome stalls on this document, the rest of the time is still usable
        window = first_capture_window(budget.left(), full_page)
        fallback_note = None
        try:
            path, raw = await page.capture_screenshot(
                self._cdp, sid, self._screenshots_dir,
                full_page=full_page, fmt=fmt, quality=quality, timeout=window)
        except TimeoutError as e:
            self.log("browser", f"screenshot: {core.detail_error(e)}")
            if not full_page:
                return self.result(
                    self._capture_failed_text(background=background),
                    success=False,
                )
            # Chrome did not answer the full-page capture: use what the call
            # still has and return the viewport, saying exactly that
            fallback_note = ("Full-page capture timed out; this is the viewport. "
                             "Reload the tab (f5) if it repeats.")
            self.log("browser", fallback_note)
            try:
                path, raw = await page.capture_screenshot(
                    self._cdp, sid, self._screenshots_dir,
                    full_page=False, fmt=fmt, quality=quality)
            except TimeoutError as e2:
                self.log("browser", f"screenshot: {core.detail_error(e2)}")
                return self.result(
                    self._capture_failed_text(background=background),
                    success=False,
                )
            # what was written is a viewport capture: region and `full_page`
            # have to describe the image, not the request
            full_page = False
            origin_x = int(round(doc_origin_x * scale))
            origin_y = int(round(doc_origin_y * scale))

        # parsed dimensions first: they feed both the grid cost estimate and the
        # region hint, so neither can drift from the pixels actually written
        # (clipping, Chrome rounding). draw_grid never changes the dimensions.
        px_w, px_h = page.image_size(raw, fmt) or (0, 0)

        # grid: drawn onto the saved PNG (no page mutation, no canvas limit).
        # Pure CPU with no command timeout watching it, so it only runs when it
        # still fits in this call (~0.85 s per megapixel).
        grid_note = None
        if grid_spacing:
            fits, need = grid_fits(px_w, px_h, budget.left())
            if not fits:
                grid_note = (f"Grid not drawn (needs ~{need:.1f}s, "
                             f"{budget.left():.1f}s left). Plain capture attached.")
                self.log("browser", grid_note)
            else:
                try:
                    raw = page.draw_grid(raw, grid_spacing, doc_origin_x,
                                         doc_origin_y, scale=scale)
                    with open(path, "wb") as f:
                        f.write(raw)
                except Exception as e:
                    grid_note = f"grid not drawn: {core.detail_error(e)}"
                    self.log("browser", grid_note)

        # region describes the IMAGE that was actually written: the parsed
        # dimensions win over the pre-capture metrics
        if full_page:
            region = {
                "x": 0, "y": 0,
                "width": px_w or shot_w or 0,
                "height": px_h or shot_h or 0,
            }
        else:
            region = {
                "x": origin_x, "y": origin_y,
                "width": px_w or int(round(int(metrics.get("innerWidth") or 0) * scale)),
                "height": px_h or int(round(int(metrics.get("innerHeight") or 0) * scale)),
            }
        region = {key: int(value or 0) for key, value in region.items()}
        # the tool result stays plain text (path + geometry): the image can't
        # travel in a `tool` message, so it is added to context as a user
        # message instead - the inference API only accepts images in user
        # turns. adding it directly avoids channel.send() inside a tool call,
        # which deadlocks the channel's stream chain.
        if scale == 1.0:
            hint = ("Screenshot image is attached. Coordinates: doc_x = shot_x + region.x, "
                    "doc_y = shot_y + region.y.")
        else:
            hint = (f"Screenshot image is attached. The capture has {scale:g} image px per "
                    f"document px: doc_x = (shot_x + region.x) / {scale:g}, "
                    f"doc_y = (shot_y + region.y) / {scale:g}. `region` itself is in image px.")
        if grid_spacing and not grid_note:
            hint += " The grid labels are document coordinates."
        meta = {
            "path": path,
            "file": os.path.basename(path),
            "bytes": len(raw),
            "full_page": bool(full_page),
            "region": region,
            "format": fmt,
            "hint": hint,
        }
        if fmt == "jpeg":
            meta["quality"] = quality
        if scale != 1.0:
            meta["scale"] = scale
        if fallback_note:
            meta["note"] = fallback_note
        if grid_spacing:
            meta["grid"] = {"spacing": grid_spacing, "applied": grid_note is None}
            if grid_note:
                meta["grid"]["reason"] = grid_note

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

    # A `vision` tool (screenshot -> channel.send(files=...) -> text) is not
    # offered: calling channel.send from inside a tool deadlocks the channel.

    @core.module.tool
    async def evaluate(self, expression: str):
        """Evaluates JavaScript in the current tab and returns the result.

        Args:
            expression: JavaScript expression to evaluate (e.g. 'document.title').
        """
        await self._ensure_connection()
        sid = self._current_session()
        # one command: it gets the whole call, so a slow script is bounded by
        # core.tool_timeout and nothing else
        params = {
            "expression": expression,
            "awaitPromise": True,
            "returnByValue": True,
            "userGesture": True,
        }
        res = await self._cdp.cmd("Runtime.evaluate", params, sid)
        if rejected_top_level_await(res.get("exceptionDetails")):
            # a script can't use `await` at its top level. Same call,
            # wrapped once - the caller should not have to know to wrap it.
            params["expression"] = wrap_await(expression)
            res = await self._cdp.cmd("Runtime.evaluate", params, sid)
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
        """Returns recent console messages, errors, and uncaught exceptions from all tabs.

        Args:
            count: How many of the most recent entries to return (0 = none).
            clear: Clear the buffer after reading.
        """
        await self._ensure_connection()
        entries = tail_entries(self._console_log, count)
        if clear:
            self._console_log.clear()
        return self.result(entries if entries else "No console output recorded.")

    @core.module.tool
    async def network_requests(self, count: int = 20, clear: bool = False):
        """Returns recent network requests (method + URL + status). Requires enable_network_monitoring.

        Args:
            count: How many of the most recent entries to return (0 = none).
            clear: Clear the buffer after reading.
        """
        if not self.config.get("enable_network_monitoring", False):
            return self.result(
                "enable_network_monitoring is off. Enable it and restart.",
                success=False,
            )
        await self._ensure_connection()
        entries = tail_entries(self._network_log, count)
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
        """Returns the cookies of the current tab's site (or of a specific domain), including their values.

        Args:
            domain: Optional domain to look up instead of the current tab (e.g. 'example.com').
        """
        await self._ensure_connection()
        sid = self._current_session()
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
    async def list_tabs(self):
        """Lists all open tabs (pages) in the controlled browser."""
        await self._ensure_connection()
        targets = await page.list_page_targets(self._cdp)
        active = await self._active_target_ids(targets)
        return self.result([
            {
                "id": t.get("targetId"),
                "title": t.get("title", ""),
                "url": t.get("url", ""),
                # current = the tab this module drives (the word Playwright MCP
                # uses); active = the tab the user is looking at (the field
                # chrome.tabs.Tab gives the tab selected in its window). They
                # differ as soon as the user clicks another tab.
                "current": t.get("targetId") == self._current_target,
                "active": t.get("targetId") in active,
            }
            for t in targets
        ])

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
        res = await self._cdp.cmd(method, params or {}, sid)
        return self.result(res)


# ---------------------------------------------------------------------------
# Tool error boundary
# ---------------------------------------------------------------------------
# Every @core.module.tool method is wrapped once here, below the class, so a
# tool added later is covered automatically; the `_is_guarded` marker keeps the
# wrapping idempotent if the file is re-executed.
#
# Only browser-facing failures are converted: CDP rejections, timeouts, dropped
# connections, and the module's own RuntimeErrors ("Unknown ref @e9 …", "No tab
# is attached …"). Programming errors (TypeError, KeyError, …) still escape.

def _error_result(self, tool_name, exc):
    """Map a browser failure to an actionable tool result."""
    # the log keeps the compact error (full traceback in --debug); the model
    # gets the short, actionable text
    self.log("browser", f"{tool_name}: {core.detail_error(exc)}")
    text = str(exc) or exc.__class__.__name__

    if isinstance(exc, CDPError):
        if "-32001" in text or "Session with given id not found" in text:
            text = f"{text}. Call list_tabs, then switch_tab or new_tab."
        elif "-32601" in text:
            text = f"Chrome does not support that command: {text}"
        elif "-32602" in text:
            text = f"Chrome rejected the parameters: {text}"
        else:
            text = f"The browser rejected the command: {text}"
    elif isinstance(exc, TimeoutError):
        # name the setting that actually bound it, never a module-private number
        #; no per-tool advice - screenshot explains its own timeouts
        text = timeout_text(text)
    elif isinstance(exc, ConnectionError):
        text = "Connection lost. Call open to reconnect (or new_tab)."
    # RuntimeError / OSError already carry a readable message

    return self.result(text, success=False)


def _guarded(tool):
    """Async wrapper that opens the call's budget and turns failures into tool errors.

    The budget lives here, not in each tool: one deadline per call, taken
    before the body runs (so it covers Chrome launch), published through
    util.CALL_BUDGET so every CDP command - including the ones in page.py -
    gets what is left of it. A tool added later is bounded automatically.
    """
    @functools.wraps(tool)
    async def wrapper(self, *args, **kwargs):
        token = CALL_BUDGET.set(self._new_budget())
        try:
            try:
                return await tool(self, *args, **kwargs)
            except (CDPError, TimeoutError, ConnectionError, RuntimeError, OSError) as exc:
                return _error_result(self, tool.__name__, exc)
        finally:
            CALL_BUDGET.reset(token)
    wrapper._is_guarded = True
    return wrapper


def _guard_tools(cls):
    """Wrap every @core.module.tool method of `cls` exactly once."""
    for name, attr in list(vars(cls).items()):
        if (callable(attr) and getattr(attr, "_is_tool", False)
                and not getattr(attr, "_is_guarded", False)):
            setattr(cls, name, _guarded(attr))


# every AI tool on the class goes through the boundary above
_guard_tools(Browser)

"""Browser module: a real Chrome/Chromium (or Brave) driven over the DevTools
Protocol.

File map: module.py holds the Browser class (settings, lifecycle, connection)
and assembles the mixins the tool loader cannot see through inheritance;
tools_*.py hold the AI tools, events.py the CDP event handling, restart.py the
headless<->visible state, webui_routes.py the panel routes; cdp.py the
transport and process layer, page.py the page/DOM helpers, util.py the pure
helpers. Only aiohttp is required, and only a Browser instance keeps state.
Profile and screenshots live under data/browser/.
"""

import core
import asyncio
import functools
import os
import time
import urllib.parse
from collections import deque

import aiohttp

# A module reload re-executes this file only: the mixins, cdp, page and util
# stay in sys.modules, so edits to any of them need a full server restart.
from . import cdp, page
from .cdp import CDPClient, CDPError, DEFAULT_PORT
from .events import CdpEvents
from .page import DEFAULT_SNAPSHOT_CHARS
from .restart import RestartState
from .tools_data import DataTools
from .tools_page import PageTools
from .tools_session import SessionTools
from .util import CALL_BUDGET, headless_from_version, timeout_text, tool_budget
from .webui_routes import WebuiRoutes


# ---------------------------------------------------------------------------
# The Module
# ---------------------------------------------------------------------------

class Browser(CdpEvents, RestartState, WebuiRoutes,
              SessionTools, PageTools, DataTools, core.module.Module):
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

    def _page_source(self):
        """Provenance for a fence: 'webpage:<host>' of the tab being driven."""
        url = self._target_url.get(self._current_target) or ""
        host = urllib.parse.urlparse(url).hostname
        return f"webpage:{host}" if host else "webpage:unknown"

    async def _tab_entries(self, targets):
        """The tabs as {id, title, url, current, active} rows.

        `current` is the tab this module drives; `active` the one the user is
        looking at. Shared by status, list_tabs and the panel's status route.
        """
        active = await self._active_target_ids(targets)
        return [
            {"id": t.get("targetId"), "title": t.get("title", ""),
             "url": t.get("url", ""),
             "current": t.get("targetId") == self._current_target,
             "active": t.get("targetId") in active}
            for t in targets
        ]

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

    async def _sid(self):
        """The current tab's session id, connecting and attaching first."""
        await self._ensure_connection()
        return self._current_session()

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

    # A `vision` tool (screenshot -> channel.send(files=...) -> text) is not
    # offered: calling channel.send from inside a tool deadlocks the channel.


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


def _adopt_tools(cls):
    """Copy the mixins' tool methods onto the class itself.

    core's tool loader reads type(module).__dict__ without walking the MRO, so
    an inherited tool is silently not registered: the method has to exist on
    the class object. WebUI routes need no such trick - the server walks the
    MRO - which is why only tools are copied.
    """
    for mixin in (SessionTools, PageTools, DataTools):
        for name, member in vars(mixin).items():
            if getattr(member, "_is_tool", False):
                setattr(cls, name, member)
    return cls


_adopt_tools(Browser)
# every AI tool on the class goes through the boundary above
_guard_tools(Browser)

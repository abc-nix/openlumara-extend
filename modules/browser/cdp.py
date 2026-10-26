# -- AI GENERATED CODE (Qwen3.8-Flash-Next) :: (2026-10-07)
"""
Transport & process layer for the browser module (step-6 split of module.py;
see README.md).

Everything about *finding, launching and talking to* a Chrome/Chromium
process over the Chrome DevTools Protocol lives here. These helpers are
stateless with respect to the module: config/http/log are passed in as
arguments. Module state (connection, tabs, refs, logs, dialogs) stays in
module.py. Dependency direction: module.py -> cdp.py (never the reverse).
"""

import asyncio
import json
import os
import shutil

import aiohttp

import core

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_PORT = 9222
CDP_CALL_TIMEOUT = 15.0        # per-command timeout (seconds)
SCREENSHOT_TIMEOUT = 20.0
MAX_MSG_SIZE = 64 * 1024 * 1024  # screenshots/AX trees can be big


class CDPError(Exception):
    """Raised when the browser returns an error for a CDP command."""


class CDPClient:
    """Minimal async CDP client over a single aiohttp WebSocket connection.

    Connects to the *browser* endpoint (ws://.../devtools/browser/<id>),
    multiplexes commands by id, and dispatches events to the on_event
    callback given at construction.
    """

    def __init__(self, http, ws_url, on_event, log):
        self.http = http
        self.ws_url = ws_url
        self.on_event = on_event
        self.log = log
        self.ws = None
        self._id = 0
        self._pending = {}
        self._reader_task = None
        self._closed = False

    async def connect(self):
        self.ws = await self.http.ws_connect(
            self.ws_url, max_msg_size=MAX_MSG_SIZE, heartbeat=30
        )
        self._reader_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self):
        try:
            async for msg in self.ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue
                    if "id" in data:
                        fut = self._pending.pop(data["id"], None)
                        if fut and not fut.done():
                            if "error" in data:
                                err = data["error"]
                                fut.set_exception(CDPError(
                                    f"{err.get('code', '?')} {err.get('message', '?')}"
                                ))
                            else:
                                fut.set_result(data.get("result", {}))
                    elif "method" in data:
                        try:
                            self.on_event(data)
                        except Exception as e:
                            # event handlers must never kill the reader
                            self.log("browser", f"event handler error: {core.detail_error(e)}")
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.log("browser", f"CDP reader stopped: {e}")
        finally:
            self._closed = True
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("CDP connection closed"))
            self._pending.clear()

    async def cmd(self, method, params=None, session_id=None, timeout=CDP_CALL_TIMEOUT):
        """Send a CDP command and await its result. Raises on error/timeout."""
        if self.ws is None or self.ws.closed:
            raise ConnectionError("Not connected to the browser")
        self._id += 1
        mid = self._id
        msg = {"id": mid, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending[mid] = fut
        await self.ws.send_json(msg)
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(mid, None)
            raise TimeoutError(f"CDP command '{method}' timed out after {timeout}s")

    async def close(self):
        if self.ws and not self.ws.closed:
            try:
                await self.ws.close()
            except Exception:
                pass
        if self._reader_task:
            self._reader_task.cancel()


# ---------------------------------------------------------------------------
# Process & endpoint management (stateless: config/http/log passed in)
# ---------------------------------------------------------------------------

def port_of(config):
    try:
        return int(config.get("port", default=DEFAULT_PORT))
    except (TypeError, ValueError):
        return DEFAULT_PORT


def profile_dir(config):
    udd = config.get("user_data_dir", default="")
    if udd:
        return udd if os.path.isabs(udd) else core.get_data_path(udd)
    return core.get_data_path("browser/profile")


def find_binary(config):
    choice = config.get("browser_binary", default="auto")
    if choice == "custom":
        path = config.get("custom_binary_path", default="")
        if path and os.path.exists(path):
            return path
        raise RuntimeError(
            f"custom_binary_path '{path}' does not exist. "
            "Set it to the full path of a browser binary."
        )
    candidates = {
        "google-chrome": ["google-chrome"],
        "chromium": ["chromium", "chromium-browser"],
        "brave-browser": ["brave-browser", "brave"],
        "auto": ["google-chrome", "chromium", "chromium-browser",
                 "brave-browser", "brave"],
    }
    for name in candidates.get(choice, []):
        found = shutil.which(name)
        if found:
            return found
    raise RuntimeError(
        f"No browser binary found for '{choice}'. Install Chrome or "
        "Chromium, or set browser_binary to 'custom' and provide "
        "custom_binary_path."
    )


async def probe(http, port, tries=1, delay=0.5):
    """Return /json/version dict if a CDP endpoint is up, else None."""
    url = f"http://127.0.0.1:{port}/json/version"
    for _ in range(tries):
        try:
            async with http.get(url, timeout=2) as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception:
            pass
        await asyncio.sleep(delay)
    return None


async def launch_browser(config, log, http, headless=None):
    """Launch the browser process and wait for its CDP endpoint.

    headless=None -> follow the config setting; an explicit bool overrides
    it (used by the restart headless<->visible flow, session-scoped).
    Returns (proc, version_info); raises if the endpoint never comes up.
    """
    binary = find_binary(config)
    profile = profile_dir(config)
    port = port_of(config)
    os.makedirs(profile, exist_ok=True)
    args = [
        binary,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        f"--window-size={config.get('window_size', default='1280,800')}",
    ]
    if headless if headless is not None else bool(config.get("headless", False)):
        args.append("--headless=new")
    log("browser", f"Launching: {' '.join(args[:4])} ...")
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    info = await probe(http, port, tries=20, delay=0.5)
    if info is None:
        raise RuntimeError(
            "Browser launched but the CDP endpoint never came up. "
            f"Check for errors (port {port}, profile {profile})."
        )
    return proc, info

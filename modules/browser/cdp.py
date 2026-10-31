# -- AI GENERATED CODE (Qwen3.8-Flash-Next) :: (2026-10-07)
"""Transport and process layer: finding, launching and talking to a Chrome or
Chromium process over the DevTools Protocol.

Stateless with respect to the module - config/http/log are passed in. Module
state (connection, tabs, refs, logs, dialogs) lives in module.py; the
dependency goes module.py -> cdp.py and never back.
"""

import asyncio
import json
import os
import shutil
import signal
import time

import aiohttp

import core

from .util import CALL_BUDGET, resolve_timeout

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_PORT = 9222

# Deadline for calls made outside a tool call (webui routes, tests). Inside one,
# a command gets what is left of the call's budget instead (util.CALL_BUDGET),
# so this number is only the fallback.
CDP_CALL_TIMEOUT = 10.0
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

    async def cmd(self, method, params=None, session_id=None, timeout=None):
        """Send a CDP command and await its result. Raises on error/timeout.

        `timeout=None` means what is left of the running tool call's budget
        (module._guarded sets it); CDP_CALL_TIMEOUT is only the fallback for
        calls made outside a tool call.
        """
        if self.ws is None or self.ws.closed:
            raise ConnectionError("Not connected to the browser")
        timeout = resolve_timeout(timeout, CALL_BUDGET.get(), CDP_CALL_TIMEOUT)
        self._id += 1
        mid = self._id
        msg = {"id": mid, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending[mid] = fut
        try:
            await self.ws.send_json(msg)
        except Exception as exc:
            # the future is already registered: drop it or it outlives the send
            self._pending.pop(mid, None)
            raise ConnectionError(
                f"Could not send '{method}' to the browser: {exc}") from None
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            # one decimal: 14.499991990000126s reads like a floating-point bug
            raise TimeoutError(f"CDP command '{method}' timed out after {timeout:.1f}s")
        finally:
            # the command owns its entry: the reader pops a reply, this pops
            # the other outcomes (timeout, cancellation, no answer at all)
            self._pending.pop(mid, None)

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
        f"No browser binary for '{choice}'. Install Chrome/Chromium, or set "
        "browser_binary='custom'."
    )


async def probe(http, port, tries=1, delay=0.5, deadline=None):
    """Return /json/version dict if a CDP endpoint is up, else None.

    `deadline` (time.monotonic()) caps the whole wait, so a launch that never
    comes up answers inside the tool call instead of at core's timer.
    """
    url = f"http://127.0.0.1:{port}/json/version"
    for _ in range(tries):
        try:
            async with http.get(url, timeout=2) as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception:
            pass
        if deadline is not None and time.monotonic() >= deadline:
            break
        await asyncio.sleep(delay)
    return None


def read_pidfile(path):
    """{'pid', 'port', 'profile'} from a pid file, or None if unusable."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        pid = int(data.get("pid"))
        if pid <= 0:
            return None
        return {"pid": pid, "port": int(data.get("port") or 0),
                "profile": str(data.get("profile") or "")}
    except (OSError, ValueError, TypeError):
        return None


def write_pidfile(path, pid, port, profile):
    """Record which process this module launched, so a reload can still own it."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"pid": int(pid), "port": int(port),
                       "profile": str(profile)}, f)
        return True
    except OSError:
        return False


def clear_pidfile(path):
    try:
        os.remove(path)
    except OSError:
        pass


def process_owns_browser(pid, port, profile):
    """Is `pid` still the browser this module launched?

    A bare pid proves nothing: it can be reused, and killing a stranger's
    process is worse than losing ownership. The /proc command line must name
    this module's profile directory and debugging port, both unique to our
    launch. Where that cannot be read, the answer is no.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmdline = f.read()
    except OSError:
        return False
    return (f"--user-data-dir={profile}".encode() in cmdline
            and f"--remote-debugging-port={port}".encode() in cmdline)


async def stop_pid(pid, wait=3.0):
    """SIGTERM a process by pid, then SIGKILL it; True once it is gone."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        await asyncio.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    await asyncio.sleep(0.2)
    try:
        os.kill(pid, 0)
    except OSError:
        return True
    return False


async def stop_process(proc, wait=3.0):
    """Terminate a process we started and make sure it is gone.

    A terminate() that is ignored (or a process already gone) falls back to
    kill(), and no exception escapes: the caller's own error matters more.
    """
    if proc is None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    except Exception:
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=wait)
        return
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=1.0)
    except Exception:
        pass


async def launch_browser(config, log, http, headless=None, deadline=None):
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
    info = await probe(http, port, tries=20, delay=0.5, deadline=deadline)
    if info is None:
        # a browser that never became reachable must not stay running: it would
        # keep a whole Chrome and its profile lock alive with nothing using it
        await stop_process(proc)
        if deadline is not None and time.monotonic() >= deadline:
            # the tool call's own budget cut the wait short: say so, and let
            # the error boundary name the setting to raise
            raise TimeoutError(
                "Chrome did not expose a CDP endpoint before the time for "
                "this tool call ran out"
            )
        raise RuntimeError(
            "Browser launched but the CDP endpoint never came up. "
            f"Check for errors (port {port}, profile {profile})."
        )
    return proc, info

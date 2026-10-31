"""CDP event handling for one browser connection: tab and URL bookkeeping,
the console and network buffers, and dialog tracking.

A mixin: module.py's Browser inherits it. It defines no tools, so the tool
loader (which scans the module class's own __dict__) never sees it.
"""

import asyncio

from .util import MAX_URL_LEN, drop_target


class CdpEvents:
    """What the browser reports, folded into the module's own buffers."""

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
                    # CDP resource type (Document, Script, Image, XHR, Fetch..)
                    "type": params.get("type") or "Other",
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


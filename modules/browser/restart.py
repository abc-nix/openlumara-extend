"""Restart headless <-> visible: capture a tab set before the browser is
stopped, recreate it after the relaunch.

A mixin, like events.py: state only, no tools.
"""

import json
import os
from datetime import datetime

import core

from . import page
from .util import captured_active_index, restorable_url


class RestartState:
    """Capture and restore what survives a browser relaunch."""

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


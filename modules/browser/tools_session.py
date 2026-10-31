"""Session tools: start/connect the browser, report its state, close it.

A mixin: the tool loader scans only the module class's own __dict__, so
module.py copies these methods into Browser (see _adopt_tools).
"""

import core

from . import page


class SessionTools:
    """open, close and status."""

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
        rows = await self._tab_entries(targets)
        brief = lambda row: {"title": row["title"], "url": row["url"]}
        return self.result({
            "connected": True,
            "owned_process": self._owns_browser(),
            # what the running browser really is (not what the setting says),
            # and the one timeout every tool call shares - the number to raise
            # when a capture says it ran out of time
            "headless": self._headless_actual(),
            "tool_timeout": self._tool_timeout(),
            "current_tab": next((brief(r) for r in rows if r["current"]), None),
            # the tab the user is looking at, read from the page itself
            # (document.visibilityState); None when the window is minimised.
            # It equals current_tab until the user clicks another tab.
            "active_tab": next((brief(r) for r in rows if r["active"]), None),
            "tabs": rows,
        })


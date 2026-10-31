"""The sidebar panel's API routes (/api/ext/browser/...).

A mixin: the webui server resolves routes through the class MRO, so nothing
has to be copied into the module class's __dict__ (unlike tools).

Contract (channels/webui/api.py): handlers are async with signature
(self, body=None, query=None) and PRIVATE, so the tool loader never registers
them as AI tools.
"""

import base64
import os

import core

# webui extension routes: zero-dependency marker (channels/webui/api.py
# contract). channels/ has no __init__.py (namespace packages) and api.py
# imports nothing, so this import has no side effects even if the webui
# channel were disabled.
import channels.webui.api as webui

from . import page


class WebuiRoutes:
    """Panel endpoints: passive status, open/close, restart and screenshots."""

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
            rows = await self._tab_entries(targets)
            current_tab = next(({"title": row["title"], "url": row["url"]}
                                for row in rows if row["current"]), None)
            active_tab = next(({"title": row["title"], "url": row["url"]}
                               for row in rows if row["active"]), None)
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


"""theme_gallery — throwaway developer tool (NOT a feature to upstream).

Lets you see and compare the WebUI's real elements under different theme
families and modes, side by side, WITHOUT ever changing the app's real theme.

How it plugs into the webui (all standard module-extension hooks, no core edits):
  webui/templates/sidebars/theme_gallery.html (+ .svg) -> a tab in the left panel
  webui/templates/chat/index.html  -> macro below_header() mounts the overlay
                                       inside #chat-window (same trick testbench uses)
  webui/assets/css|js/             -> auto-injected into every page (inert until the tab is open)

It never writes theme or settings data: it only reads theme JSON via the app's
own /api/themes endpoints and CSS class names straight from the templates.
(Not entirely side-effect free: closeOverlay() goes through the sidebar's
setTab(), which persists 'sidebarTab' in localStorage, and captureSettings()
drives the live settings component while #modals is hidden. See BUGS.md.)
"""

import core


class ThemeGallery(core.module.Module):

    settings = {
        "enable_webui_extension": {
            "description": "Show the theme gallery tab in the sidebar and load its webui assets.",
            "default": True,
        },
    }

    category = "development"

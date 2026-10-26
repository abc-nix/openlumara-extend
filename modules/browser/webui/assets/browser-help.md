#### What is this?

A dedicated Chrome window the AI can drive for you: it browses, clicks, types, takes screenshots, and reads the page console and network activity. Log in to sites once — this profile is private to the module and persists across restarts.

#### Open / Close

**Open** launches the browser (or attaches to one already running on the debug port). **Close** quits a browser the module launched; if *you* started the browser yourself, it only disconnects.

#### Restart headless ↔ visible

Reopens the same session without a window (or with one). Tabs, scroll positions and filled (non-secret) form fields are restored — browsing history and passwords are **not** (Chrome limitation). The flip applies to this session only; the permanent default is the `headless` setting.

#### Settings worth knowing

- `headless` — start invisible by default
- `auto_launch` — launch automatically when the AI needs the browser
- `close_browser_on_shutdown` — quit the browser when OpenLumara exits
- `enable_network_monitoring` — let the AI see web requests

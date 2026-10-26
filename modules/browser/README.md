# browser module — WebUI integration notes

General module description: see the `module.py` header docstring.
This file documents how the module plugs into the webui without touching
core code, plus refactoring notes. (Added 2026-10-07.)

## Routes (backend, defined in module.py)
- Four `@webui.route`-marked **private** handlers on the `Browser` class:
  `_route_ui_status`, `_route_ui_open` (POST), `_route_ui_close` (POST),
  `_route_ui_restart` (POST).
- The forwarder in `channels/webui/channel.py` exposes them at
  `/api/ext/browser/<path>`, unwraps a returned `self.result()` dict into the
  standard api envelope, and turns exceptions into `{success: false}`.
- The underscore prefix is required: the tool loader skips `_`-prefixed
  methods, so these routes never leak into the AI tool list.
- `_route_ui_status` is strictly **passive** (never `_ensure_connection`):
  merely loading a page or polling must never launch a browser. The public
  `status()` tool deliberately does auto-launch.

## Assets (webui/ folder, auto-discovered by the webui channel)
- `templates/sidebar.html` — `bottom_of_tabbar()` macro: the icon-rail
  button. Visibility follows the module-wide `enable_webui_extension` gate.
- `templates/modals/browser.html` — the modal (`$store.ui.openModal('browser')`).
- `assets/js/browser.js` — shared `Alpine.store('browserPanel')`; both
  templates bind to it so their state cannot drift. Polling runs only while
  the modal is open (watchdog inside `refresh()` self-stops leaked timers).
- `assets/css/browser.css` — status-dot badge (scoped; see the containing-
  block comment in that file) and `margin-top:auto`, which pins the button
  to the rail bottom.
- Reserved names (no core collisions as of 2026-10-07): store `browserPanel`,
  ext asset name `browser`, all `browser-ext-*` classes.

## Restart headless <-> visible (owned browser only)
- `restart` route flips the mode, relaunches the module-launched browser
  and restores state. A session-only override (`_headless_override`) wins
  over the `headless` **setting** until restart of OpenLumara; the setting
  stays the permanent source of truth (never written from module code —
  core has no module-safe config setter; worth proposing one upstream).
- Restored: open http/about tabs (URLs, active tab), scroll positions,
  non-secret form values (native setters + input/change events so SPA
  inputs react), window bounds (visible mode), and of course cookies/
  localStorage via the persistent profile.
- Never restored (CDP limits): navigation history stacks (no inject API),
  sessionStorage semantics, downloads, open dialogs. Password fields are
  deliberately never captured. External browsers are refused (we never
  kill a browser we did not launch).
- A sanitized snapshot is written to `data/browser/last_state.json`
  (crash-recovery aid for a future "restore tabs" affordance).
- Tested by `tmp/browser_restart_harness.py` (gate, capture-before-kill
  ordering, tab skip list, injection, activation, bounds, launch flag).

## Apply rules (what needs what)
- Edit template/CSS/JS: hard browser refresh only (per-request extension
  lists + Jinja auto_reload).
- New files, route/class changes, settings-structure changes: server restart.
- Enable/disable the module: the webui auto-restarts the server (module-list
  change); the extension UI then reappears only after a page reload (Ctrl+R).

## Refactoring notes
- Every AI tool must stay defined **directly** on the `Browser` class:
  `tool_loader` scans `type(module).__dict__` and does not walk the MRO —
  tools moved to a mixin/base class silently vanish.
- File split APPLIED 2026-10-07: `cdp.py` (transport/process: `CDPClient`,
  binary/port/profile discovery, launching, probing) + `page.py` (AX-tree
  walker, refs, screenshots/grid, metrics — stateless helpers) +
  `module.py` (class: settings, lifecycle, routes, all tools with thin
  bodies). Dependency direction module.py -> cdp.py, page.py (never the
  reverse); the class owns all state. Verified with
  `tmp/browser_surface_harness.py` (public `type(Browser).__dict__`,
  signatures + byte-identical tool docstrings + routes + settings).
  Note: single-module-file is the upstream core convention (http module is
  also ~1500 lines) — this split is a local maintainability choice; to
  upstream, the three files can be concatenated back into module.py with
  no other changes.
- Parked variants/old versions live in the `.backups/` folder that sits
  beside the openlumara repo (outside the git tree, e.g.
  `module.py.*.browser*.bak`, `index.html.*.browser-belowpage-variant`).

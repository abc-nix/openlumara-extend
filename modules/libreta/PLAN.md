# libreta — plan and status

One user-facing text surface for OpenLumara: **Notes** (rendered prose) and
**Code** (source) are two views of the same thing, a text file.

## Decisions (locked)

| # | Decision |
|---|---|
| 1 | **No AI tools in libreta.** The `editor` module stays the AI's file backend, so notes and code keep exactly one tool vocabulary. |
| 2 | **One data model: real files.** Direct sandboxed file I/O, no `StorageDict` mirror. |
| 3 | **Notes is just a workspace.** Single source of truth: `editor.workspaces`. |
| 4 | `editor.enable_webui_extension: false` later, so only libreta's tree exists while editor keeps its tools. |
| 5 | **codeeditor is retired by libreta** once code mode reaches parity (quick-open, keymaps, syntax gate). Not yet. |
| 6 | Name: `libreta`; module lives here (`user_modules/libreta` via the extend symlink). |
| 7 | **CodeMirror 6** is the editor engine for both views. The bundle in `webui/assets/vendor/cm6/` is built once with esbuild and vendored — no npm at runtime. CodeMirror 5 is not shipped by this module. |
| 8 | **Raw markdown is the source of truth.** Live-preview decorations are view-only; saves round-trip byte-identically. |

## Status

- **P0a — done.** Sidebar tab with Files / Recent / Pinned / Options, workspace
  tree, full-window cover with document tabs, rendered markdown, pins and
  recents persisted server-side.
- **P0a.1 — done.** Options writes module settings to `config.yml`;
  `code_autowrap` (default on) with a per-file override.
- **P0b — done.** Code view with line numbers, wrap toggle, per-tab undo, dirty
  tabs, `Ctrl/⌘+S`, close prompt, **mtime-guarded save** with a conflict banner,
  create / rename / delete.
- **P0c — done (CM6 migration).** Notes mode is one continuous **live-preview**
  editor (Obsidian style): markdown stays the text, formatting renders in place,
  markers appear only on the caret's line. Split view survives as an optional
  escape hatch; block-by-block editing is removed. Code view moved to CM6 too.
- **P2 — in progress (parity with coder/codeeditor).** Quick-open Ctrl/⌘+P
  done (route + palette); codeeditor disabled. **Autosave done** (`autosave_interval`,
  seconds, 0 = off; one timer per open document, same mtime guard as Ctrl+S,
  banner per document). Next: the CM6 editor extras (fold, brackets, selection
  matches, smart Enter, Ctrl+/), go-to-line, caret position; optional
  tree-sitter lint. Inventory and skip list live in
  `Notes/libreta/libreta-coder-parity.md`.
- **P2.0 — done (2026-11-04). Autosave.** `autosave_interval` (0 = off),
  `autosave_default` + per-file switch (caret next to Save) stored in
  `data/libreta/autosave.json`; the save button is always visible and highlights
  while there are unsaved changes.
- **P2.0b — done (2026-11-04). Paste as markdown.** Notes mode converts the
  clipboard's HTML flavour with vendored turndown (Code mode and
  `Ctrl/⌘+Shift+V` stay raw; images paste as their path).
- **P2.1 — done (9290ee4). Chat views.** full / docked (right panel, chat
  narrows) / floating (centred card, file picker instead of tabs), remembered in
  localStorage, one cycle button in the doc bar. Details:
  `Notes/libreta/libreta-views-plan.md`.
- **P3 — not started.** Wikilinks + backlinks, tags/properties, callouts,
  attachments, in-vault search.

## Editor bundle

`webui/assets/vendor/cm6/` — 612 KB raw / 217 KB gzipped IIFE (`window.LibretaCM`):
CodeMirror 6 core + markdown + code languages + the MIT live-preview engine
(`@plannotator/atomic-editor`) with its frontmatter parser. Rebuild recipe and
versions are in `vendor/cm6/README.md`. React is external, not included.

The editor keeps one `EditorState` per tab **outside** the Alpine store (CM6
rejects proxied extensions); line numbers, wrap, language and live preview are
`Compartment`s reconfigured per tab.

## Enabling it

```yaml
user_modules:
  enabled:
  - libreta

user_modules:
  settings:
    editor:
      workspaces:
      - ... existing entries ...
      - /home/abc/Documents/Notes
```

Then restart. Without a notes workspace libreta still works; it shows whatever
workspaces the editor module already has.

## Files

```
module.py                        settings + /api/ext/libreta routes (no tools)
files.py                         containment, listing, reading, writing, trash
tests/                           52 unit tests (throwaway vaults in /tmp)
webui/templates/sidebars/libreta.html (+ .svg)   the tab
webui/templates/chat/index.html                  claims the below_header slot
webui/templates/chat/cover.html                  the reading cover
webui/assets/css/libreta.css                     styling (theme vars only)
webui/assets/js/libreta.js                       stores + panel component
webui/assets/vendor/cm6/                         CM6 bundle, atomic.css, README, licenses
```

Routes: `workspaces` · `tree` · `read` · `save` · `preview` · `create` ·
`mkdir` · `rename` · `delete` · `trash` (+ restore/purge/empty) · `settings` ·
`settings/save` · `state*`.

## Known limitations to revisit

1. Folder rename/delete is refused by the backend.
2. Trash has no “restore all” and shows the current workspace only.
3. `read` refuses files larger than `max_read_bytes` (1 MB) instead of windowing.
4. Wiki links are supported by the engine but not wired to the vault.
5. Relative image paths resolve against the page URL, not the workspace.
6. Find & replace exists per document; cross-file search does not (backend route + results pane still to build).
7. core `notes` module still enabled although unused; `codeeditor` still ships
   its own CM5 vendor (retire both once libreta covers them).
8. **Keyboard shortcuts live in localStorage** (`libreta.keys`, edited by the
   modal in `webui/assets/js/libreta-hotkeys.js`). Deliberate for now: it applies
   immediately with no restart, like `show_line_numbers`. When the bindings
   should follow the user between browsers, move that object to
   `data/libreta/hotkeys.json` behind a `state/hotkeys` route (same shape as
   `data/libreta/autosave.json`); everything reads through `lbHotkeyMap()`, so
   the swap stays inside that file.
   - **Rebinding Ctrl+/ (toggle comment) still leaves the old chord working**
   (reported 2026-11-13, parked). Most likely because `defaultKeymap` also binds
   `Mod-/` to `toggleComment`, and `lbFilterKeymap()` only filters
   `searchKeymap` and `foldKeymap`. Fix shape: include a `comment` rule for
   `defaultKeymap` (pattern `/^(Mod|Ctrl)-\/$/` and the `Shift-` variant), then
   re-check every action the same way — the general rule is "whatever CM binds
   for an action we expose must be filtered, not just the two maps we knew
   about". `Alt-Alt`/`Shift-` variants matter: filter on the key pattern, not on
   an exact string.

9. **The dirty flag never clears on undo** (reported 2026-11-13): any doc change
   sets `tab.dirty = true` and nothing re-evaluates it, so undoing back to the
   text that is on disk leaves the Save button lit. Fix shape: keep the saved
   `Text` per tab (module-level Map keyed by tab, NOT in the Alpine store — a
   CM `Text` inside the proxy is asking for trouble) and recompute the flag only
   on history transactions (`tr.isUserEvent("undo")` / `"redo"`), comparing
   `state.doc.eq(savedText)`. Doing it on every keystroke would be an O(n)
   compare per key; on undo/redo alone it is rare and cheap.

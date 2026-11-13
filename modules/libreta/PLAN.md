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
- **P2 — done (Round 1 parity with coder/codeeditor).** Quick-open Ctrl/⌘+P
  (route + palette); codeeditor disabled. **Autosave** (`autosave_interval`,
  seconds, 0 = off; one timer per open document, same mtime guard as Ctrl+S,
  banner per document) with the per-file switch. **CM6 extras**: fold gutter +
  keys, bracket matching, auto-closing brackets, selection matches, comment
  toggle, smart Enter (markdownKeymap). **Go to line** (Ctrl+Alt+G) and the
  `line:col` caret readout. Round 2 (optional): tree-sitter lint, vim. Inventory
  and skip list live in `Notes/libreta/libreta-coder-parity.md`.
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
tests/                           56 unit tests (throwaway vaults in /tmp)
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

**Done in the 2026-11-13 backend batch (routes + UI wiring, 88 tests):** folder
rename/delete (directories allowed, folder-into-itself refused, subtree moved to
the trash, `state.json`/`autosave.json` keys rebased, and the UI refuses while a
file inside is dirty); trash **restore-all** with reported conflicts; **cross-file
search** (literal only — no regex, per the decision that day) behind a
`Ctrl+Shift+F` palette; **relative note images** through the `raw` route (bytes
travel base64 inside JSON because the extension API has no binary response);
**windowed read** for files over `max_read_bytes` (read-only, banner, save and
autosave refused). Two parked bugs were also closed: the `Ctrl+/` rebind (now
shadowed against `defaultKeymap`) and dirty-on-undo (`LB_SAVED` baseline).

Still open:

1. **Wiki links** are supported by the preview engine but not wired to the vault
   (no backlinks, no vault-wide resolution).
2. **Tags / properties / callouts / in-vault search** — P3, not started.
3. **`raw` first-render 404**: the image widget loads the relative path before the
   MutationObserver swaps in the blob URL. The final state is correct and the one
   console 404 is cosmetic; killing it means rewriting `src` inside the bundle,
   so it rides on a future rebuild.
4. **Keyboard shortcuts live in localStorage** (`libreta.keys`). To make them
   follow the user between browsers, move that object to
   `data/libreta/hotkeys.json` behind a `state/hotkeys` route; `lbHotkeyMap()` is
   the only reader, so the swap stays inside `libreta-hotkeys.js`.
5. **Retirements are done at config level**: core `notes`, `codeeditor` and
   `coder` are disabled; their CM5 vendor trees remain, unused.
6. **Round 2 parity (optional)**: tree-sitter lint (`/lint` + `@codemirror/lint`)
   and vim. Both need a restart; lint also needs a bundle rebuild.
7. **Theme Tier 2**: a JSON theme file compiled into a real `HighlightStyle`
   (~30 token slots). Rides on the same rebuild as item 6.

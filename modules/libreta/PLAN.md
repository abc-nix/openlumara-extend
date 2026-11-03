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
  done (route + palette); codeeditor disabled. Next: autosave interval, then the
  CM6 editor extras (fold, brackets, selection matches, smart Enter, Ctrl+/),
  go-to-line, caret position; optional tree-sitter lint. Inventory and skip list
  live in `Notes/libreta-coder-parity.md`.
- **P2.1 — next: chat views.** full (current) / docked (right panel, chat
  narrows) / floating (centred card, tab dropdown instead of tabs), remembered
  in localStorage, one cycle button in the doc bar. Full design and verification
  list: `Notes/libreta-views-plan.md` (not started).
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
6. No find-in-note panel.
7. core `notes` module still enabled although unused; `codeeditor` still ships
   its own CM5 vendor (retire both once libreta covers them).

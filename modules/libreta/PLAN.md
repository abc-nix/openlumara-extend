# libreta — plan and status

One user-facing text surface for OpenLumara: **Notes** (rendered prose) and
**Code** (source) are two views of the same thing, a text file.

## Decisions (locked)

| # | Decision |
|---|---|
| 1 | **No AI tools in libreta.** The `editor` module stays the AI's file backend. libreta is user-facing only, so notes and code keep exactly one tool vocabulary. |
| 2 | **One data model: real files.** Direct sandboxed file I/O, no `StorageDict` mirror. Notes stay at `data/notes/**/*.md` — no migration. |
| 3 | **Notes is just a workspace.** Single source of truth: `editor.workspaces`. libreta inherits that list, so what the UI can browse == what the AI can read/write. |
| 4 | `editor.enable_webui_extension: false` later, so only libreta's tree exists while editor keeps all its tools. |
| 5 | **codeeditor is retired by libreta** once code mode reaches parity (CodeMirror + keymaps + quick-open). Not yet. |
| 6 | Name: `libreta`; module lives here (`user_modules/libreta` via the extend symlink). |

## Status

- **P0a — done (read-only).** Sidebar tab with **Files / Recent / Pinned /
Options**, workspace tree, full-window reading cover with document tabs,
rendered markdown, pins and recents persisted server-side.
- **P0a.1 — done.** Options tab writes module settings to `config.yml`
  (autowrap, show hidden, inherit folders, size/snippet/limit numbers, default
  tab). `code_autowrap` (default on) wraps long lines in Code view; the
document bar toggles it per file and that override survives a reload.
- **P0b — done.** Code view is CodeMirror 5 (vendored), so line numbers and wrap
stay while editing; per-tab undo docs; `Ctrl/⌘+S`; dirty tabs; close prompt;
**mtime-guarded save** with a conflict banner (Reload from disk / Overwrite);
create / rename / delete ("+" in the Files header, hover actions on file rows).
Notes mode has an optional **split view** (source left, live preview right).

- **P0c — next.** Notes mode without the split, Obsidian-style: one pane. Real
  route is CodeMirror 6 + a markdown live-preview decoration set (syntax markers
  hidden around the caret only). CM5 cannot collapse arbitrary marker ranges
  safely, so this is a dependency swap, not a tweak.
- **P0c / P2 — not started.** Vendored CodeMirror, keymaps, quick-open, then
retire codeeditor.
- **P3 — not started.** Wikilinks + backlinks, tags/frontmatter, callouts,
attachments, in-vault search.

## Enabling it

```yaml
# config.yml
user_modules:
  enabled:
  - libreta            # add

# the notes folder as a workspace (also puts it in the AI's system prompt)
user_modules:
  settings:
    editor:
      workspaces:
      - ... existing entries ...
      - /home/abc/git/openlumara/data/notes   # add
```

Then restart. If the notes folder is not added, libreta still works — it just
shows whatever workspaces the editor module already has.

## Files

```
module.py     settings + /api/ext/libreta routes (no tools, no user-file writes)
files.py      framework-free file layer: containment, listing, reading, snippets
tests/        unit tests for the file layer (throwaway vaults in /tmp)
webui/templates/sidebars/libreta.html (+ .svg)   the tab
webui/templates/chat/index.html                  claims the below_header slot
webui/templates/chat/cover.html                  the reading cover
webui/assets/css/libreta.css                     all styling (theme vars only)
webui/assets/js/libreta.js                       stores + panel component
```

Routes: `workspaces` · `tree` · `read` · `preview` (POST) · `settings` ·
`settings/save` (POST) · `state` · `state/open` (POST) · `state/pin` (POST) ·
`state/forget` (POST).

Settings are written through the `ConfigManager` (not `ModuleConfig.set()`, which
no-ops when a key is missing from `config.yml`) and only a whitelist of keys can
be written: booleans, clamped numbers, and `default_side_tab`.

## What P0a does NOT do

- **Cannot write a user file.** The only disk write is `data/libreta_state.json`
  (recents/pins).
- No editing, no new note, no rename/delete, no CodeMirror, no wikilinks.
- `show_hidden` only affects the tree; dotfiles stay hidden by default.
- Symlinks are refused (both when resolving and when listing) — a deliberate
  safety choice, not a limitation to work around silently.

## Known limitations to revisit

1. Workspaces are named by their **folder basename**; two configured workspaces
   with the same basename are ambiguous (recents/pins store the name).
2. The tree is fetched one level per request; a 100k-file workspace is fine, but
   the *filter* box only matches rows already expanded.
3. `read` accepts up to `max_read_bytes` (1 MB). Larger files are refused rather
   than windowed; windowing belongs to P0b.
4. Escape closes the cover even if a core modal is open on top.

## Next (P0b) design notes

- `save` route: POST `{workspace, path, content, mtime}` → 409 on mtime mismatch
  (`{"error": "changed on disk"}`), so the AI's `editor_edit` and the UI can
  share a file without one silently clobbering the other.
- Per-tab dirty state + close prompt; autosave off by default.
- Notes/Code switch also decides the *write* surface (markdown source vs plain source)
  — v1 edits raw text in both modes; live preview editing comes with CodeMirror.

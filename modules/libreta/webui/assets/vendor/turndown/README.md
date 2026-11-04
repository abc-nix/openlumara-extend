# turndown — clipboard HTML → markdown (libreta)

Two browser-ready files, copied verbatim from npm; nothing is installed at
runtime, no bundling step is needed (both are already UMD).

- `turndown.umd.js` — **turndown 7.2.4** (`turndown/lib/turndown.browser.umd.js`),
  MIT. Registers the `window.TurndownService` global.
- `turndown-plugin-gfm.js` — **turndown-plugin-gfm 1.0.2**
  (`turndown-plugin-gfm/dist/turndown-plugin-gfm.js`), MIT. Registers the
  `turndownPluginGfm` global; `gfm` adds tables, strikethrough and task list
  items.
- `LICENSE-turndown`, `LICENSE-turndown-plugin-gfm` — the upstream MIT texts.

## How they were obtained

```bash
mkdir -p /tmp/tdbuild && cd /tmp/tdbuild
npm init -y && npm i turndown@7 turndown-plugin-gfm@1
cp node_modules/turndown/lib/turndown.browser.umd.js        turndown.umd.js
cp node_modules/turndown-plugin-gfm/dist/turndown-plugin-gfm.js turndown-plugin-gfm.js
```

Size: 27 KB + 4 KB raw.

## How libreta uses them

**Notes mode only.** The CodeMirror `paste` handler reads the `text/html`
flavour of the clipboard and inserts the converted markdown instead of the
flattened text. Untouched: Code mode, clipboards without HTML (terminals, text
editors), and `Ctrl/⌘+Shift+V`, which always pastes raw.

Rules that matter:

- our own copies (HTML carrying `cm-` / `atomic-` classes) fall back to the
  plain-text flavour — round-tripping the editor's own DOM would corrupt it;
- `<img>` pastes as **its `src` path**, never as an embed (attachments are out
  of scope); a `data:` URI is not a path, so only its `alt` survives;
- a file copied from a file manager arrives as `text/uri-list` → its local path
  is pasted as text;
- the converter is lazy-loaded (`lbLoadTurndown`) when the editor is created.

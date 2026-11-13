# CodeMirror 6 bundle (libreta)

`cm6.min.js` is a prebuilt, minified IIFE built **once** with esbuild — this is
not the npm-installed kind of dependency; the module ships the artifact the same
way `webui/assets/vendor/codemirror` shipped CodeMirror 5.

## What is inside

- CodeMirror 6: `@codemirror/state`, `@codemirror/view`, `@codemirror/language`,
  `@codemirror/commands`, `@codemirror/search`, `@codemirror/lang-markdown`
  (search is the find/replace engine: query state, match highlighting, regex
  including multiline patterns; libreta supplies its own panel through
  `search({createPanel})`)
- code languages: `@codemirror/lang-javascript`, `-python`, `-json`, `-yaml`,
  `-css`, `-html`, `-xml`
- `@lezer/markdown` (with the GFM table extension)
- the Obsidian-style live preview engine from `@plannotator/atomic-editor`
  (MIT): `inlinePreview()`, `tables()`, `imageBlocks()`

The bundle exposes `window.LibretaCM`. React is marked external and is **not**
included (the live-preview engine's React wrapper is unused).

Size: 645 277 B raw / 223 280 B gzipped (630 / 218 KB). CodeMirror 5 (the
previous engine, 1.2 MB on disk, 546 KB loaded) is no longer vendored here.

## How it is built

`entry.js` is **committed next to this file** and is the whole build input —
imports, the 36 exports, the highlight style, the extension→language map, the
`codeLanguages` list and the frontmatter `MarkdownConfig`. It was missing until
2026-11-13: the artifact shipped without its entry, so the old recipe could not
reproduce it. It has since been reconstructed from the artifact itself, which
still carried the export table and every piece of custom code.

```bash
mkdir -p /tmp/cm6build && cd /tmp/cm6build
npm i esbuild@0.25 @codemirror/state @codemirror/view @codemirror/language \
  @codemirror/commands @codemirror/search @codemirror/lang-markdown @codemirror/lang-javascript \
  @codemirror/lang-python @codemirror/lang-json @codemirror/lang-yaml \
  @codemirror/lang-css @codemirror/lang-html @codemirror/lang-xml \
  @lezer/common @lezer/highlight @lezer/markdown @plannotator/atomic-editor \
  react react-dom
cp <module>/webui/assets/vendor/cm6/entry.js .        # now in the repo
npx esbuild entry.js --bundle --format=iife --global-name=LibretaCM --minify \
  --external:react --external:react-dom --target=es2020 --outfile=cm6.min.js
cp node_modules/@plannotator/atomic-editor/dist/styles/inline-preview.css atomic.css
```

Versions the 2026-11-13 rebuild resolved (pin these to reproduce the artifact
exactly): `@codemirror/state` 6.7.6, `@codemirror/view` 6.43.15,
`@codemirror/language` 6.13.1, `@codemirror/commands` 6.11.1,
`@codemirror/search` 6.7.2, `@codemirror/lang-markdown` 6.5.2,
`@codemirror/lang-javascript` 6.2.5, `@codemirror/lang-python` 6.2.1,
`@codemirror/lang-json` 6.0.2, `@codemirror/lang-yaml` 6.1.3,
`@codemirror/lang-css` 6.3.1, `@codemirror/lang-html` 6.4.12,
`@codemirror/lang-xml` 6.1.0, `@lezer/common` 1.5.5, `@lezer/highlight` 1.2.5,
`@lezer/markdown` 1.8.1, `@plannotator/atomic-editor` 0.9.0, esbuild 0.25.12.

Two traps worth keeping written down:

- **`tags` come from `@lezer/highlight`**, not `@codemirror/language` — importing
  them from the latter fails the build with *no matching export*.
- **`frontmatter` cannot be imported**: `@plannotator/atomic-editor`'s `exports`
  map only allows `.`, `./code-languages` and `./styles.css`, so
  `dist/frontmatter.js` is unreachable from an external build. The config is
  copied **verbatim** into `entry.js` (keep it byte-identical to
  `node_modules/@plannotator/atomic-editor/dist/frontmatter.js`).

## Verifying a rebuild

Compare against the previous artifact instead of trusting it by eye:

1. **Export surface** — the artifact carries its table as `var Ew={};…(Ew,{...})`;
   the rebuild must expose the same 36 names, in the same order.
2. **Rendering** — CM6 renders only the viewport, so a single-page comparison
   covers ~40 lines and proves almost nothing. Jump across the document and diff
   the normalised `.cm-content` HTML at each stop (normalise `ͼ…` class names:
   they are generated per build). The 2026-11-13 rebuild was checked this way on
   `Notes/tmp/cm6-sample/gfm-spec.md` (the GFM spec source: frontmatter, tables,
   task lists, strikethrough, 1461 fences) and a kitchen-sink document:
   0 mismatches over 39 sampled positions.
3. **Size sanity** — the rebuild landed 4 B off the shipped artifact (minified
   identifier lengths), the expected magnitude for an identical module graph. A
   large delta means the graph changed.

## Licensing

- CodeMirror 6 — MIT (`LICENSE-codemirror`)
- `@plannotator/atomic-editor` live-preview engine — MIT (`LICENSE-atomic-editor`)

## Notes

- `atomic.css` is scoped to `.atomic-cm-editor`; the editor adds that class
  through `EditorView.editorAttributes`. `libreta.css` re-maps the
  `--atomic-editor-*` variables onto OpenLumara's theme variables.
- The package is a *build-time* tool only: nothing here is installed or fetched
  at runtime.

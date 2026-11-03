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

Size: 630 KB raw / 223 KB gzipped. CodeMirror 5 (the previous engine, 1.2 MB on
disk, 546 KB loaded) is no longer vendored here.

## How it was built

```bash
mkdir -p /tmp/cm6build && cd /tmp/cm6build
npm i esbuild@0.25 @codemirror/state @codemirror/view @codemirror/language \
  @codemirror/commands @codemirror/search @codemirror/lang-markdown @codemirror/lang-javascript \
  @codemirror/lang-python @codemirror/lang-json @codemirror/lang-yaml \
  @codemirror/lang-css @codemirror/lang-html @codemirror/lang-xml \
  @lezer/common @lezer/highlight @lezer/markdown @plannotator/atomic-editor \
  react react-dom
# then write entry.js (see the module history) and:
npx esbuild entry.js --bundle --format=iife --global-name=LibretaCM --minify \
  --external:react --external:react-dom --target=es2020 --outfile=cm6.min.js
cp node_modules/@plannotator/atomic-editor/dist/styles/inline-preview.css atomic.css
```

Versions used: `@codemirror/view` 6.43.15, `@codemirror/lang-markdown` 6.5.2,
`@codemirror/search` 6.7.2,
`@plannotator/atomic-editor` 0.9.0.

## Licensing

- CodeMirror 6 — MIT (`LICENSE-codemirror`)
- `@plannotator/atomic-editor` live-preview engine — MIT (`LICENSE-atomic-editor`)

## Notes

- `atomic.css` is scoped to `.atomic-cm-editor`; the editor adds that class
  through `EditorView.editorAttributes`. `libreta.css` re-maps the
  `--atomic-editor-*` variables onto OpenLumara's theme variables.
- The package is a *build-time* tool only: nothing here is installed or fetched
  at runtime.

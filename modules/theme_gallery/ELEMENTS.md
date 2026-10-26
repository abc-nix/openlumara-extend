# theme_gallery — ELEMENTS.md — inventory for the "Elements" category

Purpose: single reference for building the **Elements** category (a static, scrollable
style-guide of every themed OpenLumara UI element, Storybook-style). Multi-session
build — this file survives compression; keep it updated (tick `[x]` as sections land,
append discoveries at the bottom).

Build policy (agreed):
- STATIC sample template (same approach as `tg-sample-chat`): real markup/classes
  copied from the source files listed per item, FAKE content. No clones, no capture,
  no per-user variance, zero risk.
- Every element gets its interesting STATES side by side (hover states via Freeze-hover).
- Sections render as `.tg-el-section` blocks: `<h3>` title + rows of samples + small
  caption with the CSS source file, so missing/undefined vars are instantly visible.
- Cross-refs: BUGS.md = landmines. Sidebar/Settings/Welcome categories already cover
  some elements live (see coverage map) — Elements = isolated STATES gallery, not a
  duplicate of the live surfaces.
- Gallery VIEW OPTIONS (side panel, 2026-10-25): "Highlight" checkbox = outline the
  hovered element in its own pane (supersedes the retired Freeze-hover for state
  eyeballing; works single-view). Sync 🔗 mirrors scroll/hover/click only — mode
  buttons are strictly per-pane (user decision, cascade removed).

SECTION SOURCE (2026-10-25): one file per section lives in
`webui/templates/chat/elements/<name>.html` (included, in order, by
`chat/element_sheet.html`; see BUGS.md #20). This document is the inventory +
QA log, not the markup — edit the per-section file.

Sources scanned (2026-10-25): channels/webui/assets/css/**, templates/chat/** (incl
turns/), templates/modals/**, js/renderers/markdown.js, live DOM class scan.

---

## Section inventory

### E1 Buttons & actions  `[x]`
- generic `button` (base styling) — base/base.css, main.css
- `.footer-btn` (+ `.save`, `.reset`, `:disabled`) — settings modal
- `.close` (modal ×), `.close-prompt .prompt-btn` (+ `.danger`) — modals/*.css
- `.sidebar-btn` (New Chat), `.action-btn` (chat row ⋮, + `.active`), `.tg-*` = ours, skip
- `.menu-btn`, `.gear` — chat header (header.css)
- `.copy-btn` on code blocks — chat/code-copy.css
- `.message-buttons button` (copy/edit/regenerate/delete; `:disabled` during stream;
  copy shows CHECKMARK state) — templates/chat/message_buttons.html, message_buttons.css
- `.rename-action-btn.save/.cancel` — sidebar rename rows
- `.search-toggle` (+ `.active`) — sidebar.css
- variant classes found in modules (`tb-primary/tb-ghost/tb-danger`) — see E18

### E2 Form controls  `[x]` (basics; array/object/api_url rows → E15)
- **UNSAFE TRUTH (user QA round 4, 2026-10-25)**: the unsafe toggles users
  actually see are the modules/channels cards — `.unified-list >
  .unified-card(.unsafe/.disabled) > .info > (span.btn | span.channel-name-btn
  + p.description) + button.toggle(.toggle-on/.toggle-off)` (channels.html /
  modules.html; discovered via the live settings capture, which has 3 real
  unsafe rows, ALL unified-cards — the `.item.unsafe` fields variant renders
  for NO setting in this store). fields.css `.unified-card` nests under root
  `.modal`; modal.css `.settings-modal .unsafe` supplies the dashed-red +
  red-tint highlight, and `.unsafe .toggle.toggle-on` turns the track RED
  (only `.toggle-on` triggers it — `.active` demos could never show this).
  Sheet now demos all three card states + keeps the rare `.item.unsafe`
  variant (fixed: fields.html nests `.item > div > (.toggle-row +
  .description)` — the inner div shields `.toggle-row` from the
  `.unsafe & > *` border/bg strip rule).
- `input[type=text|number|password|search]`, `select`, `textarea` (+ `:disabled`,
  `:focus` rings) — base.css, fields.css, sidebar.css `.search input`
- toggle switches used by settings booleans (fields.css `.setting-item` block)
  — NOTE: booleans are NOT `input[type=checkbox]`; custom switch markup —
  fields.html: `.item > div > .toggle-row > label + button.toggle(.active)`
- `.rename-input` — sidebar
- `.coder-select`, `.docs-select`, `.mem-select`, `.tb-select/.tb-toggle` — module-owned (E18)
- `#context-pill` is a BUTTON (E4, not a form field)

### E3 Badges, chips & tags  `[x]`
- `.tag-chip` (+ `.selected`), `.inline-tag`, `.tag-filter-clear`, `.tag-icon-wrap` — sidebar.css
- `.welcome-model-badge` — chat/window.css (badge under welcome hero)
- `.module-chip` / `.module-action` / `.action-count` / `.tool-repeat` — combined_block
- `.stream-state` (header dot/text), `.tb-dot`, `.tb-run-pill` (testbench header widget)

### E4 Pills (context)  `[x]`
- `#context-pill` full state matrix: normal → `.warning` (≥ threshold) → hero
  `.danger` percent; compaction badge; popup rows (`ctx-bar`, `ctx-legend`,
  popup-rows, threshold zone) — context_pill.css. FAKE replica in Welcome category
  already implements normal state; add warning + danger variants here (drive via
  FAKE_CONTEXT-like props or just extra static variants).

### E5 Messages & turns  `[x]`
- `.message-wrapper`/`.turn`/`.message.user|.assistant` — message_bubble.css
- `.turn-timestamp` (inline clock chip) — message_buttons.html
- `.flex.fullwidth.nogaps` layout helpers used by turns
- user turn with attachments: `.multimodal-content`, `.media-preview`,
  `.text-preview` — upload.css + turns/user.html
- assistant history vs streaming variants — turns/assistant_history|stream.html
- message-buttons row attached to a bubble (E1 tie-in), disabled-during-stream look

### E6 Markdown & code  `[x]`  (expanded 2026-10-25: chat sample now mirrors the renderer byte-for-byte — `.copy-btn` before `<code>`, `hljs language-*` + token spans, `thead/tbody`; E6 adds nested lists/quotes, a second language, an inline image, and gap demos for unstyled `<code>`, `h5/h6` and unconstrained `img`)
- pipeline: marked + DOMPurify + hljs (`pre code.hljs`), injected
  `<button class="copy-btn">Copy</button> before every pre — renderers/markdown.js
- sample row must include: headings, paragraph, `inline code`, fenced code block
  (python), blockquote, links, ul/ol, TASK LIST checkboxes, TABLE, hr, images,
  and RAW-mode (escaped text, no md) — theme code colors from css/code-themes/*
- `.snippet.snippet-3` + `<mark>` highlights (search result styling) — sidebar.css

### E7 Reasoning / thinking  `[x]`
- `.reasoning-wrapper`, `.standalone-chevron` — reasoning.css, templates/chat/reasoning.html
- inside combined block: `.reasoning-row` + `.reasoning-text` (auto-scroll box)

### E8 Tool calls  `[x]` (custom per-tool views → diff DONE 2026-10-25; rest = E18 list)
- **Diff viewer ADDED (audit find)**: `.diff-container >
  .tool-display.diff-unified > .diff-line(.diff-del/.diff-add/.diff-gap) >
  (.diff-gutter + .diff-text)` + `.display-footer` — scoped under
  `.tool-call-card` (custom_tool_views.css). GOTCHA: `.diff-container` has a
  CSS **container query** (≥640px → unified hides, `.diff-side` shows); sheet
  renders only the unified child → CSS caps `.tg-elements .diff-container` to
  560px so unified stays visible (real app picks unified at this width too).
- `.tool-calls-container` (+ `.tool-calls-streaming-container`) — toolcalls.css
  **streaming variant ADDED 2026-10-25 (gap-audit follow-up)**: same cards,
  gap 8px vs 14px — sheet demos both side by side (verified computed gaps).
- `.tool-call-card` states: default, `.tool-pending`, `.tool-error`, collapsed vs
  expanded (`.card-chevron.expanded`), `.node-dot.expanded`
- header parts: `.module` prefix, `.name`, `.call-args`, `.call-result`
- body: `.default-display`, `.arg-row` (`.arg-name`/`.arg-value` in x-box),
  `.response-row`, `.raw-response`, `.display-error`
- processing overlay: `.tool-processing-indicator/-spinner/-text`,
  `.tool-progress-bar(-fill)`, `.tool-processing-percent`
- `.diff-container` (git diff view in tool cards) — custom_tool_views.css
- custom tool views: see E18 note (module-owned internals)

### E9 Agent chain & combined block  `[x]` (+ sbox variants 2026-10-25)
- **Thought sbox pair (user QA round 4)**: `.combined-block .reasoning-text`
  clamps at `max-height:120px; overflow-y:auto` → step 2 shows SHORT text
  (no scrollbar), step 4 LONG text (scrolls; verified clientH 120 vs
  scrollH 200 vs short 20/20). white-space:pre-wrap → literal newlines work.
- `.agent-chain` (+ `.is-streaming`), `.segment`, `.activity`, `.label`, `.icon`, `.node-dot`
- `.combined-block` states: `.collapsed`/`.expanded`; `.dot-done`/`.dot-failed`/`.dot-live`
- header pieces: `.thought-header` + `.step-thought` snippet, `.tool-names`,
  `.module-group`/`.tool-module`/`.tool-action`/`.tool-repeat`, `.step-args`,
  `.step-summary`, `.dots` (animated ellipsis), `.block`, `.step-content`

### E10 Prompt processing & streaming states  `[x]`
- `.prompt-processing-indicator-wrapper/-indicator`, `.progress-header`,
  `.prompt-processing-percent`, `.prompt-processing-eta`, `.prompt-progress-bar(-fill)`,
  `.pp-tool`, `.pp-prompt` — prompt_processing.css
- `.streaming-indicator` + `.indicator-dot` (input bar), `.stop-text` — input.css
- `.typing-indicator` (3 dots) — typing_indicator.css

### E11 Uploads & attachments  `[x]`
- `.upload-queue` chips (with remove ✕ + progress), templates/chat/upload.html
- `.image-modal` (lightbox shell) **FRAGMENT ACTUALLY ADDED 2026-10-25**
  (earlier claim was inaccurate — audit proved it never landed; and grep
  proved NO template/JS instantiates the class: dead CSS). Static fragment:
  real flex-center + img rules (max 90vw/90vh, --radius-md) kept; the
  rgba(0,0,0,.9) overlay bg + fixed positioning neutralized (would blacken
  the pane), captioned honestly.
- user-message attachment previews → E5 tie-in
- **E5 GAP PATCH (2026-10-25 coverage audit)**: `.media-preview` was CLAIMED
  here but the sheet copy lacked the class — fixed: user turn now has a real
  `.media-preview.image-preview > img` (fake SVG) beside the text-preview.
- **E8 GAP PATCH (same audit)**: `.tool-result-image` (served-build rule:
  margin 4/8/8/24, max-width 220px — NOT in the git workspace copy, see
  BUGS.md #16 DRIFT) added inside the expanded read_file card's body.

### E12 Notices, empties, spinners  `[x]`
- `.notice` (chat window banners: info/warn/danger variants where defined) — window.css
- spinner instances: modal `.spinner`, `.search-spinner` (in-field),
  `.search-spinner-inline` (list row), `.tool-processing-spinner`,
  `.tool-progress-bar` (bar), settings loading
- empty states: `.tag-filter-empty`, `.docs-empty`, `.mem-empty`, toolcalls `.empty`,
  fields `.empty-hint`, "no chats found" `.item`, search "searching.." row
- `.coder-readonly-badge`, `.ed-ro-badge` (module — E18)

### E13 Popups & menus (static fragments)  `[x]` (fused-fix 2026-10-25, see RESOLVED note below)
- chat-action-menu / category-menu / tag-popup: position:fixed overlays →
  neutralized to static blocks (menus) — verified visible inline.
- `.chat-action-menu` (Rename/Move/Delete `.danger`) — sidebar.css
- `.category-select` FUSED demo: `.category-trigger.open` + `.category-menu`
  (+ `.category-option`, `.manage`, divider) rendered as ONE box (open state)
- `.tag-popup`
- context popup = E4
- model dropdown — see Welcome category (FAKE select built there)

### E14 Modal shells (fragments, not live modals)  `[x]` (+ search states 2026-10-25)
- **Global-search side states ADDED (audit find)**: `.global-search-loading`
  (.spinner + .loading-text) · `.global-search-empty` (svg + p) ·
  `.global-search-no-results` (svg + p) — all inside `.global-search-results`
  above the result rows (they're mutually exclusive in the real modal; sheet
  shows all three, captioned).
- **RETRY TRUTH**: `.settings-error` (modals/settings/index.html) has NO CSS
  in ANY stylesheet incl. themes — modal.css styles `.error` but the template
  never uses it → the REAL Retry button is a plain browser-default button.
  Sheet copy is faithful to that app gap (caption fixed; logged BUGS #13).
- **GAP-AUDIT ROUND 2 (2026-10-25)**: fresh CSSOM scan + stylesheet attribution.
  Added: `.image-modal` fragment (E11) + `.tool-calls-streaming-container`
  variant (E8) → both verified (15 sections, computed gaps 14/8px). Remaining
  un-demoed core classes judged acceptable: `.restart-modal` + `.move-chat-modal`
  exist LIVE in the Settings/Sidebar categories; `.login-wrapper` = login page
  (login page — statically added later as E35, kept by user decision 2026-10-25:
  likely a remnant of an older version, but kept for completeness); rest = module CSS false-positives
  (git.css/codeeditor.css generic words) or live-covered appearance.css.
  App-side typo class `fage` observed in their CSS (theirs, ignore).
- **MISSING-ELEMENT AUDIT (2026-10-25, CSSOM scan)**: every class styled by the
  app CSS vs DOM. Outcome: additions above were the only real core gaps.
  Everything else is either (a) live-covered by Settings/Sidebar categories
  (appearance.css theme controls etc.), (b) module-owned E18 families
  (tb-*/ed-*/coder-*/git-*/mem-*/docs-*/browser-ext-*/dp-vlm-*/CodeMirror),
  (c) mobile-only responsive.css, (d) DEAD selectors never used by templates
  (base.css category-item/chat-item/tool-call-header press-scale, modals.css
  confirm-modal/confirm-overlay/modal-overlay), or (e) hljs token palette
  (already demoed via sample code block).
- global-search fragment (.global-search-modal + content + result rows w/
  mark snippets + tags) — GOTCHA: its CONTENT div is the fixed element
  (`.global-search-modal-content`), neutralize that too.
- categories fragment (.category-rows/.confirm-inline delete-confirm state/
  .add-btn/.category-error). restart overlay + move_chat skipped (sidebar/
  settings live categories cover the flow; restart is trivial).
- `.modal` shell anatomy: `.header h2#title` + `.close`, body, footer — modals.css
- fragments worth showing: move_chat list rows, categories rows, global-search result
  row + `.global-search-loading`, close-prompt box (`.prompt-box`), restart overlay
- login page card — login.css (maybe out of scope; mark optional)

### E15 Settings field widgets in isolation  `[x]`  (DONE 2026-10-25 — see E23 in the completeness pass below; previously deferred because the Settings category showed them live, now also statically isolated)
- `.setting-item` (parent/child indent), `.unsafe` highlight, label + description
  two-column, toggle row, select row, array/object editor rows, api_url row + error —
  modals/fields.css

### E18 Module surfaces appendix (informational, NOT first-pass scope)  `[ ]`
- Live-scan found big module class families that consume theme vars:
  `tb-*` (testbench: panels, cards, tabs, chips, bars, its own `.tb-modal` suite),
  `ed-*`/`coder-*` (editor), `docs-*`, `mem-*`, `git-*`, header `tb-run-pill`.
- Themes generally style these via shared vars; an Elements sheet need not clone
  them — Sidebar category shows them live. Only add rows here if a theme bug appears.
- **Get Modules (module_library) ANSWER (user Q 2026-10-25)**: fully covered
  LIVE by the Settings category — its tab was captured (library cards w/
  status chips, repo rows, install/update/uninstall buttons, audit overlay
  shell; captures its own GET /api/libraries/status on mount). Module-owned
  CSS (modals/settings/module_library.css) = not restated as static sheet
  rows; audit-overlay static fragment could join E14 if wanted. Known bug:
  audit overlay throws `audit is not defined` (x-teleport scope loss) —
  pre-existing, logged in BUGS.md #10.

### E19 Variable swatches (raw-var idea, parked → belongs HERE)  `[x]` (~70 vars/pane: 66 theme + 4 runtime from theming.js, computed values — see BUGS.md #18)
- grid: one swatch per theme var (bg/text/accent/border families), label = var name,
  resolved value + "inherited from base?" flag. Pairs with diff drawer missing rows.

### E21 Composer & header controls  `[x]`  (added 2026-10-25, user request)
- header.html chain: `<header> > .left(.menu-btn + h1) / .right(.stream-state,
  .model-switcher > .model-select FAKE names, .btn #settings-btn/export/clear)`
  + input.html chain: `#input > #upload + textarea#user-input + #send` and the
  streaming variant `#stop(.stop-text)` — pane lives inside real #chat-window so
  input.css/header.css scoping applies. Composer IS typable (local), Send/Stop/
  gear clicks blocked. Verified heights 46px/90px + signatures.
- Editing-message box must live INSIDE `.message.user` (message_bubble.css
  `.editing` is nested under `&.user,&.assistant`); rename row needs
  `#chat-list > .chats > .day-chats > .item.renaming > .title-row`
  (rename-input: transparent bg + 1px accent border — verified).

### E20 Global bits  `[x]` passive — scrollbars (::webkit-scrollbar +
    scrollbar-color), ::selection, :focus-visible rings, `*:disabled`
    opacity .5 all live on :root/global selectors → THEY ALREADY apply
    everywhere inside the sheet automatically (no section needed; just
    demoed via Tab-focus + text selection + disabled samples).

## COMPLETENESS PASS 2026-10-25 (E22–E36) — sheet now covers every channel+core element
Driven by a class-level audit: recursive CSS-selector walk over the 33 core
stylesheets (502 classes — the old coverage walked only top-level rules and
saw 110) × the app markup harvested from the page DOM **and every
`<template>`'s `.content`** (querySelectorAll does not descend into templates)
× the sheet. Before: **180** app-rendered core-styled classes were missing
from the sheet. After: **0**. New sections, one file each in
`webui/templates/chat/elements/`:

- **E22 settings shell, nav & footer** `[x]` — `settings_shell.html`:
  `.settings-modal.modal` frame (header/close), `aside.sidebar > nav.nav` with
  `.item(.active)`, `.item-icon`, `.nav-divider`, `.sub-list/.sub-group/
  .sub-group-label/.sub-group-items/.chevron`, `.settings-footer > .unsaved +
  .footer-buttons`, `.loading > .spinner`, `.mobile-category-list`.
- **E23 settings field widgets** `[x]` (this is E15, now built) — all
  `fields.html` row types: boolean `.toggle-row`, number, text/url,
  textarea, select, `.array-field > .array-header + .items > .item +
  .remove + .add-row > .add`, `.empty-hint`, `.api-status > .status-dot
  (.connected/.disconnected) + p.error`, `.model-setting-row >
  .model-input-container > select/input + .model-refresh-btn +
  .model-empty-note`, and `.setting-item.setting-parent/.setting-child`.
- **E24 appearance** `[x]` — `.settings-section`, `.section-title`,
  `.section-desc`, `.theme-controls-row > .theme-dropdown-wrapper >
  select.theme-select` (grouped optgroups), `.theme-mode-toggle >
  .mode-button(.active/:disabled)`, `.typography-section > .setting-item >
  .slider-header > .slider-label/.slider-value`, range inputs, preference
  toggles.
- **E25 modules & channels** `[x]` — `.unified-list > h3 + .section-desc +
  .module-group > h4.module-group-label + .unified-card(.disabled/.unsafe) >
  .info > span.btn | span.channel-name-btn + p.description + button.toggle
  (.toggle-on/.toggle-off)`; `.module-drilldown > .back-btn + .section-title +
  .section-desc + .setting-item` and the `.no-settings-msg` variant.
- **E26 module library** `[x]` — `.module-library > .library-message +
  .library-restart > .library-btn.primary + .library-type-tabs >
  .library-type-tab(.active) > .tab-count + .library-sync-btn >
  .library-sync-icon`; `.library-loading`, `.library-list > .library-empty`,
  `.library-group > .library-group-header > a`, `.library-item(.installed) >
  .library-item-main > .library-item-header > .library-item-name +
  .library-item-doc + .library-installed-indicator +
  .library-item-actions > .library-btn(.danger)`.
- **E27 library code-review overlay** `[x]` — `.library-audit-overlay` >
  `.library-audit-modal` (header/repo/lead/flags/warnings/filebar/loading/
  error/`code > .audit-gutter > .audit-line(.focus) + pre.audit-code.hljs`/
  footer+actions), `.audit-flag(.focused|.danger|.warn) >
  .audit-flag-name/-line/-desc`, `.audit-truncated`, `.audit-warning`.
- **E28 system tabs** `[x]` — `.system-logs > .entry > .category + .message`
  (INFO/WARN/ERROR), and `.fullwidth > .system-prompt` + `.section-title/
  .section-desc`.
- **E29 system overlays** `[x]` — `.modal-overlay` (shown as a swatch: real
  element is a full-screen blurred veil), `.modal.move-chat-modal >
  .header(h2 + .close) + .move-chat-content > .move-chat-title +
  .move-chat-row > select + .move-chat-accept(:disabled)`,
  `.restart-modal > .spinner + p`.
- **E30 sidebar shell** `[x]` — `.sidebar-shell > nav.sidebar-tabbar >
  .sidebar-tab(.active) / .sidebar-tab.ext`; `#chat-list > .search
  (.search-icon/.search-spinner/input/.search-toggle)`; `.chats >
  .day-group > button.day-header(.expanded)[aria-expanded] > svg.day-chevron +
  .day-label + .day-count > .day-chats > .item > .title-row > span.title +
  .tag-icon-wrap`; `.snippet.snippet-3`; `.sidebar-panel`.
- **E31 welcome panel** `[x]` — REAL, unneutralized: `.welcome-panel >
  svg.welcome-icon + h2.welcome-title + p.welcome-hint +
  .welcome-model-badge`. (E3's badge wrapper became `.welcome-panel
  .tg-el-scope`, so the neutralizer no longer steals the panel's own look.)
- **E32 tool view: web search** `[x]` — `.search-results > .search-card >
  .search-title-row > a.search-title | span.search-title.plain +
  img.search-thumb`, `.search-url`, `.search-snippet`,
  `.grep-note` (policy-rejected result).
- **E33 tool views: file read / grep / glob / tools-load** `[x]` —
  `.file-read-box.hljs` + `.display-footer` + truncation note;
  `.grep-running > .grep-running-label/-pattern`; `.grep-results >
  .grep-file > .grep-file-name + .grep-box > .grep-line(.grep-add/.grep-del)
  > .grep-num + .grep-text`; `.glob-files > .glob-file`; `.module-chips >
  .module-chip`; `.tool-load-group > .tool-load-module >
  .tool-load-count + .tool-load-rows > .tool-load-row > .tool-load-name +
  .tool-load-desc`.
- **E34 tool views: shell & jobs** `[x]` — `.shell-box > .shell-prompt >
  .shell-prompt-symbol + .shell-command.hljs + .shell-cursor`,
  `.shell-divider`, `.shell-output`; `.job-card > .job-when (svg + text +
  .job-tag) + .job-action`.
- **E36 audio tab** `[x]` — `.item > .row > label.label + input[type=range] +
  span.label.right`; sound rows (`.toggle-row` toggle-on/off, `.description`,
  `input[type=file]`, `.actions > button.btn`).
- **E37 tool results value renderer** `[x]` — the JSON grid every tool
  response passes through (`renderers/tool_results.js`): `.kv-rows` /
  `.array-rows` (+`.nested`) > `.kv-row` > `.key`(+.index) + `.value` >
  `.string` | `.scalar.number` | `.scalar.boolean` | `.null` | `.empty` |
  nested rows, plus `.truncated` ("+ N more"), and `.content-row > .value` /
  `.response-rest` wrapping from `toolResponseRenderer`.
- **E35 login page** `[x]` — `.login-wrapper > .login-card > h1 + form >
  .error-msg? + .form-group (label + input) + button`. Rendered with the
  theme vars RESET to login.css's own fallbacks, because that is what the
  real page does (see the core defect below).

### Fidelity fixes made in existing sections
- **E4 was wrong, not just thin**: the popup used invented `.row-label` /
  `.row-tokens`; `chat/context_pill.html` uses `.popup-key`/`.popup-value`.
  E4 now carries the full real chain: `.ctx-compaction > .compaction-label`,
  `.ctx-bar > .bar-seg(.seg-sys/.seg-tools/.seg-history/.seg-end/.seg-free) +
  .bar-threshold-zone + .bar-threshold`, `.ctx-legend > .legend-item >
  .legend-dot + .legend-label + .legend-pct`, `.popup-rows`, plus the
  `.popup-loading` state.
- **E8**: added the side-by-side half of the diff view
  (`.diff-side > .diff2-row > .diff2-cell(.diff2-del/.diff2-add/
  .diff2-empty) > .diff2-gutter + .diff2-text`). `.diff-container` is a size
  container; the sheet's 560 px cap kept unified visible, so `.tg-el-diffwide`
  lifts the cap for this one sample (still falls back to unified in a narrow
  split pane, exactly like the app).
- **E9**: added the no-reasoning step variant `.bare-header > .tool-names +
  .step-args + .step-summary` and `.block > .flex.fullwidth.nogaps >
  .step-content`.
- **E1**: the unsaved-changes prompt now includes the real `.prompt-text`
  paragraph (was missing).

### Recorded duplicates (2026-10-25 — KEPT, not removed, per user decision)
The completeness pass re-demos some elements that already had a home. They
stay for now, so the sheet can be trimmed deliberately later instead of
guessed at. Owner = the section that should keep the state matrix.

| element | owner | duplicated in | what is genuinely new in the duplicate |
|---|---|---|---|
| chat row `.item > .title-row > .item-actions > .action-btn(.active)` | E1 | E30 | `span.title`, `.tag-icon-wrap`, `.snippet`, `.menu-open` |
| search field (`#chat-list > .search`, `.search-icon`, `.search-toggle`) | E1 | E30 | `.search-spinner` |
| `.unified-list` / `.unified-card(.disabled/.unsafe/.toggle-on/.toggle-off)` | E2 | E25 | `.module-group`, `.module-group-label`, `.module-drilldown` |
| basic field rows (`.item > .row`: text/number/select/textarea/disabled) | E2 | E23 | only the new types (array/object/api_url/model_select, `.setting-item` hierarchy) |
| modal shell (`.modal > .header h2 + .close`) | E14 | E29 | `.move-chat-content/-title/-row/-accept` |
| `.settings-footer > .footer-buttons > .footer-btn` | E1 | E22 | footer in place inside the shell frame |
| `.module-chip` | E3 | E9, E33 | `.module-chips` wrapper (tools-load streaming state) |
| `.welcome-model-badge` | E3 | E31 | the real `.welcome-panel` around it |
| `.library-btn(.primary/.danger)` | E26 | E27 | audit footer layout |
| `.section-title` / `.section-desc` | E24 (appearance) | E22, E25, E26, E27, E28 | different section content |
| `.display-footer` / `.grep-note` | E8 | E32, E33 | per-view context |
| `.tool-call-card > .header` skeleton | E8 | E32, E33, E34, E37 | each view's body |
| `.context-pill` | E4 | E21 | composer context (pre-existing overlap) |
| `.message-buttons` row | E1 | E5 | per-turn placement (pre-existing overlap) |

**Kept on purpose, not duplicates to trim:** E8's unified + side-by-side pair
(comparing the container-query layouts), and `.item > .row` in E23 vs E24
(same classes, different ancestor semantics: hierarchy vs typography).

### Renderer coverage after the pass
Static-union measure over the 33 core sheets (recursive selectors, same
classifiers as the Coverage category): **465 / 496 classes = 94 %**. The 26
that remain are NOT rendered anywhere in the running app — they fall in two
buckets, both verified:
- **runtime-only** (added while streaming/animating, so no static markup can
  hold them): `.token-fade` (fade_tail.js wraps streamed characters),
  `.token-bar-hidden` (body state), `.streaming` / `.animate-in` (transient
  animation/state hooks).
- **dead selectors** (styled but referenced by no template, no JS, no
  directive): `.color-theme-section`, `.theme-section`, `.theme-buttons`,
  `.theme-label` (the old appearance layout, replaced by
  `.theme-controls-row`), `.model-row`, `.group-content`, `.library-item-lib`,
  `.popup-refresh` (the context-pill refresh button was removed),
  `.user_command`, `.command_response` (superseded by `.command`),
  `.welcome-subtitle` (superseded by `.welcome-hint`), `.file-size`,
  `.file-download`, `.preview-bracket`, `.category-hint`,
  `.option-description`, `.name-btn`, `.reasoning`, `.group`, `.drilldown`,
  `.confirm`, `.connecting`.

### Deliberately NOT in the sheet (evidence, not laziness)
- **Disabled core modules** (calendar, characters, scheduler, shell, tutorial,
  coder, git): the app does not serve their stylesheets at all while they are
  disabled — verified by HTTP 404 on `/ext-assets/<mod>/css/<sheet>.css` for
  exactly those modules. Markup without its stylesheet is not a preview, and
  loading a disabled module's CSS globally would change the app's own page.
  Their surfaces stay covered LIVE: the Sidebar category clones
  `.sidebar-panel` for every enabled module, and module modals mount for real
  when opened. `docs`/`memory` are enabled here (their CSS is served, 200),
  and their panels are shown live the same way.
- **Module-owned sheets in general** (E18) remain out of the static sheet for
  the same reason: they are not part of `channels/webui`.
- **Mobile/responsive layout changes** — `responsive.css` is media-query only;
  a pane cannot change the viewport. The rules that ARE class-based
  (`.mobile-category-list`, `.mobile-category-btn`) are shown.

## ⚠ OPEN ISSUE — Popups & menus → **RESOLVED 2026-10-25 (option a + screenshot round 2, pending final user eyes)**
- ROUND 1 diagnosis (computed styles): `.category-menu` by design is a
  TRIGGER-FUSED dropdown: radius `0 0 6px 6px`, NO border-top, NO shadow — it
  visually continues the `.category-trigger` above it (real JS overlaps by 1px,
  CSS adds margin-top:-4px; trigger.open hides its bottom border/corners).
  Rendered ALONE in the sheet it looked broken (open box, cut-off top).
- ROUND 1 fix (option a — most faithful): sheet renders the OPEN state as
  ONE fused unit: `.category-trigger.open` (home icon + "general" + chevron,
  rotated 180° via .open) + `.category-menu` (work/research rows, divider,
  manage). Scoped like the app: `#chat-list.tg-cat-fused-scope >
  .category-select` (sidebar.css nests the trigger under #chat-list — scope
  provider already neutralized; seam margin kept; wrapper width 240px needs
  id+class+!important to out-specify `.tg-elements #chat-list{width:auto
  !important}`).
  Verified by signatures vs REAL DOM trigger: borders byte-identical
  rgb(224,224,234), height 36px, font/padding identical; ONLY the intentional
  open deltas differ (transparent bottom border, bottom radius 0). Menu vs the
  app's own hidden teleported menu: identical. Seam -3.99px; widths equal.
- ROUND 2 (SCREENSHOT — finally reliable, user foregrounded the window; see
  BUGS.md #12): section still looked broken. THREE culprits, all
  TELEPORT-ANCHOR RESIDUE surviving the position:static neutralization:
  1. `.global-search-modal-content` keeps `transform: translate(-50%,-50%)`
     (the app centers it that way) → sheet copy shoved up-LEFT over the
     previous section; only right-aligned dates stayed inside the pane clip
     → giant "empty" panel. FIX: `transform:none !important` in its rule.
  2. `.chat-action-menu` keeps `margin-top:-24px` (anchor offset) → climbed
     onto the section h3. `.tag-popup` keeps `translateY(-100%)`. FIX:
     transform none + margin 0 for both (`.category-menu`'s -4px kept —
     intentional fused seam).
  3. Self-inflicted: the pass-3 neutralizer's `display:block !important`
     destroyed the app's own `display:flex` → tag chips merged into
     "galleryqa", menu column layouts flattened. FIX: dropped the display
     override entirely (position:static alone suffices).
  + fused wrapper given `background: var(--bg-primary)` (trigger is
    transparent; the sheet section's grey ground was bleeding through the
    box's top half — real app sits on bg-primary).
- VERIFIED after round 2: gs content transform none + static, cam margin 0,
  tag-popup display flex gap 4/8 with TWO chips (33px + 12px), fused seam
  -4px, wrapper/menu/trigger bg rgb(250,250,254), clicks blocked, console
  clean, 15 sections. Screenshot confirms visual sanity end-to-end.
- LESSON: neutralizing position:fixed must ALSO kill transform + anchor
  margins, and NEVER blanket-force display — the app's flex/gap IS the look.

## Composer structural truth (final pass, 2026-10-25)
- `#input > .input-wrapper` (flex, align-items:STRETCH, bordered rounded
  container) wraps #upload + (hidden file input) + #user-input + #context-pill
  + #send|#stop → children stretch to bar height (48px). A FLAT #input (my
  first attempt) makes buttons collapse to text height (the user spotted it
  instantly: "something is wrong with send/stop"). Sheet composer now mirrors
  the real child list incl. inline pill replica.
- HARNESS LESSON (again): never read element sizes around a tgState mutation
  that REBUILDS panes (toggleSplit/setSlotMode) — measure AFTER, re-query
  first; detached nodes return offsetHeight 0 / empty computed styles.
- E15 (settings widgets in isolation) → DEFERRED by decision: Settings
  category already shows every widget live in context; revisit only if user
  asks. Custom tool views (E18) = open-ended module territory, parked.
- scrollbar styling, `::selection`, focus-visible rings, `.hljs` token palette,
  base.css typography scale, `.spinner` keyframes — base/*.css

---

## Coverage map (already shown live elsewhere — don't duplicate as live clones)
> UPDATE 2026-10-25: the **Coverage category is BUILT** (BUGS.md #16) — live
> per-stylesheet class audit (static-source union, currently 76%), replacing
> the old parked "coverage report" idea. This table stays as the manual map.
| Element family | Shown live by | Elements-sheet job |
|---|---|---|
| header + input bar + welcome | Welcome category | state variants only |
| settings fields | Settings category (all 12 tabs) | isolated widget rows (E15, optional) |
| sidebar rows/search/chips/panels | Sidebar category | compact state matrix (active/renaming/tagged) |
| context pill normal state | Welcome (fake pill) | + warning/danger variants (E4) |

## Suggested build order
1st pass (core look of themes): E1, E2, E3, E4, E5, E6, E12, E19 → **BUILT 2026-10-25, user QA pending**
2nd pass (chat machinery): E7, E8, E9, E10, E11 → BUILT + QA fixes applied
3rd pass (chrome): E13, E14, E20 → BUILT 2026-10-25 (E15 deferred by decision;
E18 module surfaces parked by design). ALL PLANNED SECTIONS DONE — sheet =
15 sections; remaining work is user-QA-driven fidelity polish only.

## Pass-2 + fidelity notes (2026-10-25, second batch)
- USER QA (pass 1): "buttons don't have the same shape or color" → ROOT CAUSE:
  base.css has NO global button styles (only font-family + press-scale). Every
  real button is styled strictly by its COMPONENT SCOPE.
- RULE going forward: reproduce the EXACT DOM ANCESTOR CHAIN from the real
  templates (e.g. `#chat-list > .chats > .item > .title-row > .item-actions >
  .action-btn`, `.settings-modal > .settings-footer > .footer-buttons >
  .footer-btn`, `.close-prompt > .prompt-box > .prompt-buttons`). Guessed
  subsets leave browser-default buttons in the sheet.
- FIDELITY MEASUREMENT: compare getComputedStyle signatures
  {borderRadius, padding, backgroundColor, borderTopWidth+Style} between the
  REAL element in the live DOM and the sheet copy. sidebar-btn / search-toggle /
  action-btn verified BYTE-IDENTICAL after the chain fix (action-btn needed
  the .chats wrapper). footer/prompt/message buttons measured styled.
- Pass 2 (E7-E11) built same batch: reasoning expanded+thinking states,
  tool-call cards (pending/expanded-args/error), agent chain with
  dot-done/live/failed, pp-prompt & pp-tool bars, streaming dots + stop-text,
  upload queue rows. All in #chat-window scope already (pane inherits).
- Overlay reopen without touching localStorage: `document.body.toggleAttribute
  ('data-tg', true)` is exactly what the tab x-effect does. (Never click the
  real .sidebar-tab from automation — setTab() writes localStorage!)

## Pass-2 QA fixes (2026-10-25, user round 2)
- TEXT BOXES: fields.css nests under **`.modal`** — the real dialog root is
  `class="settings-modal modal"` (BOTH classes). Sheet wrappers now use
  `settings-modal modal` → inputs/select/textarea/range render themed
  (verified 6px radius + bg + border). Range slider row added (styling exists!).
  Toggle truth: `.toggle.active` uses background-**gradient** → check
  backgroundImage when measuring, backgroundColor reads transparent (false alarm).
- PILL TRUTH: state classes live on `.pill-button` (NOT #context-pill):
  `'active': pct>=70` (accent), `'warning': pct>=75` (stroke+pct → var(--error));
  hero pct `.danger` at >=75. Sheet: 35% plain / 72% active / 93% active+warning
  (verified rgb(220,38,38) fill + hero).
- AGENT CHAIN: dots are absolute left:-1em (built for #messages 20px gutter) →
  `.tg-elements .agent-chain{padding-left:1.8em}`; dots now inset, blocks shift
  right into the box (user QA).
- REMAINING KNOWN GAPS (fine for later): custom per-tool views (E18), settings
  widgets in isolation (E15), E13/E14 chrome = pass 3.

## First-pass build notes (2026-10-25)
- Template `#tg-sample-elements` in module chat/index.html; category 'elements';
  root is `div#messages.tg-elements` (panes live inside the REAL #chat-window,
  so window.css/message scoping applies automatically).
- SCOPE-PROVIDER WRAPPER TRICK: samples that need nested-CSS ancestors are wrapped
  in the app's own container classes (`.settings-modal`, `#chat-list`, `.search`,
  `.combined-block`, `.welcome-panel`) and theme_gallery.css NEUTRALIZES those
  wrappers' own box styling (`.tg-elements .settings-modal{position:static…}`).
  Keep this pattern for pass 2/3 (e.g. E13 menus: wrap in a neutralized ancestor
  matching sidebar.css scoping).
- Swatches (E19): JS fills `.tg-el-swatches` AFTER the pane's theme is written
  (_renderInner hook). Keys parsed from `pane.style.cssText` — Object.keys on a
  CSSStyleDeclaration does NOT list custom properties! Values via
  getComputedStyle(pane) so var()/color-mix chains resolve like the app renders.
  66 vars per pane; per-island resolution verified (light #7c3aed vs dark #a78bfa).
- Toggle truth: boolean settings = `button.toggle` (+.active) inside
  `.item > .toggle-row` — NOT an input. .unsafe row variant included.
- Verified: 7 sections render; hljs spans + copy-btn; task lists; table; toggle
  states; 3 pill replicas (35/78/93 + open popup w/ hero .danger); notice; empty
  rows; spinners (search-inline, tool-processing w/ progress bar); typing allowed
  in sample fields; link/copy/toggle/New-Chat clicks blocked; settings store
  untouched; Esc ok; console clean.
- REMAINING un-ticked from first pass: none (E1/E2/E3/E4/E5/E6/E12/E19 all in;
  some E2 rows minimal — array/object editor + api_url rows belong to E15).

## TODO / not yet researched (next sessions)
- tool_displays.html + custom_tool_views.js: enumerate built-in custom views
  (what renders per tool: web search cards? file reads? diffs only?) — 31KB file
- debug.html (debug turn view) — include or skip?
- exact boolean-toggle markup (fields.html boolean template body)
- base.css / main.css / animations.css selector list (E20)
- global_search.html + categories.html + move_chat.html class details (E14)
- login.html markup (E14 optional)
- responsive.css: which elements change layout at mobile widths (informational)
- diff drawer (ours) could gain a "N/M vars defined" badge — parked idea
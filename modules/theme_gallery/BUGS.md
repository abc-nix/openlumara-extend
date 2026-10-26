# theme_gallery — bug log & landmines (survives session compress)

Found during development of the v2 Split + Welcome-screen categories (2026-10-25).
Each entry: symptom → root cause → fix → status. Newest/most critical first.

---

## 1. CRITICAL — "inert clones" are NOT inert (session-breaking store leak)
- **Symptom**: Typing in the Welcome pane's preview textarea typed into the USER'S REAL input box; `$store.chat.user_input` mutated.
- **Root cause**: Alpine v3 processes cloned subtrees *once they are inserted into the live DOM*. The cloned `<textarea>` kept its `x-model="$store.chat.user_input"` and Alpine re-bound it on the clone. Proven by sentinel test: bare `.value` set = no leak; dispatching `input` = leak; clone had `_x_*` internals attached.
- **Fix**: `stripAlpine(root)` in theme_gallery.js — BEFORE inserting any clone, recursively remove every attribute matching `/^(x-|on|@|:|_x_)/i` (`x-*`, `@event`, `:bind`, inline `on*`, Alpine internals). Classes/IDs are kept, so app CSS still styles the clone identically. Verified after fix: store + real input unchanged while clone typing + autosize work.
- **Status**: FIXED (2026-10-25). Rule for FUTURE categories (Settings, Sidebar): **always stripAlpine() every clone before appending it anywhere in the live DOM.**

## 2. Context pill would poll the API + write the store
- **Symptom (avoided by design)**: real `#context-pill` fetches `/api/system/context_size` and POLLS every 5 s while open, and writes `$store.chat.currentTokenUsage`. Reviving the clone = API traffic + store mutation.
- **Fix**: `installFakeContextPill()` — hand-built replica using the app's exact markup/classes (`pill-button`, `pill-outline`+`pill-fill` SVG (path `M38 1 H61 A14 14 0 0 1 75 15 …Z`, `pathLength=100`, dasharray trick), `context-popup` hero/`ctx-bar`/`ctx-legend`/`popup-rows`, compaction badge, threshold zone) driven by `tgState.FAKE_CONTEXT` (35% · 11,456/32,768 tokens). Own click toggle + outside-click close. No fetch, no Alpine.
- **Status**: DONE + post-QA tweak (2026-10-25): the `_pillOutside` doc-listener (outside-click close, copied from the real `@click.outside`) was REMOVED — popups are now **STICKY**: each opens/closes only via its own pill button. Reason (user QA): in split view, clicking pane B's pill closed pane A's popup, making side-by-side comparison impossible. Verified with REAL mouse clicks: A open → click B → both open; clicks elsewhere keep both; own button toggles. No API traffic.

## 3. Keyboard/paste could reach app window-level handlers
- **Risk**: while focused in the preview textarea, Ctrl+V could trigger the real app's image-upload paste handler; Ctrl+S etc. could fire global shortcuts.
- **Fix**: pane-level CAPTURE `stopPropagation` for `keydown`/`keyup`/`paste` + bubble `stopPropagation` on `input`; Escape passes through (Esc-close still works); native text insertion unaffected (stopPropagation ≠ preventDefault).
- **Status**: FIXED + verified (store unchanged during char-by-char typing).

## 4. Model dropdown: two failed approaches before the right one
- **Attempt 1** (`Alpine.initTree` on the cloned `.model-switcher`): Alpine error `model is not defined` — the clone carried the *already-rendered* `<option>` siblings of `<template x-for>`, whose `x-text="model"` was evaluated outside loop scope. Interim fix: strip rendered siblings before initTree.
- **Attempt 2** (revived + read-only guard): worked, but showed the REAL model name and allowed `lazyFetch` GETs.
- **Final design (user request)**: STATIC select — x-data stripped, children replaced with `.model-select` holding FAKE names (`gemma-4-12b-it-qat`, `llama-3.3-70b-instruct-awq`, `qwen3-32b-exl3`). No Alpine, no fetch, no real model anywhere in the preview.
- **Status**: DONE + verified (options fake, `switcherHasXData=false`).

## 5. Buttons/native actions had to be hard-disabled (user requirement)
- **Fix**: pane CAPTURE click listener blocks every click except `.tg-pane-label` (gallery's own controls) and `#context-pill` (fake pill); `preventDefault` also kills native defaults (label→file-picker, link jumps). `input[type=file]` disabled; readonly removed again in favor of "typing allowed" (see #1/#3).
- **Verified**: #upload/#send/#settings-btn dispatches → `defaultPrevented=true`; no modal, stream idle, real chat untouched; real-mouse click on gear did nothing; Freeze-hover still works (hover deliberately NOT blocked — froze the gear's hover style as proof).

## 6. Alpine.reactive proxy vs raw object
- **Symptom**: split toggle mutated `tgState.slots` but DOM never re-rendered (`panes: 0`).
- **Root cause**: `window.tgState = Alpine.reactive({...})` leaves `window.tgState` on the RAW target; bindings watch the PROXY. Raw-object mutations don't trigger x-show/x-for.
- **Fix**: keep object in a local, then `window.tgState = Alpine.reactive(tgRaw)` (use the returned proxy).

## 7. Reactive proxy identity breaks indexOf()
- **Symptom**: diff counts all zero; `slot.vars` not written.
- **Root cause**: Alpine proxies nested objects ON ACCESS — `for (const slot of this.slots)` hands out fresh proxies, so `this.slots.indexOf(slot) === -1`.
- **Fix**: iterate BY NUMBER; stable position stored as `slot._i`.

## 8. Concurrent renders raced and wiped panes
- **Symptom**: panes present then vanishing (`panes: 0`) after rapid state calls; drawer showed stale pane order.
- **Root cause**: `render()` is async (awaits theme fetches) but callers don't await; overlapping runs both `innerHTML=''` + append, tearing each other's output.
- **Fix**: serialized `render()` — `_renderBusy` mutex + ONE coalesced deferred pass per burst; pane-state snapshotted AFTER the last await; DOM build synchronous. Stress-tested with a 3-call hammer (swap+split+family) → DOM stayed consistent. **Updated 2026-10-25**: the `_renderAgain` one-slot flag was replaced by a promise chain — every call now returns a promise that resolves after the pass IT caused (before, a call arriving mid-render returned `undefined`, so `await render()` lied and harnesses read a half-rendered DOM).

## 9. Smaller ones
- Theme JSON metadata keys (`name`) polluted the diff → filter keys starting with `--` only.
- `setMode`/`setSlotMode` lacked unsupported-mode guards → linked cascade assigned `light` to dark-only Mocha. Fixed with no-op guards (+ disabled buttons).
- `toggleBoth` early-return blocked its own collapse branch (Both couldn't switch off once expanded) → restructured into expand/collapse branches.
- Diff drawer painted *under* sticky context pill → `.tg-pane { position:relative; z-index:0 }`.
- Pane label caption too low-contrast under light themes → `--text-secondary` instead of `--text-muted`.

## 10. SETTINGS category landmines (build notes, 2026-10-25)
- The settings modal exists ONLY while open (`x-if currentModal==='settings'`) and its fields are **Alpine-generated** from `$store.settings` → the `<template>` skeleton in `#modals` is an EMPTY shell; cloning it is useless. Technique used: `captureSettings()` mounts the REAL modal inside `#modals{display:none}` (renders, never paints → zero flash), runs the component's own `switchCategory()` over all 12 cats, waits ~320 ms (x-if + 170 ms entry anim), serializes `staticClone()` per tab, then `finally` restores `currentModal` + display.
- Nav buttons' `@click` holds the LOOP VAR (`switchCategory(cat)`), no literal → category tagging by **DOM order**: x-for renders one `<div>` per category in `sortedCategories` order (static siblings like Restart Server are NOT divs → stay untagged/blocked).
- Alpine binds input values/checked/selected as PROPERTIES; `cloneNode()` copies ATTRIBUTES only → `materializeValues()` syncs props→attrs on the live subtree before cloning, else every field serializes empty.
- One capture = 12 full-modal HTML strings (~7.6 MB total); split render ≈ 1.2 s. Acceptable; memoize parsed DOM if it ever hurts.
- **Pre-existing APP bug exposed (not ours)**: `module_library.html`'s audit overlay is `x-teleport="body"` → outside the component scope `x-show="audit"` throws `audit is not defined` on EVERY module_library tab mount (benign: x-cloak keeps it hidden; unmount removes it — verified no orphans). Also teleports briefly append to `body` during capture. Core-file fix would be needed → left alone per directive.
- Capture fires the app's OWN read-only GETs (`settings/load`, `chat/prompt`, `check_connection`, `libraries/status`) — never any POST/PUT. Verified: `hasChanges=false` + cats byte-length unchanged after Save/Revert/×/Restart clicks + EVIL-CHANGE typing.
- Unrelated pre-existing console error at page load: `editor.js` reading `.categories` of undefined (editor user module).

## 11. SIDEBAR category notes (build, 2026-10-25)
- Live clone (like welcome), rebuilt FRESH every render → mirrors user's real sidebar incl. their module panels + real chat list (privacy: real chat titles in preview, same caveat as settings values).
- Panel↔tab map: rail buttons keep `data-tab` through stripAlpine; panels are keyed by their `x-show` LITERAL `sidebarTab === 'mod/name'` — parse it BEFORE stripping. `data-tab=""` = built-in chat list (`#chat-list`).
- x-teleport popups (category-menu, chat-action-menu, tag-popup) live in `<body>` — they are NOT inside `.sidebar-shell`, so they can never be captured into the clone; `body > .category-menu` ×2 + `.chat-action-menu` ×N with x-show attrs are the APP's own (pre-existing), not ours.
- Real `#chat-list`/`chat-<id>` nodes precede the overlay in document order → app querySelector/getElementById always resolve to the ORIGINAL, never a clone.
- Deliberate deviation from the "mirror reality" seed rule: preview STARTS on the chat list, not the user's active tab — with the gallery tab active, a mirror would nest the gallery panel inside a gallery pane. Rail switcher exposes every panel (works per-pane in split).
- Verified (2026-10-25): 0 leftover Alpine attrs; switcher exact (rail .active + panel visibility, pane independence); clicks blocked: New Chat / chat row / day header / search toggles / category trigger / actions menu — `selectedChat`, `searchQuery`, category, BOTH localStorage keys, `currentModal` all unchanged; typing 'evil-search' in clone fired no `/api/chats/search`; theme islands light/dark computed correct; Esc ok; no new console errors.

## 12. HARNESS pitfall — stale screenshots (NOT a module bug)
- **Symptom**: browser_screenshot repeatedly returned identical-byte, outdated frames (dark panes that computed as light, phantom notices, reversed pane order).
- **Root cause**: the CDP-controlled window is occluded/minimized on the user's desktop → Chrome's compositor serves stale tiles; `fromSurface:false` unavailable (allow_raw_cdp disabled).
- **Rule**: NEVER trust screenshots from this session as proof. Verify via DOM queries, computed styles, `elementFromPoint`, store reads, network logs. For visual QA, ask the user to look at / foreground the OpenLumara window.
- Also: the page reloads itself occasionally during tests (user refreshing the visible window) — re-check state after external reloads; functional assertions must be run after them.
- UPDATE 2026-10-25 12:32: screenshots work RELIABLY once the user foregrounds the OpenLumara window (compositor stops serving stale tiles). A screenshot then catches layout bugs computed-style checks MISS (leftover transforms, anchor margins, clipped content) — prefer it when available.

---

## 13. Pre-existing APP gaps found via the sheet (NOT ours — faithful copies, don't touch core)
- **`.settings-error` has no CSS anywhere** (all stylesheets incl. themes
  grepped): modal.css styles `.error`, the settings template uses
  `.settings-error` → the real Retry button is a browser-default UA button.
  Sheet shows it as-is with an honest caption.
- base.css press-scale selectors `category-item/chat-item/tool-call-header`
  and modals.css `confirm-modal/confirm-overlay/modal-overlay` are DEAD
  (no template uses them) — deliberately excluded from the sheet.

## 14. ORACLE technique + self-inflicted incident (2026-10-25, user QA round 4)
- **Settings capture = ground-truth oracle** for real per-user markup:
  `tgState.setCategory('settings')` → poll for `_settingsCapture` (shape:
  `{sections:[{html,…}×12]}`, ~7.6 MB) → `DOMParser` → querySelector (e.g.
  `.unsafe`) — showed unsafe rows are `.unified-card.unsafe` NOT `.item.unsafe`
  (user's "wrong css element", confirmed). setCategory is the module's own
  SAFE path (hidden mount, restores state). Capture takes ~4-5 s: poll with
  short evaluates, never await it inside one 15 s tool call (timed out once).
- **INCIDENT (caught by screenshot, fixed same session)**: an inserted
  fragment re-opened `.global-search-content > .global-search-results` and
  closed them early with `</div></div>`; the original closers then unbalanced
  the whole sheet: E19 got swallowed into E14 (14 sections!) and
  `.categories-modal` escaped `.tg-elements` scope → position:fixed floating
  panel again. LESSON: replacements must be div-balance-neutral; after any
  template edit RE-COUNT sections and verify `.categories-modal` computed
  position is static.

## 15. SPLIT "SYNC" MODE (feature, 2026-10-25 — replaced "🔗 modes" + Freeze-hover)
- 🔗 button renamed **modes → sync** (user QA proposal, adopted): while ON in
  split, the panes act as one — (1) mode buttons cascade (old `linked`
  behavior), (2) MAIN-scroller position mirrors by RATIO (inner scrollers like
  the reasoning sbox stay local; echo guard 120 ms), (3) hovered element gets
  `.tg-xhl` outline on its COUNTERPART in the other pane (outline uses each
  pane's OWN --accent), (4) real (isTrusted) clicks replay on the counterpart.
- ENGINE (theme_gallery.js bindPaneMirror, bound per-pane at the END of
  lockdownPane → dies with every rebuild, all category paths covered):
  · mapping = document-order index among pane.querySelectorAll('*') (panes
    render identical markup). Guard: node counts must match — diverged
    pane-local state (different settings tab…) auto-disables hover/click map.
    Settings tab switches NEVER diverge (all 12 modals stay mounted, display-
    toggle only — verified 5558===5558 nodes after a switch).
  · click mirror rides the BUBBLE phase: the lockdown's CAPTURE listener
    already swallowed inert clicks, so only locally-effective ones (fake pill,
    settings/sidebar tab buttons) get mirrored. Skips input/textarea/select
    (typing stays local) and .tg-pane-label chrome (mode buttons cascade via
    linked already). Synthetic clicks ignored (isTrusted) → no feedback loops.
- VERIFIED: ratio 0.108↔0.108; outline appears ONLY in the other pane, in its
  own accent (green under cute-stardew); sync OFF → no outline; REAL mouse
  click on one fake pill opened BOTH popups; tab switch keeps trees equal.
- FREEZE-HOVER REMOVED (user: "used it once, that is it"). If ever needed
  again: delegated mouseover on .tg-panes copied getComputedStyle props
  (color/bg/borders/shadow/opacity/transform…) into inline styles of elements
  matching FREEZABLE, with a [{el,prev}] restore list + counter note; sidebar
  checkbox + .tg-frozen-note strip + .tg-freeze-box CSS. All gone now — the
  hover-mirror supersedes it for comparison work.

- CHAT-SAMPLE GAP (found 2026-10-25 when user asked "is sync only for
  elements?"): the chat-sample branch of _renderInner was v1 code and never
  called lockdownPane → no mirror (no key-leak protection either). Fixed: the
  branch now marks fields .tg-allow and calls lockdownPane like every other
  category. Verified: scroll follows (300px), hover mirrors. Note: click-sync
  in the chat sample is a deliberate no-op — the sample is fully static, so no
  click there "does" anything to mirror.
- SYNC + BOTH CONFLICT → **RESOLVED 2026-10-25 (user decision)**: the mode
  cascade is GONE. `setSlotMode` is always local (one press = one pane);
  sync = scroll/hover/click mirroring only; 🔗 tooltip updated to match.
  A "Shift-click apply-to-all" escape hatch was OFFERED AND DECLINED — user
  policy: NO hidden/extra features that confuse ("prefer the user works an
  extra second"), idea parked in case they change their mind.
- HIGHLIGHT (sidebar checkbox, 2026-10-25 user request): outlines the element
  UNDER THE CURSOR in its OWN pane (.tg-xhl, same per-island accent). Works
  in single view and independently of sync; with both ON, source AND
  counterpart outline. OFF → clears lingering outlines. State: tgState.highlight.
- HOVER-OUTLINE OWNERSHIP (user follow-up same day): the checkbox is now the
  SOLE owner of ALL hover outlining — sync (🔗) itself never draws boxes
  anymore (mouse*over handler early-returns on !highlight; counterpart still
  drawn when split regardless of linked). 🔗 tooltip drops "hover-outline".
  Checkbox label "Highlight element" + visible one-line description under it
  (.tg-opt-desc). Verified matrix: OFF+sync → 0/0; ON → 1/1; OFF mid-hover →
  0 lingering; sync-OFF+ON → 1/1.
- HARNESS POISONING LESSON (this session): instrumenting tgState slots broke
  my own tests. Two traps: (a) a leftover `Proxy` wrapper on slot objects +
  `bind()`ing an old method copy BYPASSES the Alpine proxy → writes go to raw
  targets, later reads hand back stale values ("phantom light|light"); (b) a
  sloppy defineProperty getter (shared window._v keys) returned the wrong
  slot's value. FALSE cascade alarms followed. GROUND TRUTH for mode tests =
  DOM only: pane computed --bg-primary + .tg-mini-mode-btn.active. Also:
  dark-only families (cute-rouge has NO light) snap to 'dark' under the old
  cascade — looked like "state says light, island renders dark".
- mouseover mirror guard `last===src`: dispatching mouseover on the SAME
  element twice no-ops → a "broken" outline may just be the guard; hover a
  different element first.

## 16. COVERAGE category (built 2026-10-25, user: "do both")
- 6th category: union of classes RENDERED by statically-measurable sources —
  #tg-sample-elements + #tg-sample-chat + app shell (sidebar-shell, header/
  input, server-rendered #modals skeletons) + settings capture IF visited
  this page-load (84→85/111 when present). Classifier: CORE_SHEET whitelist,
  module sheets bucketed (never count vs core), DEAD set, responsive/hljs/
  own CSS excluded. Per-file rows, chips green/red(dashed)/grey(strike).
  ↻ re-measure button = whitelisted in lockdownPane via
  `.tg-el-coverview-refresh`. Memo: tgState._coverageReport (per page-load).
- HARVEST LANDMINES (all three bit during build): (a) querySelectorAll('*')
  skips the ROOT → false miss on .sidebar-shell; (b) SVG classList is NOT
  iterable here (svg-heavy sources threw, wiping the pane host) → use
  getAttribute('class'); (c) DOMParser roots are Documents (nodeType 9, no
  getAttribute) → include root ONLY when nodeType===1. A throw inside a
  pane builder happens AFTER host.innerHTML='' → panes:0 until next render.
- HONEST LIMITS (shown in the pane's note): Welcome/Sidebar are runtime
  clones (not measurable statically); per-tool JS renderers likewise.
  Remaining reds are by-design: appearance.css (live in Settings cat),
  restart/move-chat (live in categories), login (skipped by user decision),
  transient state classes (.streaming/.token-bar-hidden/.token-fade/
  .actions/.nopadding), .diff-side (hidden by the sheet's 560px cap ON
  PURPOSE — container query would swap the unified diff away, see E8).
- DRIFT DISCOVERY: the RUNNING server (:3001) serves CSS/markup NEWER than
  the git workspace — `.tool-result-image` exists in the served toolcalls.
  css but NOT in /home/abc/git/openlumara's copy (served root is not the
  repo dir; tabbyAPI ≠ this app). Trust the SERVED CSSOM as ground truth;
  repo grep can miss live classes.

## Open / next (as of 2026-10-25 ~11:07)
- **Welcome category: visual QA PASSED** (2026-10-25 ~11:14) — one tweak implemented: fake-pill popups made sticky for split-view comparison (see #2). Current live state: gallery open, category=welcome, SPLIT, 2 panes, both popups open.
- **Settings category: BUILT + functionally verified** (2026-10-25): 12 tabs captured, pane-local switcher (per-pane independence), fields inert, buttons blocked, theming islands work (light vs dark computed colors), Esc ok, network clean. **User visual QA pending.** Note: compression note "server-rendered into #modals" was WRONG — see #10.
- **Sidebar category: BUILT + functionally verified** (2026-10-25), **user visual QA pending**. Live state: gallery open, category=sidebar, split, A light / B dark, linked off.
- Categories: Welcome ✅QA'd · Settings ✅QA'd · Sidebar ✅QA'd · **Elements: ALL PLANNED PASSES BUILT** (15 sections; composer `.input-wrapper` truth + popups/menu fragments + global-search/categories modal fragments + passive globals). FIDELITY RULE: exact ancestor chains (`.settings-modal.modal` dual class; `#input>.input-wrapper` stretch container; `.global-search-modal-content` is the fixed one) + computed-signature checks. Remaining = user-QA polish only. Parked: E15 (deferred by decision), E18 custom tool views, coverage badge, demo-safe mode.
- HARNESS lesson this session: when scripting split tests, CHECK `linked` first — setSlotMode with linked ON cascades to both slots (correct behavior, fooled one test into "islands broken").

- **Elements "Popups & menus" OPEN ISSUE → FIXED 2026-10-25 (option a):** E13 now
  renders `.category-trigger.open` + `.category-menu` as ONE fused box (the app's
  real open-state design) inside `#chat-list.tg-cat-fused-scope >
  .category-select`; signatures verified vs the live DOM trigger; wrapper width
  needed id+class+!important (beats the `#chat-list` width:auto neutralization).
  ROUND 2 via fresh screenshot: teleport-anchor residue survived neutralization
  (global-search content translate(-50%,-50%), chat-action-menu margin-top:-24px,
  tag-popup translateY(-100%)) + our own display:block!important flattened app
  flex (merged chips). All neutralized now; screenshot confirms clean section.
  Final user visual confirmation pending.
- Minor fact: `category` does NOT persist across page reloads (resets to 'chat');
  reopen = `body.toggleAttribute('data-tg',true)` + `tgState.setCategory(...)`.
- Parked: coverage report / acceptance #1 (user put on hold); Tool calls category; raw-variable category (user idea: one element per var to eyeball missing ones — pairs with diff drawer's "missing" rows).
- Parked (2026-10-25, user decision): collapsing the sidebar with the gallery tab active hides the category list but keeps the cover up. Decided **NOT a bug** (same collapse semantics as the Chats tab: hide the list, give the content more room; the tab strip is one click away and Esc/✕ close also restore it). Improvement idea if it ever annoys: put a compact **category dropdown in `.tg-bar`** so the cover is self-sufficient.
- Files (all gitignored, branch `fix/theme-undefined-variables` has the var fix at 1218306; repo was seen detached at upstream/dev `6e46d51` during this session):
  `module.py`, `webui/templates/chat/index.html`, `webui/templates/sidebars/theme_gallery.{html,svg}`, `webui/assets/css/theme_gallery.css`, `webui/assets/js/theme_gallery.js`.
- Cache pitfall: after asset edits, `fetch(url,{cache:'reload'})` then reload before testing (SW + heuristic caching); raw CDP disabled.
---

## 17. Settings preview: Appearance tab frozen on its loading spinner (capture race)
- **Symptom**: first visit to the Settings category shows the Appearance tab as an endless centered spinner — no fields at all. The other 11 captured tabs are fine. Switching tabs INSIDE the preview (Audio → back to Appearance) does **not** recover it: the preview is static HTML, so whatever `staticClone()` froze stays frozen (verified 2026-10-25: `contentMounted=false`, `.loading` display `flex` before AND after a nav round-trip that did switch the shown section).
- **Root cause**: `captureSettings()` waits only for the `.settings-modal` element to exist, then immediately drives `switchCategory('appearance')` + `sleep(320)` and serializes. The settings component loads its store on mount and renders a `loading` state plus the real content inside `<template x-if>`. If the capture runs while `loading` is still true, `staticClone()` serializes `<div class="loading"><div class="spinner"></div></div>` and the **unmounted** template; `stripAlpine()` removes the x-if attribute but can never mount template content → the pane is a static spinner forever.
- **Upstream trait**: the real Settings modal shows the same spinner for its initially selected tab until settings load (user-confirmed 2026-10-25). The gallery's job is not to freeze that intermediate state.
- **Suggested fix**: before the first capture, poll `Alpine.$data(sm).loading === false` (timeout ~2 s); after each `switchCategory()`, poll until the section has a `.content` child and no visible `.loading`, and only then `staticClone()`. If a tab still captures a visible `.loading`, mark it failed / allow a re-capture instead of silently freezing it.
- **Fix (2026-10-25, user approved)**: `captureSettings()` now
  1. waits for `$store.settings.loading === false` (poll 50 ms, 2.5 s cap) right after the modal mounts — until then `.content` is not mounted at all AND `sortedCategories` is empty, so an early capture also loses the category list;
  2. replaced the blind `sleep(320)` per tab with `_waitForSection(sm)`: `.content` exists + `.loading` is display-hidden + the content's innerHTML stopped changing between two ~120 ms samples (1.4 s cap, one retry) — fast when a tab mounts instantly, patient when it fetches data (module_library …);
  3. records any tab that never got there in `cap.failed`, and `buildSettingsBody()` renders a `.tg-capture-note` banner naming them (new CSS rule). A leftover placeholder now reads as "the capture arrived early", not as a theme bug.
  Bonus: the capture is usually FASTER than the old fixed 12 × 320 ms.
- **Verified (2026-10-25, standalone probe of the new algorithm against the live app)**: `$store.settings` took **470 ms** to finish loading — well past the old `sleep(320)` budget, which is exactly why Appearance used to freeze. With the wait: all **12/12 tabs `ready`, `failed: []`**, Appearance captured **418 KB of real markup** (it used to be ~1 KB of spinner), store-loaded 470 ms, total capture **3 709 ms** (vs ~3 840 ms + overhead for the old fixed delays → now faster AND complete).
- **Status**: FIXED 2026-10-25 (found via screenshot QA while working on #18). The running page still holds the old build — one reload activates it.

## 18. Preview panes forced base.json's Arial, ignoring the user's font
- **Symptom**: with a non-default font set in Settings → Appearance, the Settings preview (and parts of the Sidebar preview, e.g. `.tag-chip`) rendered in **Arial** while the rest of the app used the chosen font — two fonts inside one pane, i.e. a theme-QA tool showing a font that is not the real one.
- **Root cause**: `theming.js apply()` writes `base.json` → the user's font (`setFont` → `--font-primary` inline on `<html>`) → theme vars. A pane only received `base + theme[mode]`, so its `--font-primary` was base.json's Arial. Elements that *explicitly* say `font-family: var(--font-primary)` (`.settings-modal` in modal.css, sidebar `.tag-chip`, …) resolved to Arial inside the pane, while elements that merely inherit kept the user's font. (No theme JSON declares `--font-primary`, so the theme can never legitimately win here.)
- **Fix**: `RUNTIME_VARS` + `applyRuntimeVars(pane, slot)` in theme_gallery.js — after the theme vars are written, copy the root's LIVE inline value for `--font-primary`, `--font-size-base`, `--chat-content-width`, `--message-max-width`, `--token-fade-duration`, skipping any var the previewed family declares itself (a theme beats the user font in `apply()` because theme vars are written last). Keeps the module future-proof for the other runtime vars too.
- **Verified** (2026-10-25): with the root font forced to Georgia, `.settings-modal .nav .item` / `label` / `description` in the pane read `Arial` before the overlay and `Georgia` after it; root restored, no settings written.
- **Side effect**: the E19 swatch grid now lists those 4 extra runtime vars per pane (they ARE part of what the pane renders); the "66 vars" note in ELEMENTS.md is now ~70.
- **Status**: FIXED 2026-10-25 (user asked for this one). Needs a page reload to load the new asset.

---

## 19. Dead-code sweep + module metadata (2026-10-25, no behaviour change)
- **Removed `isColorish()`** (top-level) and **`nextFamily()`** (tgState method): zero call sites. Evidence method: list every definition (45 object methods, 6 top-level functions) and count uses as `this.<name>(` / `tgState.<name>` — only those two were orphaned. (The diff chips' colour heuristic is an inline regex in the template, so `isColorish` was superseded, not needed.)
- **Removed smaller leftovers**: unused `x-ref="panes"` on `.tg-panes` (JS uses `document.querySelector('.tg-panes')`); the dead `.tg-cat:disabled` rules (no category button is ever disabled — all six exist now) and the stale "only Chat exists in v1, the rest are listed greyed" comment in the sidebar panel.
- **Follow-up sweep (same day)**: the JS state block carried the *second* copy of the stale "only Chat exists in v1" claim → replaced with what `category` actually means; the sidebar panel's category comment still called the other five surfaces "the agreed roadmap" (they are all built) → rewritten; and `x-init` called `tgState.init(this)` although `init()` takes no parameters → `tgState.init()`. Comment/argument-level only, zero behaviour change; served JS re-checked with `new Function` (OK, 76 207 chars).
- **CSS audit**: every `.tg-*` class defined in theme_gallery.css (63) has at least one use in JS or in rendered markup → no dead classes to remove.
- **module.py**: added `category = "development"` (same grouping as git/editor/coder/codeeditor) so the module isn't uncategorised in Settings → Modules; dropped ~25 trailing blank lines; corrected the docstring claim — it now says it never writes theme/settings data and names the two real side effects (`closeOverlay()` → core `setTab()` persists `sidebarTab`; `captureSettings()` drives the live settings component while `#modals` is hidden).
- **ELEMENTS.md**: E19 now reads "~70 vars/pane (66 theme + 4 runtime)" — the #18 runtime overlay adds font/size/widths to the swatch grid.
- **Restore points**: `.backups/user_modules/theme_gallery` (pre-A4/#17) and `.backups/user_modules/theme_gallery.20261025_1652` (pre-cleanup, i.e. A4+#17 included).

---

## 20. Template split: chat/index.html → macro shell + 18 include files (2026-10-25)
- **Why**: the module's chat template was one 1 244-line / 83.8 KB file, ~95 % of it inside a single `{% macro below_header() %}`. Core's own answer to the same problem is includes (`chat/index.html` pulls `chat/header.html`, `welcome.html`, `chat/turns/*`, `chat/input.html`…; `modals/settings/index.html` pulls its 8 tabs), so the split follows the project idiom: **no numbered prefixes, semantic subfolder** (`chat/elements/` alongside `chat/turns/`).
- **Layout**: `chat/index.html` = the macro + 3 includes. `chat/overlay.html` = the cover (both top bars, `.tg-panes` host, diff drawer). `chat/sample_chat.html` = `#tg-sample-chat`. `chat/element_sheet.html` = `#tg-sample-elements` + 15 includes. `chat/elements/{buttons, form_controls, chips, pill_states, messages, composer, reasoning, tool_calls, agent_chain, prompt_processing, uploads, notices, popups, modals, swatches}.html` = one `<section class="tg-el-section">` each, listed in sheet order.
- **PREFIX RULE**: includes must be written `'theme_gallery/chat/…'`. `env.loader` is core's FileSystemLoader FIRST, then a `PrefixLoader` keyed by module name (`channel.py::setup_ui_extensions`), so an unprefixed path can never resolve inside the module. The prefix also keeps the partials private: only paths mirroring a core template are ever rendered standalone.
- **SILENT FAILURE MODE**: `extension_slot()` swallows any template error, logs "skipping UI extension/slot" and emits nothing — the page still returns 200, the overlay is just absent. Verify by DOM, never by HTTP status.
- **Verified 2026-10-25 BEFORE the browser reload** (`GET /` + DOMParser — note `querySelectorAll` does NOT descend into `<template>`, one false alarm came from that): `.tg-cover` 1 with 2 `.tg-bar`, 1 `.tg-panes`, 1 `.tg-drawer`; `#tg-sample-chat` = 355 nodes / 6 turns / 1 `.input-wrapper` / 2 `.tool-call-card` / 1 context pill (= a 356-node pane, matching the pre-split count); `#tg-sample-elements` = **15 sections in the exact pre-split order**, 1 `.tg-el-swatches`, both E14 fragments present with 2 `.category-row` (the BUGS #14 imbalance did NOT come back). No "skipping" line in the served HTML.
- **Deliberately NOT split**: JS (1 403 lines) and CSS (23 KB). `coder.js` is 75 954 B vs our 76 207 B, so size is no argument; and the module asset loader sorts filenames per directory but NOT directories (`channel.py::_ui_asset_urls`), so core's role-folder layout (`components/`, `renderers/`…) can't be reused inside a module.
- **Restore point**: `.backups/user_modules/theme_gallery.20261025_1731` (pre-split).
---

## 21. JS split: one 1 403-line file → 7 parts + a registry (2026-10-25)
- **Shape**: `theme_gallery.js` keeps only the shared helpers (`resolveVars`, `rawVars`, `stripAlpine`, `sleep`, `materializeValues`, `staticClone`), the part registry and the **single** `alpine:init` handler. Six domain files each call `tgPart({...})` exactly once: `_state` (state keys, startup, queries, controls, diff — 23 methods), `_render` (`render`/`_renderInner`/chrome/swatches — 7), `_categories` (elements/welcome/sidebar/settings bodies + fake pill — 6), `_capture` (`captureSettings` + the 2 wait helpers — 3), `_coverage` (2), `_sync` (`lockdownPane`, `bindPaneMirror`, `_xhlClear` — 3). **44 methods, same inventory as before.**
- **ORDER CONTRACT**: the boot handler merges `window.tgParts` and wraps the result with `Alpine.reactive()` — at `alpine:init`, i.e. AFTER every module script has executed (all sit at the end of `<body>`, before DOMContentLoaded). So filenames carry **no numbers** and load order genuinely does not matter. Corollary: never assign `window.tgState` outside that handler — exactly one proxy may exist.
- **Why not numbered files**: no file in this repo uses numeric prefixes, and the module loader sorts filenames per directory but NOT directories, so numbering would encode a contract it cannot enforce. The registry makes the contract explicit instead.
- **`this` is unchanged**: methods are invoked through the proxy (`tgState.x()`), so `this._renderBusy`, `this._queued`, `this._settingsCapture`, `this._coverageReport` behave exactly as before.
- **VERIFIED 2026-10-25 without a reload**: the served page lists 7 `theme_gallery` scripts, all HTTP 200 and `new Function()` OK (8 327 / 5 894 / 16 966 / 9 158 / 11 380 / 13 342 / 9 253 chars); running all 7 concatenated in a stubbed sandbox registers **6 parts** and merges to **44 methods (missing `[]`, extra `[]`)** plus all 15 state properties — `FAKE_CONTEXT` 11 456 tokens, `RUNTIME_VARS` ×5, `slots` `[{family:'',mode:'dark'}]`.
- **Restore point**: `.backups/user_modules/theme_gallery.20261025_1748` (pre-JS-split).

---

## 22. COMPLETENESS PASS (2026-10-25) — Elements sheet vs the whole channel+core UI
- **Trigger**: user asked for a review of *all* WebUI elements, with missing ones added.
- **Oracle check first (Phase 0)**: served CSS byte-lengths equal the checkout for 11/12 core sheets (`toolcalls` 10 617 = 10 617 … `settings/modal` 19 203, `base` 3 579; only `combined_block.css` differs by 5 B). ⇒ **BUGS #16's "DRIFT DISCOVERY" was wrong**: `.tool-result-image` exists in NO core CSS (repo grep + all 1 125 served rules). E8's "served-build rule" caption was false; the class is unstyled and the sample now says so.
- **Coverage was measuring a fraction (methodology bug, fixed in `theme_gallery_coverage.js`)**: the app CSS uses **native nesting** (`agent_chain.css` = 349 lines, 2 top-level rules). `computeCoverage()` walked only top-level `ss.cssRules`, so every nested selector was invisible → denominator "110 core classes". The whitelist ALSO mis-classified `module_library.css`, `api.css`, `logs.css`, `system_prompt.css` as module sheets although they live in `channels/webui/assets/css/modals/settings/`. Both fixed: recursive rule walk + those four moved to `CORE_SHEET`. Recursive count = **502** core classes.
- **Audit method**: recursive CSS selectors × app markup harvested from the page DOM **and every `<template>`'s `.content`** (querySelectorAll does not descend into templates — the trap that made the first attribution run return 4 rows) × the sheet markup. Result: **180** app-rendered, core-styled classes absent from the sheet.
- **Added** (16 new section files → 16 new sections, sheet 15 → **31**): settings shell/nav/footer, settings field widgets, appearance, audio, modules/channels + drill-down, module library, library audit overlay, system logs/prompt, move-chat/restart/backdrop, sidebar shell + day groups, welcome panel, web-search view, file/grep/glob/tools-load views, shell/jobs views, tool-results value grid, login. Details + per-class inventory in ELEMENTS.md ("COMPLETENESS PASS").
- **Final renderer coverage** (recursive static union, same classifiers as the Coverage category): **465 / 496 = 94 %**. The remaining 26 are not rendered anywhere in the app: runtime-only state/animation classes (`.token-fade`, `.token-bar-hidden`, `.streaming`, `.animate-in`) plus dead selectors (old appearance markup `.theme-section/.theme-buttons/.theme-label/.color-theme-section`, `.popup-refresh`, `.user_command`, `.command_response`, `.welcome-subtitle`, `.file-size`, `.file-download`, `.model-row`, `.group-content`, `.library-item-lib`, `.preview-bracket`, `.category-hint`, `.option-description`, `.name-btn`, `.reasoning`, `.group`, `.drilldown`, `.confirm`, `.connecting`).
- **Fidelity bugs found while adding**:
  - **E4 used invented classes** (`.row-label`/`.row-tokens`); the real popup is `.popup-key`/`.popup-value` + `.ctx-bar/.bar-seg/.bar-threshold(-zone)/.ctx-legend/.legend-*/.ctx-compaction/.compaction-label/.popup-loading`. Rewritten.
  - **E8** lacked the side-by-side diff (`.diff-side/.diff2-*`), unreachable while the sheet caps `.diff-container` at 560 px; added `.tg-el-diffwide` (720 px) so the `@container (min-width:640px)` branch renders too.
  - **E9** lacked the no-reasoning `.bare-header` step variant (`.step-args`, `.step-summary`, `.step-content`, `.nogaps`).
  - **E1** lacked `.prompt-text`.
  - **E3's** `.welcome-panel` scope wrapper is now `.tg-el-scope`, so the sheet's neutralizer no longer flattens the REAL welcome panel shown in E31.
- **Core defect found (not ours, report only)**: `templates/login.html` links `css/base/variables.css`, which **does not exist anywhere in the repo**, and the login page loads no theme engine. So `login.css`'s hardcoded fallbacks are what actually paint, and `--border` doesn't exist in base.json (only `--border-color`). E35 renders with the vars reset to those fallbacks, matching reality.
- **Scope boundary, evidence-based**: disabled core modules (calendar, characters, scheduler, shell, tutorial, coder, git) serve **no CSS at all** — `/ext-assets/<mod>/css/<sheet>.css` returns 404 for exactly those, 200 for the enabled docs/memory. A sample without its stylesheet is not a preview, and loading disabled modules' CSS globally would restyle the user's page. Those surfaces remain covered live by the Sidebar category; ELEMENTS.md records the reasoning.
- **Verified without a page reload** (server renders templates per request): `GET /` → 30 `.tg-el-section` in the expected order, `.tg-cover` 1 / `.tg-bar` 2 / `.tg-panes` 1, no "skipping UI extension" line, no leftover Jinja. Gap metric after the pass: **missing core-rendered classes = 0** (was 180). Spot-checked computed styles on the freshly-rendered sheet inserted into the real `#chat-window`: all 30 sampled elements resolve real values (theme select 6 px radius, mode button accent fill 36×36, library item 96 px, welcome title 24 px/1.5 rem, restart spinner 40 px accent ring, login input `#333` border…). CSS braces balanced; `theme_gallery_coverage.js` parses (`new Function`, 10 062 chars).
- **Restore point**: `.backups/user_modules/theme_gallery.20261025_1824` (pre-pass).

---

## 23. A6 visible failure states + Coverage corrections (2026-10-25)
- **A6 was NOT fixed** — it was only partially: the settings capture had a
  visible "unavailable / incomplete" note (#17), but the *boot* paths were
  console-only. Verified by reading the code, not by assuming:
  - `init()` caught `/api/themes` failures, logged `console.error` and
    **returned before `render()`** → a permanently empty pane, empty family
    dropdown, no hint.
  - `loadFamily()` swallowed per-family failures into `cache = {}` → a
    half-themed preview (base.json only) that reads like a theme bug.
  - `render()`'s catch logged only → after `host.innerHTML = ''` a throw left
    `panes: 0` (the BUGS #16 harvest landmine did exactly this).
  - `computeCoverage()` had no guard → same blank-pane failure mode.
  - an empty `/api/themes` list produced no message at all.
- **Fix** (all in the module, no core edits):
  - state: `loadError` / `loadErrorDetail` / `loadWarning` / `failedFamilies`
    + `retryThemes()`. `init()` never returns early now; failures set the
    state and still call `render()` + `installPaneControlEvents()`. HTTP status
    is checked (`!r.ok`) so 500/404 are failures, not empty JSON. The Esc
    listener is guarded by `_escBound` (retry re-enters `init()` — before,
    every retry would have stacked another window listener).
  - render: `_paneNote()` (blocking, title + exception + ↻ retry, `.tg-allow`
    so the lockdown lets the button through) and `_paneWarn()` (non-blocking
    strip). The pane now shows: boot failure / empty theme list / this
    family's JSON failed. Single-view pane label appends
    "· ⚠ theme JSON failed". `render()`'s catch appends a `.tg-error-note`
    into `.tg-panes` instead of leaving it blank.
  - coverage: guarded `computeCoverage()` with the same note + retry.
  - sync: the split click-mirror ignores `.tg-error-note/.tg-warn-note`, so a
    retry is never replayed into the other pane (two retries per click).
  - CSS: `.tg-error-note/-title/-detail`, `.tg-retry-btn`, `.tg-warn-note` —
    theme vars only (`--error`, `--button-bg`, `--bg-tertiary`).
- **Verified** (stubbed sandbox, real part code): network failure →
  `loadError` + detail + `render()` called once + `_escBound` true; empty list
  → `loadWarning` + render; family 500 → `failedFamilies ['monochrome']`;
  `retryThemes()` clears and re-inits without stacking listeners. All 7 served
  JS parts parse (`new Function`), CSS braces balanced.
- **Coverage corrections (same pass)**:
  - `.modal-overlay` removed from the DEAD set — `modals/index.html` renders it
    for every open modal (verified in the template), so it was mislabeled dead.
    (`confirm-modal`, `confirm-overlay`, `category-item`, `chat-item`,
    `tool-call-header` stay: CSS-only, no template/JS use.)
  - the per-file note for `login.css` / `restart.css` / `move_chat.css` was
    stale ("live elsewhere / login skipped") — all three now have static
    samples (E29/E35); the note now says grey = state or dead selector.
- **Duplicates recorded, not removed** (user decision): ELEMENTS.md now has a
  "Recorded duplicates" table (owner section + duplicate + what is genuinely
  new) and the trim policy to apply when the user asks for it.
- **Login**: kept, per user decision — likely a remnant of an older version,
  documented as such in E35/E14 notes.
- **Needs one page reload** to load the new JS/CSS.

---

## 24. Performance pass (2026-10-25) — measured hot paths, not guesses
Diagnosis first (all costs confirmed by measuring the real algorithms against
real DOM shapes):

| hot path | cost (measured) | why it was hot |
|---|---|---|
| settings re-render | 346 ms (BUGS #10) | 12 captured modals (~129 KB each, 7.6 MB total) were re-**parsed** from HTML strings on EVERY render, including every mode/family press |
| split scroll mirror | 17.3 ms per scan (5 000-node pane) | `mainScroller()` ran `getComputedStyle` on every element, on every scroll event, for the source pane **and** each counterpart |
| split hover mirror | 1.17 ms per event | `mapTo()` rebuilt `[...querySelectorAll('*')]` for both panes and `indexOf`'d on every mouseover frame |
| var-only state changes | full rebuild | every `setMode/setFamily/swap` re-cloned all panes even though only CSS custom properties changed |

Fixes:
- **Fragment cache** (`settingsFragCache` in `_categories`): each captured tab
  is parsed ONCE and deep-cloned afterwards. Bench: 129 KB parse **8.7 ms** vs
  clone **1.0 ms** (~8.7× on that stage; the 7.6 MB capture is where the 346 ms
  lived). Keyed by `cat` + source-HTML check, so a re-capture re-parses.
- **Fast path** in `_renderInner`: a structure fingerprint (`category|pane
  count`) plus `_rebuildNeeded` decides between `_applyToPanes()` (vars +
  chrome + swatches only) and a full rebuild. Theme/mode/family/swap now touch
  only inline custom properties. Stale keys are removed per pane
  (`pane._varKeys`), because a key the new theme does not define would
  otherwise keep the old inline value — the exact leak the diff drawer hunts.
  `_rebuildNeeded` is set when structure/content actually changes: category
  switch (via the fingerprint), capture landing, coverage re-measure,
  failed-family changes, boot/retry (so error/warn strips update).
  Pane chrome is updated in place (`updatePaneChrome`: label text, per-pane
  select value + `data-slot`, mode-button active/disabled).
- **Cached main scroller** (`_paneScroller` + `_invalidateScroller`): one scan
  per pane, invalidated on settings-tab switch and sidebar-tab switch.
  Bench: **17.33 ms → 0.008 ms** per lookup. Split scroll mirrors now cost
  essentially the scroll event itself.
- **Cached element list** for hover mapping (`p._flat`): built once per pane.
  Bench: **1.17 ms → 0.001 ms** per hover frame.
- Deliberately NOT done: caching the live clones for welcome/sidebar across
  var-only updates (they mirror the user's DOM; a stale clone would be a
  correctness regression, so they still refresh on rebuild), and memoizing
  the settings capture itself beyond the page-load (unchanged behavior).

Bug caught by the fixture tests (would have shipped silently): the first
`_applyToPanes` used `document.querySelectorAll('.tg-panes > .tg-pane')`, so a
second gallery instance (or a fixture) could receive the writes. Now it
explicitly takes and scopes to the host. The same tests verified: label text,
per-pane select value + `data-slot`, mode-button active/disabled, stale custom
property removal, and var keys tracking.

Verification: all 7 served parts parse (`new Function`); markers present for
fast path / host scoping / `_varKeys` / fragment cache / scroller cache / flat
cache. Exact settings render time after the change needs one page reload +
a Settings visit to be measured live (expected to drop to clone-only cost;
the 346 ms was dominated by re-parsing).

**Needs one page reload.**

---

## 25. Chat sample: agentic reply re-authored on CORE modules (2026-10-25)
- **Reference**: the user opened a real 13-step chain in the app; inspected its
  live DOM (`#chat-window #messages`) instead of guessing. It ran on user
  modules (Editor / Git / Web). Request: prefer **core** modules.
- **Tool-naming truth** (from `templates/chat/toolcalls.html` +
  `combined_block.html`): the card header derives
  `module = displayName.split('_')[0]` title-cased + `': '`, and
  `name = displayName.split('_').slice(1).join(' ')`. Provenance chips use
  `module + ':'` and `action.name.replaceAll('_',' ')`, with `.action-count`
  carrying the literal `xN` (verified: the x lives inside the span).
  So `scheduler_add_job` renders "Scheduler:" + "add job", not `add_job`.
- **New question**: docs → memory → reminder → notes
  ("Check the docs for how theme variables are resolved, save the important
  bits to memory, and schedule a reminder for tomorrow morning… Put the summary
  in notes too.").
- **New 6-station chain** (all core modules in `modules/`):
  `Docs: list` → `Docs: read` ×2 → **Memory: create** (expanded tool card,
  arg rows + raw response) → **Calendar: add event** FAILED
  (`.tool-error` + `dot-failed` + `.display-error`, "notify channel 'telegram'
  does not exist") → **Scheduler: add job** (expanded) → **Notes: create**
  (no reasoning text → the real `.bare-header` variant with
  `.tool-names/.step-args/.step-summary`, body still a tool card) → chain-end.
  Cards now include the real `.card-chevron` and a `<span>` around the action,
  matching `toolcalls.html`.
- **Final answer rewritten** to match the tools actually called (3 memory
  entries, review at 09:00, cheatsheet in notes, wrong docs folder, calendar
  fallback), so the prose and the trace agree.
- Verified on the served page: 6 stations (done/done/done/failed/done/done),
  4 tool cards (Memory/Calendar/Scheduler/Notes), chips
  `Docs: list · Docs: readx2 · Memory: create · Scheduler: add job`,
  1 `.bare-header`, 4 `.card-chevron`, 31 sections intact, no Jinja leftovers.

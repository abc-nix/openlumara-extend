/* Theme gallery — SHARED HELPERS + the part registry + the Alpine bootstrap.
   Split into parts 2026-10-25 (was one 1403-line file; see BUGS.md #21).

   All behaviour still hangs off ONE reactive object (tgState) shared by the
   sidebar panel and the overlay. Plain JS, no framework:

   1. Fetches the theme list + base.json values through the app's OWN endpoints
      (/api/themes, /api/themes/base) — the same data the real theme switcher
      uses. Nothing is ever written to settings or localStorage.
   2. Keeps a list of "slots": each slot is one pane = {family, mode}. One slot
      in single view, 2+ in split. A pane is a plain div that receives every
      theme variable as an INLINE custom property, exactly like theming.js does
      for <html> — but scoped, so the app's real theme never changes.
   3. Clones the static chat sample (see templates/chat/index.html) into each
      pane so all panes show the same markup under different variables.
   4. Split "🔗 sync": while on, the panes act as one — the main scroll
      position mirrors (ratio) and real clicks replay in the other pane
      (popups/tabs open on both sides). Mode buttons are deliberately
      per-pane (no cascade since 2026-10-25).
   5. Sidebar "Highlight element": outlines the element under the cursor in
      its own pane (+ its counterpart in split) — one checkbox owns all
      hover outlining, sync never draws boxes. (The old "Freeze hover" was retired 2026-10-25:
      its mechanism was copying an element's computed styles inline on hover;
      see BUGS.md before rebuilding it.)
   6. In split, computes a variable diff between the first two slots: which
      variables the two themes define DIFFERENTLY, and which one defines a
      variable the other leaves undefined (classic theme-bug candidate).

   WHERE THE CODE LIVES (each file is its own <script>, loaded alphabetically
   from webui/assets/js/ — but see the registry note below: order is irrelevant):
     theme_gallery.js                 helpers + registry + alpine:init bootstrap (this)
     theme_gallery_state.js           state keys, startup, queries, controls, diff
     theme_gallery_render.js          render()/_renderInner + pane chrome + swatches
     theme_gallery_categories.js      welcome/sidebar/settings/elements pane bodies + fake pill
     theme_gallery_capture.js         captureSettings() + its two wait helpers
     theme_gallery_coverage.js        computeCoverage() + the report pane
     theme_gallery_sync.js            lockdownPane() + split sync (scroll/hover/click) */

/* Mirrors theming.js apply(): base first, then theme[mode] on top, with the
   same fallback chain (requested mode → opposite mode → dark). Returns the
   flat {varName: value} map for one family+mode. */
function resolveVars(base, themeData, mode) {
    let effective = mode;
    if (themeData && !themeData[mode]) {
        const alternate = mode === 'dark' ? 'light' : 'dark';
        effective = themeData[alternate] ? alternate : 'dark';
    }
    const out = {};
    for (const [k, v] of Object.entries(base || {})) out[k] = v;
    for (const [k, v] of Object.entries((themeData && themeData[effective]) || {})) out[k] = v;
    return out;
}

/* raw vars a theme declares for one mode ({} while missing). Skips metadata
   keys like "name" that /api/themes/{family} ships alongside dark/light. */
function rawVars(themeData, mode) {
    if (!themeData || !themeData[mode]) return {};
    const out = {};
    for (const [k, v] of Object.entries(themeData[mode])) {
        if (k.startsWith('--')) out[k] = v;
    }
    return out;
}

/* Make a cloned subtree GENUINELY static by removing every Alpine directive
   BEFORE it is inserted into the live DOM.

   WHY this is needed (observed): Alpine v3 processes cloned subtrees once they
   are inserted, so a clone of the real input came back ALIVE — its x-model was
   bound to the SHARED $store.chat.user_input, so typing in the preview typed
   into the user's REAL input box (a session-breaking leak). Alpine directives
   live in attributes: x-*, @event (also x-on:), :bind (also x-bind:), inline
   on*, and Alpine's own _x_* attributes. Stripping them up front leaves plain
   HTML: styled identically by the app CSS, but with nothing for Alpine to bind. */
function stripAlpine(root) {
    const kill = /^(x-|on|@|:|_x_)/i;
    const walk = (el) => {
        if (el.nodeType !== 1) return;
        for (const a of [...el.attributes]) {
            if (kill.test(a.name)) el.removeAttribute(a.name);
        }
        for (const c of el.children) walk(c);
    };
    walk(root);
    if (root instanceof Element) root.removeAttribute('x-cloak');
    return root;
}

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

/* Sync RUNTIME props → attributes on a LIVE subtree so a clone serializes
   what the user actually sees: Alpine binds input values / checked boxes /
   selected options as PROPERTIES, and cloneNode() only copies ATTRIBUTES.
   Only setAttribute/removeAttribute — no events fired, no state changed. */
function materializeValues(root) {
    for (const el of root.querySelectorAll('input')) {
        if (el.type === 'checkbox' || el.type === 'radio') {
            if (el.checked) el.setAttribute('checked', '');
            else el.removeAttribute('checked');
        } else if (el.type !== 'file') {
            el.setAttribute('value', el.value);
        }
        if (el.disabled) el.setAttribute('disabled', '');
    }
    for (const el of root.querySelectorAll('textarea')) el.textContent = el.value;
    for (const el of root.querySelectorAll('select')) {
        for (const o of el.options) {
            if (o.selected) o.setAttribute('selected', '');
            else o.removeAttribute('selected');
        }
    }
}

/* Clone a LIVE (Alpine-mounted) subtree for static reuse: materialize
   runtime values → clone → stripAlpine → serialize. The HTML string is
   inert by construction (all bindings stripped BEFORE it can be inserted). */
function staticClone(el) {
    materializeValues(el);
    return stripAlpine(el.cloneNode(true)).outerHTML;
}

/* ---- PART REGISTRY ------------------------------------------------------
   Every theme_gallery_<domain>.js file calls tgPart({...}) exactly ONCE, at
   parse time, with an object of methods (and, for the state part, the state
   keys). This file merges them all and wraps the result in ONE reactive
   object.

   FILE ORDER THEREFORE DOES NOT MATTER: every module script has executed by
   the time DOMContentLoaded fires, and Alpine starts (and dispatches
   'alpine:init') on DOMContentLoaded. That is also why there are no numbered
   filenames — the order contract is the boot phase, not the sort order.

   NEVER set window.tgState anywhere else: exactly one reactive proxy may
   exist, because tgState is the identity everything binds to. */
window.tgParts = [];

function tgPart(part) {
    window.tgParts.push(part);
}

document.addEventListener('alpine:init', () => {

    // CRITICAL ORDERING FIX: the sample replica also has id="messages" (so the
    // app's own "#chat-window #messages" rules style it). The below_header slot
    // puts the overlay BEFORE the real #messages in the DOM, and core code does
    // document.getElementById('messages') (ui.js scroll/lazy-mount), which
    // returns the FIRST match. So we move the whole overlay to the END of
    // #chat-window, before Alpine starts processing the page. The cover is
    // position:absolute inset:0, so its position inside #chat-window makes no
    // visual difference — only the id-lookup order matters.
    const cover = document.querySelector('.tg-cover');
    const chatWindow = document.getElementById('chat-window');
    if (cover && chatWindow) chatWindow.appendChild(cover);

    // NOTE the two-step: Alpine.reactive() returns a PROXY; DOM bindings watch
    // THAT, not the raw object. `window.tgState = Alpine.reactive({...})` alone
    // would leave window.tgState pointing at the raw target — mutations made
    // through it (e.g. new arrays in toggleSplit) would update values but never
    // trigger x-show/x-for re-renders. So we assign the proxy afterwards.
    const tgRaw = {};
    for (const part of window.tgParts) Object.assign(tgRaw, part);
    window.tgState = Alpine.reactive(tgRaw);
});

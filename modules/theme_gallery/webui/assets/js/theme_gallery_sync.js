/* theme_gallery — LOCKDOWN + SPLIT SYNC (scroll/hover/click mirroring).
   Registers onto tgRaw via tgPart(); see theme_gallery.js for the merge. */
tgPart({

    // Shared lockdown for every CLONE category (welcome + settings): the
    // pane is a picture, not a cockpit. Buttons (send/export/clear/
    // settings/upload/restart/save), native pickers and app shortcuts must
    // NEVER do anything. A CAPTURE click listener swallows every click
    // that isn't one of our own controls, the fake pill, or an element
    // explicitly marked .tg-allow (settings tabs + form fields — their
    // bindings are stripped, so interacting only flips pixels).
    // preventDefault also stops NATIVE defaults (file-picker, links).
    // Hover is deliberately untouched (CSS :hover must survive; the 🔗
    // sync hover-mirror binds its own delegated listeners below).
    lockdownPane(pane) {
        pane.addEventListener('click', (e) => {
            if (e.target.closest('.tg-pane-label')) return;   // our controls stay live
            if (e.target.closest('#context-pill')) return;    // fake pill is interactive
            if (e.target.closest('.tg-allow')) return;        // inert-by-construction widgets
            if (e.target.closest('.tg-el-coverview-refresh')) return; // coverage's own button
            e.preventDefault();
            e.stopPropagation();
        }, true);
        // text fields: TYPING is allowed (pure local DOM — no Alpine is
        // attached, so Enter/blur/send paths simply don't exist). We
        // re-implement the app's autosize so the box grows like the real
        // one, and we mirror its blur height (48px).
        pane.querySelectorAll('textarea').forEach(t => {
            t.removeAttribute('readonly');
            t.addEventListener('input', () => {
                t.style.height = '48px';
                t.style.height = Math.min(t.scrollHeight, 240) + 'px';
            });
            t.addEventListener('blur', () => { if (!t.value) t.style.height = '48px'; });
        });
        // paste & keystrokes must not leak to the app's WINDOW-level
        // handlers (Ctrl+V image-upload, global shortcuts, Ctrl+S…).
        // stopPropagation keeps the event off window/document while the
        // NATIVE default (text insertion) still happens in the textarea.
        // Escape passes through so Esc-close keeps working.
        const keepKeysOffApp = (e) => { if (e.key !== 'Escape') e.stopPropagation(); };
        pane.addEventListener('keydown', keepKeysOffApp, true);
        pane.addEventListener('keyup', keepKeysOffApp, true);
        pane.addEventListener('paste', (e) => e.stopPropagation(), true);
        // CRITICAL (observed leak): the real <textarea> is x-model-bound to
        // the shared $store.chat.user_input, and the app has a delegated
        // listener ABOVE the pane that writes e.target.value into that
        // store. A bubbling `input` from the CLONE therefore typed into the
        // REAL input box. We stop `input` bubbling AT the pane (bubble
        // phase, so the textarea's own autosize handler in the target phase
        // already ran) — the store never sees a clone keystroke.
        pane.addEventListener('input', (e) => e.stopPropagation());
        pane.querySelectorAll('[contenteditable]').forEach(el => el.removeAttribute('contenteditable'));
        // file inputs (upload): disable outright — belt AND suspenders
        pane.querySelectorAll('input[type="file"]').forEach(i => { i.disabled = true; });
        // every category path ends here → mirror listeners ride along with
        // each rebuild (they die with the pane node, so no stale refs)
        this.bindPaneMirror(pane);
    },

    // ---------------- split "sync" mode (🔗 button) ----------------
    // While linked && split the two panes act as one:
    //  · SCROLL: the pane's MAIN scroller (biggest genuinely scrollable
    //    descendant — inner boxes like the reasoning sbox stay local)
    //    mirrors position by RATIO (light/dark islands can differ a few
    //    px in content height, absolute px sync would drift).
    //  · (hover outlines are NOT part of sync — see "Highlight element"
    //    below. User decision 2026-10-25: the sidebar checkbox owns ALL
    //    outlining; sync itself never draws boxes.)
    //  · CLICK: clicks that actually DO something locally (fake pill,
    //    settings/sidebar tab switchers — everything else is swallowed in
    //    the lockdown's CAPTURE phase) replay on the counterpart, so
    //    popups/tabs open on both sides. Never form fields (typing stays
    //    local) and never the pane chrome strip (mode buttons are per-pane
    //    on purpose — the old linked mode-cascade was removed, see setSlotMode).
    // INDEPENDENT of `linked`: if tgState.highlight (sidebar "Highlight") is
    // on, the element under the cursor is ALSO outlined in its own pane —
    // works in single view too, no counterpart needed.
    // If pane-local state diverged (different settings tab per pane), the
    // node counts differ → hover/click mapping skips by design; scroll
    // mirror keeps working.
    // Bindings live ON the pane → they die cleanly when render() rebuilds.
    bindPaneMirror(pane) {
        const others = () => [...document.querySelectorAll('.tg-panes .tg-pane')]
            .filter(p => p !== pane && document.contains(p));
        const on = () => this.linked && this.slots.length > 1;
        // perf 2026-10-25: both of these used to rebuild arrays and read
        // getComputedStyle on EVERY scroll event / hover frame. The main
        // scroller is now cached per pane (invalidated on tab switches), and
        // the DOM-order element list is built once per pane.
        const mainScroller = p => this._paneScroller(p);
        const flat = p => p._flat || (p._flat = [...p.querySelectorAll('*')]);
        const mapTo = (from, el) => {
            const o = others()[0];
            if (!o) return null;
            const a = flat(from), b = flat(o);
            const idx = a.indexOf(el);
            if (idx < 0 || b.length !== a.length) return null;   // trees diverged → no map
            return b[idx] || null;
        };

        // ---- scroll mirror (capture: scroll events don't bubble) ----
        pane.addEventListener('scroll', (e) => {
            const src = e.target;
            if (!on() || src === pane || src !== mainScroller(pane)) return;
            if (src._syncTs && Date.now() - src._syncTs < 120) return;   // echo guard
            const m = src.scrollHeight - src.clientHeight;
            const r = m > 0 ? src.scrollTop / m : 0;
            for (const o of others()) {
                const t = mainScroller(o);
                if (!t) continue;
                t._syncTs = Date.now();
                t.scrollTop = Math.round(r * (t.scrollHeight - t.clientHeight));
            }
        }, true);

        // ---- hover outlines: owned ENTIRELY by tgState.highlight ("Highlight
        // element" checkbox). OFF = no boxes anywhere, sync included.
        // ON = the element under the cursor is outlined in its own pane;
        // in split (sync on or not) its COUNTERPART is outlined too, so
        // you can see where the same element lives in the other theme. ----
        let raf = 0, last = null;
        pane.addEventListener('mouseover', (e) => {
            if (!this.highlight) return;
            const src = e.target;
            if (src === last) return;
            last = src;
            if (raf) return;                       // coalesce to one map/frame
            raf = requestAnimationFrame(() => {
                raf = 0;
                this._xhlClear(pane);
                // outline the element the cursor is ON, in its own pane
                if (pane.contains(last)) last.classList.add('tg-xhl');
                // split → outline its COUNTERPART in the other pane too
                const t = this.slots.length > 1 ? mapTo(pane, last) : null;
                if (!t) return;
                pane._xhlOut = t;
                t.classList.add('tg-xhl');
            });
        });
        pane.addEventListener('mouseleave', () => this._xhlClear(pane));

        // ---- click mirror (bubble: capture-lockdown already swallowed
        // anything inert, so only locally-effective clicks get here) ----
        pane.addEventListener('click', (e) => {
            if (!on() || !e.isTrusted) return;
            if (e.target.closest('.tg-pane-label')) return;          // chrome: local only
            if (e.target.closest('.tg-error-note, .tg-warn-note')) return; // A6 notes: never mirrored (retry would fire twice)
            if (e.target.closest('input, textarea, select')) return; // typing: local only
            const t = mapTo(pane, e.target);
            if (t) t.click();    // counterpart's own handler runs; its mirror
                                 // listener ignores the synthetic (untrusted) event
        });
    },

    // ---------------- cached main scroller (perf 2026-10-25) ----------------
    // The scan is ~35 ms on a settings pane (5.4k nodes × getComputedStyle) and
    // used to run on every scroll event, for the source pane AND each
    // counterpart. Cached per pane; invalidated when pane-local content
    // changes (settings tab switch, sidebar tab switch, rebuild).
    _paneScroller(p) {
        if (p._scrollerDirty) {
            p._scroller = undefined;
            p._scrollerDirty = false;
        }
        if (p._scroller !== undefined) return p._scroller;
        let best = null;
        for (const el of p.querySelectorAll('*')) {
            const cs = getComputedStyle(el);
            if ((cs.overflowY === 'auto' || cs.overflowY === 'scroll')
                && el.scrollHeight > el.clientHeight + 8
                && (!best || el.scrollHeight > best.scrollHeight)) best = el;
        }
        p._scroller = best;
        return best;
    },

    _invalidateScroller(pane) {
        if (pane) pane._scrollerDirty = true;
    },

    // remove this pane's outgoing outline AND any incoming one
    _xhlClear(pane) {
        if (pane._xhlOut) { pane._xhlOut.classList.remove('tg-xhl'); pane._xhlOut = null; }
        for (const el of pane.querySelectorAll('.tg-xhl')) el.classList.remove('tg-xhl');
    },
});

/* theme_gallery — PANE RENDERING (serialized render, pane chrome, swatches).
   Registers onto tgRaw via tgPart(); see theme_gallery.js for the merge. */
tgPart({

    // perf bookkeeping (2026-10-25): structure fingerprint + invalidation flag
    // used by the fast path in _renderInner (var-only updates skip the rebuild)
    _rebuildNeeded: false,
    _structKey: '',

    // ---------------- pane rendering ----------------
    // Renders are SERIALIZED: two overlapping renders race on host.innerHTML
    // and can wipe each other's panes (observed). Every call now returns a
    // promise that resolves when the pass IT CAUSED has finished:
    //   · idle → the pass starts now, its promise is returned
    //   · busy → calls coalesce into ONE deferred pass (no queue buildup)
    //            and share its promise, so `await render()` really means
    //            "the DOM now reflects the state". (Before, a busy call
    //            returned undefined and awaiting it resolved immediately —
    //            measurements/harnesses read a half-rendered DOM.)
    // NOTE: _renderPromise is reassigned by EVERY pass, including deferred
    // ones — chaining off a stale promise would start the next pass while
    // the current one is still running (race). A throw inside _renderInner
    // is logged here rather than surfacing as an unhandled rejection: the
    // busy flag must clear either way and internal callers never await.
    render() {
        const pass = () => {
            this._renderBusy = true;
            this._renderPromise = this._renderInner()
                .catch(e => {
                    console.error('[theme_gallery] render failed:', e);
                    // A6: a throw AFTER host.innerHTML='' used to leave
                    // panes:0 with no explanation (the BUGS #16 harvest
                    // landmine caused exactly that). Put the reason in the
                    // pane host so it is visible, not console-only.
                    const host = document.querySelector('.tg-panes');
                    if (host && !host.querySelector('.tg-error-note')) {
                        const n = document.createElement('div');
                        n.className = 'tg-note tg-error-note';
                        n.textContent = 'Render failed: ' + ((e && e.message) ? e.message : String(e));
                        host.appendChild(n);
                    }
                })
                .finally(() => { this._renderBusy = false; });
            return this._renderPromise;
        };
        if (!this._renderBusy) return pass();
        if (!this._queued) {
            this._queued = (this._renderPromise || Promise.resolve()).then(() => {
                this._queued = null;
                return pass();
            });
        }
        return this._queued;
    },

    async _renderInner() {
        const host = document.querySelector('.tg-panes');
        if (!host) return;

        // NOTE (perf 2026-10-25): the host is no longer cleared up front.
        // Two reasons: the fast path below must be able to SKIP the rebuild
        // without a visible blank frame, and a family fetch failure would
        // otherwise leave an empty host.
        // all slot families before building anything (cached, deduped)
        await Promise.all(this.slots.map(s => this.loadFamily(s.family)));

        // NOTE: iterate/index BY NUMBER. Alpine.reactive proxies nested
        // objects on access, so `for (const slot of this.slots)` hands out
        // fresh proxies whose identity !== this.slots[i] — indexOf() would
        // return -1. _i is the stable position, stored on the raw object.
        // State is re-read AFTER the await (latest values) and the DOM build
        // below is synchronous, so a late mutation can never tear a render.
        const plan = [];
        for (let i = 0; i < this.slots.length; i++) {
            const s = this.slots[i];
            const snap = {family: s.family, mode: s.mode, vars: null};
            s._i = i; snap._i = i;
            snap.vars = resolveVars(this.base, this.cache[snap.family], snap.mode);
            plan.push(snap);
        }

        // ---------- FAST PATH (perf 2026-10-25) ----------
        // Theme/mode/family/swap changes only rewrite custom properties: the
        // pane markup is identical (all panes show the same category body),
        // so re-cloning it was pure waste — for Settings that meant cloning
        // 12 captured modals (~7.6 MB of HTML, 346 ms measured) on every
        // mode press. Structure changes (category, pane count, a capture
        // landing, coverage re-measure) set _rebuildNeeded and fall through.
        const structKey = this.category + '|' + plan.length;
        if (!this._rebuildNeeded && this._structKey === structKey
            && host.querySelectorAll(':scope > .tg-pane').length === plan.length) {
            this._applyToPanes(plan, host);
            this.buildDiff();
            Alpine.nextTick(() => {
                document.querySelectorAll('.tg-select').forEach(sel => {
                    const i = Number(sel.dataset.slot || 0);
                    if (this.slots[i]) sel.value = this.slots[i].family;
                });
            });
            return;
        }

        // ---------- FULL REBUILD ----------
        this._rebuildNeeded = false;
        this._structKey = structKey;

        // destroy Alpine components that may live in the old panes
        // (initTree'd model switchers in the welcome clone) BEFORE wiping them
        if (window.Alpine && Alpine.destroyTree) {
            host.querySelectorAll('.tg-pane').forEach(p => Alpine.destroyTree(p));
        }
        host.innerHTML = '';

        for (const slot of plan) {
            const pane = document.createElement('div');
            pane.className = 'tg-pane';
            pane.appendChild(this.buildPaneChrome(slot));
            this.buildPaneBody(pane, slot);
            host.appendChild(pane);

            // write the theme onto THIS pane only (inline, scoped)
            for (const [k, v] of Object.entries(slot.vars)) pane.style.setProperty(k, v);
            // ...then the user-runtime values theming.js layers on <html>
            this.applyRuntimeVars(pane, slot);

            // elements category: swatches show THIS pane's resolved vars,
            // so they must be filled AFTER the theme lands on the pane
            if (this.category === 'elements') this.fillSwatches(pane);
        }

        this.buildDiff();

        // keep every family <select> in sync with the state: a <select>
        // only accepts a value once its <option>s exist, so we set it here
        // (all state changes funnel through render, options exist by now)
        Alpine.nextTick(() => {
            document.querySelectorAll('.tg-select').forEach(sel => {
                const i = Number(sel.dataset.slot || 0);
                if (this.slots[i]) sel.value = this.slots[i].family;
            });
        });
    },

    // ---------------- fast path: apply state to EXISTING panes ----------------
    // Only custom properties + pane chrome change when the structure is the
    // same (mode, family, swap). Vars are tracked per pane so a key the new
    // theme does NOT define is removed — otherwise switching from a theme
    // that sets --x to one that doesn't would keep the stale inline value
    // (that is exactly the leak the diff drawer hunts for).
    _applyToPanes(plan, host) {
        // scope to the REAL host: a document-wide '.tg-panes > .tg-pane'
        // lookup silently picks up any other gallery instance first (caught
        // by a fixture test where the live pane received the writes).
        const root = host || document.querySelector('.tg-panes');
        if (!root) return;
        const panes = [...root.querySelectorAll(':scope > .tg-pane')];
        for (let i = 0; i < plan.length; i++) {
            const pane = panes[i], slot = plan[i];
            const next = new Set(Object.keys(slot.vars));
            if (pane._varKeys) {
                for (const k of pane._varKeys) if (!next.has(k)) pane.style.removeProperty(k);
            }
            pane._varKeys = next;
            for (const [k, v] of Object.entries(slot.vars)) pane.style.setProperty(k, v);
            this.applyRuntimeVars(pane, slot);
            this.updatePaneChrome(pane, slot, i);
            if (this.category === 'elements') this.fillSwatches(pane);
        }
    },

    // Text / control state of the label strip without rebuilding it.
    updatePaneChrome(pane, slot, i) {
        const label = pane.querySelector(':scope > .tg-pane-label');
        if (!label) return;
        if (this.slots.length === 1) {
            label.textContent = slot.family + ' · ' + slot.mode
                + (this.failedFamilies.includes(slot.family) ? ' · ⚠ theme JSON failed' : '');
            return;
        }
        const sel = label.querySelector('select.tg-mini-select');
        if (sel) {
            sel.dataset.slot = String(i);
            sel.value = slot.family;
        }
        const info = this.familyInfo(slot.family);
        label.querySelectorAll('.tg-mini-mode-btn').forEach((btn, idx) => {
            const mode = idx === 0 ? 'dark' : 'light';
            btn.disabled = !(info && info[mode]);
            btn.classList.toggle('active', slot.mode === mode && !btn.disabled);
        });
    },

    /* ---- user-runtime vars (the part of theming.js that is NOT theme data)
       theming.js apply() writes, in this order: base.json → the user's font
       (setFont) → font size / chat width / message width / token fade →
       theme[mode]. Those runtime values live inline on <html>, so a pane
       that only got base+theme keeps base.json's --font-primary (Arial):
       every rule saying `font-family: var(--font-primary)` then renders in
       Arial while everything else inherits the user's real font — two
       fonts inside one preview (measured: .settings-modal, sidebar
       .tag-chip). Copy the root's LIVE value for each runtime var, except
       where the previewed family declares it itself (a theme beats the
       user font in apply() because theme vars are written last). */
    applyRuntimeVars(pane, slot) {
        const rootStyle = document.documentElement.style;
        if (!rootStyle || typeof rootStyle.length !== 'number') return;
        const data = this.cache[slot.family] || {};
        for (const k of this.RUNTIME_VARS) {
            const themed = ['dark', 'light'].some(m => data[m] && data[m][k] !== undefined);
            if (themed) continue;              // theme's own value already on the pane
            const value = rootStyle.getPropertyValue(k);
            if (value) pane.style.setProperty(k, value);
        }
    },

    // the pane's content for the current category.
    //   'chat'    → the handcrafted static sample (template)
    //   'welcome' → a CLONE of the real app DOM, so it always matches
    //               whatever the user actually sees (config, module header
    //               slots, module welcome content) — never hardcoded
    buildPaneBody(pane, slot) {
        // ---- A6 visible failure states (2026-10-25) ----
        // 1) boot failed: the pane is the message, with a retry button.
        if (this.loadError) {
            this._paneNote(pane, this.loadError, this.loadErrorDetail, () => this.retryThemes());
            return;
        }
        // 2) list came back empty: non-blocking, preview base.json only.
        if (this.loadWarning) this._paneWarn(pane, this.loadWarning);
        // 3) THIS family's JSON failed: preview base+fallback, but say so —
        //    a half-themed pane must not read as a theme bug.
        if (slot && this.failedFamilies.includes(slot.family)) {
            this._paneWarn(pane, 'Theme JSON for "' + slot.family + '" failed to load — this pane shows base.json values only.');
        }

        if (this.category === 'welcome') {
            this.buildWelcomeBody(pane);
            return;
        }
        if (this.category === 'settings') {
            this.buildSettingsBody(pane);
            return;
        }
        if (this.category === 'sidebar') {
            this.buildSidebarBody(pane);
            return;
        }
        if (this.category === 'elements') {
            this.buildElementsBody(pane);
            return;
        }
        if (this.category === 'coverage') {
            this.buildCoverageBody(pane);
            return;
        }
        const sample = document.getElementById('tg-sample-chat');
        if (sample) pane.appendChild(sample.content.cloneNode(true));
        // FIX 2026-10-25: the chat sample is v1 code that predates
        // lockdownPane → it never got lockdown, key-leak protection, or
        // the split-sync mirror. The sample is fully static markup, so
        // lockdown adds only protection + textarea care + mirror binding.
        pane.querySelectorAll('input, select, textarea').forEach(el => {
            if (el.type === 'file') { el.disabled = true; return; }
            el.classList.add('tg-allow');
        });
        this.lockdownPane(pane);
    },

    // the strip on top of each pane: static label in single view, family
    // dropdown + 🌙/☀️ per pane in split (so each side is tunable directly)
    buildPaneChrome(slot) {
        const label = document.createElement('div');
        label.className = 'tg-pane-label';
        if (this.slots.length === 1) {
            label.textContent = slot.family + ' · ' + slot.mode
                + (this.failedFamilies.includes(slot.family) ? ' · ⚠ theme JSON failed' : '');
            return label;
        }
        const wrap = document.createElement('div');
        wrap.className = 'tg-pane-mini-controls';

        const sel = document.createElement('select');
        sel.className = 'tg-select tg-mini-select';
        sel.dataset.slot = String(slot._i);   // stable index (see render note)
        for (const g of this.groups()) {
            const og = document.createElement('optgroup');
            og.label = g.name;
            for (const t of g.themes) {
                const opt = document.createElement('option');
                opt.value = t.name;
                opt.textContent = t.label;
                og.appendChild(opt);
            }
            sel.appendChild(og);
        }
        wrap.appendChild(sel);

        for (const mode of ['dark', 'light']) {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'tg-mode-btn tg-mini-mode-btn';
            btn.title = mode + ' mode';
            btn.innerHTML = mode === 'dark' ? '&#127769;' : '&#9728;&#65039;';
            const info = this.familyInfo(slot.family);
            if (!(info && info[mode])) btn.disabled = true;
            else if (slot.mode === mode) btn.classList.add('active');
            btn.addEventListener('click', () => this.setSlotMode(slot._i, mode));
            wrap.appendChild(btn);
        }

        label.appendChild(wrap);
        return label;
    },

    // delegated events for the mini-controls built above (elements are
    // recreated on every render, so we listen once on the host)
    installPaneControlEvents() {
        const host = document.querySelector('.tg-panes');
        if (!host || this._paneEvents) return;
        this._paneEvents = true;
        host.addEventListener('change', (e) => {
            const sel = e.target.closest('.tg-mini-select');
            if (!sel) return;
            this.setSlotFamily(Number(sel.dataset.slot || 0), sel.value);
        });
    },

    fillSwatches(pane) {
        for (const box of pane.querySelectorAll('.tg-el-swatches')) {
            box.innerHTML = '';
            // Object.keys(pane.style) does NOT enumerate custom properties set via
// setProperty() → parse the inline cssText for --names instead.
const keys = [...new Set((pane.style.cssText.match(/--[\w-]+(?=\s*:)/g) || []))].sort();
            const cs = getComputedStyle(pane);
            for (const k of keys) {
                const value = cs.getPropertyValue(k).trim();
                const sw = document.createElement('div');
                sw.className = 'tg-swatch';
                const chip = document.createElement('span');
                chip.className = 'tg-swatch-chip';
                // chip is only meaningful for color-ish values; everything
                // else (radii, shadows, widths) renders its own demo box
                chip.style.background = value || 'transparent';
                const name = document.createElement('span');
                name.className = 'tg-swatch-name';
                name.textContent = k;
                const val = document.createElement('span');
                val.className = 'tg-swatch-val';
                val.textContent = value.length > 34 ? value.slice(0, 33) + '…' : value || '(empty)';
                sw.append(chip, name, val);
                sw.title = k + ': ' + value;
                box.appendChild(sw);
            }
        }
    },

    // ---------------- A6 visible failure notes ----------------
    // Blocking note that REPLACES a pane body: title + optional exception
    // detail + optional retry. Carries .tg-allow so the lockdown's capture
    // click listener lets the retry button through.
    _paneNote(pane, text, detail, retry) {
        const note = document.createElement('div');
        note.className = 'tg-note tg-error-note tg-allow';
        const title = document.createElement('div');
        title.className = 'tg-error-title';
        title.textContent = text;
        note.appendChild(title);
        if (detail) {
            const sub = document.createElement('div');
            sub.className = 'tg-error-detail';
            sub.textContent = detail;
            note.appendChild(sub);
        }
        if (retry) {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'tg-retry-btn';
            btn.textContent = '↻ retry';
            btn.addEventListener('click', () => {
                try { retry(); } catch (e) { console.error('[theme_gallery] retry failed:', e); }
            });
            note.appendChild(btn);
        }
        pane.appendChild(note);
        return note;
    },

    // Non-blocking strip above the pane content (empty list, failed family).
    _paneWarn(pane, text) {
        const warn = document.createElement('div');
        warn.className = 'tg-warn-note';
        warn.textContent = text;
        pane.appendChild(warn);
        return warn;
    },
});

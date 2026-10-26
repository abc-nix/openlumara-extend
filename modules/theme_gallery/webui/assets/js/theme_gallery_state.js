/* theme_gallery — STATE, STARTUP, QUERIES, CONTROLS, DIFF.
   Registers onto tgRaw via tgPart(); theme_gallery.js wraps the merged object
   with Alpine.reactive() at alpine:init. File order does not matter. */
tgPart({

    // ---------------- state ----------------
    themes: [],            // [{name, dark, light}] from /api/themes
    base: {},              // base.json flat dict
    cache: {},             // family -> theme JSON ({} while loading)
    // ---- VISIBLE FAILURE STATE (A6, 2026-10-25) ----
    // Before this, every load failure ended in console.error only: an empty
    // pane, an empty dropdown and no hint WHY. Now the pane says it.
    loadError: '',         // blocking: the theme list itself could not be fetched
    loadErrorDetail: '',   // the exception text, shown small under the message
    loadWarning: '',       // non-blocking: list came back empty → base-only preview
    failedFamilies: [],    // family JSONs that failed → base-only preview + per-pane note
    slots: [{family: '', mode: 'dark'}],   // one entry per pane
    linked: true,          // 🔗 sync (split): scroll/hover/click mirrored (NOT modes)
    highlight: false,      // sidebar "Highlight": outline the hovered element itself
    diffOpen: false,
    diffRaw: [],           // [{name, a, b, ea, eb, missing}] first two slots
    diffRawCount: 0,       // # vars defined differently by the two themes
    diffMissingCount: 0,   //   ... of those: defined by one, not the other
    diffEffCount: 0,       // # vars that RENDER differently (base included)
    category: 'chat',      // surface the panes show (chat is the default sample)
    _coverageReport: null,   // memoized computeCoverage() (this page-load)
    // user-runtime vars theming.js writes inline on <html> (font family,
    // font size, chat/message width, token fade). NOT theme data, so a
    // pane must mirror them — see applyRuntimeVars(). --font-primary is
    // listed too because base.json ALSO defines it: without the mirror the
    // pane keeps base's Arial while the real app uses the user's font.
    RUNTIME_VARS: ['--font-primary', '--font-size-base', '--chat-content-width',
                   '--message-max-width', '--token-fade-duration'],

    // ---------------- startup ----------------
    // A6: a failed boot must still RENDER. The pane then shows the reason and
    // a retry button instead of staying blank with a console line nobody sees.
    async init() {
        // (the DOM move happens earlier, in the alpine:init handler above)

        // default selection = the theme the app currently uses (read-only)
        this.slots[0].family = localStorage.getItem('themeFamily') || 'monochrome';
        this.slots[0].mode = localStorage.getItem('themeMode') || 'dark';
        this.loadError = '';
        this.loadErrorDetail = '';
        this.loadWarning = '';
        // perf: boot (or a retry) changes what the pane shows → never the fast path
        this._rebuildNeeded = true;

        try {
            const [themes, base] = await Promise.all([
                fetch('/api/themes').then(r => {
                    if (!r.ok) throw new Error('/api/themes → HTTP ' + r.status);
                    return r.json();
                }),
                fetch('/api/themes/base').then(r => {
                    if (!r.ok) throw new Error('/api/themes/base → HTTP ' + r.status);
                    return r.json();
                }),
            ]);
            this.themes = themes || [];
            this.base = base || {};
            if (!this.themes.length) {
                this.loadWarning = 'No themes were returned by /api/themes — previewing base.json values only.';
            }
            await this.loadFamily(this.slots[0].family);
            // if the saved family can't do the saved mode, snap to a supported one
            if (!this.slotSupports(0, this.slots[0].mode)) {
                this.slots[0].mode = this.slotSupports(0, 'dark') ? 'dark' : 'light';
            }
        } catch (e) {
            console.error('[theme_gallery] failed to load themes:', e);
            this.loadError = 'Could not load the theme list from the app. The gallery has nothing to preview.';
            this.loadErrorDetail = (e && e.message) ? e.message : String(e);
        }

        // ALWAYS render: the failure state lives in the DOM (A6).
        this.render();
        this.installPaneControlEvents();

        // Esc closes the overlay, same habit as the modals.
        // Guarded: retryThemes() calls init() again and must not stack listeners.
        if (!this._escBound) {
            this._escBound = true;
            window.addEventListener('keydown', (e) => {
                if (e.key === 'Escape' && document.body.hasAttribute('data-tg')) {
                    if (this.diffOpen) this.diffOpen = false;   // Esc closes drawer first
                    else this.closeOverlay();
                }
            });
        }
    },

    // A6: retry after a failed boot. Clears every cached failure first, so a
    // server that came back up is not masked by the empty cache.
    async retryThemes() {
        this.loadError = '';
        this.loadErrorDetail = '';
        this.loadWarning = '';
        this.themes = [];
        this.cache = {};
        this.failedFamilies = [];
        await this.init();
    },

    async loadFamily(family) {
        if (this.cache[family]) return this.cache[family];
        try {
            const r = await fetch('/api/themes/' + encodeURIComponent(family));
            if (!r.ok) throw new Error('HTTP ' + r.status);
            this.cache[family] = await r.json();
            // A6: a family this gallery used to fail on is no longer failed
            if (this.failedFamilies.includes(family)) {
                this.failedFamilies = this.failedFamilies.filter(f => f !== family);
                this._rebuildNeeded = true;   // perf: drop the "base only" warn strip
            }
        } catch (e) {
            console.error('[theme_gallery] failed to load theme', family, e);
            this.cache[family] = {};
            // A6: mark it, so the pane says "base only" instead of silently
            // rendering a half-themed preview that looks like a theme bug.
            if (!this.failedFamilies.includes(family))
                this.failedFamilies = [...this.failedFamilies, family];
            this._rebuildNeeded = true;   // perf: the pane needs the "base only" warn strip
        }
        return this.cache[family];
    },

    // ---------------- queries ----------------
    familyInfo(name) {
        return this.themes.find(t => t.name === name) || null;
    },
    currentInfo() {
        return this.familyInfo(this.slots[0].family);
    },
    slotSupports(i, mode) {
        const info = this.familyInfo(this.slots[i].family);
        return info ? !!info[mode] : false;
    },
    currentSupports(mode) {
        return this.slotSupports(0, mode);
    },
    hasBoth() {
        const info = this.currentInfo();
        return info ? !!(info.dark && info.light) : false;
    },
    // sidebar status line: what the panes show, in words
    modeSummary() {
        if (this.slots.length === 1) {
            return this.slots[0].family + ' · ' + this.slots[0].mode;
        }
        const s = this.slots;
        if (s[0].family === s[1].family) {
            return s[0].family + ' · ' + s.map(x => x.mode).join(' | ');
        }
        return s.map(x => x.family + ' · ' + x.mode).join('  vs  ');
    },

    // dropdown groups, same rule as the settings appearance dropdown
    // (theming.js getFamilies + settings.js formatThemeName): the word
    // before the first hyphen is the group, the rest is the display name.
    groups() {
        const map = {};
        const others = [];
        for (const t of this.themes) {
            const parts = t.name.split('-');
            const rest = parts.length > 1 ? parts.slice(1).join(' ') : parts[0];
            const label = rest.charAt(0).toUpperCase() + rest.slice(1);
            const entry = { name: t.name, label: label, dark: t.dark, light: t.light };
            if (parts.length > 1) {
                const key = parts[0].toLowerCase();
                if (!map[key]) map[key] = [];
                map[key].push(entry);
            } else {
                others.push(entry);
            }
        }
        if (others.length) {
            others.sort((a, b) => a.name.localeCompare(b.name));
        }
        const out = Object.keys(map).sort().map(k => ({
            name: k.charAt(0).toUpperCase() + k.slice(1),
            themes: map[k],
        }));
        if (others.length) out.push({ name: 'Other', themes: others });
        return out;
    },

    // ---------------- slot helpers ----------------
    // keep a slot's mode valid for its family (snap if unsupported)
    snapMode(i) {
        const s = this.slots[i];
        if (!this.slotSupports(i, s.mode)) {
            s.mode = this.slotSupports(i, 'dark') ? 'dark' : 'light';
        }
    },

    // ---------------- controls ----------------
    // category = which SURFACE the panes show. 'chat' is our handcrafted
    // static sample; 'welcome' clones the REAL app DOM (top bar + welcome
    // panel + input bar), so it inherits the user's config, header module
    // slots and welcome-slot modules automatically — never hardcoded.
    setCategory(cat) {
        if (this.category === cat) return;
        this.category = cat;
        // 'settings': the first visit mounts the REAL modal invisibly
        // (#modals set to display:none → zero paint) to capture every
        // tab's rendered HTML once — see captureSettings(). Until the
        // capture lands, the pane shows a "preparing" note, then we
        // re-render with the cache.
        if (cat === 'settings' && this._settingsCapture === undefined && !this._settingsCapturing) {
            this._settingsCapturing = true;
            this.captureSettings().then(cap => {
                this._settingsCapture = cap || false;
                this._settingsCapturing = false;
                this._rebuildNeeded = true;   // perf: capture landing must rebuild the bod
                // the capture is a coverage SOURCE (12 mounted tabs): the cached
                // report was measured without it → drop it so the numbers (and
                // the "settings capture: yes/no" note) deepen on next view.
                this._coverageReport = null;
                if (this.category === 'settings') this.render();
            });
        }
        this.render();
    },

    // single-view dropdown: drives slot 0 (and stays the "main" family
    // while split, so the bar and pane A never disagree)
    async setFamily(name) {
        this.slots[0].family = name;
        await this.loadFamily(name);
        this.snapMode(0);
        if (this.slots.length > 1) this.snapMode(1);
        this.render();
    },
    setMode(mode) {
        if (!this.slotSupports(0, mode)) return;   // like v1: unsupported = no-op
        this.slots[0].mode = mode;
        this.render();
    },

    // "Both" exists only in single view: expands to two slots, same family
    // dark | light; collapses back to the family's dark pane. (No Both in
    // split — the panes each have their own controls there.)
    toggleBoth() {
        // collapse branch: shown exactly when slots == the Both pattern
        if (this.slots.length > 1) {
            if (this.slots[0].family === this.slots[1].family
                && this.slots[0].mode !== this.slots[1].mode) {
                this.slots = [{family: this.slots[0].family, mode: 'dark'}];
                this.snapMode(0);
                this.diffOpen = false;
                this.render();
            }
            return;
        }
        if (!this.hasBoth()) return;
        this.slots = [
            {family: this.slots[0].family, mode: 'dark'},
            {family: this.slots[0].family, mode: 'light'},
        ];
        this.diffOpen = false;
        this.render();
    },
    // Split: add a pane that mirrors what was on screen (same family, same
    // mode — e.g. light if light was showing), then you change its family.
    // If Both was up, the pair collapses to a split of light|dark instead.
    toggleSplit() {
        if (this.diffOpen) this.diffOpen = false;
        if (this.slots.length > 1) {
            const keep = this.slots[0];
            this.slots = [{family: keep.family, mode: keep.mode}];
            this.render();
            return;
        }
        const a = this.slots[0];
        let seedMode = a.mode;
        if (!this.slotSupports(0, seedMode)) seedMode = this.slotSupports(0, 'dark') ? 'dark' : 'light';
        this.slots.push({family: a.family, mode: seedMode});
        this.snapMode(1);
        this.render();
    },
    swap() {
        if (this.slots.length < 2) return;
        const a = this.slots[0];
        this.slots[0] = this.slots[1];
        this.slots[1] = a;
        this.render();
    },
    toggleLink() {
        this.linked = !this.linked;
        if (!this.linked)   // drop lingering hover mirrors
            document.querySelectorAll('.tg-xhl').forEach(el => el.classList.remove('tg-xhl'));
    },
    toggleHighlight() {
        this.highlight = !this.highlight;
        if (!this.highlight)   // drop lingering outlines
            document.querySelectorAll('.tg-xhl').forEach(el => el.classList.remove('tg-xhl'));
    },

    // per-pane controls (split mode)
    async setSlotFamily(i, name) {
        this.slots[i].family = name;
        await this.loadFamily(name);
        this.snapMode(i);
        this.render();
    },
    setSlotMode(i, mode) {
        // ALWAYS local: a pane's 🌙/☀️ button sets ONLY that pane. The old
        // "linked" cascade (one press rewrote every pane) was REMOVED per
        // user QA 2026-10-25 — it silently collapsed dark|light comparisons.
        if (!this.slotSupports(i, mode)) return;   // unsupported = no-op
        this.slots[i].mode = mode;
        this.render();
    },
    toggleDiff() {
        if (this.slots.length < 2) return;
        this.diffOpen = !this.diffOpen;
    },
    closeOverlay() {
        // same helper testbench uses: clear the shared sidebarTab state
        const shell = window.Alpine ? Alpine.$data(document.querySelector('.sidebar-shell')) : null;
        if (shell && shell.setTab) shell.setTab('');
    },

    // ---------------- diff (first two slots) ----------------
    buildDiff() {
        this.diffRaw = [];
        this.diffRawCount = 0;
        this.diffMissingCount = 0;
        this.diffEffCount = 0;
        if (this.slots.length < 2) return;
        const A = this.slots[0], B = this.slots[1];
        const rawA = rawVars(this.cache[A.family], A.mode);
        const rawB = rawVars(this.cache[B.family], B.mode);
        // computed here (not read off the slots) so the diff never depends
        // on render bookkeeping; cache is populated: buildDiff runs AFTER
        // _renderInner awaited every slot family.
        const varsA = resolveVars(this.base, this.cache[A.family], A.mode);
        const varsB = resolveVars(this.base, this.cache[B.family], B.mode);
        const all = new Set([...Object.keys(this.base), ...Object.keys(rawA), ...Object.keys(rawB)]);
        const rows = [];
        let differing = 0, missing = 0, eff = 0;
        for (const k of all) {
            // raw comparison also catches theme-vs-theme gaps; theme JSONs
            // ship non-var metadata keys (name, ...) which are not vars.
            if (!k.startsWith('--')) continue;
            const ra = rawA[k] !== undefined ? rawA[k] : null;
            const rb = rawB[k] !== undefined ? rawB[k] : null;
            if (ra !== rb) {
                differing++;
                if (ra === null || rb === null) missing++;
            }
            const ea = varsA[k] !== undefined ? varsA[k] : null;
            const eb = varsB[k] !== undefined ? varsB[k] : null;
            if (ea !== eb) eff++;
            if (ra !== rb || ea !== eb) rows.push({name: k, a: ra, b: rb, ea: ea, eb: eb});
        }
        rows.sort((x, y) => x.name.localeCompare(y.name));
        this.diffRaw = rows;
        this.diffRawCount = differing;
        this.diffMissingCount = missing;
        this.diffEffCount = eff;
    },
});

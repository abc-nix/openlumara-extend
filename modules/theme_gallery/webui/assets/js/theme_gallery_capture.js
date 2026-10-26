/* theme_gallery — SETTINGS CAPTURE (mount the real modal invisibly, serialize
   each tab once) + the two adaptive wait helpers it needs (BUGS.md #17/#10).
   Registers onto tgRaw via tgPart(); see theme_gallery.js for the merge. */
tgPart({

    // ---- settings category: capture the REAL modal once ----
    // The settings modal only exists in the DOM while it is OPEN (x-if)
    // and its fields are Alpine-generated from $store.settings — a static
    // clone of the page template would be an empty skeleton. So we MOUNT
    // the real modal inside #modals while #modals is display:none: the
    // subtree renders (layout/DOM as normal) but NEVER PAINTS → the user
    // sees no flash. For each category we run the component's own
    // switchCategory(), wait out the x-if mount + 170ms entry animation,
    // then serialize staticClone() of the fully rendered modal. finally
    // restores currentModal + display → the real modal unmounts exactly
    // as it was. Read-only: the settings store is already loaded and
    // switchCategory() is pure component-local state.
    async captureSettings() {
        const modals = document.querySelector('#modals');
        if (!modals || window.ADMIN_SETTINGS_DISABLED) return null;
        const ui = Alpine.store('ui');
        if (!ui) return null;
        const prevModal = ui.currentModal;
        const prevDisplay = modals.style.display;
        try {
            modals.style.display = 'none';
            ui.currentModal = 'settings';
            let sm = null;
            for (let i = 0; i < 60 && !sm; i++) {
                await sleep(25);
                sm = modals.querySelector('.settings-modal');
            }
            if (!sm) return null;
            // the modal MOUNTS before $store.settings finishes loading: the
            // fields live in <template x-if="!$store.settings.loading">, so a
            // capture taken too early freezes the spinner forever (static
            // HTML, never recovers) — BUGS.md #17. Wait for the store first;
            // sortedCategories below is empty until then, too.
            if (!(await this._waitFor(() => !Alpine.store('settings')?.loading, 2500))) {
                console.warn('[theme_gallery] settings store slow to load; capturing anyway');
            }
            const data = Alpine.$data(sm);
            // Tag the nav buttons with their category BEFORE stripping.
            // The buttons' @click holds the LOOP VARIABLE ("switchCategory
            // (cat)"), not a literal — but x-for over $store.settings
            // .sortedCategories renders ONE <div> child per category in
            // store order, so DOM order maps 1:1 to cats[]. (The static
            // siblings — Restart Server, logout, extension slots — are NOT
            // divs and stay untagged = blocked in the pane.)
            const cats = (Alpine.store('settings').sortedCategories || []).map(([c]) => c);
            const divs = [...sm.querySelectorAll('.nav > div')];
            divs.forEach((d, i) => {
                const b = d.querySelector(':scope > button.item');
                if (b && cats[i]) b.dataset.tgScat = cats[i];
            });
            const sections = [];
            const failed = [];
            for (const cat of cats) {
                try {
                    if (typeof data.switchCategory === 'function') await data.switchCategory(cat);
                    else data.activeCategory = cat;
                } catch (e) { continue; }
                if (!sm.isConnected) break;                    // yanked from under us
                // adaptive wait (was a blind sleep(320)): returns as soon as the
                // tab's content is mounted AND stable, retries once, and records
                // a tab that never got there instead of freezing a spinner
                let ready = await this._waitForSection(sm);
                if (!ready) ready = await this._waitForSection(sm);
                if (!sm.isConnected) break;
                sections.push({cat, html: staticClone(sm), ready});
                if (!ready) failed.push(cat);
            }
            return sections.length ? {sections, failed} : null;
        } catch (e) {
            console.warn('[theme_gallery] settings capture failed:', e);
            return null;
        } finally {
            ui.currentModal = prevModal;
            modals.style.display = prevDisplay;
        }
    },

    // ---------------- capture helpers (BUGS.md #17) ----------------
    // Small poll: true as soon as pred() is truthy, false on timeout.
    async _waitFor(pred, timeout, step = 50) {
        const end = Date.now() + timeout;
        for (;;) {
            try { if (pred()) return true; } catch (e) { /* not ready yet */ }
            if (Date.now() >= end) return false;
            await sleep(step);
        }
    },

    // A settings tab is "really mounted" when the .content div exists (it
    // lives in <template x-if="!$store.settings.loading">), the loading block
    // is hidden, and the content's innerHTML stopped changing between two
    // samples ~120ms apart. Replaces the blind sleep(320): fast when a tab
    // mounts instantly, patient when it fetches data (module_library …).
    async _waitForSection(sm, timeout = 1400) {
        let prev = -1;
        const end = Date.now() + timeout;
        while (Date.now() < end) {
            await sleep(120);
            if (!sm.isConnected) return false;
            const content = sm.querySelector('.content');
            const loading = sm.querySelector('.loading');
            const stillLoading = !!loading && getComputedStyle(loading).display !== 'none';
            const size = content ? content.innerHTML.length : 0;
            if (content && size > 0 && !stillLoading && size === prev) return true;
            prev = size;
        }
        return false;
    },
});

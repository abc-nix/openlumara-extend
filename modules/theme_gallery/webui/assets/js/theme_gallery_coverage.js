/* theme_gallery — COVERAGE REPORT (CSSOM × rendered-class union, memoized).
   Registers onto tgRaw via tgPart(); see theme_gallery.js for the merge. */
tgPart({

    // ---------------- coverage category (static-source union) ----------------
    // Which app-CSS classes are actually RENDERED by the surfaces the
    // gallery can measure statically? Sources unioned:
    //   · #tg-sample-elements (the style guide) + #tg-sample-chat
    //   · the app shell in the page (sidebar/header/input chrome, incl. the
    //     server-rendered #modals skeletons — restart/move-chat/etc.)
    //   · the settings live-clone capture IF this page-load visited
    //     Settings (its 12 mounted tabs are a huge class source)
    // NOT measurable statically (honest note in the pane): Welcome +
    // Sidebar live clones (runtime-only) and per-tool JS renderers.
    // Classifiers: theme_gallery.css (ours), hljs code themes, responsive.
    // css (media queries over counted classes) are excluded; module-owned
    // sheets and dead selectors are bucketed, never counted against core.
    computeCoverage() {
        // core component sheets (chat + chrome). combined_block/custom_tool_views
    // belong to CORE chat rendering (agent turns), not to user modules.
    // FIX 2026-10-25: module_library/api/logs/system_prompt live at
    // channels/webui/assets/css/modals/settings/ — they are CORE sheets that
    // the first version mis-bucketed as "module-owned", which silently
    // dropped ~140 core classes from the denominator.
    const CORE_SHEET = /^(base|main|animations|modal|modals|fields|sidebar|window|input|header|message_bubble|message_buttons|upload|context_pill|reasoning|toolcalls|combined_block|custom_tool_views|agent_chain|prompt_processing|typing_indicator|global_search|categories|move_chat|restart|appearance|login|code-copy|welcome|module_library|api|logs|system_prompt)\.css$/;
        const MODULE_SHEET = /^(coder|codeeditor|editor|git|docs|memories|mem|testbench|dp_vlm|browser|vim)\.css$/;
        const EXCLUDE = /theme_gallery|hljs|code-themes|responsive|normalize|github-light|\.user\.css/;
        // FIX 2026-10-25: .modal-overlay is NOT dead — modals/index.html
        // renders it for every open modal (verified in the template). Keeping
        // it here mislabeled a live element as a dead selector.
        const DEAD = new Set(['category-item', 'chat-item', 'tool-call-header',
            'confirm-modal', 'confirm-overlay']);
        const styledBy = {};                       // class -> Set(files)
        for (const ss of document.styleSheets) {
            let rules; try { rules = ss.cssRules; } catch (e) { continue; }
            const file = (ss.href || 'inline').split('/').pop().split('?')[0];
            if (EXCLUDE.test(file)) continue;
            // NATIVE CSS NESTING (found 2026-10-25): a style rule can carry
            // child rules — agent_chain.css is 349 lines but only 2 top-level
            // rules (.node-dot + .agent-chain, everything else nested inside).
            // Walking only the top level missed every nested selector, which
            // is why this report used to count 110 core classes; the recursive
            // count is 502. Walk rules AND @media/@supports alike.
            const walk = (list) => {
                for (const r of list) {
                    if (r.selectorText)
                        for (const m of r.selectorText.matchAll(/\.([A-Za-z][\w-]*)/g))
                            (styledBy[m[1]] = styledBy[m[1]] || new Set()).add(file);
                    if (r.cssRules && r.cssRules.length) walk(r.cssRules);
                }
            };
            walk(rules);
        }
        // union of RENDERED classes (never include .tg-* — that's our UI)
        const used = new Set();
        // NOTE: iterate the ROOT TOO (querySelectorAll('*') skips it —
        // cost us a false miss on .sidebar-shell its first round).
        // getAttribute-based (NOT classList iteration: SVG elements' classlist is
            // not iterable here, and the samples are svg-heavy — threw once)
            const harvest = root => {
                const els = (root.nodeType === 1 ? [root] : []).concat([...root.querySelectorAll('*')]);
                for (const el of els)
                    for (const c of (el.getAttribute('class') || '').split(/\s+/))
                        if (c && !c.startsWith('tg-')) used.add(c);
            };
        const sources = { sheet: false, sample: false, shell: false, modals: false, settings: false };
        const tEl = document.getElementById('tg-sample-elements');
        if (tEl)      { harvest(tEl.content); sources.sheet = true; }
        const tCh = document.getElementById('tg-sample-chat');
        if (tCh)      { harvest(tCh.content); sources.sample = true; }
        const shell = document.querySelector('.sidebar-shell');
        if (shell)    { harvest(shell); sources.shell = true; }
        const hdr = document.querySelector('#chat-window, header');
        if (hdr)      { harvest(hdr); sources.shell = true; }
        const mods = document.getElementById('modals');
        if (mods)     { harvest(mods); sources.modals = true; }
        if (this._settingsCapture) {
            for (const sec of this._settingsCapture.sections || []) {
                try { harvest(new DOMParser().parseFromString(sec.html, 'text/html')); sources.settings = true; }
                catch (e) { /* skip malformed fragment */ }
            }
        }
        // classify every styled class
        const byFile = {};                         // file -> {file,total,cov,rows}
        let core = 0, cov = 0;
        const misses = [], dead = [], moduleOnly = [];
        for (const cls of Object.keys(styledBy)) {
            if (cls.startsWith('tg-')) continue;
            const files = [...styledBy[cls]];
            const coreFiles = files.filter(f => CORE_SHEET.test(f) && !MODULE_SHEET.test(f));
            const isCore = coreFiles.length > 0;
            const isUsed = used.has(cls);
            if (!isCore) { if (!isUsed) moduleOnly.push(cls + ' (' + files[0] + ')'); continue; }
            core++;
            const status = isUsed ? 'cov' : (DEAD.has(cls) ? 'dead' : 'miss');
            if (status === 'cov') cov++; else if (status === 'dead') dead.push(cls);
            else misses.push(cls);
            for (const f of coreFiles) {
                const g = byFile[f] = byFile[f] || { file: f, total: 0, cov: 0, rows: [] };
                g.total++; if (status === 'cov') g.cov++;
                g.rows.push({ cls, status, moduleish: false });
            }
        }
        for (const f of Object.keys(byFile))
            byFile[f].rows.sort((a, b) => (a.status === b.status) ? a.cls.localeCompare(b.cls)
                : (a.status === 'miss' ? -1 : 1));
        return {
            ts: Date.now(), sources, core, cov, pct: Math.round(cov / core * 100),
            misses: misses.length, deadCount: dead.length,
            deadList: dead.sort(), moduleCount: moduleOnly.length,
            files: Object.values(byFile).sort((a, b) =>
                ((b.total - b.cov) / b.total) - ((a.total - a.cov) / a.total) || b.total - a.total),
        };
    },

    buildCoverageBody(pane) {
        // CACHE (see _coverageReport): one scan per page-load, reused by every
        // re-render of this category (mode/family/split changes) and by BOTH
        // panes in split — without this every render re-parsed the ~7.8 MB
        // settings capture, 170-245 ms per call (measured). Stale after a
        // settings capture (deeper sources) -> invalidated there, and the ↻
        // button nulls it explicitly.
        // A6: the harvest landmines (BUGS #16) used to throw here AFTER the
        // host was cleared, leaving panes:0 with a console-only error. Now the
        // failure has a message and a retry, inside the pane.
        let rep;
        try {
            rep = this._coverageReport || (this._coverageReport = this.computeCoverage());
        } catch (e) {
            console.error('[theme_gallery] coverage failed:', e);
            this._paneNote(pane, 'Coverage could not be measured.',
                (e && e.message) ? e.message : String(e),
                () => { this._coverageReport = null; this.render(); });
            this.lockdownPane(pane);
            return;
        }
        const sec = document.createElement('div');
        sec.className = 'tg-coverage tg-cov-wrap';
        const head = document.createElement('div');
        head.className = 'tg-cov-head';
        head.innerHTML =
            '<b>' + rep.cov + ' / ' + rep.core + '</b> core CSS classes rendered ' +
            '<span class="tg-cov-pct">' + rep.pct + '%</span>' +
            '<button type="button" class="tg-el-coverview-refresh">↻ re-measure</button>';
        const note = document.createElement('div');
        note.className = 'tg-cov-note';
        note.textContent = 'static-source union: Elements sheet + chat sample + app shell (incl. modal skeletons)'
            + ' + settings capture: ' + (rep.sources.settings ? 'yes' : 'no — open Settings once for deeper numbers')
            + '. Welcome/Sidebar are runtime clones — not measurable statically.'
            + ' · module-only classes: ' + rep.moduleCount + ' (E18) · dead selectors: ' + rep.deadCount
            + ' · measured ' + new Date(rep.ts).toLocaleTimeString();
        sec.append(head, note);
        for (const g of rep.files) {
            const t = document.createElement('div');
            t.className = 'tg-cov-file';
            t.textContent = g.file + ' — ' + g.cov + '/' + g.total
                + (g.total - g.cov ? ' (' + (g.total - g.cov) + ' missing)' : ' ✓');
            const box = document.createElement('div');
            box.className = 'tg-cov-classes';
            for (const r of g.rows) {
                const s = document.createElement('span');
                s.className = 'tg-cov-chip ' + (r.status === 'cov' ? 'ok' : r.status);
                s.textContent = '.' + r.cls;
                box.appendChild(s);
            }
            sec.append(t, box);
            if (g.file === 'login.css' || g.file === 'restart.css' || g.file === 'move_chat.css') {
                const n = document.createElement('div');
                n.className = 'tg-cov-note';
                // UPDATED 2026-10-25: all three now have static samples in the
                // sheet (restart/move-chat in E29, login in E35). What stays
                // grey are runtime-only state classes, not missing elements.
                n.textContent = '↑ shown statically in the sheet (login E35 · restart/move chat E29). Grey = a state or dead selector, not a missing element.';
                sec.appendChild(n);
            }
        }
        pane.appendChild(sec);
        head.querySelector('.tg-el-coverview-refresh').addEventListener('click', () => {
            this._coverageReport = null;
            this._rebuildNeeded = true;   // perf: same structKey, new report body
            this.render();
        });
        this.lockdownPane(pane);   // inert except our refresh button
    },
});

/* theme_gallery — CATEGORY PANE BODIES (elements sheet, welcome/sidebar clones,
   captured settings, the fake context pill).
   Registers onto tgRaw via tgPart(); see theme_gallery.js for the merge. */
// perf 2026-10-25: the settings capture is 12 full-modal HTML strings
// (~7.6 MB). Parsing all of them on EVERY settings render measured 346 ms;
// parse once per capture and CLONE the parsed fragment instead — cloning is
// an order of magnitude cheaper and produces the same DOM.
const settingsFragCache = new Map();

tgPart({

    // ---- elements category: static style guide (see ELEMENTS.md) ----
    // Fully static template (fake content, real app classes) → nothing to
    // disable except the generic pane lockdown; fields are marked
    // .tg-allow so they can be CLICKED/TYPED locally (live look of inputs)
    // while staying inert. Swatch grid is filled after the theme is
    // written onto the pane (fillSwatches), using COMPUTED values so
    // var()/color-mix chains resolve exactly as the app would render.
    buildElementsBody(pane) {
        const tpl = document.getElementById('tg-sample-elements');
        if (!tpl) return;
        pane.appendChild(tpl.content.cloneNode(true));
        pane.querySelectorAll('input, select, textarea').forEach(el => {
            if (el.type === 'file') { el.disabled = true; return; }
            el.classList.add('tg-allow');
        });
        this.lockdownPane(pane);
    },

    // ---- welcome category: clone the real welcome screen ----
    // The three parts are DIRECT children of the real #chat-window; the
    // overlay lives inside #chat-window too (last child), so ':scope >'
    // selectors can never pick up a clone from a previous render.
    //
    // Every clone runs through stripAlpine() BEFORE insertion: Alpine v3 WOULD
    // otherwise process the inserted subtree and re-bind x-model/@click to the
    // SHARED stores (observed: a live clone's x-model typed into the user's
    // real input). Stripped clones are plain HTML — app CSS still styles them
    // identically, but there is nothing for Alpine (or the app) to act on.
    buildWelcomeBody(pane) {
        const cw = document.getElementById('chat-window');
        if (!cw) return;

        const header = cw.querySelector(':scope > header');
        if (header) pane.appendChild(stripAlpine(header.cloneNode(true)));

        // messages container: the app's own "#chat-window #messages" rule
        // gives flex:1, which centers the welcome panel exactly like real
        const messages = document.createElement('div');
        messages.id = 'messages';
        const liveWelcome = cw.querySelector(':scope > #messages .welcome-panel');
        if (liveWelcome) {
            messages.appendChild(stripAlpine(liveWelcome.cloneNode(true)));
        } else {
            // the panel lives in an <template x-if> (mounted only while
            // the live chat is empty) → fall back to the template content
            const tmpl = [...cw.querySelectorAll(':scope > #messages > template')]
                .find(t => t.content.querySelector('.welcome-panel'));
            if (tmpl) messages.appendChild(stripAlpine(tmpl.content.querySelector('.welcome-panel').cloneNode(true)));
        }
        pane.appendChild(messages);

        const input = cw.querySelector(':scope > #input');
        if (input) pane.appendChild(stripAlpine(input.cloneNode(true)));

        // ---- model switcher: STATIC + FAKE on purpose ----
        // A live-revived dropdown (Alpine.initTree) was replaced: the real
        // model list must not surface in a preview, no lazyFetch GET may
        // fire, and nothing should even LOOK switchable for real. We keep
        // the exact widget classes (.model-switcher / .model-select) so
        // themes style it identically, but fill it with made-up names.
        const sw = pane.querySelector('.model-switcher');
        if (sw) {
            const fake = document.createElement('select');
            fake.className = 'model-select';
            for (const name of ['gemma-4-12b-it-qat', 'llama-3.3-70b-instruct-awq', 'qwen3-32b-exl3']) {
                const opt = document.createElement('option');
                opt.value = name;
                opt.textContent = name;
                fake.appendChild(opt);
            }
            sw.replaceChildren(fake);
        }

        // ---- fake but fully-working context pill ----
        // The real pill is an Alpine widget that fetches
        // /api/system/context_size and POLLS it every 5s while open —
        // reviving it would hit the API and mutate the chat store. So we
        // replace the cloned shell with a hand-built replica using the
        // app's EXACT markup/classes/CSS and made-up numbers. Clicking it
        // opens the identical popup (own toggle, no Alpine, no fetch).
        const pill = pane.querySelector('#context-pill');
        if (pill) this.installFakeContextPill(pill);

        // hard-disable every interaction except the whitelisted ones
        this.lockdownPane(pane);
    },

    // ---- sidebar category: live clone of the real sidebar shell ----
    // Cloned FRESH on every render (like welcome), so it always mirrors
    // the user's actual sidebar: their module panels, chat list, day
    // groups, footer buttons. Panel↔tab mapping is read from each panel's
    // x-show LITERAL (sidebarTab === 'mod/name') before stripping — the
    // rail buttons keep their data-tab (data-* survives stripAlpine).
    // Teleported popups (category menu, chat action menus, tag popups)
    // live in <body>, NOT inside .sidebar-shell → they can never leak
    // into the clone. Real #chat-list precedes the overlay in document
    // order, so app querySelector/getElementById still hit the ORIGINAL.
    buildSidebarBody(pane) {
        const shell = document.querySelector('.sidebar-shell');
        if (!shell) {
            const w = document.createElement('div');
            w.className = 'tg-note';
            w.textContent = 'Sidebar not present on this page.';
            pane.appendChild(w);
            return;
        }
        const clone = shell.cloneNode(true);
        // tag panels with their owning tab BEFORE stripAlpine eats x-show
        clone.querySelectorAll('.sidebar-panel').forEach(p => {
            const m = (p.getAttribute('x-show') || '').match(/sidebarTab === '([^']+)'/);
            if (m) p.dataset.tgPanel = m[1];
        });
        stripAlpine(clone);

        const host = document.createElement('div');
        host.className = 'tg-sidebar-host';
        host.appendChild(clone);

        // pane-local tab switcher: rail buttons show their panel
        const show = (tab) => {
            // pane-local panel swap can change which element is the main scroller
            this._invalidateScroller(pane);
            const list = clone.querySelector('#chat-list');
            clone.querySelectorAll('.sidebar-panel').forEach(p => {
                p.style.display = p.dataset.tgPanel === tab ? '' : 'none';
            });
            if (list) list.style.display = tab === '' ? '' : 'none';
            clone.querySelectorAll('.sidebar-tab').forEach(b =>
                b.classList.toggle('active', (b.dataset.tab || '') === tab));
        };
        clone.querySelectorAll('.sidebar-tab').forEach(b => {
            b.classList.add('tg-allow');
            b.addEventListener('click', () => show(b.dataset.tab || ''));
        });
        // form fields clickable but inert (bindings stripped → searching
        // the clone cannot touch $store.chat or fire /api/chats/search)
        host.querySelectorAll('input, select, textarea').forEach(el => {
            if (el.type === 'file') { el.disabled = true; return; }
            el.classList.add('tg-allow');
        });
        // start on the CHAT LIST, not a mirror of the user's real tab:
        // with theme_gallery active, a mirror would show our own gallery
        // panel inside a preview pane (gallery-in-gallery). The chat list
        // is also the most element-dense sidebar surface for theme QA.
        show('');
        pane.appendChild(host);
        this.lockdownPane(pane);
    },

    // Rebuild each pane's settings preview from the cached HTML: per
    // category we re-parse a full modal clone (nav included, with its
    // .active button already correct), stack them in a host and show one.
    // The pane-local switcher wires the captured nav buttons — plain JS,
    // no Alpine, no $store.settings ever touched.
    buildSettingsBody(pane) {
        const wrap = document.createElement('div');
        wrap.className = 'tg-settings';
        const cap = this._settingsCapture;
        if (cap === undefined) {
            wrap.innerHTML = '<div class="tg-note">Preparing settings capture…</div>';
            pane.appendChild(wrap);
            return;
        }
        if (!cap) {
            wrap.innerHTML = '<div class="tg-note">Settings capture unavailable (admin settings disabled?).</div>';
            pane.appendChild(wrap);
            this.lockdownPane(pane);
            return;
        }
        // tabs that were STILL mounting when captured (BUGS.md #17): say so,
        // so a leftover loading placeholder reads as "capture was early",
        // not as a theme bug.
        if (cap.failed && cap.failed.length) {
            const warn = document.createElement('div');
            warn.className = 'tg-capture-note';
            warn.textContent = 'Capture incomplete for: ' + cap.failed.join(', ')
                + ' — still loading when captured; their preview may show a placeholder until the page is reloaded.';
            wrap.appendChild(warn);
        }
        const host = document.createElement('div');
        host.className = 'tg-settings-host';
        for (const s of cap.sections) {
            // parse ONCE per captured tab, then clone (see settingsFragCache)
            let frag = settingsFragCache.get(s.cat);
            if (!frag || frag._srcHtml !== s.html) {
                const t = document.createElement('template');
                t.innerHTML = s.html.trim();
                frag = t.content;
                frag._srcHtml = s.html;
                settingsFragCache.set(s.cat, frag);
            }
            const modal = frag.firstElementChild.cloneNode(true);
            if (!modal) continue;
            modal.dataset.tgSec = s.cat;
            if (host.children.length) modal.style.display = 'none';
            host.appendChild(modal);
        }
        // pane-local tab switcher (buttons were tagged during capture)
        host.querySelectorAll('[data-tg-scat]').forEach(b => {
            b.classList.add('tg-allow');
            b.addEventListener('click', () => this._settingsShow(host, b.dataset.tgScat));
        });
        // form fields: clickable/typable but INERT — their bindings were
        // stripped at capture, so toggling a checkbox flips pixels only.
        host.querySelectorAll('input, select, textarea').forEach(el => {
            if (el.type === 'file') { el.disabled = true; return; }
            el.classList.add('tg-allow');
        });
        // paranoia: a stray <form> must never navigate the page on Enter
        host.querySelectorAll('form').forEach(f =>
            f.addEventListener('submit', (e) => e.preventDefault()));
        wrap.appendChild(host);
        pane.appendChild(wrap);
        this.lockdownPane(pane);
    },

    _settingsShow(host, cat) {
        host.querySelectorAll(':scope > .settings-modal').forEach(m => {
            m.style.display = m.dataset.tgSec === cat ? '' : 'none';
        });
        // a different tab = different heights = possibly a different main scroller
        this._invalidateScroller(host.closest('.tg-pane'));
    },

    // made-up context stats: realistic for a mid-sized chat
    FAKE_CONTEXT: {
        max_context: 32768,
        total_tokens: 11456,
        percent_full: 35,
        compaction_count: 2,
        auto_compress: true,
        compression_threshold: 80,
        system_prompt: {tokens: 1840},
        tools: {active: 9, tokens: 2320},
        message_history: {tokens: 7060},
        end_prompt: {tokens: 180},
    },

    // Builds the pill + popup replica (markup verbatim-equivalent to
    // templates/chat/context_pill.html, minus Alpine) and wires the
    // open/close toggle by hand.
    installFakeContextPill(host) {
        const d = this.FAKE_CONTEXT;
        const fmt = n => (n ?? 0).toLocaleString();
        const parts = [
            {key: 'sys', label: 'system prompt', tokens: d.system_prompt.tokens},
            {key: 'tools', label: 'tools', tokens: d.tools.tokens},
            {key: 'history', label: 'message history', tokens: d.message_history.tokens},
            {key: 'end', label: 'end prompt', tokens: d.end_prompt.tokens},
        ];
        const usedPct = Math.min((d.total_tokens / d.max_context) * 100, 100);
        const segs = parts.map(p => ({...p, pct: Math.min((p.tokens / d.max_context) * 100, 100)}));
        segs.push({key: 'free', label: 'free', tokens: d.max_context - d.total_tokens, pct: 100 - usedPct});
        const legendPct = s => s.pct >= 1 ? Math.round(s.pct) + '%' : (s.pct > 0 ? '<1%' : '0%');
        const pillPct = Math.round(usedPct);
        const PATH = 'M38 1 H61 A14 14 0 0 1 75 15 A14 14 0 0 1 61 29 H14 A14 14 0 0 1 1 15 A14 14 0 0 1 14 1 Z';

        host.removeAttribute('x-data');
        host.removeAttribute('x-cloak');
        host.innerHTML =
            '<button class="pill-button" type="button" title="' + fmt(d.total_tokens) + ' / ' + fmt(d.max_context) + ' tokens"' +
            ' aria-label="Context usage: ' + pillPct + '%">' +
                '<svg class="pill-outline" viewBox="0 0 76 30" preserveAspectRatio="none" aria-hidden="true">' +
                    '<path class="pill-track" d="' + PATH + '"></path>' +
                    '<path class="pill-fill" pathLength="100" stroke-dasharray="' + pillPct + ' ' + (100 - pillPct) + '" d="' + PATH + '"></path>' +
                '</svg>' +
                '<span class="pill-pct">' + pillPct + '%</span>' +
            '</button>' +
            '<div class="context-popup" style="display:none">' +
                '<div class="popup-header"><span class="popup-title">Context</span></div>' +
                '<div class="popup-body">' +
                    '<div class="popup-hero">' +
                        '<span class="hero-pct' + (d.percent_full >= 75 ? ' danger' : '') + '">' + Math.round(d.percent_full) + '%</span>' +
                        '<span class="hero-sub">' + fmt(d.total_tokens) + ' / ' + fmt(d.max_context) + ' tokens</span>' +
                    '</div>' +
                    (d.compaction_count > 0
                        ? '<div class="ctx-compaction" title="older context was summarized away"><span class="compaction-label">compacted ' + d.compaction_count + ' time' + (d.compaction_count === 1 ? '' : 's') + '</span></div>'
                        : '') +
                    '<div class="ctx-bar" role="img" aria-label="Context: ' + Math.round(d.percent_full) + ' percent full">' +
                        segs.map(s => '<div class="bar-seg seg-' + s.key + '" style="width:' + s.pct + '%" title="' + s.label + ': ' + fmt(s.tokens) + ' tokens (' + s.pct.toFixed(1) + '%)"></div>').join('') +
                        (d.auto_compress && d.compression_threshold > 0
                            ? '<div class="bar-threshold-zone" style="left:' + d.compression_threshold + '%" title="compression threshold: ' + d.compression_threshold + '%"></div>' +
                              '<div class="bar-threshold" style="left:' + d.compression_threshold + '%"></div>'
                            : '') +
                    '</div>' +
                    '<div class="ctx-legend">' +
                        segs.filter(s => s.key !== 'free').map(s =>
                            '<div class="legend-item seg-' + s.key + '">' +
                                '<span class="legend-dot"></span>' +
                                '<span class="legend-label">' + s.label + '</span>' +
                                '<span class="legend-pct">' + legendPct(s) + '</span>' +
                            '</div>').join('') +
                    '</div>' +
                    '<div class="popup-rows">' +
                        '<div class="popup-row"><span class="popup-key">system prompt</span><span class="popup-value">' + fmt(d.system_prompt.tokens) + ' tokens</span></div>' +
                        '<div class="popup-row"><span class="popup-key">tools</span><span class="popup-value">' + d.tools.active + ' active | ' + fmt(d.tools.tokens) + ' tokens</span></div>' +
                        '<div class="popup-row"><span class="popup-key">message history</span><span class="popup-value">' + fmt(d.message_history.tokens) + ' tokens</span></div>' +
                        '<div class="popup-row"><span class="popup-key">end prompt</span><span class="popup-value">' + fmt(d.end_prompt.tokens) + ' tokens</span></div>' +
                    '</div>' +
                '</div>' +
            '</div>';

        // hand-rolled toggle (mirrors the real component's open state;
        // no fetching, no polling, no store writes).
        // STICKY on purpose: unlike the real pill (@click.outside), a popup
        // stays open until its OWN pill button is pressed again. Deliberate
        // gallery behavior so split view can compare both popups side by
        // side — an outside-click closer would close pane A's popup the
        // moment you click pane B's pill (user QA request 2026-10-25).
        const popup = host.querySelector('.context-popup');
        host.querySelector('.pill-button').addEventListener('click', () => {
            popup.style.display = popup.style.display === 'none' ? '' : 'none';
        });
    },
});

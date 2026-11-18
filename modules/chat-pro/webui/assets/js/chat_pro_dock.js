/* chat-pro - the right dock: tabs, geometry, full mode, hosted panels.

   Registers onto CHAT_PRO via chatPart(); chat_pro.js is loaded first (alphabetically,
   and '.' sorts before '_') and owns the object + the alpine:init registration, so the
   order among the other files does not matter. See chat_pro.js for the file map. */
chatPart({
    togglePanel() {
        this.setPanelOpen(!this.panelOpen);
    },

    setPanelOpen(open) {
        if (open) {
            // one right dock at a time. delegate doesn't know chat-pro exists, so
            // it cannot step aside by itself; ask its public store to close first
            // (feature-detected: it simply isn't there when the module is off)
            try {
                const delegate = Alpine.store("delegateView");
                if (delegate && delegate.isOpen && typeof delegate.close === "function") {
                    delegate.close();
                }
            } catch (error) { /* delegate not loaded */ }
        }

        this.panelOpen = Boolean(open);
        try { localStorage.setItem("chatProPanelOpen", this.panelOpen ? "1" : "0"); } catch (error) { /* private mode */ }
        // the width change re-wraps the chat: keep the reader's place
        this._withChatAnchor(() => this._applyPanelLayout(this.panelOpen));
        this._syncDelegateHost();
    },

    // the other direction: if delegate opens while this panel is up, yield to it
    watchDelegate() {
        if (this._delegateWatcher || !window.Alpine) { return; }
        let delegate = null;
        try { delegate = Alpine.store("delegateView"); } catch (error) { /* not loaded */ }
        if (!delegate) { return; }

        this._delegateWatcher = true;
        Alpine.effect(() => {
            if (delegate.isOpen && Alpine.store("chatPro").panelOpen) {
                Alpine.store("chatPro").setPanelOpen(false);
            }
        });
    },

    // the docked panel reserves width in the chat column, so opening/closing it - and
    // switching to full mode - re-wraps every message. measured on a 205-turn
    // window: opening it shrank #messages from 974px to 614px and grew the content
    // from 110848 to 150370px while scrollTop stayed 57368, so what you were reading
    // jumped ~21566px out of view; closing it put it back. core sets overflow-anchor:
    // none on #messages, so the browser does not compensate for the reflow either.
    //
    // hold the same message under the viewport top across the change: remember the
    // first turn whose bottom is below it (and its offset), plus whether we were at
    // the bottom, then correct scrollTop for a handful of frames while the new
    // layout settles.
    _withChatAnchor(apply) {
        const container = document.getElementById("messages");
        if (!container) { apply(); return; }

        const top = container.getBoundingClientRect().top;
        let anchor = null;
        for (const el of container.querySelectorAll("[data-turn-index]")) {
            if (el.getBoundingClientRect().bottom > top) { anchor = el; break; }
        }

        const turn = anchor ? Number(anchor.dataset.turnIndex) : null;
        const before = anchor ? anchor.getBoundingClientRect().top - top : 0;
        const atBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 50;

        apply();

        if (!Number.isFinite(turn)) { return; }

        // wrapping settles over the next frames; every pass is a no-op once the
        // anchor is back where it was, and the loop is bounded either way
        const limit = atBottom ? 6 : 20;
        let pass = 0;
        let previous = null;

        const settle = () => {
            if (pass++ >= limit) { return; }

            const el = container.querySelector(`[data-turn-index="${turn}"]`);
            if (!el) { return; }

            if (atBottom) {
                container.scrollTo({ top: container.scrollHeight, behavior: "instant" });
                requestAnimationFrame(settle);
                return;
            }

            const offset = el.getBoundingClientRect().top - container.getBoundingClientRect().top - before;
            if (Math.abs(offset) > 1) {
                container.scrollTo({ top: container.scrollTop + offset, behavior: "instant" });
            } else if (previous !== null && Math.abs(previous) <= 1) {
                return;
            }

            previous = offset;
            requestAnimationFrame(settle);
        };

        requestAnimationFrame(settle);
    },

    // the panel hangs under the header and the chat reserves its width. the header
    // height is measured rather than hard-coded (themes size their buttons
    // differently), and re-measured on resize.
    _applyPanelLayout(open) {
        const win = document.getElementById("chat-window");
        if (!win) { return; }

        // measure against the live rects: offsetHeight rounds and the header can
        // still be growing when the page is initialising (model switcher, fonts),
        // so re-measure once after the next frame and again after alpine:initialized
        const measure = () => {
            const header = win.querySelector("header");
            if (!header) { return; }
            const bottom = header.getBoundingClientRect().bottom - win.getBoundingClientRect().top;
            win.style.setProperty("--cp-top", `${Math.max(0, Math.round(bottom))}px`);
        };

        measure();
        requestAnimationFrame(measure);

        win.classList.toggle("cp-panel-open", Boolean(open));
        win.classList.toggle("cp-panel-full", Boolean(this.panelFull));

        // the header can keep growing after this point (model switcher, theme
        // fonts, streaming badge): follow it instead of measuring once
        if (!this._headerObserver && window.ResizeObserver) {
            const header = win.querySelector("header");
            if (header) {
                this._headerObserver = new ResizeObserver(() => measure());
                this._headerObserver.observe(header);
            }
        }
    },

    closePanel() {
        this.setPanelOpen(false);
    },

    // full ⇄ docked. the flag, not the tab, decides the geometry, so every tab
    // gets it for free; it is remembered per browser like the open state.
    togglePanelFull() {
        this.panelFull = !this.panelFull;
        try { localStorage.setItem("chatProPanelFull", this.panelFull ? "1" : "0"); } catch (error) { /* private mode */ }
        this._withChatAnchor(() => this._applyPanelLayout(this.panelOpen));
    },

    /* ----------------
     * delegate hosting
     * ----------------- */
    // delegate's header button checks this before opening its own dock; the
    // setting is rendered onto .cp-cover, since module settings do not reach js
    hostDelegate() {
        const cover = document.querySelector(".cp-cover");
        return !cover || cover.dataset.hostDelegate !== "0";
    },

    openPanelTab(name) {
        this.setPanelOpen(true);
        this.setPanelTab(name);
    },

    // delegate keeps its own geometry and polling unless this dock exists
    _markDelegateEmbedded() {
        let delegate = null;
        try { delegate = Alpine.store("delegateView"); } catch (error) { /* not loaded */ }
        if (delegate && typeof delegate.setEmbedded === "function") {
            delegate.setEmbedded(this.hostDelegate());
        }
    },

    // its panel is "visible" only while the Tasks tab is the one on screen
    _syncDelegateHost() {
        let delegate = null;
        try { delegate = Alpine.store("delegateView"); } catch (error) { /* not loaded */ }
        if (!delegate || typeof delegate.hostActive !== "function") { return; }
        delegate.hostActive(this.hostDelegate() && this.panelOpen && this.panelTab === "tasks");
    },

    setPanelTab(tab) {
        const known = ["branches", "search", "links", "tasks"];
        this.panelTab = known.includes(tab) ? tab : "branches";
        try { localStorage.setItem("chatProPanelTab", this.panelTab); } catch (error) { /* private mode */ }
        if (this.panelTab === "links") this.scanRefs();
        this._syncDelegateHost();
    },

    restorePanel() {
        try {
            this.panelOpen = localStorage.getItem("chatProPanelOpen") === "1";
            this.panelFull = localStorage.getItem("chatProPanelFull") === "1";
            const tab = localStorage.getItem("chatProPanelTab");
            if (["branches", "search", "links", "tasks"].includes(tab)) { this.panelTab = tab; }
        } catch (error) { /* private mode */ }

        // the width change re-wraps the chat: keep the reader's place
        this._withChatAnchor(() => this._applyPanelLayout(this.panelOpen));
        this._syncDelegateHost();
    },
});

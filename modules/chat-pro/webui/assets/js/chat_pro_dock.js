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
        this._applyPanelLayout(this.panelOpen);
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
        this._applyPanelLayout(this.panelOpen);
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

        this._applyPanelLayout(this.panelOpen);
        this._syncDelegateHost();
    },
});

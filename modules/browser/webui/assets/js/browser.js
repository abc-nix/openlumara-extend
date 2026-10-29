// -- AI GENERATED CODE (Qwen3.8-Flash-Next) :: (2026-10-07)
/*
 * Browser module webui panel: one shared Alpine store ('browserPanel') +
 * glue for the module's ext routes (see user_modules/browser/module.py):
 *   GET  /api/ext/browser/status   -> passive (NEVER launches the browser)
 *   POST /api/ext/browser/open     -> launch + attach
 *   POST /api/ext/browser/close    -> terminate (owned) / disconnect (external)
 *
 * The rail button and the popover both bind to this store, so their state
 * can never drift apart. The passive status route is polled for as long as
 * the page is visible - NOT only while the popover is open: the dot on the
 * rail button shows whether the browser is up, and it used to stay stale
 * until the panel was clicked (there is no event for "a tool closed the
 * browser", so the UI has to look). Polling pauses while the tab is hidden,
 * requests never overlap, and every action (open/close/restart) still
 * refreshes immediately.
 * Popover open/close lives here (panelOpen) so the button toggle, the
 * click-outside dismiss and the Esc listener can never disagree.
 *
 * Loaded via the ext_js pipeline (/ext-assets/browser/js/browser.js); Alpine
 * registers through its own alpine:init listener, so load order vs core
 * files doesn't matter (Alpine itself is deferred).
 */

// The UI reads the Alpine store *proxy*; writing through the raw object
// (BROWSER_PANEL) changes the data without notifying Alpine, so the render
// never follows. Every write has to go through this: the proxy once Alpine
// exists, the raw object before that (first paint / tests).
function browserPanelStore() {
    if (typeof Alpine !== 'undefined' && Alpine.store) {
        const store = Alpine.store('browserPanel');
        if (store) return store;
    }
    return BROWSER_PANEL;
}

const BROWSER_PANEL_CLOSED = {
    connected: false, browser_running: false, owned_process: false,
    browser: null, current_tab: null, tab_count: null,
};

const BROWSER_PANEL = {
    status: null,        // null = never fetched (dot shows 'checking')
    busy: false,         // an open/close call is in flight
    error: null,
    polling: false,
    _timer: null,
    _inflight: false,

    async refresh() {
        if (this._inflight) return;   // never stack requests on a slow route
        this._inflight = true;
        try {
            this.status = await simpleApiFetch('/api/ext/browser/status');
        } catch (e) {
            // route unreachable (module disabled/reloaded mid-session):
            // show closed, don't nag with an error line
            this.status = { ...BROWSER_PANEL_CLOSED };
        } finally {
            this._inflight = false;
        }
    },

    async openBrowser() {
        if (this.busy) return;
        this.busy = true;
        this.error = null;
        try {
            await simpleApiPost('/api/ext/browser/open');
        } catch (e) {
            this.error = typeof e === 'string' ? e : ((e && e.message) || 'could not open the browser');
        }
        this.busy = false;
        this.refresh();
    },

    async closeBrowser() {
        if (this.busy) return;
        this.busy = true;
        this.error = null;
        try {
            await simpleApiPost('/api/ext/browser/close');
        } catch (e) {
            this.error = typeof e === 'string' ? e : ((e && e.message) || 'could not close the browser');
        }
        this.busy = false;
        this.refresh();
    },

    // ---- single Open/Close toggle (one browser instance can exist) ----
    detailsOpen: false,

    // ---- popover lifecycle ----
    panelOpen: false,

    openPanel() {
        this.panelOpen = true;
        this.error = null;
        this.refresh();
        this.startPolling();
    },

    closePanel() {
        this.panelOpen = false;
        this.helpOpen = false;
        // polling keeps running: the rail dot must stay truthful while the
        // popover is closed (that was the bug)
    },

    togglePanel() {
        if (this.panelOpen) this.closePanel();
        else this.openPanel();
    },

    actionLabel() {
        if (this.busy) return 'working..';
        const s = this.status;
        if (!s) return '..';
        if (s.connected) return 'Close browser';
        // something already on the port (e.g. external browser): open() attaches
        return s.browser_running ? 'Connect browser' : 'Open browser';
    },

    async toggleBrowser() {
        if (this.busy) return;
        if (this.status && this.status.connected) return this.closeBrowser();
        return this.openBrowser();
    },

    // ---- restart in the opposite headless mode (owned browser only) ----
    async restartBrowser() {
        if (this.busy) return;
        this.busy = true;
        this.error = null;
        try {
            await simpleApiPost('/api/ext/browser/restart');
        } catch (e) {
            this.error = typeof e === 'string' ? e : ((e && e.message) || 'could not restart the browser');
        }
        this.busy = false;
        this.refresh();
    },

    restartLabel() {
        // "Restart headless" read as ambiguous (to? from?), so name the target
        // mode instead. unknown headless (external/none) defaults to headless.
        return (this.status && this.status.headless) ? 'Switch to visible window' : 'Switch to headless';
    },

    // ---- help window: body comes from assets/browser-help.md ----
    // Fetched (no-store) on every open, so editing the .md file is enough
    // to change this text. Rendered with core's global renderMarkdown()
    // (marked); <pre> fallback if that helper is ever unavailable.
    helpOpen: false,
    helpHtml: '',
    helpError: null,

    async toggleHelp() {
        if (this.helpOpen) {
            this.helpOpen = false;
            return;
        }
        await this.loadHelp();
        this.helpOpen = true;
    },

    async loadHelp() {
        this.helpError = null;
        try {
            const res = await fetch('/ext-assets/browser/browser-help.md',
                                    { cache: 'no-store', credentials: 'same-origin' });
            if (!res.ok) throw new Error('HTTP ' + res.status);
            const md = await res.text();
            this.helpHtml = (typeof renderMarkdown === 'function')
                ? renderMarkdown(md)
                : '<pre>' + md.replace(/[&<>]/g, c => (
                    { '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c])) + '</pre>';
        } catch (e) {
            this.helpError = 'could not load help (' + ((e && e.message) || e) + ')';
            this.helpHtml = '';
        }
    },

    startPolling() {
        if (this._timer) return;      // already running (called on init, on show, on panel open)
        this.polling = true;
        this._timer = setInterval(() => {
            // refresh through the Alpine proxy, never through the captured
            // raw object: otherwise the fetch updates data the UI is not
            // subscribed to (the dot stayed stale while status was correct)
            if (!document.hidden) browserPanelStore().refresh();
        }, 3000);
    },

    stopPolling() {
        if (this._timer) clearInterval(this._timer);
        this._timer = null;
        this.polling = false;
    },

    // ---- display helpers, shared by footer button + modal ----
    dotClass() {
        const s = this.status;
        if (!s) return 'checking';
        if (s.connected) return s.owned_process ? 'on' : 'ext';
        return s.browser_running ? 'ext' : 'off';
    },

    statusText() {
        const s = this.status;
        if (!s) return 'checking..';
        if (s.connected) {
            return s.owned_process
                ? 'connected · launched by the module'
                : 'connected · external browser';
        }
        return s.browser_running ? 'running, not connected' : 'closed';
    },

    // pluralised tab count for the details block ("1 tab" / "3 tabs")
    tabCountText() {
        const n = this.status ? this.status.tab_count : null;
        if (n === null || n === undefined) return '';
        return n === 1 ? '1 tab' : n + ' tabs';
    },

    dotTitle() {
        return 'Browser — ' + this.statusText();
    },
};

document.addEventListener('alpine:init', () => {
    Alpine.store('browserPanel', BROWSER_PANEL);
    // through the proxy: the first paint gets real data and Alpine's effects
    // subscribe to it from the start
    browserPanelStore().refresh();
});

// The rail dot is visible whether or not the popover is open, so polling must
// not depend on Alpine having fired yet (a listener registered late would
// never run, and the dot would stay at "checking.." until a click). The timer
// itself resolves the store per tick, so it keeps writing through the proxy
// once Alpine is up.
BROWSER_PANEL.startPolling();
document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
        BROWSER_PANEL.stopPolling();
    } else {
        browserPanelStore().refresh();
        BROWSER_PANEL.startPolling();
    }
});

/*
 * chat-pro - frontend half of the message-row controls and the right dock.
 *
 *   fork  (leftmost button)  -> a new chat, a full copy up to that message
 *   branch (rightmost)       -> `>` starts a variant inside this chat, `< n/m >`
 *                               walks the siblings of a message
 *
 * both are backend jobs (they write a chat / a tree file), so this store asks
 * the module's routes and then lets the core switch chats and re-render.
 *
 * one subtlety worth knowing: the ids of the branch nodes cannot be read off
 * the client's turn objects. core's turn_hash() deliberately ignores _metadata,
 * and mergeTurnHistory() reuses a turn whose hash matches - so an id added to a
 * message later would never reach the turn object already in memory. the state
 * route therefore maps *live message index* -> node id (`index_ids`), and that
 * is what the counters look up.
 *
 * WHERE THE CODE LIVES (core appends every file in webui/assets/js, alphabetically;
 * 'chat_pro.js' sorts first because '.' < '_', and each part registers itself below,
 * so the order among parts is irrelevant):
 *   chat_pro.js          the object, its state fields, the alpine bootstrap (this)
 *   chat_pro_branch.js   counters, > / < n/m > navigation, prune, fork modal
 *   chat_pro_dock.js     the right dock: tabs, geometry, full mode, hosted panels
 *   chat_pro_refs.js     the references tab (urls + file paths)
 *   chat_pro_graph.js    the branch graph window, its rail and hover miniature
 */

const CHAT_PRO = {};

// every part calls this at parse time; all of them run before alpine:init fires
function chatPart(part) {
    Object.assign(CHAT_PRO, part);
}

chatPart({
    forking: false,
    busy: false,

    hasTree: false,
    variants: {},
    indexIds: {},
    // live message index -> ABSOLUTE turn index, straight from the state route.
    // core only keeps a window of turns in memory now, so a card whose message
    // is older than that window has no turn index the client could derive.
    turnIndex: {},
    turnTotal: 0,
    branches: [],
    branchCount: 0,
    branchPoints: 0,
    active: [],
    chatId: null,

    _signature: null,
    _branchEdit: null,
    _patched: false,

    // node id awaiting the in-panel delete confirmation (see prune())
    confirming: null,

    // pending fork, answered by the modal in chat-pro/modals/chatpro_fork.html.
    // same reason libreta draws its own prompts: native confirm() is auto-answered
    // in some environments, so it forked without ever asking.
    forkConfirm: null,

    // right panel (chat-pro/chat/panel.html). the tab is remembered per browser,
    // so reopening lands where the user left it.
    panelOpen: false,
    panelTab: "branches",
    panelFull: false,     // docked beside the chat, or covering the chat window
    _headerObserver: null,

    // the branch graph (modals/chatpro_graph.html): every node of the tree,
    // fetched only while that window is open. `state` stays the collapsed map
    // the poll refreshes; this is the heavy, on-demand payload.
    graphOpen: false,
    graphLoading: false,
    graphNodes: [],
    graphActive: [],
    graphStats: { nodes: 0, branches: 0 },

    // the graph's landmark rail: fork points and compression summaries, in canvas
    // order, so a long tree can be navigated without hunting for the one card.
    // built by _graphRailItems() from the payload (children / compressed), section
    // state remembered per browser.
    graphRail: { branches: [], compressions: [] },
    graphRailOpen: { branches: true, compressions: true },

    // references tab: urls and file paths found in this chat's history
    refs: [],
    // true while the scan is still paging the turns older than the loaded window
    refsScanning: false,
    refsQuery: "",
    refKind: "all",        // "all" | "url" | "file"
    refShowContent: true,  // references the message text mentions
    refShowTools: false,   // references tool output - off by default, it is noise
    refShowReasoning: false, // references mentioned while thinking
});

chatPart({
    /* ----------------
     * bootstrapping
     * ----------------- */
    patch() {
        if (this._patched) { return true; }
        if (typeof window === "undefined" || !window.Alpine) { return false; }

        const alpine = window.Alpine;
        const chat = alpine.store("chat");

        // the chat store is registered by init.js inside its own alpine:init
        // listener, which runs *after* this module's - so this may be called
        // again later (alpine:initialized and the poll both retry)
        if (!chat || typeof chat.saveEdit !== "function") { return false; }

        const originalSave = chat.saveEdit.bind(chat);
        chat.saveEdit = async function (index) {
            const store = alpine.store("chatPro");
            if (!store._branchEdit) { return originalSave(index); }

            const pending = store._branchEdit;
            store._branchEdit = null;
            return store._saveBranchEdit(chat, pending);
        };

        const originalCancel = chat.cancelEdit.bind(chat);
        chat.cancelEdit = function () {
            alpine.store("chatPro")._branchEdit = null;
            return originalCancel();
        };

        this._patched = true;
        return true;
    },

    _fail(prefix, error) {
        console.error(`chat-pro: ${prefix}`, error);
        const text = String(error).replace(/[&<>"']/g, (ch) => ({
            "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
        }[ch]));

        const ui = Alpine.store("ui");
        ui.notice = `${prefix}: ${text}`;
        setTimeout(() => {
            if (ui.notice && ui.notice.startsWith(prefix)) { ui.notice = null; }
        }, 6000);
    }
});

document.addEventListener("alpine:init", () => {
    Alpine.store("chatPro", CHAT_PRO);
    CHAT_PRO.restorePanel();
    CHAT_PRO.restoreRefPrefs();
    setTimeout(() => CHAT_PRO.patch(), 0);
});

document.addEventListener("alpine:initialized", () => {
    CHAT_PRO.patch();
    // the header is fully rendered by now: settle the panel's top offset
    CHAT_PRO._applyPanelLayout(CHAT_PRO.panelOpen);
    // one right dock at a time (delegate's store is public; absent when off)
    CHAT_PRO.watchDelegate();
    // and: if delegate is here, this dock is its host
    CHAT_PRO._markDelegateEmbedded();
    CHAT_PRO._syncDelegateHost();
});

// the panel hangs from the header, so its top offset follows the header's height
window.addEventListener("resize", () => {
    if (CHAT_PRO.panelOpen) { CHAT_PRO._applyPanelLayout(true); }
});

// counters/siblings are refreshed when the open chat, its turn count, the tail
// turn's content, or the stream state move; a signature guard keeps the poll
// from hitting the backend while nothing changes
setInterval(() => {
    try {
        if (!CHAT_PRO._patched) { CHAT_PRO.patch(); }
        CHAT_PRO.refresh(false);
    } catch (error) { /* ignore */ }
}, 1500);


/* core paginates turns: scrollToTurn() only finds turns that are actually
 * loaded (a window starting at chat.turnStart), so a jump to an older turn has
 * to page it in first. the page size is raised for the walk, so a long jump is a
 * handful of requests instead of one per five turns. returns whether the target
 * ended up loaded. */
async function cpEnsureTurnLoaded(absIndex) {
    if (!Number.isFinite(absIndex) || absIndex < 0) { return false; }

    const chat = window.Alpine && window.Alpine.store("chat");
    if (!chat || !chat.turnAt) { return false; }

    if (absIndex >= (Number(chat.turnStart) || 0)) { return true; }

    const pageSize = chat.turnPageSize;
    chat.turnPageSize = 200;

    try {
        for (let guard = 0; guard < 100; guard += 1) {
            if (absIndex >= (Number(chat.turnStart) || 0)) { break; }
            if (!chat.hasMoreOlder) { break; }

            await chat.loadOlderTurns(false);
            await window.Alpine.nextTick();
        }
    } finally {
        chat.turnPageSize = pageSize;
    }

    return absIndex >= (Number(chat.turnStart) || 0);
}

/* total turns in the open chat: core keeps a window in memory, so the count is
 * totalTurns, never turnHistory.length (that is only what is loaded). */
function cpTurnTotal(chat) {
    if (!chat) { return 0; }
    const loaded = (Number(chat.turnStart) || 0) + (chat.turnHistory || []).length;
    return Math.max(Number(chat.totalTurns) || 0, loaded);
}

/*
 * chat-pro - frontend half of the message-row controls.
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
 */

const CHAT_PRO = {
    forking: false,
    busy: false,

    hasTree: false,
    variants: {},
    indexIds: {},
    nodes: [],
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

    // right panel (chat-pro/chat/panel.html). shell only for now; the tab is
    // remembered per browser so reopening lands where the user left it.
    panelOpen: false,
    panelTab: "branches",
    _headerObserver: null,

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

    setPanelTab(tab) {
        const known = ["branches", "search", "links", "tasks"];
        this.panelTab = known.includes(tab) ? tab : "branches";
        try { localStorage.setItem("chatProPanelTab", this.panelTab); } catch (error) { /* private mode */ }
    },

    restorePanel() {
        try {
            this.panelOpen = localStorage.getItem("chatProPanelOpen") === "1";
            const tab = localStorage.getItem("chatProPanelTab");
            if (["branches", "search", "links", "tasks"].includes(tab)) { this.panelTab = tab; }
        } catch (error) { /* private mode */ }

        this._applyPanelLayout(this.panelOpen);
    },

    /* ----------------
     * branch state
     * ----------------- */
    info(turn) {
        const index = turn ? turn.first_message_index : null;
        const id = (index === null || index === undefined)
            ? null
            : (this.indexIds[index] ?? this.indexIds[String(index)] ?? null);

        const variant = id ? this.variants[id] : null;

        if (!variant) {
            return { id: id || null, position: 1, total: 1, siblings: null };
        }

        return {
            id: id,
            position: variant.position,
            total: variant.total,
            siblings: variant.siblings
        };
    },

    signature(chat) {
        const turns = chat.turnHistory || [];
        const last = turns[turns.length - 1];
        const tail = last ? (last.hash || (last.live ? "live" : "")) : "";
        return `${chat.selectedChat}|${turns.length}|${tail}|${Alpine.store("stream").state}`;
    },

    async refresh(force = false) {
        const chat = Alpine.store("chat");
        if (!chat.selectedChat) { return; }

        const signature = this.signature(chat);
        if (!force && signature === this._signature) { return; }
        this._signature = signature;

        try {
            const state = await simpleApiFetch("/api/ext/chat-pro/state");
            if (!state) { return; }

            this.chatId = state.chat_id || chat.selectedChat;
            this.hasTree = Boolean(state.has_tree);
            this.variants = state.variants || {};
            this.indexIds = state.index_ids || {};
            this.nodes = state.nodes || [];
            this.active = state.active || [];

            // a confirmation belongs to one node of one chat: drop it when that
            // node is gone (pruned elsewhere, or a different chat was loaded)
            if (this.confirming && !this.nodes.some((node) => node.id === this.confirming)) {
                this.confirming = null;
            }

            // a pending fork is tied to one chat and one turn count: if either
            // moved, its message index would point somewhere else now
            if (this.forkConfirm && (this.forkConfirm.chat !== chat.selectedChat ||
                    this.forkConfirm.turns !== (chat.turnHistory || []).length)) {
                this.forkConfirm = null;
            }
        } catch (error) {
            // the module may be disabled mid-session: stay quiet, keep the row clean
            console.warn("chat-pro: state refresh failed:", error);
        }
    },

    /* ----------------
     * navigation / branching
     * ----------------- */
    async prev(turn) {
        return this.step(turn, -1);
    },

    async next(turn, turnIndex) {
        const info = this.info(turn);

        // walking forward through existing variants, or starting a new one
        if (info.total > 1 && info.position < info.total) {
            return this.step(turn, 1);
        }

        return this.branch(turn, turnIndex);
    },

    async step(turn, delta) {
        if (this.busy) { return; }
        const info = this.info(turn);
        if (!info.siblings || info.total < 2) { return; }

        const index = info.position - 1 + delta;
        if (index < 0 || index >= info.siblings.length) { return; }

        return this._applyNode(info.siblings[index]);
    },

    async goto(nodeId) {
        if (!nodeId) { return; }
        return this._applyNode(nodeId);
    },

    async _applyNode(nodeId) {
        if (this.busy) { return; }
        if (Alpine.store("stream").state !== "idle") { return; }

        this.busy = true;
        try {
            await simpleApiPost("/api/ext/chat-pro/switch", { node: nodeId });
            // the backend broadcasts sync: the chat reloads from it, and the poll
            // refreshes the counters once the new history is in place
            this._signature = null;
        } catch (error) {
            this._fail("Switching branch failed", error);
        } finally {
            this.busy = false;
        }
    },

    async branch(turn, turnIndex) {
        if (this.busy) { return; }
        if (Alpine.store("stream").state !== "idle") { return; }

        if (turn.role === "user") {
            // rewrite the prompt as a new variant; saving is what creates it
            this._branchEdit = { index: turn.first_message_index, turnIndex };
            await Alpine.store("chat").startEdit(turnIndex);
            return;
        }

        this.busy = true;
        try {
            await simpleApiPost("/api/ext/chat-pro/branch", {
                mode: "regenerate",
                index: turn.first_message_index
            });
            this._signature = null;
        } catch (error) {
            this._fail("Starting a new variant failed", error);
        } finally {
            this.busy = false;
        }
    },

    async _saveBranchEdit(chat, pending) {
        // mirrors core's saveEdit(): only the text block the user typed is
        // rewritten, attachment blocks (and their names) stay as they are
        let content = chat.editContent;
        let filenames = chat.editBlocks ? [...(chat.editFilenames || [])] : null;

        if (chat.editBlocks) {
            let replaced = false;
            content = chat.editBlocks.map((block, i) => {
                if (!replaced && block && block.type === "text" && !chat.editFilenames[i]) {
                    replaced = true;
                    return { ...block, text: chat.editContent };
                }
                return block;
            });

            if (!replaced && chat.editContent) {
                content = [{ type: "text", text: chat.editContent }, ...content];
                filenames = ["", ...(filenames || [])];
            }
        }

        const files = (chat.editFiles || []).length
            ? await Promise.all(chat.editFiles.map(async (file) => ({
                  name: file.name,
                  data: await Alpine.store("upload").readFileAsBase64(file)
              })))
            : null;

        this.busy = true;
        try {
            await simpleApiPost("/api/ext/chat-pro/branch", {
                mode: "edit",
                index: pending.index,
                content: content,
                filenames: filenames,
                files: files
            });
            this._signature = null;
        } catch (error) {
            this._fail("Turning this message into a new variant failed", error);
        } finally {
            this.busy = false;
            chat.editingMessageIndex = null;
            chat.editContent = "";
            chat.editFiles = [];
        }
    },

    /* ----------------
     * pruning (the branches panel)
     * ----------------- */
    // a native confirm() is not trustworthy here: the CDP-driven browser this
    // project is tested in auto-accepts dialogs, so a destructive click went
    // through unguarded. ask -> confirm bar -> explicit Delete instead.
    askPrune(node) {
        if (this.busy) { return; }
        if (Alpine.store("stream").state !== "idle") { return; }
        this.confirming = node && node.id ? node.id : null;
    },

    cancelPrune() {
        this.confirming = null;
    },

    async confirmPrune(node) {
        if (this.busy || !node) { return; }
        if (Alpine.store("stream").state !== "idle") { return; }

        this.busy = true;
        this.confirming = null;
        try {
            await simpleApiPost("/api/ext/chat-pro/prune", { node: node.id });
            // the route changes the tree without broadcasting sync, so the panel
            // is stale until it re-reads the state itself
            this._signature = null;
            await this.refresh(true);
        } catch (error) {
            this._fail("Deleting the variant failed", error);
        } finally {
            this.busy = false;
        }
    },

    /* ----------------
     * fork
     * ----------------- */
    // clicking the fork icon only *asks*: the prompt is the banner above the
    // input bar (chat/index.html), mirroring libreta's in-app confirmations
    fork(turnIndex) {
        if (this.forking) { return; }
        if (Alpine.store("stream").state !== "idle") { return; }

        const chat = Alpine.store("chat");
        const turn = (chat.turnHistory || [])[turnIndex];
        if (!turn) { return; }

        const index = turn.last_message_index ?? turn.first_message_index;
        if (index === null || index === undefined) { return; }

        const last = turn.messages && turn.messages.length
            ? turn.messages[turn.messages.length - 1]
            : null;

        this.forkConfirm = {
            turnIndex: turnIndex,
            index: index,
            chat: chat.selectedChat,
            turns: (chat.turnHistory || []).length,
            role: turn.role,
            label: this._previewOf(last)
        };

        // the modal template lives in webui/templates/modals/chatpro_fork.html
        Alpine.store("ui").openModal("chatpro_fork");
    },

    cancelFork() {
        this.forkConfirm = null;
        const ui = Alpine.store("ui");
        if (ui.currentModal === "chatpro_fork") { ui.closeModal(); }
    },

    async confirmFork() {
        const pending = this.forkConfirm;
        if (!pending || this.forking) { return; }
        if (Alpine.store("stream").state !== "idle") { return; }

        this.forkConfirm = null;
        this.forking = true;

        const ui = Alpine.store("ui");
        if (ui.currentModal === "chatpro_fork") { ui.closeModal(); }

        const chat = Alpine.store("chat");
        try {
            const result = await simpleApiPost("/api/ext/chat-pro/fork", { index: pending.index });
            if (!result || !result.id) { return; }

            // the backend broadcasts chat_switched as well: if that already
            // switched us, loadChat is a no-op, otherwise it switches here
            await chat.reloadChats();
            if (chat.selectedChat !== result.id) {
                await chat.loadChat(result.id);
            }
        } catch (error) {
            this._fail("Fork failed", error);
        } finally {
            this.forking = false;
        }
    },

    _previewOf(message) {
        if (!message) { return ""; }

        let text = message.content;
        if (Array.isArray(text)) {
            text = text
                .filter((block) => block && block.type === "text" && block.text)
                .map((block) => block.text)
                .join(" ");
        }

        text = String(text || "").replace(/\s+/g, " ").trim();
        if (!text) { text = message.reasoning_content ? "[reasoning]" : "[message]"; }

        return text.length > 72 ? `${text.slice(0, 71)}\u2026` : text;
    },

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
};

document.addEventListener("alpine:init", () => {
    Alpine.store("chatPro", CHAT_PRO);
    CHAT_PRO.restorePanel();
    setTimeout(() => CHAT_PRO.patch(), 0);
});

document.addEventListener("alpine:initialized", () => {
    CHAT_PRO.patch();
    // the header is fully rendered by now: settle the panel's top offset
    CHAT_PRO._applyPanelLayout(CHAT_PRO.panelOpen);
    // one right dock at a time (delegate's store is public; absent when off)
    CHAT_PRO.watchDelegate();
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

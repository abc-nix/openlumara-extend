/*
 * chat-pro - frontend half of the message-row controls.
 *
 *   fork  (leftmost button)  -> a new chat, a full copy up to that message
 *   branch (rightmost)       -> `>` starts a variant inside this chat, `< n/m >`
 *                               walks the siblings of a message
 *
 * both are backend jobs (they write a chat / a tree file), so this store asks
 * the module's routes and then lets the core switch chats and re-render. the
 * branch tree itself (nodes, active path) is kept here for the panel and the
 * counters; `refresh()` is idempotent and cheap while nothing changes.
 */

const CHAT_PRO = {
    forking: false,
    busy: false,

    hasTree: false,
    variants: {},
    nodes: [],
    active: [],
    chatId: null,

    _signature: null,
    _branchEdit: null,

    /* ----------------
     * branch state
     * ----------------- */
    info(turn) {
        const first = (turn && turn.messages && turn.messages[0]) || null;
        const id = first && first._metadata ? first._metadata.chatpro_id : null;
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

    async refresh(force = false) {
        const chat = Alpine.store("chat");
        const id = chat.selectedChat;
        if (!id) { return; }

        // refetch only when the open chat or its number of turns moved
        const signature = `${id}:${(chat.turnHistory || []).length}`;
        if (!force && signature === this._signature) { return; }
        this._signature = signature;

        try {
            const state = await simpleApiFetch("/api/ext/chat-pro/state");
            if (!state) { return; }

            this.chatId = state.chat_id || id;
            this.hasTree = Boolean(state.has_tree);
            this.variants = state.variants || {};
            this.nodes = state.nodes || [];
            this.active = state.active || [];
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
            // the backend broadcasts sync as well: the chat rebuilds from that
            this._signature = null;
            await this.refresh(true);
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
     * fork
     * ----------------- */
    async fork(turnIndex) {
        if (this.forking) { return; }

        const chat = Alpine.store("chat");
        const stream = Alpine.store("stream");
        const turn = (chat.turnHistory || [])[turnIndex];

        if (!turn) { return; }
        if (stream.state !== "idle") { return; }

        const index = turn.last_message_index ?? turn.first_message_index;
        if (index === null || index === undefined) { return; }

        const kept = turn.role === "user"
            ? "your message (the fork starts with it unanswered)"
            : "this reply";

        if (!confirm(
            `Fork from ${kept}?\n\n` +
            "A new chat gets a copy of the conversation up to this point, " +
            "with the same metadata and attachments. " +
            "The chat you are in stays exactly as it is."
        )) { return; }

        this.forking = true;
        try {
            const result = await simpleApiPost("/api/ext/chat-pro/fork", { index });
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

    /* ----------------
     * shared bits
     * ----------------- */
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

    const chat = Alpine.store("chat");
    if (chat && typeof chat.saveEdit === "function") {
        // an edit started by `> ` on a user message is a *new variant*: saving it
        // goes to the branch route instead of message_edit, so the original
        // prompt (and everything under it) stays intact as another variant
        const originalSave = chat.saveEdit.bind(chat);
        chat.saveEdit = async function (index) {
            const store = Alpine.store("chatPro");
            if (!store._branchEdit) { return originalSave(index); }

            const pending = store._branchEdit;
            store._branchEdit = null;
            return store._saveBranchEdit(chat, pending);
        };

        const originalCancel = chat.cancelEdit.bind(chat);
        chat.cancelEdit = function () {
            Alpine.store("chatPro")._branchEdit = null;
            return originalCancel();
        };
    }

    // counters are refreshed when the open chat or its turn count moves; the
    // poll only refetches when the signature changed, so it is nearly free
    setInterval(() => {
        try { CHAT_PRO.refresh(false); } catch (error) { /* ignore */ }
    }, 1500);
});

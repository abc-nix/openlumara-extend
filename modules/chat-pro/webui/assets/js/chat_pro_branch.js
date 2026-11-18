/* chat-pro - branch counters, navigation, prune and the fork modal.

   Registers onto CHAT_PRO via chatPart(); chat_pro.js is loaded first (alphabetically,
   and '.' sorts before '_') and owns the object + the alpine:init registration, so the
   order among the other files does not matter. See chat_pro.js for the file map. */
chatPart({
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
            // absolute turn index per live message, plus the true turn count: the
            // state route groups the whole history, the client only holds a window
            this.turnIndex = state.turn_index || {};
            this.turnTotal = Number(state.turn_total) || 0;
            this.branches = state.branches || [];
            this.branchCount = this.branches.filter((row) => row.kind === "branch").length;
            this.branchPoints = state.branch_points || 0;
            this.active = state.active || [];

            // a confirmation belongs to one branch of one chat: drop it when that
            // branch is gone (pruned elsewhere, or a different chat was loaded)
            if (this.confirming && !this.branches.some((row) => row.id === this.confirming)) {
                this.confirming = null;
            }

            // a pending fork is tied to one chat and one turn count: if either
            // moved, its message index would point somewhere else now
            if (this.forkConfirm && (this.forkConfirm.chat !== chat.selectedChat ||
                    this.forkConfirm.turns !== cpTurnTotal(chat))) {
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
            const chat = Alpine.store("chat");
            const beforeTotal = chat.totalTurns;
            const beforeStart = chat.turnStart;

            await simpleApiPost("/api/ext/chat-pro/switch", { node: nodeId });

            // the route broadcasts "sync" BEFORE it answers, so core's sync handler
            // is already running resyncWindow() by the time this resumes. that
            // re-fetch was started from the OLD turnStart (before = turnStart +
            // size) and REPLACES the window when it lands - measured: a fresh tail
            // of 205..209 got overwritten by the stale-shaped 9..208, because the
            // slice ends before the newest turn and the chat then stops short of
            // its own end (trailing blocks with no data, bottom not the real
            // bottom) until a hard reload.
            //
            // so: let that write land first (it ends by updating totalTurns), then
            // rebuild the window from the newest page. correct for a longer and a
            // shorter branch alike.
            for (let guard = 0; guard < 40; guard += 1) {
                if (chat.totalTurns !== beforeTotal || chat.turnStart !== beforeStart) { break; }
                await new Promise((resolve) => setTimeout(resolve, 50));
            }

            chat.resetWindow();
            await chat.reloadChat();

            // a node's turn index is only meaningful against the NEW history, so
            // re-read the state before resolving it, then page the turn in and
            // land on the message the user picked instead of the middle of nowhere
            this._signature = null;
            await this.refresh(true);

            const turn = this._turnIndexFor(nodeId);
            if (turn >= 0) {
                await cpEnsureTurnLoaded(turn);
                await Alpine.nextTick();
                await Alpine.store("ui").scrollToTurn(turn);
                await this._graphAlignTurn(turn);
            }
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
    // clicking the fork icon only *asks*: the prompt is the modal in
    // chat-pro/modals/chatpro_fork.html, same rule as libreta's in-app confirms
    fork(turnIndex) {
        if (this.forking) { return; }
        if (Alpine.store("stream").state !== "idle") { return; }

        const chat = Alpine.store("chat");
        // turnIndex arrives absolute (the message buttons pass data-turn-index),
        // so resolve through turnAt() instead of indexing the loaded window
        const turn = chat.turnAt(turnIndex);
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
            turns: cpTurnTotal(chat),
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
});

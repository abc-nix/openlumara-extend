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
    refsQuery: "",
    refKind: "all",        // "all" | "url" | "file"
    refShowContent: true,  // references the message text mentions
    refShowTools: false,   // references tool output - off by default, it is noise
    refShowReasoning: false, // references mentioned while thinking

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
                    this.forkConfirm.turns !== (chat.turnHistory || []).length)) {
                this.forkConfirm = null;
            }
        } catch (error) {
            // the module may be disabled mid-session: stay quiet, keep the row clean
            console.warn("chat-pro: state refresh failed:", error);
        }
    },

    /* ----------------
     * references (urls + file paths)
     * ----------------- */
    // scanned from the history the client already holds - no route, works on
    // whatever branch is open. the refresh button re-runs it.
    scanRefs() {
        const chat = Alpine.store("chat");
        const parts = [];

        (chat.turnHistory || []).forEach((turn) => {
            const base = turn.first_message_index || 0;
            (turn.messages || []).forEach((message, offset) => {
                const index = base + offset;
                // the answer is what the user and the final assistant message
                // say. everything else - tool output, tool_calls arguments,
                // intermediate narration and reasoning - is process
                const isFinal = message.role === "assistant" && !(message.tool_calls || []).length;
                const source = (message.role === "user" || isFinal) ? "content" : "tools";
                const push = (value) => {
                    if (value) parts.push({ value: String(value), source, index });
                };

                const content = message.content;
                if (typeof content === "string") {
                    push(content);
                } else if (Array.isArray(content)) {
                    content.forEach((block) => {
                        if (block && block.type === "text" && block.text) push(block.text);
                    });
                }
                if (Array.isArray(message.tool_calls)) {
                    message.tool_calls.forEach((call) => {
                        const args = call && call.function ? call.function.arguments : null;
                        if (args) push(typeof args === "string" ? args : JSON.stringify(args));
                    });
                }
                if (message.reasoning_content) {
                    // thinking has its own toggle: never the answer's text, and
                    // kept apart from tool output as well
                    parts.push({ value: String(message.reasoning_content), source: "reasoning", index });
                }
            });
        });

        // the below_message anchors exist for user messages and assistant
        // finals only; a tool step cannot be addressed, so it must not pretend
        const anchorable = new Set();
        (chat.turnHistory || []).forEach((turn) => {
            const base = turn.first_message_index || 0;
            (turn.messages || []).forEach((message, offset) => {
                const final = message.role === "assistant" && !(message.tool_calls || []).length;
                if (message.role === "user" || final) anchorable.add(base + offset);
            });
        });

        const found = new Map();
        const bs = String.fromCharCode(92);
        const cleanRef = (value) => {
            let out = String(value);
            ["n", "r", "t"].forEach((ch) => { out = out.split(bs + ch).join(""); });
            while (out.endsWith(bs)) out = out.slice(0, -1);
            return out;
        };

        // hits: the message indexes this reference was seen in, so a row can jump
        const add = (kind, value, source, index) => {
            const clean = cleanRef(value || "");
            if (!clean) return;
            const key = kind + ":" + clean;
            const row = found.get(key) || {
                kind, value: clean, count: 0,
                sources: { content: false, tools: false, reasoning: false },
                hits: []
            };
            row.count += 1;
            row.sources[source] = true;
            if (!row.hits.includes(index)) row.hits.push(index);
            found.set(key, row);
        };

        const roots = ["/home/", "/usr/", "/etc/", "/tmp/", "/opt/", "/var/", "/mnt/",
            "/media/", "/srv/", "/root/", "/proc/", "/dev/"];
        const urlValues = [];

        parts.forEach((part) => {
            const text = part.value;

            (text.match(/https?:\/\/[^\s<>"'`)\]]+/g) || []).forEach((raw) => {
                const value = cleanRef(raw).replace(/[.,;:!?]+$/, "");
                add("url", value, part.source, part.index);
                urlValues.push(value);
            });

            // a slice of an address is not a file: .../wiki/Character.ai is a page.
            // an absolute path must start at a real root, and // is a protocol.
            (text.match(/(?:~\/|\/|\.\/)?[\w.-]+(?:\/[\w.-]+)+\.[A-Za-z0-9]{1,6}\b/g) || []).forEach((raw) => {
                if (raw.startsWith(".../") || raw.startsWith("\u2026/")) return;
                if (raw.includes("://") || raw.startsWith("//")) return;
                if (raw.startsWith("/") && !roots.some((root) => raw.startsWith(root))) return;
                if (urlValues.some((url) => url.includes(raw))) return;
                add("file", raw.replace(/^\.\//, ""), part.source, part.index);
            });

            // bare names with a known extension, unless a path already covers them
            (text.match(/\b[\w-]+\.(?:py|md|markdown|json|ya?ml|toml|ini|cfg|js|css|html|txt|sh|log)\b/g) || []).forEach((raw) => {
                const covered = [...found.values()].some((row) => row.kind === "file" && row.value.endsWith("/" + raw));
                if (covered || urlValues.some((url) => url.includes(raw))) return;
                add("file", raw, part.source, part.index);
            });
        });

        for (const row of found.values()) {
            row.jumpHits = row.hits.filter((index) => anchorable.has(index));
        }

        this.refs = [...found.values()].sort((a, b) => {
            if (a.kind !== b.kind) return a.kind.localeCompare(b.kind);
            return a.value.localeCompare(b.value);
        });
    },

    // jump to the message that mentions a reference; repeated clicks walk its
    // occurrences. scrollToTurn mounts lazy turns, so the flash waits for it.
    async jumpToRef(row) {
        const hits = (row && row.jumpHits) || [];
        if (!hits.length) return;

        row._cursor = ((row._cursor ?? -1) + 1) % hits.length;
        const messageIndex = hits[row._cursor];

        const turns = Alpine.store("chat").turnHistory || [];
        const turnIndex = turns.findIndex((turn) => {
            const first = turn.first_message_index || 0;
            const last = first + Math.max(1, (turn.messages || []).length) - 1;
            return messageIndex >= first && messageIndex <= last;
        });
        if (turnIndex < 0) return;

        await Alpine.store("ui").scrollToTurn(turnIndex);

        // land on the message, not its turn: one turn can hold dozens of steps and
        // both mentions may live in the same one. the anchor comes from
        // chat/turns/*.html (the below_message slot), bound to message.index.
        const anchor = document.querySelector('#messages [data-cp-msg="' + messageIndex + '"]');
        let element = null;

        if (anchor) {
            const target = anchor.closest(".message-wrapper") || anchor.parentElement;
            if (target) {
                target.scrollIntoView({ block: "center" });
                element = target;
            }
        }
        if (!element) element = document.querySelector('#messages [data-turn-index="' + turnIndex + '"]');
        if (!element) return;

        element.classList.add("cp-flash");
        setTimeout(() => element.classList.remove("cp-flash"), 1200);
    },

    restoreRefPrefs() {
        try {
            const content = localStorage.getItem("chatProRefContent");
            const tools = localStorage.getItem("chatProRefTools");
            const reasoning = localStorage.getItem("chatProRefReasoning");
            if (content !== null) this.refShowContent = content === "1";
            if (tools !== null) this.refShowTools = tools === "1";
            if (reasoning !== null) this.refShowReasoning = reasoning === "1";
        } catch (error) { /* private mode */ }
    },

    toggleRefSource(which) {
        if (which === "tools") this.refShowTools = !this.refShowTools;
        else if (which === "reasoning") this.refShowReasoning = !this.refShowReasoning;
        else this.refShowContent = !this.refShowContent;
        try {
            localStorage.setItem("chatProRefContent", this.refShowContent ? "1" : "0");
            localStorage.setItem("chatProRefTools", this.refShowTools ? "1" : "0");
            localStorage.setItem("chatProRefReasoning", this.refShowReasoning ? "1" : "0");
        } catch (error) { /* private mode */ }
    },

    filteredRefs() {
        const query = (this.refsQuery || "").trim().toLowerCase();
        return this.refs.filter((row) => {
            if (this.refKind !== "all" && row.kind !== this.refKind) return false;
            const sources = row.sources || { content: true, tools: false, reasoning: false };
            const shown = (sources.content && this.refShowContent)
                || (sources.tools && this.refShowTools)
                || (sources.reasoning && this.refShowReasoning);
            if (!shown) return false;
            return !query || row.value.toLowerCase().includes(query);
        });
    },

    openRef(row) {
        if (!row) return;
        if (row.kind === "url") {
            window.open(row.value, "_blank", "noopener");
            return;
        }
        this.openFileRef(row.value);
    },

    // what a file reference does is a setting; it reaches js through data
    // attributes on .cp-cover, because module settings are jinja-only
    _fileOpenConfig() {
        const cover = document.querySelector(".cp-cover");
        const data = cover ? cover.dataset : {};
        return {
            action: data.fileOpen || "copy",
            module: data.fileOpenModule || "libreta",
            command: data.fileOpenCommand || "xdg-open {path}"
        };
    },

    async openFileRef(value) {
        const config = this._fileOpenConfig();
        if (config.action === "copy") return this.copyRef(value);

        // only offer to open what exists: a chat quotes paths relative to
        // wherever the file lived, and most of them do not resolve here.
        const resolved = await this._resolveFile(value, config.module);
        if (resolved === false) {
            await this.copyRef(value);   // its own notice would overwrite ours
            this._notice(`File not found - copied ${value}`, 4000);
            return;
        }

        if (config.action === "command") return this._runFileCommand(config.command, value, resolved);
        return this._openFileWithModule(config.module, value, resolved);
    },

    // three answers on purpose: an object when it resolves, false when the
    // module says it does not exist, null when there is no find route to ask
    async _resolveFile(path, moduleName) {
        try {
            const found = await simpleApiFetch(`/api/ext/${moduleName}/find?path=${encodeURIComponent(path)}`);
            return found && found.found ? found : false;
        } catch (error) {
            return null;
        }
    },

    _shellQuote(value) {
        const bs = String.fromCharCode(92);
        const safe = String(value).split('"').join(bs + '"')
            .split("$").join(bs + "$").split("`").join(bs + "`");
        return '"' + safe + '"';
    },

    async _runFileCommand(template, path, resolved) {
        // a command wants a real path, so prefer the resolved absolute one
        const target = resolved && resolved.root ? `${resolved.root}/${resolved.path}` : path;
        const command = String(template || "").replaceAll("{path}", this._shellQuote(target)).trim();
        if (!command) return this.copyRef(target);

        try {
            const result = await simpleApiPost("/api/ext/shell/run", { command });
            const failed = result && (result.success === false || result.status === "error");
            this._notice(failed ? `Command failed: ${command}` : `Ran ${command}`);
        } catch (error) {
            // simpleApiPost throws the server's message: show why, not just that
            this._notice(`Command failed: ${command} - ${String(error).slice(0, 90)}`);
        }
    },

    async _openFileWithModule(name, path, resolved) {
        try {
            const payload = resolved ? { path: resolved.path, workspace: resolved.workspace } : { path };
            const result = await simpleApiPost(`/api/ext/${name}/open`, payload);
            const workspace = (result && result.workspace) || (resolved && resolved.workspace);
            const rel = (result && result.path) || (resolved && resolved.path);

            let view = null;
            try { view = Alpine.store(`${name}View`); } catch (error) { /* module has no store */ }

            if (view && typeof view.open === "function" && workspace) {
                await view.open(workspace, rel);
                this._notice(`Opened ${rel}`);
                return;
            }
            this._notice(workspace ? `Resolved to ${workspace}/${rel}` : `Could not open ${path}`);
        } catch (error) {
            this._notice(`Could not open ${path} in ${name}: ${String(error).slice(0, 90)}`);
        }
    },

    _notice(text, ms = 4000) {
        const ui = Alpine.store("ui");
        ui.notice = text;
        setTimeout(() => { if (ui.notice === text) ui.notice = null; }, ms);
    },

    // a path is not clickable on its own yet (no editor route wired): copying it
    // is the honest action; opening it in libreta is the next step.
    async copyRef(value) {
        const ui = Alpine.store("ui");
        try {
            await navigator.clipboard.writeText(value);
            ui.notice = `Copied ${value}`;
        } catch (error) {
            ui.notice = value;
        }
        setTimeout(() => { if (ui.notice && ui.notice.includes(value)) ui.notice = null; }, 4000);
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
    // clicking the fork icon only *asks*: the prompt is the modal in
    // chat-pro/modals/chatpro_fork.html, same rule as libreta's in-app confirms
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
     * graph canvas (modals/chatpro_graph.html)
     * ----------------- */
    // a tidy top-down tree: leaves sit in a row, a parent is centred over its
    // children. cards are html (so the preview can line-clamp), edges are one
    // svg, and both live in a single transform so pan/zoom stay in sync.
    async openGraph() {
        if (this.graphOpen || !Alpine.store("chat").selectedChat) { return; }

        this.graphOpen = true;
        this.graphLoading = true;
        this.graphNodes = [];
        this.graphActive = [];
        this.graphStats = { nodes: 0, branches: 0 };
        this._miniCache = null;   // a turn's text may have changed since last open
        this._restoreRailPrefs();
        Alpine.store("ui").openModal("chatpro_graph");
        await Alpine.nextTick();

        try {
            const data = await simpleApiFetch("/api/ext/chat-pro/graph");
            this.graphNodes = (data && data.nodes) || [];
            this.graphActive = (data && data.active) || [];
            this.graphStats = (data && data.stats) || { nodes: 0, branches: 0 };
        } catch (error) {
            this._fail("Loading the branch graph failed", error);
        } finally {
            this.graphLoading = false;
            await Alpine.nextTick();
            if (!this.graphOpen) { return; }

            this._graphLayout();
            this._graphDraw();
            this._graphBind();

            // a small tree opens as a whole; a long one opens at the current node,
            // because fitting 113 levels shrinks the text out of readability
            const viewport = document.getElementById("cp-graph-viewport");
            const g = this._graphData;
            const current = this.graphActive[this.graphActive.length - 1];
            const fit = (viewport && g)
                ? Math.min(viewport.clientWidth / g.width, viewport.clientHeight / g.height, 1) * 0.94
                : 1;

            if (current && fit < 0.55) { this._graphFocus(current); } else { this.fitGraph(); }
        }
    },

    closeGraph() {
        this._miniHide();
        this.graphOpen = false;
        const ui = Alpine.store("ui");
        if (ui.currentModal === "chatpro_graph") { ui.closeModal(); }
    },

    _graphLayout() {
        const cardW = 160;
        const cardH = 88;
        const gapX = 34;
        const rowH = 136;

        const byId = new Map();
        this.graphNodes.forEach((node) => byId.set(node.id, node));

        const kids = new Map();
        this.graphNodes.forEach((node) => {
            if (node.parent && byId.has(node.parent)) {
                if (!kids.has(node.parent)) { kids.set(node.parent, []); }
                kids.get(node.parent).push(node);
            }
        });
        kids.forEach((list) => list.sort((a, b) => a.seq - b.seq));

        const roots = this.graphNodes
            .filter((node) => !node.parent || !byId.has(node.parent))
            .sort((a, b) => a.seq - b.seq);

        const depth = new Map();
        const walk = (node, level) => {
            depth.set(node.id, level);
            (kids.get(node.id) || []).forEach((kid) => walk(kid, level + 1));
        };
        roots.forEach((root) => walk(root, 0));

        // a node's slot is as wide as its own subtree, so leaves never overlap
        const span = new Map();
        const measure = (node) => {
            const list = kids.get(node.id) || [];
            if (!list.length) { span.set(node.id, cardW); return cardW; }

            let total = 0;
            list.forEach((kid, index) => {
                total += measure(kid);
                if (index) { total += gapX; }
            });
            span.set(node.id, Math.max(cardW, total));
            return span.get(node.id);
        };
        roots.forEach((root) => measure(root));

        const x = new Map();
        const place = (node, left) => {
            const list = kids.get(node.id) || [];
            if (!list.length) { x.set(node.id, left + (span.get(node.id) - cardW) / 2); return; }

            let cursor = left;
            list.forEach((kid) => {
                place(kid, cursor);
                cursor += span.get(kid.id) + gapX;
            });

            const first = x.get(list[0].id) + cardW / 2;
            const last = x.get(list[list.length - 1].id) + cardW / 2;
            x.set(node.id, (first + last) / 2 - cardW / 2);
        };

        let cursor = 0;
        roots.forEach((root) => {
            place(root, cursor);
            cursor += span.get(root.id) + gapX * 2;
        });

        let maxDepth = 0;
        depth.forEach((level) => { maxDepth = Math.max(maxDepth, level); });

        let minX = 0;
        let maxX = cardW;
        x.forEach((value) => { minX = Math.min(minX, value); maxX = Math.max(maxX, value + cardW); });

        this._graphData = {
            byId,
            kids,
            depth,
            x,
            cardW,
            cardH,
            rowH,
            width: maxX - Math.min(0, minX) + 80,
            height: (maxDepth + 1) * rowH + 40,
            offsetX: -Math.min(0, minX) + 40,
        };
    },

    // which turn sits at the top of the chat viewport: the cards can't follow a
    // scroll that happens behind the modal, so it is sampled on every draw (the
    // modal opens over a chat that is already positioned)
    _graphViewingTurn() {
        const container = document.getElementById("messages");
        if (!container) { return -1; }

        const top = container.getBoundingClientRect().top;
        let viewing = -1;
        container.querySelectorAll("[data-turn-index]").forEach((element) => {
            if (viewing >= 0) { return; }
            if (element.getBoundingClientRect().bottom > top + 8) {
                viewing = Number(element.dataset.turnIndex);
            }
        });
        return viewing;
    },

    // live message index -> turn index: core's turn starts are ascending, so the
    // last one at or before the message is the turn that contains it
    _graphTurnIndexForLive(messageIndex) {
        if (!(messageIndex >= 0)) { return -1; }

        const turns = Alpine.store("chat").turnHistory || [];
        let index = -1;
        turns.forEach((turn, position) => {
            if (turn.first_message_index !== undefined && turn.first_message_index <= messageIndex) {
                index = position;
            }
        });
        return index;
    },

    _graphDraw() {
        const viewport = document.getElementById("cp-graph-viewport");
        const stage = document.getElementById("cp-graph-stage");
        const g = this._graphData;
        if (!viewport || !stage || !g) { return; }

        stage.style.width = `${g.width}px`;
        stage.style.height = `${g.height}px`;

        // the landmark rail is derived from the same payload: fork points say so
        // through `children`, compressions through `compressed`
        this.graphRail = this._graphRailItems(g);

        const edges = [`<svg class="cp-graph-edges" width="${g.width}" height="${g.height}" aria-hidden="true">`];
        this.graphNodes.forEach((node) => {
            if (!node.parent || !g.byId.has(node.parent)) { return; }
            const px = g.x.get(node.parent) + g.offsetX + g.cardW / 2;
            const py = g.depth.get(node.parent) * g.rowH + g.cardH;
            const cx = g.x.get(node.id) + g.offsetX + g.cardW / 2;
            const cy = g.depth.get(node.id) * g.rowH;
            const bend = (cy - py) * 0.4;
            const cls = node.on_path ? "on-path" : "off-path";
            edges.push(`<path class="cp-edge ${cls}" d="M${px} ${py} C${px} ${py + bend} ${cx} ${cy - bend} ${cx} ${cy}"></path>`);
        });
        edges.push("</svg>");
        stage.innerHTML = edges.join("");

        // node id -> live message index, so a card can be matched to the turn the
        // chat is scrolled to (off-path cards have no live index at all)
        const liveIndexById = new Map();
        Object.entries(this.indexIds || {}).forEach(([index, id]) => liveIndexById.set(id, Number(index)));
        const viewingTurn = this._graphViewingTurn();

        this.graphNodes.forEach((node) => {
            const card = document.createElement("button");
            card.type = "button";
            card.className = `cp-gcard ${node.role === "user" ? "role-user" : "role-assistant"}` +
                (node.tool ? " tool" : "") + (node.compressed ? " compressed" : "") +
                (node.on_path ? " on-path" : " off-path");
            card.dataset.id = node.id;

            const liveIndex = liveIndexById.get(node.id);
            if (viewingTurn >= 0 &&
                    this._graphTurnIndexForLive(liveIndex === undefined ? -1 : liveIndex) === viewingTurn) {
                card.classList.add("is-viewing");
            }

            card.style.left = `${g.x.get(node.id) + g.offsetX}px`;
            card.style.top = `${g.depth.get(node.id) * g.rowH}px`;
            card.style.width = `${g.cardW}px`;
            card.style.height = `${g.cardH}px`;
            // one card per turn: the answer text, and how many steps it took
            // (tool calls, intermediate narration) when there was more than one
            const stepCount = Number(node.steps) || 0;
            const steps = node.compressed ? "context compressed" : (stepCount > 0 ? `${stepCount} ${stepCount === 1 ? "step" : "steps"}` : "");

            card.innerHTML =
                `<span class="cp-grole">${node.compressed ? "Summary" : (node.role === "user" ? "You" : (node.role === "tool" ? "Tool" : "Assistant"))}</span>` +
                `<span class="cp-gtime">${this._graphTime(node.time)}</span>` +
                `<span class="cp-gprev">${this._esc(node.preview || "")}</span>` +
                (steps ? `<span class="cp-gsteps">${steps}</span>` : "");

            // a fork point says so on the canvas too, so it can be spotted without
            // the rail (and at far zoom, where the text is gone)
            if (node.children > 1) {
                const fork = document.createElement("span");
                fork.className = "cp-gfork";
                fork.textContent = "\u2442" + (node.children > 2 ? node.children : "");
                fork.title = `${node.children} branches from here`;
                card.appendChild(fork);
            }

            stage.appendChild(card);
        });

        this._graphX = 0;
        this._graphY = 0;
        this._graphScale = 1;
        this._graphApply();
    },

    _graphApply() {
        const stage = document.getElementById("cp-graph-stage");
        const viewport = document.getElementById("cp-graph-viewport");
        if (!stage || !viewport) { return; }

        stage.style.transform = `translate(${this._graphX}px, ${this._graphY}px) scale(${this._graphScale})`;
        viewport.classList.toggle("lod-far", this._graphScale < 0.55);
    },

    // fork points and compression summaries, ordered like the canvas reads (top to
    // bottom: depth, then creation order), so the rail is a table of contents for a
    // tree too deep to fit on screen.
    _graphRailItems(g) {
        const branches = [];
        const compressions = [];

        (this.graphNodes || []).forEach((node) => {
            const isBranch = !node.compressed && node.children > 1;
            if (!node.compressed && !isBranch) { return; }

            const preview = node.preview || "";
            const item = {
                id: node.id,
                // one line fits in a 208px rail: shorten, keep the full text for the tooltip
                text: preview.length > 88 ? `${preview.slice(0, 87)}\u2026` : preview,
                full: preview,
                time: this._graphTime(node.time),
                depth: (g && g.depth.get(node.id)) || 0,
                variants: node.children || 0,
                onPath: Boolean(node.on_path),
                role: node.role,
            };

            (node.compressed ? compressions : branches).push(item);
        });

        const byCanvasOrder = (a, b) => (a.depth - b.depth) || a.text.localeCompare(b.text);
        return { branches: branches.sort(byCanvasOrder), compressions: compressions.sort(byCanvasOrder) };
    },

    _restoreRailPrefs() {
        try {
            this.graphRailOpen = {
                branches: localStorage.getItem("chatProRailBranches") !== "0",
                compressions: localStorage.getItem("chatProRailCompressions") !== "0",
            };
        } catch (error) { /* private mode: keep the defaults */ }
    },

    toggleRail(section) {
        const open = !this.graphRailOpen[section];
        this.graphRailOpen[section] = open;
        try {
            localStorage.setItem(section === "branches" ? "chatProRailBranches" : "chatProRailCompressions", open ? "1" : "0");
        } catch (error) { /* private mode */ }
    },

    // the rail moves the *camera*, the card moves the chat: panning to a landmark must
    // not switch the open branch, or one stray click would rewrite the path.
    graphFocusAt(nodeId) {
        if (!nodeId) { return; }
        const rail = document.querySelector(".cp-graph-rail");
        const inset = rail && rail.offsetWidth ? rail.offsetWidth + 16 : 0;
        this._graphFocus(nodeId, inset);

        const card = document.querySelector(`#cp-graph-stage .cp-gcard[data-id="${nodeId}"]`);
        if (!card) { return; }
        card.classList.add("is-focus");
        setTimeout(() => card.classList.remove("is-focus"), 1400);
    },

    // factor from the current scale; `at` (x/y inside the viewport) keeps the
    // point under the cursor still, so wheel-zoom feels anchored
    zoomGraph(factor, at) {
        const viewport = document.getElementById("cp-graph-viewport");
        if (!viewport || !this._graphData) { return; }

        const rect = viewport.getBoundingClientRect();
        const cx = at ? at.x : rect.width / 2;
        const cy = at ? at.y : rect.height / 2;
        const scale = Math.min(2, Math.max(0.08, this._graphScale * factor));
        const k = scale / this._graphScale;

        this._graphX = cx - (cx - this._graphX) * k;
        this._graphY = cy - (cy - this._graphY) * k;
        this._graphScale = scale;
        this._graphApply();
    },

    fitGraph() {
        const viewport = document.getElementById("cp-graph-viewport");
        const g = this._graphData;
        if (!viewport || !g) { return; }

        const rect = viewport.getBoundingClientRect();
        const scale = Math.min(rect.width / g.width, rect.height / g.height, 1) * 0.94;
        this._graphScale = Math.max(0.08, scale);
        this._graphX = (rect.width - g.width * this._graphScale) / 2;
        this._graphY = (rect.height - g.height * this._graphScale) / 2;
        this._graphApply();
    },

    // put one node in the middle at a readable zoom: the alternative to a
    // minimap is simply landing where the user is
    _graphFocus(nodeId, insetLeft = 0) {
        const viewport = document.getElementById("cp-graph-viewport");
        const g = this._graphData;
        if (!viewport || !g || !g.x.has(nodeId)) { this.fitGraph(); return; }

        const rect = viewport.getBoundingClientRect();
        this._graphScale = 0.9;
        // `insetLeft` keeps the card centred in the area the rail leaves free, so
        // focusing a landmark from the rail does not park it behind the rail
        const centreX = (rect.width + insetLeft) / 2;
        this._graphX = centreX - (g.x.get(nodeId) + g.offsetX + g.cardW / 2) * this._graphScale;
        this._graphY = rect.height / 2 - (g.depth.get(nodeId) * g.rowH + g.cardH / 2) * this._graphScale;
        this._graphApply();
    },

    _graphBind() {
        const viewport = document.getElementById("cp-graph-viewport");
        if (!viewport || viewport.dataset.bound) { return; }
        viewport.dataset.bound = "1";

        let drag = null;

        viewport.addEventListener("pointerdown", (event) => {
            if (event.target.closest(".cp-gcard") || event.target.closest(".cp-graph-zoom")
                    || event.target.closest(".cp-graph-rail")) { return; }
            this._miniHide();
            drag = { x: event.clientX, y: event.clientY, px: this._graphX, py: this._graphY };
            viewport.classList.add("panning");
            try { viewport.setPointerCapture(event.pointerId); } catch (error) { /* not fatal */ }
        });

        viewport.addEventListener("pointermove", (event) => {
            if (!drag) { return; }
            this._graphX = drag.px + (event.clientX - drag.x);
            this._graphY = drag.py + (event.clientY - drag.y);
            this._graphApply();
        });

        const endDrag = () => {
            drag = null;
            viewport.classList.remove("panning");
        };
        viewport.addEventListener("pointerup", endDrag);
        viewport.addEventListener("pointercancel", endDrag);

        viewport.addEventListener("wheel", (event) => {
            event.preventDefault();
            this._miniHide();
            const rect = viewport.getBoundingClientRect();
            this.zoomGraph(event.deltaY < 0 ? 1.12 : 0.89, {
                x: event.clientX - rect.left,
                y: event.clientY - rect.top
            });
        }, { passive: false });

        // hover miniature: the delay only exists to filter sweeps across the
        // tree, so it is paid once per card. 600ms before the first fetch, and
        // nearly none once the turn is cached - there is nothing to wait for.
        const hoverDelay = (id) => (this._miniCache && this._miniCache.has(id) ? 120 : 600);
        let hoverTimer = null;
        let hoverId = null;

        viewport.addEventListener("mouseover", (event) => {
            const card = event.target.closest(".cp-gcard");
            if (!card || card.dataset.id === hoverId) { return; }

            hoverId = card.dataset.id;
            clearTimeout(hoverTimer);
            hoverTimer = setTimeout(() => { this._miniShow(card); }, hoverDelay(card.dataset.id));
        });

        viewport.addEventListener("mouseout", (event) => {
            const card = event.target.closest(".cp-gcard");
            if (!card) { return; }
            const next = event.relatedTarget;
            if (next && card.contains(next)) { return; }
            if (next && next.closest && next.closest(".cp-gcard")) { return; }

            hoverId = null;
            clearTimeout(hoverTimer);
            this._miniHide();
        });

        viewport.addEventListener("click", (event) => {
            const card = event.target.closest(".cp-gcard");
            if (card) { this.graphPick(card.dataset.id); }
        });
    },

    // the miniature: the turn's text is fetched once per card, then rendered by
    // the same markdown pipeline the chat uses, clamped by the box
    async _miniShow(card) {
        const id = card && card.dataset ? card.dataset.id : "";
        const box = document.getElementById("cp-graph-mini");
        if (!id || !box) { return; }

        this._miniCache = this._miniCache || new Map();
        let data = this._miniCache.get(id);

        if (!data) {
            box.innerHTML = `<div class="cp-mini-loading">loading…</div>`;
            this._miniPlace(card, box);
            try {
                data = await simpleApiFetch(`/api/ext/chat-pro/node?id=${encodeURIComponent(id)}`);
            } catch (error) {
                data = null;
            }
            if (!data) {
                this._miniHide();
                return;
            }
            this._miniCache.set(id, data);
        }

        const text = String(data.content || "").trim();
        const body = text
            ? (typeof renderMarkdown === "function" ? renderMarkdown(text) : this._esc(text))
            : "<p><em>no text in this turn</em></p>";

        box.innerHTML =
            `<div class="cp-mini-head">` +
            `<span class="cp-mini-role">${this._esc(data.role === "user" ? "You" : (data.role === "tool" ? "Tool" : "Assistant"))}</span>` +
            `<span class="cp-mini-time">${this._esc(this._graphTime(data.time))}</span>` +
            `</div><div class="cp-mini-body">${body}</div>`;

        this._miniPlace(card, box);
    },

    // next to the card, flipping to the other side and clamping to the window,
    // so a card near an edge never pushes the miniature off screen
    _miniPlace(card, box) {
        const rect = card.getBoundingClientRect();
        const width = 380;
        const margin = 12;

        box.style.width = `${width}px`;
        box.classList.add("open");
        const height = box.offsetHeight || 260;

        let left = rect.right + margin;
        if (left + width > window.innerWidth - margin) { left = rect.left - width - margin; }
        left = Math.max(margin, left);

        let top = rect.top;
        if (top + height > window.innerHeight - margin) { top = window.innerHeight - height - margin; }
        top = Math.max(margin, top);

        box.style.left = `${left}px`;
        box.style.top = `${top}px`;
    },

    _miniHide() {
        const box = document.getElementById("cp-graph-mini");
        if (box) { box.classList.remove("open"); }
    },

    async graphPick(nodeId) {
        if (!nodeId || this.busy) { return; }
        if (Alpine.store("stream").state !== "idle") { return; }

        const ui = Alpine.store("ui");
        const byId = (this._graphData && this._graphData.byId) || new Map();
        const card = byId.get(nodeId);

        // a card on the open path is already the timeline the chat is showing: no
        // switch, no reload, nothing to wait for - just navigate. (a switch there
        // rewrote the same path and raced the sync for no reason.)
        const onPath = Boolean(card && card.on_path);

        this.busy = true;
        try {
            if (onPath) {
                this.closeGraph();
                await this._graphGotoTurn(this._turnIndexFor(nodeId));
                return;
            }

            // off path: -1 is truthy on purpose. core's sync handler scrolls to
            // ui.scrollToTurnIndex after it replaces the history, and falls back to
            // forceScrollToBottom() when that value is falsy - so this intent stops
            // it from dragging the view to the bottom. scrollToTurn on a missing
            // element breaks immediately and clears the intent by itself.
            ui.scrollToTurnIndex = -1;
            await simpleApiPost("/api/ext/chat-pro/switch", { node: nodeId });
            this.closeGraph();

            // counters catch up on their own signature change
            this._signature = null;
            await this.refresh(true);

            // the switch rewrote the *server's* history: the client's copy arrives
            // with the sync broadcast and the dom with reloadChat(). wait for that
            // before measuring anything - the old dom belongs to the old path.
            await this._graphWaitForPath(nodeId);

            const turnIndex = this._turnIndexFor(nodeId);
            if (turnIndex >= 0) {
                // leave the correct target behind, so a sync that lands late scrolls
                // to the card instead of the bottom of the chat
                ui.scrollToTurnIndex = turnIndex;
                await this._graphGotoTurn(turnIndex);
            }
        } catch (error) {
            ui.scrollToTurnIndex = null;
            this._fail("Switching branch failed", error);
        } finally {
            this.busy = false;
        }
    },

    // the client turn a card belongs to, resolved through the ids the tree stamped
    // on every message (_metadata.chatpro_id) - never by counting cards.
    //
    // counting was the bug: core's group_history drops messages flagged
    // _metadata.internal (screenshots, injected data), while the graph still draws
    // a card for one *and* one for its child - 2 extra cards per screenshot, none of
    // them a turn. on a chat with 21 screenshots the tail card's depth ran 42 past
    // the last real turn, so the selector found no element, scrollToTurn broke on
    // its first pass and the view stayed at the top of the chat (the first message).
    _turnIndexFor(nodeId) {
        const turns = Alpine.store("chat").turnHistory || [];
        const byId = (this._graphData && this._graphData.byId) || new Map();

        let id = nodeId;
        for (let guard = 0; id && guard < 4096; guard += 1) {
            const index = turns.findIndex((turn) => (turn.messages || []).some(
                (message) => (((message && message._metadata) || {}).chatpro_id) === id
            ));
            if (index >= 0) { return index; }

            // a card whose message the chat never renders (internal) lands on the
            // nearest ancestor card that it does render
            const card = byId.get(id);
            id = card && card.parent ? card.parent : null;
        }
        return -1;
    },

    // the switch is written server-side; the client's turnHistory arrives with the
    // sync broadcast. wait until the node is actually in it before touching the dom.
    // a card the chat never renders never shows up: after the grace period reload
    // once, so whatever we resolve next is measured against the new path.
    async _graphWaitForPath(nodeId) {
        const chat = Alpine.store("chat");
        const present = () => (chat.turnHistory || []).some((turn) => (turn.messages || []).some(
            (message) => (((message && message._metadata) || {}).chatpro_id) === nodeId
        ));

        for (let i = 0; i < 30; i += 1) {
            if (present()) { return true; }
            await new Promise((resolve) => setTimeout(resolve, 50));
        }
        await chat.reloadChat();
        return present();
    },

    async _graphGotoTurn(turnIndex) {
        if (turnIndex < 0) { return; }
        await Alpine.store("ui").scrollToTurn(turnIndex);
        await this._graphAlignTurn(turnIndex);
    },

    // scrollToTurn centres the turn (and lazy-mounts whatever it passes); bring its
    // top to the top of the viewport afterwards, because centring a 45-message chain
    // shows the middle of the answer instead of where the card begins.
    //
    // this re-measures because a turn that mounts on the way in has a placeholder's
    // height before it renders, which shifts everything below it; the loop ends as
    // soon as it is within 4px or the scrollbar cannot move at all - the last turn of
    // a chat can never reach the top, its own height is shorter than the viewport
    // (e.g. a 865px tail turn stops 324px down, fully visible).
    async _graphAlignTurn(turnIndex) {
        const container = document.getElementById("messages");
        if (!container || turnIndex < 0) { return; }

        let previousTop = -1;
        for (let pass = 0; pass < 12; pass += 1) {
            const element = container.querySelector(`[data-turn-index="${turnIndex}"]`);
            if (!element) { return; }

            const offset = element.getBoundingClientRect().top - container.getBoundingClientRect().top;
            if (Math.abs(offset) < 4) { return; }
            if (container.scrollTop === previousTop) { return; }

            previousTop = container.scrollTop;
            container.scrollTo({ top: container.scrollTop + offset, behavior: "instant" });
            await new Promise((resolve) => setTimeout(resolve, 100));
        }
    },

    // a message preview can hold any text: never hand it to innerHTML raw
    _esc(text) {
        return String(text == null ? "" : text).replace(/[&<>"']/g, (ch) => ({
            "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
        }[ch]));
    },

    _graphTime(stamp) {
        if (!stamp) { return ""; }
        const date = new Date(stamp * 1000);
        if (Number.isNaN(date.getTime())) { return ""; }

        const hh = String(date.getHours()).padStart(2, "0");
        const mm = String(date.getMinutes()).padStart(2, "0");
        const day = String(date.getDate()).padStart(2, "0");
        const month = String(date.getMonth() + 1).padStart(2, "0");

        // graph_date_style reaches js as a data attribute on the window, like the
        // other module settings; auto keeps today's hh:mm and dates older cards
        const modal = document.querySelector(".cp-graph-modal");
        const style = (modal && modal.dataset.graphDate) || "auto";

        if (style === "day_month") { return `${day}/${month} ${hh}:${mm}`; }
        if (style === "month_day") { return `${month}/${day} ${hh}:${mm}`; }

        if (date.toDateString() === new Date().toDateString()) { return `${hh}:${mm}`; }
        return `${month}/${day} ${hh}:${mm}`;
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

/* chat-pro - the references tab: urls and file paths.

   Registers onto CHAT_PRO via chatPart(); chat_pro.js is loaded first (alphabetically,
   and '.' sorts before '_') and owns the object + the alpine:init registration, so the
   order among the other files does not matter. See chat_pro.js for the file map. */
chatPart({
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
});

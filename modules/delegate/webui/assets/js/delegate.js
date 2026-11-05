/* delegate — webui extension: chat-header button + docked panel.

   The webui has no module -> frontend push channel, so live data is polled from
   /api/ext/delegate/state with a cursor (fast while the panel is open, slow
   while it is closed so the header dot still lights up). Once a run ends the
   panel reloads the canonical transcript from /api/ext/delegate/transcript.

   Nothing here builds HTML from backend data: values are bound with x-text. */

const DL_POLL_OPEN = 300;
const DL_POLL_CLOSED = 1500;
const DL_DOCK_RATIO = 0.38;

async function dlGet(path, params) {
    const search = params && Object.keys(params).length
        ? "?" + new URLSearchParams(params).toString()
        : "";

    const response = await fetch(`/api/ext/delegate/${path}${search}`, {
        headers: { "Accept": "application/json" },
    });

    let body = null;
    try { body = await response.json(); } catch (error) { body = null; }
    if (!body) throw new Error(`delegate: empty response (${response.status})`);
    if (body.success === false) {
        const detail = typeof body.data === "string" ? body.data : JSON.stringify(body.data);
        throw new Error(detail);
    }
    return body.data;
}

function dlString(value) {
    return value === null || value === undefined ? "" : String(value);
}

const DELEGATE_VIEW = {
    /* ---- state ---- */
    isOpen: false,
    tasks: [],
    activeId: "",
    activeTitle: "",
    blocks: [],
    running: [],
    statusText: "",
    seq: 0,
    _timer: null,
    _interval: 0,

    /* ---- open / close ---- */
    restore() {
        try {
            this.isOpen = localStorage.getItem("delegate.open") === "1";
        } catch (error) {
            this.isOpen = false;
        }
        this.applyOpen(this.isOpen);
        this.ensureTimer();
        if (this.isOpen) this.loadTasks();
    },

    toggle() {
        if (this.isOpen) this.close();
        else this.open();
    },

    open() {
        this.isOpen = true;
        this.applyOpen(true);
        try { localStorage.setItem("delegate.open", "1"); } catch (error) { /* private mode */ }
        this.ensureTimer();
        this.loadTasks();
        if (!this.activeId && this.running.length) {
            this.selectTask(this.running[0].task_id);
        }
    },

    close() {
        this.isOpen = false;
        this.applyOpen(false);
        try { localStorage.setItem("delegate.open", "0"); } catch (error) { /* private mode */ }
        this.ensureTimer();
    },

    applyOpen(open) {
        // the panel is server-rendered into the chat; without it (extension
        // disabled, or no chat yet) never reserve any width
        if (open && !document.querySelector(".dl-cover")) return;

        if (open) {
            document.body.dataset.delegate = "docked";
            this.syncDockWidth();
        } else {
            delete document.body.dataset.delegate;
        }
    },

    syncDockWidth() {
        // percentage padding and a percentage width resolve against different
        // boxes, so pin the panel width in px once and share it with the chat
        const win = document.getElementById("chat-window");
        if (!win) return;
        const width = Math.round(win.getBoundingClientRect().width * DL_DOCK_RATIO);
        document.documentElement.style.setProperty("--delegate-dock-w", width + "px");
    },

    /* ---- polling ---- */
    ensureTimer() {
        const wanted = this.isOpen ? DL_POLL_OPEN : DL_POLL_CLOSED;
        if (this._timer && this._interval === wanted) return;
        if (this._timer) clearInterval(this._timer);
        this._interval = wanted;
        this._timer = setInterval(() => this.poll(), wanted);
        this.poll();
    },

    async poll() {
        try {
            const params = this.seq ? { since: this.seq } : {};
            const data = await dlGet("state", params);
            if (!data) return;

            if (typeof data.seq === "number") this.seq = data.seq;
            this.running = data.running || [];

            for (const event of (data.events || [])) this.applyEvent(event);

            if (this.isOpen && !this.activeId && this.running.length) {
                this.selectTask(this.running[0].task_id);
            }
        } catch (error) {
            /* backend restarting, module disabled, page on an old build — stay quiet */
        }
    },

    /* ---- live events -> blocks ---- */
    applyEvent(event) {
        if (!event || event.task_id !== this.activeId) return;

        const kind = event.kind;

        if (kind === "run_start") {
            this.blocks = [];
            this.statusText = "running…";
            this.loadTasks();          // pick up the task that just appeared
            return;
        }
        if (kind === "run_end") {
            this.statusText = "";
            this.loadTasks();          // status changed to done
            this.loadTranscript(this.activeId);
            return;
        }
        if (kind === "run_error") {
            this.statusText = "error: " + dlString(event.text || "unknown");
            this.loadTasks();
            this.loadTranscript(this.activeId);
            return;
        }
        if (kind === "run_note") {
            this.statusText = dlString(event.text);
            return;
        }
        if (kind === "reasoning" || kind === "content") {
            this.appendText(kind, dlString(event.text));
            return;
        }
        if (kind === "tool_call") {
            this.blocks.push({
                kind: "tool",
                name: dlString(event.name),
                args: dlString(event.args),
                call_id: dlString(event.call_id),
                result: "",
            });
            return;
        }
        if (kind === "tool_result") {
            const callId = dlString(event.call_id);
            const pending = [...this.blocks].reverse().find((block) =>
                block.kind === "tool" && !block.result && (!callId || block.call_id === callId)
            );
            if (pending) pending.result = dlString(event.text);
            else this.blocks.push({ kind: "tool", name: "", args: "", call_id: callId, result: dlString(event.text) });
        }
    },

    appendText(kind, text) {
        if (!text) return;
        const last = this.blocks[this.blocks.length - 1];
        if (last && last.kind === kind) last.text += text;
        else this.blocks.push({ kind, text });
    },

    /* ---- tasks ---- */
    async loadTasks() {
        try {
            const data = await dlGet("tasks");
            if (!data) return;
            this.tasks = data.tasks || [];
            const active = this.tasks.find((task) => task.id === this.activeId);
            if (active) this.activeTitle = active.title || active.id;
        } catch (error) {
            /* stay quiet */
        }
    },

    async selectTask(id) {
        if (!id) return;
        if (id === this.activeId && this.blocks.length) return;

        this.activeId = id;
        this.statusText = "";
        this.blocks = [];

        const known = this.tasks.find((task) => task.id === id);
        this.activeTitle = known ? (known.title || known.id) : id;

        if (this.running.some((job) => job.task_id === id)) {
            this.statusText = "running…";
            return;    // live events build the view; the transcript lands on run_end
        }

        await this.loadTranscript(id);
    },

    async loadTranscript(id) {
        if (!id) return;
        try {
            const data = await dlGet("transcript", { id });
            if (!data || data.task?.id !== this.activeId) return;
            if (this.running.some((job) => job.task_id === id)) return;

            this.activeTitle = data.task.title || data.task.id;
            this.blocks = this.buildBlocks(data.messages || []);
            if (data.dropped) this.statusText = "the last round was interrupted";
        } catch (error) {
            this.statusText = String(error.message || error);
        }
    },

    /* ---- stored transcript -> the same block shape as live events ---- */
    buildBlocks(messages) {
        const blocks = [];

        for (const message of messages || []) {
            const metadata = message._metadata || {};
            if (metadata.reasoning) blocks.push({ kind: "reasoning", text: dlString(metadata.reasoning) });

            if (message.role === "assistant") {
                if (message.content) blocks.push({ kind: "content", text: dlString(message.content) });
                for (const call of (message.tool_calls || [])) {
                    const fn = call.function || {};
                    const args = typeof fn.arguments === "string" ? fn.arguments : JSON.stringify(fn.arguments ?? {});
                    blocks.push({ kind: "tool", name: dlString(fn.name), args, call_id: dlString(call.id), result: "" });
                }
            } else if (message.role === "tool") {
                const callId = dlString(message.tool_call_id);
                const pending = [...blocks].reverse().find((block) =>
                    block.kind === "tool" && !block.result && (!callId || block.call_id === callId)
                );
                if (pending) pending.result = dlString(message.content);
                else blocks.push({ kind: "tool", name: "", args: "", call_id: callId, result: dlString(message.content) });
            } else if (message.role === "user" && message.content) {
                blocks.push({ kind: "user", text: dlString(message.content) });
            }
        }

        return blocks;
    },
};

/* ---- bootstrap ---- */
document.addEventListener("alpine:init", () => {
    Alpine.store("delegateView", DELEGATE_VIEW);
    try { Alpine.store("delegateView").restore(); } catch (error) { /* no chat on this page */ }
});

window.addEventListener("resize", () => {
    try {
        const view = Alpine.store("delegateView");
        if (view && view.isOpen) view.syncDockWidth();
    } catch (error) { /* store not registered yet */ }
});

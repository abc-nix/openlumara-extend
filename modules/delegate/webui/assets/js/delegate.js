/* delegate — webui extension: chat-header button + the docked / full-screen panel.

   The webui has no module -> frontend push channel, so live data is polled from
   /api/ext/delegate/state with a cursor (fast while the panel is open, slow
   while it is closed so the header dot still lights up). Once a run ends the
   panel reloads the canonical transcript from /api/ext/delegate/transcript.

   Nothing here builds HTML from backend data: values are bound with x-text. */

const DL_POLL_OPEN = 300;
const DL_POLL_CLOSED = 1500;
const DL_DOCK_RATIO = 0.38;
const DL_MODES = ["docked", "full"];      // the two geometries the toggle swaps
const DL_MODE_KEY = "delegate.mode";      // localStorage key, default "docked"

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

function dlTryJson(text) {
    try {
        return { ok: true, value: JSON.parse(text) };
    } catch (error) {
        return { ok: false, value: null };
    }
}

async function dlPost(path, body) {
    const response = await fetch(`/api/ext/delegate/${path}`, {
        method: "POST",
        headers: { "Accept": "application/json", "Content-Type": "application/json" },
        body: JSON.stringify(body || {}),
    });

    let payload = null;
    try { payload = await response.json(); } catch (error) { payload = null; }
    if (!payload) throw new Error(`delegate: empty response (${response.status})`);
    if (payload.success === false) {
        const detail = typeof payload.data === "string" ? payload.data : JSON.stringify(payload.data);
        throw new Error(detail);
    }
    return payload.data;
}

const DELEGATE_VIEW = {
    /* ---- state ---- */
    isOpen: false,
    tasks: [],
    today: "",             // local day key of "today", from the tasks route
    searchQuery: "",
    collapsedGroups: {},
    mode: "list",          // "list" | "task" - one view at a time, never stacked
    viewMode: "docked",    // "docked" (side panel) | "full" (covers #chat-window); remembered
    activeId: "",
    activeTitle: "",
    activeTask: null,      // {id, title, status, model, rounds, ...} of the open task
    turns: [],             // grouped like chat turns (steps + final messages)
    liveStep: null,        // the round still streaming, shaped like a step
    running: [],
    statusText: "",
    fontScale: 94,          // percent of the chat's text (module setting font_scale)
    seq: 0,
    _timer: null,
    _interval: 0,
    _scaleLoaded: false,
    _scaleTimer: null,

    /* ---- open / close ---- */
    restore() {
        try {
            this.isOpen = localStorage.getItem("delegate.open") === "1";
            const saved = localStorage.getItem(DL_MODE_KEY);
            this.viewMode = DL_MODES.indexOf(saved) >= 0 ? saved : "docked";
        } catch (error) {
            // private mode / no storage: the declared defaults stand
            this.isOpen = false;
            this.viewMode = "docked";
        }
        this.syncScaleFromDom();
        this.applyOpen(this.isOpen);
        this.ensureTimer();
        if (this.isOpen) this.loadTasks();
    },

    /* ---- font size: a panel-side control over the module's font_scale setting ---- */
    syncScaleFromDom() {
        // the panel is server-rendered with the stored setting, so the inline
        // --dl-scale is the value to start from (before the first tasks poll)
        const cover = document.querySelector(".dl-cover");
        if (!cover) return;
        try {
            const scale = parseFloat(getComputedStyle(cover).getPropertyValue("--dl-scale"));
            if (Number.isFinite(scale) && scale > 0) this.fontScale = Math.round(scale * 100);
        } catch (error) { /* keep the declared default */ }
    },

    applyScale(value) {
        const scale = Number(value);
        this.fontScale = Number.isFinite(scale)
            ? Math.max(70, Math.min(130, Math.round(scale)))
            : 94;

        const cover = document.querySelector(".dl-cover");
        if (cover) cover.style.setProperty("--dl-scale", String(this.fontScale / 100));
    },

    setFontScale(value) {
        // drag feedback is immediate; the store write is debounced so a drag
        // posts once, not on every slider step
        this.applyScale(value);
        clearTimeout(this._scaleTimer);
        this._scaleTimer = setTimeout(() => this.saveFontScale(), 250);
    },

    async saveFontScale() {
        this._scaleTimer = null;
        try {
            const data = await dlPost("font-scale", { scale: this.fontScale });
            if (data && typeof data.font_scale === "number") this.applyScale(data.font_scale);
        } catch (error) {
            this.statusText = String(error.message || error);
        }
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
        // opening the panel while a task runs: jump straight into it
        if (this.mode === "list" && this.running.length) {
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
            // the dataset is the whole mechanism: "docked" reserves room for the
            // panel, "full" makes it cover #chat-window (see delegate.css)
            document.body.dataset.delegate = this.viewMode === "full" ? "full" : "docked";
            // only the docked geometry uses the pinned width; full mode needs no
            // measurement, and switching back recomputes here with the window's
            // current width (a resize may have happened while full)
            if (this.viewMode !== "full") this.syncDockWidth();
        } else {
            delete document.body.dataset.delegate;
        }
    },

    /* ---- dock ⇄ full screen ---- */
    toggleViewMode() {
        this.viewMode = this.viewMode === "full" ? "docked" : "full";
        try { localStorage.setItem(DL_MODE_KEY, this.viewMode); } catch (error) { /* private mode */ }
        this.applyOpen(this.isOpen);
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
        } catch (error) {
            /* backend restarting, module disabled, page on an old build — stay quiet */
        }
    },

    /* ---- live events -> the live step (one growing chain station) ---- */
    applyEvent(event) {
        if (!event) return;

        // a run just started: if the panel is open on the list, jump into it
        if (event.kind === "run_start" && this.isOpen && this.mode === "list") {
            this.selectTask(event.task_id);
        }

        if (event.task_id !== this.activeId) return;

        const kind = event.kind;

        if (kind === "run_start") {
            this.mode = "task";
            this.statusText = "running…";
            this.liveStep = this.blankLiveStep();
            this.loadTasks();          // pick up the task that just appeared
            this.loadChain(this.activeId);
            return;
        }
        if (kind === "run_end" || kind === "run_error") {
            this.statusText = kind === "run_error" ? "error: " + dlString(event.text || "unknown") : "";
            this.liveStep = null;
            this.loadTasks();          // status changed
            this.loadChain(this.activeId);
            return;
        }
        if (kind === "run_note") {
            this.statusText = dlString(event.text);
            return;
        }
        if (kind === "reasoning") {
            this.ensureLiveStep().reasoning_content += dlString(event.text);
            return;
        }
        if (kind === "content") {
            this.ensureLiveStep().content += dlString(event.text);
            return;
        }
        if (kind === "tool_call") {
            this.addLiveTool(event);
            return;
        }
        if (kind === "tool_result") {
            this.finishLiveTool(event);
        }
    },

    /* ---- the round still streaming, shaped like a grouped step ---- */
    blankLiveStep() {
        return {
            step: 9999,
            status: "running",
            reasoning_content: "",
            content: "",
            tool_calls: [],
            modules: [],
        };
    },

    ensureLiveStep() {
        if (!this.liveStep) this.liveStep = this.blankLiveStep();
        return this.liveStep;
    },

    addLiveTool(event) {
        const step = this.ensureLiveStep();
        const name = dlString(event.name);
        const rawArgs = dlString(event.args);
        const parsed = dlTryJson(rawArgs);

        step.tool_calls.push({
            id: dlString(event.call_id),
            type: "function",
            function: { name, arguments: rawArgs },
            _metadata: {
                summary: "",
                args_parsed: parsed.ok ? parsed.value : {},
                response: null,
                done: false,
            },
        });

        // mirror the chat's "Module: action xN" chips
        const [head, ...rest] = name.split("_");
        const module = head ? head.charAt(0).toUpperCase() + head.slice(1) : name;
        const action = (rest.length ? rest.join("_") : head || name).replaceAll("_", " ");

        let group = step.modules.find((entry) => entry.module === module);
        if (!group) {
            group = { module, actions: [] };
            step.modules.push(group);
        }
        const existing = group.actions.find((entry) => entry.name === action);
        if (existing) existing.count += 1;
        else group.actions.push({ name: action, count: 1 });
    },

    finishLiveTool(event) {
        const step = this.liveStep;
        if (!step) return;

        const callId = dlString(event.call_id);
        const call = [...step.tool_calls].reverse().find((entry) =>
            !entry._metadata.done && (!callId || entry.id === callId)
        );
        if (!call) return;

        call._metadata.response = dlString(event.text);
        call._metadata.done = true;
    },

    /* ---- tasks ---- */
    async loadTasks() {
        try {
            // the server groups tasks with the chat sidebar's own day keys/labels
            const data = await dlGet("tasks", { tz_offset: new Date().getTimezoneOffset() });
            if (!data) return;
            this.tasks = data.tasks || [];
            this.today = data.today || "";
            if (!this._scaleLoaded) {
                this._scaleLoaded = true;
                if (typeof data.font_scale === "number") this.applyScale(data.font_scale);
            }
            const active = this.tasks.find((task) => task.id === this.activeId);
            if (active) {
                this.activeTask = active;
                this.activeTitle = active.title || active.id;
            }
        } catch (error) {
            /* stay quiet */
        }
    },

    /* ---- search + day groups, mirroring the chat sidebar ---- */
    taskGroups() {
        const query = this.searchQuery.trim().toLowerCase();
        const visible = !query
            ? this.tasks
            : this.tasks.filter((task) =>
                String(task.title || "").toLowerCase().includes(query) ||
                String(task.id || "").toLowerCase().includes(query));

        const groups = [];
        const byKey = {};
        for (const task of visible) {
            const key = task.group_key || "undated";
            if (!(key in byKey)) {
                byKey[key] = { key, label: task.group_label || "Undated", count: 0, tasks: [] };
                groups.push(byKey[key]);
            }
            byKey[key].tasks.push(task);
            byKey[key].count += 1;
        }
        return groups;
    },

    isGroupCollapsed(group) {
        // search results are always shown fully, like the chat sidebar
        if (this.searchQuery.trim()) return false;
        if (group.key in this.collapsedGroups) return Boolean(this.collapsedGroups[group.key]);
        return group.key !== this.today;
    },

    toggleGroup(group) {
        this.collapsedGroups[group.key] = !this.isGroupCollapsed(group);
    },

    /* ---- task management ---- */
    async renameTask(task) {
        const current = task.title || task.id;
        const next = window.prompt("Task title", current);
        if (next === null) return;

        const title = next.trim();
        if (!title || title === current) return;

        try {
            await dlPost("rename", { id: task.id, title });
            await this.loadTasks();
        } catch (error) {
            this.statusText = String(error.message || error);
        }
    },

    async deleteTask(task) {
        const name = task.title || task.id;
        if (!window.confirm(`Delete "${name}"? Its transcript is removed too.`)) return;

        try {
            await dlPost("delete", { id: task.id });
            if (this.activeId === task.id) {
                this.activeId = "";
                this.activeTitle = "";
                this.activeTask = null;
                this.turns = [];
                this.liveStep = null;
                this.mode = "list";
            }
            await this.loadTasks();
        } catch (error) {
            this.statusText = String(error.message || error);
        }
    },

    async selectTask(id) {
        if (!id) return;
        this.mode = "task";

        if (id === this.activeId && this.turns.length) return;

        this.activeId = id;
        this.statusText = "";
        this.turns = [];
        this.liveStep = null;

        const known = this.tasks.find((task) => task.id === id);
        this.activeTask = known || { id };
        this.activeTitle = known ? (known.title || known.id) : id;

        if (this.running.some((job) => job.task_id === id)) {
            this.statusText = "running…";
            this.liveStep = this.blankLiveStep();
        }

        // grouped rounds so far; the live step above covers the round in flight
        await this.loadChain(id);
    },

    backToList() {
        this.mode = "list";
        this.statusText = "";
        this.loadTasks();
    },

    /* the transcript grouped by the core's turn collector: turns with steps[] in
       exactly the shape the chat's chain templates read (see delegate/chain) */
    async loadChain(id) {
        if (!id) return;
        try {
            const data = await dlGet("chain", { id });
            if (!data || data.task?.id !== this.activeId) return;

            this.activeTask = data.task;
            this.activeTitle = data.task.title || data.task.id;
            this.mode = "task";
            this.turns = data.turns || [];
            if (data.dropped) this.statusText = "the last round was interrupted";
        } catch (error) {
            this.statusText = String(error.message || error);
        }
    },

    turnUserText(turn) {
        const first = (turn.messages || [])[0] || {};
        const content = first.content;
        if (typeof content === "string") return content;
        if (Array.isArray(content)) {
            return content
                .filter((part) => part && part.type === "text")
                .map((part) => part.text || "")
                .join("\n");
        }
        return "";
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
        // only the docked geometry shares a pinned width with the chat; full mode
        // ignores it, and a later switch back to docked recomputes it then
        if (view && view.isOpen && view.viewMode !== "full") view.syncDockWidth();
    } catch (error) { /* store not registered yet */ }
});

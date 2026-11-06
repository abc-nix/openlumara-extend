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
    liveRun: null,         // the running task's feed: {blocks, steps, steps_done, step}
    openBlocks: {},        // block id -> the user's own open/closed choice
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
            this.syncLive();           // the feed itself is built by the bus
        } catch (error) {
            /* backend restarting, module disabled, page on an old build — stay quiet */
        }
    },

    /* ---- the live feed ----

       The ordered blocks come from the backend: events.py folds every emitted
       event into a block list as the run streams, and `state` returns it per
       running task. Nothing is folded here, on purpose — the event ring keeps
       about a minute of stream and a panel opened mid-run, switched to another
       task or refreshed never saw the earlier events, so a browser-side fold
       dropped the earlier blocks. Reading `running[].blocks` every poll makes
       all three cases correct with no cursor to replay. */
    syncLive() {
        const job = this.running.find((entry) => entry.task_id === this.activeId);
        if (!job) {
            if (this.liveRun) this.liveRun = null;   // the chain view takes over
            return;
        }
        this.liveRun = job;
        this.followLive();
    },

    scrollToBottom() {
        // opening a task must land on its END: the last answer, or the live
        // chain while it runs. Two frames: Alpine renders the new blocks in
        // between, so the first frame would scroll to a height that is not
        // final yet
        const pane = document.querySelector(".dl-scroll");
        if (!pane) return;
        requestAnimationFrame(() => requestAnimationFrame(() => {
            if (pane.isConnected) pane.scrollTop = pane.scrollHeight;
        }));
    },

    followLive() {
        // follow the stream only when the view is already at the bottom, like
        // the chat: reading an earlier block is never yanked back down
        const pane = document.querySelector(".dl-scroll");
        if (!pane) return;
        const near = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 90;
        if (!near) return;
        requestAnimationFrame(() => {
            if (pane.isConnected) pane.scrollTop = pane.scrollHeight;
        });
    },

    followBox(el) {
        // the reasoning box follows its own stream, unless the user scrolled up
        // inside it (scrollTop 0 = nothing to lose, the block just started)
        if (!el) return;
        const near = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
        if (!near && el.scrollTop > 0) return;
        requestAnimationFrame(() => {
            if (el.isConnected) el.scrollTop = el.scrollHeight;
        });
    },

    /* ---- reading the feed (the block model itself is server-side) ---- */
    liveBlocks() {
        return (this.liveRun && this.liveRun.blocks) || [];
    },

    liveStepsLabel() {
        const run = this.liveRun || {};
        const total = run.steps || 0;
        const done = run.steps_done || 0;
        // the step still in flight is shown as such, not counted as finished
        if (run.step) return `(${done} of ${total} steps)`;
        return total === 1 ? "(1 step)" : `(${total} steps)`;
    },

    blockOpen(block) {
        // thinking starts COLLAPSED and only the user's click opens it; nothing
        // auto-expands while a run streams (the previous version did, and the
        // feed jumped around as blocks opened and closed by themselves)
        if (!block) return false;
        return Boolean(this.openBlocks[block.id]);
    },

    toggleBlock(block) {
        if (!block) return;
        this.openBlocks[block.id] = !this.blockOpen(block);
    },

    /* the thinking summary row: `Thinking: <last phrase>` (tools use call.label) */
    blockLabel(block) {
        if (!block) return "";
        if (block.kind === "thinking") {
            const phrase = dlString(block.phrase);
            return phrase ? `Thinking: ${phrase}` : "Thinking";
        }
        return "";
    },

    /* the single most informative argument, like the chat's tool summaries */
    callArgs(raw) {
        const text = dlString(raw).trim();
        if (!text) return "";

        const parsed = dlTryJson(text);
        const object = parsed.ok && parsed.value && typeof parsed.value === "object"
            ? parsed.value
            : null;

        let chosen = "";
        if (object) {
            for (const key of ["path", "file_path", "filepath", "file", "filename", "url",
                               "query", "pattern", "regex_pattern", "sub_path", "subfolder",
                               "folder", "id", "name", "content", "text", "cmd"]) {
                if (key in object) { chosen = dlString(object[key]); break; }
            }
            if (!chosen) {
                const firstString = Object.values(object).find((value) => typeof value === "string");
                if (firstString) chosen = firstString;
            }
            if (!chosen && Object.keys(object).length) chosen = JSON.stringify(object);
        }
        if (!chosen) chosen = text;

        chosen = chosen.replace(/\s+/g, " ").trim();
        if (chosen.length > 70) {
            chosen = chosen.includes("/") ? ".." + chosen.slice(-68) : chosen.slice(0, 69) + "..";
        }
        return chosen;
    },

    /* ---- events: triggers only (auto-select, run_end -> the stored chain).
       Their content is already in the bus-built blocks, so nothing is folded
       here: a task switch or a refresh reads `running[].blocks` as-is. ---- */
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
            this.liveRun = null;       // the next poll starts the feed
            this.openBlocks = {};      // block ids restart with every run
            this.loadTasks();          // pick up the task that just appeared
            this.loadChain(this.activeId);
            return;
        }
        if (kind === "run_end" || kind === "run_error") {
            this.statusText = kind === "run_error" ? "error: " + dlString(event.text || "unknown") : "";
            this.liveRun = null;       // the stored chain takes over
            this.loadTasks();          // status changed
            this.loadChain(this.activeId);   // the final chain, exactly as before
            return;
        }
        if (kind === "run_note") {
            // the note is also a block of the feed, in order (see events.py)
            this.statusText = dlString(event.text);
        }
        // reasoning/content/tool_call/tool_result shape nothing here: the bus
        // already folded them into the ordered blocks this store renders
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
                this.liveRun = null;
                this.openBlocks = {};
                this.mode = "list";
            }
            await this.loadTasks();
        } catch (error) {
            this.statusText = String(error.message || error);
        }
    },

    /* a task is live while the backend lists it as running, or while its index
       status still says so (the list poll and the task poll are a beat apart) */
    activeRunning() {
        const id = this.activeId;
        if (!id) return false;
        if ((this.running || []).includes(id)) return true;
        return Boolean(this.activeTask && this.activeTask.status === "running");
    },

    /* stop the open task: the module cancels the run, the runner stores what
       landed and marks it interrupted, and the poll's run_end then reloads the
       chain, so the panel shows the outcome without a second request here */
    async stopTask() {
        const id = this.activeId;
        if (!id) return;

        try {
            await dlPost("stop", { id });
            this.statusText = "stopping\u2026";
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
        this.liveRun = null;
        // the blocks of another run are not this task's: a switch starts clean,
        // and the next poll (or the job already in `running`) fills the feed
        this.openBlocks = {};

        const known = this.tasks.find((task) => task.id === id);
        this.activeTask = known || { id };
        this.activeTitle = known ? (known.title || known.id) : id;

        const job = this.running.find((entry) => entry.task_id === id);
        if (job) {
            this.statusText = "running…";
            // the last poll already carries this run's whole feed, so a switch
            // onto a running task shows its earlier blocks immediately
            this.liveRun = job;
        }

        // grouped rounds so far; the live feed above covers the run in flight
        await this.loadChain(id);
        this.scrollToBottom();
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

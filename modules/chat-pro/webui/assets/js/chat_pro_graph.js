/* chat-pro - the branch graph window, its landmark rail and hover miniature.

   Registers onto CHAT_PRO via chatPart(); chat_pro.js is loaded first (alphabetically,
   and '.' sorts before '_') and owns the object + the alpine:init registration, so the
   order among the other files does not matter. See chat_pro.js for the file map. */
chatPart({
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

    // live message index -> ABSOLUTE turn index. the state route already maps
    // every live message to its turn (it groups the whole history), which also
    // covers messages older than the window core keeps loaded. the scan below is
    // the fallback and has to add turnStart, since positions are window-relative.
    _graphTurnIndexForLive(messageIndex) {
        if (!(messageIndex >= 0)) { return -1; }

        const mapped = this.turnIndex ? this.turnIndex[String(messageIndex)] : undefined;
        if (mapped !== undefined) { return Number(mapped); }

        const chat = Alpine.store("chat");
        const turns = chat.turnHistory || [];
        const start = Number(chat.turnStart) || 0;

        let index = -1;
        turns.forEach((turn, position) => {
            if (turn.first_message_index !== undefined && turn.first_message_index <= messageIndex) {
                index = start + position;
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
        const byId = (this._graphData && this._graphData.byId) || new Map();
        const turnIndex = this.turnIndex || {};
        const indexIds = this.indexIds || {};

        let id = nodeId;
        for (let guard = 0; id && guard < 4096; guard += 1) {
            // node id -> live message index -> absolute turn index: both maps come
            // from the state route, which groups the whole history, so a card older
            // than the loaded window resolves too (the loaded window alone is what
            // used to be scanned here, and it silently missed those)
            for (const [messageIndex, candidate] of Object.entries(indexIds)) {
                if (candidate !== id) { continue; }

                const mapped = turnIndex[messageIndex];
                if (mapped !== undefined) { return Number(mapped); }
            }

            // fallback for a node the state does not map yet: the loaded window,
            // translated to an absolute index
            const chat = Alpine.store("chat");
            const turns = chat.turnHistory || [];
            const start = Number(chat.turnStart) || 0;
            const position = turns.findIndex((turn) => (turn.messages || []).some(
                (message) => (((message && message._metadata) || {}).chatpro_id) === id
            ));
            if (position >= 0) { return start + position; }

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

        // the switched-to path can be older than the window core loads: page the
        // target turn in so the dom can actually receive it before we measure
        const turnIndex = this._turnIndexFor(nodeId);
        if (turnIndex >= 0) { await cpEnsureTurnLoaded(turnIndex); }

        return present();
    },

    async _graphGotoTurn(turnIndex) {
        if (turnIndex < 0) { return; }

        // the target can be older than the window core keeps loaded; scrollToTurn
        // only finds turns that are in the dom, so page it in first
        await cpEnsureTurnLoaded(turnIndex);
        await Alpine.nextTick();

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
});

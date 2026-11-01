/* -- AI GENERATED CODE (deepseek-v4.1-flash-1017-maya-s) :: 2026-11-01 harness:OpenLumara
   chat_nav: one search engine, two views.

   store chatNav      - index, query, filters, hits, jumping (shared)
   component panel    - sidebar outline + hit list (sidebars/chat_nav.html)
   component findbar  - floating Ctrl+F bar (chat/index.html, below_header)

   The webui already holds the whole open chat in memory
   (Alpine.store('chat').turnHistory) but only renders the turns near the
   viewport (stores/ui.js lazy mounting), which is why the browser's own find
   cannot reach the rest of a long session. Everything here works on that
   in-memory history and jumps with core's ui.scrollToTurn(). No backend.

   block  = one user message or one whole assistant chain (a turn).
   record = a piece of a block tagged with a kind; the chips pick which kinds
   count as a match. */

const CHAT_NAV_CHIPS = [
    { kind: "user", label: "You", hint: "your own messages" },
    { kind: "answer", label: "AI", hint: "the assistant's final answers" },
    { kind: "process", label: "Step", hint: "reasoning and intermediate narration (noisy)" },
    { kind: "tools", label: "Tool", hint: "tool calls and tool results (noisy)" },
    { kind: "commands", label: "Cmd", hint: "slash commands and system notices" }
];

// chips group the six record kinds into five buttons
const RECORD_CHIP = {
    user: "user",
    answer: "answer",
    process: "process",
    tool_call: "tools",
    tool_result: "tools",
    command: "commands"
};

// user/answer hits already show their role on the left, so they carry no label
const RECORD_LABEL = {
    user: "",
    answer: "",
    process: "Step",
    tool_call: "Call",
    tool_result: "Result",
    command: "Cmd"
};

// per-turn cache: mergeTurnHistory() reuses unchanged turn objects across
// reloads and streams, so keying by object identity means only the turn that
// actually changed gets re-flattened. live turns are never cached.
const _turnCache = new WeakMap();

// heavy data stays out of Alpine's reactive proxy: blocks carry their records
const _index = { blocks: [] };

// outline state (panel only), kept outside reactivity so the indexing effect
// cannot depend on it and re-trigger itself
const _view = { active: 0, from: -1, to: -1, renderedGen: -1, query: "" };

// the current hits grouped by the message (or block) they belong to, so one
// paint cycle can cover every match that is on screen right now
const _groups = { messages: new Map(), blocks: new Map() };

function flattenContent(content) {
    if (typeof content === "string") return content;
    if (!Array.isArray(content)) return "";

    const parts = [];
    for (const block of content) {
        // image/attachment blocks are not text; never index their payload
        if (block && block.type === "text" && typeof block.text === "string") {
            parts.push(block.text);
        }
    }
    return parts.join("\n");
}

function asText(value) {
    if (typeof value === "string") return value;
    if (value === undefined || value === null) return "";
    try {
        return JSON.stringify(value);
    } catch (error) {
        return String(value);
    }
}

function condense(text, limit = 90) {
    const flat = text.replace(/\s+/g, " ").trim();
    return flat.length > limit ? flat.slice(0, limit - 1) + "…" : flat;
}

function turnInfo(turn, toolLimit) {
    const cached = _turnCache.get(turn);
    if (cached && cached.toolLimit === toolLimit) return cached;

    const records = [];
    // msgIndex is set for the kinds the DOM can address per message (user
    // messages, assistant final messages); it is what lets a hit land on one
    // specific occurrence instead of the block's first one
    const push = (kind, text, cap, msgIndex) => {
        if (typeof text !== "string") return;
        const trimmed = text.trim();
        if (!trimmed) return;

        records.push({
            kind,
            text: cap > 0 && trimmed.length > cap ? trimmed.slice(0, cap) : trimmed,
            msgIndex
        });
    };

    let preview = "";
    let toolSteps = 0;
    const messages = turn.messages || [];

    if (turn.role === "user") {
        const message = messages[0] || {};
        const meta = message._metadata || {};
        const kind = meta.is_cmd || meta.ghost || meta.signal ? "command" : "user";
        const text = flattenContent(message.content);

        push(kind, text, 0, message.index);
        preview = text;
    } else {
        const finals = turn.final_messages || [];
        const answers = [];

        // one record per final message: they render as separate elements, so
        // an answer's occurrences must not be counted across all of them
        for (const message of finals) {
            const text = flattenContent(message && message.content);
            if (!text.trim()) continue;

            answers.push(text);
            push("answer", text, 0, message.index);
        }
        preview = answers.join("\n\n");

        const steps = turn.steps || [];
        const processParts = [];

        for (const step of steps) {
            if (step.reasoning_content) processParts.push(step.reasoning_content);
            if (step.content) processParts.push(step.content);

            for (const call of (step.tool_calls || [])) {
                toolSteps += 1;

                const meta = call._metadata || {};
                const func = call.function || {};
                const name = meta.display_name || func.name || "";

                // tool text is aggregated across steps and rendered as cards,
                // not as a markdown message: no per-occurrence addressing
                push("tool_call", name + " " + asText(func.arguments), toolLimit);
                push("tool_result", (meta.result_summary || "") + " " + asText(meta.response), toolLimit);
            }
        }
        push("process", processParts.join("\n\n"));
    }

    let chars = 0;
    for (const record of records) chars += record.text.length;

    const info = {
        toolLimit,
        records,
        chars,
        toolSteps,
        preview: condense(preview || (records.length ? records[0].text : "") || "empty block")
    };

    if (turn.live !== true) _turnCache.set(turn, info);
    return info;
}

function escapeHtml(text) {
    return text.replace(/[&<>"']/g, character => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    })[character]);
}

function escapeRegExp(text) {
    return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// a hit carries its block (jump target), its message anchor and which
// occurrence of the term it is, a highlighted snippet and the terms that
// produced it, so the chat can be highlighted after the jump too
function makeHit(block, record, terms, position, sequence, occurrence) {
    const text = record.text;
    const start = Math.max(0, position - 45);
    const end = Math.min(text.length, position + 120);

    let html = escapeHtml(text.slice(start, end).replace(/\s+/g, " "));
    const patterns = terms.map(term => escapeRegExp(escapeHtml(term))).filter(Boolean);
    if (patterns.length) {
        html = html.replace(new RegExp("(" + patterns.join("|") + ")", "gi"), "<mark>$1</mark>");
    }
    if (start > 0) html = "…" + html;
    if (end < text.length) html += "…";

    return {
        key: block.index + ":" + record.kind + ":" + sequence,
        block: block.index,
        ordinal: block.ordinal,
        role: block.role === "user" ? "You" : "AI",
        kind: record.kind in RECORD_LABEL ? RECORD_LABEL[record.kind] : record.kind,
        msgIndex: record.msgIndex,
        occurrence,
        terms,
        html
    };
}

// every occurrence of the needles inside the given elements, in document
// order - the same order the index counted them in
function collectRanges(roots, needles, limit) {
    const ranges = [];

    for (const root of roots) {
        const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);

        while (ranges.length < limit && walker.nextNode()) {
            const node = walker.currentNode;
            const text = node.nodeValue;
            if (!text || text.length < 2) continue;

            const lower = text.toLowerCase();
            for (const needle of needles) {
                let from = 0;

                while (ranges.length < limit) {
                    const at = lower.indexOf(needle, from);
                    if (at < 0) break;

                    const range = new Range();
                    range.setStart(node, at);
                    range.setEnd(node, Math.min(node.length, at + needle.length));
                    ranges.push(range);

                    from = at + needle.length;
                }
            }
        }
    }

    return ranges;
}

function messagesContainer() {
    return document.getElementById("messages");
}

// ---------------------------------------------------------------- the engine

const CHAT_NAV_STORE = {
    // module settings, mirrored from the find bar's data attributes
    maxResults: 200,
    toolLimit: 4000,
    highlight: true,
    highlightSeconds: 0,
    takeoverCtrlF: true,
    perOccurrence: true,

    query: "",
    filters: { user: true, answer: true, process: false, tools: false, commands: false },
    chips: CHAT_NAV_CHIPS,
    counts: {},
    results: [],
    activeHit: -1,

    // bookkeeping the panel watches
    blockCount: 0,
    generation: 0,
    chatId: "",

    _total: 0,
    _shown: 0,
    _hidden: 0,
    _follow: false,
    _configured: false,
    _ready: false,
    _searchTimer: null,
    _highlightTimer: null,
    _repaintTimer: null,

    configure() {
        if (this._configured) return;
        this._configured = true;

        const layer = document.querySelector(".cn-find-layer");
        const data = (layer && layer.dataset) || {};

        this.maxResults = Number(data.maxResults) > 0 ? Number(data.maxResults) : 200;
        this.toolLimit = data.toolLimit === undefined ? 4000 : Math.max(0, Number(data.toolLimit) || 0);
        this.highlight = data.highlight !== "0";
        this.takeoverCtrlF = data.ctrlF !== "0";
        this.perOccurrence = data.perOccurrence !== "0";

        // seconds the highlight stays; 0 keeps it until the search changes
        const seconds = Number(data.highlightSeconds);
        this.highlightSeconds = Number.isFinite(seconds) && seconds >= 0 ? seconds : 0;

        for (const chip of CHAT_NAV_CHIPS) {
            const saved = localStorage.getItem("chatNavFilter_" + chip.kind);
            const key = "default" + chip.kind.charAt(0).toUpperCase() + chip.kind.slice(1);
            this.filters[chip.kind] = saved === null ? data[key] === "1" : saved === "1";
        }
    },

    // rebuilds the index whenever the chat or its turn list actually changes.
    // the effect reads the chat store and writes elsewhere only, so it cannot
    // re-trigger itself.
    ensureReady() {
        if (this._ready) return;
        this._ready = true;

        let signature = null;
        Alpine.effect(() => {
            const chat = Alpine.store("chat");
            const turns = chat.turnHistory || [];
            const tail = turns.length ? (turns[turns.length - 1].hash || "live") : "";
            const next = (chat.selectedChat || "") + "|" + turns.length + "|" + tail;
            if (next === signature) return;

            signature = next;
            this.reindex(chat.selectedChat || "");
        });

        // the chat recycles message DOM while scrolling (stores/ui.js unmounts
        // turns that drift away), so a permanent highlight is re-applied when
        // its turn comes back. reading the key set is what subscribes this
        // effect to mount changes.
        Alpine.effect(() => {
            const mounted = Alpine.store("ui").turnMounted;
            void Object.keys(mounted || {}).length;

            if (this.highlightSeconds === 0) this.queueRepaint();
        });
    },

    reindex(chatId) {
        const turns = Alpine.store("chat").turnHistory || [];
        const blocks = new Array(turns.length);
        let maxChars = 1;

        for (let i = 0; i < turns.length; i++) {
            const info = turnInfo(turns[i], this.toolLimit);
            if (info.chars > maxChars) maxChars = info.chars;

            blocks[i] = {
                index: i,
                ordinal: i + 1,
                role: turns[i].role,
                preview: info.preview,
                tools: info.toolSteps,
                chars: info.chars,
                records: info.records,
                width: ""
            };
        }

        // relative block size, sqrt-normalised so short blocks stay visible
        for (const block of blocks) {
            block.width = (8 + 92 * Math.sqrt(block.chars / maxChars)).toFixed(1) + "%";
        }

        _index.blocks = blocks;

        this.blockCount = blocks.length;
        this.chatId = chatId;
        this.generation += 1;

        this.results = [];
        this.activeHit = -1;
        this.counts = {};
        this._total = 0;
        this._shown = 0;
        this._hidden = 0;
        this.highlightClear();

        if (_view.query) this.scheduleSearch(0, false);
    },

    /* ---------------------------- searching ---------------------------- */

    setQuery(value, follow) {
        this.query = value;
        _view.query = value;
        this.scheduleSearch(120, follow === true);
    },

    scheduleSearch(delay, follow) {
        if (follow) this._follow = true;
        if (this._searchTimer) clearTimeout(this._searchTimer);

        this._searchTimer = setTimeout(() => {
            this._searchTimer = null;
            this.runSearch();
        }, delay === undefined ? 120 : delay);
    },

    toggle(kind) {
        this.filters[kind] = !this.filters[kind];
        localStorage.setItem("chatNavFilter_" + kind, this.filters[kind] ? "1" : "0");
        if (_view.query) this.runSearch();
    },

    clear() {
        this.setQuery("", false);
        this.runSearch();
    },

    runSearch() {
        const query = _view.query.trim();
        const follow = this._follow;
        this._follow = false;

        if (!query) {
            this.results = [];
            this.counts = {};
            this.activeHit = -1;
            this._total = 0;
            this._shown = 0;
            this._hidden = 0;
            this.highlightClear();
            return;
        }

        const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
        const anchor = terms[0];
        const perOccurrence = this.perOccurrence;
        const enabled = new Set(CHAT_NAV_CHIPS.filter(chip => this.filters[chip.kind]).map(chip => chip.kind));

        const hits = [];
        const counts = {};
        let total = 0;
        let shown = 0;
        let hidden = 0;
        let sequence = 0;

        for (const block of _index.blocks) {
            for (const record of block.records) {
                if (record.lower === undefined) record.lower = record.text.toLowerCase();

                // every term must be present (same AND semantics as core.search)
                let position = -1;
                let matchedAll = true;

                for (const term of terms) {
                    const found = record.lower.indexOf(term);
                    if (found < 0) {
                        matchedAll = false;
                        break;
                    }
                    if (position < 0 || found < position) position = found;
                }
                if (!matchedAll) continue;

                const chip = RECORD_CHIP[record.kind] || record.kind;
                const isEnabled = enabled.has(chip);

                // occurrences are enumerated for the first term, and only for
                // records the DOM can address per message: everywhere else
                // there is no way to scroll to one specific occurrence, so a
                // record stays a single hit
                if (!perOccurrence || record.msgIndex === undefined) {
                    total += 1;
                    counts[chip] = (counts[chip] || 0) + 1;

                    if (!isEnabled) {
                        hidden += 1;
                        continue;
                    }

                    shown += 1;
                    if (hits.length < this.maxResults) {
                        hits.push(makeHit(block, record, terms, position, sequence++, 0));
                    }
                    continue;
                }

                let from = 0;
                let occurrence = 0;

                while (true) {
                    const at = record.lower.indexOf(anchor, from);
                    if (at < 0) break;

                    total += 1;
                    counts[chip] = (counts[chip] || 0) + 1;

                    if (isEnabled) {
                        shown += 1;
                        if (hits.length < this.maxResults) {
                            hits.push(makeHit(block, record, terms, at, sequence++, occurrence));
                        }
                    } else {
                        hidden += 1;
                    }

                    occurrence += 1;
                    from = at + anchor.length;
                }
            }
        }

        this.counts = counts;
        this.results = hits;
        this.activeHit = -1;
        this._total = total;
        this._shown = shown;
        this._hidden = hidden;

        this.groupHits();

        // find bar semantics: a fresh query lands on its first match, and
        // either way every match that is currently rendered gets painted
        if (follow && hits.length) {
            this.openHit(0);
        } else {
            this.paintSearch(false);
        }
    },

    // index the hits by message (or block) so a paint cycle can walk only the
    // elements that actually hold matches
    groupHits() {
        _groups.messages.clear();
        _groups.blocks.clear();

        for (const hit of this.results) {
            const anchored = hit.msgIndex !== undefined;
            const by = anchored ? _groups.messages : _groups.blocks;
            const key = anchored ? hit.msgIndex : hit.block;

            let list = by.get(key);
            if (!list) by.set(key, list = []);
            list.push(hit);
        }
    },

    next(backwards) {
        const count = this.results.length;
        if (!count) return;

        let index;
        if (this.activeHit < 0) {
            index = backwards ? count - 1 : 0;
        } else {
            index = backwards ? (this.activeHit - 1 + count) % count : (this.activeHit + 1) % count;
        }
        this.openHit(index);
    },

    async openHit(index) {
        const hit = this.results[index];
        if (!hit) return;

        this.activeHit = index;

        // mount and centre the block first; a hit with a message anchor then
        // re-aims at its own occurrence inside it
        await Alpine.store("ui").scrollToTurn(hit.block);
        this.flash(hit.block);

        this.paintSearch(true);
    },

    /* ----------------------------- jumping ----------------------------- */

    async jumpTo(blockIndex) {
        await Alpine.store("ui").scrollToTurn(blockIndex);
        this.flash(blockIndex);
    },

    flash(index) {
        const element = document.querySelector('#messages [data-turn-index="' + index + '"]');
        if (!element) return;

        element.classList.add("cn-flash");
        setTimeout(() => element.classList.remove("cn-flash"), 1000);
    },

    // paint every hit the DOM can show right now: the exact occurrences inside
    // each rendered message, plus the active one in its own colour. Called
    // after a search, after a jump and whenever the chat re-mounts turns while
    // scrolling - that is what lights up messages that were not rendered yet.
    paintSearch(scroll) {
        this.highlightClear();

        if (!this.highlight) return;
        if (!window.CSS || !CSS.highlights || typeof Highlight === "undefined") return;
        if (!this.results.length) return;

        const active = this.activeHit >= 0 ? this.results[this.activeHit] : null;
        const terms = (active || this.results[0]).terms || [];
        const needle = terms[0] ? terms[0].toLowerCase() : "";
        if (!needle) return;

        const others = [];
        let activeRange = null;
        let scrollTarget = null;

        // 1) message-anchored hits: occurrences are in document order, so a
        // hit's occurrence index picks its own range
        for (const [msgIndex, hits] of _groups.messages) {
            const anchor = document.querySelector('#messages [data-cn-msg="' + msgIndex + '"]');
            if (!anchor || !anchor.parentElement) continue;

            const bodies = anchor.parentElement.querySelectorAll("[x-md]");
            const roots = bodies.length ? Array.from(bodies) : [anchor.parentElement];
            const ranges = collectRanges(roots, [needle], 120);
            if (!ranges.length) continue;

            for (const hit of hits) {
                const range = ranges[hit.occurrence];
                if (!range) continue;

                if (hit === active) {
                    activeRange = range;
                    if (scroll) scrollTarget = range;
                } else if (others.length < 400) {
                    others.push(range);
                }
            }
        }

        // 2) block-level hits (process, tool calls) have no addressable
        // occurrence: the active one paints its whole block, as before
        if (active && active.msgIndex === undefined) {
            const element = document.querySelector('#messages [data-turn-index="' + active.block + '"]');
            if (element) {
                const blockRanges = collectRanges([element], terms.map(term => term.toLowerCase()), 40);

                for (const range of blockRanges) {
                    if (others.length >= 400) break;
                    others.push(range);
                }
                if (scroll && blockRanges.length) scrollTarget = blockRanges[0];
            }
        }

        this.applyHighlights(others, activeRange);

        if (scroll && scrollTarget) this.scrollToRange([scrollTarget]);
    },

    applyHighlights(others, active) {
        if (!this.highlight) return;
        if (!window.CSS || !CSS.highlights || typeof Highlight === "undefined") return;

        if (others.length) CSS.highlights.set("chat-nav-hit", new Highlight(...others));
        if (active) CSS.highlights.set("chat-nav-current", new Highlight(active));

        // 0 means "keep it", until the query changes, the bar closes or another
        // hit is picked
        if (this.highlightSeconds > 0) {
            this._highlightTimer = setTimeout(() => this.highlightClear(), this.highlightSeconds * 1000);
        }
    },

    // the webui recycles message DOM while scrolling (stores/ui.js unmounts
    // turns that drift away), which kills the nodes a highlight's ranges point
    // at. a permanent highlight is re-applied to whatever is mounted now.
    queueRepaint() {
        if (this._repaintTimer) return;

        this._repaintTimer = setTimeout(() => {
            this._repaintTimer = null;
            this.repaint();
        }, 120);
    },

    repaint() {
        if (this.highlightSeconds !== 0) return;   // timed flashes are not re-armed
        if (!this.results.length) return;

        this.paintSearch(false);
    },

    scrollToRange(ranges) {
        const container = messagesContainer();
        if (!container) return;

        // a match inside a collapsed chain has no layout box; take the first
        // occurrence that is actually laid out, and otherwise keep the block
        // jump that scrollToTurn already made
        let rect = null;
        for (const range of ranges) {
            const candidate = range.getBoundingClientRect();
            if (candidate && candidate.height) {
                rect = candidate;
                break;
            }
        }
        if (!rect) return;

        const containerRect = container.getBoundingClientRect();
        const offset = rect.top - containerRect.top - containerRect.height * 0.35;
        if (Math.abs(offset) > 8) container.scrollTo({ top: container.scrollTop + offset, behavior: "instant" });
    },

    highlightClear() {
        if (this._highlightTimer) {
            clearTimeout(this._highlightTimer);
            this._highlightTimer = null;
        }
        if (window.CSS && CSS.highlights) {
            CSS.highlights.delete("chat-nav-hit");
            CSS.highlights.delete("chat-nav-current");
        }
    },

    /* -------------------------- template text -------------------------- */

    counterText() {
        if (!this.results.length) return "0 / 0";
        return (this.activeHit < 0 ? 0 : this.activeHit + 1) + " / " + this.results.length;
    },

    statusText() {
        const total = this._total;
        let text = total + (total === 1 ? " match" : " matches");

        if (this._hidden) text += " · " + this._hidden + " hidden";
        if (this._shown > this.results.length) text += " · first " + this.results.length;

        return text;
    },

    emptyText() {
        if (!_index.blocks.length) return "No messages in this chat yet.";
        if (this._hidden) return "Nothing in the enabled kinds — " + this._hidden + " matches in the hidden ones.";
        return "No matches.";
    }
};

// registered before the core stores because extension scripts load first;
// nothing here reads another store at registration time
document.addEventListener("alpine:init", () => {
    Alpine.store("chatNav", CHAT_NAV_STORE);
});

// ----------------------------------------------------------- the find bar

const CHAT_NAV_FINDBAR = {
    open: false,
    _inited: false,
    _root: null,

    init() {
        if (this._inited) return;
        this._inited = true;

        this._root = document.querySelector(".cn-find-layer");

        const nav = Alpine.store("chatNav");
        nav.configure();
        nav.ensureReady();

        document.addEventListener("keydown", event => this.onKeydown(event));
    },

    async openBar() {
        if (!this.open) this.open = true;

        // x-show (and its transition) may not have painted yet; without this
        // the focus lands on nothing and the field stays empty-looking
        await Alpine.nextTick();

        const input = this._root && this._root.querySelector(".cn-find-input");
        if (!input) return;

        input.focus();
        input.select();

        if (document.activeElement !== input) {
            requestAnimationFrame(() => {
                input.focus();
                input.select();
            });
        }

        // reopening on a leftover query: position on the first match, the way
        // a find bar normally behaves
        const nav = Alpine.store("chatNav");
        if (nav.results.length && nav.activeHit < 0) nav.openHit(0);
    },

    closeBar() {
        this.open = false;
        Alpine.store("chatNav").highlightClear();
    },

    // Ctrl+F replaces the browser's find across the chat window, including
    // while the composer has focus (the browser cannot search the chat
    // anyway). The sidebar and modals keep the native one: there it still
    // has a job.
    onKeydown(event) {
        const nav = Alpine.store("chatNav");
        if (!nav.takeoverCtrlF) return;
        if (!(event.ctrlKey || event.metaKey) || event.altKey || event.shiftKey) return;
        if (event.key.toLowerCase() !== "f") return;
        if (!this.shouldTakeOver(event.target)) return;

        event.preventDefault();
        this.openBar();
    },

    shouldTakeOver(target) {
        if (!(target instanceof Element)) return true;
        return !target.closest("#sidebar, #modals");
    }
};

// ---------------------------------------------------------- the sidebar

const CHAT_NAV_PANEL = {
    activeBlock: 0,
    visibleBlocks: [],

    _span: 150,
    _inited: false,
    _spyAttached: false,
    _spyTimer: null,
    _seenGen: -1,
    _seenChat: null,
    _root: null,

    init() {
        if (this._inited) return;
        this._inited = true;

        this._root = document.querySelector(".chat-nav");

        const nav = Alpine.store("chatNav");
        nav.configure();
        nav.ensureReady();

        // the engine rebuilds the index; the panel only owns what is on screen
        Alpine.effect(() => {
            const store = Alpine.store("chatNav");
            const generation = store.generation;
            const chatId = store.chatId;

            if (generation === this._seenGen && chatId === this._seenChat) return;

            const switched = this._seenChat !== null && this._seenChat !== chatId;
            this._seenGen = generation;
            this._seenChat = chatId;

            if (switched) _view.active = 0;
            _view.from = -1;
            _view.to = -1;
            this.activeBlock = _view.active;
            this.refreshWindow();
        });

        requestAnimationFrame(() => this.attachScrollSpy());
    },

    // x-intersect: fired whenever this tab becomes visible
    opened() {
        this.attachScrollSpy();
        this.refreshWindow();
        this.spy();
        this.scrollActiveIntoView();
    },

    refreshWindow() {
        const blocks = _index.blocks;
        if (!blocks.length) {
            _view.from = -1;
            _view.to = -1;
            this.visibleBlocks = [];
            this.activeBlock = 0;
            return;
        }

        const active = Math.min(Math.max(_view.active, 0), blocks.length - 1);
        const from = Math.max(0, active - this._span);
        const to = Math.min(blocks.length, active + this._span + 1);
        const generation = Alpine.store("chatNav").generation;

        if (from === _view.from && to === _view.to && generation === _view.renderedGen) return;

        const rows = new Array(to - from);
        for (let i = from; i < to; i++) {
            const block = blocks[i];
            rows[i - from] = {
                index: block.index,
                ordinal: block.ordinal,
                role: block.role,
                preview: block.preview,
                tools: block.tools,
                width: block.width
            };
        }

        _view.from = from;
        _view.to = to;
        _view.renderedGen = generation;
        this.visibleBlocks = rows;
        this.activeBlock = _view.active;
    },

    setActive(index) {
        _view.active = index;
        this.activeBlock = index;
        this.refreshWindow();
        this.scrollActiveIntoView();
    },

    attachScrollSpy() {
        if (this._spyAttached) return;

        const container = document.getElementById("messages");
        if (!container) return;

        this._spyAttached = true;
        container.addEventListener("scroll", () => this.queueSpy(), { passive: true });
    },

    queueSpy() {
        if (this._spyTimer) return;

        this._spyTimer = setTimeout(() => {
            this._spyTimer = null;
            this.spy();
        }, 100);
    },

    // only mounted turns are around the viewport, and ui.js tells us which
    // ones they are - no need to measure thousands of placeholders per scroll
    spy() {
        if (!_index.blocks.length || !this.isOpen()) return;

        const container = document.getElementById("messages");
        if (!container) return;

        const mounted = Object.keys(Alpine.store("ui").turnMounted || {}).map(Number).sort((a, b) => a - b);
        if (!mounted.length) return;

        const rect = container.getBoundingClientRect();
        const probe = rect.top + rect.height * 0.35;

        let active = mounted[0];
        for (const index of mounted) {
            const element = container.querySelector('[data-turn-index="' + index + '"]');
            if (!element) continue;
            if (element.getBoundingClientRect().top > probe) break;
            active = index;
        }

        if (active !== _view.active) this.setActive(active);
    },

    scrollActiveIntoView() {
        if (!this.isOpen() || _view.query || !this._root) return;

        const row = this._root.querySelector('.cn-block[data-block="' + _view.active + '"]');
        if (row) row.scrollIntoView({ block: "nearest" });
    },

    isOpen() {
        return !!this._root && this._root.offsetParent !== null;
    },

    emptyOutlineText() {
        return this.visibleBlocks.length ? "" : "No messages in this chat yet.";
    }
};

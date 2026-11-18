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
   count as a match.

   a query is matched as the string you typed, one hit per occurrence, the way
   the browser's own find works. two word-mode switches sit beside the kind
   chips: "AND" loosens the query to all-words - every word must appear in the
   message, and the hit is then the message, placed on the tightest run of text
   holding them all - and "MD" ignores the emphasis markers markdown renders
   away, so a phrase can span **bold** or _italics_ the way the page reads. */

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

// full-history cache for the outline, outside reactivity like _index/_view:
// core only keeps a window of turns in memory now (stores/chat.js turnStart,
// turnPageSize), so the rest of the chat is paged in through /api/chat/turns.
const _all = { chatId: "", turns: [], loading: false };

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

// bold and italic markers are rendered away, so they must not stand between you
// and a match: * is always dropped, _ only when it is a marker - between two
// letters or digits it belongs to the word (chat_nav, user_modules).
function stripEmphasis(text) {
    if (!text.includes("*") && !text.includes("_")) return text;

    const word = /[\p{L}\p{N}]/u;
    let out = "";

    for (let i = 0; i < text.length; i++) {
        const character = text[i];

        if (character === "*") continue;
        if (character === "_") {
            const before = text[i - 1] || "";
            const after = text[i + 1] || "";
            if (!(word.test(before) && word.test(after))) continue;
        }

        out += character;
    }

    return out;
}

// the text a record is searched in: lowercased, with every run of whitespace
// collapsed to one space - what the rendered page shows, and therefore what the
// browser's own find matches. `plain` (the MD switch) also drops the emphasis
// markers markdown renders away. every position used for a hit (snippet centre,
// occurrence order) is a position in that copy, never in the raw text; both
// variants are cached because the switch can be flipped while a chat is open.
function recordFlat(record, plain) {
    const key = plain ? "flatPlain" : "flat";

    if (record[key] === undefined) {
        const text = plain ? stripEmphasis(record.text) : record.text;
        record[key] = text.replace(/\s+/g, " ").toLowerCase();
    }

    return record[key];
}

// the same rules applied to what you type, so the query and the copy it is
// matched against always speak the same language
function normalizeQuery(text, plain) {
    const value = plain ? stripEmphasis(text) : text;
    return value.replace(/\s+/g, " ").toLowerCase();
}

// "all words" mode: every occurrence of every term, in text order. that is the
// same order collectRanges() reports for the rendered message, so a hit's
// occurrence index can address one specific match when the chat is painted.
function termOffsets(flat, terms) {
    const marks = [];

    for (const term of terms) {
        let from = 0;
        while (true) {
            const at = flat.indexOf(term, from);
            if (at < 0) break;
            marks.push({ at, term, span: term.length });
            from = at + term.length;
        }
    }

    return marks.sort((a, b) => a.at - b.at);
}

// in "all words" mode a hit is the message itself, so it is placed on the
// tightest run of text that holds every term - what a reader calls "where the
// match is". returns {span, left, right, at} over marks (at is the run's start,
// left/right index the marks it covers), or null if a term is missing.
function tightestWindow(marks, terms) {
    const counts = new Map();
    let have = 0;
    let best = null;

    for (let right = 0, left = 0; right < marks.length; right++) {
        const term = marks[right].term;
        const seen = (counts.get(term) || 0) + 1;
        counts.set(term, seen);
        if (seen === 1) have += 1;

        while (have === terms.length) {
            const span = marks[right].at + marks[right].span - marks[left].at;
            if (!best || span < best.span) best = { span, left, right, at: marks[left].at };

            const leftTerm = marks[left].term;
            const rest = counts.get(leftTerm) - 1;
            counts.set(leftTerm, rest);
            if (rest === 0) have -= 1;
            left += 1;
        }
    }

    return best;
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

    // the outline preview is display only, so it never shows the emphasis
    // markers whatever the MD switch says
    const previewText = preview || (records.length ? records[0].text : "") || "empty block";

    const info = {
        toolLimit,
        records,
        chars,
        toolSteps,
        preview: condense(stripEmphasis(previewText))
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
// occurrence it is, a highlighted snippet and the needles that produced it, so
// the chat can be highlighted after the jump too. span is how many occurrences
// the match covers: 1 for the typed string, more for an "all words" window.
function makeHit(block, record, needles, position, sequence, occurrence, span, plain) {
    const text = recordFlat(record, plain);
    const start = Math.max(0, position - 45);
    const end = Math.min(text.length, position + 120);

    let html = escapeHtml(text.slice(start, end));
    const patterns = needles.map(term => escapeRegExp(escapeHtml(term))).filter(Boolean);
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
        span: Math.max(1, span || 1),
        terms: needles,
        html
    };
}

// collapse every run of whitespace to one space, keeping the original index of
// every kept character, so a match found in the collapsed text can be turned
// back into an offset on the text it came from.
function collapseWithMap(text) {
    const flat = [];
    const map = [];
    let spaceAt = -1;

    for (let i = 0; i < text.length; i++) {
        const character = text[i];

        if (/\s/.test(character)) {
            if (spaceAt < 0) spaceAt = i;
            continue;
        }

        if (spaceAt >= 0 && flat.length) {
            map.push(spaceAt);
            flat.push(" ");
        }
        spaceAt = -1;

        map.push(i);
        flat.push(character);
    }

    return { flat: flat.join(""), map };
}

// every occurrence of the needles inside the given elements, in document order
// - the same order the index counted them in (ties keep the needles' order),
// which is what lets a hit's occurrence index address one specific range.
//
// the elements' text nodes are concatenated first and their whitespace
// collapsed across node edges, so the result lines up with the search copies
// and a match may cross an inline element (`**bold**`, a link, italics)
// exactly the way the rendered text reads. crossNode=false stops every match
// at a node boundary: that is the mode where the index still holds the markers,
// so a match spanning them was never counted and painting it would shift every
// later occurrence in the message.
function collectRanges(roots, needles, limit, crossNode) {
    const nodes = [];
    let raw = "";

    for (const root of roots) {
        const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);

        while (walker.nextNode()) {
            const node = walker.currentNode;
            const text = node.nodeValue;
            if (!text) continue;

            const lowered = text.toLowerCase();
            nodes.push({ node, from: raw.length });
            raw += lowered;
        }
    }

    if (!raw || !nodes.length) return [];

    const flat = collapseWithMap(raw);
    if (!flat.flat) return [];

    // flat offset -> the text node that holds it, and where inside that node
    const locate = offset => {
        let low = 0;
        let high = nodes.length - 1;

        while (low < high) {
            const middle = (low + high + 1) >> 1;
            if (nodes[middle].from <= offset) low = middle;
            else high = middle - 1;
        }

        return { node: nodes[low].node, offset: offset - nodes[low].from };
    };

    const found = [];

    for (const needle of needles) {
        if (!needle) continue;

        let from = 0;

        while (found.length < limit) {
            const at = flat.flat.indexOf(needle, from);
            if (at < 0) break;

            const start = flat.map[at];
            const last = flat.map[at + needle.length - 1];

            if (start === undefined || last === undefined || last < start) {
                from = at + needle.length;
                continue;
            }

            const head = locate(start);
            const tail = locate(last);

            if (!crossNode && head.node !== tail.node) {
                from = at + needle.length;
                continue;
            }

            const range = new Range();
            range.setStart(head.node, head.offset);
            range.setEnd(tail.node, tail.offset + 1);
            found.push({ at, range });

            from = at + needle.length;
        }
    }

    found.sort((a, b) => a.at - b.at);
    return found.map(item => item.range);
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
    // "all words" is a filter, not a kind: a query searches for the string you
    // typed (the browser's find), and this switch loosens that to AND instead,
    // while the second switch decides whether the emphasis markers markdown
    // renders away (bold, italics) stand between you and a match
    andMode: false,
    markdownMode: true,
    // the ALL switch: search the whole chat (paging older turns in) or only the
    // turns the chat already has loaded
    searchAllTurns: false,
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

        // the two word-mode switches, remembered like the chips; the module
        // settings decide their starting state
        const savedAnd = localStorage.getItem("chatNavAnd");
        this.andMode = savedAnd === null ? data.defaultAnd === "1" : savedAnd === "1";

        const savedMarkdown = localStorage.getItem("chatNavMarkdown");
        this.markdownMode = savedMarkdown === null ? data.defaultMarkdown === "1" : savedMarkdown === "1";

        // the third switch: whole-chat search or loaded-window search
        const savedSearchAll = localStorage.getItem("chatNavSearchAll");
        this.searchAllTurns = savedSearchAll === null ? data.defaultSearchAll === "1" : savedSearchAll === "1";
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
            // turnStart is part of the signature: paging older turns changes
            // which slice is loaded, and the outline has to follow it
            const next = (chat.selectedChat || "") + "|" + (chat.turnStart || 0) + "|" + turns.length + "|" + tail;
            if (next === signature) return;

            signature = next;
            this.reindex(chat.selectedChat || "");
        });

        // turns are not unmounted/recycled anymore (core renders everything it
        // has loaded), so a permanent highlight only has to be re-applied when
        // the loaded window changes: paging older turns, or another chat.
        // (ui.turnMounted is gone - reading turnStart/length is the signal now.)
        Alpine.effect(() => {
            const chat = Alpine.store("chat");
            void (chat.turnStart || 0);
            void (chat.turnHistory || []).length;

            if (this.highlightSeconds === 0) this.queueRepaint();
        });
    },

    reindex(chatId) {
        const chat = Alpine.store("chat");
        const windowTurns = chat.turnHistory || [];
        const start = Number(chat.turnStart) || 0;
        const serverTotal = Number(chat.totalTurns) || 0;
        const total = Math.max(serverTotal, start + windowTurns.length);

        // the outline covers the WHOLE chat. core switched to absolute turn
        // indexes (a window of turns is kept in memory, data-turn-index is
        // turnStart + position), so building blocks from the full history keeps
        // block position == absolute turn index: the DOM selector, scrollToTurn
        // and every hit line up again with no translation anywhere.
        if (_all.chatId !== chatId) {
            _all.chatId = chatId;
            _all.turns = [];
        }

        const turns = _all.turns.slice();
        if (turns.length < total) turns.length = total;

        // the loaded window is the freshest data (it carries the live turn), so
        // it wins over whatever the pages said
        for (let i = 0; i < windowTurns.length; i++) turns[start + i] = windowTurns[i];

        const blocks = new Array(turns.length);
        let maxChars = 1;

        for (let i = 0; i < turns.length; i++) {
            const turn = turns[i];

            if (!turn) {
                // a turn whose page has not arrived yet: the slot keeps the
                // outline complete and fills in when the page lands
                blocks[i] = {
                    index: i,
                    ordinal: i + 1,
                    role: "",
                    preview: "",
                    tools: 0,
                    chars: 0,
                    records: [],
                    width: "8.0%"
                };
                continue;
            }

            const info = turnInfo(turn, this.toolLimit);
            if (info.chars > maxChars) maxChars = info.chars;

            blocks[i] = {
                index: i,
                ordinal: i + 1,
                role: turn.role,
                preview: info.preview,
                tools: info.toolSteps,
                chars: info.chars,
                records: info.records,
                width: ""
            };
        }

        // relative block size, sqrt-normalised so short blocks stay visible
        for (const block of blocks) {
            if (!block.records.length) continue;
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

        // only the ALL switch pages the history in: off keeps the outline at
        // ordinals-with-no-preview for unloaded turns (block count stays right)
        // and never fetches an older page
        if (this.searchAllTurns && _all.turns.length < serverTotal) this.ensureFullHistory(chatId);
    },

    /* ------------------- full history (paging) --------------------- */

    // one page of the currently open chat's turns, or null when the chat
    // changed underneath the request
    async _fetchPage(chatId, before, limit) {
        const url = before === null
            ? `/api/chat/turns?limit=${limit}`
            : `/api/chat/turns?before=${before}&limit=${limit}`;

        const result = await simpleApiFetch(url);
        if (!result) return null;
        if (Alpine.store("chat").selectedChat !== chatId) return null;

        return {
            turns: result.turns || [],
            start: Number(result.start) || 0,
            total: Number(result.total) || 0
        };
    },

    // walks the chat backwards page by page; the core caps one page at 200 turns
    async _fetchAllPages(chatId) {
        const limit = 200;
        let collected = [];
        let before = null;

        for (let guard = 0; guard < 100; guard++) {
            const page = await this._fetchPage(chatId, before, limit);
            if (!page) return null;

            collected = page.turns.concat(collected);
            if (!page.turns.length || page.start <= 0) break;
            before = page.start;
        }

        return collected;
    },

    async ensureFullHistory(chatId) {
        if (_all.loading || _all.chatId !== chatId) return;

        _all.loading = true;
        try {
            if (_all.turns.length === 0) {
                const everything = await this._fetchAllPages(chatId);
                if (everything) _all.turns = everything;
            } else {
                // cheap top-up after new turns: the newest page, spliced in at
                // its own start index (older entries stay as they are)
                const page = await this._fetchPage(chatId, null, 200);
                if (page) {
                    for (let i = 0; i < page.turns.length; i++) {
                        _all.turns[page.start + i] = page.turns[i];
                    }
                }
            }
        } finally {
            _all.loading = false;
        }

        if (Alpine.store("chat").selectedChat === chatId) this.reindex(chatId);
    },

    // core only keeps a window of turns loaded and scrollToTurn gives up when
    // no element matches, so a jump has to page the target in first. the page
    // size is raised for the walk so a long jump is a handful of requests
    // instead of one per five turns.
    async ensureTurnLoaded(absIndex) {
        const chat = Alpine.store("chat");
        if (!Number.isFinite(absIndex) || absIndex < 0) return false;
        if (absIndex >= (Number(chat.turnStart) || 0)) return true;

        const pageSize = chat.turnPageSize;
        chat.turnPageSize = 200;

        try {
            for (let guard = 0; guard < 100; guard++) {
                if (absIndex >= (Number(chat.turnStart) || 0)) break;
                if (!chat.hasMoreOlder) break;

                await chat.loadOlderTurns(false);
                await Alpine.nextTick();
            }
        } finally {
            chat.turnPageSize = pageSize;
        }

        return absIndex >= (Number(chat.turnStart) || 0);
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

    // the panel's own switch: the whole query is matched as typed, or every
    // word in it must be present somewhere in the message
    toggleAnd() {
        this.andMode = !this.andMode;
        localStorage.setItem("chatNavAnd", this.andMode ? "1" : "0");
        if (_view.query) this.runSearch();
    },

    // whether the markers that markdown renders away are searched as if they
    // were not there; off means the raw text, markers included
    toggleMarkdown() {
        this.markdownMode = !this.markdownMode;
        localStorage.setItem("chatNavMarkdown", this.markdownMode ? "1" : "0");
        if (_view.query) this.runSearch();
    },

    // ALL: page the whole history in and search all of it. off (default) keeps
    // the loaded window as the searchable copy, so searching never pulls older
    // pages off the backend; switching it on loads whatever is missing and the
    // search re-runs over everything (see ensureFullHistory).
    toggleAll() {
        this.searchAllTurns = !this.searchAllTurns;
        localStorage.setItem("chatNavSearchAll", this.searchAllTurns ? "1" : "0");

        const chatId = Alpine.store("chat").selectedChat || "";

        if (this.searchAllTurns) {
            this.ensureFullHistory(chatId);
        } else if (_all.turns.length) {
            // drop the cached pages: "off" must mean loaded-only, even in a
            // session where ALL was on before
            _all.turns = [];
            this.reindex(chatId);
        }

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

        // the query is searched as the string you typed, like the browser's
        // find: runs of whitespace count as one space because that is what the
        // rendered page shows, and with the MD switch on the emphasis markers
        // that markdown renders away count as nothing. AND mode splits it into
        // terms that must all be present instead, and then a hit is the message.
        const plain = this.markdownMode;
        const needle = normalizeQuery(query, plain);
        const andMode = this.andMode;

        // empty, or nothing left after the markers were dropped
        if (!needle) {
            this.results = [];
            this.counts = {};
            this.activeHit = -1;
            this._total = 0;
            this._shown = 0;
            this._hidden = 0;
            this.highlightClear();
            return;
        }

        const needles = andMode ? [...new Set(needle.split(" "))].filter(Boolean) : [needle];

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
                const flat = recordFlat(record, plain);

                // where the match is, in this record's own text: `at` places the
                // snippet, `occurrence` picks the painted range, `span` says how
                // many occurrences the match covers
                const matches = [];

                if (andMode) {
                    // every term must be present, not necessarily together; the
                    // hit lands on the tightest run that holds all of them
                    const window_ = tightestWindow(termOffsets(flat, needles), needles);
                    if (!window_) continue;

                    matches.push({
                        at: window_.at,
                        occurrence: window_.left,
                        span: window_.right - window_.left + 1
                    });
                } else {
                    let from = 0;
                    while (true) {
                        const at = flat.indexOf(needle, from);
                        if (at < 0) break;
                        matches.push({ at, occurrence: matches.length, span: 1 });
                        from = at + needle.length;
                    }
                    if (!matches.length) continue;
                }

                // records the DOM cannot address per message keep one hit, and
                // so does the whole search when per-occurrence is off
                if (!andMode && (!perOccurrence || record.msgIndex === undefined)) matches.length = 1;

                const chip = RECORD_CHIP[record.kind] || record.kind;
                const isEnabled = enabled.has(chip);

                total += matches.length;
                counts[chip] = (counts[chip] || 0) + matches.length;

                if (!isEnabled) {
                    hidden += matches.length;
                    continue;
                }

                shown += matches.length;

                for (const match of matches) {
                    if (hits.length >= this.maxResults) break;
                    hits.push(makeHit(block, record, needles, match.at, sequence++, match.occurrence, match.span, plain));
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

        // page the target turn in first (core only scrolls to loaded turns),
        // then centre the block; a hit with a message anchor re-aims at its own
        // occurrence inside it further down in paintSearch()
        await this.ensureTurnLoaded(hit.block);
        await Alpine.nextTick();
        await Alpine.store("ui").scrollToTurn(hit.block);
        this.flash(hit.block);

        this.paintSearch(true);
    },

    /* ----------------------------- jumping ----------------------------- */

    async jumpTo(blockIndex) {
        // block index is the absolute turn index now; load it before scrolling
        await this.ensureTurnLoaded(blockIndex);
        await Alpine.nextTick();
        await Alpine.store("ui").scrollToTurn(blockIndex);
        await this.alignTurnTop(blockIndex);
        this.flash(blockIndex);
    },

    // scrollToTurn() centres the turn, which is wrong for a "go to this message"
    // click: on a tall answer (a long chain easily runs to 2000px) the centre
    // puts the start of the message off the top of the viewport, so you land in
    // the middle of it. bring the turn's top to the top of the viewport instead,
    // re-measuring until it settles (markdown/images keep changing height while
    // they render, and the last turn of a chat cannot always reach the top).
    async alignTurnTop(absIndex) {
        const container = document.getElementById("messages");
        if (!container || !Number.isFinite(absIndex)) return;

        let previousTop = -1;
        for (let pass = 0; pass < 12; pass += 1) {
            const element = container.querySelector('[data-turn-index="' + absIndex + '"]');
            if (!element) return;

            const offset = element.getBoundingClientRect().top - container.getBoundingClientRect().top;
            if (Math.abs(offset) < 4) return;
            if (container.scrollTop === previousTop) return;

            previousTop = container.scrollTop;
            container.scrollTo({ top: container.scrollTop + offset, behavior: "instant" });
            await new Promise(resolve => setTimeout(resolve, 80));
        }
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
        // every hit of one search carries the same needles: the typed string,
        // or the words of an AND query. matches may cross an inline element
        // only when the index dropped the markers too, so painting and counts
        // always describe the same text.
        const needles = (active || this.results[0]).terms || [];
        if (!needles.length) return;

        const crossNode = this.markdownMode;

        const others = [];
        let activeRange = null;
        let scrollTarget = null;

        // 1) message-anchored hits: occurrences are in document order, so a
        // hit's occurrence index picks its own range. a match covers `span`
        // consecutive occurrences (1 for the typed string, the words of the
        // tightest window for an "all words" query)
        for (const [msgIndex, hits] of _groups.messages) {
            const anchor = document.querySelector('#messages [data-cn-msg="' + msgIndex + '"]');
            if (!anchor || !anchor.parentElement) continue;

            const bodies = anchor.parentElement.querySelectorAll("[x-md]");
            const roots = bodies.length ? Array.from(bodies) : [anchor.parentElement];
            const ranges = collectRanges(roots, needles, 400, crossNode);
            if (!ranges.length) continue;

            for (const hit of hits) {
                const first = hit.occurrence || 0;
                const span = Math.max(1, hit.span || 1);

                for (let index = first; index < first + span && index < ranges.length; index++) {
                    const range = ranges[index];

                    if (hit === active && !activeRange) {
                        activeRange = range;
                        if (scroll) scrollTarget = range;
                        continue;
                    }
                    if (others.length < 400) others.push(range);
                }
            }
        }

        // 2) block-level hits (process, tool calls) have no addressable
        // occurrence: the active one paints its whole block, as before
        if (active && active.msgIndex === undefined) {
            const element = document.querySelector('#messages [data-turn-index="' + active.block + '"]');
            if (element) {
                const blockRanges = collectRanges([element], needles, 40, crossNode);

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

// --------------------------------------------------------- sidebar tab setting

// core builds a tab for every sidebars/*.html it finds, so a module setting
// cannot stop that discovery: "show_sidebar_tab: false" renders only a marker,
// and the tab (plus its now-empty panel) is hidden here.
const HIDE_TAB_CSS = ".sidebar-tab[data-cn-hidden], .sidebar-panel[data-cn-hidden] { display: none !important; }";

function hideChatNavSidebarTab() {
    const marker = document.querySelector('[data-cn-sidebar-tab="off"]');
    if (!marker) return;

    if (!document.getElementById("cn-hide-tab-style")) {
        const style = document.createElement("style");
        style.id = "cn-hide-tab-style";
        style.textContent = HIDE_TAB_CSS;
        document.head.appendChild(style);
    }

    const panel = marker.closest(".sidebar-panel");
    if (panel) panel.dataset.cnHidden = "1";

    const tab = document.querySelector('.sidebar-tab[data-tab="chat_nav/chat_nav"]');
    if (!tab) return;
    tab.dataset.cnHidden = "1";

    // a saved selection pointing at a hidden tab would leave the sidebar empty
    const shell = document.querySelector(".sidebar-shell");
    const data = shell && window.Alpine ? Alpine.$data(shell) : null;
    if (data && data.sidebarTab === "chat_nav/chat_nav") {
        data.sidebarTab = "";
        try { localStorage.removeItem("sidebarTab"); } catch (error) { /* private mode */ }
    }
}

document.addEventListener("alpine:initialized", hideChatNavSidebarTab);

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
        // libreta owns Ctrl+F while its cover is open: it has its own
        // find/replace bar for the document being edited
        if (document.body && document.body.dataset.lb) return false;
        if (!(target instanceof Element)) return true;
        return !target.closest("#sidebar, #modals, .lb-cover");
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

        // this.$root, not the first .chat-nav in the page: two hosts can
        // render the panel at once (dock + sidebar)
        this._root = this.$root || document.querySelector(".chat-nav");

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

    // every loaded turn is in the DOM now (no lazy mounting) and
    // data-turn-index carries the absolute turn index, so the active block can
    // be read straight off the elements around the viewport
    spy() {
        if (!_index.blocks.length || !this.isOpen()) return;

        const container = document.getElementById("messages");
        if (!container) return;

        const elements = container.querySelectorAll("[data-turn-index]");
        if (!elements.length) return;

        const rect = container.getBoundingClientRect();
        const probe = rect.top + rect.height * 0.35;

        let active = Number(elements[0].dataset.turnIndex);
        for (const element of elements) {
            if (element.getBoundingClientRect().top > probe) break;
            active = Number(element.dataset.turnIndex);
        }

        if (Number.isFinite(active) && active !== _view.active) this.setActive(active);
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

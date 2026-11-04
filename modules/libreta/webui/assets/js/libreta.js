/* -- AI GENERATED CODE (deepseek-v4.1-flash-1017-maya-s) :: 2026-11-02 harness:OpenLumara

   libreta: one user-facing text surface, two stores + one panel component.

   store libretaData  - recent / pinned lists (server state file)
   store libretaView  - open document tabs + the full-window reading cover
   component LIBRETA_PANEL - sidebar tab: Files / Recent / Pinned

   P0a is read-only: nothing here writes a user file. Tabs, mode and side-tab
   prefs live in localStorage; recents and pins live on the server so they
   follow you between browsers and devices.
*/

/* ------------------------------------------------------------------ helpers */
const LB_LS = {
    sideTab: "libreta.sideTab",
    workspace: "libreta.workspace",
    tabs: "libreta.tabs",
    active: "libreta.active",
    open: "libreta.open",
    view: "libreta.view",
};

// cover presentation modes, in cycle order
const LB_VIEWS = ["docked", "full", "floating"];
const LB_NARROW = 900;   // below this, dock/floating fall back to full

function lbErr(error) {
    if (error && typeof error === "object") return error.message || error.error || JSON.stringify(error);
    return String(error || "unknown error");
}

/* The reactive store. Everything that is not a store method itself (the CM6
   update listener, the keymap) must go through this: the module-level const
   holds the raw tab objects, and mutating those bypasses Alpine's proxy — no
   dirty dot, no save button, no live-preview refresh. */
function lbStore() {
    try { return Alpine.store("libretaView"); } catch (e) { return null; }
}

function lbReadRaw(key, fallback) {
    try {
        const raw = localStorage.getItem(key);
        return raw === null ? fallback : raw;
    } catch (e) {
        return fallback;
    }
}

function lbReadJson(key, fallback) {
    try {
        const raw = localStorage.getItem(key);
        return raw === null ? fallback : JSON.parse(raw);
    } catch (e) {
        return fallback;
    }
}

function lbWriteJson(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* private mode */ }
}

function lbWriteRaw(key, value) {
    try { localStorage.setItem(key, String(value)); } catch (e) { /* private mode */ }
}

function lbIsProse(path) {
    return /\.(md|markdown|mdx)$/i.test(String(path || ""));
}

/* strict fuzzy scorer for quick-open (ported from coder/codeeditor, same
   scoring so muscle memory and rankings feel familiar). */
function lbFuzzyScore(text, q) {
    let ti = 0;
    let score = 0;
    let run = 0;
    let firstFound = -1;

    for (let qi = 0; qi < q.length; qi++) {
        const found = text.indexOf(q[qi], ti);
        if (found === -1) return 0;

        const gap = found - ti;
        if (gap === 0) {
            run++;
            score += 5 + run * 2;
        } else {
            run = 0;
            let crossed = 0;
            for (let i = ti; i < found; i++) {
                if (text[i] === "/") crossed++;
            }
            score += 1 - Math.min(gap, 10) - crossed * 3;
        }

        if (found === 0 || text[found - 1] === "/") score += 6;
        if (firstFound === -1) firstFound = found;
        ti = found + 1;
    }

    if (firstFound > text.lastIndexOf("/")) score += 5;

    return score - Math.min(text.length, 60) / 10;
}

function lbEscape(text) {
    return String(text || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}

/* markdown -> sanitised html. renderMarkdown is core's global (marked +
   DOMPurify + hljs); the fallback only exists so a missing core file can never
   leave a blank page. */
function lbRender(text) {
    if (typeof renderMarkdown === "function") {
        try { return renderMarkdown(text || "", false); } catch (e) { return lbEscape(text); }
    }
    return lbEscape(text);
}

function lbAgo(at) {
    const value = Number(at) || 0;
    if (!value) return "";
    const ms = value > 1e12 ? value : value * 1000;
    const minutes = Math.floor(Math.max(0, Date.now() - ms) / 60000);
    if (minutes < 1) return "just now";
    if (minutes < 60) return minutes + " min ago";
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return hours + " h ago";
    const days = Math.floor(hours / 24);
    if (days < 30) return days + " d ago";
    try { return new Date(ms).toLocaleDateString(); } catch (e) { return ""; }
}

/* ---- line-numbered, highlighted code view ----

   We reuse CORE's highlighter (renderers/custom_tool_views/shared.js:
   highlightedCode = hljs + escape, and the same token-aware line splitter idea)
   but emit our OWN markup + classes. The coder/editor modules style
   .code-line/.code-num/.code-text in their own CSS, so borrowing their class
   names would render unnumbered boxes the moment those modules are off.

   Every global here is lb-prefixed on purpose: classic scripts share one
   top-level lexical scope, and a generic name like langForPath would throw
   "Identifier already declared" and kill every script loaded after this one. */
const LB_LANG_BY_EXT = {
    py: "python", js: "javascript", mjs: "javascript", cjs: "javascript",
    ts: "typescript", tsx: "typescript", jsx: "javascript",
    json: "json", html: "html", htm: "html", xml: "xml", svg: "xml",
    css: "css", scss: "scss", yml: "yaml", yaml: "yaml", md: "markdown",
    rs: "rust", go: "go", c: "c", h: "c", cpp: "cpp", hpp: "cpp",
    sh: "bash", bash: "bash", zsh: "bash", toml: "ini", ini: "ini",
    sql: "sql", java: "java", rb: "ruby", php: "php", txt: "plaintext",
};

// files this big skip the gutter instead of building tens of thousands of rows
const LB_CODE_SPLIT_MAX = 500000;

function lbLangForPath(path) {
    const ext = String(path || "").split(".").pop().toLowerCase();
    return LB_LANG_BY_EXT[ext];
}

// split hljs OUTPUT into per-line rows, tracking open token spans so multi-line
// tokens (docstrings, block comments) stay coloured on every line they cover.
function lbSplitHighlighted(html, startLine) {
    const tokenRe = /<[^>]+>|[^<]+/g;
    const open = [];
    const lines = [];
    let cur = "";

    for (const token of String(html || "").match(tokenRe) || []) {
        if (token[0] === "<") {
            if (token.startsWith("</")) open.pop();
            else if (!token.endsWith("/>")) open.push(token);
            cur += token;
            continue;
        }

        token.split("\n").forEach((part, index) => {
            if (index > 0) {
                lines.push(cur + "</span>".repeat(open.length));
                cur = open.join("");
            }
            cur += part;
        });
    }
    lines.push(cur + "</span>".repeat(open.length));

    return lines
        .map((line, index) =>
            '<div class="lb-line"><span class="lb-ln">' + (startLine + index) + "</span>" +
            '<span class="lb-lc">' + line + "</span></div>"   // never inject U+200B: it is invisible in the editor but travels with copied text
        )
        .join("");
}

function lbCodeHtml(text, path) {
    const source = String(text === undefined || text === null ? "" : text);
    if (!source) return "";

    let html;
    if (typeof highlightedCode === "function") {
        try {
            html = highlightedCode(source, lbLangForPath(path));
        } catch (e) {
            html = lbEscape(source);
        }
    } else {
        html = lbEscape(source);
    }

    if (source.length > LB_CODE_SPLIT_MAX) {
        // keep the view responsive: one block, no gutter
        return '<div class="lb-line"><span class="lb-lc">' + html + "</span></div>";
    }

    try {
        return lbSplitHighlighted(html, 1);
    } catch (e) {
        return '<div class="lb-line"><span class="lb-ln">1</span><span class="lb-lc">' + lbEscape(source) + "</span></div>";
    }
}

const LB_ICONS = {
    folder: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>',
    folderOpen: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h7a2 2 0 0 1 2 2v2H6.2a2 2 0 0 0-1.9 1.4L3 17z"/><path d="M3 17l1.5-4.6A2 2 0 0 1 6.4 11H21l-1.6 6.6a2 2 0 0 1-2 1.4H5a2 2 0 0 1-2-2z"/></svg>',
    note: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/><path d="M9 13h6"/><path d="M9 17h4"/></svg>',
    file: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/></svg>',
};

/* ---- CodeMirror 6 (vendored bundle, lazy) ----

   One engine for both views. Code mode is a source editor with a gutter;
   Notes mode is the same editor carrying an Obsidian-style live preview: the
document stays plain markdown and the syntax markers are hidden while the
   caret is elsewhere (no split pane, no block swapping). The bundle is built
   once and vendored — see webui/assets/vendor/cm6/README.md for the recipe. */
const LB_CM6_BASE = "/ext-assets/libreta/vendor/cm6/";
const LB_CM6_CSS = ["atomic.css"];
const LB_CM6_JS = ["cm6.min.js"];

let LB_CM6_LOAD = null;
let LB_CM6 = null;            // the vendored namespace (window.LibretaCM)
let LB_VIEW = null;           // one EditorView; documents are swapped per tab
const LB_STATES = new Map();  // tab key -> EditorState (deliberately outside Alpine's proxy)
const LB_SCROLL = new Map();  // tab key -> scrollTop
const LB_COMP = { wrap: null, numbers: null, lang: null, preview: null };

function lbLoadScript(src) {
    return new Promise((resolve, reject) => {
        const script = document.createElement("script");
        script.src = src;
        script.onload = resolve;
        script.onerror = () => reject(new Error("failed to load " + src));
        document.head.appendChild(script);
    });
}

function lbLoadCss(href) {
    return new Promise((resolve, reject) => {
        const link = document.createElement("link");
        link.rel = "stylesheet";
        link.href = href;
        link.onload = resolve;
        link.onerror = () => reject(new Error("failed to load " + href));
        document.head.appendChild(link);
    });
}

function lbLoadCm6() {
    if (!LB_CM6_LOAD) {
        LB_CM6_LOAD = (async () => {
            for (const file of LB_CM6_CSS) await lbLoadCss(LB_CM6_BASE + file);
            for (const file of LB_CM6_JS) await lbLoadScript(LB_CM6_BASE + file);
            LB_CM6 = window.LibretaCM;
            if (!LB_CM6) throw new Error("the editor bundle did not register LibretaCM");
            LB_COMP.wrap = new LB_CM6.Compartment();
            LB_COMP.numbers = new LB_CM6.Compartment();
            LB_COMP.lang = new LB_CM6.Compartment();
            LB_COMP.preview = new LB_CM6.Compartment();
        })();
    }
    return LB_CM6_LOAD;
}

/* ---- turndown: clipboard HTML -> markdown (Notes mode paste) ----

   Notes mode is markdown, so a paste coming from a browser, a word processor
   or Google Docs is converted instead of dropped in as flattened text. Code
   mode, plain-text clipboards and Ctrl/⌘+Shift+V paste verbatim. The two files
   are plain UMD builds, vendored — see webui/assets/vendor/turndown/README.md. */
const LB_TD_BASE = "/ext-assets/libreta/vendor/turndown/";
const LB_TD_JS = ["turndown.umd.js", "turndown-plugin-gfm.js"];

let LB_TD_LOAD = null;
let LB_TD = null;             // configured TurndownService
let LB_PLAIN_PASTE = false;   // set by Ctrl/⌘+Shift+V, for one paste

function lbLoadTurndown() {
    if (!LB_TD_LOAD) {
        LB_TD_LOAD = (async () => {
            for (const file of LB_TD_JS) await lbLoadScript(LB_TD_BASE + file);
            if (!window.TurndownService) throw new Error("the converter did not register TurndownService");
            LB_TD = lbMakeTurndown();
        })();
    }
    return LB_TD_LOAD;
}

function lbMakeTurndown() {
    const Service = window.TurndownService;
    if (!Service) return null;
    const service = new Service({
        headingStyle: "atx",        // "## heading", not underlined
        hr: "---",
        bulletListMarker: "-",
        codeBlockStyle: "fenced",
        emDelimiter: "*",
        strongDelimiter: "**",
        linkStyle: "inlined",
    });
    const gfm = window.turndownPluginGfm && window.turndownPluginGfm.gfm;
    if (gfm) service.use(gfm);      // tables, strikethrough, task list items

    // attachments are out of scope: an image pastes as its path, never as an
    // embed. A data: URI is not a path, so only its alt text survives.
    service.addRule("imagePath", {
        filter: (node) => node.nodeName === "IMG",
        replacement: (content, node) => {
            const src = (node.getAttribute("src") || "").trim();
            const alt = (node.getAttribute("alt") || "").trim();
            if (!src || /^data:/i.test(src)) return alt;
            return src;
        },
    });
    return service;
}

function lbTurndownService() {
    if (LB_TD) return LB_TD;
    if (!window.TurndownService) {
        lbLoadTurndown().catch(() => {});   // ready for the next paste
        return null;
    }
    LB_TD = lbMakeTurndown();
    return LB_TD;
}

// a file copied in a file manager has no text, only uri(s): paste the path
function lbUriPaths(uris) {
    return String(uris || "")
        .split(/\r?\n/)
        .map((line) => line.trim())
        .filter((line) => line && !line.startsWith("#"))
        .map((line) => {
            try { return decodeURIComponent(line.replace(/^file:\/\//i, "")); }
            catch (e) { return line.replace(/^file:\/\//i, ""); }
        });
}

function lbPasteHandler(event, view) {
    const store = lbStore();
    const tab = store ? store.activeTab() : null;
    // Notes mode only: Code mode keeps the clipboard verbatim (source editing).
    if (!tab || tab.mode !== "notes" || !tab.prose) return false;

    const plainAsked = LB_PLAIN_PASTE;
    LB_PLAIN_PASTE = false;
    if (plainAsked) return false;            // Ctrl/⌘+Shift+V

    const data = event.clipboardData;
    if (!data) return false;

    const html = (data.getData("text/html") || "").trim();

    // an image/file copied from a file manager: only its path is available
    if (!html) {
        const plain = data.getData("text/plain") || "";
        const uris = data.getData("text/uri-list") || "";
        if (!plain.trim() && uris) {
            const paths = lbUriPaths(uris);
            if (!paths.length) return false;
            event.preventDefault();
            view.dispatch(view.state.replaceSelection(paths.join("\n")), { scrollIntoView: true });
            return true;
        }
        return false;
    }

    // our own copy: the browser's HTML flavour of a contentEditable selection
    // carries the editor's spans (cm-, atomic-). Round-tripping those would
    // corrupt the text, so fall through to the plain flavour.
    if (/class="(?:cm-|atomic-)/.test(html)) return false;

    const service = lbTurndownService();
    if (!service) return false;              // still loading: raw paste this time

    let md = "";
    try {
        const body = new DOMParser().parseFromString(html, "text/html").body;
        md = service.turndown(body)
            // turndown pads list markers to three columns ("-   item"); notes
            // should read like something written by hand
            .replace(/^([ \t]*)([-*+])[ \t]{2,}(?=\S)/gm, "$1$2 ")
            .replace(/^([ \t]*)(\d+\.)[ \t]{2,}(?=\S)/gm, "$1$2 ")
            .replace(/\n{3,}/g, "\n\n")
            .trim();
    } catch (e) {
        return false;
    }
    if (!md) return false;

    event.preventDefault();
    view.dispatch(view.state.replaceSelection(md), { scrollIntoView: true });
    return true;
}

function lbExtensionOf(path) {
    return String(path || "").split(".").pop().toLowerCase();
}

// colours ride on CSS variables, so the editor follows the app theme without a
// reconfigure on every theme switch.
function lbCm6Theme() {
    const CM = LB_CM6;
    return CM.EditorView.theme({
        "&": { minHeight: "100%", backgroundColor: "transparent", color: "var(--text-primary)" },
        "&.cm-focused": { outline: "none" },
        ".cm-scroller": { fontFamily: "inherit", lineHeight: "1.7" },
        ".cm-content": { padding: "6px 0 72px", caretColor: "var(--text-primary)" },
        ".cm-gutters": {
            backgroundColor: "transparent",
            border: "none",
            color: "var(--text-muted)",
        },
        ".cm-gutterElement": { padding: "0 8px" },
        ".cm-activeLine": { backgroundColor: "color-mix(in srgb, var(--text-primary) 5%, transparent)" },
        ".cm-activeLineGutter": { backgroundColor: "transparent" },
        ".cm-cursor": { borderLeftWidth: "2px", borderLeftColor: "var(--text-primary)" },
        ".cm-selectionBackground": { backgroundColor: "color-mix(in srgb, var(--accent) 22%, transparent)" },
        "&.cm-focused .cm-selectionBackground": { backgroundColor: "color-mix(in srgb, var(--accent) 30%, transparent)" },
        ".cm-line": { padding: "0 10px" },
        ".cm-panels": { backgroundColor: "var(--bg-secondary)", color: "var(--text-primary)" },
    }, { dark: false });
}

function lbCm6Extensions() {
    const CM = LB_CM6;
    return [
        CM.history(),
        // native selection on purpose: CM6's drawSelection paints in a layer
        // *behind* the content, so opaque line backgrounds (fenced code) hide
        // it. The browser's own ::selection draws above backgrounds.
        CM.highlightActiveLine(),
        CM.libretaHighlight,
        CM.keymap.of([
            ...CM.defaultKeymap,
            ...CM.historyKeymap,
            ...CM.searchKeymap,   // Mod-f / Mod-g / F3 / Escape while the panel is open
            CM.indentWithTab,
            { key: "Mod-s", run: () => { const store = lbStore(); if (store) store.save(); return !!store; } },
        ]),
        // atomic.css is scoped under this class
        CM.EditorView.editorAttributes.of({ class: "atomic-cm-editor" }),
        CM.EditorView.updateListener.of((update) => {
            // MUST go through the reactive store: the module-level const holds the
            // raw tab objects, and mutating those bypasses Alpine's proxy (no
            // dirty dot, no save button, no live preview refresh).
            const store = lbStore();
            const tab = store ? store.activeTab() : null;
            if (!tab) return;
            // the view is the source of truth for the open buffer; keep the
            // per-tab state (outside the reactive store) in step
            LB_STATES.set(tab.key, update.state);
            if (!update.docChanged) return;
            tab.dirty = true;
            if (tab.mode === "notes" && store.split) store.schedulePreview(tab);
            store.scheduleAutosave(tab);
        }),
        lbCm6Theme(),
        // Notes mode pastes as markdown; Code mode and plain clipboards are
        // untouched (the handler returns false and CM6 pastes normally)
        CM.EditorView.domEventHandlers({ paste: lbPasteHandler }),
        // find & replace: engine from @codemirror/search, panel built below
        CM.search({ top: true, createPanel: lbFindPanel }),
        LB_COMP.wrap.of([]),
        LB_COMP.numbers.of([]),
        LB_COMP.lang.of([]),
        LB_COMP.preview.of([]),
    ];
}

// one EditorState per tab: undo history, cursor and fold state stay separate
function lbStateFor(tab) {
    let state = LB_STATES.get(tab.key);
    if (state) return state;
    state = LB_CM6.EditorState.create({
        doc: tab.content || "",
        extensions: lbCm6Extensions(),
    });
    LB_STATES.set(tab.key, state);
    return state;
}

/* ---- find & replace ----

   The engine is @codemirror/search (query state, match highlighting, regex —
   including multiline patterns — and replace as real transactions so undo
   works). Only the panel is ours: `search({createPanel})` replaces the stock
   panel with this compact bar, which folds a replace row open instead of
   showing two rows all the time. */
function lbFindPanel(view) {
    const CM = LB_CM6;
    const root = document.createElement("div");
    root.className = "lb-find";

    const mk = (tag, cls, text) => {
        const node = document.createElement(tag);
        if (cls) node.className = cls;
        if (text !== undefined) node.textContent = text;
        return node;
    };
    const svgBtn = (cls, title, path) => {
        const node = mk("button", cls);
        node.type = "button";
        node.title = title;
        node.setAttribute("aria-label", title);
        node.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' + path + "</svg>";
        return node;
    };

    const toggle = mk("button", "lb-find-toggle", "\u25b8");
    toggle.type = "button";
    toggle.title = "Show replace";
    toggle.setAttribute("aria-label", "Show replace");

    const searchInput = mk("input", "lb-find-input");
    searchInput.type = "text";
    searchInput.placeholder = "Find";
    searchInput.spellcheck = false;
    searchInput.setAttribute("aria-label", "Find");
    searchInput.setAttribute("main-field", "true");   // the engine focuses this

    const count = mk("span", "lb-find-count");

    const prev = svgBtn("lb-find-btn", "Previous match (Shift+Enter)", '<path d="m18 15-6-6-6 6"/>');
    const next = svgBtn("lb-find-btn", "Next match (Enter)", '<path d="m6 9 6 6 6-6"/>');
    const caseBtn = mk("button", "lb-find-btn lb-find-toggle-opt", "Aa");
    caseBtn.type = "button";
    caseBtn.title = "Match case";
    const wordBtn = mk("button", "lb-find-btn lb-find-toggle-opt", "ab");
    wordBtn.type = "button";
    wordBtn.title = "Whole word";
    const reBtn = mk("button", "lb-find-btn lb-find-toggle-opt", ".*");
    reBtn.type = "button";
    reBtn.title = "Regular expression (use \\n to match a line break)";
    const closeBtn = svgBtn("lb-find-btn", "Close (Esc)", '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>');

    const rowMain = mk("div", "lb-find-row");
    rowMain.append(toggle, searchInput, count, prev, next, caseBtn, wordBtn, reBtn, closeBtn);

    const replaceInput = mk("input", "lb-find-input");
    replaceInput.type = "text";
    replaceInput.placeholder = "Replace";
    replaceInput.spellcheck = false;
    replaceInput.setAttribute("aria-label", "Replace");
    const replaceBtn = mk("button", "lb-find-action", "Replace");
    replaceBtn.type = "button";
    const replaceAllBtn = mk("button", "lb-find-action", "All");
    replaceAllBtn.type = "button";
    replaceAllBtn.title = "Replace all matches";

    const rowReplace = mk("div", "lb-find-row lb-find-row-replace");
    rowReplace.hidden = true;
    rowReplace.append(replaceInput, replaceBtn, replaceAllBtn);

    root.append(rowMain, rowReplace);

    let lastQuery = null;
    let timer = null;
    let jumpTimer = null;
    let openReplace = false;

    const isOn = (btn) => btn.classList.contains("on");
    const setOn = (btn, on) => btn.classList.toggle("on", !!on);

    const spec = () => ({
        search: searchInput.value,
        replace: replaceInput.value,
        caseSensitive: isOn(caseBtn),
        regexp: isOn(reBtn),
        wholeWord: isOn(wordBtn),
    });

    function matches(limit) {
        const doc = view.state.doc;
        const s = spec();
        const out = [];
        const wordy = (ch) => !!ch && (ch === "_" || /[0-9A-Za-z]/.test(ch));
        try {
            if (s.regexp) {
                const cur = new CM.RegExpCursor(doc, s.search, { ignoreCase: !s.caseSensitive }, 0, doc.length);
                while (!cur.done && out.length < limit) {
                    cur.next();
                    if (cur.done) break;
                    const { from, to } = cur.value;
                    if (s.wholeWord) {
                        const before = from > 0 ? doc.sliceString(from - 1, from) : "";
                        const after = to < doc.length ? doc.sliceString(to, to + 1) : "";
                        if (wordy(before) || wordy(after)) continue;
                    }
                    out.push({ from, to });
                }
            } else {
                const norm = s.caseSensitive ? null : (text) => text.toLowerCase();
                const cur = new CM.SearchCursor(doc, s.search, 0, doc.length, norm || undefined);
                while (!cur.done && out.length < limit) {
                    cur.next();
                    if (cur.done) break;
                    out.push({ from: cur.value.from, to: cur.value.to });
                }
            }
        } catch (e) { /* invalid pattern: no matches */ }
        return out;
    }

    function refresh() {
        if (!searchInput.value) {
            count.textContent = "";
            root.classList.remove("lb-find-empty");
            return;
        }
        const found = matches(999);
        const sel = view.state.selection.main;
        const at = found.findIndex((m) => m.from === sel.from && m.to === sel.to);
        if (!found.length) count.textContent = "0";
        else if (at >= 0) count.textContent = at + 1 + "/" + found.length;
        else count.textContent = found.length + (found.length >= 999 ? "+" : "");
        root.classList.toggle("lb-find-empty", !found.length);
    }

    function later(fn, ms) {
        clearTimeout(timer);
        timer = setTimeout(fn, ms === undefined ? 160 : ms);
    }

    // findNext/findPrevious call input.select() on the active search field, which
    // selects the whole query after every jump and makes the next keystroke
    // overwrite it. Keep the caret where the user left it.
    function keepCaret(run) {
        if (document.activeElement !== searchInput) { run(); return; }
        const start = searchInput.selectionStart;
        const end = searchInput.selectionEnd;
        run();
        if (document.activeElement === searchInput) {
            try { searchInput.setSelectionRange(start, end); } catch (e) { /* not a text field */ }
        }
    }

    function commit(jump) {
        const query = new CM.SearchQuery(spec());
        if (lastQuery && query.eq(lastQuery)) return;
        lastQuery = query;
        view.dispatch({ effects: CM.setSearchQuery.of(query) });
        later(refresh);
        if (jump && query.valid) {
            clearTimeout(jumpTimer);
            jumpTimer = setTimeout(() => keepCaret(() => CM.findNext(view)), 260);
        }
    }

    // typing jumps to the first match after a short pause (Obsidian behaviour),
    // so the Replace button always has a match selected when it is clicked
    searchInput.addEventListener("input", () => commit(true));
    replaceInput.addEventListener("input", () => commit(false));

    searchInput.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
            event.preventDefault();
            keepCaret(() => (event.shiftKey ? CM.findPrevious(view) : CM.findNext(view)));
            later(refresh, 80);
        } else if (event.key === "Escape") {
            event.preventDefault();
            CM.closeSearchPanel(view);
        }
    });

    replaceInput.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
            event.preventDefault();
            CM.replaceNext(view);
            later(refresh, 80);
        } else if (event.key === "Escape") {
            event.preventDefault();
            CM.closeSearchPanel(view);
        }
    });

    toggle.addEventListener("click", () => {
        openReplace = !openReplace;
        rowReplace.hidden = !openReplace;
        toggle.textContent = openReplace ? "\u25be" : "\u25b8";
        toggle.title = openReplace ? "Hide replace" : "Show replace";
        if (openReplace) replaceInput.focus();
        try { view.requestMeasure(); } catch (e) { /* not mounted */ }
    });

    prev.addEventListener("click", () => { keepCaret(() => CM.findPrevious(view)); later(refresh, 80); });
    next.addEventListener("click", () => { keepCaret(() => CM.findNext(view)); later(refresh, 80); });
    closeBtn.addEventListener("click", () => CM.closeSearchPanel(view));
    caseBtn.addEventListener("click", () => { setOn(caseBtn, !isOn(caseBtn)); commit(true); });
    wordBtn.addEventListener("click", () => { setOn(wordBtn, !isOn(wordBtn)); commit(true); });
    reBtn.addEventListener("click", () => { setOn(reBtn, !isOn(reBtn)); commit(true); });
    replaceBtn.addEventListener("click", () => { CM.replaceNext(view); later(refresh, 80); });
    replaceAllBtn.addEventListener("click", () => { CM.replaceAll(view); later(refresh, 120); });

    return {
        dom: root,
        top: true,
        mount() {
            const query = CM.getSearchQuery(view.state);
            searchInput.value = query.search || "";
            replaceInput.value = query.replace || "";
            setOn(caseBtn, query.caseSensitive);
            setOn(reBtn, query.regexp);
            setOn(wordBtn, query.wholeWord);
            lastQuery = query;
            refresh();
            searchInput.focus();
            searchInput.select();
        },
        update(update) {
            if (update.docChanged || update.selectionSet) later(refresh);
            const query = CM.getSearchQuery(update.state);
            if (lastQuery && !query.eq(lastQuery)) {
                searchInput.value = query.search || "";
                replaceInput.value = query.replace || "";
                setOn(caseBtn, query.caseSensitive);
                setOn(reBtn, query.regexp);
                setOn(wordBtn, query.wholeWord);
                lastQuery = query;
                later(refresh);
            }
        },
        destroy() { clearTimeout(timer); clearTimeout(jumpTimer); },
    };
}

/* --------------------------------------------------------- store: recents/pins */
const LIBRETA_DATA = {
    recent: [],
    pinned: [],
    // data/libreta/autosave.json: only the files that disagree with the module's
    // autosave_default. Keys are 'workspace|path'.
    autosaveOff: [],
    autosaveOnKeys: [],
    loading: false,
    loaded: false,
    error: "",

    // routes answer with only the lists they touched: update what is there and
    // leave the rest alone (a state/autosave reply must not wipe recents)
    apply(data) {
        if (!data) return;
        if (Array.isArray(data.recent)) this.recent = data.recent;
        if (Array.isArray(data.pinned)) this.pinned = data.pinned;
        if (data.autosave && typeof data.autosave === "object") {
            if (Array.isArray(data.autosave.off)) this.autosaveOff = data.autosave.off;
            if (Array.isArray(data.autosave.on)) this.autosaveOnKeys = data.autosave.on;
        }
    },

    async load(force) {
        if (this.loaded && !force) return;
        this.loading = true;
        try {
            this.apply(await simpleApiFetch("/api/ext/libreta/state"));
            this.loaded = true;
            this.error = "";
        } catch (e) {
            this.error = lbErr(e);
        } finally {
            this.loading = false;
        }
    },

    async recordOpen(workspace, path) {
        try { this.apply(await simpleApiPost("/api/ext/libreta/state/open", { workspace, path })); }
        catch (e) { this.error = lbErr(e); }
    },

    isPinned(workspace, path) {
        return this.pinned.some((item) => item.workspace === workspace && item.path === path);
    },

    async togglePin(workspace, path) {
        const on = !this.isPinned(workspace, path);
        try { this.apply(await simpleApiPost("/api/ext/libreta/state/pin", { workspace, path, on })); }
        catch (e) { this.error = lbErr(e); }
        return on;
    },

    async forget(list, workspace, path) {
        try { this.apply(await simpleApiPost("/api/ext/libreta/state/forget", { list, workspace, path })); }
        catch (e) { this.error = lbErr(e); }
    },

    /* ---- per-file autosave switch ---- */
    autosaveKey(workspace, path) {
        return workspace + "|" + path;
    },

    isAutosaveOff(workspace, path) {
        return this.autosaveOff.indexOf(this.autosaveKey(workspace, path)) >= 0;
    },

    isAutosaveOn(workspace, path) {
        return this.autosaveOnKeys.indexOf(this.autosaveKey(workspace, path)) >= 0;
    },

    async setAutosave(workspace, path, on) {
        try { this.apply(await simpleApiPost("/api/ext/libreta/state/autosave", { workspace, path, on })); }
        catch (e) { this.error = lbErr(e); }
    },
};

/* ------------------------------------------------------- store: open documents */
const LIBRETA_VIEW = {
    isOpen: false,
    view: "full",        // full | docked | floating (remembered)
    tabs: [],
    active: "",
    busy: false,
    error: "",
    autowrap: true,
    autosaveInterval: 0,     // seconds of idle before a dirty document is written (config key; 0 = off)
    autosaveDefault: true,   // what a file with no per-file switch does (config key autosave_default)
    readOnly: false,         // config key read-only: the server refuses every write
    _autosave: {},           // tab key -> pending timer (one per document; coder only has one file)
    autosaveMenu: false,     // the caret menu next to the save button is open
    notesLivePreview: true,  // notes mode: one live-preview editor (config key notes_block_edit)
    split: false,          // notes mode: source + live preview side by side
    lineNumbers: true,     // interface preference (localStorage), not a config setting
    viewPadding: 16,       // text inset in docked/floating (localStorage preference)
    editorReady: false,
    cmFailed: false,       // CodeMirror could not load -> fall back to the rendered view
    pendingClose: null,    // {key, name} while an edited tab waits for a discard decision
    savedAt: 0,
    _silent: false,        // true while swapping docs / resetting buffers
    quick: { open: false, query: "", idx: 0, files: [], loadedFor: "", loadedAt: 0, ws: "", loading: false, error: "" },

    wrapOn() {
        const tab = this.activeTab();
        if (!tab) return this.autowrap;
        return tab.wrap === null || tab.wrap === undefined ? this.autowrap : !!tab.wrap;
    },

    toggleWrap() {
        const tab = this.activeTab();
        if (!tab) return;
        tab.wrap = !this.wrapOn();
        this.persist();
        this.applyEditorOptions(tab);
    },

    // notes mode + split: the editor and the rendered preview share the stage
    splitActive() {
        const tab = this.activeTab();
        return !!this.split && !!tab && tab.mode === "notes" && tab.prose;
    },

    toggleSplit() {
        this.split = !this.split;
        lbWriteJson("libreta.split", this.split);

        const tab = this.activeTab();
        if (tab && tab.mode === "notes" && tab.prose) {
            // the preview must show the buffer, not the last saved copy
            if (tab.loaded) tab.html = lbRender(this.sourceOf(tab));
            this.syncEditor();
        }
    },

    // live preview while typing in split mode (throttled; markdown render is not free)
    schedulePreview(tab) {
        clearTimeout(this._previewTimer);
        this._previewTimer = setTimeout(() => {
            if (tab) tab.html = lbRender(this.sourceOf(tab));
        }, 220);
    },

    setLineNumbers(on) {
        this.lineNumbers = !!on;
        lbWriteJson("libreta.lineNumbers", this.lineNumbers);
        this.applyEditorOptions(this.activeTab());
    },

    /* ---- persistence ---- */
    restore() {
        this.lineNumbers = lbReadJson("libreta.lineNumbers", true) !== false;
        this.split = lbReadJson("libreta.split", false) === true;
        const view = lbReadJson(LB_LS.view, "full");
        this.view = LB_VIEWS.indexOf(view) >= 0 ? view : "full";
        this.viewPadding = Math.max(0, Math.min(80, Number(lbReadJson("libreta.viewPadding", 16)) || 16));
        this.applyViewPadding();

        const saved = lbReadJson(LB_LS.tabs, []);
        if (Array.isArray(saved)) {
            this.tabs = saved
                .filter((tab) => tab && typeof tab.path === "string" && tab.path)
                .slice(0, 12)
                .map((tab) => this.makeTab(String(tab.workspace || ""), tab.path, tab.mode, tab.wrap));
        }

        const active = lbReadJson(LB_LS.active, "");
        this.active = this.tabs.some((tab) => tab.key === active)
            ? active
            : (this.tabs[0] ? this.tabs[0].key : "");

        if (lbReadJson(LB_LS.open, false)) this.show();
    },

    persist() {
        lbWriteJson(LB_LS.tabs, this.tabs.map((tab) => ({
            workspace: tab.workspace,
            path: tab.path,
            mode: tab.mode,
            wrap: tab.wrap,
        })));
        lbWriteJson(LB_LS.active, this.active);
        lbWriteJson(LB_LS.open, this.isOpen);
        lbWriteJson(LB_LS.view, this.view);
    },

    /* ---- cover views: full / docked / floating ---- */
    effectiveView() {
        // a docked panel on a narrow window is broken by definition
        if (this.view !== "full" && window.innerWidth < LB_NARROW) return "full";
        return this.view;
    },

    viewNext() {
        const index = LB_VIEWS.indexOf(this.view);
        return LB_VIEWS[(index + 1) % LB_VIEWS.length];
    },

    setView(view) {
        if (LB_VIEWS.indexOf(view) < 0 || view === this.view) return;
        if (window.innerWidth < LB_NARROW) return;   // only full fits
        this.view = view;
        lbWriteJson(LB_LS.view, view);
        this.applyView();
    },

    cycleView() {
        this.setView(this.viewNext());
    },

    applyView() {
        if (this.isOpen) {
            try { document.body.dataset.lb = this.effectiveView(); } catch (e) { /* no body */ }
        }
        this.syncDockWidth();
        // the geometry changed; CM6 must re-measure
        requestAnimationFrame(() => {
            try { if (LB_VIEW) LB_VIEW.requestMeasure(); } catch (e) { /* not mounted */ }
        });
    },

    // The panel is 42% of #chat-window, but percentage *padding* on #chat-window
    // resolves against its parent — different bases, so the chat used to reserve
    // ~128px more than the panel took. Pin the width in px once and share it.
    syncDockWidth() {
        const win = document.getElementById("chat-window");
        if (!win) return;
        try {
            const px = Math.round(win.getBoundingClientRect().width * 0.42);
            document.documentElement.style.setProperty("--lb-dock-w", px + "px");
        } catch (e) { /* no document */ }
    },

    viewLabel() {
        return { full: "full screen", docked: "docked right", floating: "floating" }[this.view] || this.view;
    },

    /* ---- view padding (browser preference) ---- */
    applyViewPadding() {
        try { document.documentElement.style.setProperty("--lb-view-pad", this.viewPadding + "px"); } catch (e) { /* no document */ }
    },

    setViewPadding(px) {
        const value = Math.max(0, Math.min(80, Math.round(Number(px) || 0)));
        this.viewPadding = value;
        lbWriteJson("libreta.viewPadding", value);
        this.applyViewPadding();
    },

    makeTab(workspace, path, mode, wrap) {
        const prose = lbIsProse(path);
        const wanted = mode === "notes" || mode === "code" ? mode : (prose ? "notes" : "code");
        return {
            key: workspace + "|" + path,
            workspace: workspace,
            path: path,
            name: path.split("/").pop() || path,
            prose: prose,
            mode: prose ? wanted : "code",
            // null = follow the module default (code_autowrap); true/false = per-file override
            wrap: typeof wrap === "boolean" ? wrap : null,
            loaded: false,
            loading: false,
            content: "",
            eol: "\n",          // line endings of the file on disk (CM6 works in LF)
            html: "",
            mtime: 0,
            size: 0,
            dirty: false,
            saving: false,      // a write for this document is in flight
            conflict: null,     // {message, path}: the mtime guard refused a save
            error: "",
        };
    },

    async loadSettings() {
        // the module default for autowrap; the per-file toggle overrides it
        try {
            const data = await simpleApiFetch("/api/ext/libreta/settings");
            this.autowrap = !!(data.values && data.values.code_autowrap);
            this.notesLivePreview = !(data.values && data.values.notes_block_edit === false);
            this.autosaveInterval = Math.max(0, Math.min(600, Number(data.values && data.values.autosave_interval) || 0));
            this.autosaveDefault = !(data.values && data.values.autosave_default === false);
            this.readOnly = !!(data.values && data.values.read_only);
        } catch (e) {
            /* keep the built-in default */
        }
    },

    /* ---- CodeMirror 6 lifecycle ---- */
    async ensureLibrary() {
        if (LB_CM6) return true;
        if (this.cmFailed) return false;
        try {
            await lbLoadCm6();
            return true;
        } catch (e) {
            this.cmFailed = true;
            this.error = "could not load the code editor (" + lbErr(e) + ")";
            return false;
        }
    },

    async ensureEditor(host) {
        if (LB_VIEW) return true;
        if (!(await this.ensureLibrary())) return false;
        if (!host) return false;

        LB_VIEW = new LB_CM6.EditorView({
            parent: host,
            state: LB_CM6.EditorState.create({ doc: "", extensions: lbCm6Extensions() }),
        });
        // warm the HTML->markdown converter so the first paste in a session
        // converts instead of falling back to the raw clipboard
        lbLoadTurndown().catch(() => {});
        this.editorReady = true;
        return true;
    },

    // free a tab's buffer (fresh load) so its undo history starts over
    dropEditorState(tab) {
        if (!tab) return;
        LB_STATES.delete(tab.key);
        LB_SCROLL.delete(tab.key);
    },

    // the element that actually scrolls the document. With the editor growing
    // inside the cover stage (no fixed height), CM6's own scroller never
    // scrolls, so walk up to the first ancestor that does.
    scrollBox() {
        let node = LB_VIEW ? LB_VIEW.scrollDOM : null;
        while (node && node.scrollHeight <= node.clientHeight + 1) node = node.parentElement;
        return node || null;
    },

    // remember the visible buffer + scroll before switching documents
    stashView() {
        if (!LB_VIEW || !LB_CM6) return;
        const tab = this.activeTab();
        if (!tab) return;
        LB_STATES.set(tab.key, LB_VIEW.state);
        const box = this.scrollBox();
        LB_SCROLL.set(tab.key, box ? box.scrollTop : 0);
    },

    // the buffer that should be saved/rendered: the live doc when the tab has one
    sourceOf(tab) {
        if (!tab) return "";
        const state = LB_STATES.get(tab.key);
        return state ? state.doc.toString() : (tab.content || "");
    },

    /* ---- notes mode: one live-preview editor (Obsidian style) ---- */
    livePreviewOn() {
        const tab = this.activeTab();
        return !!tab && tab.mode === "notes" && tab.prose && tab.loaded
            && !!this.notesLivePreview && !this.split && !this.cmFailed;
    },

    applyEditorOptions(tab) {
        if (!LB_VIEW || !LB_CM6) return;
        if (!tab) tab = this.activeTab();
        if (!tab) return;

        const CM = LB_CM6;
        const numbers = tab.mode === "notes" ? false : this.lineNumbers;
        const languages = tab.prose ? CM.markdownSupport() : CM.languageForExt(lbExtensionOf(tab.path));
        const preview = this.livePreviewOn()
            ? [CM.inlinePreview({}), CM.tables({}), CM.imageBlocks()]
            : [];

        LB_VIEW.dispatch({
            effects: [
                LB_COMP.wrap.reconfigure(this.wrapOn() ? CM.EditorView.lineWrapping : []),
                LB_COMP.numbers.reconfigure(numbers ? CM.lineNumbers() : []),
                LB_COMP.lang.reconfigure(languages || []),
                LB_COMP.preview.reconfigure(preview),
            ],
        });
    },

    async syncEditor() {
        const tab = this.activeTab();
        if (!tab) return;

        const host = document.getElementById("lb-editor-host");
        if (!(await this.ensureEditor(host))) return;

        LB_VIEW.setState(lbStateFor(tab));
        this.applyEditorOptions(tab);

        // the host may have been hidden while the editor measured itself
        requestAnimationFrame(() => {
            if (!LB_VIEW) return;
            try { LB_VIEW.requestMeasure(); } catch (e) { /* not mounted */ }
            const top = LB_SCROLL.get(tab.key);
            const box = this.scrollBox();
            if (box) box.scrollTop = typeof top === "number" ? top : 0;
        });
    },

    dirtyOf() {
        const tab = this.activeTab();
        return !!tab && !!tab.dirty;
    },

    async save(force) {
        const tab = this.activeTab();
        if (!tab) return;
        // nothing pending: a save would only bump the file's mtime. Force (the
        // conflict banner's Overwrite) still writes.
        if (!force && !tab.dirty) return;
        await this.saveTab(tab, force, false);
    },

    // One write path for manual and automatic saves. `quiet` keeps autosave out
    // of the global "working…" indicator: a background document being written
    // must not flash a spinner over whatever the user is looking at.
    async saveTab(tab, force, quiet) {
        if (!tab || tab.saving) return;
        tab.saving = true;
        if (!quiet) {
            this.busy = true;
            this.error = "";
        }
        try {
            const text = this.sourceOf(tab);
            // the buffer only has LF (CM6 normalises); write CRLF back when the
            // file was opened that way, so saving never rewrites a Windows file
            const payload = tab.eol === "\r\n" ? text.replace(/\n/g, "\r\n") : text;
            const data = await simpleApiPost("/api/ext/libreta/save", {
                workspace: tab.workspace,
                path: tab.path,
                content: payload,
                mtime: force ? 0 : tab.mtime,
                force: !!force,
            });

            tab.content = text;
            tab.mtime = data.mtime || tab.mtime;
            tab.size = data.size || tab.size;
            tab.dirty = false;
            tab.codeHtml = lbCodeHtml(tab.content, tab.path);
            tab.html = lbRender(tab.content);
            tab.conflict = null;
            this.savedAt = Date.now();
            this.persist();
        } catch (e) {
            if (e && typeof e === "object" && e.conflict) {
                // keep the conflict on the document it belongs to: with several
                // tabs open, a banner is only meaningful over its own file
                tab.conflict = { message: e.message || "the file changed on disk", path: tab.path };
            } else {
                this.error = lbErr(e);
            }
        } finally {
            tab.saving = false;
            if (!quiet) this.busy = false;
        }
    },

    async reloadFromDisk() {
        const tab = this.activeTab();
        if (!tab) return;
        tab.conflict = null;
        await this.load(tab);
    },

    conflictOf() {
        const tab = this.activeTab();
        return tab ? tab.conflict : null;
    },

    /* ---- autosave ----

       Coder debounces one file; libreta has documents behind tabs, so the timer
       is per document and saves that document, not whatever happens to be
       active when it fires. Everything else follows coder: reset the timer on
       every keystroke, go through the same mtime-guarded save, and do nothing
       while a write is already in flight. */
    autosaveEnabled() {
        // module level: an interval to wait for, and no read-only mode
        return this.autosaveInterval > 0 && !this.readOnly;
    },

    // does this document take part? the per-file switch lives in
    // data/libreta/autosave.json and is loaded with the recent/pinned state;
    // a file that was never toggled follows autosave_default.
    fileAutosaveOn(tab) {
        if (!tab) return false;
        const data = Alpine.store("libretaData");
        if (!data) return this.autosaveDefault;
        if (data.isAutosaveOff(tab.workspace, tab.path)) return false;
        if (data.isAutosaveOn(tab.workspace, tab.path)) return true;
        return this.autosaveDefault;
    },

    autosaveFor(tab) {
        return !!tab && this.autosaveEnabled() && this.fileAutosaveOn(tab);
    },

    // what the doc-bar chip shows: the interval, or 'auto off' for this file
    autosaveLabel() {
        const tab = this.activeTab();
        if (!tab || !this.autosaveEnabled()) return "";
        return this.fileAutosaveOn(tab) ? "auto " + this.autosaveInterval + "s" : "auto off";
    },

    toggleAutosaveMenu() {
        this.autosaveMenu = !this.autosaveMenu;
    },

    async toggleFileAutosave() {
        const tab = this.activeTab();
        const data = Alpine.store("libretaData");
        if (!tab || !data) return;
        const on = !this.fileAutosaveOn(tab);
        const key = data.autosaveKey(tab.workspace, tab.path);

        // optimistic: the tick moves at once, the server confirms in a moment.
        // Same rule the server applies: an entry that agrees with the default
        // is not stored at all.
        data.autosaveOff = data.autosaveOff.filter((k) => k !== key);
        data.autosaveOnKeys = data.autosaveOnKeys.filter((k) => k !== key);
        if (on !== this.autosaveDefault) {
            if (on) data.autosaveOnKeys = data.autosaveOnKeys.concat(key);
            else data.autosaveOff = data.autosaveOff.concat(key);
        }

        if (on) this.scheduleAutosave(tab);
        else this.cancelAutosave(tab.key);
        await data.setAutosave(tab.workspace, tab.path, on);
    },

    scheduleAutosave(tab) {
        if (!tab || !tab.key || !this.autosaveFor(tab)) return;
        clearTimeout(this._autosave[tab.key]);
        this._autosave[tab.key] = setTimeout(() => {
            delete this._autosave[tab.key];
            this.autosaveTab(tab.key);
        }, this.autosaveInterval * 1000);
    },

    // key omitted = drop every pending timer (used when the setting turns off)
    cancelAutosave(key) {
        if (key === undefined) {
            Object.keys(this._autosave).forEach((k) => clearTimeout(this._autosave[k]));
            this._autosave = {};
            return;
        }
        clearTimeout(this._autosave[key]);
        delete this._autosave[key];
    },

    async autosaveTab(key) {
        const tab = this.tabs.find((item) => item.key === key);
        // a closed, clean, mid-write or conflicted document has nothing to do:
        // the conflict in particular must wait for the user's Reload / Overwrite
        if (!tab || !tab.loaded || !tab.dirty || tab.saving || tab.conflict) return;
        if (!this.autosaveFor(tab)) return;
        await this.saveTab(tab, false, true);
    },

    /* ---- template accessors ---- */
    activeTab() {
        return this.tabs.find((tab) => tab.key === this.active) || null;
    },
    wsOf() { const tab = this.activeTab(); return tab ? tab.workspace : ""; },
    pathOf() { const tab = this.activeTab(); return tab ? tab.path : ""; },
    modeOf() { const tab = this.activeTab(); return tab ? tab.mode : "code"; },
    proseOf() { const tab = this.activeTab(); return !!tab && tab.prose; },
    htmlOf() { const tab = this.activeTab(); return tab ? (tab.html || "") : ""; },
    contentOf() { const tab = this.activeTab(); return tab ? (tab.content || "") : ""; },
    codeHtmlOf() { const tab = this.activeTab(); return tab ? (tab.codeHtml || "") : ""; },
    activePath() {
        const tab = this.activeTab();
        if (!tab) return "";
        return tab.workspace + " / " + (tab.parent ? tab.parent + " / " : "") + tab.name;
    },

    /* ---- quick-open (Ctrl/⌘+P) ---- */
    quickWorkspace() {
        const tab = this.activeTab();
        if (tab && tab.workspace) return tab.workspace;
        const saved = lbReadRaw(LB_LS.workspace, "");
        if (!saved) return "";
        try {
            const parsed = JSON.parse(saved);
            return typeof parsed === "string" ? parsed : "";
        } catch (e) {
            return saved;
        }
    },

    async openQuick() {
        if (!this.isOpen) this.show();
        this.quick.open = true;
        this.quick.query = "";
        this.quick.idx = 0;
        this.quick.error = "";

        const ws = this.quickWorkspace();
        this.quick.ws = ws;

        if (!ws) {
            this.quick.files = [];
            this.quick.error = "no workspace selected";
        } else if (this.quick.loadedFor !== ws || Date.now() - this.quick.loadedAt > 30000) {
            await this.loadQuickFiles(ws);
        }

        requestAnimationFrame(() => {
            const input = document.querySelector(".lb-quick-input");
            if (input) { input.focus(); input.select(); }
        });
    },

    async loadQuickFiles(ws) {
        this.quick.loading = true;
        try {
            const data = await simpleApiFetch("/api/ext/libreta/files?workspace=" + encodeURIComponent(ws));
            this.quick.files = Array.isArray(data.files) ? data.files : [];
            this.quick.loadedFor = ws;
            this.quick.loadedAt = Date.now();
            this.quick.error = "";
        } catch (e) {
            this.quick.files = [];
            this.quick.error = lbErr(e);
        } finally {
            this.quick.loading = false;
        }
    },

    // a create/rename/delete makes the cached index stale; the panel calls this
    quickInvalidate() {
        this.quick.loadedFor = "";
        this.quick.loadedAt = 0;
    },

    quickMatches() {
        const q = (this.quick.query || "").trim().toLowerCase();
        const all = this.quick.files || [];
        if (!q) return all.slice(0, 40);

        const scored = [];
        for (const file of all) {
            const score = lbFuzzyScore(file.toLowerCase(), q);
            if (score > 0) scored.push([score, file]);
        }
        scored.sort((a, b) => (b[0] - a[0]) || a[1].localeCompare(b[1]));
        return scored.slice(0, 40).map((item) => item[1]);
    },

    quickNav(delta) {
        const max = this.quickMatches().length - 1;
        if (max < 0) return;
        this.quick.idx = Math.max(0, Math.min(this.quick.idx + delta, max));
        requestAnimationFrame(() => {
            const row = document.querySelector(".lb-quick-item.active");
            if (row) row.scrollIntoView({ block: "nearest" });
        });
    },

    quickEnter() {
        const file = this.quickMatches()[this.quick.idx];
        if (file) this.quickOpen(file);
    },

    quickOpen(path) {
        const ws = this.quick.ws || this.quickWorkspace();
        this.quick.open = false;
        if (!ws) return;
        this.open(ws, path);
    },

    quickClose() {
        this.quick.open = false;
    },

    /* ---- overlay ---- */
    show() {
        this.isOpen = true;
        this.syncDockWidth();
        try { document.body.dataset.lb = this.effectiveView(); } catch (e) { /* no body yet */ }
        this.persist();
        try { Alpine.store("libretaData").load(); } catch (e) { /* not registered yet */ }
    },

    hide() {
        this.stashView();
        this.isOpen = false;
        try { delete document.body.dataset.lb; } catch (e) { /* no body yet */ }
        this.persist();
    },

    /* ---- documents ---- */
    async open(workspace, path, mode) {
        const key = workspace + "|" + path;
        this.stashView();
        this.show();

        if (this.tabs.some((tab) => tab.key === key)) {
            await this.activate(key);
            return;
        }

        const tab = this.makeTab(workspace, path, mode);
        this.tabs.push(tab);
        this.active = key;
        this.persist();
        // take the reactive handle back out of the array: mutating the raw
        // object that was pushed would bypass Alpine's proxy and never repaint
        await this.load(this.tabs.find((item) => item.key === key) || tab);
    },

    async activate(key) {
        this.stashView();
        this.active = key;
        this.persist();
        const tab = this.activeTab();
        if (!tab) return;
        if (!tab.loaded) await this.load(tab);   // load() syncs the editor itself
        else await this.syncEditor();
    },

    async load(tab) {
        this.busy = true;
        this.error = "";
        tab.loading = true;
        tab.error = "";
        // the buffer is about to be replaced: a pending autosave would write the
        // old text back over the file we just read
        this.cancelAutosave(tab.key);
        tab.conflict = null;
        try {
            const data = await simpleApiFetch(
                "/api/ext/libreta/read?workspace=" + encodeURIComponent(tab.workspace) +
                "&path=" + encodeURIComponent(tab.path)
            );
            const raw = data.content || "";
            // the server sends the file verbatim (newline="" in files.py), but
            // CM6 works in LF: keep the file's endings for the save path and
            // normalise the cached copy so it matches the buffer
            tab.eol = /\r\n/.test(raw) ? "\r\n" : "\n";
            tab.content = raw.replace(/\r\n/g, "\n");
            tab.mtime = data.mtime || 0;
            tab.size = data.size || 0;
            tab.parent = data.parent || "";
            tab.loaded = true;
            tab.dirty = false;
            this.dropEditorState(tab);            // fresh buffer; undo history starts here
            tab.codeHtml = lbCodeHtml(tab.content, tab.path);
            if (tab.mode === "notes") tab.html = lbRender(tab.content);
        } catch (e) {
            tab.error = lbErr(e);
            this.error = tab.error;
        } finally {
            tab.loading = false;
            this.busy = false;
        }

        if (this.active === tab.key) await this.syncEditor();
    },

    async reload() {
        const tab = this.activeTab();
        if (tab) await this.load(tab);
    },

    setMode(mode) {
        const tab = this.activeTab();
        if (!tab) return;
        if (mode === "notes" && !tab.prose) return;
        this.stashView();   // keep the buffer and scroll while the editor is rebuilt

        // carry the editor buffer across the switch: notes mode renders what the
        // doc currently holds, not the stale tab.content
        if (mode === "notes" && tab.loaded) tab.html = lbRender(this.sourceOf(tab));

        tab.mode = mode;
        this.persist();
        this.syncEditor();
    },

    async close(key, force) {
        const index = this.tabs.findIndex((tab) => tab.key === key);
        if (index < 0) return;

        const closing = this.tabs[index];
        if (closing && closing.dirty && !force) {
            // in-app confirm (see the pendingClose banner); native confirm() is
            // auto-answered in some environments
            this.pendingClose = { key: key, name: closing.name };
            return;
        }

        this.pendingClose = null;
        this.cancelAutosave(key);
        LB_STATES.delete(key);
        LB_SCROLL.delete(key);
        this.tabs.splice(index, 1);
        if (this.active === key) {
            const next = this.tabs[Math.min(index, this.tabs.length - 1)];
            this.active = next ? next.key : "";
            // the view still holds the closed document. Point it at whatever is
            // active now: a stale buffer here would be stashed under the next
            // tab's key and later saved into the wrong file.
            if (next) {
                if (!next.loaded) await this.load(next);
                else await this.syncEditor();
            }
        }
        if (!this.tabs.length) this.hide();
        this.persist();
    },

    async togglePin() {
        const tab = this.activeTab();
        if (!tab) return;
        await Alpine.store("libretaData").togglePin(tab.workspace, tab.path);
    },

    // an open document follows a rename instead of pointing at a dead path
    retarget(oldKey, newPath) {
        const tab = this.tabs.find((item) => item.key === oldKey);
        if (!tab) return;

        tab.path = newPath;
        tab.name = newPath.split("/").pop() || newPath;
        tab.prose = lbIsProse(newPath);
        if (tab.mode === "notes" && !tab.prose) tab.mode = "code";

        const key = tab.workspace + "|" + newPath;
        const movedState = LB_STATES.get(oldKey);
        if (movedState) { LB_STATES.delete(oldKey); LB_STATES.set(key, movedState); }
        const movedScroll = LB_SCROLL.get(oldKey);
        if (movedScroll !== undefined) { LB_SCROLL.delete(oldKey); LB_SCROLL.set(key, movedScroll); }
        if (this.active === oldKey) this.active = key;
        tab.key = key;
        // the pending timer still points at the old key: drop it and let the
        // renamed document schedule its own if it is still dirty
        this.cancelAutosave(oldKey);
        this.scheduleAutosave(tab);
        this.persist();
    },

    // the file is gone: close its tab (close() asks first when there are edits;
    // force is used when the delete itself was already confirmed)
    forget(workspace, path, force) {
        const key = workspace + "|" + path;
        if (this.tabs.some((item) => item.key === key)) this.close(key, force);
    },

    confirmClose() {
        const pending = this.pendingClose;
        this.pendingClose = null;
        if (pending) this.close(pending.key, true);
    },

    cancelClose() {
        this.pendingClose = null;
    },
};

/* ----------------------------------------------------------- sidebar component */
const LIBRETA_PANEL = {
    sideTab: "files",
    loaded: false,

    workspaces: [],
    wsIndex: 0,
    rows: [],
    treeLoading: false,
    treeError: "",
    filter: "",

    recentCards: [],
    pinnedCards: [],
    cardsLoading: false,

    // in-app dialog for create/rename/delete. NEVER window.prompt/confirm:
    // native dialogs can be auto-answered by the environment (a controlled
    // browser answered confirm() with true and prompt() with empty, so a delete
    // ran without asking and a rename did nothing).
    dialog: null,
    afterAction: "",
    trashEntries: [],
    trashLoading: false,
    // right-click menu (same model as the editor module's tree menu)
    menu: { open: false, x: 0, y: 0, row: null },

    opts: {
        loaded: false,
        loading: false,
        saving: false,
        values: {},
        workspaces: [],
        inherited: false,
        status: "",
    },

    init() {
        const saved = lbReadRaw(LB_LS.sideTab, "");
        if (saved === "files" || saved === "recent" || saved === "pinned" || saved === "trash" || saved === "options") this.sideTab = saved;

        this.$watch("sideTab", (value) => {
            lbWriteRaw(LB_LS.sideTab, value);
            if (value === "recent" || value === "pinned") this.buildCards(true);
            if (value === "options") this.loadOptions();
            if (value === "trash") this.loadTrash();
        });

        this.$watch("$store.libretaData.recent", () => {
            if (this.sideTab === "recent" && this.loaded) this.buildCards(false);
        });
        this.$watch("$store.libretaData.pinned", () => {
            if (this.sideTab === "pinned" && this.loaded) this.buildCards(false);
        });
    },

    async onShow() {
        // settings are needed for the trash tab's visibility and the options form
        await this.ensureSettings();

        if (!this.loaded) {
            this.loaded = true;
            await this.reload();
        }
        if (this.sideTab === "recent" || this.sideTab === "pinned") await this.buildCards(true);
        if (this.sideTab === "trash") await this.loadTrash();
        if (this.sideTab === "options") await this.loadOptions();
    },

    async ensureSettings() {
        if (this.opts.loaded) return;
        try {
            this.applyOptions(await simpleApiFetch("/api/ext/libreta/settings"));
            this.opts.loaded = true;
        } catch (e) {
            /* the panel still works without it */
        }
    },

    setSideTab(tab) {
        this.sideTab = tab;
    },

    /* ---- files ---- */
    async reload() {
        this.treeError = "";
        this.treeLoading = true;
        try {
            const data = await simpleApiFetch("/api/ext/libreta/workspaces");
            this.workspaces = Array.isArray(data.workspaces) ? data.workspaces : [];
            // keep the workspace the user last picked (survives page reloads and
            // server restarts; stored by name, not by index, so reordering the
            // editor.workspaces list doesn't jump them somewhere else)
            this.wsIndex = this.resolveWorkspaceIndex();
            this.rows = [];
            this.filter = "";
            if (this.workspaces.length) await this.loadTree("", 0);
            this.rememberWorkspace();
        } catch (e) {
            this.treeError = lbErr(e);
        } finally {
            this.treeLoading = false;
        }
    },

    wsName() {
        const workspace = this.workspaces[this.wsIndex];
        return workspace ? workspace.name : "";
    },

    rememberWorkspace() {
        const workspace = this.workspaces[this.wsIndex];
        if (workspace) lbWriteRaw(LB_LS.workspace, workspace.name);
    },

    resolveWorkspaceIndex() {
        const saved = lbReadRaw(LB_LS.workspace, "");
        if (saved) {
            const index = this.workspaces.findIndex((workspace) => workspace.name === saved);
            if (index >= 0) return index;
        }
        return this.wsIndex < this.workspaces.length ? this.wsIndex : 0;
    },

    async selectWorkspace(index) {
        this.wsIndex = Number(index) || 0;
        this.rememberWorkspace();
        this.rows = [];
        this.filter = "";
        await this.loadTree("", 0);
        if (this.sideTab === "trash") await this.loadTrash();
    },

    async loadTree(path, depth) {
        this.treeLoading = true;
        try {
            const data = await simpleApiFetch(
                "/api/ext/libreta/tree?workspace=" + encodeURIComponent(this.wsIndex) +
                "&path=" + encodeURIComponent(path)
            );
            const rows = (data.entries || []).map((entry) => this.makeRow(entry, depth));
            if (!path) this.rows = rows;
            return rows;
        } catch (e) {
            this.treeError = lbErr(e);
            return [];
        } finally {
            this.treeLoading = false;
        }
    },

    makeRow(entry, depth) {
        return {
            key: this.wsIndex + "|" + entry.path,
            name: entry.name,
            path: entry.path,
            depth: depth,
            is_dir: !!entry.is_dir,
            mode: entry.mode || "code",
            open: false,
        };
    },

    rowPad(row) {
        return 8 + row.depth * 14;
    },

    rowIcon(row) {
        if (row.is_dir) return row.open ? LB_ICONS.folderOpen : LB_ICONS.folder;
        return row.mode === "notes" ? LB_ICONS.note : LB_ICONS.file;
    },

    visibleRows() {
        const filter = (this.filter || "").trim().toLowerCase();
        if (!filter) return this.rows;
        return this.rows.filter((row) =>
            row.name.toLowerCase().includes(filter) || row.path.toLowerCase().includes(filter)
        );
    },

    treeEmpty() {
        if (this.treeLoading) return "loading…";
        if (this.treeError) return this.treeError;
        if (!this.workspaces.length) {
            return "No workspaces configured. Turn on the editor module, or fill in Libreta's workspaces.";
        }
        if ((this.filter || "").trim()) return "Nothing matches that filter.";
        return "This folder is empty.";
    },

    async rowClick(row) {
        if (!row.is_dir) {
            await this.openRow(row);
            return;
        }

        if (row.open) {
            this.collapse(row);
            return;
        }

        const index = this.rows.findIndex((item) => item.key === row.key);
        if (index < 0) return;
        row.open = true;
        const children = await this.loadTree(row.path, row.depth + 1);
        this.rows.splice(index + 1, 0, ...children);
    },

    collapse(row) {
        row.open = false;
        const prefix = row.path + "/";
        this.rows = this.rows.filter((item) => !item.path.startsWith(prefix));
    },

    async openRow(row) {
        await Alpine.store("libretaData").recordOpen(this.wsName(), row.path);
        await Alpine.store("libretaView").open(this.wsName(), row.path, row.mode);
    },

    /* ---- create / rename / delete ---- */
    // where a new entry should land: the folder itself, or the parent of a file
    parentOf(row) {
        if (!row) return "";
        if (row.is_dir) return row.path;
        return row.path.includes("/") ? row.path.split("/").slice(0, -1).join("/") : "";
    },

    /* ---- right-click menu ---- */
    openMenu(event, row) {
        const menuHeight = 250;
        this.menu = {
            open: true,
            x: Math.max(4, Math.min(event.clientX, window.innerWidth - 190)),
            y: Math.max(4, Math.min(event.clientY, window.innerHeight - menuHeight)),
            row: row || null,
        };
    },

    closeMenu() {
        this.menu.open = false;
    },

    menuAction(fn) {
        const row = this.menu.row;
        this.closeMenu();
        if (fn) fn(row);
    },

    copyPath(row) {
        if (!row) return;
        const full = this.wsName() + "/" + row.path;
        try {
            navigator.clipboard.writeText(full);
            this.afterAction = "Copied " + full;
            setTimeout(() => { if (this.afterAction) this.afterAction = ""; }, 4000);
        } catch (e) {
            this.afterAction = full;
        }
    },

    toggleRowPin(row) {
        if (!row || row.is_dir) return;
        Alpine.store("libretaData").togglePin(this.wsName(), row.path);
    },

    askCreate(row) {
        if (!this.workspaces.length) return;
        this.afterAction = "";
        this.dialog = { kind: "create", parent: this.parentOf(row), value: "untitled.md" };
    },

    askNewFolder(row) {
        if (!this.workspaces.length) return;
        this.afterAction = "";
        this.dialog = { kind: "folder", parent: this.parentOf(row), value: "New folder" };
    },

    cancelDialog() {
        this.dialog = null;
    },

    submitDialog() {
        const dialog = this.dialog;
        if (!dialog) return;

        const value = String(dialog.value || "").trim();
        this.dialog = null;

        if (dialog.kind === "create") {
            if (value) this.performCreate(value, dialog.parent);
            return;
        }
        if (dialog.kind === "folder") {
            if (value) this.performFolder(value, dialog.parent);
            return;
        }
        if (dialog.kind === "rename") {
            if (value && value !== dialog.row.name) this.performRename(dialog.row, value);
            return;
        }
        if (dialog.kind === "delete") {
            this.performDelete(dialog.row, false);
            return;
        }
        if (dialog.kind === "delete-permanent") {
            this.performDelete(dialog.row, true);
            return;
        }
        if (dialog.kind === "purge") {
            this.performPurge(dialog.entry);
            return;
        }
        if (dialog.kind === "empty") this.performEmptyTrash();
    },

    async performCreate(name, parent) {
        this.treeError = "";
        try {
            const data = await simpleApiPost("/api/ext/libreta/create", {
                workspace: this.wsName(),
                parent: parent || "",
                name: name,
                content: "",
            });
            await this.reload();
            await Alpine.store("libretaView").open(this.wsName(), data.path, undefined);
        } catch (e) {
            this.treeError = lbErr(e);
        }
    },

    async performFolder(name, parent) {
        this.treeError = "";
        try {
            const path = parent ? parent + "/" + name : name;
            await simpleApiPost("/api/ext/libreta/mkdir", { workspace: this.wsName(), path: path });
            await this.reload();
        } catch (e) {
            this.treeError = lbErr(e);
        }
    },

    askRename(row) {
        if (row.is_dir) return;
        this.afterAction = "";
        this.dialog = { kind: "rename", row: row, value: row.name };
    },

    async performRename(row, name) {
        const folder = row.path.includes("/")
            ? row.path.split("/").slice(0, -1).join("/") + "/"
            : "";

        this.treeError = "";
        try {
            const data = await simpleApiPost("/api/ext/libreta/rename", {
                workspace: this.wsName(),
                path: row.path,
                new_path: folder + name,
            });
            // an open tab follows the rename instead of pointing at a dead path
            Alpine.store("libretaView").retarget(this.wsName() + "|" + row.path, data.path);
            await this.reload();
        } catch (e) {
            this.treeError = lbErr(e);
        }
    },

    // moving a file to the trash needs no confirmation (it is recoverable);
    // a real delete does. The quick affordance follows the setting.
    askDelete(row) {
        if (row.is_dir) return;
        if (this.opts.values.use_trash === false) this.askPermanentDelete(row);
        else this.trashRow(row);
    },

    trashRow(row) {
        if (row.is_dir) return;
        this.afterAction = "";
        this.performDelete(row, false);
    },

    // the context menu's "Move to trash" item
    askTrash(row) {
        this.trashRow(row);
    },

    askPermanentDelete(row) {
        if (row.is_dir) return;
        this.afterAction = "";
        this.dialog = { kind: "delete-permanent", row: row };
    },

    async performDelete(row, permanent) {
        this.treeError = "";
        try {
            const data = await simpleApiPost("/api/ext/libreta/delete", {
                workspace: this.wsName(),
                path: row.path,
                permanent: !!permanent,
            });
            // the delete is already confirmed here, so close the tab without a
            // second prompt (force)
            Alpine.store("libretaView").forget(this.wsName(), row.path, true);
            await Alpine.store("libretaData").load(true);
            await this.reload();

            this.afterAction = data.trashed
                ? "Moved " + row.path + " to " + data.trashed
                : "Deleted " + row.path + " permanently";
            setTimeout(() => { if (this.afterAction) this.afterAction = ""; }, 6000);
        } catch (e) {
            this.treeError = lbErr(e);
        }
    },

    /* ---- trash ---- */
    async loadTrash() {
        this.trashLoading = true;
        try {
            const data = await simpleApiFetch("/api/ext/libreta/trash?workspace=" + encodeURIComponent(this.wsIndex));
            this.trashEntries = (data.entries || []).map((entry) => ({
                ...entry,
                when: lbAgo(entry.mtime),
                sizeLabel: this.formatSize(entry.size),
            }));
        } catch (e) {
            this.treeError = lbErr(e);
        } finally {
            this.trashLoading = false;
        }
    },

    formatSize(bytes) {
        const value = Number(bytes) || 0;
        if (value < 1024) return value + " B";
        if (value < 1024 * 1024) return (value / 1024).toFixed(1) + " KB";
        return (value / (1024 * 1024)).toFixed(1) + " MB";
    },

    async restoreEntry(entry) {
        this.treeError = "";
        try {
            const data = await simpleApiPost("/api/ext/libreta/trash/restore", {
                workspace: this.wsName(),
                trashed: entry.trashed,
            });
            this.afterAction = "Restored " + data.path;
            setTimeout(() => { if (this.afterAction) this.afterAction = ""; }, 4000);
            await this.reload();
            await this.loadTrash();
        } catch (e) {
            this.treeError = lbErr(e);
        }
    },

    askPurge(entry) {
        this.afterAction = "";
        this.dialog = { kind: "purge", entry: entry };
    },

    askEmptyTrash() {
        this.afterAction = "";
        this.dialog = { kind: "empty" };
    },

    async performPurge(entry) {
        try {
            await simpleApiPost("/api/ext/libreta/trash/purge", { workspace: this.wsName(), trashed: entry.trashed });
            await this.loadTrash();
        } catch (e) {
            this.treeError = lbErr(e);
        }
    },

    async performEmptyTrash() {
        try {
            const data = await simpleApiPost("/api/ext/libreta/trash/empty", { workspace: this.wsName() });
            this.afterAction = "Removed " + (data.removed || 0) + " file(s) from the trash";
            setTimeout(() => { if (this.afterAction) this.afterAction = ""; }, 4000);
            await this.loadTrash();
        } catch (e) {
            this.treeError = lbErr(e);
        }
    },

    /* ---- recent / pinned cards ---- */
    async buildCards(reload) {
        const store = Alpine.store("libretaData");
        if (reload) await store.load(true);

        const which = this.sideTab === "pinned" ? "pinned" : "recent";
        const list = which === "pinned" ? store.pinned : store.recent;

        const cards = list.map((item) => {
            const path = String(item.path || "");
            return {
                key: item.workspace + "|" + path,
                workspace: String(item.workspace || ""),
                path: path,
                name: path.split("/").pop() || path,
                parent: path.includes("/") ? path.split("/").slice(0, -1).join("/") : "workspace root",
                at: item.at || 0,
                mode: lbIsProse(path) ? "notes" : "code",
                snippet: "",
                meta: lbAgo(item.at),
                missing: false,
                note: "",
            };
        });

        if (cards.length) {
            this.cardsLoading = true;
            try {
                const data = await simpleApiPost("/api/ext/libreta/preview", {
                    files: cards.map((card) => ({ workspace: card.workspace, path: card.path })),
                });
                (data.files || []).forEach((result, index) => {
                    const card = cards[index];
                    if (!card) return;
                    card.missing = !!result.missing;
                    card.snippet = result.snippet || "";
                    card.mode = result.mode || card.mode;
                    card.note = result.missing ? (result.error || "file is gone") : "";
                    card.meta = lbAgo(card.at) + " · " + card.parent;
                });
            } catch (e) {
                cards.forEach((card) => { card.meta = lbAgo(card.at) + " · " + card.parent; });
            } finally {
                this.cardsLoading = false;
            }
        }

        if (which === "pinned") this.pinnedCards = cards;
        else this.recentCards = cards;
    },

    async openCard(card) {
        if (card.missing) return;
        await Alpine.store("libretaData").recordOpen(card.workspace, card.path);
        await Alpine.store("libretaView").open(card.workspace, card.path, card.mode);
    },

    async forgetCard(card, list) {
        await Alpine.store("libretaData").forget(list, card.workspace, card.path);
        await this.buildCards(false);
    },

    /* ---- options ---- */
    async loadOptions() {
        if (this.opts.loaded) return;
        this.opts.loading = true;
        try {
            this.applyOptions(await simpleApiFetch("/api/ext/libreta/settings"));
            this.opts.loaded = true;
        } catch (e) {
            this.opts.status = lbErr(e);
        } finally {
            this.opts.loading = false;
        }
    },

    applyOptions(data) {
        this.opts.values = (data && data.values) || {};
        this.opts.workspaces = (data && data.workspaces) || [];
        this.opts.inherited = !!(data && data.inherited);
    },

    async saveOptions() {
        this.opts.saving = true;
        this.opts.status = "";
        try {
            this.applyOptions(await simpleApiPost("/api/ext/libreta/settings/save", { values: this.opts.values }));

            // the open document picks up a new autowrap default immediately
            const view = Alpine.store("libretaView");
            view.autowrap = !!this.opts.values.code_autowrap;
            // autosave is a live setting too: apply it without a reload, and drop
            // pending timers when it was switched off
            view.autosaveInterval = Math.max(0, Math.min(600, parseInt(this.opts.values.autosave_interval, 10) || 0));
            view.autosaveDefault = this.opts.values.autosave_default !== false;
            view.readOnly = !!this.opts.values.read_only;
            if (!view.autosaveEnabled()) {
                view.cancelAutosave();
            } else {
                // a new default may have taken documents out of autosave: drop the
                // timers that no longer belong to an autosaved file
                Object.keys(view._autosave).forEach((key) => {
                    const tab = view.tabs.find((item) => item.key === key);
                    if (!tab || !view.autosaveFor(tab)) view.cancelAutosave(key);
                });
            }

            this.opts.status = "saved";
            setTimeout(() => { if (this.opts.status === "saved") this.opts.status = ""; }, 2000);

            // show_hidden / workspace changes only matter to the tree
            await this.reload();
        } catch (e) {
            this.opts.status = lbErr(e);
        } finally {
            this.opts.saving = false;
        }
    },
};

/* ------------------------------------------------------------------ bootstrap */
document.addEventListener("alpine:init", () => {
    Alpine.store("libretaData", LIBRETA_DATA);
    Alpine.store("libretaView", LIBRETA_VIEW);

    // restore through the PROXY, not the raw object: a restored tab whose
    // content arrives after the first paint must still be reactive
    const view = Alpine.store("libretaView");
    view.loadSettings();
    // the per-file autosave switches come with the state file: load them early
    // so the first keystrokes already know which documents opted out
    Alpine.store("libretaData").load();
    view.restore();

    if (view.isOpen) {
        const tab = view.tabs.find((item) => item.key === view.active);
        if (tab) view.load(tab);
    }
});

// Ctrl/Cmd+S saves, Ctrl/Cmd+F opens find & replace. The editor's own keymap
// already handles both while it has focus; this covers the cover being open
// with focus somewhere else (chat_nav no longer steals Ctrl+F inside .lb-cover).
document.addEventListener("keydown", (event) => {
    const key = String(event.key || "").toLowerCase();
    if (!(event.ctrlKey || event.metaKey) || event.altKey) return;

    // Ctrl/⌘+Shift+V asks for an unformatted paste even in Notes mode. The
    // paste event cannot see the modifiers, so remember the chord here; the
    // flag is one-shot (cleared by the paste handler) and expires on its own.
    if (key === "v" && event.shiftKey) {
        LB_PLAIN_PASTE = true;
        setTimeout(() => { LB_PLAIN_PASTE = false; }, 1200);
        return;
    }

    let view = null;
    try { view = Alpine.store("libretaView"); } catch (e) { return; }
    if (!view || !view.isOpen) return;

    if (key === "s") {
        event.preventDefault();
        view.save();
        return;
    }

    if (key === "p") {
        // only inside the cover: Ctrl+P is the browser's print shortcut
        // everywhere else (coder hijacks it app-wide; libreta does not).
        // The editor itself is contentEditable, so only real text fields are
        // excluded here.
        const target = event.target;
        const tag = target ? String(target.tagName || "").toUpperCase() : "";
        if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
        event.preventDefault();
        view.openQuick();
        return;
    }

    if (key === "f") {
        const target = event.target;
        const tag = target ? String(target.tagName || "").toUpperCase() : "";
        if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (target && target.isContentEditable)) return;
        if (!LB_VIEW || !LB_CM6) return;
        event.preventDefault();
        LB_CM6.openSearchPanel(LB_VIEW);
    }
});

// Esc closes the reading cover, except while typing in a field
document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;

    let view = null;
    try { view = Alpine.store("libretaView"); } catch (e) { return; }
    if (!view || !view.isOpen) return;

    // the quick-open palette closes first, then the cover
    if (view.quick && view.quick.open) {
        view.quickClose();
        event.preventDefault();
        return;
    }

    const target = event.target;
    const tag = target ? String(target.tagName || "").toUpperCase() : "";
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (target && target.isContentEditable)) return;

    view.hide();
});

// keep the docked panel and the space reserved for it in step when the window
// resizes (the width is pinned in px so padding and panel share one base)
window.addEventListener("resize", () => {
    try { Alpine.store("libretaView").syncDockWidth(); } catch (e) { /* store not ready */ }
});

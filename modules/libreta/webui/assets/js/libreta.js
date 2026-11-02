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
};

function lbErr(error) {
    if (error && typeof error === "object") return error.message || error.error || JSON.stringify(error);
    return String(error || "unknown error");
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

/* ---- CodeMirror (vendored under webui/assets/vendor/, lazy) ----

   The code view is a real editor, like Geany: numbers and wrap stay while you
type. Vendored and lazy-loaded exactly the way codeeditor does it (vendor/
sits outside assets/css+assets/js, so the channel never force-loads it). */
const LB_CM_BASE = "/ext-assets/libreta/vendor/codemirror/";

const LB_CM_CSS = ["lib/codemirror.css"];

const LB_CM_JS = [
    "lib/codemirror.js",
    "mode/xml/xml.js",
    "mode/javascript/javascript.js",
    "mode/css/css.js",
    "mode/htmlmixed/htmlmixed.js",
    "mode/markdown/markdown.js",
    "mode/python/python.js",
    "mode/shell/shell.js",
    "mode/yaml/yaml.js",
    "mode/toml/toml.js",
];

let LB_CM_LOAD = null;
let LB_CM = null;     // the live code editor (one doc per open tab)
let LB_BLK_CM = null; // the small in-place editor used by Notes block editing

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

function lbLoadCodeMirror() {
    if (!LB_CM_LOAD) {
        LB_CM_LOAD = (async () => {
            for (const file of LB_CM_CSS) await lbLoadCss(LB_CM_BASE + file);
            // sequential: script order is dependency order here
            for (const file of LB_CM_JS) await lbLoadScript(LB_CM_BASE + file);
        })();
    }
    return LB_CM_LOAD;
}

// extension -> CodeMirror mode. anything unmatched opens as plain text.
const LB_CM_MODE_BY_EXT = {
    py: "python", js: "javascript", mjs: "javascript", cjs: "javascript",
    json: "application/json", jsx: "javascript",
    css: "css", scss: "css",
    html: "htmlmixed", htm: "htmlmixed", xml: "xml", svg: "xml",
    md: "markdown", markdown: "markdown", mdx: "markdown",
    sh: "shell", bash: "shell", zsh: "shell",
    yml: "yaml", yaml: "yaml", toml: "toml",
};

function lbCmMode(path) {
    const ext = String(path || "").split(".").pop().toLowerCase();
    return LB_CM_MODE_BY_EXT[ext] || null;
}

function lbCmTheme() {
    try {
        const theme = Alpine.store("theme");
        return theme && theme.mode === "light" ? "lumara-light" : "lumara";
    } catch (e) {
        return "lumara";
    }
}

/* ---- Notes mode: split the source into editable blocks ----

   Line-based (no AST): a block ends at a blank line or at the start of
   another top-level construct (heading / fence / list / quote). Rendering
   each block's own source with the markdown renderer gives the same HTML a
   whole-document render would, so a block can be edited and re-rendered
   without ever rebuilding the rest of the file. BUGS: a table counts as a
   paragraph block (still renders correctly, just thicker). */
function lbBlocks(source) {
    const lines = String(source === undefined || source === null ? "" : source).split("\n");
    const blocks = [];
    let index = 0;

    const isBlank = (line) => line.trim() === "";
    const isFence = (line) => /^\s{0,3}(?:```|~~~)/.test(line);
    const isHeading = (line) => /^\s{0,3}#{1,6}\s/.test(line);
    const isItem = (line) => /^\s{0,3}(?:[-*+]|\d{1,3}[.)])\s+/.test(line);
    const isQuote = (line) => /^\s{0,3}>/.test(line);

    while (index < lines.length) {
        if (isBlank(lines[index])) {
            index++;
            continue;
        }

        const start = index;

        if (isFence(lines[index])) {
            index++;
            while (index < lines.length && !isFence(lines[index])) index++;
            if (index < lines.length) index++;   // closing fence
        } else if (isHeading(lines[index])) {
            index++;
        } else if (isItem(lines[index]) || isQuote(lines[index])) {
            const listish = isItem(lines[index]);
            index++;
            while (index < lines.length) {
                const line = lines[index];
                if (isBlank(line)) {
                    const next = lines[index + 1];
                    if (next === undefined) break;
                    if (listish ? (isItem(next) || /^\s{2,}/.test(next)) : isQuote(next)) {
                        index++;
                        continue;
                    }
                    break;
                }
                if (listish ? (isItem(line) || /^\s{2,}/.test(line)) : isQuote(line)) {
                    index++;
                    continue;
                }
                break;
            }
        } else {
            index++;
            while (
                index < lines.length &&
                !isBlank(lines[index]) && !isHeading(lines[index]) && !isFence(lines[index]) &&
                !isItem(lines[index]) && !isQuote(lines[index])
            ) index++;
        }

        const text = lines.slice(start, index).join("\n");
        blocks.push({ start: start, end: index - 1, text: text, html: lbRender(text) });
    }

    // an empty (or all-blank) file still needs one block to click into
    if (!blocks.length) {
        blocks.push({ start: 0, end: Math.max(lines.length - 1, 0), text: lines.join("\n"), html: "" });
    }

    return blocks;
}

/* --------------------------------------------------------- store: recents/pins */
const LIBRETA_DATA = {
    recent: [],
    pinned: [],
    loading: false,
    loaded: false,
    error: "",

    apply(data) {
        if (!data) return;
        this.recent = Array.isArray(data.recent) ? data.recent : [];
        this.pinned = Array.isArray(data.pinned) ? data.pinned : [];
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
};

/* ------------------------------------------------------- store: open documents */
const LIBRETA_VIEW = {
    isOpen: false,
    tabs: [],
    active: "",
    busy: false,
    error: "",
    autowrap: true,
    notesBlockEdit: true,  // notes mode: click a block to edit it in place
    split: false,          // notes mode: source + live preview side by side
    lineNumbers: true,     // interface preference (localStorage), not a config setting
    editorReady: false,
    cmFailed: false,       // CodeMirror could not load -> fall back to the rendered view
    conflict: null,        // {message, path} while a save was refused by the mtime guard
    pendingClose: null,    // {key, name} while an edited tab waits for a discard decision
    savedAt: 0,
    _silent: false,        // true while swapping docs / resetting buffers

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
        if (LB_CM) LB_CM.setOption("lineWrapping", this.wrapOn());
        requestAnimationFrame(() => { try { if (LB_CM) LB_CM.refresh(); } catch (e) { /* not mounted */ } });
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
        if (LB_CM) LB_CM.setOption("lineNumbers", this.lineNumbers);
        requestAnimationFrame(() => { try { if (LB_CM) LB_CM.refresh(); } catch (e) { /* not mounted */ } });
    },

    /* ---- persistence ---- */
    restore() {
        this.lineNumbers = lbReadJson("libreta.lineNumbers", true) !== false;
        this.split = lbReadJson("libreta.split", false) === true;

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
            html: "",
            mtime: 0,
            size: 0,
            dirty: false,
            error: "",
        };
    },

    async loadSettings() {
        // the module default for autowrap; the per-file toggle overrides it
        try {
            const data = await simpleApiFetch("/api/ext/libreta/settings");
            this.autowrap = !!(data.values && data.values.code_autowrap);
            this.notesBlockEdit = !(data.values && data.values.notes_block_edit === false);
        } catch (e) {
            /* keep the built-in default */
        }
    },

    /* ---- CodeMirror lifecycle ---- */
    // load the vendored CodeMirror library once (shared by Code mode and the
    // Notes block editor)
    async ensureLibrary() {
        if (typeof CodeMirror !== "undefined") return true;
        if (this.cmFailed) return false;
        try {
            await lbLoadCodeMirror();
            return true;
        } catch (e) {
            this.cmFailed = true;
            this.error = "could not load the code editor (" + lbErr(e) + ")";
            return false;
        }
    },

    async ensureEditor(host) {
        if (LB_CM) return true;
        if (!(await this.ensureLibrary())) return false;
        if (!host) return false;

        LB_CM = CodeMirror(host, {
            value: "",
            mode: null,
            theme: lbCmTheme(),
            lineNumbers: this.lineNumbers,
            lineWrapping: this.wrapOn(),
            tabSize: 4,
            indentUnit: 4,
            styleActiveLine: true,
            readOnly: false,
            extraKeys: {
                "Ctrl-S": () => Alpine.store("libretaView").save(),
                "Cmd-S": () => Alpine.store("libretaView").save(),
            },
        });

        LB_CM.on("change", () => {
            if (this._silent) return;
            const tab = this.activeTab();
            if (!tab) return;
            tab.dirty = true;
            // split view: keep the rendered pane in step with the source pane
            if (tab.mode === "notes" && this.split) this.schedulePreview(tab);
        });

        // CodeMirror renders a zero-width space inside EMPTY lines as a caret
        // placeholder, and browsers copy it silently. Take over the copy so
        // blank lines never turn into invisible characters in another file.
        LB_CM.on("copy", (cm, event) => {
            const text = cm.getSelection();
            if (!text) return;
            try {
                event.clipboardData.setData("text/plain", text.replace(/\u200b/g, ""));
                event.preventDefault();
            } catch (e) { /* let the browser copy normally */ }
        });

        window.addEventListener("theme-changed", () => {
            if (LB_CM) LB_CM.setOption("theme", lbCmTheme());
        });

        this.editorReady = true;
        return true;
    },

    // the buffer that should be saved/rendered: the live doc when the tab has one
    sourceOf(tab) {
        if (!tab) return "";
        return tab.doc ? tab.doc.getValue() : (tab.content || "");
    },

    async syncEditor() {
        const tab = this.activeTab();
        if (!tab) return;

        // the editor is visible in Code mode, and in Notes mode while split is on
        const wantEditor = tab.mode === "code" || (tab.mode === "notes" && tab.prose && this.split);
        if (!wantEditor) return;

        const host = document.getElementById("lb-editor-host");
        if (!(await this.ensureEditor(host))) return;

        // one doc per tab: swapDoc keeps each file's undo history separate
        this._silent = true;
        try {
            if (!tab.doc) tab.doc = CodeMirror.Doc(tab.content || "", lbCmMode(tab.path));
            LB_CM.swapDoc(tab.doc);
        } finally {
            this._silent = false;
        }

        LB_CM.setOption("mode", lbCmMode(tab.path));
        LB_CM.setOption("lineWrapping", this.wrapOn());
        LB_CM.setOption("lineNumbers", this.lineNumbers);
        LB_CM.setOption("theme", lbCmTheme());

        // the host may have been hidden (notes mode) when the editor was made
        requestAnimationFrame(() => { try { LB_CM.refresh(); } catch (e) { /* not mounted */ } });
        setTimeout(() => { try { LB_CM.refresh(); } catch (e) { /* not mounted */ } }, 120);
    },

    /* ---- notes mode: one block at a time (CM5, no split) ---- */
    blockModeOn() {
        const tab = this.activeTab();
        return !!tab && tab.mode === "notes" && tab.prose && tab.loaded
            && this.notesBlockEdit && !this.cmFailed;
    },

    blocksOf() {
        const tab = this.activeTab();
        return tab && tab.blocks ? tab.blocks : [];
    },

    editingIndex() {
        const tab = this.activeTab();
        return tab && typeof tab.editing === "number" ? tab.editing : -1;
    },

    ensureBlocks(tab) {
        if (!tab) return;
        const source = this.sourceOf(tab);
        if (tab.blocksSource === source) return;
        tab.blocksSource = source;
        tab.blocks = lbBlocks(source);
        tab.editing = -1;
    },

    blockClick(event, index) {
        if (!this.blockModeOn()) return;
        // links stay links
        if (event && event.target && event.target.closest && event.target.closest("a")) return;
        if (this.editingIndex() === index) return;

        this.commitBlockEdit();
        const tab = this.activeTab();
        if (tab) tab.editing = index;
    },

    async mountBlockEditor(index) {
        const tab = this.activeTab();
        if (!tab || !tab.blocks || !tab.blocks[index]) return;

        const host = document.getElementById("lb-blk-" + index);
        if (!host) return;
        if (!(await this.ensureLibrary())) return;

        LB_BLK_CM = CodeMirror(host, {
            value: tab.blocks[index].text,
            mode: "markdown",
            theme: lbCmTheme(),
            lineNumbers: false,
            lineWrapping: true,
            tabSize: 4,
            extraKeys: {
                Esc: () => this.commitBlockEdit(),
                "Ctrl-S": () => { this.commitBlockEdit(); this.save(); },
                "Ctrl-Enter": () => this.commitBlockEdit(),
            },
        });

        LB_BLK_CM.focus();
    },

    destroyBlockEditor() {
        const tab = this.activeTab();
        if (tab) tab.editing = -1;
        LB_BLK_CM = null;   // the host element is removed by Alpine
    },

    commitBlockEdit() {
        const tab = this.activeTab();
        if (!tab || this._blockBusy) return;

        const index = this.editingIndex();
        if (index < 0 || !LB_BLK_CM) { this.destroyBlockEditor(); return; }

        this._blockBusy = true;
        try {
            const block = tab.blocks ? tab.blocks[index] : null;
            const text = LB_BLK_CM.getValue();
            this.destroyBlockEditor();
            if (!block || text === block.text) return;

            const lines = this.sourceOf(tab).split("\n");
            lines.splice(block.start, block.end - block.start + 1, ...text.split("\n"));
            const next = lines.join("\n");

            tab.content = next;
            if (tab.doc) {
                // keep the Code-mode buffer in step with the block edit
                this._silent = true;
                tab.doc.setValue(next);
                this._silent = false;
            }
            tab.html = lbRender(next);
            tab.dirty = true;

            tab.blocksSource = null;
            this.ensureBlocks(tab);
        } finally {
            this._blockBusy = false;
        }
    },

    dirtyOf() {
        const tab = this.activeTab();
        return !!tab && !!tab.dirty;
    },

    async save(force) {
        const tab = this.activeTab();
        if (!tab) return;

        this.busy = true;
        this.error = "";
        try {
            const data = await simpleApiPost("/api/ext/libreta/save", {
                workspace: tab.workspace,
                path: tab.path,
                content: this.sourceOf(tab),
                mtime: force ? 0 : tab.mtime,
                force: !!force,
            });

            tab.content = this.sourceOf(tab);
            tab.mtime = data.mtime || tab.mtime;
            tab.size = data.size || tab.size;
            tab.dirty = false;
            tab.codeHtml = lbCodeHtml(tab.content, tab.path);
            tab.html = lbRender(tab.content);
            this.conflict = null;
            this.savedAt = Date.now();
            this.persist();
        } catch (e) {
            if (e && typeof e === "object" && e.conflict) {
                this.conflict = { message: e.message || "the file changed on disk", path: tab.path };
            } else {
                this.error = lbErr(e);
            }
        } finally {
            this.busy = false;
        }
    },

    async reloadFromDisk() {
        const tab = this.activeTab();
        if (!tab) return;
        this.conflict = null;
        await this.load(tab);
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

    /* ---- overlay ---- */
    show() {
        this.isOpen = true;
        try { document.body.dataset.lb = "1"; } catch (e) { /* no body yet */ }
        this.persist();
        try { Alpine.store("libretaData").load(); } catch (e) { /* not registered yet */ }
    },

    hide() {
        this.isOpen = false;
        try { delete document.body.dataset.lb; } catch (e) { /* no body yet */ }
        this.persist();
    },

    /* ---- documents ---- */
    async open(workspace, path, mode) {
        const key = workspace + "|" + path;
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
        this.active = key;
        this.persist();
        const tab = this.activeTab();
        if (!tab) return;
        if (!tab.loaded) await this.load(tab);   // load() syncs the editor itself
        else await this.syncEditor();
        if (tab.mode === "notes") this.ensureBlocks(tab);
    },

    async load(tab) {
        this.busy = true;
        this.error = "";
        tab.loading = true;
        tab.error = "";
        try {
            const data = await simpleApiFetch(
                "/api/ext/libreta/read?workspace=" + encodeURIComponent(tab.workspace) +
                "&path=" + encodeURIComponent(tab.path)
            );
            tab.content = data.content || "";
            tab.mtime = data.mtime || 0;
            tab.size = data.size || 0;
            tab.parent = data.parent || "";
            tab.loaded = true;
            tab.dirty = false;
            tab.doc = null;                       // fresh buffer; undo history starts here
            tab.codeHtml = lbCodeHtml(tab.content, tab.path);
            if (tab.mode === "notes") {
                tab.html = lbRender(tab.content);
                this.ensureBlocks(tab);
            }
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

        // carry the editor buffer across the switch: notes mode renders what the
        // doc currently holds, not the stale tab.content
        if (mode === "notes" && tab.loaded) {
            tab.html = lbRender(this.sourceOf(tab));
            this.ensureBlocks(tab);
        }

        tab.mode = mode;
        this.persist();
        if (mode === "code" || (mode === "notes" && this.split)) this.syncEditor();
    },

    close(key, force) {
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
        this.tabs.splice(index, 1);
        if (this.active === key) {
            const next = this.tabs[Math.min(index, this.tabs.length - 1)];
            this.active = next ? next.key : "";
            if (next && !next.loaded) this.load(next);
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
        if (this.active === oldKey) this.active = key;
        tab.key = key;
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
            Alpine.store("libretaView").autowrap = !!this.opts.values.code_autowrap;

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
    view.restore();

    if (view.isOpen) {
        const tab = view.tabs.find((item) => item.key === view.active);
        if (tab) view.load(tab);
    }
});

// Ctrl/Cmd+S saves the active document (works in notes mode too)
document.addEventListener("keydown", (event) => {
    const key = String(event.key || "").toLowerCase();
    if (key !== "s" || !(event.ctrlKey || event.metaKey)) return;

    let view = null;
    try { view = Alpine.store("libretaView"); } catch (e) { return; }
    if (!view || !view.isOpen) return;

    event.preventDefault();
    view.save();
});

// Esc closes the reading cover, except while typing in a field
document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;

    let view = null;
    try { view = Alpine.store("libretaView"); } catch (e) { return; }
    if (!view || !view.isOpen) return;

    const target = event.target;
    const tag = target ? String(target.tagName || "").toUpperCase() : "";
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (target && target.isContentEditable)) return;

    view.hide();
});

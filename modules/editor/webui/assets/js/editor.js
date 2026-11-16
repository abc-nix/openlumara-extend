/* -- AI GENERATED CODE (deepseek-v4-flash) :: 2026-10-16
   editor sidebar panel logic. Global object used by x-data in
   sidebars/editor.html; talks to /api/ext/editor/.
   Base: foldertree panel (workspaces, tree, name search, select mode,
   preview, sort) + coder panel (content search, right-click menu,
   prompt modal, duplicate) with Attach replacing "Ask AI". */
const EDITOR_PANEL = {
    // registered as an Alpine store on alpine:init (see bottom of file) so
    // the prompt modal (rendered at app root, outside this panel's x-data)
    // can bind reactively via $store.editorPanel, like coder's $store.coderPanel.
    root: null,            // {name, path, is_dir, open, loaded, busy, children, error, truncated, ws, rel}
    rows: [],              // [{key, depth, node, loading?}] flattened visible rows
    workspacePath: "",
    workspaceError: "",
    workspaces: [],        // [{path, name, exists}] browsable roots (from the workspace route)
    wsIndex: Number(localStorage.getItem('edWsIndex') || 0),  // selected workspace (picker mode only), persisted
    combineTrees: true,    // one tree with a branch per workspace (vs the picker)
    loaded: false,
    readOnly: false,       // module read-only mode: modifying actions refuse

    // view options (persisted across sessions, like the chats sidebar toggles)
    showHidden: localStorage.getItem('edShowHidden') === 'true',
    sortKey: localStorage.getItem('edSortKey') || 'name',   // name | modified | size
    sortDir: localStorage.getItem('edSortDir') === 'desc' ? 'desc' : 'asc',
    wideScroll: localStorage.getItem('edWideScroll') === 'true',

    // dual search: names (tree results) or content (flat results)
    searchMode: 'names',   // 'names' | 'content'
    searchOpen: false,
    searchQuery: "",
    searching: false,
    searchTree: null,
    searchNoMatch: false,
    searchTruncated: false,
    searchCap: "",           // why the scan stopped early: "scan" | "results" | ""
    searchMatchCount: 0,     // raw matches returned (rows add branch ancestors too)
    _searchSeq: 0,           // guards against out-of-order debounce responses
    contentRegex: false,     // content search: regex mode
    contentSearch: { running: false, error: "", results: [], truncated: false, done: false },
    _contentSeq: 0,

    selected: null,        // file node being previewed
    preview: null,         // {name, size, modified, kind, text, truncated}
    previewBusy: false,

    // right-click menu state
    menu: { open: false, x: 0, y: 0, row: null },

    // small prompt modal state (new file / new folder / rename / move)
    prompt: { mode: "", title: "", hint: "", value: "", submitting: false, error: "", row: null, parent: "" },

    // x-intersect fires on every tab switch to this panel
    async refresh() {
        if (this.loaded) return;
        await this.load();
    },

    init() {
        // follow the settings store: changes in the settings modal apply
        // immediately
        if (this._storeWatch) return;  // global object: watch once
        this._storeWatch = true;
        let last = null;
        Alpine.effect(() => {
            const s = Alpine.store("settings");
            const ed = s.categories && s.categories.user_modules
                && s.categories.user_modules.settings && s.categories.user_modules.settings.editor;
            const v = ed && ed.value
                ? JSON.stringify({
                    combine: !!ed.value.combine_trees.value,
                    inherit: !!ed.value.inherit_from_coder.value,
                    workspaces: (ed.value.workspaces.value || []).join("\n"),
                })
                : null;
            if (last === v) return;
            last = v;
            if (v === null) return;  // settings not loaded yet
            const parsed = JSON.parse(v);
            // write through the store proxy (== the panel's reactive scope):
            // mutating the raw global here would update the data without
            // ever notifying the x-show/x-text bindings (picker stayed hidden).
            const st = Alpine.store('editorPanel');
            if (st.combineTrees !== parsed.combine) st.combineTrees = parsed.combine;
            if (st.loaded) st.reload();
        });
    },

    // combine_trees from the settings store (null when not available yet)
    _settingsCombine() {
        const s = Alpine.store("settings");
        const ed = s.categories && s.categories.user_modules
            && s.categories.user_modules.settings && s.categories.user_modules.settings.editor;
        return ed && ed.value && ed.value.combine_trees ? !!ed.value.combine_trees.value : null;
    },

    async load() {
        this.workspaceError = "";
        try {
            const info = await simpleApiFetch('/api/ext/editor/workspace');
            this.workspaces = (info.workspaces && info.workspaces.length)
                ? info.workspaces
                : [{ path: info.path, name: info.path, exists: true }];
            this.readOnly = !!info.read_only;
            // the settings store wins; the route's `combine` is only the
            // fallback
            const storeCombine = this._settingsCombine();
            this.combineTrees = storeCombine !== null ? storeCombine : (info.combine !== false);
            if (this.wsIndex >= this.workspaces.length) this.wsIndex = 0;
            if (this.combineTrees && this.workspaces.length > 1) {
                // one tree: workspace contents are merged; the workspace
                // folders never appear as rows
                this.workspacePath = "";
                this.root = this.emptyNode("", "", true);
                this.root.open = true;
                this.root.loaded = true;
                const results = await Promise.all(this.workspaces.map(async (w, i) => {
                    try {
                        if (!w.exists) throw new Error("workspace folder does not exist: " + w.path);
                        const r = await simpleApiFetch(`/api/ext/editor/list?path=&ws=${i}`);
                        return { entries: r.entries, truncated: r.truncated, error: "" };
                    } catch (e) {
                        return { entries: [], truncated: false, error: String(e) };
                    }
                }));
                const children = [];
                let truncated = false;
                for (let i = 0; i < results.length; i++) {
                    const res = results[i];
                    if (res.error) {
                        const n = this.emptyNode(this.workspaces[i].name, this.workspaces[i].path, true);
                        n.error = res.error;
                        children.push(n);
                        continue;
                    }
                    truncated = truncated || res.truncated;
                    const parent = { path: this.workspaces[i].path, ws: i, rel: "" };
                    for (const en of res.entries) children.push(this.makeNode(en, parent));
                }
                this.root.children = children;
                this.root.truncated = truncated;
            } else {
                const ws = this.workspaces[this.wsIndex];
                this.workspacePath = ws.path;
                if (!ws.exists) {
                    this.workspaceError = 'workspace folder does not exist: ' + ws.path;
                    this.loaded = true;
                    return;
                }
                this.root = this.makeNode({ name: ws.path, path: ws.path, is_dir: true }, null);
                this.root.ws = this.wsIndex;
                this.root.open = true;
                await this.loadChildren(this.root);
            }
            this.buildRows();
            this.loaded = true;
        } catch (e) {
            console.error("editor panel:", e);
            this.workspaceError = String(e);
            this.loaded = true;
        }
    },

    // picker mode (combine_trees off): switching drops the cached tree and
    // reloads from the newly selected workspace
    async selectWorkspace(i) {
        i = Number(i);
        if (!this.workspaces[i] || i === this.wsIndex) return;
        this.searchOpen = false;  // stale search results must not survive a switch
        this.exitSearch();
        this.wsIndex = i;
        localStorage.setItem('edWsIndex', i);
        this.workspacePath = this.workspaces[i].path;
        this.root = null;
        this.rows = [];
        this.closePreview();
        this.loaded = false;
        await this.load();
    },

    // refresh button: drop all cached state and re-walk from the workspace root
    async reload() {
        this.searchOpen = false;  // stale search results must not survive a refresh
        this.exitSearch();
        this.root = null;
        this.rows = [];
        this.closePreview();
        this.loaded = false;
        await this.load();
    },

    setShowHidden(on) {
        this.showHidden = Boolean(on);
        localStorage.setItem('edShowHidden', this.showHidden);
        if (this.searching && this.searchMode === 'names') {
            // search results were built with the old hidden flag - re-run
            this.runSearch(this.searchQuery);
        } else {
            this.buildRows();  // re-walks cached children - instant, no refetch
        }
    },

    isVisible(node) {
        return this.showHidden || !String(node.name).startsWith(".");
    },

    setSort(key) {
        this.sortKey = key;
        localStorage.setItem('edSortKey', key);
        this.buildRows();
    },

    toggleSortDir() {
        this.sortDir = this.sortDir === 'asc' ? 'desc' : 'asc';
        localStorage.setItem('edSortDir', this.sortDir);
        this.buildRows();
    },

    // wide mode: rows keep their natural width; the tree scrolls sideways
    // instead of clipping names. Pure relayout, no refetch.
    setWideScroll(on) {
        this.wideScroll = Boolean(on);
        localStorage.setItem('edWideScroll', this.wideScroll);
    },

    // ---- search: names <-> content ----
    toggleSearch() {
        this.searchOpen = !this.searchOpen;
        if (!this.searchOpen) this.exitSearch();
    },

    toggleSearchMode() {
        this.searchMode = this.searchMode === 'content' ? 'names' : 'content';
        if (this.searchOpen && this.searchQuery.trim()) this.runSearch(this.searchQuery);
    },

    exitSearch() {
        this.searchQuery = "";
        this.searching = false;
        this.searchTree = null;
        this.searchNoMatch = false;
        this.searchTruncated = false;
        this.searchCap = "";
        this.contentSearch = { running: false, error: "", results: [], truncated: false, done: false };
        this.buildRows();  // instant switch back to the cached tree
    },

    runSearch(q) {
        this.searchQuery = q;
        const term = (q || "").trim();
        if (!term) { this.exitSearch(); return; }
        if (this.searchMode === 'content') return this.runContentSearch(term);
        return this.runNameSearch(term);
    },

    async runNameSearch(term) {
        if (!this.root) await this.load();

        const seq = ++this._searchSeq;
        this.searching = true;
        this.searchNoMatch = false;
        this.searchTruncated = false;
        this.searchCap = "";
        this.searchMatchCount = 0;
        const combined = this.combineTrees && this.workspaces.length > 1;
        this.searchTree = this.emptyNode(combined ? "Workspaces" : "", combined ? "" : this.workspacePath, true);
        this.searchTree.open = true;
        this.searchTree.loaded = true;

        try {
            // combined mode: one request per workspace, results merged (each
            // match remembers its workspace); picker mode: just the active one
            const wsList = combined ? this.workspaces.map((w, i) => i) : [this.wsIndex];
            const all = [];
            let truncated = false, capped = "";
            for (const i of wsList) {
                const r = await simpleApiFetch(`/api/ext/editor/search?q=${encodeURIComponent(term)}&hidden=${this.showHidden ? 1 : 0}&ws=${i}`);
                if (seq !== this._searchSeq) return;  // a newer query already landed
                for (const m of r.matches) all.push({ ...m, ws: i });
                // caps are per-workspace: one workspace hitting its cap must
                // not stop the others from being searched
                if (r.truncated) {
                    truncated = true;
                    if (r.capped === "scan" || !capped) capped = r.capped || capped;
                }
            }
            this.searchTruncated = truncated;
            this.searchCap = capped;
            this.searchMatchCount = all.length;
            this.searchNoMatch = all.length === 0;
            this.buildSearchTree(all);
        } catch (e) {
            if (seq !== this._searchSeq) return;
            this.searchTree.error = String(e);
        }
        this.buildRows();
    },

    async runContentSearch(term) {
        const seq = ++this._contentSeq;
        this.searching = true;
        this.contentSearch = { running: true, error: "", results: [], truncated: false, done: false };
        const combined = this.combineTrees && this.workspaces.length > 1;
        const wsList = combined ? this.workspaces.map((w, i) => i) : [this.wsIndex];

        try {
            const all = [];
            let truncated = false;
            for (const i of wsList) {
                const r = await simpleApiPost("/api/ext/editor/content", {
                    query: term,
                    regex: this.contentRegex,
                    ws: i,
                });
                if (seq !== this._contentSeq) return;
                for (const m of (r.results || [])) all.push({ ...m, ws: i });
                truncated = truncated || !!r.truncated;
            }
            this.contentSearch.results = all;
            this.contentSearch.truncated = truncated;
            this.contentSearch.done = true;
        } catch (e) {
            if (seq !== this._contentSeq) return;
            this.contentSearch.error = e && e.message ? e.message : String(e);
            this.contentSearch.done = true;
        } finally {
            if (seq === this._contentSeq) this.contentSearch.running = false;
        }
    },

    // click a content result: preview that file in the footer
    openContentResult(r) {
        const wsRoot = this.workspaces[r.ws] || { path: "", name: "" };
        const node = this.emptyNode(
            r.file.split("/").pop(),
            wsRoot.path ? `${wsRoot.path}/${r.file}` : r.file,
            false
        );
        node.rel = r.file;
        node.ws = r.ws;
        this.previewFile(node);
    },

    // matches -> tree with only the matched branches, ancestors kept and open.
    // No per-folder fetches; in combined mode keys are ws-prefixed so equal
    // paths cannot collide.
    buildSearchTree(matches) {
        const base = this.searchTree;
        const combined = this.combineTrees && this.workspaces.length > 1;
        const dirMap = { "": base };
        for (const m of matches) {
            const ws = m.ws ?? this.wsIndex;
            const wsRoot = this.workspaces[ws] || { path: "", name: "" };
            let parent = base;
            if (combined) {
                const wsKey = `${ws}|`;
                parent = dirMap[wsKey];
                if (!parent) {
                    parent = this.emptyNode(wsRoot.name, wsRoot.path, true);
                    parent.open = true;
                    parent.loaded = true;
                    parent.ws = ws;
                    parent.rel = "";
                    dirMap[wsKey] = parent;
                    base.children.push(parent);
                }
            }
            const parts = String(m.rel).split("/");
            let relSoFar = "";
            for (let i = 0; i < parts.length; i++) {
                relSoFar = relSoFar ? `${relSoFar}/${parts[i]}` : parts[i];
                const key = combined ? `${ws}|${relSoFar}` : relSoFar;
                let node = dirMap[key];
                if (!node) {
                    const abs = wsRoot.path ? `${wsRoot.path}/${relSoFar}` : relSoFar;
                    node = this.emptyNode(parts[i], abs, i < parts.length - 1 ? true : m.is_dir);
                    node.open = true;   // search tree is fully expanded
                    node.loaded = true; // and needs no per-folder fetches
                    node.ws = ws;
                    node.rel = relSoFar;
                    dirMap[key] = node;
                    parent.children.push(node);
                }
                parent = node;
            }
        }
    },

    // one node shape for tree rows and search tree alike. ws = workspace
    // index; rel = path inside that workspace ("" = its root).
    emptyNode(name, path, is_dir) {
        return {
            name, path, is_dir,
            size: null, modified: null,
            open: false, loaded: false, busy: false,
            truncated: false, error: "", children: [],
            ws: undefined, rel: "",
        };
    },

    makeNode(entry, parent) {
        const path = parent ? `${parent.path}/${entry.name}` : entry.path;
        const node = this.emptyNode(entry.name, path, entry.is_dir);
        node.size = entry.size ?? null;
        node.modified = entry.modified ?? null;
        node.ws = parent ? parent.ws : undefined;
        node.rel = parent ? (parent.rel ? `${parent.rel}/${entry.name}` : entry.name) : "";
        return node;
    },

    async loadChildren(node) {
        node.busy = true;
        node.error = "";
        try {
            const r = await simpleApiFetch(`/api/ext/editor/list?path=${encodeURIComponent(node.rel || "")}&ws=${node.ws ?? this.wsIndex}`);
            node.children = r.entries.map(en => this.makeNode(en, node));
            node.truncated = r.truncated;
        } catch (e) {
            // keep the folder open and show the error inline
            node.error = String(e);
            node.children = [];
        } finally {
            node.busy = false;
            node.loaded = true;
            this.buildRows();
        }
    },

    toggle(node) {
        node.open = !node.open;
        if (node.open && !node.loaded && !node.busy) this.loadChildren(node);
        this.buildRows();
    },

    buildRows() {
        const rows = [];
        const walk = (node, depth) => {
            for (const child of this.sortNodes(node.children.filter(c => this.isVisible(c)))) {
                rows.push({ key: child.path, depth, node: child });
                if (child.is_dir && child.open) {
                    if (child.loaded) walk(child, depth + 1);
                    else if (child.busy) rows.push({ key: child.path + "|loading", depth: depth + 1, node: child, loading: true });
                }
            }
        };
        const base = (this.searching && this.searchMode === 'names') ? this.searchTree : this.root;
        if (base && base.loaded) walk(base, 0);
        this.rows = rows;
    },

    // folders that failed to list get their error as a note under their row
    errorRows() {
        return this.rows.filter(r => r.node.is_dir && r.node.error);
    },

    async previewFile(node) {
        this.selected = node;
        this.preview = null;
        this.previewBusy = true;
        try {
            this.preview = await simpleApiFetch(`/api/ext/editor/preview?path=${encodeURIComponent(node.rel || "")}&ws=${node.ws ?? this.wsIndex}`);
        } catch (e) {
            this.preview = { name: node.name, size: node.size, modified: node.modified, kind: "error", text: null };
            console.error("editor preview:", e);
        } finally {
            this.previewBusy = false;
        }
    },

    closePreview() {
        this.selected = null;
        this.preview = null;
        this.previewBusy = false;
    },

    previewNote() {
        if (!this.preview) return "";
        if (this.preview.kind === "binary") return "binary file - no preview";
        if (this.preview.kind === "error") return "could not preview this file";
        if (this.preview.kind) return `binary file (.${this.preview.kind}) - no preview`;
        return "preview disabled (max_preview_kb = 0)";
    },

    formatSize(bytes) {
        if (bytes === null || bytes === undefined) return "";
        if (bytes < 1024) return `${bytes} B`;
        const kb = bytes / 1024;
        if (kb < 1024) return `${kb.toFixed(kb < 10 ? 1 : 0)} KB`;
        return `${(kb / 1024).toFixed(1)} MB`;
    },

    // ---- select mode: row clicks toggle selection instead of open/preview.
    // Session-only.
    selectMode: false,
    // keyed by path so it survives tree rebuilds. Not to be confused with
    // `selected` (previewed file).
    selectedItems: [],
    setSelectMode(on) {
        this.selectMode = Boolean(on);
        if (!this.selectMode) {
            this.selectedItems = [];  // leaving mode clears it
            this.renamingPath = null;
        }
    },
    isSelected(path) {
        return this.selectedItems.some(s => s.path === path);
    },
    toggleSelect(node) {
        // reassign, don't push/splice: Alpine tracks the array reference.
        const exists = this.selectedItems.some(s => s.path === node.path);
        this.selectedItems = exists
            ? this.selectedItems.filter(s => s.path !== node.path)
            : [...this.selectedItems, { path: node.path, name: node.name, is_dir: node.is_dir, ws: node.ws, rel: node.rel }];
    },

    // menu enable/disable rules:
    //  Attach: selection has at least one file and no folders
    //  Open / Rename: exactly one item
    //  Copy path: one or more
    selAllFiles() {
        return this.selectedItems.length > 0 && this.selectedItems.every(s => !s.is_dir);
    },
    selSingle() {
        return this.selectedItems.length === 1;
    },

    // ---- actions (menu items). selNote: transient feedback beside the count
    selNote: "",
    _noteTimer: null,
    renamingPath: null,      // row showing an inline rename editor
    flashNote(msg) {
        this.selNote = msg;
        clearTimeout(this._noteTimer);
        this._noteTimer = setTimeout(() => { this.selNote = ""; }, 2000);
    },

    async copyPaths() {
        const text = this.selectedItems.map(s => s.path).join("\n");
        let ok = false;
        try {
            await navigator.clipboard.writeText(text);
            ok = true;
        } catch (e) {
            // clipboard API needs document focus; old-school fallback
            const ta = document.createElement("textarea");
            ta.value = text;
            document.body.appendChild(ta);
            ta.select();
            try { ok = document.execCommand("copy"); } catch (_) {}
            ta.remove();
        }
        this.flashNote(ok ? (this.selectedItems.length === 1 ? "path copied" : this.selectedItems.length + " paths copied") : "copy failed");
    },

    async attachFiles() {
        const store = Alpine.store("upload");
        let attached = 0;
        for (const s of this.selectedItems) {
            try {
                const r = await simpleApiFetch(`/api/ext/editor/file?path=${encodeURIComponent(s.rel || "")}&ws=${s.ws ?? this.wsIndex}`);
                const bytes = Uint8Array.from(atob(r.b64), c => c.charCodeAt(0));
                store.files.push(new File([bytes], r.name, { type: r.mime }));
                attached++;
            } catch (e) {
                this.flashNote("attach: " + (e.message || e));
                return;
            }
        }
        this.flashNote(`attached ${attached} file(s)`);
    },

    // single-file attach for the right-click menu
    async attachRow(row) {
        const store = Alpine.store("upload");
        try {
            const r = await simpleApiFetch(`/api/ext/editor/file?path=${encodeURIComponent(row.rel || "")}&ws=${row.ws ?? this.wsIndex}`);
            const bytes = Uint8Array.from(atob(r.b64), c => c.charCodeAt(0));
            store.files.push(new File([bytes], r.name, { type: r.mime }));
            this.flashNote("attached " + r.name);
        } catch (e) {
            this.flashNote("attach: " + (e.message || e));
        }
    },

    async openSelected() {
        const s = this.selectedItems[0];
        if (!s) return;
        try {
            await simpleApiPost("/api/ext/editor/open", { path: s.rel || "", ws: s.ws ?? this.wsIndex });
            this.flashNote("opened " + s.name);
        } catch (e) {
            this.flashNote(e.message || String(e));
        }
    },

    startRename() {
        if (this.selectedItems.length === 1) this.renamingPath = this.selectedItems[0].path;
    },
    async commitRename(node, newName) {
        const oldPath = node.path;
        const oldRel = node.rel || "";
        this.renamingPath = null;
        const name = (newName || "").trim();
        if (!name || name === node.name) return;
        try {
            const r = await simpleApiPost("/api/ext/editor/rename", { path: oldRel, name, ws: node.ws ?? this.wsIndex });
            // keep rel pointing at the same parent under the new name
            const sep = oldRel.lastIndexOf("/");
            const newRel = sep === -1 ? r.name : `${oldRel.slice(0, sep)}/${r.name}`;
            node.name = r.name;
            node.path = r.path;
            node.rel = newRel;
            node.children = [];     // child path strings are stale now:
            node.loaded = false;    // refetch lazily
            if (node.is_dir && node.open) this.loadChildren(node);
            // reassign to keep Alpine reactivity (see toggleSelect)
            this.selectedItems = this.selectedItems.map(s =>
                s.path === oldPath ? { ...s, path: r.path, name: r.name } : s);
            this.buildRows();
            this.flashNote("renamed");
        } catch (e) {
            this.flashNote(e.message || String(e));
        }
    },

    sortLabel() {
        const labels = { name: 'Name', modified: 'Modified', size: 'Size' };
        const dir = this.sortDir === 'asc' ? 'A\u2013Z / oldest / smallest first' : 'Z\u2013A / newest / largest first';
        return `Sort: ${labels[this.sortKey] || this.sortKey} (${dir})`;
    },

    // view-side ordering over cached children: folders always first, then the
    // active key (name tie-break keeps ties stable). no refetch needed.
    sortNodes(nodes) {
        const dir = this.sortDir === 'desc' ? -1 : 1;
        const nameCmp = (a, b) => String(a.name).localeCompare(String(b.name), undefined, { sensitivity: 'base', numeric: true });
        return nodes.slice().sort((a, b) => {
            if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1;
            let cmp = 0;
            if (this.sortKey === 'modified') cmp = (a.modified ?? 0) - (b.modified ?? 0);
            else if (this.sortKey === 'size') cmp = (a.size ?? 0) - (b.size ?? 0);
            else cmp = nameCmp(a, b);
            if (cmp === 0) cmp = nameCmp(a, b);
            return cmp * dir;
        });
    },

    // ---- right-click context menu ----
    openMenu(event, row) {
        if (this.readOnly) return;
        // taller estimate so the menu never overflows the viewport edge
        const menuH = 300;
        this.menu = {
            open: true,
            x: Math.max(4, Math.min(event.clientX, window.innerWidth - 190)),
            y: Math.max(4, Math.min(event.clientY, window.innerHeight - menuH)),
            row: row,
        };
    },

    closeMenu() {
        this.menu.open = false;
    },

    rowIsDir(row) {
        return !!(row && row.is_dir);
    },

    menuAction(fn) {
        const row = this.menu.row;
        this.closeMenu();
        if (row) fn(row);
    },

    /* ---- small prompt modal (new file / new folder / rename / move) ---- */
    openPrompt(mode, row) {
        const parent = row.is_dir ? row.rel : String(row.rel || "").split("/").slice(0, -1).join("/");
        let title = "", value = "", hint = "";

        // where the new entry will actually land (files: their parent folder)
        const inPath = parent ? `${this.workspaces[row.ws ?? this.wsIndex]?.name || "workspace"}/${parent}` : (this.workspaces[row.ws ?? this.wsIndex]?.name || "workspace");

        if (mode === "new_file") { title = `New file in ${inPath}`; hint = "filename, e.g. notes.txt"; }
        if (mode === "new_folder") { title = `New folder in ${inPath}`; hint = "folder name"; }
        if (mode === "rename") { title = `Rename ${row.name}`; value = row.name; hint = "new name"; }
        if (mode === "move") { title = `Move ${row.name}`; value = row.rel; hint = "new path within the workspace"; }

        this.prompt = { mode, title, hint, value, submitting: false, error: "", row: row, parent };
        Alpine.store("ui").openModal("editor_prompt");
    },

    closePrompt() {
        this.prompt = { mode: "", title: "", hint: "", value: "", submitting: false, error: "", row: null, parent: "" };
        Alpine.store("ui").closeModal();
    },

    async submitPrompt() {
        const p = this.prompt;
        if (!p.mode || p.submitting) return;

        const name = (p.value || "").trim();
        if (!name) { p.error = "name can't be empty"; return; }
        if (name === "." || name === "..") { p.error = "nice try"; return; }
        if ((p.mode === "new_file" || p.mode === "new_folder" || p.mode === "rename") && name.includes("/")) {
            p.error = "just a plain name here, no path separators";
            return;
        }

        p.submitting = true;
        p.error = "";
        const ws = p.row.ws ?? this.wsIndex;

        try {
            if (p.mode === "new_file" || p.mode === "new_folder") {
                const target = p.parent ? p.parent + "/" + name : name;
                await simpleApiPost("/api/ext/editor/create", { path: target, is_dir: p.mode === "new_folder", ws });
                await this.reloadAfterChange([p.parent]);
            } else if (p.mode === "rename") {
                const parent = String(p.row.rel || "").split("/").slice(0, -1).join("/");
                const target = parent ? parent + "/" + name : name;
                await simpleApiPost("/api/ext/editor/move", { src: p.row.rel, dst: target, ws });
                await this.reloadAfterChange([parent]);
            } else if (p.mode === "move") {
                const oldParent = String(p.row.rel || "").split("/").slice(0, -1).join("/");
                const dst = name.replace(/^\/+/, "").replace(/\/+$/, "");
                const newParent = dst.includes("/") ? dst.split("/").slice(0, -1).join("/") : "";
                await simpleApiPost("/api/ext/editor/move", { src: p.row.rel, dst, ws });
                await this.reloadAfterChange([oldParent, newParent]);
            }

            this.closePrompt();
        } catch (e) {
            p.error = e && e.message ? e.message : String(e);
        } finally {
            p.submitting = false;
        }
    },

    async deleteRow(row) {
        const what = row.is_dir ? "folder" : "file";
        const extra = row.is_dir ? " Everything inside it will be removed too." : "";
        if (!window.confirm(`Delete ${what} ${row.path}?${extra}`)) return;

        try {
            await simpleApiPost("/api/ext/editor/delete", { path: row.rel || "", ws: row.ws ?? this.wsIndex });
            const parent = String(row.rel || "").split("/").slice(0, -1).join("/");
            await this.reloadAfterChange([parent]);
        } catch (e) {
            this.flashNote(e && e.message ? e.message : String(e));
        }
    },

    async duplicateRow(row) {
        try {
            await simpleApiPost("/api/ext/editor/duplicate", { path: row.rel || "", ws: row.ws ?? this.wsIndex });
            const parent = String(row.rel || "").split("/").slice(0, -1).join("/");
            await this.reloadAfterChange([parent]);
        } catch (e) {
            this.flashNote(e && e.message ? e.message : String(e));
        }
    },

    copyPath(row) {
        const text = row.path;  // absolute path: the most useful form
        if (navigator.clipboard) navigator.clipboard.writeText(text).catch(() => {});
    },

    // targeted tree refresh after a filesystem change: re-fetch the given
    // parent folders (workspace-relative). unknown/closed parents fall back
    // to a full reload so the tree can never show stale entries.
    async reloadAfterChange(parents) {
        let handled = false;
        for (const rel of (parents || [])) {
            const node = this.findNode(rel);
            if (node && node.loaded) {
                await this.loadChildren(node);
                handled = true;
            }
        }
        if (!handled) await this.reload();
    },

    findNode(rel) {
        if (!this.root) return null;
        if (!rel) return this.root;
        const walk = (node) => {
            for (const child of node.children) {
                if (child.rel === rel) return child;
                if (child.is_dir && child.loaded) {
                    const found = walk(child);
                    if (found) return found;
                }
            }
            return null;
        };
        return walk(this.root);
    },

    errorText(e) {
        if (e && typeof e === "object") return e.message || JSON.stringify(e);
        return String(e);
    }
};

// the prompt modal lives at app root (core modal slot), outside the panel's
// x-data scope, so it must reach the panel through a reactive store.
document.addEventListener("alpine:init", () => {
    Alpine.store("editorPanel", EDITOR_PANEL);
});

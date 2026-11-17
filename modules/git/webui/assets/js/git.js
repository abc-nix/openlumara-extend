/* -- AI GENERATED CODE (Qwen3.8 Flash Next) :: 2026-10-13
   git sidebar tab for the coder module: status, stage/unstage, commit,
   branch switching, log, and a diff viewer modal. talks to the
   /api/ext/git/git_* routes. has its own sandbox picker: it does NOT
   follow the coder filetree tab. */
const GIT_PANEL = {
    sandboxes: [],
    sandbox: "",
    subpath: "",
    repos: [],
    _autoPicked: false,
    _didRestore: false,
    read_only: false,
    ready: false,
    loading: false,
    error: "",
    is_repo: false,
    branch: "",
    branches: [],
    changes: [],
    message: "",
    committing: false,
    log: [],
    logFilter: "",
    logLimit: 100,
    logMore: false,
    logMoreLoading: false,
    op: { name: "none", detail: null },          // merge/rebase/… in progress
    upstream: null,
    copied: "",                                   // sha just copied to clipboard
    commitIndex: -1,                              // log position of the open commit
    blame: { path: "", groups: [], loading: false, error: "" },
    diff: {
        path: "", staged: false, commit: null, back_to: "", activeFile: "",
        lines: [], rows: [], loading: false,
    },
    push: {
        branch: "", remote: "", upstream: "", has_upstream: false, target: "",
        ahead: [], behind: 0, dirty: false, loading: false, pushing: false, error: "",
    },
    // -- AI GENERATED CODE (Qwen3.8 Flash Next) :: 2026-10-13
    // fetch/pull state
    pull: { loading: false, incoming: [], error: "", pulling: false },
    // branch management (manage-branches modal state)
    branchModal: { newName: "", confirming: null, submitting: false, error: "" },
    _currentBranch: "",
    // conflicts
    conflicts: [],


    async refresh(force = false) {
        // own sandbox picker (header button forces a rescan of both the
        // sandbox list and the repos inside the selected sandbox)
        if (!this.sandboxes.length || force) {
            try {
                const sd = await simpleApiFetch("/api/ext/git/workspaces") || {};
                this.sandboxes = sd.workspaces || [];
            } catch (e) {
                this.sandboxes = [];
            }
        }

        // selection vanished (sandbox removed from config): fall back
        if (this.sandbox && !this.sandboxes.includes(this.sandbox)) {
            this.sandbox = "";
            this.subpath = "";
            this.repos = [];
            this._autoPicked = false;
        }
        if (!this.sandbox && this.sandboxes.length) {
            // prefer the sandbox chosen before a page reload over the
            // plain first entry
            const saved = localStorage.getItem("git_sandbox") || "";
            this.sandbox = this.sandboxes.includes(saved) ? saved : this.sandboxes[0];
        }

        if (!this.sandbox) {
            this.error = "no workspaces configured";
            this.ready = true;
            return;
        }

        this.loading = true;
        this.error = "";

        // (re)discover repos inside the sandbox: the root itself plus nested
        // folders containing a .git, so 'dev/openlumara_dev' is selectable.
        // the header refresh button (refresh(true)) doubles as the rescan.
        if (!this.repos || !this.repos.length || force) {
            this._autoPicked = false;
            try {
                const rd = await simpleApiFetch(`/api/ext/git/git_repos?workspace=${encodeURIComponent(this.sandbox)}`);
                this.repos = rd.repos || [];
            } catch (e) {
                this.repos = [];
            }
        }

        // first load of the page only: restore the repo this sandbox had
        // before the reload (per-sandbox key, root = ""). once per page so
        // later refreshes never undo a manual pick of "(root)".
        if (!this.subpath && !this._didRestore) {
            this._didRestore = true;
            const savedRepo = localStorage.getItem("git_repo::" + this.sandbox) || "";
            if (this.repos.includes(savedRepo)) {
                this.subpath = savedRepo;
                this._autoPicked = true; // a restored choice beats auto-hop
            }
        }

        if (!this.repos || !this.repos.includes(this.subpath)) {
            this.subpath = "";
        }

        try {
            const data = await simpleApiFetch(`/api/ext/git/git_status?workspace=${encodeURIComponent(this.sandbox)}&subpath=${encodeURIComponent(this.subpath)}`);
            this.is_repo = !!data.is_repo;

            // sandbox root isn't a repo but the scan found nested ones:
            // hop into the first one automatically, otherwise the panel
            // would sit on "not a repository" with a repo right next to it
            if (!this.is_repo && this.repos.length && !this._autoPicked) {
                this._autoPicked = true;
                this.subpath = this.repos.find(r => r !== "") || this.repos[0];
                return this.refresh();
            }

            this.branch = data.branch || "";
            this._currentBranch = this.branch;
            this.branches = data.branches || [];
            this.changes = data.changes || [];
            this.read_only = !!data.read_only;
            // a stopped rebase leaves the tree looking clean; surface it
            this.op = { name: data.operation || "none", detail: data.operation_detail || null };
            this.upstream = data.upstream || null;
        } catch (e) {
            this.error = this.errorText(e);
            this.changes = [];
            this.op = { name: "none", detail: null };
            this.upstream = null;
        } finally {
            // remember the effective selection across page reloads
            this._save("git_sandbox", this.sandbox);
            this._save("git_repo::" + this.sandbox, this.subpath);
            this.loading = false;
            this.ready = true;
            // check for merge conflicts in the background
            this.loadConflicts();
        }
    },

    _save(key, val) {
        try { localStorage.setItem(key, val); } catch (e) {}
    },

    stagedCount() {
        return this.changes.filter(c => c.staged).length;
    },

    badge(c) {
        if (c.untracked) return "new";
        if (c.staged) return "staged";
        return "changed";
    },

    async _gitPost(route, payload) {
        this.error = "";
        try {
            await simpleApiPost(`/api/ext/git/${route}`, { workspace: this.sandbox, subpath: this.subpath, ...payload });
            await this.refresh();
            return true;
        } catch (e) {
            this.error = this.errorText(e);
            return false;
        }
    },

    async switchSandbox() {
        // sandbox already bound via x-model. repos are per-sandbox: hop
        // straight back into whatever was last selected for this one
        // (refresh resets the subpath again if it no longer exists)
        this.subpath = localStorage.getItem("git_repo::" + this.sandbox) || "";
        this.repos = [];
        this._autoPicked = false;
        await this.refresh();
    },

    async switchRepo() {
        // subpath already bound via x-model. a manual choice wins from now
        // on: never auto-hop over the user's selection again
        this._autoPicked = true;
        await this.refresh();
    },

    repoLabel(repo) {
        // the sandbox has its own select above, so labels are relative
        if (!repo) return "(root)";
        return repo;
    },

    stage(path) { return this._gitPost("git_stage", { paths: [path] }); },
    unstage(path) { return this._gitPost("git_unstage", { paths: [path] }); },
    stageAll() { return this._gitPost("git_stage", { paths: this.changes.map(c => c.path) }); },
    unstageAll() { return this._gitPost("git_unstage", { paths: this.changes.map(c => c.path) }); },

    async commit() {
        const msg = (this.message || "").trim();
        if (!msg || this.committing) return;

        this.committing = true;
        this.error = "";

        try {
            await simpleApiPost("/api/ext/git/git_commit", { workspace: this.sandbox, subpath: this.subpath, message: msg });
            this.message = "";
            await this.refresh();
            // keep the log modal fresh if it happens to be open
            await this.loadLog();
        } catch (e) {
            this.error = this.errorText(e);
        } finally {
            this.committing = false;
        }
    },

    async switchBranch(name) {
        if (!name || this.read_only) return;
        this.error = "";

        try {
            await simpleApiPost("/api/ext/git/git_branch", { workspace: this.sandbox, subpath: this.subpath, name });
        } catch (e) {
            // show the error AFTER refresh so it doesn't get wiped
            await this.refresh(true);
            this.error = this.errorText(e);
            return;
        }

        await this.refresh(true);
    },

    async openLog() {
        await this.loadLog();
        Alpine.store("ui").openModal("git_log");
    },

    closeLog() {
        Alpine.store("ui").closeModal();
    },

    async openPush() {
        // fetch exactly what would be pushed BEFORE showing the confirm
        // modal; errors (no remote, detached HEAD) go to the footer.
        if (this.push.loading || this.push.pushing) return;
        this.push.loading = true;
        this.push.error = "";

        try {
            const data = await simpleApiFetch(`/api/ext/git/git_ahead?workspace=${encodeURIComponent(this.sandbox)}&subpath=${encodeURIComponent(this.subpath)}`);
            const ahead = data.ahead || [];
            if (!ahead.length) {
                this.error = "nothing to push: every commit is already on the remote";
                return;
            }
            this.push = { ...this.push, ...data, ahead, loading: false, pushing: false, error: "" };
            Alpine.store("ui").openModal("git_push");
        } catch (e) {
            this.error = this.errorText(e);
        } finally {
            this.push.loading = false;
        }
    },

    closePush() {
        if (this.push.pushing) return; // never close mid-push
        Alpine.store("ui").closeModal();
    },

    /* -- AI GENERATED CODE (Qwen3.8 Flash Next) :: 2026-10-13
       fetch + pull # */
    async doFetch() {
        if (this.pull.loading) return;
        this.pull.loading = true;
        this.pull.error = "";
        try {
            const data = await simpleApiPost("/api/ext/git/git_fetch", {
                workspace: this.sandbox, subpath: this.subpath,
            });
            this.pull.incoming = data.incoming || [];
            if (!this.pull.incoming.length) {
                this.error = "already up to date";
            }
            await this.refresh();
        } catch (e) {
            this.pull.error = this.errorText(e);
        } finally {
            this.pull.loading = false;
        }
    },

    async doPull() {
        if (this.pull.pulling) return;
        this.pull.pulling = true;
        this.pull.error = "";
        try {
            const data = await simpleApiPost("/api/ext/git/git_pull", {
                workspace: this.sandbox, subpath: this.subpath,
            });
            if (data && data.pull_failed) {
                this.conflicts = data.conflicts || [];
                this.error = "pull finished with conflicts — resolve them below";
                Alpine.store("ui").openModal("git_conflicts");
            } else {
                this.conflicts = [];
                await this.refresh();
            }
        } catch (e) {
            this.pull.error = this.errorText(e);
        } finally {
            this.pull.pulling = false;
        }
    },

    /* -- AI GENERATED CODE (Qwen3.8 Flash Next) :: 2026-10-13
       branch select handler: if "manage branches" is picked, open the
       manager modal and revert the dropdown to the actual current branch. # */
    onBranchSelect() {
        if (this.branch === "__manage__") {
            this.branch = this._currentBranch || "";
            this.openBranchManager();
            return;
        }
        this.switchBranch(this.branch);
    },

    openBranchManager() {
        this._currentBranch = this.branch;
        Alpine.store("ui").openModal("git_branch");
    },

    async closeBranchManager() {
        Alpine.store("ui").closeModal();
        await this.refresh(true);
    },

    async createBranch(name) {
        if (!name || !name.trim()) return;
        this.branchModal.error = "";
        this.branchModal.submitting = true;
        try {
            await simpleApiPost("/api/ext/git/git_branch_create", {
                workspace: this.sandbox, subpath: this.subpath, name: name.trim(), checkout: true,
            });
            this.branchModal.newName = "";
            await this.refresh(true);
        } catch (e) {
            this.branchModal.error = this.errorText(e);
        } finally {
            this.branchModal.submitting = false;
        }
    },

    async deleteBranch(name) {
        this.branchModal.error = "";
        this.branchModal.submitting = true;
        try {
            await simpleApiPost("/api/ext/git/git_branch_delete", {
                workspace: this.sandbox, subpath: this.subpath, name: name, force: true,
            });
            this.branchModal.confirming = null;
            await this.refresh(true);
        } catch (e) {
            this.branchModal.error = this.errorText(e);
        } finally {
            this.branchModal.submitting = false;
        }
    },

    /* -- AI GENERATED CODE (Qwen3.8 Flash Next) :: 2026-10-13
       conflicts # */
    async loadConflicts() {
        try {
            const params = `workspace=${encodeURIComponent(this.sandbox)}&subpath=${encodeURIComponent(this.subpath)}`;
            const data = await simpleApiFetch("/api/ext/git/git_conflicts?" + params);
            this.conflicts = data.conflicts || [];
        } catch (e) {
            this.conflicts = [];
        }
    },

    async resolveConflict(path, strategy) {
        try {
            await simpleApiPost("/api/ext/git/git_resolve", {
                workspace: this.sandbox, subpath: this.subpath, path: path, strategy: strategy,
            });
            await this.loadConflicts();
            await this.refresh();
        } catch (e) {
            this.error = this.errorText(e);
        }
    },

    closeConflicts() {
        Alpine.store("ui").closeModal();
    },



    async doPush() {
        if (this.push.pushing) return;
        this.push.pushing = true;
        this.push.error = "";

        try {
            await simpleApiPost("/api/ext/git/git_push", { workspace: this.sandbox, subpath: this.subpath });
            Alpine.store("ui").closeModal();
            await this.refresh();
        } catch (e) {
            // keep the modal open so the rejection reason is readable
            this.push.error = this.errorText(e);
        } finally {
            this.push.pushing = false;
        }
    },

    async loadLog() {
        try {
            // n=100 per page: the backend default is only 20, which always
            // fits the modal body, so deeper history was unreachable. pages
            // beyond the first go through skip (loadMoreLog), not a bigger n.
            const data = await simpleApiFetch(`/api/ext/git/git_log?workspace=${encodeURIComponent(this.sandbox)}&subpath=${encodeURIComponent(this.subpath)}&n=${this.logLimit}&skip=0`);
            this.log = data.log || [];
            this.logMore = !!data.has_more;
        } catch (e) {
            this.log = [];
            this.logMore = false;
        }
    },

    async loadMoreLog() {
        if (this.logMoreLoading) return;
        this.logMoreLoading = true;
        try {
            const data = await simpleApiFetch(`/api/ext/git/git_log?workspace=${encodeURIComponent(this.sandbox)}&subpath=${encodeURIComponent(this.subpath)}&n=${this.logLimit}&skip=${this.log.length}`);
            this.log = this.log.concat(data.log || []);
            this.logMore = !!data.has_more;
        } catch (e) {
            this.error = this.errorText(e);
        } finally {
            this.logMoreLoading = false;
        }
    },

    // the modal can hold fewer or more rows than the page size; filtering
    // happens on what is loaded, and "load more" keeps extending it
    visibleLog() {
        const q = (this.logFilter || "").trim().toLowerCase();
        if (!q) return this.log;
        return this.log.filter(e =>
            (e.msg || "").toLowerCase().includes(q) ||
            (e.author || "").toLowerCase().includes(q) ||
            (e.hash || "").toLowerCase().startsWith(q));
    },

    async copySha(hash) {
        if (!hash) return;
        try {
            await navigator.clipboard.writeText(hash);
            this.copied = hash;
            setTimeout(() => { if (this.copied === hash) this.copied = ""; }, 1400);
        } catch (e) {
            this.error = this.errorText(e);
        }
    },

    // "open in editor" has no counterpart to call: the editor module exposes
    // previewFile() for workspace-relative nodes and nothing line-addressed,
    // so copying the path is the honest action here.
    async copyPath(path) {
        if (!path) return;
        try {
            await navigator.clipboard.writeText(path);
            this.copied = path;
            setTimeout(() => { if (this.copied === path) this.copied = ""; }, 1400);
        } catch (e) {
            this.error = this.errorText(e);
        }
    },


    openDiff(c) {
        this.diff = {
            path: c.path, staged: c.staged, untracked: !!c.untracked,
            commit: null, back_to: "", lines: ["loading.."], rows: [], loading: true,
        };
        Alpine.store("ui").openModal("git_diff");

        const params = `workspace=${encodeURIComponent(this.sandbox)}&subpath=${encodeURIComponent(this.subpath)}` +
            `&path=${encodeURIComponent(c.path)}&staged=${c.staged ? 1 : 0}&untracked=${c.untracked ? 1 : 0}`;

        // fetch unified (mobile) and split (desktop) in parallel
        const unified = simpleApiFetch(`/api/ext/git/git_diff?${params}`)
            .then(d => { this.diff.lines = String(d.diff || "(no changes)").split("\n"); })
            .catch(e => { this.diff.lines = [this.errorText(e)]; });

        const split = simpleApiFetch(`/api/ext/git/git_split_diff?${params}`)
            .then(d => { this.diff.rows = d.rows || []; })
            .catch(() => { this.diff.rows = []; });

        Promise.all([unified, split]).finally(() => { this.diff.loading = false; });
    },

    // commit diff: same modal as a file diff, but with the commit header
    // (subject, author, date, body, per-file +/- counts) above the patch.
    // one route call serves both views, so the unified and split bodies can
    // not disagree about what the commit contains.
    async openCommit(hash) {
        if (!hash) return;
        this.diff = {
            path: "", staged: false, commit: { loading: true }, back_to: "git_log",
            activeFile: "", lines: ["loading.."], rows: [], loading: true,
        };
        Alpine.store("ui").openModal("git_diff");

        const params = `workspace=${encodeURIComponent(this.sandbox)}` +
            `&subpath=${encodeURIComponent(this.subpath)}&rev=${encodeURIComponent(hash)}`;
        try {
            const data = await simpleApiFetch(`/api/ext/git/git_commit_diff?${params}`) || {};
            this.diff.commit = Object.assign({ loading: false }, data.commit || {}, {
                files: data.files || [],
                truncated: !!data.truncated,
            });
            this.diff.lines = String(data.diff || "(no changes)").split("\n");
            this.diff.rows = data.rows || [];
        } catch (e) {
            this.diff.commit = { loading: false, subject: this.errorText(e) };
            this.diff.lines = [this.errorText(e)];
            this.diff.rows = [];
        } finally {
            this.diff.loading = false;
        }
    },

    closeDiff() {
        // a commit was opened from the log: go back to it instead of dumping
        // the user out of the whole panel
        const back = this.diff.back_to;
        this.diff.back_to = "";
        if (back) {
            Alpine.store("ui").openModal(back);
            return;
        }
        Alpine.store("ui").closeModal();
    },

    // -- commit browsing -----------------------------------------------------
    // navigation follows the FILTERED log, so ←/→ walks exactly what the user
    // sees in the list behind the modal
    _commitPosition() {
        const list = this.visibleLog();
        const c = this.diff.commit;
        if (!c) return { list, index: -1 };
        const index = list.findIndex(e => e.hash === c.short || e.hash === c.hash
            || (c.hash || "").startsWith(e.hash));
        return { list, index };
    },

    hasPrevCommit() { return this._commitPosition().index < this.visibleLog().length - 1; },
    hasNextCommit() { return this._commitPosition().index > 0; },

    commitNav(delta) {
        const { list, index } = this._commitPosition();
        if (index < 0) return;
        const next = list[index - delta];
        if (!next) return;
        this.openCommit(next.hash);
    },

    // jump to a file's hunks in whichever view is visible (both render one
    // DOM node per row, so the row index is the child index)
    jumpToFile(path) {
        this.diff.activeFile = path;
        const idx = (this.diff.rows || []).findIndex(r => r.type === "file" && r.path === path);
        if (idx < 0) return;
        for (const sel of [".git-diff-split .git-split-table tbody", ".git-diff-unified"]) {
            const host = document.querySelector(sel);
            const el = host && host.children && host.children[idx];
            if (el && el.scrollIntoView) el.scrollIntoView({ block: "start" });
        }
    },

    // [{index, adds, dels}] for the open file's diff, in hunk order - the
    // same order the server rebuilds when it applies one hunk
    hunkList() {
        const out = [];
        const seen = new Map();
        for (const row of this.diff.rows || []) {
            if (row.type === "file" || row.hunk === undefined || row.hunk < 0) continue;
            let h = seen.get(row.hunk);
            if (!h) {
                h = { index: row.hunk, adds: 0, dels: 0 };
                seen.set(row.hunk, h);
                out.push(h);
            }
            if (row.type === "add") h.adds += 1;
            else if (row.type === "del") h.dels += 1;
            else if (row.type === "mod") { h.adds += 1; h.dels += 1; }
        }
        return out;
    },

    // one hunk of the open file, without leaving the diff modal
    async stageHunk(row, direction) {
        if (this.read_only || this.diff.untracked || !this.diff.path || this.diff.loading) return;
        this.diff.loading = true;
        const c = { path: this.diff.path, staged: direction !== "stage", untracked: false };
        try {
            await simpleApiPost("/api/ext/git/git_stage_hunk", {
                workspace: this.sandbox, subpath: this.subpath,
                path: this.diff.path, hunk: row.hunk, direction,
            });
            await this.refresh();
            this.openDiff(c);   // hunk indexes shift: reload the same view
        } catch (e) {
            this.error = this.errorText(e);
            this.diff.loading = false;
        }
    },

    // -- blame ---------------------------------------------------------------
    async openBlame(path) {
        if (!path) return;
        this.blame = { path, groups: [], loading: true, error: "" };
        Alpine.store("ui").openModal("git_blame");
        const params = `workspace=${encodeURIComponent(this.sandbox)}` +
            `&subpath=${encodeURIComponent(this.subpath)}&path=${encodeURIComponent(path)}`;
        try {
            const data = await simpleApiFetch(`/api/ext/git/git_blame?${params}`) || {};
            this.blame = {
                path, groups: data.groups || [], loading: false,
                error: data.truncated ? "truncated - showing the first groups only" : "",
            };
        } catch (e) {
            this.blame = { path, groups: [], loading: false, error: this.errorText(e) };
        }
    },

    closeBlame() {
        Alpine.store("ui").closeModal();
    },

    /* open-in-editor was removed: it depended on the coder panel
       (Alpine.store("coderPanel")), which is gone once coder is disabled.
       the diff modal stays read-only for now. */

    errorText(e) {
        if (e && typeof e === "object") return e.message || JSON.stringify(e);
        return String(e);
    }
};

document.addEventListener("alpine:init", () => {
    Alpine.store("gitPanel", GIT_PANEL);
});

// -- AI GENERATED CODE (Qwen3.8 Flash Next) :: 2026-10-13
// fresh Proxy per mount (see shell.js shellTermComponent for why x-data
// must never receive the store object directly)
function gitPanelComponent() {
    const target = {};
    const src = () => Alpine.store("gitPanel") || GIT_PANEL;

    return new Proxy(target, {
        get(t, key) {
            if (key === Symbol.unscopables) return undefined;
            if (key in t) return t[key];
            const val = src()[key];
            return typeof val === "function" ? val.bind(src()) : val;
        },
        set(t, key, value) {
            if (key in t) {
                t[key] = value;
                return true;
            }
            src()[key] = value;
            return true;
        },
        has(t, key) {
            return key in t || key in src();
        },
        ownKeys() {
            return [...new Set([...Reflect.ownKeys(target), ...Object.keys(src())])];
        },
        getOwnPropertyDescriptor(t, key) {
            if (key in t) return Reflect.getOwnPropertyDescriptor(t, key);
            if (key in src()) {
                return { value: src()[key], enumerable: true, configurable: true, writable: true };
            }
            return undefined;
        },
    });
}

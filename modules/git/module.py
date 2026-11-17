# git: webui git panel extracted from the coder module (phase 3, 2026-10-18).
# Routes + helpers ported from modules/coder/module.py (2026-10-13);
# the coder "sandbox" concept is replaced by editor-style workspaces
# (basename / full path / 0-based index selectors), read-only comes from
# this module's own setting, and git_repos prunes with `skip_dirs`.
# AI tools: the official git MCP server's 12-tool set (local-repo focus),
# a workspaces discovery tool, and a few read-only additions the MCP set
# lacks - reflog, compare (merge-base + ahead/behind) and remotes. the split
# is deliberate: reads and index-local writes are tools, while network and
# history-rewriting operations (push/pull/fetch/rebase) stay human-initiated
# and live in the webui panel only.

import asyncio
import os
import subprocess
import time
import regex

import core
import channels.webui.api as webui


class Git(core.module.Module):
    """Git sidebar tab: status, stage/unstage, commit, branch switching,
    log, diff viewer, fetch/pull/push and conflict resolution."""

    category = "development"
    dependencies = ["regex"]

    settings = {
        "workspaces": {
            "default": ["."],
            "description": "Folders the git panel may work in (same selectors as the editor module: basename, full path or 0-based index). Empty falls back to the openlumara main folder.",
            "depends": {"inherit_from_editor": False},
        },
        "inherit_from_editor": {
            "default": True,
            "description": "Use the editor module's workspaces as the git workspaces (and its skip_dirs as the skip list). Falls back to the manual setting when the editor module is off.",
        },
        "read-only": {
            "default": False,
            "description": "Disables all modifying git operations (stage/unstage/commit/branch/push/fetch/pull/resolve/revert).",
        },
        "skip_dirs": {
            "default": [],
            "description": "Folder names to skip when discovering nested repositories (e.g. node_modules). Matched by exact name at any depth.",
        },
        "repo_scan_depth": {
            "type": "number",
            "default": 6,
            "description": "How deep git_workspaces descends to discover nested repositories (1 = only direct children of each workspace). Lower is faster on big folders.",
        },
        "max_diff_chars": {
            "type": "number",
            "default": 20000,
            "description": "Maximum characters git_diff/git_show return per call (0 = unlimited).",
        },
    }

    # -------------
    # workspace resolution (editor-style)
    # -------------
    def _editor(self):
        manager = getattr(self, "manager", None)
        if manager is None:
            return None
        return (getattr(manager, "modules", None) or {}).get("editor")

    def _workspace_info(self):
        """(roots, inherited): folders the git panel may work in.
        inherit_from_editor -> the editor module's effective workspace roots
        (in editor's order), but only while editor yields at least one root;
        any fallback (editor off, no roots) returns the manual `workspaces`
        list with inherited=False."""
        editor = self._editor() if self.config.get("inherit_from_editor") else None
        if editor:
            roots = []
            try:
                for r in editor._workspace_roots():
                    roots.append(str(r))
            except Exception:
                roots = []
            if roots:
                return roots, True
        raw_list = self.config.get("workspaces") or []
        if not raw_list:
            raw_list = ["."]
        roots = []
        for raw in raw_list:
            try:
                roots.append(os.path.realpath(os.path.expanduser(raw)).rstrip(os.sep))
            except Exception:
                continue
        return roots, False

    def _skip_dirs(self):
        """folder names to skip when discovering nested repositories.
        inherit_from_editor -> the editor module's effective skip list."""
        if self.config.get("inherit_from_editor"):
            editor = self._editor()
            if editor:
                try:
                    return list(editor._skip_dirs())
                except Exception:
                    pass
        return self.config.get("skip_dirs") or []

    def _workspace_root(self, selector):
        """basename / full path / 0-based index -> absolute workspace root.

        also accepts '<workspace>/<subpath>' (or '<index>/<subpath>', or a full
        path below a workspace root), so a repo nested inside a workspace can be
        addressed in one string: 'git/openlumara' instead of two arguments."""
        roots, _ = self._workspace_info()
        if not roots:
            raise Exception("no workspaces configured")
        sel = str(selector or "").strip()
        if sel.isdigit():
            idx = int(sel)
            if 0 <= idx < len(roots):
                return roots[idx]
            raise Exception(f"workspace index {idx} out of range (available: {len(roots)})")
        for r in roots:
            if os.path.basename(r) == sel or r == sel:
                return r
        # a path below a workspace: full path first, then 'workspace/subpath'
        for r in roots:
            if sel.startswith(r + os.sep):
                return self._inside_root(r, sel[len(r) + 1:])
        if "/" in sel:
            head, rest = sel.split("/", 1)
            if head.isdigit() and 0 <= int(head) < len(roots):
                return self._inside_root(roots[int(head)], rest)
            for r in roots:
                if os.path.basename(r) == head:
                    return self._inside_root(r, rest)
        # a bare folder name inside a workspace ('openlumara', not
        # 'git/openlumara'): accept it when exactly one workspace has it,
        # so callers do not have to know which root a repo lives under
        if sel and sel not in (".", ".."):
            hits = [os.path.join(r, sel) for r in roots
                    if os.path.isdir(os.path.join(r, sel))]
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                raise Exception(
                    f"'{sel}' exists in several workspaces ({', '.join(hits)}) - "
                    "use '<workspace>/<subpath>' to pick one")
        raise Exception(
            f"workspace '{sel}' not found (available: {', '.join(os.path.basename(r) for r in roots)}). "
            "a repo nested inside a workspace is addressed as '<workspace>/<subpath>', "
            "e.g. 'git/openlumara' - list them with the workspaces tool"
        )

    def _inside_root(self, root, rest):
        """joins a workspace-relative subpath onto a root, refusing escapes."""
        full = os.path.normpath(os.path.join(root, rest))
        rroot = os.path.realpath(root)
        if full != rroot and not full.startswith(rroot + os.sep):
            raise Exception(f"subpath escapes the workspace: {rest}")
        return full

    def _git_root(self, workspace, subpath=""):
        """resolves workspace + optional subpath (workspace-relative) into the
        folder git should run in. lets git work on repos nested inside a
        workspace, e.g. 'openlumara' within the 'git' workspace."""
        root = self._workspace_root(workspace)
        if not subpath:
            return root
        return self._inside_root(root, subpath)

    def _check_git_paths(self, workspace_root, root, paths):
        """ensures repo-relative paths passed to git resolve inside the
        workspace root. without a subpath the repo root IS the workspace
        root, so this only bites for repos nested inside a workspace."""
        for p in paths:
            full = os.path.normpath(os.path.join(root, p))
            if full != workspace_root and not full.startswith(workspace_root + os.sep):
                raise Exception(f"path escapes the workspace: {p}")

    # -------------
    # git helpers
    # -------------
    async def _run_git_bytes(self, git_root: str, *args, timeout=15):
        """runs git, returns (returncode, raw stdout bytes). no text mode,
        no stripping - for output that must be parsed byte-exactly."""
        try:
            # availability fix: subprocess.run inside an async def blocks the
            # WHOLE event loop until git exits (up to the timeout). to_thread
            # isolates the wait; stdin=DEVNULL stops git hanging on a
            # credential prompt; start_new_session detaches the child from the
            # app's process group.
            proc = await asyncio.to_thread(
                subprocess.run,
                ["git", *args],
                cwd=git_root,
                capture_output=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except FileNotFoundError:
            return 127, b"git executable not found"
        except subprocess.TimeoutExpired:
            return 124, b"git command timed out"
        except Exception as e:
            return 1, str(e).encode("utf-8", "replace")

        return proc.returncode, proc.stdout or b""

    def _parse_porcelain_z(self, raw: bytes):
        """parses `git status --porcelain=v1 -z` output from raw bytes.

        VERIFIED against real git output with od -c (2026-10-13): the
        separator is a BARE NUL, and every record is exactly
        'XY<sp>path' - including codes whose first char is a space (' M').
        renames/copies emit 'XY<sp>oldpath' followed by a separate field
        holding the new path. with -z, paths are never quoted and never
        mangled. returns a list of dicts: path / xy / staged / untracked."""

        def decode(data: bytes) -> str:
            return data.decode("utf-8", "replace").replace(os.sep, "/")

        fields = raw.split(b"\x00")

        entries = []
        idx = 0
        while idx < len(fields):
            field = fields[idx]
            idx += 1

            if not field:
                # empty piece after the trailing NUL
                continue

            if len(field) < 3 or field[2:3] != b" ":
                # not 'XY path' - malformed for us, skip rather than guess
                continue

            x = chr(field[0])
            y = chr(field[1])
            path = decode(field[3:])

            if x in ("R", "C") or y in ("R", "C"):
                # next field is the NEW path (kept); consume it either way
                if idx < len(fields):
                    newp = fields[idx]
                    idx += 1
                    if newp:
                        path = decode(newp)

            entries.append({
                "path": path,
                "xy": x + y,
                "staged": x not in (" ", "?"),
                "untracked": x == "?" and y == "?",
            })

        return entries

    # porcelain XY codes that mean "unmerged / conflict"
    _CONFLICT_XY = ("DD", "AU", "UD", "UA", "DU", "AA", "UU")

    async def _git_dir(self, root):
        """absolute path of the repo's git directory. resolved through git so
        worktrees and submodules (where .git is a file) also work."""
        ok, out = await self._run_git(root, "rev-parse", "--git-dir")
        if not ok or not out:
            return ""
        return out if os.path.isabs(out) else os.path.abspath(os.path.join(root, out))

    def _read_number(self, path):
        """first integer in a git state file (msgnum, next, ...), 0 if absent."""
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                return int((fh.read() or "").strip() or 0)
        except Exception:
            return 0

    async def _operation_state(self, root):
        """which multi-step operation is currently half-finished.

        a rebase stopped on a conflict leaves the working tree looking exactly
        like a clean one; without this the caller cannot tell "nothing to do"
        from "you are 2 of 10 commits into a rebase". returns
        {"operation": "none"} or {"operation": name, "step": "2/10", ...}."""
        gd = await self._git_dir(root)
        if not gd:
            return {"operation": "none"}
        rebase_merge = os.path.join(gd, "rebase-merge")
        rebase_apply = os.path.join(gd, "rebase-apply")
        if os.path.isdir(rebase_merge):
            cur = self._read_number(os.path.join(rebase_merge, "msgnum"))
            total = self._read_number(os.path.join(rebase_merge, "end"))
            return {"operation": "rebase", "interactive": True,
                    "step": f"{cur}/{total}" if total else (str(cur) if cur else "")}
        if os.path.isdir(rebase_apply):
            cur = self._read_number(os.path.join(rebase_apply, "next"))
            total = self._read_number(os.path.join(rebase_apply, "last"))
            return {"operation": "rebase", "interactive": False,
                    "step": f"{cur}/{total}" if total else (str(cur) if cur else "")}
        for marker, name in (("MERGE_HEAD", "merge"),
                             ("CHERRY_PICK_HEAD", "cherry-pick"),
                             ("REVERT_HEAD", "revert"),
                             ("BISECT_LOG", "bisect")):
            if os.path.exists(os.path.join(gd, marker)):
                return {"operation": name}
        return {"operation": "none"}

    async def _upstream_state(self, root):
        """{name, ahead, behind} for the current branch, or None when it has no
        upstream. 'behind' is how many upstream commits are missing locally."""
        ok, up = await self._run_git(root, "rev-parse", "--abbrev-ref", "@{u}")
        up = (up or "").strip()
        if not ok or "/" not in up:
            return None
        ahead = behind = 0
        ok, out = await self._run_git(root, "rev-list", "--left-right", "--count", f"{up}...HEAD")
        parts = (out or "").replace("\t", " ").split()
        if ok and len(parts) == 2 and all(p.isdigit() for p in parts):
            behind, ahead = int(parts[0]), int(parts[1])
        return {"name": up, "ahead": ahead, "behind": behind}

    def _parse_blame_porcelain(self, text, max_groups=200, max_lines_per_group=200):
        """parses `git blame --line-porcelain` into runs of consecutive lines
        that share a commit. --line-porcelain repeats the header for every
        line, so consecutive equal hashes are coalesced into one group.

        returns (groups, truncated): each group is
        {hash, short, author, date, summary, first_line, last_line, count, lines}."""
        raw = []
        cur = {}
        for line in text.split("\n"):
            if regex.match(r"^[0-9a-f]{40} ", line):
                parts = line.split()
                cur = {"hash": parts[0],
                       "line": int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0}
            elif line.startswith("author "):
                cur["author"] = line[len("author "):].strip()
            elif line.startswith("author-time "):
                cur["ts"] = line[len("author-time "):].strip()
            elif line.startswith("summary "):
                cur["summary"] = line[len("summary "):].strip()
            elif line.startswith("\t"):
                raw.append({
                    "hash": cur.get("hash", ""),
                    "author": cur.get("author", ""),
                    "ts": cur.get("ts", ""),
                    "summary": cur.get("summary", ""),
                    "line": cur.get("line", 0),
                    "text": line[1:],
                })
                cur = {}

        groups = []
        truncated = False
        for entry in raw:
            date = ""
            try:
                date = time.strftime("%Y-%m-%d", time.localtime(int(entry["ts"])))
            except (TypeError, ValueError):
                date = ""
            prev = groups[-1] if groups else None
            if (prev and prev["hash"] == entry["hash"]
                    and prev["last_line"] + 1 == entry["line"]):
                prev["last_line"] = entry["line"]
                prev["count"] += 1
                if len(prev["lines"]) < max_lines_per_group:
                    prev["lines"].append({"line": entry["line"], "text": entry["text"]})
                else:
                    truncated = True
                continue
            if len(groups) >= max_groups:
                truncated = True
                break
            groups.append({
                "hash": entry["hash"],
                "short": entry["hash"][:8],
                "author": entry["author"],
                "date": date,
                "summary": entry["summary"],
                "first_line": entry["line"],
                "last_line": entry["line"],
                "count": 1,
                "lines": [{"line": entry["line"], "text": entry["text"]}],
            })
        return groups, truncated

    async def _run_git(self, git_root: str, *args, accept=(0,), timeout=15):
        """runs git in the workspace/repo root. returns (ok, output).
        accept lists return codes that count as success."""
        try:
            # see _run_git_bytes: to_thread keeps the loop free, DEVNULL and a
            # new session guard against prompts and teardown
            proc = await asyncio.to_thread(
                subprocess.run,
                ["git", *args],
                cwd=git_root,
                capture_output=True,
                text=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except FileNotFoundError:
            return False, "git executable not found"
        except subprocess.TimeoutExpired:
            return False, "git command timed out"
        except Exception as e:
            return False, str(e)

        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()

        if proc.returncode not in accept:
            return False, (err or out or f"git {args[0] if args else ''} failed (code {proc.returncode})")

        return True, (out or err)

    async def _run_git_stdin(self, git_root: str, input_text: str, *args, accept=(0,), timeout=30):
        """runs git with `input_text` on stdin - used by `git apply -`, the
        only way to stage a single hunk without touching the worktree."""
        try:
            # no stdin=DEVNULL here: `input` already supplies stdin (git apply -)
            proc = await asyncio.to_thread(
                subprocess.run,
                ["git", *args],
                cwd=git_root,
                capture_output=True,
                text=True,
                timeout=timeout,
                input=input_text,
                start_new_session=True,
            )
        except FileNotFoundError:
            return False, "git executable not found"
        except subprocess.TimeoutExpired:
            return False, "git command timed out"
        except Exception as e:
            return False, str(e)

        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode not in accept:
            return False, (err or out or f"git {args[0] if args else ''} failed (code {proc.returncode})")
        return True, (out or err or "applied")

    # -------------
    # AI tools: the MCP 12-tool baseline (no remote ops on purpose - the user
    # manages push/pull) + read-only additions: reflog, compare, remotes
    # -------------
    def _as_list(self, value):
        """normalize a list-typed argument (list / JSON string / comma string)."""
        if value is None:
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            s = value.strip()
            if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
                s = s[1:-1]
            if s.startswith("[") and s.endswith("]"):
                import json as _json
                import ast as _ast
                for parser in (_json.loads, _ast.literal_eval):
                    try:
                        parsed = parser(s)
                        if isinstance(parsed, list):
                            return parsed
                    except Exception:
                        continue
            if "," in s:
                return [x.strip() for x in s.split(",") if x.strip()]
            return [s]
        return [value]

    def _num(self, key, fallback):
        """read a numeric setting defensively."""
        try:
            return int(float(self.config.get(key)))
        except (TypeError, ValueError):
            return fallback

    async def _repo(self, workspace, subpath):
        """(root or None, error): workspace+subpath -> repo root + repo check."""
        try:
            root = self._git_root(workspace, subpath)
        except Exception as e:
            return None, str(e)
        ok, _ = await self._run_git(root, "rev-parse", "--is-inside-work-tree")
        if not ok:
            return None, f"'{root}' is not a git repository"
        return root, None

    def _find_repos(self, root):
        """relative slash paths of git repos inside root: the root itself
        (as '') plus nested repos up to `repo_scan_depth` levels (default 6);
        a repo's subtree is skipped once found, so the list stays shallow."""
        self._repo_scan_truncated = False
        if not os.path.isdir(root):
            return []
        blacklist = self._skip_dirs()
        try:
            max_depth = max(1, int(self.config.get("repo_scan_depth", 6)))
        except (TypeError, ValueError):
            max_depth = 6
        max_repos = 50
        repos = []

        def is_repo(path):
            return os.path.isdir(os.path.join(path, ".git")) or os.path.isfile(os.path.join(path, ".git"))

        if is_repo(root):
            repos.append("")

        for dirpath, dirnames, _ in os.walk(root):
            rel = os.path.relpath(dirpath, root).replace(os.sep, "/")
            depth = 0 if rel == "." else len(rel.split("/"))

            # prune hidden + blacklisted folders, and anything too deep
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in blacklist and depth + 1 <= max_depth]

            if depth == 0 or depth > max_depth:
                continue

            if is_repo(dirpath):
                repos.append(rel)
                dirnames[:] = []  # skip the repo's own subtree
                if len(repos) >= max_repos:
                    self._repo_scan_truncated = True
                    break

        return repos

    @core.module.tool
    async def workspaces(self):
        """lists configured git workspaces, the git repositories inside them, and whether the module is read-only"""
        roots, inherited = self._workspace_info()
        workspaces = []
        repos = []
        truncated = False
        for r in roots:
            name = os.path.basename(r) or r
            workspaces.append({"name": name, "path": r})
            for rel in self._find_repos(r):
                entry = {"workspace": name, "path": rel}
                if not rel:
                    # the workspace folder is itself a repo
                    entry["is_root"] = True
                repos.append(entry)
            truncated = truncated or bool(getattr(self, "_repo_scan_truncated", False))
        return self.result({
            "read_only": bool(self.config.get("read-only")),
            "inherited": inherited,
            "workspaces": workspaces,
            "repos": repos,
            "repos_truncated": truncated,
        })

    @core.module.tool
    async def status(self, workspace: str, subpath: str = ""):
        """shows the working tree status: branch, in-progress operation (merge/rebase/cherry-pick/revert/bisect), conflicts, upstream tracking (ahead/behind) and staged/unstaged/untracked files"""
        root, err = await self._repo(workspace, subpath)
        if err:
            return self.result({"is_repo": False, "note": err})
        _, branch = await self._run_git(root, "rev-parse", "--abbrev-ref", "HEAD")
        rc, raw = await self._run_git_bytes(root, "status", "--porcelain=v1", "-z")
        if rc != 0:
            # never report a failed status as a clean tree
            return self.result(
                raw.decode("utf-8", "replace").strip() or "git status failed", success=False)
        changes = self._parse_porcelain_z(raw)
        op = await self._operation_state(root)
        out = {
            "is_repo": True,
            "root": root,
            "branch": branch,
            "operation": op["operation"],
            "conflicts": [c["path"] for c in changes if c["xy"] in self._CONFLICT_XY],
            "upstream": await self._upstream_state(root),
            "staged": [c["path"] for c in changes if c["staged"]],
            "unstaged": [c["path"] for c in changes if not c["staged"] and not c["untracked"]],
            "untracked": [c["path"] for c in changes if c["untracked"]],
        }
        if op["operation"] != "none":
            out["operation_detail"] = op
            out["note"] = (f"{op['operation']} in progress"
                           + (f" ({op['step']})" if op.get("step") else "")
                           + " - resolve or --abort before other work")
        return self.result(out)

    async def _diff(self, workspace, subpath, args, paths=None):
        root, err = await self._repo(workspace, subpath)
        if err:
            return self.result(err, success=False)
        if paths:
            try:
                self._check_git_paths(self._workspace_root(workspace), root, paths)
            except Exception as e:
                return self.result(str(e), success=False)
            args = [*args, "--", *paths]
        ok, out = await self._run_git(root, *args)
        if not ok:
            return self.result(out, success=False)
        cap = self._num("max_diff_chars", 20000)
        if cap and len(out) > cap:
            return self.result({
                "diff": out[:cap],
                "truncated": True,
                "note": f"diff truncated at {cap} chars ({len(out)} total) - narrow the path or read files directly",
            })
        return self.result({"diff": out, "truncated": False})

    @core.module.tool
    async def diff_unstaged(self, workspace: str, subpath: str = ""):
        """shows the working tree diff (changes not staged yet)"""
        return await self._diff(workspace, subpath, ["diff"])

    @core.module.tool
    async def diff_staged(self, workspace: str, subpath: str = ""):
        """shows the staged diff (changes ready to commit)"""
        return await self._diff(workspace, subpath, ["diff", "--staged"])

    @core.module.tool
    async def diff(self, workspace: str, target: str, subpath: str = "",
                   stat: bool = False, paths: list = ()):
        """shows the diff between the current state and a target branch/commit; target also accepts ranges ('a..b', 'a...b'); stat=True returns only the file summary; paths narrows the diff to those repo-relative files"""
        target = str(target or "").strip()
        if not target or target.startswith("-"):
            return self.result("target is required (a branch, commit, tag or range)",
                               success=False)
        plist = [str(p) for p in self._as_list(paths) if str(p).strip()]
        return await self._diff(workspace, subpath, ["diff", target]
                                + (["--stat"] if stat else []), plist)

    @core.module.tool
    async def add(self, workspace: str, files: list, subpath: str = ""):
        """stages files into the index (['.'] or repo-relative paths; empty = stage all)"""
        if self.config.get("read-only"):
            return self.result("git module is in read-only mode", success=False)
        root, err = await self._repo(workspace, subpath)
        if err:
            return self.result(err, success=False)
        paths = [str(p) for p in self._as_list(files) if str(p).strip()] or ["."]
        try:
            self._check_git_paths(self._workspace_root(workspace), root, paths)
        except Exception as e:
            return self.result(str(e), success=False)
        ok, out = await self._run_git(root, "add", "--", *paths)
        if not ok:
            return self.result(out, success=False)
        return self.result(f"staged: {', '.join(paths)}")

    async def _unstage(self, workspace, subpath):
        if self.config.get("read-only"):
            return self.result("git module is in read-only mode", success=False)
        root, err = await self._repo(workspace, subpath)
        if err:
            return self.result(err, success=False)
        ok, out = await self._run_git(root, "reset")
        if not ok:
            return self.result(out, success=False)
        return self.result(out or "unstaged all changes")

    @core.module.tool
    async def unstage(self, workspace: str, subpath: str = ""):
        """unstages everything that is staged (working tree edits are kept) - same as `reset`, named after what it actually does"""
        return await self._unstage(workspace, subpath)

    @core.module.tool
    async def reset(self, workspace: str, subpath: str = ""):
        """unstages all staged changes (working tree edits are kept); alias of `unstage`, kept for compatibility. this is a plain `git reset`, it does NOT discard changes"""
        return await self._unstage(workspace, subpath)

    @core.module.tool
    async def commit(self, workspace: str, message: str, subpath: str = ""):
        """records staged changes as a new commit with the given message"""
        if self.config.get("read-only"):
            return self.result("git module is in read-only mode", success=False)
        msg = str(message or "").strip()
        if not msg:
            return self.result("commit message is required", success=False)
        root, err = await self._repo(workspace, subpath)
        if err:
            return self.result(err, success=False)
        ok, out = await self._run_git(root, "commit", "-m", msg)
        if not ok:
            return self.result(out, success=False)
        return self.result(out or "committed")

    @core.module.tool
    async def log(self, workspace: str, subpath: str = "", max_count: int = 10, revision: str = ""):
        """shows recent commits (hash, author, date, subject), capped at 50. revision selects what to walk: a branch, tag or commit, or a range like 'upstream/dev..HEAD'; empty means HEAD"""
        root, err = await self._repo(workspace, subpath)
        if err:
            return self.result(err, success=False)
        limit = max(1, min(int(max_count or 10), 50))
        rev = str(revision or "").strip()
        if rev.startswith("-"):
            return self.result("revision must be a branch, tag, commit or range (not an option)",
                               success=False)
        # ask for one more than the caller wants: "truncated" is otherwise a
        # guess, and a history of exactly `limit` commits is not truncated
        rc, raw = await self._run_git_bytes(
            root, "log", "--pretty=%h%x09%an%x09%ad%x09%s", "--date=short",
            f"-n{limit + 1}", *([rev] if rev else []))
        if rc != 0:
            return self.result(
                raw.decode("utf-8", "replace").strip() or "git log failed", success=False)
        entries = []
        for line in raw.decode("utf-8", "replace").splitlines():
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            entries.append({
                "hash": parts[0],
                "author": parts[1],
                "date": parts[2],
                "subject": "\t".join(parts[3:]),
            })
        return self.result({
            "commits": entries[:limit],
            "truncated": len(entries) > limit,
            "revision": rev or "HEAD",
        })

    @core.module.tool
    async def create_branch(self, workspace: str, branch_name: str, subpath: str = "", base_branch: str = ""):
        """creates a new branch (optionally from a base branch)"""
        if self.config.get("read-only"):
            return self.result("git module is in read-only mode", success=False)
        name = str(branch_name or "").strip()
        if not name or name.startswith("-"):
            return self.result("branch name is required", success=False)
        root, err = await self._repo(workspace, subpath)
        if err:
            return self.result(err, success=False)
        base = str(base_branch or "").strip()
        args = ["branch", name] + ([base] if base else [])
        ok, out = await self._run_git(root, *args)
        if not ok:
            return self.result(out, success=False)
        return self.result(f"created branch {name}")

    @core.module.tool
    async def checkout(self, workspace: str, branch_name: str, subpath: str = ""):
        """switches to a branch or revision"""
        if self.config.get("read-only"):
            return self.result("git module is in read-only mode", success=False)
        name = str(branch_name or "").strip()
        if not name or name.startswith("-"):
            return self.result("branch name is required", success=False)
        root, err = await self._repo(workspace, subpath)
        if err:
            return self.result(err, success=False)
        ok, out = await self._run_git(root, "checkout", name)
        if not ok:
            return self.result(out, success=False)
        return self.result(out or f"switched to {name}")

    @core.module.tool
    async def show(self, workspace: str, revision: str, subpath: str = ""):
        """shows the contents of a commit, or of a file at a revision (HEAD:path)"""
        rev = str(revision or "").strip()
        if not rev or rev.startswith("-"):
            return self.result("revision is required (commit hash, branch, tag or HEAD:path)", success=False)
        root, err = await self._repo(workspace, subpath)
        if err:
            return self.result(err, success=False)
        ok, out = await self._run_git(root, "show", rev)
        if not ok:
            return self.result(out, success=False)
        cap = self._num("max_diff_chars", 20000)
        if cap and len(out) > cap:
            return self.result({
                "content": out[:cap],
                "truncated": True,
                "note": f"output truncated at {cap} chars ({len(out)} total)",
            })
        return self.result({"content": out, "truncated": False})

    @core.module.tool
    async def branch(self, workspace: str, subpath: str = ""):
        """lists local branches: `branches` are the names (current one in `current`), `details` adds each branch's upstream, ahead/behind track and short sha"""
        root, err = await self._repo(workspace, subpath)
        if err:
            return self.result(err, success=False)
        _, current = await self._run_git(root, "rev-parse", "--abbrev-ref", "HEAD")
        ok, out = await self._run_git(root, "branch", "--format=%(refname:short)")
        branches = [b.strip() for b in out.splitlines() if b.strip()] if ok else []
        details = []
        ok, out = await self._run_git(
            root, "for-each-ref",
            "--format=%(refname:short)%09%(upstream:short)%09%(upstream:track)%09%(objectname:short)",
            "refs/heads")
        if ok:
            for line in out.splitlines():
                parts = (line.split("\t") + ["", "", "", ""])[:4]
                name = parts[0].strip()
                if not name:
                    continue
                details.append({
                    "name": name,
                    "upstream": parts[1].strip(),
                    # git renders it as '[ahead 1, behind 50]'; drop the brackets
                    "track": parts[2].strip().strip("[]"),
                    "sha": parts[3].strip(),
                    "current": name == current,
                })
        return self.result({"current": current, "branches": branches, "details": details})

    @core.module.tool
    async def reflog(self, workspace: str, subpath: str = "", max_count: int = 30, revision: str = ""):
        """shows where HEAD (or another ref's) tip has been: selector, hash, date and action, e.g. 'rebase (start)', 'rebase (pick)', 'reset: moving to ...'. the fastest way to see what a failed rebase did"""
        root, err = await self._repo(workspace, subpath)
        if err:
            return self.result(err, success=False)
        rev = str(revision or "").strip() or "HEAD"
        if rev.startswith("-"):
            return self.result("revision must be a branch, tag or ref (not an option)", success=False)
        limit = max(1, min(int(max_count or 30), 100))
        # no %gd: with --date=short it renders 'HEAD@{2026-11-03}', which is
        # useless for follow-up commands. the index selector ('HEAD@{0}') is
        # the position in this list instead, so it can be pasted back into git.
        rc, raw = await self._run_git_bytes(
            root, "reflog", "show", rev, "--date=short",
            "--pretty=%h%x09%ad%x09%gs", f"-n{limit + 1}")
        if rc != 0:
            return self.result(
                raw.decode("utf-8", "replace").strip() or "git reflog failed", success=False)
        entries = []
        for index, line in enumerate(raw.decode("utf-8", "replace").splitlines()):
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            entries.append({
                "selector": f"{rev}@{{{index}}}",
                "hash": parts[0],
                "date": parts[1],
                "action": "\t".join(parts[2:]),
            })
        return self.result({"revision": rev, "entries": entries[:limit],
                            "truncated": len(entries) > limit})

    @core.module.tool
    async def compare(self, workspace: str, a: str = "HEAD", b: str = "", subpath: str = ""):
        """compares two revisions: their merge base plus how many commits each side has that the other does not. a defaults to HEAD, b defaults to the current branch's upstream"""
        root, err = await self._repo(workspace, subpath)
        if err:
            return self.result(err, success=False)
        rev_a = str(a or "").strip() or "HEAD"
        rev_b = str(b or "").strip()
        if rev_a.startswith("-") or rev_b.startswith("-"):
            return self.result("revisions must be branches, tags or commits (not options)",
                               success=False)
        if not rev_b:
            ok, up = await self._run_git(root, "rev-parse", "--abbrev-ref", "@{u}")
            rev_b = (up or "").strip()
            if not ok or not rev_b:
                return self.result(
                    "b is required (no upstream configured for the current branch)", success=False)
        ok, base = await self._run_git(root, "merge-base", rev_a, rev_b)
        if not ok:
            return self.result(base or f"no common ancestor between {rev_a} and {rev_b}",
                               success=False)
        a_only = b_only = 0
        ok, out = await self._run_git(root, "rev-list", "--left-right", "--count",
                                      f"{rev_a}...{rev_b}")
        parts = (out or "").replace("\t", " ").split()
        if ok and len(parts) == 2 and all(p.isdigit() for p in parts):
            a_only, b_only = int(parts[0]), int(parts[1])
        return self.result({
            "a": rev_a,
            "b": rev_b,
            "merge_base": base,
            "a_only": a_only,
            "b_only": b_only,
        })

    @core.module.tool
    async def blame(self, workspace: str, path: str, subpath: str = "", max_groups: int = 200):
        """shows which commit last touched each line of a file, as runs of consecutive lines: hash, author, date, summary and the line numbers"""
        rel = str(path or "").strip()
        if not rel or rel.startswith("-"):
            return self.result("path is required (repo-relative)", success=False)
        root, err = await self._repo(workspace, subpath)
        if err:
            return self.result(err, success=False)
        try:
            self._check_git_paths(self._workspace_root(workspace), root, [rel])
        except Exception as e:
            return self.result(str(e), success=False)
        ok, out = await self._run_git(
            root, "blame", "--line-porcelain", "--", os.path.normpath(os.path.join(root, rel)))
        if not ok:
            return self.result(out, success=False)
        groups, truncated = self._parse_blame_porcelain(out, max_groups=max(1, int(max_groups or 200)))
        return self.result({"path": rel, "groups": groups, "truncated": truncated})

    @core.module.tool
    async def remotes(self, workspace: str, subpath: str = ""):
        """lists the repo's remotes (fetch and push URLs) and when they were last fetched"""
        root, err = await self._repo(workspace, subpath)
        if err:
            return self.result(err, success=False)
        ok, out = await self._run_git(root, "remote", "-v")
        remotes = {}
        if ok:
            for line in out.splitlines():
                parts = line.split()
                if len(parts) < 3:
                    continue
                name, url, kind = parts[0], parts[1], parts[2].strip("()")
                entry = remotes.setdefault(name, {"name": name, "fetch": "", "push": ""})
                if kind == "fetch":
                    entry["fetch"] = url
                elif kind == "push":
                    entry["push"] = url
        last_fetch = None
        gd = await self._git_dir(root)
        if gd:
            try:
                last_fetch = time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(os.path.join(gd, "FETCH_HEAD"))))
            except Exception:
                last_fetch = None
        return self.result({"remotes": list(remotes.values()), "last_fetch": last_fetch})

    # -------------
    # webui routes
    # -------------
    @webui.route("workspaces")
    async def _route_workspaces(self, body=None, query=None):
        """the workspace list for the panel picker + read-only state."""
        roots, inherited = self._workspace_info()
        return {
            "workspaces": [os.path.basename(r) for r in roots],
            "read_only": bool(self.config.get("read-only")),
            "inherited": inherited,
        }

    @webui.route("git_repos")
    async def _route_git_repos(self, body=None, query=None):
        """lists git repositories inside a workspace: the workspace root
        itself if it is one, plus nested repos up to 6 folder levels deep.
        once a repo is found its subtree is skipped, so the list stays
        shallow."""
        workspace = (query or {}).get("workspace", "")

        if not workspace:
            return self.result("no workspace given", success=False)

        try:
            root = self._workspace_root(workspace)
        except Exception as e:
            return self.result(str(e), success=False)

        return {"repos": self._find_repos(root)}

    @webui.route("git_status")
    async def _route_git_status(self, body=None, query=None):
        workspace = (query or {}).get("workspace", "")
        subpath = (query or {}).get("subpath", "")

        if not workspace:
            return self.result("no workspace given", success=False)

        try:
            root = self._git_root(workspace, subpath)
        except Exception as e:
            return self.result(str(e), success=False)

        ok, _ = await self._run_git(root, "rev-parse", "--is-inside-work-tree")
        if not ok:
            return {"is_repo": False, "read_only": bool(self.config.get("read-only"))}

        _, branch = await self._run_git(root, "rev-parse", "--abbrev-ref", "HEAD")

        ok_b, b_out = await self._run_git(root, "branch", "--format=%(refname:short)")
        branches = [b.strip() for b in b_out.split("\n") if b.strip()] if ok_b else []

        # nul-delimited porcelain, parsed from raw bytes (no text mode,
        # no stripping) by a dedicated parser with explicit separator
        # handling. see _parse_porcelain_z for the exact byte format.
        rc_s, raw_s = await self._run_git_bytes(root, "status", "--porcelain=v1", "-z")
        changes = self._parse_porcelain_z(raw_s) if rc_s == 0 else []

        # the panel shows the same operation state the AI tool reports: a
        # stopped rebase leaves a clean-looking tree, which used to be
        # indistinguishable from "nothing to do"
        op = await self._operation_state(root)

        return {
            "is_repo": True,
            "root": root,
            "branch": branch,
            "branches": branches,
            "changes": changes,
            "operation": op["operation"],
            "operation_detail": op if op["operation"] != "none" else None,
            "conflicts": [c["path"] for c in changes if c["xy"] in self._CONFLICT_XY],
            "upstream": await self._upstream_state(root),
            "read_only": bool(self.config.get("read-only")),
        }

    @webui.route("git_recent")
    async def _route_git_recent(self, body=None, query=None):
        """lightweight recent-commit peek (kept from coder for API parity)."""
        workspace = (query or {}).get("workspace", "")
        subpath = (query or {}).get("subpath", "")
        limit = min(int((query or {}).get("n", 5)), 20)

        if not workspace:
            return self.result("no workspace given", success=False)

        try:
            root = self._git_root(workspace, subpath)
        except Exception as e:
            return self.result(str(e), success=False)

        ok, _ = await self._run_git(root, "rev-parse", "--is-inside-work-tree")
        if not ok:
            return {"is_repo": False}

        _, branch = await self._run_git(root, "rev-parse", "--abbrev-ref", "HEAD")

        rc, raw = await self._run_git_bytes(
            root, "log", f"--pretty=%h%x09%s", f"-n{limit}"
        )
        if rc != 0:
            # a repo without any commits yet is not an error worth surfacing
            return {"is_repo": True, "branch": branch, "log": []}

        entries = []
        for line in raw.decode("utf-8", "replace").splitlines():
            if not line.strip():
                continue
            if "\t" in line:
                h, msg = line.split("\t", 1)
            else:
                h, msg = line, ""
            entries.append({"hash": h.strip(), "msg": msg})

        return {"is_repo": True, "branch": branch, "log": entries}

    @webui.route("git_stage", method="POST")
    async def _route_git_stage(self, body=None, query=None):
        if self.config.get("read-only"):
            return self.result("git module is in read-only mode", success=False)

        workspace = (body or {}).get("workspace", "")
        subpath = (body or {}).get("subpath", "")
        paths = (body or {}).get("paths", [])

        if not workspace or not paths:
            return self.result("workspace and paths are both required", success=False)

        try:
            root = self._git_root(workspace, subpath)
            wroot = self._workspace_root(workspace)
            self._check_git_paths(wroot, root, paths)
            abs_paths = [os.path.normpath(os.path.join(root, p)) for p in paths]
        except Exception as e:
            return self.result(str(e), success=False)

        ok, out = await self._run_git(root, "add", "--", *abs_paths)
        if not ok:
            return self.result(out, success=False)

        return self.result("staged")

    @webui.route("git_unstage", method="POST")
    async def _route_git_unstage(self, body=None, query=None):
        if self.config.get("read-only"):
            return self.result("git module is in read-only mode", success=False)

        workspace = (body or {}).get("workspace", "")
        subpath = (body or {}).get("subpath", "")
        paths = (body or {}).get("paths", [])

        if not workspace or not paths:
            return self.result("workspace and paths are both required", success=False)

        try:
            root = self._git_root(workspace, subpath)
            wroot = self._workspace_root(workspace)
            self._check_git_paths(wroot, root, paths)
            abs_paths = [os.path.normpath(os.path.join(root, p)) for p in paths]
        except Exception as e:
            return self.result(str(e), success=False)

        ok, out = await self._run_git(root, "restore", "--staged", "--", *abs_paths)
        if not ok:
            return self.result(out, success=False)

        return self.result("unstaged")

    @webui.route("git_commit", method="POST")
    async def _route_git_commit(self, body=None, query=None):
        if self.config.get("read-only"):
            return self.result("git module is in read-only mode", success=False)

        workspace = (body or {}).get("workspace", "")
        subpath = (body or {}).get("subpath", "")
        message = (body or {}).get("message", "").strip()

        if not workspace or not message:
            return self.result("workspace and message are both required", success=False)

        try:
            root = self._git_root(workspace, subpath)
        except Exception as e:
            return self.result(str(e), success=False)

        ok, out = await self._run_git(root, "commit", "-m", message)
        if not ok:
            return self.result(out, success=False)

        return self.result(out)

    @webui.route("git_branch", method="POST")
    async def _route_git_branch(self, body=None, query=None):
        if self.config.get("read-only"):
            return self.result("git module is in read-only mode", success=False)

        workspace = (body or {}).get("workspace", "")
        subpath = (body or {}).get("subpath", "")
        name = (body or {}).get("name", "").strip()

        if not workspace or not name:
            return self.result("workspace and name are both required", success=False)

        try:
            root = self._git_root(workspace, subpath)
        except Exception as e:
            return self.result(str(e), success=False)

        ok, out = await self._run_git(root, "switch", name)
        if not ok:
            return self.result(out, success=False)

        return self.result(out or f"switched to {name}")

    @webui.route("git_ahead")
    async def _route_git_ahead(self, body=None, query=None):
        """what a push would send: the unpushed commits, the target ref and
        the warnings (behind/diverged, dirty tree). the push confirmation
        modal is built entirely from this payload."""
        workspace = (query or {}).get("workspace", "")
        subpath = (query or {}).get("subpath", "")

        if not workspace:
            return self.result("no workspace given", success=False)

        try:
            root = self._git_root(workspace, subpath)
        except Exception as e:
            return self.result(str(e), success=False)

        ok, branch = await self._run_git(root, "rev-parse", "--abbrev-ref", "HEAD")
        if not ok:
            return self.result(branch, success=False)
        if branch == "HEAD":
            return self.result("detached HEAD: switch to a branch before pushing", success=False)

        ok, out = await self._run_git(root, "remote")
        remotes = out.split() if ok else []
        if not remotes:
            return self.result("this repo has no remotes, nowhere to push to", success=False)
        remote = "origin" if "origin" in remotes else remotes[0]

        ok, upstream = await self._run_git(root, "rev-parse", "--abbrev-ref", "@{u}")
        upstream = upstream.strip() if ok else ""
        has_upstream = "/" in upstream

        if has_upstream:
            target = upstream
            # NOTE: _run_git_bytes returns (returncode, raw) NOT (ok, out) -
            # check rc != 0, never `if not ok` (0 means success!).
            rc, raw = await self._run_git_bytes(
                root, "log", "--pretty=%h%x09%s", f"{upstream}..HEAD", "-n100"
            )
        else:
            # brand new branch: commits that exist on no remote at all
            target = f"{remote}/{branch}"
            rc, raw = await self._run_git_bytes(
                root, "log", "--pretty=%h%x09%s", "HEAD", "--not", "--remotes", "-n100"
            )

        if rc != 0:
            return self.result(raw.decode("utf-8", "replace").strip() or "git log failed", success=False)

        ahead = []
        for line in raw.decode("utf-8", "replace").splitlines():
            if not line.strip():
                continue
            if "\t" in line:
                h, msg = line.split("\t", 1)
            else:
                h, msg = line, ""
            ahead.append({"hash": h.strip(), "msg": msg})

        behind = 0
        if has_upstream:
            ok, out = await self._run_git(root, "rev-list", "--count", f"HEAD..{upstream}")
            if ok and out.strip().isdigit():
                behind = int(out.strip())

        _, raw = await self._run_git_bytes(root, "status", "--porcelain=v1")
        dirty = bool(raw.strip())

        return {
            "branch": branch,
            "remote": remote,
            "upstream": upstream,
            "has_upstream": has_upstream,
            "target": target,
            "ahead": ahead,
            "behind": behind,
            "dirty": dirty,
        }

    @webui.route("git_push", method="POST")
    async def _route_git_push(self, body=None, query=None):
        if self.config.get("read-only"):
            return self.result("git module is in read-only mode", success=False)

        workspace = (body or {}).get("workspace", "")
        subpath = (body or {}).get("subpath", "")

        if not workspace:
            return self.result("no workspace given", success=False)

        try:
            root = self._git_root(workspace, subpath)
        except Exception as e:
            return self.result(str(e), success=False)

        # everything is re-checked server side: the modal's confirmation
        # button is human intent, not a security boundary.
        ok, branch = await self._run_git(root, "rev-parse", "--abbrev-ref", "HEAD")
        if not ok or branch == "HEAD":
            return self.result("detached HEAD: switch to a branch before pushing", success=False)

        ok, out = await self._run_git(root, "remote")
        remotes = out.split() if ok else []
        if not remotes:
            return self.result("this repo has no remotes, nowhere to push to", success=False)
        remote = "origin" if "origin" in remotes else remotes[0]

        ok, upstream = await self._run_git(root, "rev-parse", "--abbrev-ref", "@{u}")
        upstream = upstream.strip() if ok else ""

        # explicit refspec so a weird push.default config can never widen
        # the push to other branches. never --force: without it git itself
        # rejects anything that isn't a fast-forward.
        if "/" in upstream:
            up_remote, _, up_branch = upstream.partition("/")
            args = ["push", up_remote, f"{branch}:{up_branch}"]
        else:
            args = ["push", "-u", remote, branch]

        # pushes can be slow on big repos or bad connections
        rc, raw = await self._run_git_bytes(root, *args, timeout=180)
        out = raw.decode("utf-8", "replace").strip()
        if rc != 0:
            return self.result(out or "git push failed", success=False)

        return self.result(out or "pushed")

    @webui.route("git_log")
    async def _route_git_log(self, body=None, query=None):
        workspace = (query or {}).get("workspace", "")
        subpath = (query or {}).get("subpath", "")
        try:
            limit = max(1, min(int((query or {}).get("n", 20)), 100))
        except (TypeError, ValueError):
            limit = 20
        try:
            skip = max(0, int((query or {}).get("skip", 0)))
        except (TypeError, ValueError):
            skip = 0

        if not workspace:
            return self.result("no workspace given", success=False)

        try:
            root = self._git_root(workspace, subpath)
        except Exception as e:
            return self.result(str(e), success=False)

        # %x09 = literal tab: fields are split on the one character a short
        # hash, an author name and a date can never contain. parsed from bytes.
        rc, raw = await self._run_git_bytes(
            root, "log", "--pretty=%h%x09%an%x09%ad%x09%s", "--date=short",
            f"-n{limit + 1}", f"--skip={skip}"
        )
        if rc != 0:
            return self.result(raw.decode("utf-8", "replace").strip() or "git log failed", success=False)

        entries = []
        for line in raw.decode("utf-8", "replace").splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 4:
                h, author, date, msg = parts[0], parts[1], parts[2], "\t".join(parts[3:])
            else:
                h, author, date, msg = (parts[0] if parts else ""), "", "", ""
            entries.append({
                "hash": h.strip(),
                "author": author.strip(),
                "date": date.strip(),
                "msg": msg.strip(),
            })

        # one extra line was requested: has_more is a fact, not a guess
        return {"log": entries[:limit], "has_more": len(entries) > limit, "skip": skip}

    @webui.route("git_commit_diff")
    async def _route_git_commit_diff(self, body=None, query=None):
        """one commit for the log modal: metadata, per-file numstat, the patch
        itself and the same side-by-side rows git_split_diff returns. read-only,
        so the log can be browsed without leaving the panel."""
        workspace = (query or {}).get("workspace", "")
        subpath = (query or {}).get("subpath", "")
        rev = (query or {}).get("rev", "").strip()

        if not workspace or not rev:
            return self.result("workspace and rev are both required", success=False)
        if rev.startswith("-"):
            return self.result("rev must be a commit, branch, tag or HEAD", success=False)

        try:
            root = self._git_root(workspace, subpath)
        except Exception as e:
            return self.result(str(e), success=False)

        # unit separator (0x1f): never appears in a subject or an author name
        rc, raw = await self._run_git_bytes(
            root, "log", "-n1", "--date=iso-local", "--pretty=%H%x1f%h%x1f%an%x1f%ae%x1f%ad%x1f%ar%x1f%P%x1f%s%x1f%b", rev)
        if rc != 0:
            return self.result(raw.decode("utf-8", "replace").strip() or "git log failed", success=False)

        fields = raw.decode("utf-8", "replace").split("\x1f")
        fields = (fields + [""] * 9)[:9]
        parents = fields[6].split() if fields[6].strip() else []
        commit = {
            "hash": fields[0].strip(),
            "short": fields[1].strip(),
            "author": fields[2].strip(),
            "email": fields[3].strip(),
            "date": fields[4].strip(),
            "relative": fields[5].strip(),
            "parents": parents,
            "is_merge": len(parents) > 1,
            "subject": fields[7].strip(),
            "body": fields[8].strip(),
        }

        # per-file change counts
        rc, raw = await self._run_git_bytes(root, "show", "--format=", "--numstat", rev)
        files = []
        if rc == 0:
            for line in raw.decode("utf-8", "replace").splitlines():
                parts = line.split("\t")
                if len(parts) < 3:
                    continue
                added, removed, path = parts[0], parts[1], "\t".join(parts[2:])
                files.append({
                    "path": path,
                    "added": None if added == "-" else int(added) if added.isdigit() else 0,
                    "removed": None if removed == "-" else int(removed) if removed.isdigit() else 0,
                    "binary": added == "-",
                })

        # the patch; merges show nothing unless compared to a parent
        patch_args = ["show", "--format=", "--no-color", "--patch"]
        if commit["is_merge"]:
            patch_args.append("--diff-merges=first-parent")
        patch_args.append(rev)
        rc, raw = await self._run_git_bytes(root, *patch_args)
        patch = raw.decode("utf-8", "replace") if rc == 0 else ""

        cap = self._num("max_diff_chars", 20000)
        truncated = bool(cap and len(patch) > cap)
        text = patch[:cap] if truncated else patch

        return {
            "commit": commit,
            "files": files,
            "diff": text if text.strip() else "(no changes)",
            "rows": self._parse_split_diff(text),
            "truncated": truncated,
        }

    @webui.route("git_blame")
    async def _route_git_blame(self, body=None, query=None):
        """which commit last touched each line of one file, grouped into runs.
        read-only, so it never needs the read-only gate."""
        workspace = (query or {}).get("workspace", "")
        subpath = (query or {}).get("subpath", "")
        rel_path = (query or {}).get("path", "")

        if not workspace or not rel_path:
            return self.result("workspace and path are both required", success=False)

        try:
            root = self._git_root(workspace, subpath)
            self._check_git_paths(self._workspace_root(workspace), root, [rel_path])
        except Exception as e:
            return self.result(str(e), success=False)

        ok, out = await self._run_git(
            root, "blame", "--line-porcelain", "--",
            os.path.normpath(os.path.join(root, rel_path)))
        if not ok:
            return self.result(out, success=False)

        groups, truncated = self._parse_blame_porcelain(out)
        return {"path": rel_path, "groups": groups, "truncated": truncated}

    @webui.route("git_diff")
    async def _route_git_diff(self, body=None, query=None):
        workspace = (query or {}).get("workspace", "")
        subpath = (query or {}).get("subpath", "")
        rel_path = (query or {}).get("path", "")
        staged = (query or {}).get("staged", "") in ("1", "true")
        untracked = (query or {}).get("untracked", "") in ("1", "true")

        if not workspace or not rel_path:
            return self.result("workspace and path are both required", success=False)

        try:
            root = self._git_root(workspace, subpath)
            wroot = self._workspace_root(workspace)
            self._check_git_paths(wroot, root, [rel_path])
        except Exception as e:
            return self.result(str(e), success=False)

        # absolute pathspec: git resolves relative ones against the cwd,
        # which only matches the repo-root-relative porcelain paths when
        # the repo happens to BE the workspace root. absolute works from any
        # cwd, so nested repos stage/unstage/diff the right file.
        abs_path = os.path.normpath(os.path.join(root, rel_path))

        if untracked:
            # no-index exits 1 when there IS a diff: accept both codes
            rc, raw = await self._run_git_bytes(
                root, "diff", "--no-index", "--", os.devnull, abs_path
            )
            ok = rc in (0, 1)
        elif staged:
            rc, raw = await self._run_git_bytes(root, "diff", "--staged", "--", abs_path)
            ok = rc == 0
        else:
            rc, raw = await self._run_git_bytes(root, "diff", "--", abs_path)
            ok = rc == 0

        if not ok:
            return self.result(raw.decode("utf-8", "replace").strip() or f"git diff failed (code {rc})", success=False)

        text = raw.decode("utf-8", "replace")
        return {"diff": text if text.strip() else "(no changes)"}

    @webui.route("git_fetch", method="POST")
    async def _route_git_fetch(self, body=None, query=None):
        """fetches from remote without merging. returns incoming commits."""
        if self.config.get("read-only"):
            return self.result("git module is in read-only mode", success=False)

        workspace = (body or {}).get("workspace", "")
        subpath = (body or {}).get("subpath", "")

        if not workspace:
            return self.result("no workspace given", success=False)

        try:
            root = self._git_root(workspace, subpath)
        except Exception as e:
            return self.result(str(e), success=False)

        ok, branch = await self._run_git(root, "rev-parse", "--abbrev-ref", "HEAD")
        if not ok:
            return self.result(branch, success=False)

        ok, out = await self._run_git(root, "remote")
        remotes = out.split() if ok else []
        if not remotes:
            return self.result("this repo has no remotes", success=False)
        remote = "origin" if "origin" in remotes else remotes[0]

        rc, raw = await self._run_git_bytes(root, "fetch", remote, "--prune", timeout=120)
        out = raw.decode("utf-8", "replace").strip()
        if rc != 0:
            return self.result(out or "git fetch failed", success=False)

        # what's incoming after fetch
        ok, upstream = await self._run_git(root, "rev-parse", "--abbrev-ref", "@{u}")
        incoming = []
        if ok and "/" in upstream.strip():
            rc2, raw2 = await self._run_git_bytes(
                root, "log", "--pretty=%h%x09%s", f"HEAD..{upstream.strip()}", "-n100"
            )
            if rc2 == 0:
                for line in raw2.decode("utf-8", "replace").splitlines():
                    if not line.strip():
                        continue
                    if "\t" in line:
                        h, msg = line.split("\t", 1)
                    else:
                        h, msg = line, ""
                    incoming.append({"hash": h.strip(), "msg": msg})

        return {"output": out, "incoming": incoming, "branch": branch}

    @webui.route("git_pull", method="POST")
    async def _route_git_pull(self, body=None, query=None):
        """pulls from remote. returns output or conflict info."""
        if self.config.get("read-only"):
            return self.result("git module is in read-only mode", success=False)

        workspace = (body or {}).get("workspace", "")
        subpath = (body or {}).get("subpath", "")

        if not workspace:
            return self.result("no workspace given", success=False)

        try:
            root = self._git_root(workspace, subpath)
        except Exception as e:
            return self.result(str(e), success=False)

        ok, branch = await self._run_git(root, "rev-parse", "--abbrev-ref", "HEAD")
        if not ok or branch == "HEAD":
            return self.result("detached HEAD: switch to a branch before pulling", success=False)

        rc, raw = await self._run_git_bytes(root, "pull", timeout=120)
        out = raw.decode("utf-8", "replace").strip()

        if rc != 0:
            # check if there are conflicts
            _, conflict_raw = await self._run_git_bytes(root, "diff", "--name-only", "--diff-filter=U")
            conflicts = [l for l in conflict_raw.decode("utf-8", "replace").splitlines() if l.strip()]
            if conflicts:
                return {"conflicts": conflicts, "output": out, "pull_failed": True}
            return self.result(out or "git pull failed", success=False)

        return self.result(out or "pulled")

    @webui.route("git_branch_create", method="POST")
    async def _route_git_branch_create(self, body=None, query=None):
        if self.config.get("read-only"):
            return self.result("git module is in read-only mode", success=False)

        workspace = (body or {}).get("workspace", "")
        subpath = (body or {}).get("subpath", "")
        name = (body or {}).get("name", "").strip()
        checkout = bool((body or {}).get("checkout", True))

        if not workspace or not name:
            return self.result("workspace and name are both required", success=False)

        # basic branch name validation
        if not regex.match(r"^[a-zA-Z0-9._/\-]+$", name):
            return self.result("invalid branch name: use letters, digits, dots, slashes, hyphens, underscores", success=False)

        try:
            root = self._git_root(workspace, subpath)
        except Exception as e:
            return self.result(str(e), success=False)

        if checkout:
            ok, out = await self._run_git(root, "switch", "-c", name)
        else:
            ok, out = await self._run_git(root, "branch", name)
        if not ok:
            return self.result(out, success=False)

        return self.result(out or f"created branch {name}")

    @webui.route("git_branch_delete", method="POST")
    async def _route_git_branch_delete(self, body=None, query=None):
        if self.config.get("read-only"):
            return self.result("git module is in read-only mode", success=False)

        workspace = (body or {}).get("workspace", "")
        subpath = (body or {}).get("subpath", "")
        name = (body or {}).get("name", "").strip()
        force = bool((body or {}).get("force", False))

        if not workspace or not name:
            return self.result("workspace and name are both required", success=False)

        try:
            root = self._git_root(workspace, subpath)
        except Exception as e:
            return self.result(str(e), success=False)

        # refuse to delete the current branch
        _, current = await self._run_git(root, "rev-parse", "--abbrev-ref", "HEAD")
        if current.strip() == name:
            return self.result("cannot delete the branch you are currently on", success=False)

        args = ["branch", "-D" if force else "-d", name]
        ok, out = await self._run_git(root, *args)
        if not ok:
            return self.result(out, success=False)

        return self.result(out or f"deleted branch {name}")

    @webui.route("git_conflicts")
    async def _route_git_conflicts(self, body=None, query=None):
        """lists files with merge conflicts."""
        workspace = (query or {}).get("workspace", "")
        subpath = (query or {}).get("subpath", "")

        if not workspace:
            return self.result("no workspace given", success=False)

        try:
            root = self._git_root(workspace, subpath)
        except Exception as e:
            return self.result(str(e), success=False)

        rc, raw = await self._run_git_bytes(root, "diff", "--name-only", "--diff-filter=U")
        if rc != 0:
            return {"conflicts": []}

        conflicts = [l.strip() for l in raw.decode("utf-8", "replace").splitlines() if l.strip()]
        return {"conflicts": conflicts}

    @webui.route("git_resolve", method="POST")
    async def _route_git_resolve(self, body=None, query=None):
        """resolves a conflicted file: strategy is 'ours', 'theirs', or 'both'."""
        if self.config.get("read-only"):
            return self.result("git module is in read-only mode", success=False)

        workspace = (body or {}).get("workspace", "")
        subpath = (body or {}).get("subpath", "")
        rel_path = (body or {}).get("path", "")
        strategy = (body or {}).get("strategy", "ours")

        if not workspace or not rel_path:
            return self.result("workspace and path are both required", success=False)
        if strategy not in ("ours", "theirs", "both"):
            return self.result("strategy must be 'ours', 'theirs', or 'both'", success=False)

        try:
            root = self._git_root(workspace, subpath)
            wroot = self._workspace_root(workspace)
            self._check_git_paths(wroot, root, [rel_path])
        except Exception as e:
            return self.result(str(e), success=False)

        if strategy == "ours":
            ok, out = await self._run_git(root, "checkout", "--ours", "--", rel_path)
        elif strategy == "theirs":
            ok, out = await self._run_git(root, "checkout", "--theirs", "--", rel_path)
        else:
            # "both": concatenate both sides without conflict markers
            abs_path = os.path.normpath(os.path.join(root, rel_path))
            try:
                with open(abs_path, "r", encoding="utf-8") as f:
                    content = f.read()
                # strip conflict markers, keep both sides
                resolved = regex.sub(
                    r"<<<<<<< [^\n]*\n(.*?)=======\n(.*?)>>>>>>> [^\n]*\n",
                    lambda m: m.group(1) + m.group(2),
                    content,
                    flags=regex.DOTALL,
                )
                with open(abs_path, "w", encoding="utf-8") as f:
                    f.write(resolved)
                ok = True
                out = "resolved (both)"
            except Exception as e:
                ok = False
                out = str(e)

        if not ok:
            return self.result(out, success=False)

        # stage the resolved file
        abs_path = os.path.normpath(os.path.join(root, rel_path))
        await self._run_git(root, "add", "--", abs_path)

        return self.result(f"resolved {rel_path} ({strategy})")

    @webui.route("git_revert_file", method="POST")
    async def _route_git_revert_file(self, body=None, query=None):
        """restores a file to its HEAD version, discarding all local
        modifications (staged and unstaged). the webui confirms before
        calling this."""
        if self.config.get("read-only"):
            return self.result("git module is in read-only mode", success=False)

        workspace = (body or {}).get("workspace", "")
        subpath = (body or {}).get("subpath", "")
        rel_path = (body or {}).get("path", "")

        if not workspace or not rel_path:
            return self.result("workspace and path are both required", success=False)

        try:
            root = self._git_root(workspace, subpath)
            wroot = self._workspace_root(workspace)
            self._check_git_paths(wroot, root, [rel_path])
        except Exception as e:
            return self.result(str(e), success=False)

        abs_path = os.path.normpath(os.path.join(root, rel_path))

        # restore from HEAD (works for both tracked-modified and staged-modified)
        ok, out = await self._run_git(root, "restore", "--source=HEAD", "--staged", "--worktree", "--", abs_path)
        if not ok:
            return self.result(out, success=False)

        return self.result(f"reverted {rel_path} to HEAD")

    @webui.route("git_split_diff")
    async def _route_git_split_diff(self, body=None, query=None):
        """returns structured side-by-side diff data for the split view.
        each row: { old_num, old_text, new_num, new_text, type }
        type: 'equal', 'add', 'del', 'mod'"""
        workspace = (query or {}).get("workspace", "")
        subpath = (query or {}).get("subpath", "")
        rel_path = (query or {}).get("path", "")
        staged = (query or {}).get("staged", "") in ("1", "true")
        untracked = (query or {}).get("untracked", "") in ("1", "true")

        if not workspace or not rel_path:
            return self.result("workspace and path are both required", success=False)

        try:
            root = self._git_root(workspace, subpath)
            wroot = self._workspace_root(workspace)
            self._check_git_paths(wroot, root, [rel_path])
        except Exception as e:
            return self.result(str(e), success=False)

        abs_path = os.path.normpath(os.path.join(root, rel_path))

        # get unified diff, then parse into side-by-side rows
        if untracked:
            rc, raw = await self._run_git_bytes(
                root, "diff", "--no-index", "--", os.devnull, abs_path
            )
        elif staged:
            rc, raw = await self._run_git_bytes(root, "diff", "--staged", "--", abs_path)
        else:
            rc, raw = await self._run_git_bytes(root, "diff", "--", abs_path)

        if rc not in (0, 1):
            return self.result(raw.decode("utf-8", "replace").strip() or "git diff failed", success=False)

        text = raw.decode("utf-8", "replace")
        # single file by construction: no boundary rows, the modal header
        # already shows the path
        rows = self._parse_split_diff(text, file_rows=False)
        return {"rows": rows}

    @webui.route("git_stage_hunk", method="POST")
    async def _route_git_stage_hunk(self, body=None, query=None):
        """stages (or unstages) ONE hunk of one file, leaving the worktree and
        the other hunks alone - the panel's stand-in for `git add -p`.

        the client sends a hunk INDEX, never patch text: the patch is rebuilt
        server-side from the same diff the panel was shown, then piped into
        `git apply --cached`. `--recount` keeps the counts honest when the
        other hunks are already staged or not yet staged."""
        body = body or {}
        workspace = body.get("workspace", "")
        subpath = body.get("subpath", "")
        rel_path = body.get("path", "")
        direction = str(body.get("direction") or "stage").strip()
        try:
            hunk_index = int(body.get("hunk", -1))
        except (TypeError, ValueError):
            hunk_index = -1

        if self.config.get("read-only"):
            return self.result("git module is in read-only mode", success=False)
        if not workspace or not rel_path:
            return self.result("workspace and path are both required", success=False)
        if direction not in ("stage", "unstage"):
            return self.result("direction must be 'stage' or 'unstage'", success=False)

        try:
            root = self._git_root(workspace, subpath)
            self._check_git_paths(self._workspace_root(workspace), root, [rel_path])
        except Exception as e:
            return self.result(str(e), success=False)

        abs_path = os.path.normpath(os.path.join(root, rel_path))
        diff_args = ["diff", "--no-color"] + (["--staged"] if direction == "unstage" else [])
        rc, raw = await self._run_git_bytes(root, *diff_args, "--", abs_path)
        if rc not in (0, 1):
            return self.result(raw.decode("utf-8", "replace").strip() or "git diff failed",
                               success=False)
        text = raw.decode("utf-8", "replace")
        if not text.strip():
            return self.result(
                "no diff for this file (an untracked file has to be staged whole)",
                success=False)

        header, hunks = self._split_hunks(text)
        if hunk_index < 0 or hunk_index >= len(hunks):
            return self.result(
                f"hunk {hunk_index} out of range (0..{len(hunks) - 1})", success=False)

        # exactly ONE newline between the file header and the hunk: a blank
        # line there is not a separator, it is garbage inside the patch and
        # git apply rejects the whole thing ("patch with only garbage").
        head = header.rstrip("\n")
        patch = (head + "\n" if head else "") + hunks[hunk_index].rstrip("\n") + "\n"
        apply_args = ["apply", "--cached", "--recount", "--whitespace=nowarn"]
        if direction == "unstage":
            apply_args.append("--reverse")
        apply_args.append("-")
        ok, out = await self._run_git_stdin(root, patch, *apply_args)
        if not ok:
            return self.result(out, success=False)
        return {"applied": hunk_index, "hunks": len(hunks), "direction": direction}

    def _split_hunks(self, diff_text):
        """splits one file's unified diff into (header, [hunk, ...]).

        header = everything before the first '@@' (diff --git, index, ---, +++,
        mode lines); each hunk keeps its '@@' line and body. the order is the
        same order _parse_split_diff numbers, so a hunk index from the panel
        selects the same hunk server-side."""
        header = []
        hunks = []
        cur = None
        for line in diff_text.split("\n"):
            if line.startswith("@@"):
                if cur is not None:
                    hunks.append(cur)
                cur = [line]
            elif cur is None:
                header.append(line)
            else:
                cur.append(line)
        if cur is not None:
            hunks.append(cur)
        return "\n".join(header), ["\n".join(h) for h in hunks]

    def _parse_split_diff(self, unified_text, file_rows=True):
        """parses unified diff into side-by-side rows.

        file_rows=True emits a {'type': 'file', 'path': ...} row at every
        'diff --git' boundary (multi-file diffs, e.g. a whole commit). the
        single-file routes pass False: there the modal header already names
        the file, so a boundary row would just repeat it."""
        rows = []
        old_line = 0
        new_line = 0
        del_buffer = []
        add_buffer = []
        hunk_no = -1
        first_pending = False

        def emit(row):
            """append a row, stamped with its hunk index. the first data row of
            a hunk is flagged so the panel knows where to offer 'stage hunk'."""
            nonlocal first_pending
            if row["type"] == "file":
                row["hunk"] = -1
                rows.append(row)
                return
            row["hunk"] = hunk_no
            if first_pending:
                row["hunk_first"] = True
                first_pending = False
            rows.append(row)

        def flush_buffers():
            nonlocal del_buffer, add_buffer
            # pair up dels and adds as 'mod' rows
            while del_buffer and add_buffer:
                d = del_buffer.pop(0)
                a = add_buffer.pop(0)
                emit({
                    "old_num": d[0], "old_text": d[1],
                    "new_num": a[0], "new_text": a[1],
                    "type": "mod",
                })
            # leftovers are pure del or add
            for d in del_buffer:
                emit({"old_num": d[0], "old_text": d[1], "new_num": None, "new_text": None, "type": "del"})
            for a in add_buffer:
                emit({"old_num": None, "old_text": None, "new_num": a[0], "new_text": a[1], "type": "add"})
            del_buffer = []
            add_buffer = []

        for line in unified_text.split("\n"):
            # a multi-file diff (a commit, not a single file) gets a header row
            # per file, so the split view shows where one file ends and the
            # next begins - and line numbers restart from that file's hunks
            if file_rows and line.startswith("diff --git "):
                flush_buffers()
                m = regex.match(r"^diff --git a/(.+?) b/(.+)$", line)
                emit({
                    "old_num": None, "old_text": None, "new_num": None, "new_text": None,
                    "type": "file", "path": (m.group(2) if m else line[len("diff --git "):]),
                })
                continue

            hunk = regex.match(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            if hunk:
                flush_buffers()
                hunk_no += 1
                first_pending = True
                old_line = int(hunk.group(1))
                new_line = int(hunk.group(2))
                continue

            if line.startswith("---") or line.startswith("+++") or line.startswith("\\"):
                continue

            if line.startswith("-"):
                flush_buffers() if add_buffer else None
                del_buffer.append((old_line, line[1:]))
                old_line += 1
            elif line.startswith("+"):
                add_buffer.append((new_line, line[1:]))
                new_line += 1
            elif line.startswith(" "):
                flush_buffers()
                emit({
                    "old_num": old_line, "old_text": line[1:],
                    "new_num": new_line, "new_text": line[1:],
                    "type": "equal",
                })
                old_line += 1
                new_line += 1

        flush_buffers()
        return rows

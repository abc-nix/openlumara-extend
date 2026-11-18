# editor: webui extension routes (mixin for the Editor class).
#
# -- AI GENERATED CODE (deepseek-v4-flash) :: 2026-10-16
# The 13 routes under /api/ext/editor/<path>. Thin wrappers: pick
# workspace root -> resolve path -> act -> self.result(). All path logic
# lives in paths.py; settings defaults live on the Editor class (module.py).
# Routes are discovered via MRO (channels/webui/channel.py walks
# type(module).__mro__), so a mixin is safe. Handlers stay private:
# the tool loader only registers public methods.

import os
import json
import asyncio
import base64
import mimetypes
import shlex
import shutil
import subprocess
import filetype
import regex
from pathlib import Path

import core
import channels.webui.api as webui


class EditorRoutes:
    # --------------------
    # info
    # --------------------
    @webui.route("workspace")
    async def _route_workspace(self, body=None, query=None):
        """workspace info for the panel header: resolved paths + validity."""
        roots = self._workspace_roots()
        return {
            "path": str(roots[0]),
            "workspaces": [
                {"path": str(r), "name": r.name or str(r), "exists": r.is_dir()}
                for r in roots
            ],
            "inherited": self._inherit_active(),
            "combine": bool(self.config.get("combine_trees", True)),
            "mode": "sandboxed" if self.config.get("sandboxed") else "absolute",
            "read_only": bool(self.config.get("read-only")),
        }

    # --------------------
    # browse
    # --------------------
    @webui.route("list")
    async def _route_list(self, body=None, query=None):
        """one directory level (the panel calls this lazily per folder expansion)"""
        root, err = self._pick_root(query)
        if err:
            return self.result(err, False)
        if not root.is_dir():
            return self.result(f"workspace folder '{root}' does not exist", False)

        target, err = self._target(query, root)
        if err:
            return self.result(err, False)

        if not target.exists():
            return self.result(f"'{target.name}' does not exist", False)
        if not target.is_dir():
            return self.result(f"'{target.name}' is not a folder", False)

        cap = max(1, self._num_setting("max_entries", self._DEFAULT_MAX_ENTRIES))
        skip = set(self._skip_dirs())
        entries = []
        truncated = False
        try:
            with os.scandir(target) as scan:
                for entry in scan:
                    try:
                        is_dir = entry.is_dir()
                        stat = entry.stat()
                    except OSError:
                        continue  # unreadable/broken entry: skip it silently
                    if is_dir and entry.name in skip:
                        continue  # skipped folder: hidden from the view as well
                    if len(entries) >= cap:
                        truncated = True
                        break
                    entries.append({
                        "name": entry.name,
                        "is_dir": is_dir,
                        "size": None if is_dir else stat.st_size,
                        "modified": stat.st_mtime,
                    })
        except (OSError, ValueError) as e:
            return self.result(f"could not read '{target.name}': {e}", False)

        # folders first, then files, both A-Z (case-insensitive)
        entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
        return {"path": str(target), "entries": entries, "truncated": truncated}

    @webui.route("preview")
    async def _route_preview(self, body=None, query=None):
        """text preview for a file (size-capped); binary/oversized -> metadata only"""
        root, err = self._pick_root(query)
        if err:
            return self.result(err, False)
        target, err = self._target(query, root)
        if err:
            return self.result(err, False)

        if not target.exists():
            return self.result(f"'{target.name}' does not exist", False)
        if not target.is_file():
            return self.result(f"'{target.name}' is not a file", False)

        stat = target.stat()
        info = {
            "name": target.name,
            "size": stat.st_size,
            "modified": stat.st_mtime,
            "kind": None,      # binary files: extension from magic bytes
            "text": None,      # set when a text preview was produced
            "truncated": False,
        }

        kb = self._num_setting("max_preview_kb", self._DEFAULT_MAX_PREVIEW_KB)

        try:
            with self._open_target(target, "rb", root) as f:
                # one open: magic-byte detection and the text read share the
                # same handle
                guess = filetype.guess(f)
                if guess:
                    info["kind"] = guess.extension
                    return info
                if kb <= 0:
                    return info  # previews disabled: metadata only
                f.seek(0)
                blob = f.read(kb * 1024 + 1)
        except (OSError, ValueError) as e:
            return self.result(f"could not read '{target.name}': {e}", False)

        # errors="replace": a char split at the limit must not turn a
        # text file into "binary"
        text = blob[: kb * 1024].decode("utf-8", errors="replace")
        info["text"] = text
        info["truncated"] = len(blob) > kb * 1024
        return info

    # --------------------
    # search (names + content)
    # --------------------
    @webui.route("search")
    async def _route_search(self, body=None, query=None):
        """recursive name search over the workspace.

        ?hidden=0/1 mirrors the panel toggle: hidden entries are neither
        matched nor descended into. symlinked dirs are never descended
        (os.walk default). runs in a worker thread so a big scan cannot
        block the webui event loop. the `capped` field ("scan"|"results"|
        "") reports which cap stopped the scan early.
        """
        root, err = self._pick_root(query)
        if err:
            return self.result(err, False)
        if not root.is_dir():
            return self.result(f"workspace folder '{root}' does not exist", False)

        q = str((query or {}).get("q", "")).strip().lower()
        if not q:
            return {"query": "", "matches": [], "truncated": False, "capped": "", "scanned": 0}

        show_hidden = str((query or {}).get("hidden", "")).lower() in ("1", "true", "yes")
        cap = max(1, self._num_setting("search_max_results", self._DEFAULT_SEARCH_MAX_RESULTS))
        scan_limit = max(1, self._num_setting("search_scan_limit", self._DEFAULT_SEARCH_SCAN_LIMIT))
        # exact-name match (case-sensitive), so no lowercasing here
        skip = set(self._skip_dirs())

        def scan():
            matches = []
            scanned = 0
            for dirpath, dirnames, filenames in os.walk(root, topdown=True):
                all_dirs = sorted(dirnames)  # deterministic walk order
                # skip_dirs: never descend into hidden folders, but keep the
                # folders themselves matchable by name
                dirnames[:] = [d for d in all_dirs if d not in skip]
                if not show_hidden:
                    dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for name in all_dirs + sorted(filenames):
                    scanned += 1
                    if scanned > scan_limit:
                        return matches, scanned, True, "scan"
                    # hidden files in visible folders must stay invisible to search too
                    if not show_hidden and name.startswith("."):
                        continue
                    if q in name.lower():
                        matches.append(os.path.join(dirpath, name))
                        if len(matches) >= cap:
                            return matches, scanned, True, "results"
            return matches, scanned, False, ""

        raw_matches, scanned, truncated, capped = await asyncio.to_thread(scan)

        out = []
        for full in raw_matches:
            p = Path(full)
            out.append({
                "name": p.name,
                "path": str(p),
                # as_posix(): the frontend splits rel on "/" to rebuild the tree
                "rel": p.relative_to(root).as_posix(),
                "is_dir": p.is_dir(),
            })
        return {"query": q, "matches": out, "truncated": truncated,
                "capped": capped, "scanned": scanned}

    @webui.route("content", method="POST")
    async def _route_content(self, body=None, query=None):
        """recursive literal (or regex) text search across the workspace.

        skips hidden/blacklisted folders and binary/huge files, like the
        coder's sidebar search, but returns flat results for the panel.
        """
        root, err = self._pick_root(body)
        if err:
            return self.result(err, False)
        if not root.is_dir():
            return self.result(f"workspace folder '{root}' does not exist", False)

        needle = str((body or {}).get("query", "")).strip()
        use_regex = bool((body or {}).get("regex"))
        max_matches = min(
            self._num_setting("content_max_matches", self._DEFAULT_CONTENT_MAX_MATCHES),
            500,
        )

        if not needle:
            return {"results": [], "truncated": False}

        import regex as _regex
        pattern = None
        if use_regex:
            try:
                pattern = _regex.compile(needle, _regex.IGNORECASE)
            except Exception as e:
                return self.result(f"invalid regex: {e}", False)

        skip = set(self._skip_dirs())

        def scan():
            results = []
            truncated = False
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in skip]
                for fname in filenames:
                    if fname.startswith("."):
                        continue
                    fpath = os.path.join(dirpath, fname)
                    try:
                        if os.path.getsize(fpath) > self.ui_max_read_bytes:
                            continue
                        with self._nofollow_open(fpath) as f:
                            for line_num, line in enumerate(f, 1):
                                if pattern:
                                    try:
                                        hit = pattern.search(line, timeout=3)
                                    except TimeoutError:
                                        continue
                                else:
                                    hit = needle.lower() in line.lower()
                                if hit:
                                    results.append({
                                        "file": os.path.relpath(fpath, root).replace(os.sep, "/"),
                                        "line": line_num,
                                        "text": line.rstrip()[:300],
                                    })
                                    if len(results) >= max_matches:
                                        truncated = True
                                        break
                    except (UnicodeDecodeError, IsADirectoryError, PermissionError,
                            OSError, ValueError):
                        # unreadable or symlinked file: skip it
                        continue
                    if truncated:
                        break
                if truncated:
                    break
            return results, truncated

        results, truncated = await asyncio.to_thread(scan)
        return {"results": results, "truncated": truncated}

    # --------------------
    # actions
    # --------------------
    @webui.route("file")
    async def _route_file(self, body=None, query=None):
        """raw file bytes (base64) for the panel's Attach action"""
        root, err = self._pick_root(query)
        if err:
            return self.result(err, False)
        target, err = self._target(query, root)
        if err:
            return self.result(err, False)

        if not target.exists():
            return self.result(f"'{target.name}' does not exist", False)
        if not target.is_file():
            return self.result(f"'{target.name}' is not a file", False)

        cap_mb = self._num_setting("max_attach_mb", 10)
        if cap_mb <= 0:
            return self.result("attach is disabled (max_attach_mb = 0)", False)
        size = target.stat().st_size
        if size > cap_mb * 1024 * 1024:
            return self.result(f"'{target.name}' is over max_attach_mb ({cap_mb} MB)", False)

        try:
            data = await asyncio.to_thread(self._read_bytes, target, root)
        except (OSError, ValueError) as e:
            return self.result(f"could not read '{target.name}': {e}", False)

        mime, _ = mimetypes.guess_type(target.name)
        return {
            "name": target.name,
            "mime": mime or "application/octet-stream",
            "size": size,
            "b64": base64.b64encode(data).decode("ascii"),
        }

    @webui.route("media")
    async def _route_media(self, body=None, query=None):
        """image bytes (base64 JSON) for the read_media tool card.

        the tool result only carries metadata - the picture is delivered to
        the model as its own internal user message - so the card re-fetches
        it from here. addressed like the AI tool (workspace selector + path),
        resolved with the same rules (read_only=True) and capped by
        max_image_mb.
        """
        q = query or {}
        try:
            target, base = self._resolve_with_base(
                str(q.get("workspace", "")), str(q.get("path", "")), read_only=True)
        except ValueError as e:
            return self.result(str(e), False)

        if not target.exists():
            return self.result(f"'{target.name}' does not exist", False)
        if not target.is_file():
            return self.result(f"'{target.name}' is not a file", False)

        cap_mb = self._num_setting("max_image_mb", 4)
        if cap_mb <= 0:
            return self.result("image attachment is disabled (max_image_mb = 0)", False)
        size = target.stat().st_size
        if size > cap_mb * 1024 * 1024:
            return self.result(f"'{target.name}' is over max_image_mb ({cap_mb} MB)", False)

        try:
            data = await asyncio.to_thread(self._read_bytes, target, base)
        except (OSError, ValueError) as e:
            return self.result(f"could not read '{target.name}': {e}", False)

        kind = filetype.guess(data[:8192])
        mime = (kind.mime if kind
                else mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        return {
            "name": target.name,
            "mime": mime,
            "size": size,
            "b64": base64.b64encode(data).decode("ascii"),
        }

    @webui.route("block")
    async def _route_block(self, body=None, query=None):
        """the before/after snapshot of ONE block edit, addressed by its token.

        display-only by construction: the tool writes the snapshot under the
        data folder and puts only a short token in its result, so the replaced
        text cannot cost context. the token is validated as hex (no path, no
        user input reaching the filesystem) and the chat id is checked before
        it is used as a folder name - the route can only read what the tool
        itself wrote.
        """
        q = query or {}
        chat = str(q.get("chat", ""))
        token = str(q.get("token", ""))
        if not regex.fullmatch(r"[A-Za-z0-9_-]{1,40}", chat):
            return self.result("bad chat id", False)
        if not regex.fullmatch(r"[0-9a-f]{8,32}", token):
            return self.result("bad token", False)

        stash = Path(core.get_data_path(os.path.join("editor", "blocks", chat, token + ".json")))
        if not stash.exists():
            return self.result("no snapshot for this call", False)
        try:
            entry = json.loads(stash.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self.result("snapshot could not be read", False)
        if not isinstance(entry, dict):
            return self.result("snapshot could not be read", False)
        return {"old": entry.get("old", ""), "new": entry.get("new", "")}

    @webui.route("open", method="POST")
    async def _route_open(self, body=None, query=None):
        """launch a file/folder with the system default app (open_command).

        refuses files whose extension is in executable_extensions: some
        desktop handlers would run rather than display such files.
        """
        root, err = self._pick_root(body)
        if err:
            return self.result(err, False)
        target, err = self._target(body, root)
        if err:
            return self.result(err, False)

        if not (target.is_file() or target.is_dir()):
            return self.result("path does not exist", False)

        if target.is_file():
            ext = target.suffix.lstrip(".").lower()
            if ext in self._list_setting("executable_extensions", self._DEFAULT_EXEC_EXTS):
                return self.result(
                    f"refused: '.{ext}' is on the executable list (see executable_extensions setting)", False)

        cmd = str(self.config.get("open_command") or "xdg-open").strip()
        try:
            # shlex.split supports "gio open"-style commands; no shell, path
            # stays one argument
            subprocess.Popen(
                shlex.split(cmd) + [str(target)],
                stdin=None, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,  # survive server restarts
            )
        except FileNotFoundError:
            return self.result(f"open command not found: '{cmd}'", False)
        except OSError as e:
            return self.result(f"could not open: {e}", False)

        return {"opened": str(target), "command": cmd}

    def _read_only_guard(self):
        """(ok, message): modifying routes refuse in read-only mode"""
        if self.config.get("read-only"):
            return False, "editor is in read-only mode"
        return True, ""

    @webui.route("rename", method="POST")
    async def _route_rename(self, body=None, query=None):
        """rename an entry in place; the new name must be a plain basename"""
        ok, msg = self._read_only_guard()
        if not ok:
            return self.result(msg, False)

        root, err = self._pick_root(body)
        if err:
            return self.result(err, False)
        target, err = self._target(body, root)
        if err:
            return self.result(err, False)

        if target == root:
            return self.result("the workspace root itself cannot be renamed", False)
        if not (target.is_file() or target.is_dir()):
            return self.result("path does not exist", False)

        new_name = str((body or {}).get("name", "")).strip()
        if (not new_name or new_name in (".", "..")
                or "/" in new_name or "\\" in new_name or "\x00" in new_name):
            return self.result("invalid new name (basename only)", False)

        dest = target.with_name(new_name)
        if dest.exists():
            return self.result(f"'{new_name}' already exists in this folder", False)

        try:
            os.rename(target, dest)
        except (OSError, ValueError) as e:
            return self.result(f"could not read '{target.name}': {e}", False)
        except OSError as e:
            return self.result(f"rename failed: {e}", False)

        return {"path": str(dest), "name": dest.name, "is_dir": dest.is_dir()}

    @webui.route("create", method="POST")
    async def _route_create(self, body=None, query=None):
        """creates an empty file or a folder (context menu)."""
        ok, msg = self._read_only_guard()
        if not ok:
            return self.result(msg, False)

        root, err = self._pick_root(body)
        if err:
            return self.result(err, False)
        target, err = self._target(body, root)
        if err:
            return self.result(err, False)

        is_dir = bool((body or {}).get("is_dir"))
        try:
            if is_dir:
                target.mkdir(parents=True, exist_ok=False)
            else:
                # 'x' mode: fails if the file exists, no TOCTOU
                parent = target.parent
                if parent:
                    parent.mkdir(parents=True, exist_ok=True)
                with self._open_target(target, "x", root) as f:
                    f.write("")
        except FileExistsError:
            return self.result("already exists", False)
        except Exception as e:
            return self.result(str(e), False)

        return self.result(f"created {target.name}")

    @webui.route("move", method="POST")
    async def _route_move(self, body=None, query=None):
        """renames or moves an entry: src -> dst, both workspace-relative."""
        ok, msg = self._read_only_guard()
        if not ok:
            return self.result(msg, False)

        root, err = self._pick_root(body)
        if err:
            return self.result(err, False)

        try:
            src_path = self._resolve(str((body or {}).get("src", "")), root)
            dst_path = self._resolve(str((body or {}).get("dst", "")), root)
        except ValueError as e:
            return self.result(str(e), False)

        if not src_path.exists():
            return self.result("source does not exist", False)
        if dst_path.exists():
            return self.result(f"destination already exists: {dst_path.name}", False)

        try:
            parent = dst_path.parent
            if parent:
                parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src_path), str(dst_path))
        except Exception as e:
            return self.result(str(e), False)

        return self.result("moved")

    @webui.route("delete", method="POST")
    async def _route_delete(self, body=None, query=None):
        """deletes a file, or a folder recursively. the webui confirms
        before calling this; the AI-facing tools stay stricter on purpose."""
        ok, msg = self._read_only_guard()
        if not ok:
            return self.result(msg, False)

        root, err = self._pick_root(body)
        if err:
            return self.result(err, False)
        target, err = self._target(body, root)
        if err:
            return self.result(err, False)

        if not target.exists():
            return self.result("path does not exist", False)
        if target == root:
            return self.result("the workspace root itself cannot be deleted", False)

        try:
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        except Exception as e:
            return self.result(str(e), False)

        return self.result("deleted")

    @webui.route("duplicate", method="POST")
    async def _route_duplicate(self, body=None, query=None):
        """copies a file or folder next to the original as 'name copy.ext'
        (numbered if taken)."""
        ok, msg = self._read_only_guard()
        if not ok:
            return self.result(msg, False)

        root, err = self._pick_root(body)
        if err:
            return self.result(err, False)
        src, err = self._target(body, root)
        if err:
            return self.result(err, False)

        if not src.exists():
            return self.result("not found", False)

        base = src.name
        if src.is_dir():
            stem, ext = base, ""
        else:
            stem, ext = os.path.splitext(base)

        n = 1
        while True:
            suffix = " copy" if n == 1 else f" copy {n}"
            new_name = stem + suffix + ext
            candidate = src.parent / new_name
            if not candidate.exists():
                break
            n += 1

        try:
            if src.is_dir():
                shutil.copytree(src, candidate)
            else:
                shutil.copy2(src, candidate)
        except Exception as e:
            return self.result(str(e), False)

        return self.result(f"duplicated to {new_name}")

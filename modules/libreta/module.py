"""libreta — the user-facing text surface (P0a: browse + read).

No @core.module.tool methods, on purpose: file access for the AI stays in the
`editor` module, so notes and code keep exactly one tool vocabulary. libreta
only ships webui routes, a sidebar tab (Files / Recent / Pinned / Options) and
a full-window reading cover.

P0a is READ-ONLY for user files. The only disk writes are the module's own data
files under data/libreta/: state.json (recent / pinned) and autosave.json (the
files the user took out of autosave).

Routes are addressed as /api/ext/libreta/<path>. They are read-only today, so
none are marked admin_only; the write routes in P0b will follow the shipped
precedent in codeeditor (workspace containment + a read-only setting) rather
than the admin gate, which would make the editor unusable when
enable_admin_settings is off.
"""

import os
import time

import core
import channels.webui.api as webui

from . import files


class Libreta(core.module.Module):
    """Browse, read and (from P0b) edit the text files of your workspaces."""

    category = "life"

    settings = {
        "enable_webui_extension": {
            "default": True,
            "description": "Show the Libreta sidebar tab and reading view, and load its webui assets.",
        },
        "inherit_from_editor": {
            "default": True,
            "description": "Use the editor module's workspaces (and its folder blacklist) here, so the UI shows exactly the folders the AI can reach. Falls back to the manual settings below when the editor module is off.",
        },
        "workspaces": {
            "default": [],
            "description": "Folders Libreta may browse when inherit_from_editor is off. Paths are absolute.",
        },
        "skip_dirs": {
            "default": [],
            "description": "Folder names hidden from the tree when inherit_from_editor is off (e.g. node_modules). Matched by exact name at any depth.",
        },
        "show_hidden": {
            "default": False,
            "description": "Show dot-files and dot-folders in the tree.",
        },
        "use_trash": {
            "default": True,
            "description": "Deleting a file moves it into .libreta-trash inside the workspace and adds a Trash tab where it can be restored. Off: deleting removes the file permanently.",
        },
        "read-only": {
            "default": False,
            "description": "When on, Libreta refuses every write: Ctrl+S, autosave, new files, renames and deletes (moving to the trash included). Browsing and reading keep working.",
        },
        "max_read_bytes": {
            "default": 1_000_000,
            "description": "Largest file the reading view will open (bytes). Bigger files are refused with a message instead of freezing the UI.",
        },
        "code_autowrap": {
            "default": True,
            "description": "Wrap long lines in Code view instead of scrolling sideways. The document bar has a per-file override.",
        },
        "autosave_default": {
            "default": True,
            "description": "What a file with no per-file switch does: on = it is autosaved, off = autosave starts off for every file and you turn it on with the caret next to Save (or for good, here). The per-file choices live in data/libreta/autosave.json.",
        },
        "autosave_interval": {
            "default": 0,
            "description": "Auto-save this many seconds after you stop typing (0 = off). Every dirty document is saved on its own timer, through the same mtime guard as Ctrl+S: if the file changed on disk since it was opened, nothing is written and the conflict banner asks you what to do.",
        },
        "notes_block_edit": {
            "default": True,
            "description": "Notes view is one continuous live-preview editor (Obsidian style): the file stays markdown, formatting renders in place and syntax markers show only on the line being edited. Off: Notes view is a read-only page and editing happens in Code view. (The key keeps its old name so existing configs stay valid.)",
        },
        "preview_chars": {
            "default": 140,
            "description": "Length of the snippet shown on Recent and Pinned cards.",
        },
        "recent_max": {
            "default": 30,
            "description": "How many recently opened files the Recent tab keeps.",
        },
        "pinned_max": {
            "default": 50,
            "description": "How many pinned files the Pinned tab keeps.",
        },
        "default_side_tab": {
            "default": "files",
            "type": "select",
            "description": "Which Libreta tab opens first in a fresh browser.",
            "options": {
                "files": "Files (workspace tree)",
                "recent": "Recent (cards)",
                "pinned": "Pinned (cards)",
            },
        },
    }

    # ------------------------------------------------------------------
    # data files: data/libreta/<name>.json
    # ------------------------------------------------------------------
    def _data_dir(self):
        return os.path.join(core.get_data_path(), "libreta")

    def _migrate_state_file(self):
        """recent/pinned used to live at data/libreta_state.json; the module keeps
        its own folder now, so move the old file instead of orphaning it."""
        old = os.path.join(core.get_data_path(), "libreta_state.json")
        new = os.path.join(self._data_dir(), "state.json")
        try:
            if os.path.isfile(old) and not os.path.exists(new):
                os.makedirs(self._data_dir(), exist_ok=True)
                os.replace(old, new)
        except OSError as e:
            core.log("warning", f"libreta: could not move libreta_state.json ({e})")

    async def on_ready(self):
        self._migrate_state_file()
        self.state = core.storage.StorageDict("state", "json", path=self._data_dir())
        self.autosave = core.storage.StorageDict("autosave", "json", path=self._data_dir())

    def _state(self):
        """the state file, created on demand (routes may be hit before on_ready
        when a module is reloaded from the settings screen)."""
        if getattr(self, "state", None) is None:
            self._migrate_state_file()
            self.state = core.storage.StorageDict("state", "json", path=self._data_dir())
        return self.state

    # ------------------------------------------------------------------
    # per-file autosave switch (data/libreta/autosave.json)
    #
    # The config key stays the interval; this file only remembers the files the
    # user took out of autosave. An exception list, so the default (nothing
    # listed) needs no maintenance and the file stays small.
    # ------------------------------------------------------------------
    AUTOSAVE_MAX = 500

    def _autosave_store(self):
        store = getattr(self, "autosave", None)
        if store is None:
            self.autosave = core.storage.StorageDict("autosave", "json", path=self._data_dir())
            store = self.autosave
        return store

    @staticmethod
    def _autosave_key(workspace, path):
        return f"{workspace}|{path}"

    def _autosave_default(self):
        """what a file with no per-file entry does: True = autosaved."""
        return bool(self.config.get("autosave_default", True))

    def _autosave_lists(self):
        """(off, on) — the files that disagree with the default."""
        store = self._autosave_store()

        def clean(value):
            return [k for k in value if isinstance(k, str)] if isinstance(value, list) else []

        return clean(store.get("off")), clean(store.get("on"))

    def _autosave_state(self):
        off, on = self._autosave_lists()
        return {"off": off, "on": on, "default": self._autosave_default()}

    def _autosave_set(self, key, on):
        """record one file's switch. A file that agrees with the default needs no
        entry at all, so toggling back leaves the file clean."""
        store = self._autosave_store()
        off, on_list = self._autosave_lists()
        off = [k for k in off if k != key]
        on_list = [k for k in on_list if k != key]

        if bool(on) != self._autosave_default():
            if on:
                on_list = on_list[-(self.AUTOSAVE_MAX - 1):] + [key]
            else:
                off = off[-(self.AUTOSAVE_MAX - 1):] + [key]

        store["off"] = off
        store["on"] = on_list
        store.save()
        return self._autosave_state()

    @staticmethod
    def _clean(items):
        """only well-formed {workspace, path} entries survive a corrupt file."""
        if not isinstance(items, list):
            return []
        return [
            item for item in items
            if isinstance(item, dict) and isinstance(item.get("workspace", ""), str)
            and isinstance(item.get("path", ""), str)
        ]

    def _lists(self):
        """(recent, pinned) as lists of {workspace, path, at}."""
        state = self._state()
        return self._clean(state.get("recent")), self._clean(state.get("pinned"))

    def _save(self, recent=None, pinned=None):
        state = self._state()
        current_recent, current_pinned = self._lists()
        state["recent"] = list(recent if recent is not None else current_recent)
        state["pinned"] = list(pinned if pinned is not None else current_pinned)
        state.save()
        return {"recent": state["recent"], "pinned": state["pinned"]}

    @staticmethod
    def _entry(workspace, path):
        return {
            "workspace": str(workspace or ""),
            "path": files.safe_rel(path),
            "at": time.time(),
        }

    # ------------------------------------------------------------------
    # workspace resolution (same selectors as the editor module)
    # ------------------------------------------------------------------
    def _editor(self):
        manager = getattr(self, "manager", None)
        if manager is None:
            return None
        return (getattr(manager, "modules", None) or {}).get("editor")

    def _workspace_info(self):
        """(root paths, inherited_from_editor)."""
        editor = self._editor() if self.config.get("inherit_from_editor") else None
        if editor:
            roots = []
            try:
                roots = [str(r) for r in editor._workspace_roots()]
            except Exception:
                roots = []
            if roots:
                return roots, True

        raw_list = self.config.get("workspaces") or []
        roots = []
        for raw in raw_list:
            try:
                roots.append(os.path.realpath(os.path.expanduser(str(raw))).rstrip(os.sep))
            except Exception:
                continue
        return roots, False

    def _skip_dirs(self):
        if self.config.get("inherit_from_editor"):
            editor = self._editor()
            if editor:
                try:
                    return list(editor._skip_dirs())
                except Exception:
                    pass
        return list(self.config.get("skip_dirs") or [])

    def _root(self, selector):
        """workspace selector (basename / full path / 0-based index) -> root."""
        roots, _ = self._workspace_info()
        if not roots:
            raise files.FileError(
                "no workspaces configured - turn on the editor module or fill in Libreta's workspaces"
            )

        selector = str(selector or "").strip()
        if not selector:
            return roots[0]

        if selector.isdigit():
            index = int(selector)
            if 0 <= index < len(roots):
                return roots[index]
            raise files.FileError(f"workspace index {index} out of range (0-{len(roots) - 1})")

        for root in roots:
            if root == selector or os.path.basename(root) == selector:
                return root

        names = ", ".join(os.path.basename(r) or r for r in roots)
        raise files.FileError(f"workspace '{selector}' not found (available: {names})")

    def _workspace_list(self):
        roots, inherited = self._workspace_info()
        return {
            "workspaces": [
                {
                    "name": os.path.basename(root) or root,
                    "path": root,
                    "exists": os.path.isdir(root),
                }
                for root in roots
            ],
            "inherited": inherited,
            "read_only": bool(self.config.get("read-only")),
        }

    # ------------------------------------------------------------------
    # routes: browsing
    # ------------------------------------------------------------------
    @webui.route("workspaces")
    async def _route_workspaces(self, body=None, query=None):
        return self._workspace_list()

    @webui.route("tree")
    async def _route_tree(self, body=None, query=None):
        selector = (query or {}).get("workspace", "")
        rel = (query or {}).get("path", "")
        try:
            root = self._root(selector)
            entries = files.list_dir(
                root,
                rel,
                show_hidden=bool(self.config.get("show_hidden")),
                skip_dirs=self._skip_dirs(),
            )
        except files.FileError as e:
            return self.result(str(e), success=False)

        return {
            "workspace": os.path.basename(root) or root,
            "root": root,
            "path": files.safe_rel(rel),
            "entries": entries,
        }

    @webui.route("files")
    async def _route_files(self, body=None, query=None):
        """every file path in a workspace, for the Ctrl+P quick-open palette.

        Uncapped on purpose (see files.list_files); the palette scores and caps.
        """
        selector = (query or {}).get("workspace", "")
        try:
            root = self._root(selector)
            paths = files.list_files(
                root,
                show_hidden=bool(self.config.get("show_hidden")),
                skip_dirs=self._skip_dirs(),
            )
        except files.FileError as e:
            return self.result(str(e), success=False)

        return {"workspace": os.path.basename(root) or root, "files": paths}

    @webui.route("read")
    async def _route_read(self, body=None, query=None):
        selector = (query or {}).get("workspace", "")
        rel = (query or {}).get("path", "")
        if not rel:
            return self.result("path is required", success=False)

        try:
            root = self._root(selector)
            data = files.read_text(root, rel, max_bytes=self.config.get("max_read_bytes"))
        except files.FileError as e:
            return self.result(str(e), success=False)

        return {
            "workspace": os.path.basename(root) or root,
            "path": files.safe_rel(rel),
            "parent": files.parent_of(rel),
            "content": data["content"],
            "mtime": data["mtime"],
            "size": data["size"],
            "mode": data["mode"],
        }

    @webui.route("save", method="POST")
    async def _route_save(self, body=None, query=None):
        """write a file back to disk, guarded by the mtime the client loaded.

        The AI writes through the editor module's tools (blind writes), so the
        UI must refuse a save when the file changed underneath it — otherwise one
        of the two silently loses work.
        """
        if self.config.get("read-only"):
            return self.result("libreta is in read-only mode", success=False)

        body = body or {}
        workspace = str(body.get("workspace") or "")
        rel = body.get("path") or ""
        content = body.get("content")
        force = bool(body.get("force"))
        mtime = body.get("mtime") or 0

        if not isinstance(content, str):
            return self.result("content must be a string", success=False)

        try:
            root = self._root(workspace)
            result = files.write_text(root, rel, content, expected_mtime=mtime, force=force)
        except files.ConflictError as e:
            return self.result({"conflict": True, "message": str(e)}, success=False)
        except files.FileError as e:
            return self.result(str(e), success=False)

        return {"workspace": os.path.basename(root) or root, **result}

    @webui.route("create", method="POST")
    async def _route_create(self, body=None, query=None):
        if self.config.get("read-only"):
            return self.result("libreta is in read-only mode", success=False)

        body = body or {}
        workspace = str(body.get("workspace") or "")
        parent = str(body.get("parent") or "")
        name = str(body.get("name") or "").strip()
        content = body.get("content") or ""

        if not name:
            return self.result("a file name is required", success=False)

        try:
            root = self._root(workspace)
            result = files.create_file(root, files.join_rel(parent, name), content)
        except files.FileError as e:
            return self.result(str(e), success=False)

        return {"workspace": os.path.basename(root) or root, **result}

    @webui.route("mkdir", method="POST")
    async def _route_mkdir(self, body=None, query=None):
        if self.config.get("read-only"):
            return self.result("libreta is in read-only mode", success=False)

        body = body or {}
        workspace = str(body.get("workspace") or "")
        rel = str(body.get("path") or "").strip()

        if not rel:
            return self.result("a folder name is required", success=False)

        try:
            root = self._root(workspace)
            result = files.make_dir(root, rel)
        except files.FileError as e:
            return self.result(str(e), success=False)

        return {"workspace": os.path.basename(root) or root, **result}

    @webui.route("rename", method="POST")
    async def _route_rename(self, body=None, query=None):
        if self.config.get("read-only"):
            return self.result("libreta is in read-only mode", success=False)

        body = body or {}
        workspace = str(body.get("workspace") or "")
        path = body.get("path") or ""
        new_path = str(body.get("new_path") or "").strip()

        if not new_path:
            return self.result("a new name is required", success=False)

        try:
            root = self._root(workspace)
            result = files.rename_path(root, path, new_path)
        except files.FileError as e:
            return self.result(str(e), success=False)

        return {"workspace": os.path.basename(root) or root, **result}

    @webui.route("delete", method="POST")
    async def _route_delete(self, body=None, query=None):
        if self.config.get("read-only"):
            return self.result("libreta is in read-only mode", success=False)

        body = body or {}
        workspace = str(body.get("workspace") or "")
        path = body.get("path") or ""

        # explicit permanent delete (advanced, from the context menu) wins over
        # the setting; otherwise the setting decides trash vs permanent
        permanent = bool(body.get("permanent"))
        use_trash = bool(self.config.get("use_trash", True)) and not permanent

        try:
            root = self._root(workspace)
            result = files.delete_file(root, path, use_trash=use_trash)
        except files.FileError as e:
            return self.result(str(e), success=False)

        # drop it from the recent/pinned lists so they don't keep a dead entry
        name = os.path.basename(root) or root
        dead = files.safe_rel(path)
        recent, pinned = self._lists()
        recent = [r for r in recent if not (r.get("workspace") == name and r.get("path") == dead)]
        pinned = [p for p in pinned if not (p.get("workspace") == name and p.get("path") == dead)]
        self._save(recent=recent, pinned=pinned)

        return {"workspace": name, **result}

    # ------------------------------------------------------------------
    # routes: trashcan
    # ------------------------------------------------------------------
    @webui.route("trash")
    async def _route_trash(self, body=None, query=None):
        selector = (query or {}).get("workspace", "")
        try:
            root = self._root(selector)
            entries = files.list_trash(root)
        except files.FileError as e:
            return self.result(str(e), success=False)

        return {
            "workspace": os.path.basename(root) or root,
            "enabled": bool(self.config.get("use_trash", True)),
            "entries": entries,
        }

    @webui.route("trash/restore", method="POST")
    async def _route_trash_restore(self, body=None, query=None):
        if self.config.get("read-only"):
            return self.result("libreta is in read-only mode", success=False)

        body = body or {}
        workspace = str(body.get("workspace") or "")
        trashed = body.get("trashed") or ""

        try:
            root = self._root(workspace)
            result = files.restore_trash(root, trashed)
        except files.FileError as e:
            return self.result(str(e), success=False)

        return {"workspace": os.path.basename(root) or root, **result}

    @webui.route("trash/purge", method="POST")
    async def _route_trash_purge(self, body=None, query=None):
        body = body or {}
        workspace = str(body.get("workspace") or "")
        trashed = body.get("trashed") or ""

        try:
            root = self._root(workspace)
            result = files.purge_trash(root, trashed)
        except files.FileError as e:
            return self.result(str(e), success=False)

        return {"workspace": os.path.basename(root) or root, **result}

    @webui.route("trash/empty", method="POST")
    async def _route_trash_empty(self, body=None, query=None):
        body = body or {}
        workspace = str(body.get("workspace") or "")

        try:
            root = self._root(workspace)
            result = files.empty_trash(root)
        except files.FileError as e:
            return self.result(str(e), success=False)

        return {"workspace": os.path.basename(root) or root, **result}

    @webui.route("preview", method="POST")
    async def _route_preview(self, body=None, query=None):
        """snippets for card views. one call per tab, never a disk scan."""
        requested = (body or {}).get("files") or []
        limit = int(self.config.get("preview_chars") or 140)
        out = []

        for item in requested[:80]:
            workspace = str((item or {}).get("workspace", "") or "")
            rel = str((item or {}).get("path", "") or "")
            entry = {
                "workspace": workspace,
                "path": rel,
                "name": rel.rsplit("/", 1)[-1] if rel else "",
                "parent": files.parent_of(rel),
                "mode": files.mode_for(rel),
                "snippet": "",
                "mtime": 0,
                "size": 0,
                "missing": False,
                "error": "",
            }

            try:
                root = self._root(workspace)
                target = files.resolve(root, rel)
                head = files.read_head(root, rel, max_chars=max(limit * 8, 512))
                stat = os.stat(target)
                entry.update({
                    "snippet": files.make_snippet(head, limit),
                    "mtime": stat.st_mtime,
                    "size": stat.st_size,
                })
            except files.FileError as e:
                entry["missing"] = True
                entry["error"] = str(e)
            except OSError as e:
                entry["missing"] = True
                entry["error"] = str(e)

            out.append(entry)

        return {"files": out}

    # ------------------------------------------------------------------
    # options panel: the subset of settings a user can change from the tab
    # ------------------------------------------------------------------
    _BOOL_SETTINGS = (
        "code_autowrap", "notes_block_edit", "autosave_default", "show_hidden",
        "inherit_from_editor", "use_trash",
    )

    _NUMBER_SETTINGS = {
        "max_read_bytes": (1024, 50_000_000),
        "preview_chars": (40, 1000),
        "recent_max": (1, 200),
        "pinned_max": (1, 200),
        # 0 is a valid value here (autosave off), unlike the limits above
        "autosave_interval": (0, 600),
    }

    _SIDE_TABS = ("files", "recent", "pinned")

    def _settings_snapshot(self):
        roots, inherited = self._workspace_info()
        return {
            "values": {
                "code_autowrap": bool(self.config.get("code_autowrap", True)),
                "notes_block_edit": bool(self.config.get("notes_block_edit", True)),
                "autosave_default": bool(self.config.get("autosave_default", True)),
                "autosave_interval": int(self.config.get("autosave_interval") or 0),
                # read-only is not editable from this panel, but the editor has to
                # know: it must not schedule writes the server would refuse
                "read_only": bool(self.config.get("read-only")),
                "show_hidden": bool(self.config.get("show_hidden")),
                "use_trash": bool(self.config.get("use_trash", True)),
                "inherit_from_editor": bool(self.config.get("inherit_from_editor")),
                "max_read_bytes": int(self.config.get("max_read_bytes") or 1_000_000),
                "preview_chars": int(self.config.get("preview_chars") or 140),
                "recent_max": int(self.config.get("recent_max") or 30),
                "pinned_max": int(self.config.get("pinned_max") or 50),
                "default_side_tab": str(self.config.get("default_side_tab") or "files"),
            },
            "workspaces": [
                {"name": os.path.basename(root) or root, "path": root, "exists": os.path.isdir(root)}
                for root in roots
            ],
            "inherited": inherited,
        }

    def _write_setting(self, key, value):
        """persist one setting (config.yml is saved on write).

        Goes through the ConfigManager instead of ModuleConfig.set(): that one
        no-ops when the key is absent from the file, which would make a save
        look successful while writing nothing. The ConfigManager creates the
        key and saves.
        """
        manager = getattr(self.config, "config", None)
        if manager is not None:
            manager[key] = value
        else:
            self.config.set(key, value)

    def _apply_settings(self, values):
        """only the whitelisted keys are written; numbers are clamped, so a
        typo can never put the module in an unusable state."""
        values = values if isinstance(values, dict) else {}
        changed = []

        for key in self._BOOL_SETTINGS:
            if key in values:
                self._write_setting(key, bool(values[key]))
                changed.append(key)

        for key, (low, high) in self._NUMBER_SETTINGS.items():
            if key not in values:
                continue
            try:
                number = int(values[key])
            except (TypeError, ValueError):
                continue
            self._write_setting(key, max(low, min(high, number)))
            changed.append(key)

        tab = str(values.get("default_side_tab", ""))
        if tab in self._SIDE_TABS:
            self._write_setting("default_side_tab", tab)
            changed.append("default_side_tab")

        return changed

    @webui.route("settings")
    async def _route_settings(self, body=None, query=None):
        return self._settings_snapshot()

    @webui.route("settings/save", method="POST")
    async def _route_settings_save(self, body=None, query=None):
        # no reload: every route reads the config per call, so changes apply on
        # the next request. only enable_webui_extension would need a restart and
        # that one is not exposed here.
        changed = self._apply_settings((body or {}).get("values") or {})
        snapshot = self._settings_snapshot()
        snapshot["changed"] = changed
        return snapshot

    # ------------------------------------------------------------------
    # routes: recent / pinned state
    # ------------------------------------------------------------------
    @webui.route("state")
    async def _route_state(self, body=None, query=None):
        recent, pinned = self._lists()
        return {"recent": recent, "pinned": pinned, "autosave": self._autosave_state()}

    @webui.route("state/open", method="POST")
    async def _route_state_open(self, body=None, query=None):
        workspace = str((body or {}).get("workspace", "") or "")
        path = (body or {}).get("path", "")
        try:
            entry = self._entry(workspace, path)
        except files.FileError as e:
            return self.result(str(e), success=False)

        recent, pinned = self._lists()
        recent = [r for r in recent if not (r.get("workspace") == entry["workspace"] and r.get("path") == entry["path"])]
        recent.insert(0, entry)
        recent = recent[: int(self.config.get("recent_max") or 30)]
        return self._save(recent=recent, pinned=pinned)

    @webui.route("state/pin", method="POST")
    async def _route_state_pin(self, body=None, query=None):
        workspace = str((body or {}).get("workspace", "") or "")
        path = (body or {}).get("path", "")
        on = bool((body or {}).get("on", True))
        try:
            entry = self._entry(workspace, path)
        except files.FileError as e:
            return self.result(str(e), success=False)

        recent, pinned = self._lists()
        pinned = [p for p in pinned if not (p.get("workspace") == entry["workspace"] and p.get("path") == entry["path"])]
        if on:
            entry["at"] = time.time()
            pinned.insert(0, entry)
            pinned = pinned[: int(self.config.get("pinned_max") or 50)]
        return self._save(recent=recent, pinned=pinned)

    @webui.route("state/forget", method="POST")
    async def _route_state_forget(self, body=None, query=None):
        workspace = str((body or {}).get("workspace", "") or "")
        path = str((body or {}).get("path", "") or "")
        which = str((body or {}).get("list", "recent") or "recent")

        recent, pinned = self._lists()

        def keep(item):
            return not (item.get("workspace") == workspace and item.get("path") == path)

        if which == "pinned":
            pinned = [p for p in pinned if keep(p)]
        else:
            recent = [r for r in recent if keep(r)]

        return self._save(recent=recent, pinned=pinned)

    @webui.route("state/autosave", method="POST")
    async def _route_state_autosave(self, body=None, query=None):
        """Per-file autosave switch, stored per workspace + path so it survives
        closing the tab. `on` is the wanted state; an entry that matches
        autosave_default is dropped instead of stored."""
        body = body or {}
        try:
            entry = self._entry(str(body.get("workspace") or ""), body.get("path") or "")
        except files.FileError as e:
            return self.result(str(e), success=False)

        key = self._autosave_key(entry["workspace"], entry["path"])
        return {"autosave": self._autosave_set(key, bool(body.get("on", True)))}

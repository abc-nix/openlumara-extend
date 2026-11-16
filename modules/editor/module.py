# editor: workspace backbone + AI file tools (phase 2: read/navigation + write).
# webui filetree (replaces foldertree) + the workspace resolution machinery
# the AI tools build on.
#
# -- AI GENERATED CODE (deepseek-v4-flash) :: 2026-10-16
# Thin contract surface like docprocess: settings, events, AI tools and the
# /editor command live here; path logic in paths.py (PathResolver mixin),
# webui routes in webui.py (EditorRoutes mixin). AI tools MUST stay on the
# Editor class: tool_loader scans type(module).__dict__ without MRO, so a
# mixin method would never be registered (routes are the exception - the
# channel walks __mro__ for those).
#
# path contract (agreed 2026-10-16):
# - relative paths ALWAYS resolve against a workspace root, never against
#   the openlumara root (that was the file_manager bug).
# - sandboxed ON: tools can only touch files inside configured workspaces.
# - sandboxed OFF: workspaces still work for relative paths; absolute paths
#   anywhere are also accepted (phase 2 uses this).
# - the tree routes use foldertree's ws index; the AI tools use coder-style
#   selectors (basename / full path / 0-based index).

import os
import asyncio
import difflib
import base64
import shutil
import glob as globmod
import mimetypes
import filetype
import regex
from pathlib import Path

try:
    import tree_sitter_language_pack as tslp
    HAVE_TSLP = True
except ImportError:
    tslp = None
    HAVE_TSLP = False  # syntax gate degrades gracefully: no gate when missing

import core

from .paths import PathResolver
from .webui import EditorRoutes


class Editor(EditorRoutes, PathResolver, core.module.Module):
    """Browse, preview, search and manage your workspace folders from the webui sidebar, with one consistent AI file-tool family. Replaces foldertree, file_manager and media_reader."""

    category = "development"

    dependencies = ["filetype", "regex", "tree-sitter-language-pack"]

    # single source of truth: settings defaults and runtime fallbacks
    _DEFAULT_MAX_ENTRIES = 500
    _DEFAULT_MAX_PREVIEW_KB = 64
    _DEFAULT_SEARCH_MAX_RESULTS = 200
    _DEFAULT_SEARCH_SCAN_LIMIT = 50000
    _DEFAULT_CONTENT_MAX_MATCHES = 200

    # extensions the Open action refuses: the desktop handler may run them
    _DEFAULT_EXEC_EXTS = ["sh", "bash", "zsh", "fish", "bat", "cmd", "com",
                          "exe", "run", "appimage", "desktop"]

    # files the content search refuses to read (binary / huge)
    ui_max_read_bytes = 1_000_000

    settings = {
        "workspaces": {
            "default": ["."],
            "description": "Add paths for each workspace you want to include in the tree view. The tree will never browse outside these folders. Empty falls back to the openlumara main folder.",
            "depends": {"inherit_from_coder": False},
        },
        "combine_trees": {
            "default": True,
            "description": "combine multiple workspace folders into a single tree view. If off, you will be able to view workspaces separately.",
        },
        "inherit_from_coder": {
            "default": False,
            "description": "Use the coder module's sandboxes as the workspace folders and its folder blacklist as the hidden folders. Falls back to the manual settings when the coder module is off.",
        },
        "sandboxed": {
            "default": True,
            "description": "When on, file access is restricted to the configured workspaces. When off, absolute paths anywhere also work (the AI tools; phase 2).",
        },
        "read_outside_sandbox": {
            "default": False,
            "description": "In sandboxed mode, allow the read-only tools (list/read/info/glob/grep/read_media) to open absolute paths outside the workspaces. Writing tools stay sandboxed. Hidden when sandboxed is off (everything is already allowed).",
            "depends": {"sandboxed": True},
        },
        "read-only": {
            "default": False,
            "description": "Disables every action that can modify files (webui actions and AI tools). Use `/editor readonly` to toggle this during sessions.",
        },
        "line_limit": {
            "type": "number",
            "default": 1000,
            "description": "Hard limit on the lines editor_read may return at once. Greatly helps reduce token usage!",
        },
        "max_line_chars": {
            "type": "number",
            "default": 10000,
            "description": "Maximum characters shown per line in read/grep results. grep shows a window around the match, read truncates at the line start; a note reports what was cut. 0 = unlimited.",
        },
        "max_read_kb": {
            "type": "number",
            "default": 10240,
            "description": "Files larger than this (KB) are refused by read before loading (memory guard). 0 = disabled.",
        },
        "grep_max_file_kb": {
            "type": "number",
            "default": 1024,
            "description": "Files larger than this (KB) are skipped by grep (memory guard). 0 = disabled.",
        },
        "grep_scan_limit": {
            "type": "number",
            "default": 50000,
            "description": "Maximum files grep scans before stopping; the result note reports which cap was hit.",
        },
        "grep_timeout": {
            "type": "number",
            "default": 3,
            "description": "Seconds a single regex search may run on one line before it is skipped.",
        },
        "grep_max_chars": {
            "type": "number",
            "default": 50000,
            "description": "Maximum total characters of grep results (match windows + context lines) before the scan stops; the note reports which cap was hit. 0 = unlimited.",
        },
        "insert_workspaces_into_system_prompt": {
            "default": True,
            "description": "List the configured workspaces in the system prompt so the AI always knows where it can work.",
        },
        "syntax_check": {
            "default": True,
            "description": "Validate code syntax before create/edit touches disk. Files treesitter cannot check are never blocked. write/append stay raw.",
        },
        "allow_recursive_delete": {
            "default": False,
            "description": "Allow the AI delete tool to remove non-empty folders (recursive=True). Default off: delete only removes files or empty folders. The webui delete always asks for confirmation.",
        },
        "glob_max_results": {
            "type": "number",
            "default": 200,
            "description": "Maximum matches editor_glob returns per call (the max_results argument overrides this).",
        },
        "grep_max_matches": {
            "type": "number",
            "default": 30,
            "description": "Maximum matches editor_grep returns per call (the max_matches argument overrides this).",
        },
        "max_image_mb": {
            "type": "number",
            "default": 4,
            "description": "Max image size in MB that read_media attaches inline. Larger images return metadata only. Set to 0 to never attach images.",
        },
        "max_video_mb": {
            "type": "number",
            "default": 20,
            "description": "Reserved: videos are not attached yet (the framework has no video/frame delivery path, so read_media returns metadata only). This will cap the clip size once frame extraction lands.",
        },
        "max_entries": {
            "description": "Maximum number of entries shown per folder. Protects the sidebar from huge directories.",
            "type": "number",
            "default": _DEFAULT_MAX_ENTRIES,
        },
        "max_preview_kb": {
            "description": "Maximum size of a text preview, in KB. Larger files show metadata only. Set to 0 to disable previews.",
            "type": "number",
            "default": _DEFAULT_MAX_PREVIEW_KB,
        },
        "search_max_results": {
            "description": "Maximum number of matches a name search returns. If a search stops early, the panel says which limit was reached.",
            "type": "number",
            "default": _DEFAULT_SEARCH_MAX_RESULTS,
        },
        "search_scan_limit": {
            "description": "Maximum number of entries a name search scans before stopping. Guards against very large or slow folders. If the panel says the scan limit was reached, raise this.",
            "type": "number",
            "default": _DEFAULT_SEARCH_SCAN_LIMIT,
        },
        "content_max_matches": {
            "description": "Maximum number of matches a content search returns.",
            "type": "number",
            "default": _DEFAULT_CONTENT_MAX_MATCHES,
        },
        "skip_dirs": {
            "default": [],
            "description": "Folder names to hide from the tree and from search (e.g. node_modules, .git, __pycache__). Matched by exact name at any depth. Leave empty to show everything.",
            "depends": {"inherit_from_coder": False},
        },
        "open_command": {
            "description": "Command used by the Open action to open files/folders with the system default app. Common values: xdg-open (Linux), gio open, exo-open, open (macOS).",
            "default": "xdg-open",
        },
        "executable_extensions": {
            "default": list(_DEFAULT_EXEC_EXTS),
            "description": "Extensions the Open action refuses (case-insensitive), because the system may run them instead of opening them. Folders and other files are unaffected.",
        },
        "max_attach_mb": {
            "description": "Maximum file size the Attach action can add to a chat message, in MB. Set to 0 to disable Attach.",
            "type": "number",
            "default": 10,
        },
    }

    # -------------
    # events
    # -------------
    async def on_ready(self):
        self.disabled_tools = []
        if self.config.get("read-only"):
            self.disabled_tools.extend(
                ["write", "append", "edit", "create", "move", "copy", "delete", "mkdir"]
            )
        # the webui routes have their own gate (webui.py _read_only_guard).

    async def on_system_prompt(self):
        parts = []
        if self.config.get("insert_workspaces_into_system_prompt", True):
            roots = self._workspace_roots()
            if roots:
                mode = "sandboxed" if self.config.get("sandboxed") else "absolute"
                lines = [f"## Editor workspaces ({mode} mode):"]
                for r in roots:
                    lines.append(f"- {r.name or str(r)} ({r})")
                lines.append("file paths are relative to the workspace root; workspace is the name above, its path, or its 0-based index, or '<workspace>/<subfolder>' to scope into a subfolder, e.g. '" + (roots[0].name or str(roots[0])) + "/some/subdir' (see editor_workspaces)")
                if self.config.get("read_outside_sandbox"):
                    lines.append("read-only tools may also open absolute paths outside these workspaces (read_outside_sandbox)")
                if not self.config.get("sandboxed"):
                    lines.append("pass an empty workspace name to use absolute paths anywhere")
                parts.append("\n".join(lines))
        if self.config.get("read-only"):
            parts.append(
                "## IMPORTANT: Read-Only Mode\nYour editor module is in read-only mode and cannot modify files. Provide output to the user directly in your messages."
            )
        return "\n\n".join(parts) if parts else None

    # --------------------
    # AI tools (phase 2: read/navigation first; write/manage/search follow)
    # --------------------
    @core.module.tool
    async def workspaces(self):
        """lists configured workspaces (name, path, exists) and the current path mode"""
        roots = self._workspace_roots()
        return self.result({
            "mode": "sandboxed" if self.config.get("sandboxed") else "absolute",
            "read_only": bool(self.config.get("read-only")),
            "read_outside_sandbox": bool(self.config.get("read_outside_sandbox")),
            "inherited": self._inherit_active(),
            "workspaces": [
                {"name": r.name or str(r), "path": str(r), "exists": r.is_dir()}
                for r in roots
            ],
        })

    @core.module.tool
    async def list(self, workspace: str, path: str = ""):
        """lists one directory level, folders first. paths are relative to the workspace root"""
        try:
            target = self._resolve_path(workspace, path, read_only=True)
        except ValueError as e:
            return self.result(str(e), False)
        if not target.exists():
            return self.result(f"'{target.name}' does not exist", False)
        if not target.is_dir():
            return self.result(f"'{target.name}' is not a folder", False)

        cap = max(1, self._num_setting("max_entries", self._DEFAULT_MAX_ENTRIES))
        blacklist = set(self._skip_dirs())
        entries = []
        truncated = False
        try:
            with os.scandir(target) as scan:
                for entry in scan:
                    try:
                        is_dir = entry.is_dir()
                        st = entry.stat()
                    except OSError:
                        continue  # unreadable/broken entry: skip it silently
                    if is_dir and entry.name in blacklist:
                        continue  # blacklisted folder: hidden from listings too
                    if len(entries) >= cap:
                        truncated = True
                        break
                    entries.append({
                        "name": entry.name,
                        "is_dir": is_dir,
                        "size": None if is_dir else st.st_size,
                        "modified": st.st_mtime,
                    })
        except PermissionError:
            return self.result(f"permission denied: '{target.name}'", False)

        entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
        return self.result({"path": str(target), "entries": entries, "truncated": truncated})

    @core.module.tool
    async def read(self, workspace: str, path: str, line_start: int = None, line_end: int = None,
                   char_start: int = None, char_end: int = None):
        """reads a file, or part of it. use line_start and line_end to read in chunks; char_start/char_end read an exact character window of each line (bypasses max_line_chars)"""
        try:
            target = self._resolve_path(workspace, path, read_only=True)
        except ValueError as e:
            return self.result(str(e), False)
        if not target.exists():
            return self.result(f"'{target.name}' does not exist", False)
        if not target.is_file():
            return self.result(f"'{target.name}' is not a file", False)

        max_read_kb = self._num_setting("max_read_kb", 10240)
        if max_read_kb:
            try:
                size = target.stat().st_size
            except Exception as e:
                return self.result(str(e), False)
            if size > max_read_kb * 1024:
                return self.result({
                    "error": f"file is {size} bytes, over max_read_kb ({max_read_kb} KB)",
                    "note": "raise max_read_kb, read in chunks, or use grep to search it",
                }, False)

        try:
            with open(target, "r", encoding="utf-8") as f:
                content = f.read()
        except UnicodeDecodeError:
            return self.result("file is not valid utf-8 text - use info for metadata", False)
        except Exception as e:
            return self.result(str(e), False)

        lines = content.split("\n")
        # a trailing newline is not an extra line: count real lines only
        total = len(lines)
        if lines and lines[-1] == "":
            total -= 1
        max_lines = max(1, self._num_setting("line_limit", 1000))

        if line_start is None and line_end is None:
            if total <= max_lines:
                line_start, line_end = 1, len(lines)
            else:
                return self.result({
                    "error": f"file has {total} lines, over the line limit of {max_lines}",
                    "note": "read it in chunks with line_start and line_end",
                }, False)

        start = max(0, (line_start or 1) - 1)
        if start >= total:
            return self.result(
                f"line_start ({line_start}) is beyond the file's {total} lines", False)
        end = len(lines) if (line_end is None or line_end < 1 or line_end > total) else line_end
        if end - start > max_lines:
            end = start + max_lines

        try:
            char_start = int(char_start) if char_start is not None else None
        except (TypeError, ValueError):
            char_start = None
        try:
            char_end = int(char_end) if char_end is not None else None
        except (TypeError, ValueError):
            char_end = None
        max_chars = self._num_setting("max_line_chars", 10000)

        window_lines = lines[start:end]
        if (char_start is not None or char_end is not None) and window_lines and window_lines[-1] == "":
            # exact windows are about characters: drop the trailing-newline artifact
            window_lines = window_lines[:-1]
        out_lines = []
        truncated_lines = []
        for i, ln in enumerate(window_lines, start=start + 1):
            if char_start is not None or char_end is not None:
                # exact character window (1-based, inclusive): bypasses the cap
                cs = max(1, char_start or 1)
                ce = char_end if char_end is not None else len(ln)
                out_lines.append(ln[cs - 1:ce])
                continue
            if max_chars and len(ln) > max_chars:
                truncated_lines.append({"line": i, "shown": max_chars, "total": len(ln)})
                out_lines.append(ln[:max_chars] + "…")
            else:
                out_lines.append(ln)
        partial = "\n".join(out_lines)
        remaining = total - end
        truncated = remaining > 0
        result = {"content": partial, "truncated": truncated}
        if truncated:
            result["truncation_note"] = (
                f"showing lines {start + 1}-{end} of {total}. {remaining} lines remaining.")
        if truncated_lines:
            result["char_truncated"] = True
            result["char_truncation_note"] = (
                f"lines truncated at {max_chars} chars: "
                + ", ".join(f"{t['line']} ({t['total']} chars)" for t in truncated_lines[:5])
                + ("…" if len(truncated_lines) > 5 else ""))
        return self.result(result)

    @core.module.tool
    async def info(self, workspace: str, path: str):
        """returns metadata for a file or folder (size, dates, type). for files also the detected mime (magic bytes) and whether it is text"""
        try:
            target = self._resolve_path(workspace, path, read_only=True)
        except ValueError as e:
            return self.result(str(e), False)
        if not target.exists():
            return self.result("path does not exist", False)

        try:
            st = target.stat()
        except Exception as e:
            return self.result(str(e), False)

        out = {
            "name": target.name,
            "path": str(target),
            "size_bytes": st.st_size,
            "is_file": target.is_file(),
            "is_dir": target.is_dir(),
            "modified": st.st_mtime,
            "created": st.st_ctime,
        }
        if target.is_file():
            try:
                with open(target, "rb") as f:
                    head = f.read(8192)  # enough for magic-byte detection
                kind = filetype.guess(head)
            except Exception:
                kind = None
            if kind:
                out["mime"] = kind.mime
                out["is_text"] = False
                out["note"] = "binary content is not returned inline"
            else:
                try:
                    head.decode("utf-8")
                    is_text = True
                except UnicodeDecodeError:
                    is_text = False
                mime, _ = mimetypes.guess_type(target.name)
                out["mime"] = mime or ("text/plain" if is_text else "unknown")
                out["is_text"] = is_text
                if not is_text:
                    out["note"] = "binary content is not returned inline"
        return self.result(out)

    # -------------------------------
    # helper: syntax gate (treesitter)
    # -------------------------------
    async def _syntax_gate(self, code, target):
        """return a list of syntax errors, or [] when skipped/clean.

        mirrors the coder: languages treesitter cannot check (unknown,
        vimdoc/plaintext, html) are never blocked; a failed parse is blocked
        for safety; diagnostics are reported with line/col.
        """
        if not self.config.get("syntax_check", True) or not HAVE_TSLP:
            return []
        lang = tslp.detect_language_from_path(str(target))
        if not lang or lang in ("vimdoc", "html", "htm"):
            return []
        try:
            result = tslp.process(code, tslp.ProcessConfig(
                language=lang, diagnostics=True, structure=False))
        except Exception:
            return []
        if not result:
            return ["failed to parse the file for syntax errors - file was not written to disk"]
        return [
            f"line {d.span.start_line + 1}, col {d.span.start_column + 1}: {d.message}"
            for d in (result.diagnostics or [])
        ]

    # ----------------------
    # tools: writing
    # ----------------------
    @core.module.tool
    async def write(self, workspace: str, path: str, content: str):
        """writes content to a file, overwriting it. auto-creates parent folders. no syntax check (raw) - use create/edit for gated writes"""
        try:
            target = self._resolve_path(workspace, path)
        except ValueError as e:
            return self.result(str(e), False)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            return self.result(str(e), False)
        return self.result(f"wrote {path}")

    @core.module.tool
    async def append(self, workspace: str, path: str, content: str):
        """appends content to a file, creating it if missing. no syntax check (raw)"""
        try:
            target = self._resolve_path(workspace, path)
        except ValueError as e:
            return self.result(str(e), False)
        if target.exists():
            try:
                with open(target, "r", encoding="utf-8") as f:
                    f.read()
            except UnicodeDecodeError:
                return self.result("file is not valid utf-8 text - cannot append to it", False)
            except Exception as e:
                return self.result(str(e), False)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "a", encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            return self.result(str(e), False)
        return self.result(f"appended to {path}")

    @core.module.tool
    async def create(self, workspace: str, path: str, content: str):
        """creates a new file, refusing to overwrite existing ones. auto-creates parent folders. syntax-checked"""
        try:
            target = self._resolve_path(workspace, path)
        except ValueError as e:
            return self.result(str(e), False)
        errors = await self._syntax_gate(content, target)
        if errors:
            return self.result({"errors": errors, "message": "syntax check failed - file was not written"}, False)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "x", encoding="utf-8") as f:
                f.write(content)
        except FileExistsError:
            return self.result(f"'{path}' already exists - use write to overwrite or edit to modify", False)
        except Exception as e:
            return self.result(str(e), False)
        return self.result(f"created {path}")

    @core.module.tool
    async def edit(self, workspace: str, path: str, original_code: str, replacement_code: str):
        """replaces original_code with replacement_code in a file. fails if nothing matched. syntax-checked"""
        try:
            target = self._resolve_path(workspace, path)
        except ValueError as e:
            return self.result(str(e), False)
        try:
            with open(target, "r", encoding="utf-8") as f:
                current = f.read()
        except UnicodeDecodeError:
            return self.result("file is not valid utf-8 text - cannot edit it", False)
        except Exception as e:
            return self.result(str(e), False)
        if not current:
            return self.result("file is empty", False)

        new_content = current.replace(original_code, replacement_code)
        if new_content == current:
            return self.result("failed to match original_code against the file contents - try a shorter unique snippet", False)

        errors = await self._syntax_gate(new_content, target)
        if errors:
            return self.result({"errors": errors, "message": "syntax check failed - file was not written"}, False)
        try:
            with open(target, "w", encoding="utf-8") as f:
                f.write(new_content)
        except Exception as e:
            return self.result(str(e), False)
        return self.result(f"edited {path}")

    # ----------------------
    # tools: file management
    # ----------------------
    @core.module.tool
    async def move(self, workspace: str, orig_path: str, target_path: str):
        """moves or renames a file/folder. destination parent folders are created. refuses to overwrite an existing destination"""
        try:
            src = self._resolve_path(workspace, orig_path)
            dst = self._resolve_path(workspace, target_path)
        except ValueError as e:
            return self.result(str(e), False)
        if not src.exists():
            return self.result(f"'{orig_path}' does not exist", False)
        if dst.exists():
            return self.result(f"destination already exists: {target_path}", False)
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
        except Exception as e:
            return self.result(str(e), False)
        return self.result(f"moved {orig_path} -> {target_path}")

    @core.module.tool
    async def copy(self, workspace: str, orig_path: str, target_path: str):
        """copies a file or folder to a new location. destination parent folders are created. refuses to overwrite an existing destination"""
        try:
            src = self._resolve_path(workspace, orig_path)
            dst = self._resolve_path(workspace, target_path)
        except ValueError as e:
            return self.result(str(e), False)
        if not src.exists():
            return self.result(f"'{orig_path}' does not exist", False)
        if dst.exists():
            return self.result(f"destination already exists: {target_path}", False)
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(str(src), str(dst))
            else:
                shutil.copy2(str(src), str(dst))
        except Exception as e:
            return self.result(str(e), False)
        return self.result(f"copied {orig_path} -> {target_path}")

    @core.module.tool
    async def delete(self, workspace: str, path: str, recursive: bool = False):
        """deletes a file, or an EMPTY folder. recursive=True (non-empty folders) only works when allow_recursive_delete is on. a configured workspace root can never be deleted"""
        try:
            target = self._resolve_path(workspace, path)
        except ValueError as e:
            return self.result(str(e), False)
        if not target.exists():
            return self.result("path does not exist", False)
        # a configured workspace root can never be deleted (any mode)
        if target in self._workspace_roots():
            return self.result("a configured workspace root cannot be deleted", False)
        try:
            if target.is_dir() and not target.is_symlink():
                if recursive:
                    if not self.config.get("allow_recursive_delete"):
                        return self.result(
                            "recursive deletion is disabled (allow_recursive_delete is off)", False)
                    shutil.rmtree(target)
                else:
                    target.rmdir()  # empty folders only
            else:
                target.unlink()
        except OSError as e:
            return self.result(str(e), False)
        return self.result(f"deleted {path}")

    @core.module.tool
    async def mkdir(self, workspace: str, path: str):
        """creates a directory and any needed parent directories"""
        try:
            target = self._resolve_path(workspace, path)
        except ValueError as e:
            return self.result(str(e), False)
        try:
            target.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return self.result(str(e), False)
        return self.result(f"created folder {path}")

    # ----------------------
    # tools: search
    # ----------------------
    def _as_list(self, value):
        """normalize a list-typed tool argument: some call paths deliver
        lists as JSON strings ('["py"]') or bare strings ('py')."""
        if value is None:
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            s = value.strip()
            if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
                s = s[1:-1]  # strip surrounding quotes
            if s.startswith("[") and s.endswith("]"):
                for parser in ("json", "ast"):
                    try:
                        if parser == "json":
                            import json as _json
                            parsed = _json.loads(s)
                        else:
                            import ast as _ast
                            parsed = _ast.literal_eval(s)
                        if isinstance(parsed, list):
                            return parsed
                    except Exception:
                        continue
            if "," in s:
                return [x.strip() for x in s.split(",") if x.strip()]
            return [s]
        return [value]

    @core.module.tool
    async def glob(self, workspace: str, pattern: str, sub_path: str = "", max_results: int = None):
        """finds files by glob pattern (no regex). paths are relative to the workspace root"""
        pattern = str(pattern or "").strip()
        if not pattern:
            return self.result("pattern is required", False)
        if os.path.isabs(pattern):
            return self.result("pattern must be relative (no absolute paths)", False)
        try:
            root = self._resolve_path(workspace, sub_path or "", read_only=True)
        except ValueError as e:
            return self.result(str(e), False)
        if not root.exists():
            return self.result(f"'{root.name}' does not exist", False)
        if not root.is_dir():
            return self.result("sub_path is not a folder", False)

        blacklist = set(self._skip_dirs())
        cap = max(1, int(max_results) if max_results else self._num_setting("glob_max_results", 200))
        try:
            raw_matches = globmod.glob(os.path.join(str(root), pattern), recursive=True)
        except Exception as e:
            return self.result(str(e), False)

        results = []
        total = 0
        for match in sorted(raw_matches):
            p = Path(match).resolve()
            # containment: a pattern with ../ could escape the workspace
            if p != root and root not in p.parents:
                continue
            rel = os.path.relpath(p, str(root))
            if any(part in blacklist for part in Path(rel).parts):
                continue
            total += 1
            if total > cap:
                break
            results.append(rel.replace(os.sep, "/"))

        if total > cap:
            return self.result({
                "results": results,
                "truncated": True,
                "total": total,
                "note": f"showing first {cap} of {total} matches - narrow your pattern to see more",
            })
        return self.result(results)

    @core.module.tool
    async def grep(self, workspace: str, sub_path: str, regex_pattern: str,
                   case_sensitive: bool = False, context: int = 0,
                   file_extensions: list = None, max_matches: int = None):
        """searches a regex pattern in a file or in all files under a folder. match lines are shown as a window around the match (max_line_chars); each match reports char_start; context lines are capped at 500 chars; total result size is capped at grep_max_chars (0 = unlimited)"""
        if str(regex_pattern or "").strip() in [".", "*"]:
            return self.result("Forbidden pattern detected. Use a more specific search!", False)
        try:
            target = self._resolve_path(workspace, sub_path or "", read_only=True)
        except ValueError as e:
            return self.result(str(e), False)
        if not target.exists():
            return self.result("path does not exist", False)

        flags = 0 if case_sensitive else regex.IGNORECASE
        try:
            compiled = regex.compile(regex_pattern, flags)
        except Exception as e:
            return self.result(f"invalid regex: {e}", False)
        blacklist = set(self._skip_dirs())
        cap = max(1, int(max_matches) if max_matches else self._num_setting("grep_max_matches", 30))
        ctx = max(0, int(context or 0))
        exts = [str(e).lstrip(".").lower() for e in self._as_list(file_extensions)]
        max_chars = self._num_setting("max_line_chars", 10000)
        file_kb = self._num_setting("grep_max_file_kb", 1024)
        scan_limit = max(1, self._num_setting("grep_scan_limit", 50000))
        timeout = max(0, self._num_setting("grep_timeout", 3))
        total_budget = max(0, self._num_setting("grep_max_chars", 50000))
        # per-entry budget: context lines share max_chars with the match line
        if max_chars:
            if ctx:
                ctx_cap = min(500, max(1, max_chars // (2 * ctx + 1)))
            else:
                ctx_cap = 500
            match_budget = max(1, max_chars - 2 * ctx * ctx_cap)
        else:
            ctx_cap = None
            match_budget = None

        def window(text, match_start, budget):
            """centered window around the match; returns (text, shown_range or None)"""
            text = text.rstrip("\n")
            if budget is None or len(text) <= budget:
                return text, None
            s = max(0, match_start - budget // 2)
            e = min(len(text), s + budget)
            if e - s < budget:
                s = max(0, e - budget)
            piece = text[s:e]
            if s > 0:
                piece = "…" + piece
            if e < len(text):
                piece = piece + "…"
            return piece, (s + 1, e)

        def ctx_line(l):
            l = l.rstrip("\n")
            if ctx_cap and len(l) > ctx_cap:
                return l[:ctx_cap] + "…"
            return l

        def scan_file(filepath, rel_base, state):
            """grep one file, return (matches, hit) - hit is None, "max_matches" or "chars\""""
            matches = []
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    all_lines = f.readlines()
            except (UnicodeDecodeError, OSError):
                return matches, None
            for line_num, line in enumerate(all_lines, 1):
                try:
                    m = compiled.search(line, timeout=timeout)
                    if m is None:
                        continue
                except TimeoutError:
                    continue
                piece, shown = window(line, m.start(), match_budget)
                entry = {
                    "file": os.path.relpath(filepath, rel_base).replace(os.sep, "/"),
                    "line_num": line_num,
                    "line": piece,
                    "char_start": m.start() + 1,
                }
                if shown:
                    entry["chars_shown"] = shown
                    entry["line_chars"] = len(line.rstrip("\n"))
                if ctx:
                    start = max(0, line_num - 1 - ctx)
                    end = min(len(all_lines), line_num + ctx)
                    entry["context_before"] = [ctx_line(l) for l in all_lines[start:line_num - 1]]
                    entry["context_after"] = [ctx_line(l) for l in all_lines[line_num:end]]
                matches.append(entry)
                if total_budget:
                    state["chars"] += len(piece) + sum(
                        len(l) for l in entry.get("context_before", [])
                    ) + sum(len(l) for l in entry.get("context_after", []))
                    if state["chars"] >= total_budget:
                        return matches, "chars"
                if len(matches) >= cap:
                    return matches, "max_matches"
            return matches, None

        def scan():
            """walk the target (file or folder); returns (matches, truncated, skipped, cap_hit)"""
            matches = []
            skipped = 0
            visited = 0
            state = {"chars": 0}
            if target.is_file():
                if file_kb:
                    try:
                        if target.stat().st_size > file_kb * 1024:
                            return [], False, 1, "file_kb"
                    except OSError:
                        pass
                m, hit = scan_file(str(target), str(target.parent), state)
                return m, hit, 0, hit or ""
            for dirpath, dirnames, filenames in os.walk(target):
                dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in blacklist]
                for name in filenames:
                    if exts:
                        _, ext = os.path.splitext(name)
                        if ext[1:].lower() not in exts:
                            continue
                    fp = os.path.join(dirpath, name)
                    if file_kb:
                        try:
                            if os.path.getsize(fp) > file_kb * 1024:
                                skipped += 1
                                continue
                        except OSError:
                            continue
                    visited += 1
                    if visited > scan_limit:
                        return matches, True, skipped, "scan_limit"
                    m, hit = scan_file(fp, str(target), state)
                    matches.extend(m)
                    if hit:
                        return matches, True, skipped, hit
                    if len(matches) >= cap:
                        return matches, True, skipped, "max_matches"
            return matches, False, skipped, ""

        matches, truncated, skipped, cap_hit = await asyncio.to_thread(scan)

        if not matches:
            if cap_hit == "file_kb":
                return self.result({
                    "error": "file is over grep_max_file_kb",
                    "note": "raise grep_max_file_kb (0 = disabled) or narrow the search",
                }, False)
            if skipped:
                return self.result({
                    "error": "No matches found",
                    "note": f"{skipped} file(s) skipped (over grep_max_file_kb).",
                }, False)
            return self.result("No matches found", False)
        result = {"matches": matches}
        notes = []
        if truncated:
            if cap_hit == "max_matches":
                notes.append(
                    f"Showing up to {cap} matches. More results may be available "
                    f"- narrow your search pattern to see them.")
            elif cap_hit == "scan_limit":
                notes.append(f"Stopped early: scan limit reached ({scan_limit} files visited).")
            elif cap_hit == "chars":
                chars_shown = sum(
                    len(e["line"])
                    + sum(len(l) for l in e.get("context_before", []))
                    + sum(len(l) for l in e.get("context_after", []))
                    for e in matches
                )
                notes.append(f"Stopped early: result char budget reached ({chars_shown} chars shown).")
        if skipped:
            notes.append(f"{skipped} file(s) skipped (over grep_max_file_kb).")
        if notes:
            result["note"] = " ".join(notes)
        return self.result(result)

    # ----------------------
    # tools: media
    # ----------------------
    async def _attach_media(self, meta, hint):
        """deliver an image to the model as its own user message.

        the tool result is JSON text, so an image cannot travel inside it -
        it is added to the context as an internal user message carrying an
        image_url block, the same contract browser_screenshot uses. the
        inference API only accepts images in user turns; _metadata.internal
        keeps the message out of the turn history (core/turns.py) while
        context.get() still sends it to the model. adding it directly avoids
        channel.send() inside a tool call, which deadlocks the channel's
        stream chain. the result itself stays plain metadata."""
        max_mb = self.config.get("max_image_mb")
        max_bytes = int(float(max_mb) * 1024 * 1024) if max_mb else 0
        if meta["size_bytes"] > max_bytes:
            meta["note"] = (
                f"Image too large to attach inline ({meta['size_bytes']} bytes > "
                f"{max_bytes} byte limit). Raise max_image_mb if needed. Metadata only.")
            return self.result(meta)
        with open(meta["path"], "rb") as f:
            raw = f.read()
        mime = meta["mime_type"]
        await self.channel.context.chat.messages.add({
            "role": "user",
            "content": [{
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,"
                                     + base64.b64encode(raw).decode("ascii")},
            }],
            "_metadata": {"internal": "read_media", "path": meta["path"]},
        })
        meta["hint"] = hint
        return self.result(meta)

    @core.module.tool
    async def read_media(self, workspace: str, path: str, attach: bool = True):
        """attaches an image so the AI can see it. the image arrives as its own user message, not inside the tool result. videos are not attached (no frame path yet) and return metadata only. attach=False returns metadata only"""
        try:
            target = self._resolve_path(workspace, path, read_only=True)
        except ValueError as e:
            return self.result(str(e), False)
        if not target.exists():
            return self.result("path does not exist", False)
        if not target.is_file():
            return self.result("path is not a file", False)

        try:
            size = target.stat().st_size
            with open(target, "rb") as f:
                head = f.read(8192)
            kind = filetype.guess(head)
            mime = kind.mime if kind else "unknown"
        except Exception as e:
            return self.result(str(e), False)

        meta = {"path": str(target), "file": target.name,
                "size_bytes": size, "mime_type": mime}
        if not attach:
            if mime.startswith("image/") or mime.startswith("video/"):
                meta["note"] = "Media detected but attach is false; not attached."
            else:
                meta["note"] = "File is not an image or video; nothing to attach."
            return self.result(meta)

        if mime.startswith("image/"):
            return await self._attach_media(
                meta, "Image is attached; you will see it in the next message.")

        if mime.startswith("video/"):
            meta["note"] = ("Video detected but not attached: the framework has no video/"
                            "frame delivery path (images only). Extract frames and read them "
                            "as images, or transcribe the clip.")
            return self.result(meta)

        meta["note"] = "File is not an image or video; nothing to attach."
        return self.result(meta)

    # ---------------------
    # tool result summaries (webui header hints)
    # ---------------------
    def on_tool_summary(self, name, args, response):
        """one-line result hint for the completed tool call header.

        NOT a tool (no @core.module.tool): core/turns.py's
        TurnCollector.on_result_summary calls this hook on the module that
        owns the tool and stamps the return value as _metadata.result_summary
        for the webui (live stream AND history reloads). the caller flattens
        to the first line and caps it at 120 chars, and swallows exceptions.
        """
        match name:
            case "editor_edit":
                if not isinstance(response, dict) or response.get("status") != "success":
                    return ""
                original = args.get("original_code") or ""
                replacement = args.get("replacement_code") or ""
                if not original or not replacement:
                    return ""
                added = deleted = 0
                matcher = difflib.SequenceMatcher(
                    None, original.splitlines(), replacement.splitlines())
                for op, i1, i2, j1, j2 in matcher.get_opcodes():
                    if op in ("insert", "replace"):
                        added += j2 - j1
                    if op in ("delete", "replace"):
                        deleted += i2 - i1
                return f"+{added} \u2212{deleted}"

            case "editor_read":
                if not isinstance(response, dict) or response.get("status") == "error":
                    return ""
                content = response.get("content")
                if isinstance(content, dict):
                    content = content.get("content")
                if not isinstance(content, str) or not content:
                    return ""
                text = content[:-1] if content.endswith("\n") else content
                n = len(text.split("\n"))
                return f"{n} line" + ("" if n == 1 else "s")

            case "editor_write" | "editor_append" | "editor_create":
                if not isinstance(response, dict) or response.get("status") != "success":
                    return ""
                content = args.get("content") or ""
                if not content:
                    return ""
                text = content[:-1] if content.endswith("\n") else content
                n = len(text.split("\n"))
                return f"{n} line" + ("" if n == 1 else "s")

            case "editor_grep":
                if not isinstance(response, dict) or response.get("status") != "success":
                    return ""
                c = response.get("content")
                if isinstance(c, list):
                    items = c
                elif isinstance(c, dict) and isinstance(c.get("matches"), list):
                    items = c["matches"]
                else:
                    items = []
                fallback = args.get("sub_path") or "matches"
                files = set()
                n = 0
                for m in items:
                    if not isinstance(m, dict):
                        continue
                    files.add(m.get("file") or fallback)
                    n += 1
                if not n:
                    return ""
                if len(files) > 1:
                    return f"{n} matches \u00b7 {len(files)} files"
                return f"{n} match" + ("" if n == 1 else "es")

            case "editor_glob":
                if not isinstance(response, dict) or response.get("status") != "success":
                    return ""
                c = response.get("content")
                if isinstance(c, list):
                    items = c
                elif isinstance(c, dict) and isinstance(c.get("results"), list):
                    items = c["results"]
                else:
                    items = []
                n = len([f for f in items if isinstance(f, str)])
                if not n:
                    return ""
                return f"{n} file" + ("" if n == 1 else "s")

        return ""

    # ---------------------
    # user-facing commands
    # ---------------------
    @core.module.command("editor", help={
        "readonly": "toggle read-only mode",
        "sandbox": "toggle sandboxed mode (off = absolute paths anywhere allowed)",
        "readoutside": "toggle read_outside_sandbox (read-only tools may open absolute paths outside workspaces)",
    })
    async def cmd_editor(self, args: list):
        if not args:
            return "please specify a sub-command (see /help editor)"
        if args[0] == "readonly":
            current_state = self.config.get("read-only")
            self.config.set("read-only", not current_state)
            await self.manager.reload_module(self.name)
            return "read-only mode disabled" if current_state else "read-only mode enabled"
        if args[0] == "sandbox":
            current_state = self.config.get("sandboxed")
            self.config.set("sandboxed", not current_state)
            # no reload needed: tools read the config per call; only the
            # system prompt (rebuilt per message) reflects the new mode
            if current_state:
                return "sandboxed mode disabled - absolute paths anywhere now work (use with care)"
            return "sandboxed mode enabled - file access restricted to workspaces again"
        if args[0] == "readoutside":
            current_state = self.config.get("read_outside_sandbox")
            self.config.set("read_outside_sandbox", not current_state)
            # no reload needed (same reasoning as sandbox)
            if current_state:
                return "read_outside_sandbox disabled - read-only tools are confined to the workspaces again"
            note = ""
            if not self.config.get("sandboxed"):
                note = " (only relevant while sandboxed mode is on - it is currently off)"
            return f"read_outside_sandbox enabled - read-only tools may open absolute paths outside the workspaces{note}"
        return "unknown sub-command (see /help editor)"

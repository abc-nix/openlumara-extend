# editor: workspace backbone + one file-tool family for the AI. replaces
# foldertree (webui filetree), file_manager, media_reader and the coder's file
# tools, so coder and file_manager can stay disabled.
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
#   anywhere are also accepted.
# - the tree routes use foldertree's ws index; the AI tools use coder-style
#   selectors (basename / full path / 0-based index).

import ast
import json
import os
import time
import hashlib
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


def _strip_sig_comment(signature: str):
    """drops a trailing # or // comment from a signature.

    the language pack bakes the comment sitting after a def line into the
    signature itself. quote-aware, so a # inside a quoted default
    (color="#fff") survives.
    """
    quote = None
    i = 0
    while i < len(signature):
        char = signature[i]
        if quote:
            if char == "\\":
                i += 2
                continue
            if char == quote:
                quote = None
        elif char in ("'", '"', "`"):
            quote = char
        elif char == "#" or (char == "/" and signature[i + 1:i + 2] == "/"):
            return signature[:i].rstrip()
        i += 1
    return signature


def _look_up_decorators(lines: list, def_index: int):
    """the decorator stack sitting directly above a definition, or None.

    the pack's spans start at the def/class keyword, so the
    decorated_definition wrapper (and every @decorator with it) is lost.
    walks upward tracking bracket depth, so a decorator wrapped across
    several lines is consumed as one chunk - a chunk only counts if it
    bottoms out at a line starting with @.
    """
    decorators = []
    chunk = []
    depth = 0
    i = def_index - 1
    while i >= 0:
        line = lines[i]
        stripped = line.strip()
        if depth == 0 and (not stripped or stripped.startswith("#")):
            i -= 1
            continue
        # counting upward: a line closing more brackets than it opens means
        # the statement continues on a line further above
        depth += line.count(")") + line.count("]") - line.count("(") - line.count("[")
        if depth < 0:
            depth = 0
        chunk.insert(0, _strip_sig_comment(stripped))
        if depth == 0:
            if chunk[0].startswith("@"):
                decorators.insert(0, " ".join(chunk))
            else:
                break
            chunk = []
        i -= 1
    if not decorators:
        return None
    joined = " ".join(decorators)
    return joined if len(joined) <= 120 else joined[:117] + "..."


def _short_import(source: str):
    """module name from an import statement: 'import os' -> 'os',
    'from pathlib import Path' -> 'pathlib', 'import glob as g' -> 'glob'.
    keeps the outline narrow."""
    text = " ".join(str(source or "").split())
    if text.startswith("import "):
        text = text[len("import "):]
    elif text.startswith("from "):
        text = text[len("from "):].split(" import ", 1)[0].strip()
    return text.split(" as ", 1)[0].strip()


# prose formats whose outline is a heading map instead of a symbol map
_PROSE_SUFFIXES = (".md", ".markdown", ".mdx", ".rst")

# config formats whose outline is a key map: json/yaml expose no definitions
# for the structure pass (it answers zero symbols for both), and the map needs
# no parser, so these also work when the tree-sitter runtime is missing
_CONFIG_SUFFIXES = (".json", ".yaml", ".yml")

# never collect more landmark rows than this, even with outline_max_keys = 0
_CONFIG_KEY_CEILING = 50000

# a key line in a yaml mapping (matched on the text after any "- "): a quoted
# key may hold a colon, a bare one stops at the first - yaml requires a space
# (or the line end) after it, so "a:1" stays a plain scalar and matches nothing
_YAML_KEY = regex.compile(
    r"""^(?P<key>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|[^\s#[{][^:]*?)\s*:(?:\s|$)""")

# "key": at the start of a pretty-printed json object line
_JSON_KEY = regex.compile(r'^\s*"(?P<key>(?:[^"\\]|\\.)*)"\s*:')

# json string literals, dropped before counting brackets: a "${VAR}" default
# or a "a{b" string must not shift the depth of the lines below it
_JSON_STRING = regex.compile(r'"(?:[^"\\]|\\.)*"')

# a block target that names lines instead of a symbol: "ln:12-40", "12-40", "ln:12"
_SPAN_TARGET = regex.compile(r"^(?:ln:)?\s*(\d+)\s*(?:-\s*(\d+))?$")


def _plain_heading(text):
    """comparison form of a heading: markdown emphasis/code markers dropped,
    closing #s removed, whitespace collapsed, lowercased.

    a heading written "## **Settings**" must be reachable as "Settings" -
    otherwise every formatted note needs its punctuation copied by hand.
    exact and prefix matching on the RAW text runs first, so a heading that
    genuinely contains underscores (a snake_case name) still matches exactly.
    """
    plain = " ".join(str(text or "").split()).strip("#").strip()
    for marker in ("*", "`", "~", "_"):
        plain = plain.replace(marker, "")
    return plain.lower()


def _span_target(target):
    """(start_line, end_line) from a line-range target, or None.

    1-based and inclusive; a bare number is a single line. this is the target
    form every text file understands, whether or not it has a grammar.
    """
    match = _SPAN_TARGET.match(str(target or "").strip())
    if not match:
        return None
    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else start
    return (start, end)


class Editor(EditorRoutes, PathResolver, core.module.Module):
    """Browse, preview, search and manage your workspace folders from the webui sidebar, with one consistent AI file-tool family. Replaces foldertree, file_manager and media_reader."""

    category = "development"

    dependencies = ["filetype", "regex", "tree-sitter", "tree-sitter-language-pack"]

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
            "description": "When on, file access is restricted to the configured workspaces. When off, absolute paths anywhere also work (the AI tools).",
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
        "outline_max_keys": {
            "type": "number",
            "default": 200,
            "description": "Maximum rows outline lists for json/yaml files: config keys, or one row per record in a json record list. 0 = unlimited, up to a 50000-row safety ceiling.",
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
                ["write", "append", "edit", "create", "replace_block", "insert_block",
                 "move", "copy", "delete", "mkdir"]
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
                lines.append("paths are workspace-relative; pass a workspace name, its path or its 0-based index, or 'name/subdir' to scope (see editor_workspaces)")
                lines.append("use editor_outline before editor_read: it maps definitions, headings, config keys or records with their positions")
                if self.config.get("read_outside_sandbox"):
                    lines.append("read-only tools may also use absolute paths outside the workspaces")
                if not self.config.get("sandboxed"):
                    lines.append("pass an empty workspace name to use absolute paths anywhere")
                parts.append("\n".join(lines))
        if self.config.get("read-only"):
            parts.append(
                "## IMPORTANT: Read-Only Mode\nYour editor module is in read-only mode and cannot modify files. Provide output to the user directly in your messages."
            )
        return "\n\n".join(parts) if parts else None

    # --------------------
    # AI tools (navigation, writing, the editing family, search)
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
            target, base = self._resolve_with_base(workspace, path, read_only=True)
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
            target, base = self._resolve_with_base(workspace, path, read_only=True)
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
            with self._open_target(target, "r", base) as f:
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
            target, base = self._resolve_with_base(workspace, path, read_only=True)
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
                with self._open_target(target, "rb", base) as f:
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

    @core.module.tool
    async def outline(self, workspace: str, path: str):
        """maps a file before reading it: definitions, headings, config keys or records, each with its position"""
        try:
            target, base = self._resolve_with_base(workspace, path, read_only=True)
        except ValueError as e:
            return self.result(str(e), False)
        if not target.exists():
            return self.result(f"'{target.name}' does not exist", False)
        if not target.is_file():
            return self.result(f"'{target.name}' is not a file", False)

        max_read_kb = self._num_setting("max_read_kb", 10240)
        if max_read_kb:
            try:
                if target.stat().st_size > max_read_kb * 1024:
                    return self.result({
                        "error": f"file is over max_read_kb ({max_read_kb} KB)",
                        "note": "raise max_read_kb, or use grep to search it instead",
                    }, False)
            except OSError:
                pass

        try:
            with self._open_target(target, "r", base) as f:
                code = f.read()
        except UnicodeDecodeError:
            return self.result("file is not valid utf-8 text - use info for metadata", False)
        except Exception as e:
            return self.result(str(e), False)

        # prose first: headings need no parser, so this works even when
        # tree-sitter is missing or the file's grammar has no definitions
        if target.suffix.lower() in _PROSE_SUFFIXES:
            sections = self._render_sections(code, rst=target.suffix.lower() == ".rst")
            if not sections:
                return self.result({
                    "error": "no headings found",
                    "note": "this file has no markdown/rst headings to map - use read",
                }, False)
            return self.result("\n".join(sections))

        # config files: a key map, also without a parser (json/yaml have no
        # definitions for the structure pass to find), so it must sit above the
        # HAVE_TSLP gate just like the prose branch
        if target.suffix.lower() in _CONFIG_SUFFIXES:
            limit = max(0, self._num_setting("outline_max_keys", 200))
            keys, capped = self._render_config_keys(code, target.suffix.lower(), limit)
            if not keys:
                return self.result({
                    "error": "no keys found",
                    "note": "this config file has no keys to map - use read",
                }, False)
            if capped:
                return self.result({
                    "content": "\n".join(keys),
                    "truncated": True,
                    # "entries" covers both reports: config keys and record rows
                    "truncation_note": (f"showing the first {len(keys)} entries - raise "
                                        "outline_max_keys for the rest (0 = unlimited)"),
                })
            return self.result("\n".join(keys))

        if not HAVE_TSLP:
            return self.result({
                "error": "this installation has no tree-sitter runtime, so files cannot be parsed",
                "note": "use read instead",
            }, False)

        structure = await self._extract_code_structure(code, target)
        if not structure:
            return self.result({
                "error": "no functions or classes found",
                "note": "this file has no definitions to map, or its language is not supported - use read",
            }, False)
        return self.result(self._render_compact(structure))

    # -------------------------------
    # helper: syntax gate (treesitter)
    # -------------------------------
    async def _syntax_gate(self, code, target):
        """return a list of syntax errors, or [] when skipped/clean.

        mirrors the coder: languages treesitter cannot check (unknown,
        vimdoc/plaintext, html) and ones its checker is too aggressive on
        (glsl) are never blocked; a failed parse is blocked for safety;
        diagnostics are reported with line/col.

        python also goes through CPython's own parser: treesitter's error
        recovery silently accepts unindented function/class bodies
        ("def f():\\nreturn 1"), so a file python itself rejects would
        otherwise land on disk.
        """
        if not self.config.get("syntax_check", True) or not HAVE_TSLP:
            return []
        lang = tslp.detect_language_from_path(str(target))
        if not lang or lang in ("vimdoc", "html", "htm", "glsl"):
            return []
        try:
            result = tslp.process(code, tslp.ProcessConfig(
                language=lang, diagnostics=True, structure=False))
        except Exception:
            return []
        if not result:
            return ["failed to parse the file for syntax errors - file was not written to disk"]
        errors = [
            f"line {d.span.start_line + 1}, col {d.span.start_column + 1}: {d.message}"
            for d in (result.diagnostics or [])
        ]
        if lang == "python":
            # the BOM must go first, or a valid file is rejected as an
            # illegal character; the parser itself flags the bad indentation
            try:
                ast.parse(code.lstrip("\ufeff"), filename=str(target))
            except SyntaxError as e:
                errors.append(f"line {e.lineno or 1}, col {e.offset or 1}: {e.msg}")
        return errors

    # ------------------------------------
    # helper: code structure (treesitter)
    # ------------------------------------
    async def _extract_code_structure(self, code, file_path):
        """the treesitter structure of a source file: symbols with their line
        spans, signatures and decorators, plus imports.

        returns None when there is no grammar, the parse fails or the file
        exposes no definitions, so the caller can tell the AI to read instead.
        """
        if not HAVE_TSLP:
            return None
        lang = tslp.detect_language_from_path(str(file_path))
        if not lang:
            return None
        try:
            extraction = tslp.process(code, tslp.ProcessConfig(
                language=lang,
                structure=True,
                imports=True,
                comments=False,
                docstrings=False,
                parse_timeout_ms=2000,
            ))
        except Exception:
            return None
        if not extraction:
            return None

        def flatten(items, prefix=""):
            flattened = []
            for item in items:
                full_name = f"{prefix}{item.name}" if item.name else prefix.rstrip(".")
                entry = {"name": full_name or "(anonymous)", "kind": str(item.kind)}
                if item.span:
                    entry["start_line"] = int(item.span.start_line) + 1
                    entry["end_line"] = int(item.span.end_line) + 1
                if item.signature:
                    entry["signature"] = " ".join(item.signature.split())
                flattened.append(entry)
                if item.children:
                    flattened.extend(flatten(item.children, prefix=f"{full_name}."))
            return flattened

        symbols = flatten(extraction.structure)
        if not symbols:
            return None

        # the pack bakes comments into signatures and its spans skip
        # decorators: clean both up so the outline shows what each symbol is
        code_lines = code.splitlines()
        for entry in symbols:
            if entry.get("signature"):
                cleaned = _strip_sig_comment(entry["signature"])
                if cleaned:
                    entry["signature"] = cleaned
                else:
                    del entry["signature"]
            if entry.get("start_line"):
                decorators = _look_up_decorators(code_lines, entry["start_line"] - 1)
                if decorators:
                    entry["decorators"] = decorators

        imports = []
        for imported in getattr(extraction, "imports", None) or []:
            name = _short_import(getattr(imported, "source", ""))
            if name and name not in imports:
                imports.append(name)

        return {"symbols": symbols, "imports": imports}

    async def _locate_symbol(self, code, file_path, target_symbol):
        """(start_line, end_line) of a definition named by a dotted path, or None.

        walks the structure tree one name at a time, so Alpha.run and
        Beta.run stay distinct - a plain text search would hit whichever came
        first. spans are 0-indexed and end-inclusive, excluding decorators.
        """
        if not HAVE_TSLP:
            return None
        lang = tslp.detect_language_from_path(str(file_path))
        if not lang:
            return None
        try:
            extraction = tslp.process(code, tslp.ProcessConfig(
                language=lang,
                structure=True,
                imports=False,
                exports=False,
                parse_timeout_ms=2000,
            ))
        except Exception:
            return None
        if not extraction:
            return None

        candidates = extraction.structure
        item = None
        for part in str(target_symbol).split("."):
            item = None
            for candidate in candidates:
                if candidate.name == part:
                    item = candidate
                    break
            if not item:
                return None
            candidates = item.children

        if not item or not item.span:
            return None
        return (int(item.span.start_line), int(item.span.end_line))

    def _render_compact(self, structure):
        """renders the symbol list as a compact text outline: one line per
        definition, indented by nesting depth, prefixed with its line range."""
        lines = []
        for symbol in structure["symbols"]:
            if "start_line" not in symbol:
                continue
            full_name = symbol["name"]
            display = symbol.get("signature") or full_name.rsplit(".", 1)[-1].lstrip(".")
            if not display:
                continue
            # broken grammar output: an anonymous single-line span adds nothing
            if full_name == "(anonymous)" and len(display.split()) <= 1 \
                    and symbol["end_line"] == symbol["start_line"]:
                continue
            depth = full_name.count(".") - (1 if full_name.startswith(".") else 0)
            prefix = ""
            if symbol.get("decorators"):
                prefix = symbol["decorators"] + " "
            # no separator before ln: at depth 0 - the indent IS the marker
            indent = "    " * max(0, depth)
            lines.append(f"{indent}ln:{symbol['start_line']}-{symbol['end_line']} {prefix}{display}")
        imports = structure.get("imports")
        if imports:
            lines.append("imports: " + " | ".join(imports))
        return "\n".join(lines)

    def _sections(self, code, rst=False):
        """structured heading list: [{line, level, text, raw}], 1-based lines.

        no parser needed. markdown uses ATX headings (#..###### - "#tag" without
        a space is not one), rst uses underline headings and takes its levels
        from the order the underline characters first appear. the underline rule
        is only applied to rst, or a markdown frontmatter "---" would read as a
        heading. fenced code blocks are skipped, so a # inside a sample is not a
        heading either. this is the single source both the outline and the
        heading locator read, so they can never disagree.
        """
        lines = code.split("\n")
        sections = []
        in_fence = False
        rst_styles = []
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("```") or stripped.startswith("~~~"):
                in_fence = not in_fence
                continue
            if in_fence or not stripped:
                continue
            if stripped.startswith("#"):
                level = len(stripped) - len(stripped.lstrip("#"))
                if 1 <= level <= 6 and (len(stripped) == level or stripped[level] == " "):
                    sections.append({
                        "line": i + 1,
                        "level": level,
                        "text": stripped[level:].strip(),
                        "raw": stripped,
                    })
                    continue
            if rst and i + 1 < len(lines):
                under = lines[i + 1].strip()
                if (len(under) >= 3 and len(set(under)) == 1
                        and under[0] in "=-~^*+\"'"
                        and len(under) >= len(stripped)):
                    if under[0] not in rst_styles:
                        rst_styles.append(under[0])
                    sections.append({
                        "line": i + 1,
                        "level": min(6, rst_styles.index(under[0]) + 1),
                        "text": stripped,
                        "raw": stripped,
                    })
        return sections

    def _render_sections(self, code, rst=False):
        """heading map text: `ln:<line> <heading>`, indented by level."""
        return [
            f"{'    ' * (max(1, s['level']) - 1)}ln:{s['line']} {s['raw']}"
            for s in self._sections(code, rst=rst)
        ]

    # ------------------------------------
    # helpers: config key map (json/yaml)
    # ------------------------------------
    def _yaml_key_rows(self, code, cap):
        """key rows of a yaml file from indentation alone: [(line, depth, key)].

        the stack holds (key column, key); a "- item:" line's column is where
        its text starts, so the next line's keys at the same column come out as
        siblings instead of children. blank, comment and document-marker lines
        are skipped, and the body of a block scalar ("run: |", "- |") is
        swallowed whole - otherwise every "key: value"-looking line inside a
        shell snippet would be mapped as config. anchors/aliases ("<<: *base")
        are not resolved: the key is emitted as written.

        stops after cap rows (cap = 0 means no cap).
        """
        rows = []
        stack = []
        block_indent = None
        for i, line in enumerate(code.split("\n")):
            stripped = line.strip()
            if block_indent is not None:
                column = len(line) - len(line.lstrip())
                if not stripped or column > block_indent:
                    continue  # still inside the block scalar's body
                block_indent = None
            if not stripped or stripped.startswith("#"):
                continue
            if stripped in ("---", "..."):
                if stripped == "---":
                    rows.append((i + 1, 0, "--- document ---"))
                stack.clear()  # a new document starts at depth 0
                continue
            if stripped.startswith("%"):
                continue  # directive (%YAML, %TAG)
            lead = len(line) - len(line.lstrip())
            body = line[lead:]
            column = lead
            if body.startswith("-") and (len(body) == 1 or body[1] == " "):
                tail = body[1:]
                # the item's mapping starts where its text starts, so a dash
                # that carries a key puts the column one step right of the dash
                column = lead + 1 + (len(tail) - len(tail.lstrip(" ")))
                body = tail.lstrip(" ")
                if not body:
                    continue  # a bare "-" carries no key of its own
                if body[0] in "|>":
                    block_indent = column
                    continue
            match = _YAML_KEY.match(body)
            if not match:
                continue
            key = match.group("key").strip()
            if key[:1] in ("'", '"'):
                key = key[1:-1]
            while stack and stack[-1][0] >= column:
                stack.pop()
            rows.append((i + 1, len(stack), key))
            stack.append((column, key))
            if body[match.end():].strip()[:1] in ("|", ">"):
                block_indent = column
            if cap and len(rows) >= cap:
                return rows
        return rows

    def _json_key_rows(self, code, cap):
        """key rows of a pretty-printed json file: [(line, depth, key)].

        depth is counted from the brackets opened so far (minus the root
        container, which is not a nesting level), not from the indentation, so
        2-space, 4-space and tab-indented files all map the same; string
        literals are dropped before counting, or a "${VAR}" default would shift
        it. a one-line document is never scanned - the caller sends it to
        _json_path_rows before this runs.
        """
        rows = []
        open_containers = 0
        for i, line in enumerate(code.split("\n")):
            bare = _JSON_STRING.sub('""', line)
            # a key may sit behind the bracket/comma that introduces it
            # ({ "e": 2 } inside an array), so strip those before matching
            match = _JSON_KEY.match(line.lstrip(" \t[{,"))
            if match:
                # the root container is not a nesting level: its keys are depth
                # 0, or every line of a normal json file would be indented once
                depth = max(0, open_containers - 1)
                rows.append((i + 1, depth, match.group("key")))
                if cap and len(rows) >= cap:
                    return rows
            open_containers += (bare.count("{") + bare.count("[")
                                - bare.count("}") - bare.count("]"))
            if open_containers < 0:
                open_containers = 0
        return rows

    def _json_path_rows(self, data, cap, max_depth=6):
        """key paths of a parsed json document: [(None, 0, "a.b[0].c")].

        the fallback for minified json, where a line number would be useless:
        every object key and every array element holding an object is listed by
        path, so "how many entries, where does this branch start" is answered
        without positions. max_depth keeps a pathological tree bounded.
        """
        rows = []

        def walk(node, path, depth):
            if isinstance(node, dict):
                for key, value in node.items():
                    if cap and len(rows) >= cap:
                        return
                    child = f"{path}.{key}" if path else str(key)
                    rows.append((None, 0, child))
                    if isinstance(value, (dict, list)) and depth < max_depth:
                        walk(value, child, depth + 1)
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    if not isinstance(value, (dict, list)):
                        continue
                    if cap and len(rows) >= cap:
                        return
                    child = f"{path}[{index}]"
                    rows.append((None, 0, child))
                    if depth < max_depth:
                        walk(value, child, depth + 1)

        walk(data, "", 0)
        return rows

    def _json_element_lines(self, code):
        """the 1-based line each element of a root-level json array starts on.

        walks the brackets and records a line only while the root array is the
        single open container, so a record's own nested lists are ignored.
        string literals are blanked first, like the depth count elsewhere.
        """
        starts = []
        depth = 0
        for i, line in enumerate(code.split("\n")):
            stripped = line.strip()
            if not stripped:
                continue
            if depth == 1 and stripped[0] in "{[":
                starts.append(i + 1)
            bare = _JSON_STRING.sub('""', stripped)
            depth += (bare.count("{") + bare.count("[")
                      - bare.count("}") - bare.count("]"))
            if depth < 0:
                depth = 0
        return starts

    def _looks_like_identifier(self, name):
        """True for field names that name a record instead of describing it.

        "tool_call_id" is the worst offender: it is longer than a short tool
        result, so 'first long string wins' printed call ids as the record's
        text. shape-based and short: id/uuid/hash/key suffixes.
        """
        name = str(name).lower()
        return (name == "id" or name.endswith(("_id", "_uuid", "uuid", "_hash", "_key"))
                or name in ("key", "hash"))

    def _record_summary(self, record):
        """one landmark line for a record, from shapes only (no field names).

        short scalars come first, because they identify the record ("role=user",
        "type=click", "timestamp=1794…"); the longest string that is not an
        identifier becomes '1234B "first words"' (ids lose to results, or a
        tool row showed its call id instead of the output); nested containers
        are reduced to a count ("2 tool_calls"). a schema dump would be the same
        for every element and so identify nothing - that was the first, useless
        version of this map.
        """
        labels = []
        best = None  # (length, rendered text): the longest non-id string wins
        nested = []
        for key, value in record.items():
            if isinstance(value, bool) or isinstance(value, (int, float)):
                labels.append(f"{key}={value}")
            elif isinstance(value, str):
                plain = " ".join(value.split())
                if not plain:
                    continue
                if len(plain) <= 24:
                    labels.append(f"{key}={plain}")
                elif not self._looks_like_identifier(key):
                    if best is None or len(plain) > best[0]:
                        cut = "\u2026" if len(plain) > 40 else ""
                        # no wrapping quotes: a json result brings its own, and
                        # '"{'status"…' reads as a typo
                        best = (len(plain), f"{len(value)}B {plain[:40]}{cut}")
            elif isinstance(value, list):
                if value:
                    nested.append(f"{len(value)} {key}")
            elif isinstance(value, dict):
                for inner_key, inner in value.items():
                    if isinstance(inner, bool) or isinstance(inner, (int, float)):
                        labels.append(f"{inner_key}={inner}")
                    elif isinstance(inner, str) and 0 < len(inner) <= 24 and "\n" not in inner:
                        labels.append(f"{inner_key}={inner}")
        parts = labels[:3]
        if best:
            parts.append(best[1])
        parts.extend(nested[:2])
        return " ".join(parts) or "(empty record)"

    def _json_record_rows(self, code, data, cap):
        """(rows, capped) or None - a table of contents for a root array of objects.

        a homogeneous record list has no keys worth mapping: every element
        repeats them, so the map degenerated into N copies of one schema and
        a 4.9 MB chat log produced 200 rows that all said "role". the landmark
        is the record, so each row is its line plus a summary, and the first row
        is the collection itself (count and the union of its fields).

        returns None when the element lines cannot be lined up with the parsed
        elements (a one-line document, or an element starting inline), so the
        caller can fall back to the key map instead of printing wrong lines.
        """
        starts = self._json_element_lines(code)
        if len(starts) != len(data):
            return None
        fields = []
        for record in data:
            for key in record:
                if key not in fields:
                    fields.append(key)
        plural = "" if len(data) == 1 else "s"
        rows = [(None, 0, f"{len(data)} record{plural} \u00b7 fields: {', '.join(fields)}")]
        for line, record in zip(starts, data):
            rows.append((line, 0, self._record_summary(record)))
            if cap and len(rows) >= cap:
                break
        return rows

    def _config_key_rows(self, code, suffix, limit):
        """(rows, capped) - the landmark rows of a config file, capped at limit.

        two json shapes, two reports: an object tree gets a key map, a
        root-level array gets a record table of contents (see
        _json_record_rows). limit = 0 means no limit (the ceiling still
        applies). one row past the limit is collected first, so a file with
        exactly `limit` rows is not reported as truncated. invalid json (or a
        yaml file without a single key) returns no rows, which the caller
        reports as an error the model can act on.
        """
        want = limit or _CONFIG_KEY_CEILING
        cap = want + 1
        if suffix == ".json":
            # a root-level array is not an object tree: its keys repeat per
            # element, so a key map would say nothing about where anything is
            if code.lstrip("\ufeff \t\r\n").startswith("["):
                try:
                    data = json.loads(code.lstrip("\ufeff"))
                except Exception:
                    data = None
                if isinstance(data, list) and data:
                    if all(isinstance(record, dict) for record in data):
                        rows = self._json_record_rows(code, data, cap)
                        if rows is not None:
                            return rows[:want], len(rows) > want
                        # can't align element lines (a one-line document): the
                        # path walk below still names the keys, so fall through
                    else:
                        kinds = ", ".join(sorted({type(value).__name__ for value in data}))
                        plural = "" if len(data) == 1 else "s"
                        return [(None, 0, f"{len(data)} item{plural} ({kinds})")], False
            # a minified document has no per-key line to point at, so it goes
            # straight to the path walk - the line scan would map only the keys
            # that happen to share a line with the root bracket
            rows = [] if "\n" not in code.strip() else self._json_key_rows(code, cap)
            if not rows:
                try:
                    data = json.loads(code.lstrip("\ufeff"))
                except Exception:
                    return [], False
                rows = self._json_path_rows(data, cap)
        else:
            rows = self._yaml_key_rows(code, cap)
        return rows[:want], len(rows) > want

    def _render_config_keys(self, code, suffix, limit):
        """the config key map as (text lines, capped).

        same shape as the other backends - `ln:<line> <key>`, indented two
        spaces per nesting level (yaml's own step; the heading map's four would
        push a deep config's line numbers off the screen). a minified file has
        no line to point at, so its rows are bare key paths with no indent.
        """
        rows, capped = self._config_key_rows(code, suffix, limit)
        lines = [
            ("  " * min(depth, 12)) + (text if line is None else f"ln:{line} {text}")
            for line, depth, text in rows
        ]
        return lines, capped

    def _match_section(self, sections, target):
        """(index, error_text) for a heading target.

        accepts the heading text with or without its #s, case-insensitively:
        exact match first, then a unique prefix. returns an error string when no
        heading matches or the text is ambiguous (two "## Duplicate"s).
        """
        wanted = " ".join(str(target or "").split()).lstrip("#").strip().lower()
        if not wanted:
            return None, "no heading name was given"

        def pick(candidates, label):
            if len(candidates) == 1:
                return candidates[0], None
            if len(candidates) > 1:
                return None, self._ambiguous_sections([sections[i] for i in candidates], label)
            return None, None

        # raw text first (so snake_case headings match exactly), then the
        # formatting-blind form, so "Settings" reaches "## **Settings**".
        # within each form: exact before prefix
        for raw in (True, False):
            key = (lambda text: text.lower()) if raw else _plain_heading
            value = wanted if raw else _plain_heading(wanted)
            index, error = pick(
                [i for i, s in enumerate(sections) if key(s["text"]) == value], target)
            if index is not None or error:
                return index, error
            index, error = pick(
                [i for i, s in enumerate(sections) if key(s["text"]).startswith(value)], target)
            if index is not None or error:
                return index, error

        return None, f"heading '{target}' was not found in this file - use outline to see the headings"

    def _ambiguous_sections(self, matches, wanted):
        """error text listing the headings a target text collided with."""
        listed = ", ".join(f"line {s['line']}: {s['raw']}" for s in matches[:8])
        return (f"heading '{wanted}' matches {len(matches)} headings: {listed} - "
                f"pass more of the heading text to pick one")

    def _section_end(self, sections, index, total):
        """1-based last line of a section: the line before the next heading of
        the same or a higher level, or the last line of the file."""
        level = sections[index]["level"]
        for later in sections[index + 1:]:
            if later["level"] <= level:
                return later["line"] - 1
        return total

    def _line_indent(self, lines, line_idx):
        """the leading whitespace of a 0-indexed line."""
        line = lines[line_idx]
        return line[: len(line) - len(line.lstrip())]

    def _reindent_block(self, content, target_indent):
        """re-indents a block to a target indentation, keeping its own nesting.

        only the block's COMMON leading indent is stripped, then the target
        indent is applied. stripping every line's own indent instead (what the
        pre-rewrite coder did) flattens nested blocks and writes invalid code.
        leading and trailing blank lines are trimmed for predictable spacing.
        """
        content_lines = str(content or "").split("\n")

        block_indent = None
        for line in content_lines:
            if line.strip():
                indent = line[: len(line) - len(line.lstrip())]
                if block_indent is None:
                    block_indent = indent
                else:
                    block_indent = os.path.commonprefix([block_indent, indent])
        if block_indent is None:
            return []

        reindented = []
        for line in content_lines:
            if not line.strip():
                reindented.append("")
            else:
                reindented.append(target_indent + line[len(block_indent):])

        while reindented and not reindented[0]:
            reindented.pop(0)
        while reindented and not reindented[-1]:
            reindented.pop()
        return reindented

    # ------------------------------------------
    # helpers: block-edit snapshots (card diff)
    # ------------------------------------------
    _BLOCK_STASH_DAYS = 7

    def _chat_id(self):
        """the current chat's id, or None when there is no chat (tests, startup)."""
        try:
            chat = self.channel.context.chat
            return str(chat.data[chat.current]["id"])
        except Exception:
            return None

    def _stash_block(self, target_path, target, start, end, old_text, new_text):
        """remember what a block edit replaced, for the card's before/after view.

        one file per CALL, named by a fresh token that the result then carries
        (`… in f.py · #7f3a2c`): a tool function never sees its call id (core
        invokes it with the schema arguments only), so the token is the only way
        to give every call its own snapshot - keying by path/target/span instead
        made repeated edits share one entry, and an old card could then show a
        newer "before".

        display-only: the file lives under the data folder and the card fetches
        it through a module route, so the replaced text never enters the tool
        result and cannot cost context. failures are swallowed (- the edit must
        never fail because a snapshot could not be written) and callers only
        append the token when one comes back. snapshots are pruned by age at
        write time, because nothing ties these files to a chat's deletion.
        """
        chat_id = self._chat_id()
        if not chat_id or not old_text:
            return ""
        try:
            folder = Path(core.get_data_path(os.path.join("editor", "blocks", chat_id)))
            folder.mkdir(parents=True, exist_ok=True)
            token = hashlib.sha1(os.urandom(16)).hexdigest()[:12]
            entry = {
                "ts": int(time.time()),
                "path": str(target_path),
                "target": str(target),
                "start": start,
                "end": end,
                "old": old_text,
                "new": new_text,
            }
            (folder / (token + ".json")).write_text(json.dumps(entry), encoding="utf-8")

            cutoff = time.time() - self._BLOCK_STASH_DAYS * 86400
            for old_file in folder.iterdir():
                try:
                    if old_file.suffix == ".json" and old_file.stat().st_mtime < cutoff:
                        old_file.unlink()
                except OSError:
                    pass
            return token
        except Exception:
            return ""

    def _decorator_start(self, lines, def_idx):
        """index of the first line of the decorator stack above a def/class
        line, or def_idx when there is none.

        the structure span starts at the def/class keyword, so inserting
        "before" a decorated symbol without this would slip between the
        decorators and their definition. brackets are counted upward:
        a closing paren means the decorator statement continues further up.
        """
        i = def_idx - 1
        balance = 0
        while i >= 0:
            line = lines[i]
            stripped = line.strip()
            balance += line.count("(") - line.count(")")
            if balance == 0:
                if stripped.startswith("@"):
                    i -= 1
                    continue
                break
            if balance > 0 or not stripped:
                break
            i -= 1
        return i + 1

    # ----------------------
    # tools: writing
    # ----------------------
    @core.module.tool
    async def write(self, workspace: str, path: str, content: str):
        """writes content to a file, overwriting it. auto-creates parent folders. no syntax check (raw) - use create/edit for gated writes"""
        try:
            target, base = self._resolve_with_base(workspace, path)
        except ValueError as e:
            return self.result(str(e), False)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with self._open_target(target, "w", base) as f:
                f.write(content)
        except Exception as e:
            return self.result(str(e), False)
        return self.result(f"wrote {path}")

    @core.module.tool
    async def append(self, workspace: str, path: str, content: str):
        """appends content to a file, creating it if missing. no syntax check (raw)"""
        try:
            target, base = self._resolve_with_base(workspace, path)
        except ValueError as e:
            return self.result(str(e), False)
        if target.exists():
            try:
                with self._open_target(target, "r", base) as f:
                    f.read()
            except UnicodeDecodeError:
                return self.result("file is not valid utf-8 text - cannot append to it", False)
            except Exception as e:
                return self.result(str(e), False)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with self._open_target(target, "a", base) as f:
                f.write(content)
        except Exception as e:
            return self.result(str(e), False)
        return self.result(f"appended to {path}")

    @core.module.tool
    async def create(self, workspace: str, path: str, content: str):
        """creates a new file, refusing to overwrite existing ones. auto-creates parent folders. syntax-checked"""
        try:
            target, base = self._resolve_with_base(workspace, path)
        except ValueError as e:
            return self.result(str(e), False)
        errors = await self._syntax_gate(content, target)
        if errors:
            return self.result({"errors": errors, "message": "syntax check failed - file was not written"}, False)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with self._open_target(target, "x", base) as f:
                f.write(content)
        except FileExistsError:
            return self.result(f"'{path}' already exists - use write to overwrite or edit to modify", False)
        except Exception as e:
            return self.result(str(e), False)
        return self.result(f"created {path}")

    @core.module.tool
    async def edit(self, workspace: str, path: str, original_code: str, replacement_code: str):
        """replaces original_code with replacement_code in a file; fails if nothing matched. for a small exact snippet - to replace a whole definition or section use replace_block instead. syntax-checked"""
        try:
            target, base = self._resolve_with_base(workspace, path)
        except ValueError as e:
            return self.result(str(e), False)
        try:
            with self._open_target(target, "r", base) as f:
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
            with self._open_target(target, "w", base) as f:
                f.write(new_content)
        except Exception as e:
            return self.result(str(e), False)
        return self.result(f"edited {path}")

    @core.module.tool
    async def replace_block(self, workspace: str, path: str, target: str, content: str):
        """replaces a whole block: a definition ('Class.method'), a markdown/rst heading ('Settings') or exact lines ('ln:12-40'). prefer it over edit for whole definitions/sections: re-indented, and the merged file is syntax-checked"""
        try:
            target_path, base = self._resolve_with_base(workspace, path)
        except ValueError as e:
            return self.result(str(e), False)
        if not target_path.is_file():
            return self.result(f"'{target_path.name}' is not a file", False)

        try:
            with self._open_target(target_path, "r", base) as f:
                code = f.read()
        except UnicodeDecodeError:
            return self.result("file is not valid utf-8 text - cannot edit it", False)
        except Exception as e:
            return self.result(str(e), False)
        if not code:
            return self.result("file is empty", False)

        lines = code.split("\n")
        total = len(lines) - 1 if lines and lines[-1] == "" else len(lines)

        prose = target_path.suffix.lower() in _PROSE_SUFFIXES
        lead_blank = False
        span = _span_target(target)
        if span:
            start, end = span
            if start < 1 or end < start or end > total:
                return self.result({
                    "error": f"line range {start}-{end} does not fit this {total}-line file",
                    "note": "use outline or read to find the right range",
                }, False)
            start_idx, end_idx = start - 1, end - 1
            label = f"lines {start}-{end}"
        elif prose:
            sections = self._sections(code, rst=target_path.suffix.lower() == ".rst")
            index, error = self._match_section(sections, target)
            if error:
                return self.result({"error": error, "note": "use outline to see the headings"}, False)
            section = sections[index]
            section_end = self._section_end(sections, index, total)
            # replace the BODY, keeping the heading: the heading is what names
            # the section, so re-emitting it is where headings get lost
            start_idx = section["line"]                     # first line after the heading
            end_idx = max(start_idx - 1, section_end - 1)   # an empty section replaces nothing
            lead_blank = True                               # keep one blank line under the heading
            label = f"section '{section['raw']}'"
        else:
            if not HAVE_TSLP:
                return self.result({
                    "error": "this installation has no tree-sitter runtime, so symbols cannot be located",
                    "note": "pass a line range instead, e.g. 'ln:12-40' (use outline or read to find it)",
                }, False)
            located = await self._locate_symbol(code, target_path, target)
            if not located:
                return self.result({
                    "error": f"symbol '{target}' was not found in {path}",
                    "note": "names are dotted, e.g. 'Class.method'; use outline to see what is available, or pass a line range like 'ln:12-40'",
                }, False)
            start_idx, end_idx = located
            label = f"'{target}'"

        new_lines = self._reindent_block(content, self._line_indent(lines, start_idx))
        if not new_lines:
            return self.result("replacement content was empty - pass the complete new block", False)
        if lead_blank:
            # a section body needs a blank line under its heading and before
            # whatever heading follows it
            new_lines = [""] + new_lines + [""]

        merged = "\n".join(lines[:start_idx] + new_lines + lines[end_idx + 1:])
        if not prose:
            errors = await self._syntax_gate(merged, target_path)
            if errors:
                return self.result({
                    "errors": errors,
                    "message": "syntax check failed - file was not written",
                }, False)
        # what the card may show side by side later; captured before the write
        old_block = "\n".join(lines[start_idx:end_idx + 1]) if end_idx >= start_idx else ""
        new_block = "\n".join(new_lines)
        try:
            with self._open_target(target_path, "w", base) as f:
                f.write(merged)
        except Exception as e:
            return self.result(str(e), False)
        # display-only snapshot, and only now that the write landed
        token = self._stash_block(target_path, target, start_idx + 1, end_idx + 1,
                                  old_block, new_block)
        # the token is the only trace of the snapshot in the result: a dozen
        # characters the card needs to fetch the old text, nothing else
        ref = f" \u00b7 #{token}" if token else ""
        if span:
            return self.result(f"replaced {label} in {path}{ref}")
        if prose:
            if end_idx < start_idx:
                return self.result(
                    f"{label} had no body - inserted the content after the heading in {path}{ref}")
            return self.result(
                f"replaced the body of {label} (was lines {start_idx + 1}-{end_idx + 1}) in {path}{ref}")
        return self.result(
            f"replaced {label} (was lines {start_idx + 1}-{end_idx + 1}) in {path}{ref}")

    @core.module.tool
    async def insert_block(self, workspace: str, path: str, target: str, content: str, where: str = "before"):
        """inserts content before/after a definition ('Class.method'), a markdown/rst heading ('Settings') or a line ('ln:12'); it adds a block rather than rewriting one - use edit to change existing text, append for end-of-file. re-indented, syntax-checked"""
        if str(where).strip().lower() not in ("before", "after"):
            return self.result(f"'where' must be 'before' or 'after', not '{where}'", False)
        before = str(where).strip().lower() == "before"

        try:
            target_path, base = self._resolve_with_base(workspace, path)
        except ValueError as e:
            return self.result(str(e), False)
        if not target_path.is_file():
            return self.result(f"'{target_path.name}' is not a file", False)

        try:
            with self._open_target(target_path, "r", base) as f:
                code = f.read()
        except UnicodeDecodeError:
            return self.result("file is not valid utf-8 text - cannot edit it", False)
        except Exception as e:
            return self.result(str(e), False)

        lines = code.split("\n")
        total = len(lines) - 1 if lines and lines[-1] == "" else len(lines)

        prose = target_path.suffix.lower() in _PROSE_SUFFIXES
        span = _span_target(target)
        if span:
            start, end = span
            if start < 1 or end < start or end > total:
                return self.result({
                    "error": f"line range {start}-{end} does not fit this {total}-line file",
                    "note": "use outline or read to find the right line",
                }, False)
            indent = self._line_indent(lines, start - 1)
            insert_idx = start - 1 if before else end
            label = f"line {start}" if start == end else f"lines {start}-{end}"
        elif prose:
            sections = self._sections(code, rst=target_path.suffix.lower() == ".rst")
            index, error = self._match_section(sections, target)
            if error:
                return self.result({"error": error, "note": "use outline to see the headings"}, False)
            section = sections[index]
            section_end = self._section_end(sections, index, total)
            indent = self._line_indent(lines, section["line"] - 1)
            # "after" means after the whole SECTION, matching "after" a symbol
            insert_idx = section["line"] - 1 if before else section_end
            label = f"section '{section['raw']}'"
        else:
            if not HAVE_TSLP:
                return self.result({
                    "error": "this installation has no tree-sitter runtime, so symbols cannot be located",
                    "note": "pass a line instead, e.g. 'ln:12' (use outline or read to find it)",
                }, False)
            located = await self._locate_symbol(code, target_path, target)
            if not located:
                return self.result({
                    "error": f"symbol '{target}' was not found in {path}",
                    "note": "names are dotted, e.g. 'Class.method'; use outline to see what is available, or pass a line like 'ln:12'",
                }, False)
            start_idx, end_idx = located
            indent = self._line_indent(lines, start_idx)
            insert_idx = self._decorator_start(lines, start_idx) if before else end_idx + 1
            label = f"'{target}'"

        reindented = self._reindent_block(content, indent)
        if not reindented:
            return self.result("content was empty - pass the block to insert", False)
        if span:
            # arbitrary text: nothing to infer, insert exactly what was passed
            block = reindented
        else:
            # a block landing between two neighbours needs a blank line on each
            # side that does not already have one - adding unconditionally
            # doubled an existing blank on one side and left the other glued
            needs_before = insert_idx > 0 and lines[insert_idx - 1].strip() != ""
            needs_after = insert_idx < len(lines) and lines[insert_idx].strip() != ""
            block = ([""] if needs_before else []) + reindented + ([""] if needs_after else [])

        merged = "\n".join(lines[:insert_idx] + block + lines[insert_idx:])
        if not prose:
            errors = await self._syntax_gate(merged, target_path)
            if errors:
                return self.result({
                    "errors": errors,
                    "message": "syntax check failed - file was not written",
                }, False)
        try:
            with self._open_target(target_path, "w", base) as f:
                f.write(merged)
        except Exception as e:
            return self.result(str(e), False)
        direction = "before" if before else "after"
        return self.result(f"inserted {len(reindented)} line(s) {direction} {label} in {path}")

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
                with self._nofollow_open(filepath) as f:
                    all_lines = f.readlines()
            except (UnicodeDecodeError, OSError, ValueError):
                # unreadable or symlinked file: skip it, like an oversized one
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
    async def _attach_media(self, meta, hint, base=None):
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
        with self._open_target(meta["path"], "rb", base) as f:
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
            target, base = self._resolve_with_base(workspace, path, read_only=True)
        except ValueError as e:
            return self.result(str(e), False)
        if not target.exists():
            return self.result("path does not exist", False)
        if not target.is_file():
            return self.result("path is not a file", False)

        try:
            size = target.stat().st_size
            with self._open_target(target, "rb", base) as f:
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
                meta, "Image is attached; you will see it in the next message.", base)

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

            case "editor_replace_block" | "editor_insert_block":
                # the card and the collapsed step row both read this slot; the
                # full result string ("replaced 'greet' (was lines 4-7) in …")
                # is already in the card's note, so the hint stays short
                if not isinstance(response, dict) or response.get("status") != "success":
                    return ""
                content = args.get("content")
                if not isinstance(content, str) or not content:
                    return ""
                text = content[:-1] if content.endswith("\n") else content
                n = max(1, len(text.split("\n")))
                label = f"{n} line" + ("" if n == 1 else "s")
                target = str(args.get("target") or "").strip()
                if name == "editor_insert_block":
                    where = "after" if args.get("where") == "after" else "before"
                    return f"inserted {label} {where} {target}".replace("  ", " ").strip()
                return f"replaced {target} \u00b7 {label}".replace("  ", " ").strip()

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

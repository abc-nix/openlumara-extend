# editor: path + workspace resolution machinery.
#
# -- AI GENERATED CODE (deepseek-v4-flash) :: 2026-10-16
# Mixin for the Editor class (see module.py). Holds every path rule so the
# security contract lives in one offline-testable file:
#
# - relative paths ALWAYS resolve against a workspace root, never against
#   the openlumara root (that was the file_manager bug).
# - sandboxed ON: tools can only touch files inside configured workspaces.
# - sandboxed OFF: workspaces still work for relative paths; absolute paths
#   anywhere are also accepted (AI tools).
# - tree routes use the foldertree `ws` index; AI tools use coder-style
#   selectors (basename / full path / 0-based index).
# - escape protection (foldertree `_resolve`): expanduser -> absolutize ->
#   resolve symlinks -> containment check. Covers ../ traversal, absolute
#   paths and symlink escapes in one check. Non-strict: missing paths pass
#   resolution and are rejected per-call instead.

import os
from pathlib import Path

import core


class PathResolver:
    # --------------------
    # settings helpers
    # --------------------
    def _num_setting(self, key, fallback):
        """read a numeric setting defensively (config values can be floats/strings)"""
        try:
            return int(float(self.config.get(key)))
        except (TypeError, ValueError):
            return fallback

    def _list_setting(self, key, fallback, lower=True, source=None):
        """read a list setting defensively (tolerates a comma string in config).

        lower=True lowercases entries (extension lists, which are compared
        case-insensitively). Use lower=False for case-sensitive exact names
        (skip_dirs: folder names must match the filesystem spelling).
        source: config object to read from (defaults to this module's own
        config; used to read the coder module's settings).
        """
        raw = (source or self.config).get(key)
        if raw is None:
            return list(fallback)
        if isinstance(raw, str):
            raw = raw.split(",")
        out = [str(s).strip() for s in raw if str(s).strip()]
        return [s.lower() for s in out] if lower else out

    # --------------------
    # workspaces
    # --------------------
    def _resolve_workspace(self, raw):
        """resolve a configured folder path (manual workspace or coder sandbox)"""
        raw = str(raw or "").strip() or "."
        p = Path(os.path.expanduser(raw))
        if not p.is_absolute():
            p = Path(core.get_path(str(p)))
        return p.resolve()

    def _coder(self):
        """the coder module instance, or None when it is disabled/not loaded"""
        manager = getattr(self, "manager", None)
        if manager is None:
            return None
        return (getattr(manager, "modules", None) or {}).get("coder")

    def _workspace_info(self):
        """(roots, inherited): the folders the panel may show and whether
        they actually come from the coder module.

        inherit_from_coder -> one root per coder sandbox (in coder's order),
        but only while coder yields at least one resolvable sandbox; any
        fallback (coder off, no sandboxes, unreadable entries) returns the
        manual `workspaces` list with inherited=False.
        """
        coder = self._coder() if self.config.get("inherit_from_coder") else None
        if coder:
            roots = []
            for raw in (coder.config.get("sandbox_paths") or []):
                try:
                    roots.append(self._resolve_workspace(raw))
                except Exception:
                    continue  # unreadable sandbox entry: skip it
            if roots:
                return roots, True
        raw_list = self.config.get("workspaces")
        if not raw_list:
            # no workspaces configured: default to the openlumara main folder
            # (same as file_manager's relative-path root)
            raw_list = ["."]
        return [self._resolve_workspace(raw) for raw in raw_list], False

    def _inherit_active(self):
        """inherit_from_coder is on AND coder actually provides the roots"""
        return self._workspace_info()[1]

    def _workspace_roots(self):
        """absolute folders the editor may access, in order"""
        return self._workspace_info()[0]

    def _pick_root(self, source):
        """choose the workspace root for a request via its `ws` index.

        returns (Path, None) or (None, error_message). the client may only
        select among the configured roots; the index is validated here.
        """
        roots = self._workspace_roots()
        try:
            idx = int(str((source or {}).get("ws", 0) or 0))
        except (TypeError, ValueError):
            idx = 0
        if idx < 0 or idx >= len(roots):
            return None, f"workspace index out of range (0-{len(roots) - 1})"
        return roots[idx], None

    def _skip_dirs(self):
        """folder names hidden from the view and skipped by search.

        inherit_from_coder -> the coder module's folder_blacklist; otherwise
        the `skip_dirs` setting. Exact-name match either way.
        """
        if self._inherit_active():
            return self._list_setting("folder_blacklist", [], lower=False,
                                      source=self._coder().config)
        return self._list_setting("skip_dirs", [], lower=False)

    # --------------------
    # the security funnel
    # --------------------
    def _resolve(self, raw, root):
        """map a client-supplied path onto the workspace, rejecting escapes.

        "" (empty) means the workspace root itself. The path is expanded,
        absolutized, then symlinks are resolved - and the result must still
        live inside the workspace. That single check covers ../ traversal,
        absolute paths and symlink escapes alike.

        Resolution is non-strict (Path.resolve() does not require the path
        to exist): a missing path passes this check and is rejected by each
        call's own existence check instead.
        """
        raw = str(raw or "").strip()
        p = Path(os.path.expanduser(raw)) if raw else root
        if not p.is_absolute():
            p = root / p
        p = p.resolve()
        if p != root and root not in p.parents:
            raise ValueError("path escapes the workspace folder")
        return p

    def _target(self, source, root):
        """resolve a client-supplied path (query or body dict) onto the workspace.

        returns (Path, None) on success or (None, error_message) on escape.
        """
        try:
            return self._resolve(str((source or {}).get("path", "")), root), None
        except ValueError as e:
            return None, str(e)

    # --------------------
    # tool-facing selectors (coder-style: basename / full path / index)
    # --------------------
    def _workspace_root(self, sel):
        """resolve a workspace selector (basename, full path or 0-based index)"""
        roots = self._workspace_roots()
        if not roots:
            raise ValueError("no workspaces configured (see the workspaces setting)")
        sel = str(sel or "").strip()
        if sel.isdigit():
            idx = int(sel)
            if 0 <= idx < len(roots):
                return roots[idx]
            raise ValueError(f"workspace index out of range (0-{len(roots) - 1})")
        for r in roots:
            if (r.name or str(r)) == sel or str(r) == sel:
                return r
        available = ", ".join(r.name or str(r) for r in roots)
        raise ValueError(f"unknown workspace '{sel}'. available: {available}")

    def _scope(self, sel):
        """resolve a workspace selector into (root, subfolder scope).

        accepts everything _workspace_root accepts (index / basename / full
        path) plus a subfolder scope written '<workspace>/<sub>[/...]', e.g.
        'git/openlumara'. split at the FIRST separator, so a scope may be
        several levels deep ('git/openlumara/user_modules'). root is always
        the configured workspace - that is what keeps containment and the
        workspace-root protection unchanged - and the scope only narrows the
        anchor. scope is '' for a plain selector.

        resolution is non-strict, like every other path here: the scope
        folder does not have to exist yet (mkdir/create rely on that).
        raises ValueError when the lead is not a configured workspace; the
        message keeps _workspace_root's text and adds the scope syntax.
        """
        sel = str(sel or "").strip()
        try:
            return self._workspace_root(sel), ""
        except ValueError as e:
            plain_error = e
        norm = sel.replace("\\", "/")
        head, _, rest = norm.partition("/")
        rest = rest.strip("/\\")
        if head and rest:
            try:
                return self._workspace_root(head), rest
            except ValueError:
                pass
        raise ValueError(
            f"{plain_error}. to scope into a subfolder, prefix a workspace "
            f"name: '<workspace>/<subfolder>' (e.g. 'git/openlumara')")

    def _resolve_path(self, workspace, path, read_only=False):
        """(workspace, path) -> absolute Path for the AI tools.

        sandboxed: workspace required, path resolved inside it. not
        sandboxed: empty workspace = absolute path anywhere, OR - when
        exactly ONE workspace is configured (option A, user decision
        2026-10-17) - a relative path resolves against that sole workspace
        (containment still enforced). 0 or 2+ workspaces with a relative
        path = error: there is no unambiguous anchor. raises ValueError on
        escapes / unknown workspace / invalid mode.

        workspace may also be a subfolder scope, '<workspace>/<sub>[/...]'
        (see _scope): the scope becomes the anchor AND the containment
        boundary, so '..' cannot step out of it; the configured workspace is
        still the outer bound (a scope may never resolve outside it).

        read_only=True (read-only tools only): in sandboxed mode, an
        absolute path outside the workspaces is still allowed when the
        read_outside_sandbox setting is on. Writes are never exempt
        (read_only defaults False). Named workspaces keep full containment.
        """
        ws = str(workspace or "").strip()
        raw = str(path or "").strip()
        if not ws:
            if self.config.get("sandboxed"):
                if read_only and self.config.get("read_outside_sandbox"):
                    p = Path(os.path.expanduser(raw))
                    if p.is_absolute():
                        return p.resolve()
                raise ValueError("workspace is required when sandboxed mode is on (see workspaces)")
            p = Path(os.path.expanduser(raw))
            if not p.is_absolute():
                roots = self._workspace_roots()
                if len(roots) == 1:
                    # option A: a single workspace is an unambiguous anchor
                    p = (roots[0] / p).resolve()
                    if p != roots[0] and roots[0] not in p.parents:
                        raise ValueError("path escapes the workspace folder")
                    return p
                raise ValueError(
                    "path must be absolute when no workspace is given "
                    "(or name one of the configured workspaces)")
            return p.resolve()
        root, scope = self._scope(ws)
        if scope:
            boundary = (root / scope).resolve()
            if boundary != root and root not in boundary.parents:
                raise ValueError(
                    f"workspace scope escapes the workspace folder: "
                    f"'{root.name}/{scope}'")
        else:
            boundary = root
        if raw:
            p = Path(os.path.expanduser(raw))
            if not p.is_absolute():
                p = boundary / p
        else:
            p = boundary
        p = p.resolve()
        if p != boundary and boundary not in p.parents:
            if scope:
                raise ValueError(
                    f"path escapes the workspace scope '{root.name}/{scope}'")
            raise ValueError("path escapes the workspace folder")
        return p

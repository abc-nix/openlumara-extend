"""libreta — framework-free file layer for the user-facing text surface.

Deliberately has no `core` imports:
  * the tests load this file directly (no OpenLumara boot, no stubs),
  * the rules about what a workspace may touch live in one auditable place.

P0a is read-only. The write path (P0b) lands here too, next to the mtime
conflict guard, so the UI and the AI tools obey the same rules.

Path contract:
  * every path handled here is WORKSPACE-RELATIVE ('Work/note.md'),
  * '..' and absolute paths are refused,
  * symlinks are resolved before the containment check, so a link inside a
    workspace cannot point elsewhere; symlinks found while listing are skipped.
"""

import os
import re
import time


PROSE_EXTS = (".md", ".markdown", ".mdx")

# files are "deleted" by moving them here (inside the workspace, dot-prefixed so
# the tree hides it). a notes app must never make a delete unrecoverable.
TRASH_DIR = ".libreta-trash"

# zero-width space: CodeMirror and our own line renderer put it inside EMPTY
# lines as a caret placeholder, and it travels with copied text. It is invisible
# in the editor but turns blank lines into "strange symbols" once saved, so it is
# stripped on write.
_ZWSP = "\u200b"


def clean_content(content) -> str:
    return str(content).replace(_ZWSP, "")

# markdown line prefixes that add no meaning to a one-line snippet
_SNIPPET_PREFIX = re.compile(
    r"^\s{0,3}(?:#{1,6}\s+|>\s?|(?:[-*+]|\d{1,3}[.)])\s+)"
)
_SNIPPET_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_SNIPPET_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")


class FileError(Exception):
    """a request that cannot be served (bad path, missing file, binary, too big)."""


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------
def join_rel(base: str, name: str) -> str:
    """'Work' + 'note.md' -> 'Work/note.md'."""
    base = str(base or "").strip("/")
    return f"{base}/{name}" if base else str(name)


def parent_of(rel: str) -> str:
    rel = str(rel or "").strip("/")
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def safe_rel(rel) -> str:
    """normalise a workspace-relative path, refusing absolute paths and '..'."""
    raw = str(rel or "").replace("\\", "/").strip()
    if "\x00" in raw:
        raise FileError("invalid path")
    if raw.startswith("/"):
        raise FileError("absolute paths are not allowed here")

    parts = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise FileError("path escapes the workspace")
        parts.append(part)
    return "/".join(parts)


def resolve(root, rel=""):
    """absolute path for `rel` inside `root`; FileError when it escapes."""
    root_real = os.path.realpath(str(root))
    if not os.path.isdir(root_real):
        raise FileError(f"workspace folder does not exist: {root}")

    rel = safe_rel(rel)
    full = os.path.realpath(os.path.join(root_real, rel)) if rel else root_real
    if full != root_real and not full.startswith(root_real + os.sep):
        raise FileError("path escapes the workspace")
    return full


def mode_for(name: str) -> str:
    """'notes' for markdown (the prose surface), 'code' for everything else."""
    return "notes" if str(name or "").lower().endswith(PROSE_EXTS) else "code"


# ---------------------------------------------------------------------------
# listing
# ---------------------------------------------------------------------------
def list_dir(root, rel="", show_hidden=False, skip_dirs=()) -> list:
    """one folder level, folders first. symlinks are skipped on purpose."""
    target = resolve(root, rel)
    if not os.path.isdir(target):
        raise FileError("not a folder")

    rel = safe_rel(rel)
    skip_dirs = {str(name) for name in (skip_dirs or [])}
    entries = []

    try:
        with os.scandir(target) as scan:
            for entry in scan:
                name = entry.name
                if name == TRASH_DIR:
                    continue  # the trash is reachable from its own tab, never from the tree
                if not show_hidden and name.startswith("."):
                    continue
                try:
                    if entry.is_symlink():
                        continue
                    is_dir = entry.is_dir(follow_symlinks=False)
                    if is_dir and name in skip_dirs:
                        continue
                    stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue

                entries.append({
                    "name": name,
                    "path": join_rel(rel, name),
                    "is_dir": is_dir,
                    "size": None if is_dir else stat.st_size,
                    "mtime": stat.st_mtime,
                    "mode": None if is_dir else mode_for(name),
                })
    except OSError as e:
        raise FileError(str(e)) from e

    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    return entries


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------
def _stat_file(target) -> os.stat_result:
    if not os.path.isfile(target):
        raise FileError("not a file")
    try:
        return os.stat(target)
    except OSError as e:
        raise FileError(str(e)) from e


def read_text(root, rel, max_bytes=1_000_000) -> dict:
    """whole file as text, with the mtime the write path will guard on later."""
    target = resolve(root, rel)
    stat = _stat_file(target)

    if stat.st_size > int(max_bytes):
        raise FileError(
            f"file is too large to open here ({stat.st_size} bytes, limit {int(max_bytes)})"
        )

    try:
        # newline="" disables universal-newline translation: a CRLF file must
        # reach the editor with its \r intact, otherwise the first save would
        # rewrite every line ending (and show up as a whole-file diff).
        with open(target, "r", encoding="utf-8", newline="") as f:
            content = f.read()
    except UnicodeDecodeError as e:
        raise FileError("this looks like a binary file and cannot be opened here") from e
    except OSError as e:
        raise FileError(str(e)) from e

    return {
        "content": content,
        "mtime": stat.st_mtime,
        "size": stat.st_size,
        "mode": mode_for(rel),
    }


def read_head(root, rel, max_chars=512) -> str:
    """the first `max_chars` characters, for card snippets."""
    target = resolve(root, rel)
    _stat_file(target)
    try:
        with open(target, "r", encoding="utf-8") as f:
            return f.read(int(max_chars))
    except UnicodeDecodeError as e:
        raise FileError("this looks like a binary file") from e
    except OSError as e:
        raise FileError(str(e)) from e


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------
class ConflictError(FileError):
    """the file changed on disk since it was read (optimistic-lock failure)."""


def write_text(root, rel, content, expected_mtime=None, force=False) -> dict:
    """overwrite an existing file, refusing when it changed since it was read.

    expected_mtime is the mtime the client loaded; `force` skips the check (the
    UI's explicit "Overwrite"). Returns the new mtime/size so the next save
    compares against the write we just made instead of the one we loaded.
    """
    target = resolve(root, rel)
    if os.path.isdir(target):
        raise FileError("that path is a folder")
    if not os.path.isfile(target):
        raise FileError("file does not exist (creating notes is a later step)")

    try:
        stat = os.stat(target)
    except OSError as e:
        raise FileError(str(e)) from e

    if not force and expected_mtime:
        try:
            expected = float(expected_mtime)
        except (TypeError, ValueError):
            expected = None
        if expected is not None and abs(stat.st_mtime - expected) > 0.001:
            raise ConflictError(
                "the file changed on disk since it was opened (another tab, or the AI edited it)"
            )

    try:
        # newline="" writes the line endings the caller sent, verbatim
        with open(target, "w", encoding="utf-8", newline="") as f:
            f.write(clean_content(content))
    except OSError as e:
        raise FileError(str(e)) from e

    stat = os.stat(target)
    return {"path": safe_rel(rel), "mtime": stat.st_mtime, "size": stat.st_size}


# ---------------------------------------------------------------------------
# creating / renaming / deleting
# ---------------------------------------------------------------------------
def create_file(root, rel, content="") -> dict:
    """create a new file (never overwrites); parents are created as needed."""
    target = resolve(root, rel)
    if os.path.exists(target):
        raise FileError("something already exists at that name")
    if not str(rel or "").strip():
        raise FileError("a file name is required")

    parent = os.path.dirname(target)
    try:
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        # 'x' refuses to clobber, even if something appeared since the check
        with open(target, "x", encoding="utf-8", newline="") as f:
            f.write(clean_content(content))
    except FileExistsError as e:
        raise FileError("something already exists at that name") from e
    except OSError as e:
        raise FileError(str(e)) from e

    stat = os.stat(target)
    return {"path": safe_rel(rel), "mtime": stat.st_mtime, "size": stat.st_size}


def make_dir(root, rel) -> dict:
    """create a folder (parents included); refuses to replace a file."""
    target = resolve(root, rel)
    if os.path.isfile(target):
        raise FileError("a file already exists at that name")
    if not str(rel or "").strip():
        raise FileError("a folder name is required")
    try:
        os.makedirs(target, exist_ok=True)
    except OSError as e:
        raise FileError(str(e)) from e
    return {"path": safe_rel(rel)}


def rename_path(root, rel, new_rel) -> dict:
    """rename/move a file inside the workspace; refuses to overwrite."""
    src = resolve(root, rel)
    if not os.path.exists(src):
        raise FileError("file does not exist")
    if os.path.isdir(src):
        raise FileError("folders are not renamed here yet")

    dst = resolve(root, new_rel)
    if dst == src:
        raise FileError("nothing to rename")
    if os.path.exists(dst):
        raise FileError("something already exists at the new name")

    parent = os.path.dirname(dst)
    try:
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        os.rename(src, dst)
    except OSError as e:
        raise FileError(str(e)) from e

    stat = os.stat(dst)
    return {"path": safe_rel(new_rel), "mtime": stat.st_mtime, "size": stat.st_size}


def delete_file(root, rel, use_trash=True) -> dict:
    """delete a file, with or without a trashcan.

    use_trash -> move to <workspace>/.libreta-trash/<stamp>/<path>: the file stays
    on disk, the tree hides it (dot folder) and it can be restored from the Trash
    tab. Without the trash the file is really removed — the UI asks first.
    """
    target = resolve(root, rel)
    if os.path.isdir(target):
        raise FileError("folders are not deleted here yet")
    if not os.path.isfile(target):
        raise FileError("file does not exist")

    rel = safe_rel(rel)

    if not use_trash:
        try:
            os.remove(target)
        except OSError as e:
            raise FileError(str(e)) from e
        return {"path": rel, "trashed": None}

    root_real = resolve(root, "")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest_rel = f"{TRASH_DIR}/{stamp}/{rel}"
    dest = os.path.join(root_real, *dest_rel.split("/"))

    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        os.rename(target, dest)
    except OSError as e:
        raise FileError(str(e)) from e

    return {"path": rel, "trashed": dest_rel}


def _trash_root(root) -> str:
    return os.path.join(resolve(root, ""), TRASH_DIR)


def list_trash(root) -> list:
    """every trashed file, newest first: {trashed, original, stamp, name, size, mtime}.
    `original` is where it would be restored to."""
    base = _trash_root(root)
    if not os.path.isdir(base):
        return []

    root_real = resolve(root, "")
    entries = []

    try:
        stamps = sorted(os.listdir(base), reverse=True)
    except OSError:
        return []

    for stamp in stamps:
        stamp_dir = os.path.join(base, stamp)
        if not os.path.isdir(stamp_dir):
            continue
        for dirpath, _dirnames, filenames in os.walk(stamp_dir):
            for filename in filenames:
                full = os.path.join(dirpath, filename)
                try:
                    stat = os.stat(full)
                except OSError:
                    continue
                entries.append({
                    "trashed": os.path.relpath(full, root_real).replace(os.sep, "/"),
                    "original": os.path.relpath(full, stamp_dir).replace(os.sep, "/"),
                    "stamp": stamp,
                    "name": filename,
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                })

    entries.sort(key=lambda e: (e["stamp"], e["original"]), reverse=True)
    return entries


def restore_trash(root, trashed_rel) -> dict:
    """move a trashed file back to where it came from; refuses to overwrite."""
    trash_real = os.path.realpath(_trash_root(root))
    src = resolve(root, trashed_rel)

    if not src.startswith(trash_real + os.sep):
        raise FileError("that file is not in the trash")
    if not os.path.isfile(src):
        raise FileError("trashed file does not exist")

    # .libreta-trash/<stamp>/<original path...>
    parts = os.path.relpath(src, trash_real).split(os.sep)
    if len(parts) < 2:
        raise FileError("trashed file has no original path")
    original_rel = "/".join(parts[1:])

    dest = resolve(root, original_rel)
    if os.path.exists(dest):
        raise FileError(f"something already exists at {original_rel}")

    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        os.rename(src, dest)
    except OSError as e:
        raise FileError(str(e)) from e

    return {"path": original_rel, "restored_from": safe_rel(trashed_rel)}


def purge_trash(root, trashed_rel) -> dict:
    """permanently remove ONE trashed file."""
    trash_real = os.path.realpath(_trash_root(root))
    src = resolve(root, trashed_rel)

    if not src.startswith(trash_real + os.sep):
        raise FileError("that file is not in the trash")
    if not os.path.isfile(src):
        raise FileError("trashed file does not exist")

    try:
        os.remove(src)
    except OSError as e:
        raise FileError(str(e)) from e

    # prune empty parents (a trashed file keeps its original folder nesting),
    # but never the trash root itself
    parent = os.path.dirname(src)
    while parent and parent.startswith(trash_real + os.sep):
        try:
            if not os.path.isdir(parent) or os.listdir(parent):
                break
            os.rmdir(parent)
        except OSError:
            break
        parent = os.path.dirname(parent)

    return {"purged": safe_rel(trashed_rel)}


def empty_trash(root) -> dict:
    """permanently remove everything in the trash (stamp folders included)."""
    base = _trash_root(root)
    if not os.path.isdir(base):
        return {"removed": 0}

    removed = 0
    for dirpath, dirnames, filenames in os.walk(base, topdown=False):
        for filename in filenames:
            try:
                os.remove(os.path.join(dirpath, filename))
                removed += 1
            except OSError:
                continue
        for dirname in dirnames:
            try:
                os.rmdir(os.path.join(dirpath, dirname))  # only if empty now
            except OSError:
                continue

    return {"removed": removed}


# ---------------------------------------------------------------------------
# snippets
# ---------------------------------------------------------------------------
def make_snippet(text, limit=140) -> str:
    """first readable prose of a file: code fences dropped, markdown markers
    stripped, whitespace collapsed, truncated with an ellipsis."""
    if not text:
        return ""

    chunks = []
    in_fence = False

    for raw_line in str(text).splitlines():
        line = raw_line.strip()
        if line.startswith("```") or line.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence or not line:
            continue

        line = _SNIPPET_PREFIX.sub("", line)
        line = _SNIPPET_IMAGE.sub(r"\1", line)
        line = _SNIPPET_LINK.sub(r"\1", line)
        line = line.replace("**", "").replace("__", "").replace("`", "")
        chunks.append(line.strip())

        if len(" ".join(chunks)) >= limit:
            break

    snippet = " ".join(chunks)
    if len(snippet) > limit:
        snippet = snippet[:limit].rstrip() + "\u2026"
    return snippet

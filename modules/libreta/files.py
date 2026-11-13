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

import base64
import mimetypes
import os
import re
import shutil
import time


PROSE_EXTS = (".md", ".markdown", ".mdx")

# files are "deleted" by moving them here (inside the workspace, dot-prefixed so
# the tree hides it). a notes app must never make a delete unrecoverable.
TRASH_DIR = ".libreta-trash"

# raw bytes are for images embedded in notes. every /api/ext route answers JSON,
# so the bytes travel base64-encoded: that costs a third more, and the whole
# payload is held in memory twice, hence a cap far below what a text read allows.
RAW_MAX_BYTES = 8 * 1024 * 1024

# cross-file search is a live scan, not an index: one stray dump or video in a
# workspace must not stall the request, so oversized files are skipped, not read.
SEARCH_MAX_FILE_BYTES = 1_000_000
SEARCH_MAX_LINE_CHARS = 400

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


def list_files(root, show_hidden=False, skip_dirs=()) -> list:
    """every file path in the workspace, for the quick-open palette.

    Same rules as list_dir: the trash and dot entries are skipped unless
    show_hidden, symlinks are ignored, and skip_dirs matches an exact folder
    name at any depth. Deliberately uncapped, like coder's: a cap inside a
    depth-first walk starves every folder that sorts after the one that ate the
    budget, and the palette caps what it renders anyway.
    """
    base = resolve(root)
    skip = {str(name) for name in (skip_dirs or [])}
    found = []

    def walk(current, prefix):
        try:
            with os.scandir(current) as scan:
                children = []
                for entry in scan:
                    name = entry.name
                    if name == TRASH_DIR:
                        continue
                    if not show_hidden and name.startswith("."):
                        continue
                    try:
                        if entry.is_symlink():
                            continue
                        is_dir = entry.is_dir(follow_symlinks=False)
                        is_file = entry.is_file(follow_symlinks=False)
                    except OSError:
                        continue
                    if is_dir and name in skip:
                        continue
                    if is_dir or is_file:
                        children.append((name, is_dir, entry.path))
        except OSError:
            return

        for name, is_dir, path in children:
            if is_dir:
                walk(path, join_rel(prefix, name))
            else:
                found.append(join_rel(prefix, name))

    walk(base, "")
    found.sort(key=str.lower)
    return found


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


def read_text(root, rel, max_bytes=1_000_000, offset=None, limit=None) -> dict:
    """whole file as text, or one byte window of it, with the mtime the write
    path will guard on later.

    Without offset/limit nothing changes: the file must fit in max_bytes and is
    decoded strictly, so a binary file is refused. With a window only that byte
    range is read, decoded with errors="replace" (a window can cut a multi-byte
    character in half) and the payload carries `read_only: True`: the caller
    holds a PARTIAL buffer and must never save it back over the whole file.
    `total` is always the real file size, `offset` the position actually read
    (clamped to EOF when the caller asked for more than the file holds).
    """
    target = resolve(root, rel)
    stat = _stat_file(target)
    total = stat.st_size
    windowed = offset is not None or limit is not None

    payload = {
        "mtime": stat.st_mtime,
        "size": total,
        "mode": mode_for(rel),
        "total": total,
        "windowed": windowed,
        "read_only": windowed,
    }

    if not windowed:
        max_bytes = 1_000_000 if max_bytes is None else int(max_bytes)
        if total > max_bytes:
            raise FileError(
                f"file is too large to open here ({total} bytes, limit {max_bytes})"
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

        payload["content"] = content
        return payload

    start = 0 if offset is None else max(0, int(offset))
    if start > total:
        start = total
    span = None if limit is None else max(0, int(limit))

    try:
        with open(target, "rb") as f:
            f.seek(start)
            raw = f.read() if span is None else f.read(span)
    except OSError as e:
        raise FileError(str(e)) from e

    payload["content"] = raw.decode("utf-8", errors="replace")
    payload["offset"] = start
    return payload


def raw_type(name) -> str:
    """mimetype for a file name; a generic binary type when it cannot be guessed."""
    guessed = mimetypes.guess_type(str(name or ""))[0]
    return guessed or "application/octet-stream"


def read_bytes(root, rel, max_bytes=None) -> dict:
    """one file's bytes as base64, for images embedded in notes.

    The extension API has no binary/streaming response, so the bytes have to
    travel inside JSON. Containment is exactly the read path's: '..' and
    absolute paths are refused, a symlink that resolves outside the workspace
    is refused (resolve realpaths before the containment check), folders and
    missing files are refused.
    """
    target = resolve(root, rel)
    if os.path.isdir(target):
        raise FileError("that path is a folder")
    stat = _stat_file(target)

    cap = RAW_MAX_BYTES if max_bytes is None else int(max_bytes)
    if stat.st_size > cap:
        raise FileError(
            f"file is too large to show inline ({stat.st_size} bytes, limit {cap})"
        )

    try:
        with open(target, "rb") as f:
            data = f.read()
    except OSError as e:
        raise FileError(str(e)) from e

    return {
        "path": safe_rel(rel),
        "content_type": raw_type(rel),
        "size": len(data),
        "base64": base64.b64encode(data).decode("ascii"),
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
# cross-file search (literal, never a regex)
# ---------------------------------------------------------------------------
def _is_word_boundary(text, start, end) -> bool:
    """true when `text[start:end]` is fenced by non-word characters (or the ends)."""
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    return not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_")


def find_in_line(line, needle, case_sensitive=False, whole_word=False) -> int:
    """1-based column of the first literal match of `needle` in `line`, or 0.

    Literal only, on purpose: no re module ever touches the user's query, so
    'a.*b' searches for that exact text instead of exploding into a pattern.
    """
    if not needle:
        return 0

    hay = str(line)
    wanted = str(needle)
    if not case_sensitive:
        hay = hay.lower()
        wanted = wanted.lower()

    start = 0
    while True:
        index = hay.find(wanted, start)
        if index < 0:
            return 0
        if not whole_word or _is_word_boundary(hay, index, index + len(wanted)):
            return index + 1
        start = index + 1


def search_files(root, needle, case_sensitive=False, whole_word=False,
                 max_results=200, max_per_file=10, show_hidden=False, skip_dirs=()) -> dict:
    """literal search across every file of one workspace, line by line.

    Walks files.list_files, so hidden entries, skip_dirs, the trash and symlinks
    (a link inside the workspace pointing elsewhere) are already skipped.
    Files over SEARCH_MAX_FILE_BYTES and files that fail utf-8 decode are
    counted as skipped, never fatal: one dump or image must not kill a search
    over the whole workspace.
    """
    needle = str(needle or "")
    if not needle:
        raise FileError("a search query is required")

    max_results = max(0, int(max_results))
    max_per_file = max(1, int(max_per_file))

    results = []
    truncated = False
    scanned = 0
    skipped = 0

    for rel in list_files(root, show_hidden=show_hidden, skip_dirs=skip_dirs):
        hits = []

        try:
            target = resolve(root, rel)
            if os.stat(target).st_size > SEARCH_MAX_FILE_BYTES:
                skipped += 1
                continue
            # the hits stay local until the file was read to the end: a decode
            # error half way through must not leave half a file in the results
            with open(target, "r", encoding="utf-8") as f:
                for number, line in enumerate(f, 1):
                    if len(hits) >= max_per_file:
                        break
                    column = find_in_line(line, needle, case_sensitive, whole_word)
                    if column:
                        hits.append({
                            "path": rel,
                            "line": number,
                            "col": column,
                            "text": line.strip()[:SEARCH_MAX_LINE_CHARS],
                        })
        except (FileError, OSError, UnicodeDecodeError):
            skipped += 1
            continue

        scanned += 1
        for hit in hits:
            if len(results) >= max_results:
                truncated = True
                break
            results.append(hit)
        if truncated:
            break

    return {
        "results": results,
        "truncated": truncated,
        "scanned_files": scanned,
        "skipped_files": skipped,
    }


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
    """rename/move a file OR folder inside the workspace; refuses to overwrite.

    A folder rename is refused when the destination lands inside the folder
    itself (`a` -> `a/b`), because that tree would swallow its own root and
    leave an unreachable copy. The caller (module.py) additionally rebases the
    stored recents/pins/autosave keys after a folder move.
    """
    src = resolve(root, rel)
    if not os.path.exists(src):
        raise FileError("file does not exist")

    dst = resolve(root, new_rel)
    if dst == src:
        raise FileError("nothing to rename")
    if os.path.isdir(src) and dst.startswith(src.rstrip(os.sep) + os.sep):
        raise FileError("cannot move a folder inside itself")
    if os.path.exists(dst):
        raise FileError("something already exists at the new name")

    parent = os.path.dirname(dst)
    try:
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        os.rename(src, dst)
    except OSError as e:
        raise FileError(str(e)) from e

    if os.path.isdir(dst):
        return {"path": safe_rel(new_rel), "dir": True, "mtime": None, "size": None}

    stat = os.stat(dst)
    return {"path": safe_rel(new_rel), "dir": False, "mtime": stat.st_mtime, "size": stat.st_size}


def delete_file(root, rel, use_trash=True) -> dict:
    """delete a file, with or without a trashcan.

    use_trash -> move to <workspace>/.libreta-trash/<stamp>/<path>: the file stays
    on disk, the tree hides it (dot folder) and it can be restored from the Trash
    tab. Without the trash the file is really removed — the UI asks first.
    """
    target = resolve(root, rel)
    is_dir = os.path.isdir(target)
    if not is_dir and not os.path.isfile(target):
        raise FileError("file does not exist")

    rel = safe_rel(rel)

    if not use_trash:
        try:
            if is_dir:
                shutil.rmtree(target)
            else:
                os.remove(target)
        except OSError as e:
            raise FileError(str(e)) from e
        return {"path": rel, "trashed": None, "dir": is_dir}

    root_real = resolve(root, "")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest_rel = f"{TRASH_DIR}/{stamp}/{rel}"
    dest = os.path.join(root_real, *dest_rel.split("/"))

    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        os.rename(target, dest)
    except OSError as e:
        raise FileError(str(e)) from e

    return {"path": rel, "trashed": dest_rel, "dir": is_dir}


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


def restore_all_trash(root) -> dict:
    """restore every trashed file of one workspace.

    One collision must not abort the batch: each entry is restored on its own
    and failures are collected, so the user gets one answer that says what came
    back and what is still waiting. `conflicts[].path` is where the file would
    have been restored to (the place that is occupied).
    """
    restored = []
    conflicts = []

    for entry in list_trash(root):
        try:
            result = restore_trash(root, entry["trashed"])
        except FileError as e:
            conflicts.append({"path": entry["original"], "reason": str(e)})
            continue
        restored.append(result["path"])

    return {"restored": restored, "conflicts": conflicts}


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

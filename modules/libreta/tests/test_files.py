"""libreta file layer tests.

Run from anywhere, no OpenLumara boot needed:

    python -m unittest discover -s user_modules/libreta/tests -v

(files.py has no `core` imports on purpose, so it loads straight from disk.)
Every vault here is a throwaway folder under tempfile - never your real notes.
"""

import base64
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent

_spec = importlib.util.spec_from_file_location("libreta_files", HERE.parent / "files.py")
files = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(files)


class VaultCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "vault"
        self.root.mkdir()

        (self.root / "Work").mkdir()
        (self.root / "Work" / "note.md").write_text("# Title\n\nbody text\n", encoding="utf-8")
        (self.root / "Work" / "script.py").write_text("print('hi')\n", encoding="utf-8")
        (self.root / ".hidden").write_text("secret\n", encoding="utf-8")
        (self.root / ".git").mkdir()
        (self.root / ".git" / "config").write_text("x\n", encoding="utf-8")
        (self.root / "binary.bin").write_bytes(b"\xff\xfe\x00\x01")

    def tearDown(self):
        self._tmp.cleanup()


class TestPaths(VaultCase):
    def test_safe_rel_normalises(self):
        self.assertEqual(files.safe_rel("Work//note.md"), "Work/note.md")
        self.assertEqual(files.safe_rel("./Work/note.md"), "Work/note.md")
        self.assertEqual(files.safe_rel(""), "")
        self.assertEqual(files.safe_rel(None), "")

    def test_safe_rel_refuses_escapes(self):
        for bad in ("../etc/passwd", "Work/../../x", "/etc/passwd"):
            with self.assertRaises(files.FileError):
                files.safe_rel(bad)

    def test_resolve_inside_root(self):
        resolved = files.resolve(str(self.root), "Work/note.md")
        self.assertEqual(Path(resolved), (self.root / "Work" / "note.md").resolve())

    def test_resolve_refuses_escape(self):
        with self.assertRaises(files.FileError):
            files.resolve(str(self.root), "../outside.txt")

    def test_resolve_refuses_symlink_escape(self):
        outside = Path(self._tmp.name) / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("nope\n", encoding="utf-8")
        link = self.root / "link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not supported here")

        with self.assertRaises(files.FileError):
            files.resolve(str(self.root), "link/secret.txt")

    def test_missing_root_is_reported(self):
        with self.assertRaises(files.FileError):
            files.resolve(str(self.root / "nope"), "")


class TestListing(VaultCase):
    def test_folders_first_and_names(self):
        entries = files.list_dir(str(self.root))
        names = [entry["name"] for entry in entries]
        self.assertEqual(names[0], "Work")
        self.assertTrue(entries[0]["is_dir"])
        self.assertIn("binary.bin", names)

    def test_hidden_skipped_by_default(self):
        names = [entry["name"] for entry in files.list_dir(str(self.root))]
        self.assertNotIn(".hidden", names)
        self.assertNotIn(".git", names)

    def test_show_hidden(self):
        names = [entry["name"] for entry in files.list_dir(str(self.root), show_hidden=True)]
        self.assertIn(".hidden", names)

    def test_skip_dirs(self):
        names = [entry["name"] for entry in files.list_dir(str(self.root), skip_dirs=["Work"])]
        self.assertNotIn("Work", names)

    def test_mode_and_size(self):
        entries = {entry["name"]: entry for entry in files.list_dir(str(self.root), "Work")}
        self.assertEqual(entries["note.md"]["mode"], "notes")
        self.assertEqual(entries["script.py"]["mode"], "code")
        self.assertGreater(entries["note.md"]["size"], 0)
        self.assertFalse(entries["note.md"]["is_dir"])

        root_entries = {entry["name"]: entry for entry in files.list_dir(str(self.root))}
        self.assertTrue(root_entries["Work"]["is_dir"])
        self.assertIsNone(root_entries["Work"]["size"])

    def test_symlinks_skipped(self):
        outside = Path(self._tmp.name) / "outside"
        outside.mkdir()
        (outside / "a.txt").write_text("x\n", encoding="utf-8")
        try:
            (self.root / "alias").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not supported here")

        names = [entry["name"] for entry in files.list_dir(str(self.root))]
        self.assertNotIn("alias", names)


class TestListFiles(VaultCase):
    """the recursive index that backs Ctrl+P quick-open."""

    def test_lists_every_file_recursively(self):
        paths = files.list_files(str(self.root))
        self.assertIn("Work/note.md", paths)
        self.assertIn("Work/script.py", paths)
        self.assertIn("binary.bin", paths)
        self.assertEqual(paths, sorted(paths, key=str.lower))
        self.assertFalse(any(p.startswith("/") for p in paths))

    def test_skips_hidden_and_skip_dirs(self):
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules" / "dep.js").write_text("x\n", encoding="utf-8")
        paths = files.list_files(str(self.root), skip_dirs=("node_modules",))
        self.assertNotIn("node_modules/dep.js", paths)
        self.assertNotIn(".hidden", paths)
        self.assertNotIn(".git/config", paths)
        self.assertIn("Work/note.md", paths)

    def test_show_hidden_includes_dot_entries(self):
        paths = files.list_files(str(self.root), show_hidden=True)
        self.assertIn(".hidden", paths)
        self.assertIn(".git/config", paths)

    def test_skips_trash_and_symlinks(self):
        trash = self.root / files.TRASH_DIR / "stamp"
        trash.mkdir(parents=True)
        (trash / "old.md").write_text("x\n", encoding="utf-8")
        link = self.root / "link.md"
        try:
            os.symlink(self.root / "Work" / "note.md", link)
        except (OSError, NotImplementedError):
            link = None

        paths = files.list_files(str(self.root), show_hidden=True)
        self.assertNotIn(f"{files.TRASH_DIR}/stamp/old.md", paths)
        if link is not None:
            self.assertNotIn("link.md", paths)


class TestReading(VaultCase):
    def test_read_text(self):
        data = files.read_text(str(self.root), "Work/note.md")
        self.assertIn("body text", data["content"])
        self.assertEqual(data["mode"], "notes")
        self.assertGreater(data["mtime"], 0)

    def test_read_refuses_big_files(self):
        with self.assertRaises(files.FileError):
            files.read_text(str(self.root), "Work/note.md", max_bytes=4)

    def test_read_refuses_binary(self):
        with self.assertRaises(files.FileError):
            files.read_text(str(self.root), "binary.bin")

    def test_read_refuses_folder(self):
        with self.assertRaises(files.FileError):
            files.read_text(str(self.root), "Work")

    def test_read_head_truncates(self):
        head = files.read_head(str(self.root), "Work/note.md", max_chars=3)
        self.assertEqual(len(head), 3)


class TestWriting(VaultCase):
    def test_write_replaces_content(self):
        path = self.root / "Work" / "note.md"
        before = os.stat(path).st_mtime
        result = files.write_text(str(self.root), "Work/note.md", "# New\n", expected_mtime=before)

        self.assertEqual(path.read_text(encoding="utf-8"), "# New\n")
        self.assertGreater(result["mtime"], 0)
        self.assertEqual(result["size"], len("# New\n"))

    def test_stale_mtime_is_a_conflict(self):
        stale = os.stat(self.root / "Work" / "note.md").st_mtime - 30
        with self.assertRaises(files.ConflictError):
            files.write_text(str(self.root), "Work/note.md", "x", expected_mtime=stale)

    def test_force_skips_the_check(self):
        stale = os.stat(self.root / "Work" / "note.md").st_mtime - 30
        result = files.write_text(str(self.root), "Work/note.md", "forced\n", expected_mtime=stale, force=True)
        self.assertGreater(result["mtime"], 0)
        self.assertEqual((self.root / "Work" / "note.md").read_text(encoding="utf-8"), "forced\n")

    def test_missing_file_refused(self):
        with self.assertRaises(files.FileError):
            files.write_text(str(self.root), "Work/nope.md", "x")

    def test_folder_refused(self):
        with self.assertRaises(files.FileError):
            files.write_text(str(self.root), "Work", "x")

    def test_escape_refused(self):
        with self.assertRaises(files.FileError):
            files.write_text(str(self.root), "../outside.md", "x")


class TestFileOps(VaultCase):
    def test_create_new_file(self):
        result = files.create_file(str(self.root), "Work/new.md", "hello\n")
        self.assertEqual((self.root / "Work" / "new.md").read_text(encoding="utf-8"), "hello\n")
        self.assertGreater(result["mtime"], 0)

    def test_create_never_overwrites(self):
        with self.assertRaises(files.FileError):
            files.create_file(str(self.root), "Work/note.md", "clobber\n")
        self.assertIn("body text", (self.root / "Work" / "note.md").read_text(encoding="utf-8"))

    def test_create_makes_parents(self):
        files.create_file(str(self.root), "deep/deeper/x.md", "x")
        self.assertTrue((self.root / "deep" / "deeper" / "x.md").is_file())

    def test_create_outside_refused(self):
        with self.assertRaises(files.FileError):
            files.create_file(str(self.root), "../evil.md", "x")

    def test_make_dir(self):
        files.make_dir(str(self.root), "New/Folder")
        self.assertTrue((self.root / "New" / "Folder").is_dir())

    def test_make_dir_refuses_existing_file(self):
        with self.assertRaises(files.FileError):
            files.make_dir(str(self.root), "Work/note.md")

    def test_make_dir_escape_refused(self):
        with self.assertRaises(files.FileError):
            files.make_dir(str(self.root), "../outside")

    def test_rename_moves_the_file(self):
        files.rename_path(str(self.root), "Work/note.md", "Work/renamed.md")
        self.assertFalse((self.root / "Work" / "note.md").exists())
        self.assertTrue((self.root / "Work" / "renamed.md").is_file())

    def test_rename_refuses_existing_target(self):
        with self.assertRaises(files.FileError):
            files.rename_path(str(self.root), "Work/note.md", "Work/script.py")

    def test_rename_moves_a_folder_with_its_contents(self):
        result = files.rename_path(str(self.root), "Work", "Archive")
        self.assertTrue(result["dir"])
        self.assertFalse((self.root / "Work").exists())
        self.assertTrue((self.root / "Archive" / "note.md").is_file())
        self.assertTrue((self.root / "Archive" / "script.py").is_file())

    def test_rename_refuses_folder_into_itself(self):
        with self.assertRaises(files.FileError):
            files.rename_path(str(self.root), "Work", "Work/Inner")

    def test_rename_folder_refuses_existing_target(self):
        (self.root / "Other").mkdir()
        with self.assertRaises(files.FileError):
            files.rename_path(str(self.root), "Work", "Other")

    def test_rename_escape_refused(self):
        with self.assertRaises(files.FileError):
            files.rename_path(str(self.root), "Work/note.md", "../moved.md")

    def test_delete_moves_to_trash(self):
        result = files.delete_file(str(self.root), "Work/note.md")
        self.assertFalse((self.root / "Work" / "note.md").exists())
        self.assertTrue(result["trashed"].startswith(".libreta-trash/"))

        trashed = self.root / result["trashed"]
        self.assertTrue(trashed.is_file())
        self.assertIn("body text", trashed.read_text(encoding="utf-8"))

    def test_write_strips_zero_width_spaces(self):
        before = os.stat(self.root / "Work" / "note.md").st_mtime
        files.write_text(str(self.root), "Work/note.md", "a\n\u200b\nb\n", expected_mtime=before)
        content = (self.root / "Work" / "note.md").read_text(encoding="utf-8")
        self.assertNotIn("\u200b", content)
        self.assertEqual(content, "a\n\nb\n")

    def test_delete_trashes_a_folder_with_its_contents(self):
        result = files.delete_file(str(self.root), "Work")
        self.assertTrue(result["dir"])
        self.assertFalse((self.root / "Work").exists())

        trashed = self.root / result["trashed"]
        self.assertTrue((trashed / "note.md").is_file())
        self.assertTrue((trashed / "script.py").is_file())
        # the tree hides the trash folder, so the name is gone from the listing
        names = [entry["name"] for entry in files.list_dir(str(self.root))]
        self.assertNotIn("Work", names)

    def test_delete_folder_permanently_removes_the_subtree(self):
        result = files.delete_file(str(self.root), "Work", use_trash=False)
        self.assertTrue(result["dir"])
        self.assertFalse((self.root / "Work").exists())

    def test_delete_missing_file(self):
        with self.assertRaises(files.FileError):
            files.delete_file(str(self.root), "Work/nope.md")

    def test_delete_without_trash_removes_permanently(self):
        result = files.delete_file(str(self.root), "Work/note.md", use_trash=False)
        self.assertFalse((self.root / "Work" / "note.md").exists())
        self.assertIsNone(result["trashed"])

    def test_trash_is_hidden_from_the_tree(self):
        files.delete_file(str(self.root), "Work/note.md")
        names = [entry["name"] for entry in files.list_dir(str(self.root), show_hidden=True)]
        self.assertNotIn(".libreta-trash", names)

    def test_list_restore_roundtrip(self):
        files.delete_file(str(self.root), "Work/note.md")
        entries = files.list_trash(str(self.root))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["original"], "Work/note.md")
        self.assertIn("body text", (self.root / entries[0]["trashed"]).read_text(encoding="utf-8"))

        files.restore_trash(str(self.root), entries[0]["trashed"])
        self.assertIn("body text", (self.root / "Work" / "note.md").read_text(encoding="utf-8"))
        self.assertEqual(files.list_trash(str(self.root)), [])

    def test_restore_refuses_to_overwrite(self):
        files.delete_file(str(self.root), "Work/note.md")
        entries = files.list_trash(str(self.root))
        (self.root / "Work" / "note.md").write_text("occupied\n", encoding="utf-8")
        with self.assertRaises(files.FileError):
            files.restore_trash(str(self.root), entries[0]["trashed"])

    def test_restore_refuses_files_outside_the_trash(self):
        with self.assertRaises(files.FileError):
            files.restore_trash(str(self.root), "Work/note.md")

    def test_purge_prunes_the_stamp_folder(self):
        files.delete_file(str(self.root), "Work/note.md")
        entries = files.list_trash(str(self.root))
        files.purge_trash(str(self.root), entries[0]["trashed"])

        trash = self.root / ".libreta-trash"
        self.assertEqual([p for p in trash.iterdir() if p.is_dir()], [])

    def test_purge_and_empty(self):
        files.delete_file(str(self.root), "Work/note.md")
        files.delete_file(str(self.root), "Work/script.py")
        entries = files.list_trash(str(self.root))
        self.assertEqual(len(entries), 2)

        files.purge_trash(str(self.root), entries[0]["trashed"])
        self.assertEqual(len(files.list_trash(str(self.root))), 1)

        result = files.empty_trash(str(self.root))
        self.assertEqual(result["removed"], 1)
        self.assertEqual(files.list_trash(str(self.root)), [])


class TestRawBytes(VaultCase):
    """raw bytes for images in notes: base64 in JSON, same containment as read."""

    def _image(self):
        folder = self.root / "pics"
        folder.mkdir(exist_ok=True)
        payload = b"\x89PNG\r\n\x1a\n\x00\x01binary bytes"
        (folder / "tiny.png").write_bytes(payload)
        return payload

    def test_round_trip(self):
        payload = self._image()
        data = files.read_bytes(str(self.root), "pics/tiny.png")

        self.assertEqual(base64.b64decode(data["base64"]), payload)
        self.assertEqual(data["size"], len(payload))
        self.assertEqual(data["content_type"], "image/png")
        self.assertEqual(data["path"], "pics/tiny.png")

    def test_unknown_type_falls_back(self):
        (self.root / "blob.zzz").write_bytes(b"abcd")
        data = files.read_bytes(str(self.root), "blob.zzz")
        self.assertEqual(data["content_type"], "application/octet-stream")

    def test_absolute_path_refused(self):
        with self.assertRaises(files.FileError):
            files.read_bytes(str(self.root), "/etc/hostname")

    def test_dotdot_escape_refused(self):
        with self.assertRaises(files.FileError):
            files.read_bytes(str(self.root), "../outside.png")

    def test_symlink_outside_refused(self):
        outside = Path(self._tmp.name) / "outside"
        outside.mkdir()
        (outside / "secret.png").write_bytes(b"secret")
        link = self.root / "linked.png"
        try:
            link.symlink_to(outside / "secret.png")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not supported here")

        with self.assertRaises(files.FileError):
            files.read_bytes(str(self.root), "linked.png")

    def test_directory_refused(self):
        with self.assertRaises(files.FileError):
            files.read_bytes(str(self.root), "Work")

    def test_missing_file_refused(self):
        with self.assertRaises(files.FileError):
            files.read_bytes(str(self.root), "Work/nope.png")

    def test_over_cap_refused(self):
        (self.root / "big.png").write_bytes(b"x" * 64)
        old = files.RAW_MAX_BYTES
        files.RAW_MAX_BYTES = 8
        try:
            with self.assertRaises(files.FileError):
                files.read_bytes(str(self.root), "big.png")
            # and the same bytes still come through when the cap allows them
            files.RAW_MAX_BYTES = 64
            self.assertEqual(files.read_bytes(str(self.root), "big.png")["size"], 64)
        finally:
            files.RAW_MAX_BYTES = old


class TestWindowedRead(VaultCase):
    """a file bigger than max_read_bytes is opened one byte window at a time."""

    def test_plain_read_is_unchanged(self):
        data = files.read_text(str(self.root), "Work/note.md")
        self.assertEqual(data["content"], "# Title\n\nbody text\n")
        self.assertFalse(data["windowed"])
        self.assertFalse(data["read_only"])
        self.assertEqual(data["total"], data["size"])
        self.assertEqual(data["mode"], "notes")

    def test_zero_to_n_is_the_prefix(self):
        size = os.stat(self.root / "Work" / "note.md").st_size
        data = files.read_text(str(self.root), "Work/note.md", offset=0, limit=4)

        self.assertEqual(data["content"], "# Ti")
        self.assertEqual(data["offset"], 0)
        self.assertEqual(data["total"], size)
        self.assertTrue(data["windowed"])

    def test_offset_inside_the_file(self):
        data = files.read_text(str(self.root), "Work/note.md", offset=9, limit=9)
        self.assertEqual(data["content"], "body text")
        self.assertEqual(data["offset"], 9)

    def test_offset_past_eof_is_empty(self):
        size = os.stat(self.root / "Work" / "note.md").st_size
        data = files.read_text(str(self.root), "Work/note.md", offset=5000)

        self.assertEqual(data["content"], "")
        self.assertEqual(data["offset"], size)  # clamped to EOF
        self.assertEqual(data["total"], size)

    def test_window_works_where_a_full_read_is_refused(self):
        with self.assertRaises(files.FileError):
            files.read_text(str(self.root), "Work/note.md", max_bytes=4)
        data = files.read_text(str(self.root), "Work/note.md", max_bytes=4, offset=0, limit=4)
        self.assertEqual(data["content"], "# Ti")

    def test_windowed_payload_is_read_only(self):
        windowed = files.read_text(str(self.root), "Work/note.md", limit=4)
        self.assertTrue(windowed["read_only"])
        self.assertTrue(windowed["windowed"])

    def test_cut_multibyte_character_is_replaced(self):
        (self.root / "Work" / "utf8.md").write_bytes("h\u00e9llo".encode("utf-8"))
        data = files.read_text(str(self.root), "Work/utf8.md", offset=1, limit=1)
        self.assertEqual(data["content"], "\ufffd")


class TestSearch(VaultCase):
    """literal cross-file search: no regex, no index, one scan per request."""

    def _write(self, rel, text):
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def test_finds_across_files_with_line_and_column(self):
        self._write("Work/search.md", "alpha beta\nBeta again\n")
        self._write("Work/other.md", "nothing\nbeta here\n")
        result = files.search_files(str(self.root), "beta")

        found = {(r["path"], r["line"], r["col"]) for r in result["results"]}
        self.assertEqual(found, {
            ("Work/other.md", 2, 1),
            ("Work/search.md", 1, 7),
            ("Work/search.md", 2, 1),
        })
        self.assertEqual(result["truncated"], False)
        self.assertGreaterEqual(result["scanned_files"], 4)

    def test_case_folding(self):
        self._write("Work/up.md", "BETA\n")
        self.assertEqual(files.search_files(str(self.root), "beta")["results"][0]["path"], "Work/up.md")
        strict = files.search_files(str(self.root), "beta", case_sensitive=True)
        self.assertEqual(strict["results"], [])

    def test_whole_word_excludes_partial_matches(self):
        self._write("Work/words.md", "concatenate\ncat\n")

        loose = files.search_files(str(self.root), "cat")
        self.assertEqual({r["line"] for r in loose["results"]}, {1, 2})

        strict = files.search_files(str(self.root), "cat", whole_word=True)
        self.assertEqual({(r["line"], r["col"]) for r in strict["results"]}, {(2, 1)})

    def test_query_is_never_a_regex(self):
        self.assertEqual(files.find_in_line("a.*b", "a.*b"), 1)
        self.assertEqual(files.find_in_line("axb", "a.*b"), 0)
        self.assertEqual(files.find_in_line("(x", "(x"), 1)

    def test_max_results_truncates(self):
        self._write("Work/hits.md", "hit\nhit\nhit\nhit\nhit\n")
        result = files.search_files(str(self.root), "hit", max_results=2)
        self.assertEqual(len(result["results"]), 2)
        self.assertTrue(result["truncated"])

    def test_max_per_file(self):
        self._write("Work/hits.md", "hit\nhit\nhit\nhit\nhit\n")
        result = files.search_files(str(self.root), "hit", max_per_file=2)
        self.assertEqual(len(result["results"]), 2)
        self.assertFalse(result["truncated"])

    def test_binary_file_is_skipped_not_fatal(self):
        result = files.search_files(str(self.root), "print")
        self.assertEqual([r["path"] for r in result["results"]], ["Work/script.py"])
        self.assertGreaterEqual(result["skipped_files"], 1)

    def test_oversized_file_is_skipped_not_fatal(self):
        self._write("Work/big.md", "x" * 40 + "\nneedle\n")
        self._write("Work/small.md", "needle\n")
        old = files.SEARCH_MAX_FILE_BYTES
        files.SEARCH_MAX_FILE_BYTES = 20
        try:
            result = files.search_files(str(self.root), "needle")
        finally:
            files.SEARCH_MAX_FILE_BYTES = old

        paths = [r["path"] for r in result["results"]]
        self.assertIn("Work/small.md", paths)
        self.assertNotIn("Work/big.md", paths)
        self.assertGreaterEqual(result["skipped_files"], 1)

    def test_no_matches(self):
        result = files.search_files(str(self.root), "no-such-needle")
        self.assertEqual(result["results"], [])
        self.assertFalse(result["truncated"])
        self.assertGreaterEqual(result["scanned_files"], 1)

    def test_symlink_outside_is_never_searched(self):
        outside = Path(self._tmp.name) / "outside"
        outside.mkdir()
        (outside / "leak.txt").write_text("uniqueneedle\n", encoding="utf-8")
        link = self.root / "leak.txt"
        try:
            link.symlink_to(outside / "leak.txt")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not supported here")

        result = files.search_files(str(self.root), "uniqueneedle")
        self.assertEqual(result["results"], [])

    def test_empty_query_is_refused(self):
        with self.assertRaises(files.FileError):
            files.search_files(str(self.root), "")


class TestRestoreAll(VaultCase):
    """restore-all: one occupied name must not abort the batch."""

    def test_everything_restored(self):
        files.delete_file(str(self.root), "Work/note.md")
        files.delete_file(str(self.root), "Work/script.py")

        result = files.restore_all_trash(str(self.root))
        self.assertEqual(sorted(result["restored"]), ["Work/note.md", "Work/script.py"])
        self.assertEqual(result["conflicts"], [])
        self.assertEqual(files.list_trash(str(self.root)), [])
        self.assertIn("body text", (self.root / "Work" / "note.md").read_text(encoding="utf-8"))

    def test_conflict_keeps_the_rest(self):
        files.delete_file(str(self.root), "Work/note.md")
        files.delete_file(str(self.root), "Work/script.py")
        (self.root / "Work" / "note.md").write_text("occupied\n", encoding="utf-8")

        result = files.restore_all_trash(str(self.root))
        self.assertEqual(result["restored"], ["Work/script.py"])
        self.assertEqual(len(result["conflicts"]), 1)
        self.assertEqual(result["conflicts"][0]["path"], "Work/note.md")
        self.assertIn("already exists", result["conflicts"][0]["reason"])

        # the occupied file is untouched and the collided entry is still trashed
        self.assertEqual((self.root / "Work" / "note.md").read_text(encoding="utf-8"), "occupied\n")
        self.assertEqual(len(files.list_trash(str(self.root))), 1)

    def test_empty_trash(self):
        result = files.restore_all_trash(str(self.root))
        self.assertEqual(result, {"restored": [], "conflicts": []})


class TestSnippets(unittest.TestCase):
    def test_strips_markdown_markers(self):
        text = "# Heading\n\n- first point\n> quote\n```\ncode ignored\n```\nplain line\n"
        snippet = files.make_snippet(text, limit=200)
        self.assertNotIn("#", snippet)
        self.assertNotIn("-", snippet)
        self.assertNotIn("code ignored", snippet)
        self.assertIn("Heading", snippet)
        self.assertIn("first point", snippet)
        self.assertIn("plain line", snippet)

    def test_links_and_emphasis(self):
        snippet = files.make_snippet("**bold** [label](http://x) ![alt](http://y)\n", limit=100)
        self.assertNotIn("**", snippet)
        self.assertNotIn("http", snippet)
        self.assertIn("label", snippet)
        self.assertIn("alt", snippet)

    def test_truncates_with_ellipsis(self):
        snippet = files.make_snippet("a" * 500, limit=20)
        self.assertEqual(len(snippet), 21)
        self.assertTrue(snippet.endswith("\u2026"))

    def test_empty(self):
        self.assertEqual(files.make_snippet(""), "")
        self.assertEqual(files.make_snippet(None), "")


class TestHelpers(unittest.TestCase):
    def test_parent_of(self):
        self.assertEqual(files.parent_of("Work/note.md"), "Work")
        self.assertEqual(files.parent_of("note.md"), "")

    def test_join_rel(self):
        self.assertEqual(files.join_rel("", "a.md"), "a.md")
        self.assertEqual(files.join_rel("Work", "a.md"), "Work/a.md")
        self.assertEqual(files.join_rel("Work/", "a.md"), "Work/a.md")

    def test_mode_for(self):
        self.assertEqual(files.mode_for("a.md"), "notes")
        self.assertEqual(files.mode_for("a.MARKDOWN"), "notes")
        self.assertEqual(files.mode_for("a.py"), "code")
        self.assertEqual(files.mode_for("a"), "code")


if __name__ == "__main__":
    unittest.main()

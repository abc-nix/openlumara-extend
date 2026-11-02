"""libreta file layer tests.

Run from anywhere, no OpenLumara boot needed:

    python -m unittest discover -s user_modules/libreta/tests -v

(files.py has no `core` imports on purpose, so it loads straight from disk.)
Every vault here is a throwaway folder under tempfile - never your real notes.
"""

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

    def test_rename_refuses_folder(self):
        with self.assertRaises(files.FileError):
            files.rename_path(str(self.root), "Work", "Work2")

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

    def test_delete_refuses_folder(self):
        with self.assertRaises(files.FileError):
            files.delete_file(str(self.root), "Work")
        self.assertTrue((self.root / "Work").is_dir())

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

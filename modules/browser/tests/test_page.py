# Tests for the browser module's stateless helpers.
#
# They deliberately do NOT import module.py (the entry file pulls in core,
# aiohttp and the webui channel). Instead they load cdp.py/page.py as a tiny
# package with `core` and `aiohttp` stubbed out, so the suite is pure stdlib
# and has no side effects on the running install.
#
# Run from anywhere:
#   python3 -m unittest discover -s openlumara-extend/modules/browser/tests -v
# (the path only matters for discovery; imports are resolved from __file__)

import importlib.util
import struct
import sys
import types
import unittest
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]
PKG = "_browser_helpers_pkg"


def _load_helpers():
    """import cdp.py + page.py as a package, with core/aiohttp stubbed."""
    if PKG in sys.modules:
        return sys.modules[PKG]

    # cdp.py imports these at module level but only calls them from methods
    sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))
    core_stub = types.ModuleType("core")
    core_stub.get_data_path = lambda subpath=None: str(MODULE_DIR)   # noqa: E731
    core_stub.detail_error = lambda e: str(e)                        # noqa: E731
    sys.modules.setdefault("core", core_stub)

    pkg = types.ModuleType(PKG)
    pkg.__path__ = [str(MODULE_DIR)]
    sys.modules[PKG] = pkg
    return pkg


_load_helpers()

from _browser_helpers_pkg import page  # noqa: E402


class NormalizeGridTests(unittest.TestCase):
    def test_off_values(self):
        for value in (None, False, "", "false", "FALSE", "0", "none", "  "):
            self.assertIsNone(page.normalize_grid(value), f"{value!r} should be off")

    def test_on_values(self):
        for value in (True, "true", "yes", "TRUE"):
            self.assertEqual(page.normalize_grid(value), 100, value)
        self.assertEqual(page.normalize_grid("50"), 50)
        self.assertEqual(page.normalize_grid(80), 80)

    def test_clamping_and_fallback(self):
        self.assertEqual(page.normalize_grid(1), 20)       # clamped up
        self.assertEqual(page.normalize_grid(99999), 2000)  # clamped down
        self.assertEqual(page.normalize_grid("not-a-number"), 100)  # documented default


class FindTargetTests(unittest.TestCase):
    TARGETS = [
        {"targetId": "AAA"},
        {"targetId": "BBB"},
    ]

    def test_by_index(self):
        self.assertEqual(page.find_target(self.TARGETS, "0"), "AAA")
        self.assertEqual(page.find_target(self.TARGETS, "1"), "BBB")

    def test_by_id(self):
        self.assertEqual(page.find_target(self.TARGETS, "BBB"), "BBB")

    def test_misses(self):
        self.assertIsNone(page.find_target(self.TARGETS, "9"))
        self.assertIsNone(page.find_target(self.TARGETS, "ZZZ"))
        self.assertIsNone(page.find_target([], "0"))


class WalkAxTreeTests(unittest.TestCase):
    def _ax_nodes(self):
        return [
            {
                "nodeId": 1,
                "role": {"value": "rootWebArea"},
                "name": {"value": "Example"},
                "childIds": [2, 3],
            },
            {
                "nodeId": 2,
                "role": {"value": "button"},
                "name": {"value": "Submit"},
                "backendDOMNodeId": 42,
                "properties": [{"name": "focusable", "value": {"value": True}}],
                "childIds": [4],
            },
            {
                "nodeId": 3,
                "role": {"value": "StaticText"},
                "name": {"value": "Hello"},
            },
            {
                "nodeId": 4,
                "role": {"value": "StaticText"},
                "name": {"value": "Submit"},
            },
        ]

    def test_interactive_nodes_get_refs(self):
        lines, refs = page.walk_ax_tree(self._ax_nodes())
        self.assertEqual(refs, {"@e1": 42})
        # lines are indented by depth, so match on the line content
        self.assertTrue(any(l.strip() == "button: Submit [@e1]" for l in lines), lines)
        self.assertEqual(lines[0], "rootWebArea: Example")  # depth 0

    def test_non_interactive_nodes_get_no_ref(self):
        lines, _ = page.walk_ax_tree(self._ax_nodes())
        hello = next(l for l in lines if l.strip() == "StaticText: Hello")
        self.assertNotIn("@e", hello)


class ImageSizeTests(unittest.TestCase):
    @staticmethod
    def _png(width, height):
        # PNG layout: 8-byte signature | length(4) | "IHDR"(4) | width(4) | height(4)
        # png_size() reads the magic at 0..8, "IHDR" at 12..16 and w/h at 16..24
        return (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\r" + b"IHDR"
                + struct.pack(">II", width, height) + b"\x00" * 8)

    @staticmethod
    def _jpeg(width, height):
        sof = b"\xff\xc0\x00\x11\x08" + struct.pack(">HH", height, width) + b"\x03" * 9
        return b"\xff\xd8" + sof + b"\xff\xd9"

    def test_png(self):
        self.assertEqual(page.png_size(self._png(320, 200)), (320, 200))

    def test_jpeg(self):
        self.assertEqual(page.jpeg_size(self._jpeg(1024, 768)), (1024, 768))

    def test_dispatch(self):
        self.assertEqual(page.image_size(self._png(10, 20), "png"), (10, 20))
        self.assertEqual(page.image_size(self._jpeg(30, 40), "jpeg"), (30, 40))

    def test_garbage(self):
        self.assertIsNone(page.png_size(b"nope"))
        self.assertIsNone(page.jpeg_size(b"nope"))
        self.assertIsNone(page.jpeg_size(b"\xff\xd8\xff\xc0"))


if __name__ == "__main__":
    unittest.main()

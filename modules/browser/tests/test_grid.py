# Tests for the PNG grid annotation (page.decode_png / encode_png / draw_grid).
# Stdlib only; the helper module is loaded with core/aiohttp stubbed, like
# test_page.py. Run:
#   python3 -m unittest discover -s modules/browser/tests -v

import struct
import sys
import types
import unittest
import zlib
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]
PKG = "_browser_helpers_pkg"
RED = (224, 0, 0)
BASE = (10, 20, 30)


def _load_helpers():
    if PKG in sys.modules:
        return
    sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))
    core_stub = types.ModuleType("core")
    core_stub.get_data_path = lambda subpath=None: str(MODULE_DIR)   # noqa: E731
    core_stub.detail_error = lambda e: str(e)                        # noqa: E731
    sys.modules.setdefault("core", core_stub)
    pkg = types.ModuleType(PKG)
    pkg.__path__ = [str(MODULE_DIR)]
    sys.modules[PKG] = pkg


_load_helpers()

from _browser_helpers_pkg import page  # noqa: E402


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)


def build_png(width, height, bpp, pixels, filters=(0,)):
    """Independent PNG writer with selectable per-row filter types (0-4).

    Written here instead of reusing page.encode_png so the decoder is tested
    against something it did not produce, and so all filter types get covered.
    """
    stride = width * bpp
    raw = bytearray()
    prev = bytes(stride)
    for y in range(height):
        row = bytes(pixels[y * stride:(y + 1) * stride])
        ftype = filters[y % len(filters)]
        if ftype == 1:
            out = bytes([(row[i] - (row[i - bpp] if i >= bpp else 0)) & 255 for i in range(stride)])
        elif ftype == 2:
            out = bytes([(a - b) & 255 for a, b in zip(row, prev)])
        elif ftype == 3:
            out = bytes([(row[i] - (((row[i - bpp] if i >= bpp else 0) + prev[i]) >> 1)) & 255
                         for i in range(stride)])
        elif ftype == 4:
            out = bytes([(row[i] - _paeth(row[i - bpp] if i >= bpp else 0, prev[i],
                                          prev[i - bpp] if i >= bpp else 0)) & 255
                         for i in range(stride)])
        else:
            out = row
        raw.append(ftype)
        raw += out
        prev = row

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2 if bpp == 3 else 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))


def solid(width, height, bpp, colour):
    return bytearray(bytes(colour) * (width * height))


def px(pixels, width, bpp, x, y):
    i = (y * width + x) * bpp
    return tuple(pixels[i:i + 3])


def alpha(pixels, width, bpp, x, y):
    return pixels[(y * width + x) * bpp + 3]


class DecodeTests(unittest.TestCase):
    def test_all_filter_types_roundtrip(self):
        width, height, bpp = 13, 7, 3
        pixels = bytearray()
        for y in range(height):
            for x in range(width):
                pixels += bytes(((x * 17 + y * 31) & 255, (x * 5 + y * 7) & 255, (x + y) * 3 & 255))
        for filters in ([0], [1], [2], [3], [4], [0, 1, 2, 3, 4]):
            raw = build_png(width, height, bpp, pixels, filters)
            w, h, decoded_bpp, decoded = page.decode_png(raw)
            self.assertEqual((w, h, decoded_bpp), (width, height, bpp), filters)
            self.assertEqual(bytes(decoded), bytes(pixels), f"filter set {filters}")

    def test_rejects_non_png(self):
        with self.assertRaises(ValueError):
            page.decode_png(b"not a png")

    def test_encode_decode_roundtrip(self):
        width, height, bpp = 9, 5, 4
        pixels = solid(width, height, bpp, (1, 2, 3, 128))
        w, h, decoded_bpp, decoded = page.decode_png(page.encode_png(width, height, bpp, pixels))
        self.assertEqual((w, h, decoded_bpp), (width, height, bpp))
        self.assertEqual(bytes(decoded), bytes(pixels))


class DrawGridTests(unittest.TestCase):
    W, H = 200, 120

    def _drawn(self, spacing=50, origin=(0, 0), bpp=3, scale=1.0):
        base = solid(self.W, self.H, bpp, BASE if bpp == 3 else BASE + (200,))
        raw = build_png(self.W, self.H, bpp, base, filters=(0, 1, 2, 3, 4))
        out = page.draw_grid(raw, spacing, origin_x=origin[0], origin_y=origin[1],
                             scale=scale)
        self.assertEqual(page.png_size(out), (self.W, self.H))
        _, _, decoded_bpp, pixels = page.decode_png(out)
        self.assertEqual(decoded_bpp, bpp)
        return pixels, bpp

    def test_lines_land_on_spacing(self):
        pixels, bpp = self._drawn(spacing=50)
        self.assertEqual(px(pixels, self.W, bpp, 50, 90), RED)     # vertical line
        self.assertNotEqual(px(pixels, self.W, bpp, 51, 90), RED)  # exactly 1px wide
        # x=60 is not on a vertical line, so it isolates the horizontal one
        self.assertEqual(px(pixels, self.W, bpp, 60, 100), RED)    # horizontal line
        self.assertNotEqual(px(pixels, self.W, bpp, 60, 101), RED)
        self.assertEqual(px(pixels, self.W, bpp, 60, 70), BASE)    # background untouched

    def test_origin_offsets_the_grid(self):
        pixels, bpp = self._drawn(spacing=50, origin=(30, 25))
        # document multiples of 50 sit at image x = 20, 70, ... and y = 25, 75, ...
        self.assertEqual(px(pixels, self.W, bpp, 20, 60), RED)
        self.assertNotEqual(px(pixels, self.W, bpp, 0, 60), RED)
        self.assertEqual(px(pixels, self.W, bpp, 100, 25), RED)
        self.assertNotEqual(px(pixels, self.W, bpp, 100, 20), RED)

    def test_scale_maps_document_px_to_image_px(self):
        # devicePixelRatio 2: a 50-document-px grid sits every 100 image px
        pixels, bpp = self._drawn(spacing=50, scale=2)
        self.assertEqual(px(pixels, self.W, bpp, 100, 90), RED)
        self.assertNotEqual(px(pixels, self.W, bpp, 50, 90), RED)
        self.assertNotEqual(px(pixels, self.W, bpp, 101, 90), RED)

    def test_scale_keeps_the_document_origin(self):
        # left edge at document x=30, scale 2: the document-50 line lands at
        # image x=(50-30)*2=40, not at 20 (scale 1) or 60
        pixels, bpp = self._drawn(spacing=50, origin=(30, 0), scale=2)
        self.assertEqual(px(pixels, self.W, bpp, 40, 90), RED)
        self.assertNotEqual(px(pixels, self.W, bpp, 20, 90), RED)

    def test_labels_have_white_backing_and_ink(self):
        pixels, bpp = self._drawn(spacing=50)
        # "x=50" label starts at (x+3, 2) with one scale-unit of padding
        self.assertEqual(px(pixels, self.W, bpp, 53, 2), (255, 255, 255))
        box = [px(pixels, self.W, bpp, 53 + dx, 3 + dy) for dx in range(34) for dy in range(13)]
        self.assertIn(RED, box, "label ink missing")

    def test_rgba_keeps_alpha(self):
        pixels, bpp = self._drawn(spacing=50, bpp=4)
        self.assertEqual(px(pixels, self.W, bpp, 50, 90), RED)     # vertical line
        self.assertEqual(alpha(pixels, self.W, bpp, 50, 90), 200)  # alpha preserved
        self.assertEqual(px(pixels, self.W, bpp, 100, 100), RED)   # horizontal line
        self.assertEqual(alpha(pixels, self.W, bpp, 100, 100), 200)

    def test_spacing_is_clamped(self):
        base = solid(40, 20, 3, BASE)
        out = page.draw_grid(build_png(40, 20, 3, base), 0)
        self.assertEqual(page.png_size(out), (40, 20))


if __name__ == "__main__":
    unittest.main()

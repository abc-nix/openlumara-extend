# Tests for get_images: the page filter (page.collect_images), the pure
# dedupe/cap step (util.unique_images) and the tool's result shape.
#
# Run from anywhere:
#   python3 -m unittest discover -s modules/browser/tests -v

import asyncio
import json
import os
import tempfile
import unittest

from test_module_events import module, make_browser
from _browser_module_pkg import page, util


class UniqueImagesTests(unittest.TestCase):
    def test_duplicates_are_dropped_in_page_order(self):
        images = util.unique_images([
            {"src": "a.png", "alt": ""},
            {"src": "b.png", "alt": "B"},
            {"src": "a.png", "alt": ""},
        ])
        self.assertEqual([image["src"] for image in images], ["a.png", "b.png"])
        self.assertEqual(images[1]["alt"], "B")

    def test_long_urls_are_cut(self):
        images = util.unique_images([{"src": "https://x.test/" + "a" * 300}], max_url=40)
        self.assertEqual(len(images[0]["src"]), 40)
        self.assertTrue(images[0]["src"].endswith("..."))

    def test_sizes_are_integers(self):
        images = util.unique_images([{"src": "a.png", "width": "12", "height": None}])
        self.assertEqual(images[0]["width"], 12)
        self.assertEqual(images[0]["height"], 0)

    def test_useless_entries_are_skipped(self):
        self.assertEqual(util.unique_images([{"src": ""}, "nope", None]), [])


class _ImageCDP:
    def __init__(self, entries=None):
        self.entries = entries or []
        self.calls = []

    async def cmd(self, method, params=None, session_id=None, timeout=None):
        params = dict(params or {})
        self.calls.append((method, params))
        return {"result": {"value": json.dumps(self.entries)}}


class CollectImagesTests(unittest.TestCase):
    def test_entries_are_parsed_and_the_size_reaches_the_page(self):
        cdp = _ImageCDP([{"src": "a.png", "alt": "", "width": 100, "height": 80}])
        images = asyncio.run(page.collect_images(cdp, "S1", 0))
        self.assertEqual(images[0]["src"], "a.png")
        self.assertIn("(0)", cdp.calls[0][1]["expression"])

    def test_a_failed_call_reads_as_no_images(self):
        class Broken:
            async def cmd(self, *args, **kwargs):
                raise RuntimeError("boom")
        self.assertEqual(asyncio.run(page.collect_images(Broken(), "S1")), [])


def _get_images(entries, **kwargs):
    browser = make_browser()

    async def fake_connect():
        return True

    browser._ensure_connection = fake_connect
    browser._cdp = _ImageCDP(entries)
    browser._snapshots_dir = tempfile.mkdtemp()
    result = asyncio.run(module.Browser.get_images.__wrapped__(browser, **kwargs))
    return result, browser._cdp


class GetImagesToolTests(unittest.TestCase):
    ENTRIES = [
        {"src": "https://x.test/a.png", "alt": "A", "width": 100, "height": 100},
        {"src": "https://x.test/a.png", "alt": "A", "width": 100, "height": 100},
        {"src": "https://x.test/b.png", "alt": "B", "width": 200, "height": 100},
    ]

    def test_the_answer_is_fenced_and_counts_what_it_saw(self):
        result, _ = _get_images(self.ENTRIES)
        content = result["data"]["web_content"]
        self.assertEqual(content["total"], 2)
        self.assertEqual(content["returned"], 2)
        self.assertEqual(content["omitted"], 0)

    def test_a_capped_list_is_saved_whole(self):
        result, cdp = _get_images(self.ENTRIES, limit=1)
        content = result["data"]["web_content"]
        self.assertEqual(content["returned"], 1)
        self.assertEqual(content["omitted"], 1)
        path = content["full_path"]
        self.assertTrue(os.path.isfile(path))
        with open(path, encoding="utf-8") as f:
            self.assertEqual(len(json.load(f)), 2)

    def test_an_empty_page_stays_a_plain_empty_list(self):
        result, _ = _get_images([])
        self.assertEqual(result["data"], [])

    def test_min_size_is_clamped_and_passed_through(self):
        _, cdp = _get_images(self.ENTRIES, min_size=-5)
        self.assertIn("(0)", cdp.calls[0][1]["expression"])
        _, cdp = _get_images(self.ENTRIES, min_size=200)
        self.assertIn("(200)", cdp.calls[0][1]["expression"])


if __name__ == "__main__":
    unittest.main()

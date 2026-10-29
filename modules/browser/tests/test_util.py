# Tests for util.py - pure stdlib, loaded straight from the file (no package,
# no core/aiohttp stubs). Run:
#   python3 -m unittest discover -s modules/browser/tests -v

import importlib.util
import unittest
from collections import deque
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, MODULE_DIR / f"{name.split('_')[-1]}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


util = _load("_browser_util")


class HeadlessFromProductTests(unittest.TestCase):
    def test_chrome_is_visible(self):
        self.assertFalse(util.headless_from_product("Chrome/131.0.6778.85"))

    def test_headless_chrome_is_headless(self):
        self.assertTrue(util.headless_from_product("HeadlessChrome/131.0.6778.85"))

    def test_unknown_when_no_string(self):
        for value in (None, "", "   "):
            self.assertIsNone(util.headless_from_product(value))

    def test_only_a_product_name_starting_with_headless_counts(self):
        # the check looks at the product name before the "/", not at any
        # substring of it
        self.assertFalse(util.headless_from_product("SomeHeadlessFork/1.0"))
        self.assertTrue(util.headless_from_product("HeadlessFork/1.0"))


class HeadlessFromVersionTests(unittest.TestCase):
    """B33: modern Chrome is headless without saying so in the product string."""

    BROWSER = "Chrome/156.0.8078.73"

    def test_new_headless_admits_it_only_in_the_user_agent(self):
        # measured 2026-10-29: --headless=new still reports Browser=Chrome/...
        self.assertTrue(util.headless_from_version({
            "Browser": self.BROWSER,
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) HeadlessChrome/156.0.0.0 Safari/537.36",
        }))

    def test_visible_browser(self):
        self.assertFalse(util.headless_from_version({
            "Browser": self.BROWSER,
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36",
        }))

    def test_old_headless_product_string_still_counts(self):
        self.assertTrue(util.headless_from_version({"Browser": "HeadlessChrome/131"}))

    def test_nothing_known_is_none(self):
        self.assertIsNone(util.headless_from_version(None))
        self.assertIsNone(util.headless_from_version({}))


class DropTargetTests(unittest.TestCase):
    def test_current_tab_dying_clears_the_current_target(self):
        attached = {"A": "sid-a", "B": "sid-b"}
        self.assertEqual(util.drop_target(attached, "A", "A"), None)
        self.assertNotIn("A", attached)
        self.assertIn("B", attached)

    def test_other_tab_dying_keeps_the_current_target(self):
        attached = {"A": "sid-a", "B": "sid-b"}
        self.assertEqual(util.drop_target(attached, "A", "B"), "A")
        self.assertNotIn("B", attached)
        self.assertEqual(attached["A"], "sid-a")

    def test_unknown_id_is_harmless(self):
        attached = {"A": "sid-a"}
        self.assertEqual(util.drop_target(attached, "A", "Z"), "A")
        self.assertEqual(len(attached), 1)

    def test_no_target_id_changes_nothing(self):
        attached = {"A": "sid-a"}
        self.assertEqual(util.drop_target(attached, "A", None), "A")
        self.assertEqual(len(attached), 1)


class NormalizeUrlTests(unittest.TestCase):
    def test_empty(self):
        for value in ("", "   ", None):
            self.assertEqual(util.normalize_url(value), "about:blank")

    def test_schemeless_gets_https(self):
        for value in ("example.com", "example.com/path?q=1", "  example.com  "):
            self.assertEqual(util.normalize_url(value), "https://" + value.strip())

    def test_host_with_port_is_not_mistaken_for_a_scheme(self):
        # urlparse("localhost:3000").scheme == "localhost"
        self.assertEqual(util.normalize_url("localhost:3000"), "https://localhost:3000")
        self.assertEqual(util.normalize_url("127.0.0.1:8080"), "https://127.0.0.1:8080")

    def test_protocol_relative(self):
        self.assertEqual(util.normalize_url("//example.com/a"), "https://example.com/a")

    def test_known_schemes_pass_through(self):
        for value in (
            "https://example.com",
            "http://example.com",
            "HTTP://Example.com",          # scheme is lowercased by urlparse
            "about:blank",
            "data:text/html,<h1>hi</h1>",
            "chrome://newtab/",
            "file:///etc/hosts",
        ):
            self.assertEqual(util.normalize_url(value), value)


class TailEntriesTests(unittest.TestCase):
    def setUp(self):
        self.buf = deque(range(1, 6))  # 1..5

    def test_zero_returns_nothing(self):
        # the bug being guarded: [-0:] would return the whole buffer
        self.assertEqual(util.tail_entries(self.buf, 0), [])

    def test_negative_and_unparseable_return_nothing(self):
        for value in (-3, "abc", None):
            self.assertEqual(util.tail_entries(self.buf, value), [])

    def test_tail(self):
        self.assertEqual(util.tail_entries(self.buf, 2), [4, 5])
        self.assertEqual(util.tail_entries(self.buf, "3"), [3, 4, 5])
        self.assertEqual(util.tail_entries(self.buf, 3.9), [3, 4, 5])

    def test_more_than_available(self):
        self.assertEqual(util.tail_entries(self.buf, 99), [1, 2, 3, 4, 5])

    def test_empty_buffer(self):
        self.assertEqual(util.tail_entries(deque(), 5), [])


if __name__ == "__main__":
    unittest.main()

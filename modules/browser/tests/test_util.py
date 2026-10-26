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

# Tests for console(): severity floor, exact type filter and result counts.
#
# Run from anywhere:
#   python3 -m unittest discover -s modules/browser/tests -v

import asyncio
import unittest
from collections import deque

from test_module_events import module, make_browser
from _browser_module_pkg import util

ENTRIES = [
    {"type": "debug", "text": "lazyLoadVideos, start"},
    {"type": "log", "text": "Portals Logger ready"},
    {"type": "info", "text": "__PROVIDER__ is undefined"},
    {"type": "warning", "text": "preload not used"},
    {"type": "error", "text": "Failed to load resource: 403"},
    {"type": "exception", "text": "ReferenceError: x is not defined"},
    {"type": "timeEnd", "text": "a: 0.002 ms"},
]


class FilterConsoleTests(unittest.TestCase):
    def test_the_default_hides_debug_chatter(self):
        kept, hidden = util.filter_console(ENTRIES)
        kinds = [entry["type"] for entry in kept]
        self.assertNotIn("debug", kinds)
        self.assertEqual(hidden, {"level": 1, "type": 0})
        self.assertEqual(len(kept), 6)

    def test_debug_shows_everything(self):
        kept, hidden = util.filter_console(ENTRIES, level="debug")
        self.assertEqual(len(kept), 7)
        self.assertEqual(hidden["level"], 0)

    def test_warning_keeps_warnings_and_errors(self):
        kept, _ = util.filter_console(ENTRIES, level="warning")
        self.assertEqual({entry["type"] for entry in kept},
                         {"warning", "error", "exception"})

    def test_error_keeps_errors_only(self):
        kept, hidden = util.filter_console(ENTRIES, level="error")
        self.assertEqual({entry["type"] for entry in kept}, {"error", "exception"})
        self.assertEqual(hidden["level"], 5)

    def test_warn_is_accepted_as_warning(self):
        kept, _ = util.filter_console(ENTRIES, level="warn")
        self.assertEqual(len(kept), 3)

    def test_an_unknown_type_ranks_as_info(self):
        kept, _ = util.filter_console([{"type": "futureThing", "text": "x"}])
        self.assertEqual(len(kept), 1)

    def test_types_filter_is_exact(self):
        kept, hidden = util.filter_console(ENTRIES, types="error, warning")
        self.assertEqual({entry["type"] for entry in kept}, {"warning", "error"})
        self.assertEqual(hidden["type"], 5)


def _browser(entries=None):
    browser = make_browser()

    async def fake_connect():
        return True

    browser._ensure_connection = fake_connect
    browser._console_log = deque(entries if entries is not None else list(ENTRIES),
                                 maxlen=500)
    return browser


def _call(browser, **kwargs):
    return asyncio.run(module.Browser.console.__wrapped__(browser, **kwargs))


class ConsoleToolTests(unittest.TestCase):
    def test_the_result_says_what_it_hid(self):
        content = _call(_browser())["data"]["web_content"]
        self.assertEqual(content["total"], 7)
        self.assertEqual(content["matched"], 6)
        self.assertEqual(content["shown"], 6)
        self.assertEqual(content["hidden"]["level"], 1)

    def test_a_level_filter_narrows_and_reports(self):
        content = _call(_browser(), level="error")["data"]["web_content"]
        self.assertEqual(content["matched"], 2)

    def test_count_applies_after_filtering(self):
        # the error is the second to last entry; a tail of 2 would miss it if
        # the slice came first
        content = _call(_browser(), count=2, level="error")["data"]["web_content"]
        self.assertEqual(content["shown"], 2)

    def test_clear_empties_the_buffer(self):
        browser = _browser()
        _call(browser, clear=True)
        self.assertEqual(list(browser._console_log), [])

    def test_an_empty_buffer_says_so(self):
        result = _call(_browser(entries=[]))
        self.assertEqual(result["data"], "No console output recorded.")


if __name__ == "__main__":
    unittest.main()

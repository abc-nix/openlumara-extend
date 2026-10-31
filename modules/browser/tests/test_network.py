# Tests for network_requests: entry type capture, the pure filter and the
# tool's default / static / filter / clear behaviour.
#
# Run from anywhere:
#   python3 -m unittest discover -s modules/browser/tests -v

import asyncio
import unittest
from collections import deque

from test_module_events import module, make_browser
from _browser_module_pkg import util

ENTRIES = [
    {"url": "https://x.test/app.js", "type": "Script", "status": 200},
    {"url": "https://x.test/broken.js", "type": "Script", "status": 404},
    {"url": "https://x.test/pic.png", "type": "Image", "status": 200},
    {"url": "https://x.test/api/data", "type": "XHR", "status": 200},
    {"url": "https://x.test/doc", "type": "Document", "status": 200},
    {"url": "https://ads.test/sync", "type": "Fetch", "status": 200},
]


class FilterRequestsTests(unittest.TestCase):
    def test_successful_statics_are_hidden_and_failed_ones_kept(self):
        kept, hidden = util.filter_requests(ENTRIES)
        urls = [entry["url"] for entry in kept]
        self.assertNotIn("https://x.test/app.js", urls)
        self.assertNotIn("https://x.test/pic.png", urls)
        self.assertIn("https://x.test/broken.js", urls)
        self.assertEqual(hidden, {"static": 2, "other": 0})

    def test_including_statics_shows_everything(self):
        kept, hidden = util.filter_requests(ENTRIES, skip_static=False)
        self.assertEqual(len(kept), 6)
        self.assertEqual(hidden["static"], 0)

    def test_a_url_pattern_narrows_the_list(self):
        kept, hidden = util.filter_requests(ENTRIES, pattern=r"/api/")
        self.assertEqual([entry["url"] for entry in kept], ["https://x.test/api/data"])
        self.assertEqual(hidden["other"], 5)

    def test_an_invalid_regex_falls_back_to_substring(self):
        kept, _ = util.filter_requests(ENTRIES, pattern="ads")
        self.assertEqual([entry["url"] for entry in kept], ["https://ads.test/sync"])

    def test_types_accept_a_comma_string(self):
        kept, _ = util.filter_requests(ENTRIES, types="xhr, document")
        self.assertEqual({entry["type"] for entry in kept}, {"XHR", "Document"})

    def test_types_accept_a_list(self):
        kept, _ = util.filter_requests(ENTRIES, types=["XHR"])
        self.assertEqual(len(kept), 1)


def _browser(entries=None):
    browser = make_browser(config_values={"enable_network_monitoring": True})

    async def fake_connect():
        return True

    browser._ensure_connection = fake_connect
    browser._network_log = deque(entries if entries is not None else list(ENTRIES),
                                 maxlen=500)
    return browser


def _call(browser, **kwargs):
    return asyncio.run(module.Browser.network_requests.__wrapped__(browser, **kwargs))


class NetworkRequestsToolTests(unittest.TestCase):
    def test_the_default_hides_static_successes_and_says_so(self):
        result = _call(_browser())
        content = result["data"]["web_content"]
        self.assertEqual(content["total"], 6)
        self.assertEqual(content["shown"], 4)
        self.assertEqual(content["hidden"], {"static": 2, "other": 0})

    def test_static_true_restores_them(self):
        content = _call(_browser(), static=True)["data"]["web_content"]
        self.assertEqual(content["shown"], 6)

    def test_a_filter_is_applied_and_counted(self):
        content = _call(_browser(), filter="api")["data"]["web_content"]
        self.assertEqual(content["shown"], 1)
        self.assertEqual(content["hidden"]["other"], 5)

    def test_count_slices_after_filtering(self):
        content = _call(_browser(), count=2)["data"]["web_content"]
        self.assertEqual(content["matched"], 4)
        self.assertEqual(content["shown"], 2)
        self.assertEqual(content["total"], 6)

    def test_a_filter_reaches_entries_older_than_the_count_window(self):
        # the page's own requests are the oldest in the buffer: filtering has
        # to see them even when only the last two results are asked for
        content = _call(_browser(), count=2, filter="api")["data"]["web_content"]
        self.assertEqual(content["shown"], 1)
        self.assertEqual(content["matched"], 1)

    def test_count_zero_returns_no_entries_but_keeps_the_counts(self):
        content = _call(_browser(), count=0)["data"]["web_content"]
        self.assertEqual(content["requests"], [])
        self.assertEqual(content["matched"], 4)

    def test_clear_empties_the_buffer(self):
        browser = _browser()
        _call(browser, clear=True)
        self.assertEqual(list(browser._network_log), [])

    def test_an_empty_buffer_says_so(self):
        result = _call(_browser(entries=[]))
        self.assertEqual(result["data"], "No network requests recorded.")


class RequestTypeCaptureTests(unittest.TestCase):
    def test_the_resource_type_is_recorded_with_the_request(self):
        browser = _browser(entries=[])
        browser._on_cdp_event({
            "method": "Network.requestWillBeSent", "sessionId": "S1",
            "params": {"requestId": "R1", "timestamp": 1.0, "type": "Script",
                       "request": {"method": "GET", "url": "https://x.test/app.js"}},
        })
        self.assertEqual(list(browser._network_log)[-1]["type"], "Script")


if __name__ == "__main__":
    unittest.main()

# Tests for logic that lives in module.py (event handling, tool plumbing).
#
# module.py imports core, aiohttp and the webui channel, so it is loaded
# through stubs - the same trick test_page.py uses for cdp/page, one level up.
# Only pure in-process behaviour is tested here: no browser, no connection.
#
# Run from anywhere:
#   python3 -m unittest discover -s modules/browser/tests -v

import asyncio
import importlib
import sys
import types
import unittest
from collections import deque
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]
PKG = "_browser_module_pkg"


class _Config:
    """Stand-in for core.config: reads from a dict, else the default."""

    def __init__(self, values=None):
        self.values = values or {}

    def get(self, key=None, default=None, **kwargs):
        if key in self.values:
            return self.values[key]
        return default


def _load_module():
    if PKG in sys.modules:
        return sys.modules[PKG]

    sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))

    core = types.ModuleType("core")
    core.detail_error = lambda e: f"{type(e).__name__}: {e}"
    core.get_data_path = lambda subpath=None: str(MODULE_DIR)

    config = types.ModuleType("core.config")
    config.get = lambda section=None, key=None, default=None, **kw: default
    core.config = config

    module_stub = types.ModuleType("core.module")

    class Module:
        def result(self, data=None, success=True):
            return {"data": data, "success": success}

        def log(self, *args, **kwargs):
            pass

    def tool(fn):
        fn._is_tool = True
        return fn

    module_stub.Module = Module
    module_stub.tool = tool
    core.module = module_stub

    channels = types.ModuleType("channels")
    webui = types.ModuleType("channels.webui")
    api = types.ModuleType("channels.webui.api")

    def route(name, method="GET"):
        return lambda fn: fn

    api.route = route
    channels.webui = webui
    webui.api = api

    sys.modules.update({
        "core": core, "core.config": config, "core.module": module_stub,
        "channels": channels, "channels.webui": webui,
        "channels.webui.api": api,
    })

    pkg = types.ModuleType(PKG)
    pkg.__path__ = [str(MODULE_DIR)]
    sys.modules[PKG] = pkg
    importlib.import_module(f"{PKG}.module")
    return pkg


_load_module()
module = importlib.import_module(f"{PKG}.module")


def make_browser(attached=None, urls=None, config_values=None):
    """A Browser instance with just the state the pieces under test touch."""
    browser = module.Browser.__new__(module.Browser)
    browser._attached = dict(attached or {"T1": "S1"})
    browser._current_target = next(iter(browser._attached), None)
    browser._refs_by_tab = {}
    browser._pending_dialog = None
    browser._load_event = asyncio.Event()
    browser._console_log = deque(maxlen=50)
    browser._network_log = deque(maxlen=50)
    browser._net_pending = {}
    browser._target_url = dict(urls or {})
    browser.config = _Config(config_values)
    return browser


class EventAttributionTests(unittest.TestCase):
    """B29: a log entry says which tab and page produced it."""

    def _console(self, browser, sid="S1", value="hi"):
        browser._on_cdp_event({
            "method": "Runtime.consoleAPICalled",
            "sessionId": sid,
            "params": {"type": "log", "args": [{"value": value}], "timestamp": 1},
        })
        return list(browser._console_log)[-1]

    def test_console_entry_names_the_tab_and_page(self):
        browser = make_browser(urls={"T1": "https://a.test/"})
        entry = self._console(browser)
        self.assertEqual(entry["tab"], "T1")
        self.assertEqual(entry["page"], "https://a.test/")

    def test_page_is_the_url_at_event_time_not_the_current_one(self):
        # the tab navigates after the message was logged: the entry must keep
        # the page it came from instead of inheriting the new one
        browser = make_browser(urls={"T1": "https://a.test/"})
        self._console(browser, value="from a.test")
        browser._on_cdp_event({"method": "Target.targetInfoChanged", "params": {
            "targetInfo": {"targetId": "T1", "type": "page", "url": "https://b.test/"}}})
        self._console(browser, value="from b.test")
        first, second = list(browser._console_log)
        self.assertEqual(first["page"], "https://a.test/")
        self.assertEqual(second["page"], "https://b.test/")

    def test_unknown_session_is_tagged_as_unknown(self):
        browser = make_browser(urls={"T1": "https://a.test/"})
        entry = self._console(browser, sid="S9")
        self.assertIsNone(entry["tab"])
        self.assertIsNone(entry["page"])

    def test_log_entry_prefers_its_own_source_url(self):
        browser = make_browser(urls={"T1": "https://a.test/"})
        browser._on_cdp_event({
            "method": "Log.entryAdded", "sessionId": "S1",
            "params": {"entry": {"level": "error", "text": "boom",
                                 "url": "https://cdn.test/x.js"}},
        })
        self.assertEqual(list(browser._console_log)[-1]["page"], "https://cdn.test/x.js")

    def test_network_entry_is_tagged_too(self):
        browser = make_browser(urls={"T1": "https://a.test/"},
                               config_values={"enable_network_monitoring": True})
        browser._on_cdp_event({
            "method": "Network.requestWillBeSent", "sessionId": "S1",
            "params": {"requestId": "R1", "timestamp": 10.0,
                       "request": {"method": "GET", "url": "https://a.test/app.js"}},
        })
        entry = list(browser._network_log)[-1]
        self.assertEqual(entry["tab"], "T1")
        self.assertEqual(entry["page"], "https://a.test/")
        self.assertEqual(entry["url"], "https://a.test/app.js")

    def test_destroyed_tab_forgets_its_url(self):
        browser = make_browser(urls={"T1": "https://a.test/"})
        browser._on_cdp_event({"method": "Target.targetDestroyed",
                               "params": {"targetId": "T1"}})
        self.assertNotIn("T1", browser._target_url)


class TabRefsTests(unittest.TestCase):
    """B18: a snapshot of one tab must not clobber another tab's refs."""

    def test_refs_are_per_tab(self):
        browser = make_browser(attached={"T1": "S1", "T2": "S2"})
        browser._refs_by_tab = {"T1": {"@e1": 11}, "T2": {"@e1": 22}}
        browser._current_target = "T1"
        self.assertEqual(browser._tab_refs(), {"@e1": 11})
        browser._current_target = "T2"
        self.assertEqual(browser._tab_refs(), {"@e1": 22})

    def test_never_snapshotted_tab_has_no_refs(self):
        browser = make_browser()
        browser._current_target = "T1"
        self.assertEqual(browser._tab_refs(), {})

    def test_navigation_drops_only_that_tab_refs(self):
        browser = make_browser(attached={"T1": "S1", "T2": "S2"},
                               urls={"T1": "https://a.test/", "T2": "https://b.test/"})
        browser._refs_by_tab = {"T1": {"@e1": 11}, "T2": {"@e1": 22}}
        browser._on_cdp_event({
            "method": "Page.frameNavigated", "sessionId": "S1",
            "params": {"frame": {"url": "https://a.test/next"}},
        })
        self.assertNotIn("T1", browser._refs_by_tab)
        self.assertIn("T2", browser._refs_by_tab)
        # the tab's URL is updated from the navigation, not just from targetInfo
        self.assertEqual(browser._target_url["T1"], "https://a.test/next")

    def test_subframe_navigation_keeps_refs(self):
        browser = make_browser(urls={"T1": "https://a.test/"})
        browser._refs_by_tab = {"T1": {"@e1": 11}}
        browser._on_cdp_event({
            "method": "Page.frameNavigated", "sessionId": "S1",
            "params": {"frame": {"url": "https://a.test/iframe", "parentId": "F0"}},
        })
        self.assertIn("T1", browser._refs_by_tab)

    def test_destroyed_tab_drops_its_refs(self):
        browser = make_browser(attached={"T1": "S1"})
        browser._refs_by_tab = {"T1": {"@e1": 11}}
        browser._on_cdp_event({"method": "Target.targetDestroyed",
                               "params": {"targetId": "T1"}})
        self.assertNotIn("T1", browser._refs_by_tab)


class _AwaitRejectingCDP:
    """Answers like Chrome does for a script with a top-level await."""

    def __init__(self):
        self.calls = []

    async def cmd(self, method, params=None, session_id=None, timeout=None):
        expression = (params or {}).get("expression", "")
        self.calls.append(expression)
        if "(async () =>" in expression:
            return {"result": {"value": 42}}
        if "await" in expression:
            description = ("SyntaxError: await is only valid in async functions "
                           "and the top level bodies of modules")
            return {"exceptionDetails": {"text": "Uncaught " + description,
                                         "exception": {"description": description}}}
        return {"result": {"value": 1}}


class EvaluateAwaitTests(unittest.TestCase):
    """B30: a top-level await is retried wrapped, instead of failing."""

    def _run(self, expression):
        browser = make_browser()

        async def fake_connect():
            return True

        browser._ensure_connection = fake_connect
        browser._current_session = lambda: "S1"
        browser._cdp = _AwaitRejectingCDP()
        result = asyncio.run(module.Browser.evaluate.__wrapped__(browser, expression))
        return result, browser._cdp.calls

    def test_top_level_await_is_retried_wrapped(self):
        result, calls = self._run("await Promise.resolve(42)")
        self.assertEqual(result["data"], 42)
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[1].startswith("(async () =>"))

    def test_plain_expression_is_sent_once_and_untouched(self):
        result, calls = self._run("1")
        self.assertEqual(result["data"], 1)
        self.assertEqual(calls, ["1"])


if __name__ == "__main__":
    unittest.main()

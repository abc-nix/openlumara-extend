# Tests for the <select> branch: the JS helper in page.py and type_text using
# it instead of typing (fake CDP, no browser).
#
# Run from anywhere:
#   python3 -m unittest discover -s modules/browser/tests -v

import asyncio
import json
import unittest

from test_module_events import module, make_browser
from _browser_module_pkg import page


class _SelectCDP:
    """Answers DOM/Runtime calls; the select helper's reply is configurable."""

    def __init__(self, tag="SELECT", reply=None, fail=False):
        self.tag = tag
        self.reply = reply
        self.fail = fail
        self.calls = []

    async def cmd(self, method, params=None, session_id=None, timeout=None):
        params = dict(params or {})
        self.calls.append((method, params))
        if self.fail:
            raise RuntimeError("boom")
        if method == "Runtime.callFunctionOn":
            if "tagName" in params.get("functionDeclaration", ""):
                return {"result": {"value": self.tag}}
            return {"result": {"value": json.dumps(self.reply or {})}}
        if method == "DOM.getDocument":
            return {"root": {"nodeId": 1}}
        if method == "DOM.querySelector":
            return {"nodeId": 2}
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "obj1"}}
        return {}


class SelectOptionHelperTests(unittest.TestCase):
    def test_the_wanted_text_is_passed_and_the_answer_parsed(self):
        cdp = _SelectCDP(reply={"ok": True, "text": "Spain", "value": "ES"})
        out = asyncio.run(page.select_option(cdp, "obj1", "Spain", "S1"))
        self.assertEqual(out, {"ok": True, "text": "Spain", "value": "ES"})
        self.assertEqual(cdp.calls[0][1]["arguments"], [{"value": "Spain"}])

    def test_a_miss_returns_the_labels_it_saw(self):
        cdp = _SelectCDP(reply={"ok": False, "labels": ["France", "Spain"]})
        out = asyncio.run(page.select_option(cdp, "obj1", "Narnia", "S1"))
        self.assertFalse(out["ok"])
        self.assertEqual(out["labels"], ["France", "Spain"])

    def test_a_broken_call_reads_as_no_answer(self):
        cdp = _SelectCDP(fail=True)
        self.assertEqual(asyncio.run(page.select_option(cdp, "obj1", "x", "S1")), {})


def _type_text(target, text, **kwargs):
    browser = make_browser()

    async def fake_connect():
        return True

    browser._ensure_connection = fake_connect
    browser._cdp = _SelectCDP(**kwargs)
    result = asyncio.run(module.Browser.type_text.__wrapped__(browser, target, text))
    return result, browser._cdp.calls


class TypeTextSelectTests(unittest.TestCase):
    def test_a_select_gets_its_option_picked(self):
        result, calls = _type_text("#country", "Spain",
                                   reply={"ok": True, "text": "Spain", "value": "ES"})
        self.assertEqual(result["data"], "Selected 'Spain' in #country.")
        sent = [params for method, params in calls
                if params.get("arguments") == [{"value": "Spain"}]]
        self.assertEqual(len(sent), 1)

    def test_a_select_is_not_typed_into(self):
        _, calls = _type_text("#country", "Spain",
                              reply={"ok": True, "text": "Spain", "value": "ES"})
        methods = [method for method, _ in calls]
        self.assertNotIn("Input.insertText", methods)
        self.assertNotIn("Input.dispatchKeyEvent", methods)

    def test_a_missing_option_names_the_first_few(self):
        result, _ = _type_text("#country", "Narnia",
                               reply={"ok": False,
                                      "labels": ["France", "Germany", "Italy", "Spain"]})
        self.assertFalse(result["success"])
        self.assertIn("Narnia", result["data"])
        self.assertIn("France, Germany, Italy", result["data"])
        self.assertNotIn("Spain", result["data"])

    def test_an_unreadable_select_says_so(self):
        result, _ = _type_text("#country", "Spain", reply={})
        self.assertFalse(result["success"])
        self.assertIn("#country", result["data"])

    def test_an_input_still_goes_through_typing(self):
        result, calls = _type_text("#name", "Ada", tag="INPUT")
        self.assertEqual(result["data"], "Typed 3 characters into #name.")
        methods = [method for method, _ in calls]
        self.assertIn("Input.insertText", methods)
        insert = [params for method, params in calls if method == "Input.insertText"]
        self.assertEqual(insert[0]["text"], "Ada")


if __name__ == "__main__":
    unittest.main()

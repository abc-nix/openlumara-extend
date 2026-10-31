# Tests for fill_form: several fields in one call, each reported on its own
# (fake CDP, no browser).
#
# Run from anywhere:
#   python3 -m unittest discover -s modules/browser/tests -v

import asyncio
import json
import unittest

from test_module_events import module, make_browser
from _browser_module_pkg import page


class _FormCDP:
    """DOM/Runtime answers for a set of selectors with a per-element spec."""

    def __init__(self, elements=None):
        self.elements = elements or {}
        order = list(self.elements)
        self.node_ids = {selector: index + 1 for index, selector in enumerate(order)}
        self.selectors = {f"obj-{index + 1}": selector
                          for index, selector in enumerate(order)}
        self.calls = []

    async def cmd(self, method, params=None, session_id=None, timeout=None):
        params = dict(params or {})
        self.calls.append((method, params))
        if method == "DOM.getDocument":
            return {"root": {"nodeId": 1}}
        if method == "DOM.querySelector":
            node = self.node_ids.get(params.get("selector"))
            return {"nodeId": node or 0}
        if method == "DOM.resolveNode":
            return {"object": {"objectId": f"obj-{params.get('nodeId')}"}}
        if method == "Runtime.callFunctionOn":
            spec = self.elements.get(self.selectors.get(params.get("objectId")), {})
            fn = params.get("functionDeclaration", "")
            if "HTMLSelectElement" in fn:
                return {"result": {"value": json.dumps(spec.get("select") or {})}}
            if "this.checked" in fn:
                reply = spec.get("checked") or {"checked": True}
                return {"result": {"value": json.dumps(reply)}}
            if "this.tagName === 'INPUT'" in fn:
                return {"result": {"value": spec.get("type", "text")}}
            if "this.tagName" in fn:
                return {"result": {"value": spec.get("tag", "DIV")}}
            return {"result": {}}
        return {}


def _fill(fields, elements=None):
    browser = make_browser()

    async def fake_connect():
        return True

    browser._ensure_connection = fake_connect
    browser._cdp = _FormCDP(elements)
    result = asyncio.run(module.Browser.fill_form.__wrapped__(browser, fields))
    return result, browser._cdp.calls


class FillFormTests(unittest.TestCase):
    def test_fields_are_filled_in_order_and_reported(self):
        result, _ = _fill(
            [{"target": "#name", "text": "Ada"},
             {"target": "#country", "text": "Spain"}],
            {"#name": {"tag": "INPUT", "type": "text"},
             "#country": {"tag": "SELECT",
                          "select": {"ok": True, "text": "Spain", "value": "ES"}}})
        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["filled"], 2)
        self.assertEqual(result["data"]["failed"], 0)
        messages = [entry["message"] for entry in result["data"]["results"]]
        self.assertEqual(messages, ["Typed 3 characters into #name.",
                                    "Selected 'Spain' in #country."])

    def test_the_typed_value_reaches_the_page(self):
        _, calls = _fill([{"target": "#name", "text": "Ada"}],
                         {"#name": {"tag": "INPUT", "type": "text"}})
        inserted = [params for method, params in calls if method == "Input.insertText"]
        self.assertEqual(inserted[0]["text"], "Ada")

    def test_one_bad_field_does_not_stop_the_rest(self):
        result, _ = _fill(
            [{"target": "#name", "text": "Ada"},
             {"target": "#nope", "text": "x"},
             {"target": "#city", "text": "Malaga"}],
            {"#name": {"tag": "INPUT", "type": "text"},
             "#city": {"tag": "INPUT", "type": "text"}})
        self.assertFalse(result["success"])
        self.assertEqual(result["data"]["filled"], 2)
        self.assertEqual(result["data"]["failed"], 1)
        self.assertIn("No element found for selector '#nope'",
                      result["data"]["results"][1]["message"])
        self.assertEqual(result["data"]["results"][2]["message"],
                         "Typed 6 characters into #city.")

    def test_a_checkbox_takes_a_real_boolean(self):
        result, _ = _fill([{"target": "#tos", "text": True}],
                          {"#tos": {"tag": "INPUT", "type": "checkbox",
                                    "checked": {"checked": True}}})
        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["results"][0]["message"], "Checked #tos.")

    def test_a_checkbox_refuses_a_non_boolean(self):
        result, _ = _fill([{"target": "#tos", "text": "maybe"}],
                          {"#tos": {"tag": "INPUT", "type": "checkbox"}})
        self.assertFalse(result["success"])
        self.assertIn("Use true or false for #tos.",
                      result["data"]["results"][0]["message"])

    def test_a_radio_can_be_switched_off(self):
        result, _ = _fill([{"target": "#news", "text": "no"}],
                          {"#news": {"tag": "INPUT", "type": "radio",
                                     "checked": {"checked": False}}})
        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["results"][0]["message"], "Unchecked #news.")

    def test_a_json_string_is_accepted(self):
        result, _ = _fill(json.dumps([{"target": "#name", "text": "Ada"}]),
                          {"#name": {"tag": "INPUT", "type": "text"}})
        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["filled"], 1)

    def test_an_item_without_a_target_is_reported(self):
        result, _ = _fill([{"text": "orphan"}])
        self.assertFalse(result["success"])
        self.assertEqual(result["data"]["results"][0]["message"], "No target.")

    def test_a_non_list_is_refused(self):
        result, calls = _fill(42)
        self.assertFalse(result["success"])
        self.assertEqual(calls, [])

    def test_a_ref_name_works_as_the_target(self):
        result, _ = _fill([{"ref": "#name", "value": "Ada"}],
                          {"#name": {"tag": "INPUT", "type": "text"}})
        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["results"][0]["target"], "#name")


class SetCheckedMechanismTests(unittest.TestCase):
    def test_the_state_is_toggled_with_a_real_click(self):
        # assigning .checked leaves React's state stale: demoqa's submit showed
        # an empty Gender row until the element was clicked instead
        self.assertIn("this.click()", page._SET_CHECKED_JS)

    def test_clicking_is_skipped_when_the_state_already_matches(self):
        self.assertIn("if(this.checked===want)", page._SET_CHECKED_JS)


if __name__ == "__main__":
    unittest.main()

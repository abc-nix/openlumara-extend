# Tests for key chords: util.split_chord, page.key_definition and the event
# sequence the press tool sends (fake CDP, no browser).
#
# Run from anywhere:
#   python3 -m unittest discover -s modules/browser/tests -v

import asyncio
import unittest

from test_module_events import module, make_browser
from _browser_module_pkg import page, util


class SplitChordTests(unittest.TestCase):
    def test_plus_separated_chord(self):
        self.assertEqual(util.split_chord("Control+A"), (["control"], "A"))

    def test_lowercase_alias(self):
        self.assertEqual(util.split_chord("ctrl+shift+tab"),
                         (["control", "shift"], "tab"))

    def test_modifiers_in_their_own_argument(self):
        self.assertEqual(util.split_chord("a", "control shift"),
                         (["control", "shift"], "a"))

    def test_modifiers_as_a_list(self):
        self.assertEqual(util.split_chord("ArrowUp", ["alt"]), (["alt"], "ArrowUp"))

    def test_cmd_is_meta(self):
        self.assertEqual(util.split_chord("cmd+a"), (["meta"], "a"))

    def test_order_is_canonical(self):
        self.assertEqual(util.split_chord("shift+ctrl+a"), (["control", "shift"], "a"))

    def test_a_modifier_alone_is_not_a_key(self):
        with self.assertRaises(ValueError):
            util.split_chord("control")

    def test_two_keys_are_refused(self):
        with self.assertRaises(ValueError):
            util.split_chord("a b")

    def test_unknown_modifier_is_named(self):
        with self.assertRaises(ValueError) as ctx:
            util.split_chord("hyper+a")
        self.assertIn("hyper", str(ctx.exception))


class KeyDefinitionTests(unittest.TestCase):
    def test_named_keys_come_from_the_map(self):
        self.assertEqual(page.key_definition("enter"), (13, "Enter", "Enter"))
        self.assertEqual(page.key_definition("space"), (32, " ", "Space"))
        self.assertEqual(page.key_definition("TAB"), (9, "Tab", "Tab"))

    def test_a_letter_is_keyed_by_its_character(self):
        self.assertEqual(page.key_definition("a"), (65, "a", "KeyA"))
        self.assertEqual(page.key_definition("A"), (65, "A", "KeyA"))

    def test_a_digit_is_keyed_by_its_character(self):
        self.assertEqual(page.key_definition("7"), (55, "7", "Digit7"))

    def test_anything_else_is_unknown(self):
        self.assertIsNone(page.key_definition("abc"))
        self.assertIsNone(page.key_definition(""))


class _KeyCDP:
    """Records the key events press sends."""

    def __init__(self):
        self.events = []

    async def cmd(self, method, params=None, session_id=None, timeout=None):
        self.events.append((method, dict(params or {})))
        return {}


def _press(key, modifiers=None):
    browser = make_browser()

    async def fake_connect():
        return True

    browser._ensure_connection = fake_connect
    browser._cdp = _KeyCDP()
    result = asyncio.run(module.Browser.press.__wrapped__(browser, key, modifiers))
    keys = [(params.get("type"), params.get("key"))
            for method, params in browser._cdp.events
            if method == "Input.dispatchKeyEvent"]
    return result, keys, browser._cdp.events


class PressChordTests(unittest.TestCase):
    def test_a_chord_holds_and_releases_the_modifier(self):
        result, keys, events = _press("Control+A")
        self.assertEqual(result["data"], "Pressed Control+A.")
        self.assertEqual(keys, [("keyDown", "Control"), ("keyDown", "a"),
                                ("keyUp", "a"), ("keyUp", "Control")])

    def test_the_modifier_bit_travels_with_each_event(self):
        _, _, events = _press("Control+A")
        masks = [params.get("modifiers") for method, params in events
                 if method == "Input.dispatchKeyEvent"]
        self.assertEqual(masks, [2, 2, 2, 0])

    def test_a_control_chord_inserts_no_character(self):
        _, _, events = _press("Control+A")
        self.assertNotIn("text", events[1][1])

    def test_shift_keeps_the_letter_case(self):
        _, keys, events = _press("Shift+A")
        self.assertEqual(keys, [("keyDown", "Shift"), ("keyDown", "A"),
                                ("keyUp", "A"), ("keyUp", "Shift")])
        self.assertEqual(events[1][1].get("text"), "A")

    def test_a_plain_letter_still_inserts_its_character(self):
        _, keys, events = _press("a")
        self.assertEqual(keys, [("keyDown", "a"), ("keyUp", "a")])
        self.assertEqual(events[0][1].get("text"), "a")

    def test_enter_carries_a_carriage_return(self):
        _, _, events = _press("enter")
        self.assertEqual(events[0][1].get("text"), "\r")

    def test_tab_carries_no_character(self):
        _, _, events = _press("Shift+Tab")
        self.assertNotIn("text", events[1][1])

    def test_f5_reloads_instead_of_dispatching(self):
        result, keys, events = _press("f5")
        self.assertEqual(result["data"], "Reloaded the page (F5).")
        self.assertEqual(keys, [])
        self.assertEqual(events[0][0], "Page.reload")

    def test_f5_with_modifiers_is_refused(self):
        result, keys, _ = _press("Control+f5")
        self.assertFalse(result["success"])
        self.assertIn("f5", result["data"])
        self.assertEqual(keys, [])

    def test_an_unknown_key_names_itself(self):
        result, keys, _ = _press("abc")
        self.assertFalse(result["success"])
        self.assertIn("abc", result["data"])
        self.assertEqual(keys, [])


if __name__ == "__main__":
    unittest.main()

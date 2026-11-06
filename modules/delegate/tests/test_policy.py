"""delegate start/detach policy tests — no OpenLumara boot.

policy.py is pure functions, so the two decisions that guard the module's
background rules are tested directly: may a start take a background slot, and
may a run detach at the caller's tool deadline.

Run from the repo root:

    python3 -m unittest discover -s modules/delegate/tests -v
"""

import sys
import unittest
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(MODULE_DIR.parent))

from delegate.policy import (  # noqa: E402
    running_count,
    same_model,
    should_detach,
    slots_left,
    start_refusal,
)


class SlotsTest(unittest.TestCase):
    def test_count_accepts_dicts_sets_and_numbers(self):
        self.assertEqual(running_count({}), 0)
        self.assertEqual(running_count({"a": 1, "b": 2}), 2)
        self.assertEqual(running_count(2), 2)
        self.assertEqual(running_count(None), 0)

    def test_slots_never_negative(self):
        self.assertEqual(slots_left(1, {}), 1)
        self.assertEqual(slots_left(1, {"a": 1}), 0)
        self.assertEqual(slots_left(1, {"a": 1, "b": 2}), 0)

    def test_cap_above_one(self):
        self.assertEqual(slots_left(3, {"a": 1, "b": 2}), 1)

    def test_bad_cap_still_means_one_slot(self):
        self.assertEqual(slots_left(0, {}), 1)
        self.assertEqual(slots_left("nonsense", {}), 1)


class SameModelTest(unittest.TestCase):
    def test_case_and_whitespace_insensitive(self):
        self.assertTrue(same_model("Qwen3.5-9B ", "qwen3.5-9b"))
        self.assertFalse(same_model("qwen3.5-9b", "gemma-4-31b-it-qat"))
        self.assertFalse(same_model(None, "qwen3.5-9b"))
        self.assertTrue(same_model("", None))


class StartRefusalTest(unittest.TestCase):
    def test_foreground_always_allowed(self):
        # a foreground run blocks the turn, so neither the cap nor the model rule applies
        self.assertIsNone(start_refusal(False, 1, {"a": 1}, "other-model", "current"))

    def test_background_needs_a_free_slot(self):
        message = start_refusal(True, 1, {"a": 1}, "current", "current")
        self.assertIsNotNone(message)
        self.assertIn("slot", message)
        self.assertIn("foreground", message)
        # plural / singular wording follows the cap
        self.assertIn("all 1 background delegate slot is in use", message)
        self.assertIn("all 2 background delegate slots are in use",
                      start_refusal(True, 2, {"a": 1, "b": 2}, "current", "current"))

    def test_background_free_slot_same_model_allowed(self):
        self.assertIsNone(start_refusal(True, 1, {}, "current", "current"))
        self.assertIsNone(start_refusal(True, 1, {}, "CURRENT ", "current"))

    def test_background_different_model_refused(self):
        message = start_refusal(True, 1, {}, "qwen3.5-9b", "gemma-4-31b-it-qat")
        self.assertIsNotNone(message)
        self.assertIn("current model", message)
        self.assertIn("gemma-4-31b-it-qat", message)   # the chat's model
        self.assertIn("qwen3.5-9b", message)           # the one that was asked for
        self.assertIn("foreground", message)

    def test_slot_check_runs_before_the_model_check(self):
        # both rules broken: the message names the cap (the actionable one)
        message = start_refusal(True, 1, {"a": 1}, "qwen3.5-9b", "gemma-4-31b-it-qat")
        self.assertIn("slot", message)


class ShouldDetachTest(unittest.TestCase):
    def test_no_slot_never_detaches(self):
        self.assertFalse(should_detach(None, 0))
        self.assertFalse(should_detach(9.0, 0))

    def test_silent_run_detaches_with_a_slot(self):
        self.assertTrue(should_detach(2.5, 1))

    def test_run_without_any_tool_call_detaches(self):
        self.assertTrue(should_detach(None, 1))

    def test_tool_call_in_flight_keeps_blocking(self):
        self.assertFalse(should_detach(0.2, 1))
        self.assertFalse(should_detach(1.0, 1))     # threshold is strict

    def test_threshold_is_configurable(self):
        self.assertTrue(should_detach(3.0, 1, quiet_threshold=2.0))
        self.assertFalse(should_detach(1.5, 1, quiet_threshold=2.0))


if __name__ == "__main__":
    unittest.main()

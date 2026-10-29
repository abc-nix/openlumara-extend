# Tests for the per-tool-call time budget (util.Budget / util.grid_fits).
# Pure stdlib, loaded straight from util.py - no package, no core stubs. Run:
#   python3 -m unittest discover -s modules/browser/tests -v
#
# Why these are worth testing: the numbers are the whole point. The framework
# cancels a tool call at core.tool_timeout and answers with a bare "Tool timed
# out after Ns"; every assertion here is about the module answering *first*,
# inside that same number, and about never starting CPU work it cannot finish.

import importlib.util
import time
import unittest
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location("_browser_util_budget", MODULE_DIR / "util.py")
util = importlib.util.module_from_spec(spec)
spec.loader.exec_module(util)


class BudgetTests(unittest.TestCase):
    def test_left_never_goes_negative(self):
        b = util.Budget(0.05)
        time.sleep(0.1)
        self.assertEqual(b.left(), 0.0)

    def test_left_counts_down(self):
        b = util.Budget(5.0)
        first = b.left()
        time.sleep(0.05)
        self.assertLess(b.left(), first)
        self.assertLess(first, 5.0)

    def test_cap_uses_ceiling_when_time_is_left(self):
        b = util.Budget(20.0)
        self.assertEqual(b.cap(10.0), 10.0)

    def test_cap_uses_remaining_when_it_is_tighter(self):
        b = util.Budget(2.0)
        self.assertLess(b.cap(10.0), 2.0)
        self.assertGreater(b.cap(10.0), 1.0)

    def test_cap_never_exceeds_the_whole_budget(self):
        # the property that makes the fix safe: no command may outlive the call
        b = util.Budget(1.0)
        self.assertLessEqual(b.cap(600.0), b.total)

    def test_cap_is_zero_once_time_is_up(self):
        b = util.Budget(0.01)
        time.sleep(0.05)
        self.assertEqual(b.cap(10.0), 0.0)

    def test_zero_timeout_fails_fast_instead_of_hanging(self):
        # wait_for(fut, 0) raises at once: an exhausted budget answers, it does
        # not sit in the CDP call until core kills the tool
        import asyncio

        async def never():
            await asyncio.sleep(5)

        async def run():
            await asyncio.wait_for(never(), 0)

        with self.assertRaises(asyncio.TimeoutError):
            asyncio.run(run())

    def test_total_survives_string_input(self):
        self.assertEqual(util.Budget("4").total, 4.0)


class GridFitsTests(unittest.TestCase):
    def test_small_image_with_time_left_fits(self):
        fits, need = util.grid_fits(1272, 1305, 14.5)
        self.assertTrue(fits)
        self.assertLess(need, 2.0)

    def test_tall_capture_does_not_fit_a_short_budget(self):
        fits, need = util.grid_fits(1272, 12000, 1.0)
        self.assertFalse(fits)
        self.assertGreater(need, 10.0)

    def test_estimate_scales_with_pixels(self):
        _fits, small = util.grid_fits(1000, 1000, 99)
        _fits, big = util.grid_fits(2000, 2000, 99)
        self.assertAlmostEqual(big, small * 4, places=6)

    def test_matches_the_measured_cost_constant(self):
        # 1257x2952 measured 3.1 s end to end on a real capture
        _fits, need = util.grid_fits(1257, 2952, 99)
        expected = 1257 * 2952 / 1e6 * util.GRID_COST_S_PER_MPX
        self.assertAlmostEqual(need, expected, places=6)
        self.assertAlmostEqual(need, 3.1, delta=0.1)

    def test_no_time_left_never_fits(self):
        self.assertFalse(util.grid_fits(10, 10, 0.0)[0])

    def test_garbage_dimensions_do_not_fit(self):
        self.assertEqual(util.grid_fits(None, "x", 10), (False, 0.0))


if __name__ == "__main__":
    unittest.main()

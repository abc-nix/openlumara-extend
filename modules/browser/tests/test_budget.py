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


class ToolBudgetTests(unittest.TestCase):
    def test_budget_is_always_shorter_than_the_call(self):
        # the reserve exists so the module answers before core's timer fires;
        # it must hold at the low end too, where a fixed 0.5 s could equal or
        # swallow the whole call
        for total in (1.0, 2.0, 5.0, 15.0, 60.0):
            self.assertLess(util.tool_budget(total).total, total)

    def test_default_install_reserves_half_a_second(self):
        self.assertAlmostEqual(util.tool_budget(15).total, 14.5)

    def test_reserve_shrinks_with_small_totals(self):
        self.assertAlmostEqual(util.tool_budget(1).total, 0.8)   # min(0.5, 1*0.2)
        self.assertAlmostEqual(util.tool_budget(2).total, 1.6)   # min(0.5, 2*0.2)


class ResolveTimeoutTests(unittest.TestCase):
    def test_explicit_timeout_wins(self):
        self.assertEqual(util.resolve_timeout(3.0, util.Budget(60)), 3.0)

    def test_none_takes_what_the_tool_call_has_left(self):
        self.assertAlmostEqual(util.resolve_timeout(None, util.Budget(10)), 10,
                               delta=0.2)

    def test_none_without_a_budget_uses_the_fallback(self):
        self.assertEqual(util.resolve_timeout(None, None, 7.0), 7.0)

    def test_exhausted_budget_still_gives_a_usable_timeout(self):
        # never hand wait_for a 0: it raises at once with a message that looks
        # like a bug instead of an exhausted budget
        b = util.Budget(0.01)
        time.sleep(0.05)
        self.assertEqual(util.resolve_timeout(None, b), 0.1)


class CallBudgetContextTests(unittest.TestCase):
    def test_no_budget_outside_a_tool_call(self):
        self.assertIsNone(util.CALL_BUDGET.get())

    def test_concurrent_calls_do_not_share_a_deadline(self):
        import asyncio

        async def worker(total, out):
            token = util.CALL_BUDGET.set(util.Budget(total))
            try:
                await asyncio.sleep(0)
                out.append(util.CALL_BUDGET.get().total)
            finally:
                util.CALL_BUDGET.reset(token)

        out = []

        async def main():
            await asyncio.gather(worker(1, out), worker(9, out))

        asyncio.run(main())
        self.assertEqual(sorted(out), [1.0, 9.0])


class CaptureFitsTests(unittest.TestCase):
    def test_the_page_that_was_refused_now_fits(self):
        # 8079 px tall, ~1200 wide = 9.7 Mpx: refused by the old 8000 px height
        # cap, and it is nowhere near the cost of a 40k px article
        self.assertTrue(util.capture_fits(1200, 8079, 25_000_000)[0])

    def test_a_whole_article_does_not_fit(self):
        fits, mpx = util.capture_fits(1200, 40415, 25_000_000)
        self.assertFalse(fits)
        self.assertAlmostEqual(mpx, 48.5, places=1)

    def test_area_not_height(self):
        # tall and narrow costs little; short and very wide costs a lot
        self.assertTrue(util.capture_fits(300, 40000, 25_000_000)[0])
        self.assertFalse(util.capture_fits(20000, 2000, 25_000_000)[0])

    def test_garbage_does_not_fit(self):
        self.assertEqual(util.capture_fits(None, "x", 1), (False, 0.0))


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

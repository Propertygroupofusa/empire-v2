"""The daily sweep's call site in main.py.

The sweep's own rules are tested in test_rotation_daily_sweep.py. What is
asserted here is the thing that file cannot see: that something actually
calls it, on a cadence, and that a failure in it can never take down the
loop or the startup around it.
"""
import ast
import pathlib
import unittest

SRC = pathlib.Path(__file__).with_name("main.py").read_text()
TREE = ast.parse(SRC)


def _fn(name, tree=TREE):
    for n in ast.walk(tree):
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == name:
            return n
    return None


class WiringTests(unittest.TestCase):
    def test_the_loop_function_exists_and_is_async(self):
        f = _fn("_daily_idle_sweep_loop")
        self.assertIsNotNone(f, "_daily_idle_sweep_loop is missing from main.py")
        self.assertIsInstance(f, ast.AsyncFunctionDef)

    def test_something_actually_starts_it(self):
        self.assertIn("asyncio.create_task(_daily_idle_sweep_loop())", SRC)

    def test_it_calls_the_sweep(self):
        body = ast.unparse(_fn("_daily_idle_sweep_loop"))
        self.assertIn("run_daily_idle_sweep", body)

    def test_it_repeats_rather_than_running_once(self):
        f = _fn("_daily_idle_sweep_loop")
        self.assertTrue(any(isinstance(n, ast.While) for n in ast.walk(f)),
                        "a one-shot call would only ever sweep on the boot it ran")

    def test_it_waits_before_the_first_look(self):
        body = ast.unparse(_fn("_daily_idle_sweep_loop"))
        self.assertIn("asyncio.sleep(300)", body,
                      "a cold read at boot measures nothing")

    def test_it_rechecks_hourly_not_daily(self):
        """The 24h gate is the DATABASE's, not the sleep's. An exactly-24h
        sleep would drift past the window after any restart."""
        body = ast.unparse(_fn("_daily_idle_sweep_loop"))
        self.assertIn("asyncio.sleep(3600)", body)
        self.assertNotIn("asyncio.sleep(86400)", body)

    def test_an_error_never_kills_the_loop(self):
        f = _fn("_daily_idle_sweep_loop")
        tries = [n for n in ast.walk(f) if isinstance(n, ast.Try)]
        self.assertTrue(tries, "an unhandled error would end the sweep for the process")
        caught = [h for t in tries for h in t.handlers]
        names = [ast.unparse(h.type) if h.type else "" for h in caught]
        self.assertTrue(any("Exception" in n for n in names))

    def test_cancellation_is_re_raised_not_swallowed(self):
        """Swallowing CancelledError makes a shutdown hang."""
        f = _fn("_daily_idle_sweep_loop")
        body = ast.unparse(f)
        self.assertIn("CancelledError", body)
        self.assertIn("raise", body)

    def test_starting_it_cannot_break_startup(self):
        i = SRC.index("asyncio.create_task(_daily_idle_sweep_loop())")
        window = SRC[max(0, i - 400):i + 400]
        self.assertIn("try:", window)
        self.assertIn("except Exception", window)

    def test_it_does_not_touch_auto_rotate(self):
        f = _fn("_daily_idle_sweep_loop")
        body = ast.unparse(f)
        self.assertNotIn("GRID_AUTO_ROTATE", body)
        self.assertNotIn("set_grid_auto_rotate_active", body)
        self.assertNotIn("auto_rotate", body)

    def test_it_places_no_order_itself(self):
        body = ast.unparse(_fn("_daily_idle_sweep_loop"))
        for forbidden in ("place_market_sell", "place_market_buy", "_submit_order",
                          "close_all_grid_slices", "withdraw_from_grid_branch"):
            self.assertNotIn(forbidden, body,
                             f"the call site must delegate, not call {forbidden}")

    def test_the_boot_rotation_path_is_untouched(self):
        self.assertIn("rotation_task.run_at_boot(_rtg)", SRC)
        self.assertIn("ROTATION_TASK_TICKET unset", SRC)


if __name__ == "__main__":
    unittest.main(verbosity=2)

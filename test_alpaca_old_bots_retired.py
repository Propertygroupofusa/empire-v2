"""The old Alpaca bots are retired: main.py starts none of them.

Run as written: python3 test_alpaca_old_bots_retired.py

The owner, 2026-10-31: "Alpaca needs a spring cleaning." prop_bot, the swing
bot, the wheel, market_brain's runner and the account-wide auto-close lost
money from Sep 21 and have been stopped since Oct 14. What these hold:
  * main.py starts none of their loops - no switch can wake them;
  * their modules still import (the dashboards read their history);
  * the patient stock bot's boot hook is still there, and is the only stock bot.
"""
import ast
import unittest

with open("main.py") as _f:
    MAIN = _f.read()


def _calls(src):
    """Every dotted call target and every Thread(target=...) in the source."""
    out = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Call):
            out.add(ast.unparse(node.func))
            for kw in node.keywords:
                if kw.arg == "target":
                    out.add("target=" + ast.unparse(kw.value))
    return out


CALLS = _calls(MAIN)


class Retired(unittest.TestCase):
    def test_no_old_alpaca_loop_is_started(self):
        for t in ("target=prop_bot_module.run", "target=alpaca_swing_bot_module.run",
                  "target=alpaca_wheel_bot.run", "target=market_brain_runner.run"):
            self.assertNotIn(t, CALLS, t)
        self.assertNotIn("run_auto_close_periodically", CALLS)
        self.assertNotIn("run_auto_close_periodically()", MAIN)

    def test_the_retirement_is_written_down_where_the_loops_were(self):
        self.assertIn("THE OLD ALPACA BOTS ARE RETIRED", MAIN)

    def test_the_patient_bot_is_still_started(self):
        i = MAIN.index("import alpaca_patient")
        hook = MAIN[i:i + 900]
        self.assertIn("alpaca_patient.run_forever()", hook)
        self.assertIn("alpaca_patient_live.run_forever()", hook)


if __name__ == "__main__":
    unittest.main(verbosity=2)

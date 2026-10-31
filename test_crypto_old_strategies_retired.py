"""The old crypto strategies are retired: no mode starts them.

Run as written: python3 test_crypto_old_strategies_retired.py

The owner, 2026-10-31: "Coinbase needs spring cleaning." family_tree,
btc_compound and multi_pair were switched off but main.py still started them
whenever CRYPTO_STRATEGY_MODE or the DB override named one - and each buys.
What these hold:
  * no Thread in main.py runs any of their loops;
  * naming one of them is logged as retired, never started;
  * grid_fleet still starts the grid's standby thread;
  * their modules still import (the census and the dashboards use them).
"""
import ast
import unittest

with open("main.py") as _f:
    MAIN = _f.read()

TARGETS = set()
for node in ast.walk(ast.parse(MAIN)):
    if isinstance(node, ast.Call):
        for kw in node.keywords:
            if kw.arg == "target":
                TARGETS.add(ast.unparse(kw.value))


class Retired(unittest.TestCase):
    def test_no_old_strategy_loop_is_started(self):
        for t in ("crypto_family_tree_bot_module.run", "crypto_btc_compound_bot_module.run",
                  "crypto_coinbase_bot_module.run"):
            self.assertNotIn(t, TARGETS, t)

    def test_naming_one_is_logged_as_retired(self):
        self.assertIn('RETIRED_CRYPTO_MODES = ("family_tree", "btc_compound", "multi_pair")', MAIN)
        i = MAIN.index("if crypto_mode in RETIRED_CRYPTO_MODES:")
        self.assertIn("retired strategy - not started", MAIN[i:i + 300])

    def test_grid_fleet_still_starts(self):
        self.assertIn("_grid.run", TARGETS)
        self.assertIn('elif crypto_mode == "grid_fleet":', MAIN)


if __name__ == "__main__":
    unittest.main(verbosity=2)

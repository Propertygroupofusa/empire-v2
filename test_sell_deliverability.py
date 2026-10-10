#!/usr/bin/env python3
"""Tests for the sell-side deliverability pre-flight.

    python3 test_sell_deliverability.py

The thing under test guards the SELL path, so most of these assert that it
does NOT block. A check that strands a profitable slice is far worse than
the loop it replaces.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sell_deliverability as sd


class Env(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("GRID_SELL_DELIVERABILITY_CHECK")
        os.environ.pop("GRID_SELL_DELIVERABILITY_CHECK", None)   # default ON

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("GRID_SELL_DELIVERABILITY_CHECK", None)
        else:
            os.environ["GRID_SELL_DELIVERABILITY_CHECK"] = self._prev


class TestItBlocksOnlyTheDoomedOrder(Env):
    def test_the_staked_case_is_skipped(self):
        """The live case: ETH owned, staked, zero available."""
        send, state, why = sd.verdict("ETH-USD", 0.19440136, {"ETH": 0.0})
        self.assertFalse(send)
        self.assertEqual(state, sd.SKIP)
        self.assertIn("would reject", why)
        self.assertIn("Nothing is cancelled", why)

    def test_a_covered_sell_is_sent(self):
        send, state, _ = sd.verdict("SOL-USD", 1.5, {"SOL": 1.5})
        self.assertTrue(send)
        self.assertEqual(state, sd.SEND)

    def test_more_than_enough_is_sent(self):
        send, _, _ = sd.verdict("SOL-USD", 1.0, {"SOL": 99.0})
        self.assertTrue(send)

    def test_a_rounding_crumb_is_not_a_shortfall(self):
        qty = 1.55675300
        send, state, _ = sd.verdict("SOL-USD", qty, {"SOL": qty - qty * 1e-12})
        self.assertTrue(send)
        self.assertEqual(state, sd.SEND)

    def test_a_real_partial_shortfall_is_skipped(self):
        """The venue needs the whole size; 90% does not fill a 100% order."""
        send, state, _ = sd.verdict("ETH-USD", 1.0, {"ETH": 0.9})
        self.assertFalse(send)
        self.assertEqual(state, sd.SKIP)


class TestUnknownNeverBlocks(Env):
    def test_unreadable_balance_sends(self):
        send, state, why = sd.verdict("ETH-USD", 1.0, None)
        self.assertTrue(send)
        self.assertEqual(state, sd.UNKNOWN)
        self.assertIn("never blocked", why)

    def test_asset_absent_from_a_readable_map_is_unknown_not_zero(self):
        """available_units_map adds a 0.0 only for a CONFIRMED empty asset,
        so absence means nobody looked."""
        send, state, why = sd.verdict("ETH-USD", 1.0, {"SOL": 5.0})
        self.assertTrue(send)
        self.assertEqual(state, sd.UNKNOWN)
        self.assertIn("UNKNOWN", why)

    def test_a_garbage_balance_value_sends(self):
        send, state, _ = sd.verdict("ETH-USD", 1.0, {"ETH": "not-a-number"})
        self.assertTrue(send)
        self.assertEqual(state, sd.UNKNOWN)

    def test_a_garbage_quantity_sends(self):
        send, state, _ = sd.verdict("ETH-USD", None, {"ETH": 0.0})
        self.assertTrue(send)
        self.assertEqual(state, sd.UNKNOWN)

    def test_a_confirmed_zero_IS_blocked_unlike_an_absent_one(self):
        """The distinction the whole module turns on."""
        self.assertFalse(sd.verdict("ETH-USD", 1.0, {"ETH": 0.0})[0])
        self.assertTrue(sd.verdict("ETH-USD", 1.0, {})[0])


class TestItProbesSoNothingStrands(Env):
    def test_it_sends_one_after_probe_every_skips(self):
        for n in range(0, 19):
            send, state, _ = sd.verdict("ETH-USD", 1.0, {"ETH": 0.0}, n, probe_every=20)
            self.assertFalse(send, n)
        send, state, why = sd.verdict("ETH-USD", 1.0, {"ETH": 0.0}, 19, probe_every=20)
        self.assertTrue(send)
        self.assertEqual(state, sd.PROBE)
        self.assertIn("last word", why)

    def test_probe_every_of_one_never_blocks_at_all(self):
        send, state, _ = sd.verdict("ETH-USD", 1.0, {"ETH": 0.0}, 0, probe_every=1)
        self.assertTrue(send)
        self.assertEqual(state, sd.PROBE)

    def test_the_loop_is_bounded_not_merely_slowed(self):
        """Over 200 cycles - the BCH number - at most 10 orders go out."""
        sent = skips = 0
        for _ in range(200):
            send, state, _ = sd.verdict("ETH-USD", 1.0, {"ETH": 0.0}, skips, probe_every=20)
            if send:
                sent += 1
                skips = 0
            else:
                skips += 1
        self.assertLessEqual(sent, 10)
        self.assertGreater(sent, 0)


class TestTheKillSwitch(unittest.TestCase):
    def test_disabling_restores_previous_behaviour_exactly(self):
        prev = os.environ.get("GRID_SELL_DELIVERABILITY_CHECK")
        try:
            for off in ("0", "false", "no", "off"):
                os.environ["GRID_SELL_DELIVERABILITY_CHECK"] = off
                send, state, _ = sd.verdict("ETH-USD", 1.0, {"ETH": 0.0}, 0)
                self.assertTrue(send, off)
                self.assertEqual(state, sd.SEND)
        finally:
            if prev is None:
                os.environ.pop("GRID_SELL_DELIVERABILITY_CHECK", None)
            else:
                os.environ["GRID_SELL_DELIVERABILITY_CHECK"] = prev


class TestAssetParsing(unittest.TestCase):
    def test_pair_to_asset(self):
        self.assertEqual(sd.asset_of("ETH-USD"), "ETH")
        self.assertEqual(sd.asset_of("eth-usd"), "ETH")
        self.assertEqual(sd.asset_of(" SHIB-USD "), "SHIB")
        self.assertEqual(sd.asset_of(None), "")


class TestItCannotPlaceOrCancelAnything(unittest.TestCase):
    """Structural. Scans what the module CALLS, not what its prose says -
    the first version of this grepped for substrings and failed on the word
    "cancelled" inside a reason string, which is exactly the kind of false
    positive that gets a real safety test deleted instead of fixed."""

    def setUp(self):
        import ast
        self.tree = ast.parse(open(os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "sell_deliverability.py")).read())
        self.ast = ast

    def _called_names(self):
        names = set()
        for n in self.ast.walk(self.tree):
            if isinstance(n, self.ast.Call):
                f = n.func
                if isinstance(f, self.ast.Name):
                    names.add(f.id)
                elif isinstance(f, self.ast.Attribute):
                    names.add(f.attr)
        return names

    def test_it_calls_nothing_that_reaches_a_venue_or_a_database(self):
        called = self._called_names()
        for bad in ("place_maker_sell", "place_market_sell", "place_order",
                    "cancel_order", "post", "commit", "execute", "delete"):
            self.assertNotIn(bad, called, bad)

    def test_it_imports_no_network_or_database_module(self):
        mods = set()
        for n in self.ast.walk(self.tree):
            if isinstance(n, self.ast.Import):
                mods |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, self.ast.ImportFrom) and n.module:
                mods.add(n.module.split(".")[0])
        for bad in ("aiohttp", "requests", "sqlalchemy", "database", "models"):
            self.assertNotIn(bad, mods, bad)

    def test_it_only_returns_a_verdict_and_never_mutates_state(self):
        """No module-level mutable state is written by verdict()."""
        fn = [n for n in self.ast.walk(self.tree)
              if isinstance(n, self.ast.FunctionDef) and n.name == "verdict"][0]
        for n in self.ast.walk(fn):
            self.assertNotIsInstance(n, self.ast.Global)


class TestItIsWiredBeforeTheOrder(unittest.TestCase):
    def setUp(self):
        self.src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "crypto_grid_bot.py")).read()

    def test_the_check_runs_before_grid_sell(self):
        chk = self.src.index("import sell_deliverability as _sd")
        sell = self.src.index("fill = await grid_sell(session, oldest.qty")
        self.assertLess(chk, sell)

    def test_it_reads_available_not_owned(self):
        seg = self.src[self.src.index("import sell_deliverability as _sd"):
                       self.src.index("fill = await grid_sell(session, oldest.qty")]
        self.assertIn("wallet_available_units()", seg)
        self.assertNotIn("wallet_owned_units", seg)

    def test_wallet_available_units_uses_the_available_map(self):
        seg = self.src[self.src.index("async def wallet_available_units"):
                       self.src.index("_SELL_SKIPS = {}")]
        self.assertIn("available_units_map", seg)
        self.assertNotIn("owned_units_map", seg)

    def test_the_skip_counter_resets_on_a_send(self):
        self.assertIn("_SELL_SKIPS.pop(_sd_key, None)", self.src)

    def test_the_feed_write_is_throttled(self):
        self.assertIn('_feed_should_write("SELL_UNDELIVERABLE"', self.src)


if __name__ == "__main__":
    unittest.main(verbosity=2)

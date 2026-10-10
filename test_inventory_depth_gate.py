#!/usr/bin/env python3
"""Tests for the inventory-depth buy gate. Run the way this repo runs tests:

    python3 test_inventory_depth_gate.py

Pure: no database, no network, no live clock. Every test sets the arming
variable explicitly and restores it, so a stray export cannot make the
suite pass for the wrong reason.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import inventory_depth_gate as g

NOW = 1_000_000.0
def at(hours_ago):
    """An opened_at stand-in; the epoch reader is injected, so a float is fine."""
    return NOW - hours_ago * 3600.0
EPOCH = lambda v: v          # the caller owns the clock; tests inject identity


def sl(basis, mae=None, hours=1.0, qty=1.0):
    return {"qty": qty, "entry_price": basis / qty, "mae_pct": mae,
            "opened_at": at(hours)}


class Armed(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("GRID_INVENTORY_DEPTH_GATE")
        os.environ["GRID_INVENTORY_DEPTH_GATE"] = "1"

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("GRID_INVENTORY_DEPTH_GATE", None)
        else:
            os.environ["GRID_INVENTORY_DEPTH_GATE"] = self._prev


class TestOffByDefault(unittest.TestCase):
    """The single most important property: unarmed, it changes nothing."""

    def test_unset_allows_the_worst_possible_branch(self):
        prev = os.environ.pop("GRID_INVENTORY_DEPTH_GATE", None)
        try:
            ok, why = g.verdict([sl(100.0, mae=-0.99, hours=9999.0)], NOW, EPOCH)
            self.assertTrue(ok)
            self.assertIn("not armed", why)
        finally:
            if prev is not None:
                os.environ["GRID_INVENTORY_DEPTH_GATE"] = prev

    def test_only_truthy_strings_arm_it(self):
        prev = os.environ.get("GRID_INVENTORY_DEPTH_GATE")
        try:
            for v in ("", "0", "false", "no", "off", "maybe"):
                os.environ["GRID_INVENTORY_DEPTH_GATE"] = v
                self.assertFalse(g.is_armed(), v)
            for v in ("1", "true", "TRUE", "yes", "on", " on "):
                os.environ["GRID_INVENTORY_DEPTH_GATE"] = v
                self.assertTrue(g.is_armed(), v)
        finally:
            if prev is None:
                os.environ.pop("GRID_INVENTORY_DEPTH_GATE", None)
            else:
                os.environ["GRID_INVENTORY_DEPTH_GATE"] = prev


class TestRefusesOnlyWhatItShould(Armed):
    def test_refuses_when_alert_share_is_at_the_line(self):
        ok, why = g.verdict([sl(25.0, mae=-0.99), sl(75.0, mae=0.0)], NOW, EPOCH)
        self.assertFalse(ok)
        self.assertIn("no new dollars go in", why)
        self.assertIn("Nothing is sold", why)

    def test_allows_just_under_the_line(self):
        ok, why = g.verdict([sl(24.0, mae=-0.99), sl(76.0, mae=0.0)], NOW, EPOCH)
        self.assertTrue(ok)
        self.assertIn("inventory ok", why)

    def test_a_healthy_branch_is_allowed(self):
        ok, _ = g.verdict([sl(50.0, mae=-0.01, hours=2.0)] * 3, NOW, EPOCH)
        self.assertTrue(ok)

    def test_empty_branch_is_allowed_and_does_not_divide_by_zero(self):
        ok, why = g.verdict([], NOW, EPOCH)
        self.assertTrue(ok)
        self.assertIn("UNKNOWN", why)


class TestFailsOpen(Armed):
    def test_unreadable_slices_are_unknown_not_a_refusal(self):
        bad = [{"qty": 0, "entry_price": 0, "mae_pct": None, "opened_at": None}]
        ok, why = g.verdict(bad, NOW, EPOCH)
        self.assertTrue(ok)
        self.assertIn("UNKNOWN is not a refusal", why)

    def test_an_exploding_epoch_reader_allows_through(self):
        def boom(_):
            raise RuntimeError("clock unavailable")
        ok, why = g.verdict([sl(100.0, mae=-0.99)], NOW, boom)
        self.assertTrue(ok)
        self.assertIn("UNKNOWN", why)

    def test_a_partly_unreadable_branch_still_judges_what_it_can(self):
        rows = [sl(100.0, mae=-0.99),
                {"qty": None, "entry_price": None, "opened_at": None}]
        m = g.measure(rows, NOW, EPOCH)
        self.assertEqual(m["slices_read"], 1)
        self.assertEqual(m["slices_unreadable"], 1)
        self.assertTrue(m["readable"])


class TestTheStatisticIsTheStoredExcursion(Armed):
    def test_missing_mae_is_unknown_not_shallow(self):
        st, why = g.classify_slice(None, 1.0)
        self.assertEqual(st, g.NORMAL)
        self.assertIn("UNKNOWN", why)

    def test_missing_mae_still_escalates_on_age(self):
        st, why = g.classify_slice(None, g.RECOVERED_HOLD_MAX + 1)
        self.assertEqual(st, g.ALERT)

    def test_a_positive_excursion_is_not_depth(self):
        st, _ = g.classify_slice(+0.02, 1.0)
        self.assertEqual(st, g.NORMAL)

    def test_boundaries_are_the_safer_state(self):
        self.assertEqual(g.classify_slice(-g.RECOVERED_DEPTH_P95, 1.0)[0], g.NORMAL)
        self.assertEqual(g.classify_slice(-g.RECOVERED_DEPTH_MAX, 1.0)[0], g.WARN)
        self.assertEqual(g.classify_slice(0.0, g.RECOVERED_HOLD_P95)[0], g.NORMAL)
        self.assertEqual(g.classify_slice(0.0, g.RECOVERED_HOLD_MAX)[0], g.WARN)

    def test_one_tick_past_each_boundary_escalates(self):
        self.assertEqual(g.classify_slice(-(g.RECOVERED_DEPTH_P95 + 1e-9), 1.0)[0], g.WARN)
        self.assertEqual(g.classify_slice(-(g.RECOVERED_DEPTH_MAX + 1e-9), 1.0)[0], g.ALERT)
        self.assertEqual(g.classify_slice(0.0, g.RECOVERED_HOLD_P95 + 1e-9)[0], g.WARN)
        self.assertEqual(g.classify_slice(0.0, g.RECOVERED_HOLD_MAX + 1e-9)[0], g.ALERT)

    def test_state_can_only_ratchet_as_the_high_water_mark_deepens(self):
        """Why no hysteresis: mae_pct is a running minimum, so it only falls."""
        order = {g.NORMAL: 0, g.WARN: 1, g.ALERT: 2}
        prev, mae = -1, 0.0
        for _ in range(400):
            mae -= 0.001
            prev2 = order[g.classify_slice(mae, 1.0)[0]]
            self.assertGreaterEqual(prev2, prev)
            prev = prev2
        self.assertEqual(prev, order[g.ALERT])


class TestTheDenominator(Armed):
    def test_share_is_against_deployed_basis(self):
        m = g.measure([sl(100.0, mae=-0.99), sl(300.0, mae=0.0)], NOW, EPOCH)
        self.assertEqual(m["deployed_basis_usd"], 400.0)
        self.assertEqual(m["alert_share_of_deployed"], 0.25)

    def test_banking_a_winner_cannot_unpause_the_branch(self):
        """Under allocated_usd a realised win dilutes the share. Under
        deployed basis, selling a HEALTHY slice removes healthy basis, so
        the share can only RISE - which is the correct direction."""
        before = g.measure([sl(50.0, mae=-0.99), sl(150.0, mae=0.0)], NOW, EPOCH)
        after = g.measure([sl(50.0, mae=-0.99), sl(100.0, mae=0.0)], NOW, EPOCH)
        self.assertGreater(after["alert_share_of_deployed"],
                           before["alert_share_of_deployed"])


class TestItCannotSellOrResize(unittest.TestCase):
    """Structural, not behavioural: the module must have no verb that could
    reach the venue or move a threshold. Cheaper than trusting review."""

    def test_module_contains_no_order_or_mutation_verbs(self):
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "inventory_depth_gate.py")).read()
        for forbidden in ("place_order", "grid_sell", "grid_buy", "session.post",
                          "requests.", "aiohttp", "commit(", "DELETE", "UPDATE "):
            self.assertNotIn(forbidden, src, forbidden)

    def test_a_refusal_names_that_nothing_is_sold(self):
        prev = os.environ.get("GRID_INVENTORY_DEPTH_GATE")
        os.environ["GRID_INVENTORY_DEPTH_GATE"] = "1"
        try:
            ok, why = g.verdict([sl(100.0, mae=-0.99)], NOW, EPOCH)
            self.assertFalse(ok)
            self.assertIn("existing slices sell normally", why)
        finally:
            if prev is None:
                os.environ.pop("GRID_INVENTORY_DEPTH_GATE", None)
            else:
                os.environ["GRID_INVENTORY_DEPTH_GATE"] = prev


class TestItIsWiredIntoTheBuyPath(unittest.TestCase):
    def test_the_executor_consults_it_before_buying(self):
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "crypto_grid_bot.py")).read()
        self.assertIn("import inventory_depth_gate as _idg", src)
        self.assertIn("_idg.verdict(", src)
        self.assertIn('"INVENTORY_DEPTH"', src)

    def test_its_feed_write_and_log_line_are_both_throttled(self):
        """Unthrottled, three armed branches would each write the feed every
        ~50s and push everything else off it - the ZEC EXITING failure."""
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "crypto_grid_bot.py")).read()
        seg = src[src.index("_idg.verdict("):src.index("fill = await grid_sell(")]
        self.assertIn('_feed_should_write("INVENTORY_DEPTH"', seg)
        self.assertIn('should_say_state(f"{branch.bot_name}:inventory"', seg)

    def test_it_sits_after_the_backing_gate_and_before_the_execution_gate(self):
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "crypto_grid_bot.py")).read()
        back = src.index("branch_backing_verdict(")
        inv = src.index("_idg.verdict(")
        execg = src.index("execution gate -")
        self.assertLess(back, inv)
        self.assertLess(inv, execg)


class TestStatusIsWhatTheGateWillDo(unittest.TestCase):
    """status() feeds /grid-status. Its whole value is that it cannot
    disagree with the gate it describes, so these tie the two together
    rather than testing the dict in isolation."""

    def setUp(self):
        self._prev = os.environ.get("GRID_INVENTORY_DEPTH_GATE")

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("GRID_INVENTORY_DEPTH_GATE", None)
        else:
            os.environ["GRID_INVENTORY_DEPTH_GATE"] = self._prev

    def _set(self, v):
        if v is None:
            os.environ.pop("GRID_INVENTORY_DEPTH_GATE", None)
        else:
            os.environ["GRID_INVENTORY_DEPTH_GATE"] = v

    def test_armed_agrees_with_is_armed_for_every_value(self):
        for v in (None, "", "0", "1", "true", "TRUE", " on ", "yes", "false",
                  "off", "no", "tru", "enabled", "2", "armed", "y"):
            self._set(v)
            self.assertEqual(g.status()["armed"], g.is_armed(), repr(v))

    def test_armed_agrees_with_what_verdict_actually_does(self):
        """Not just the flag: the behaviour. An armed status must mean a
        deep branch is really refused, an unarmed one that it is allowed."""
        bad = [sl(100.0, mae=-0.99, hours=9999.0)]
        for v in ("1", "true", "on", "yes"):
            self._set(v)
            self.assertTrue(g.status()["armed"], v)
            self.assertFalse(g.verdict(bad, NOW, EPOCH)[0], v)
        for v in (None, "0", "tru", "enabled", "armed"):
            self._set(v)
            self.assertFalse(g.status()["armed"], repr(v))
            self.assertTrue(g.verdict(bad, NOW, EPOCH)[0], repr(v))

    def test_a_typo_is_reported_as_unrecognised_not_as_armed(self):
        """The mistake worth catching from outside: meant to arm, did not."""
        for v in ("tru", "enabled", "armed", "y", "2"):
            self._set(v)
            st = g.status()
            self.assertFalse(st["armed"], v)
            self.assertTrue(st["env_is_set"], v)
            self.assertFalse(st["value_recognised"], v)

    def test_recognised_values_are_flagged_recognised(self):
        for v in (None, "", "0", "false", "no", "off", "1", "true", "yes", "on"):
            self._set(v)
            self.assertTrue(g.status()["value_recognised"], repr(v))

    def test_the_raw_value_is_never_echoed(self):
        """A variable pasted into the wrong field could hold anything."""
        self._set("sk-live-DO-NOT-PRINT-THIS-1234567890")
        blob = repr(g.status())
        self.assertNotIn("DO-NOT-PRINT", blob)
        self.assertNotIn("sk-live", blob)

    def test_it_says_armed_does_not_mean_it_has_refused(self):
        self._set("1")
        self.assertIn("about to buy", g.status()["runs_only_when"])

    def test_status_never_raises(self):
        import unittest.mock as m
        with m.patch.object(g.os, "getenv", side_effect=RuntimeError("boom")):
            st = g.status()
        self.assertFalse(st["readable"])


class TestStatusIsOnGridStatus(unittest.TestCase):
    def setUp(self):
        self.src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "crypto_grid_bot.py")).read()

    def test_the_helper_is_spread_into_the_payload(self):
        self.assertIn("**_armed_flags_block(),", self.src)

    def test_the_helper_isolates_each_module(self):
        seg = self.src[self.src.index("def _armed_flags_block"):
                       self.src.index("def _wallet_units_fallback")]
        self.assertIn("try:", seg)
        self.assertIn('"readable": False', seg)
        self.assertIn("inventory_depth_gate", seg)
        self.assertIn("sell_deliverability", seg)


if __name__ == "__main__":
    unittest.main(verbosity=2)

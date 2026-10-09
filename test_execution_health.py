"""The health rules measure and name. They must never act.

[1] NOTHING EXECUTES. By AST, not by promise. This is the section that
    protects live money: every other guarantee in these two modules is
    worthless if a rule can reach a broker.
[2] TRAPPED CAPITAL, against the live DOG/RWM case.
[3] UNKNOWN IS NOT OK. A rule that could not read its input must not
    report health.
[4] THE SCORE's profit-factor gate and its unit trap.
[5] SIZING: the three bugs in the spec's version.
[6] KELLY on the real book.

Run: python3 -m unittest test_execution_health -v
"""

import ast
import inspect
import unittest

import capital_sizing as cs
import execution_health as eh


# ═══ [1] NOTHING EXECUTES ═════════════════════════════════════════════

class TheseModulesCannotAct(unittest.TestCase):
    """The spec named actions like pause_symbol(), disable_new_entries(),
    reduce_position_size(50), close_open_signals(), pause_trading(). Each
    is a trading-control write. They appear here only as STRINGS a human
    reads. If one ever becomes a call, this test fails."""

    MODULES = (eh, cs)

    # Named for what they do, so a rename does not sneak past. Checked as
    # whole call names, not substrings, so a string mentioning
    # "pause_trading()" in a recommendation is not a false positive.
    FORBIDDEN_CALLS = {
        "pause_symbol", "pause_trading", "disable_new_entries",
        "disable_all_entries", "disable_new_trades", "close_open_signals",
        "reduce_position_size", "close_position", "submit_order",
        "place_order", "cancel_order", "create_order",
        "_close_whole_position", "execute_futures_trade",
        "post", "put", "patch", "delete", "request",
    }

    def _calls(self, mod):
        tree = ast.parse(inspect.getsource(mod))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                n = getattr(fn, "id", None) or getattr(fn, "attr", None)
                if n:
                    names.add(n)
        return names

    def test_no_module_calls_anything_that_acts(self):
        for mod in self.MODULES:
            called = self._calls(mod)
            overlap = called & self.FORBIDDEN_CALLS
            self.assertEqual(
                overlap, set(),
                f"{mod.__name__} calls {sorted(overlap)} - these modules "
                f"name actions, they do not take them")

    def test_no_module_imports_a_transport_or_a_broker(self):
        for mod in self.MODULES:
            tree = ast.parse(inspect.getsource(mod))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
            for banned in ("aiohttp", "requests", "httpx", "urllib",
                           "http", "socket", "prop_bot", "crypto_grid_bot"):
                self.assertNotIn(
                    banned, imported,
                    f"{mod.__name__} imports {banned} - a module that "
                    f"cannot reach a venue cannot trade by accident")

    def test_both_modules_declare_it(self):
        for mod in self.MODULES:
            self.assertTrue(getattr(mod, "EXECUTES_NOTHING", False),
                            f"{mod.__name__} must declare EXECUTES_NOTHING")

    def test_recommendations_are_strings_not_callables(self):
        v = eh.drawdown_breach(15.0)
        self.assertTrue(v.fired)
        self.assertTrue(v.recommended_actions)
        for a in v.recommended_actions:
            self.assertIsInstance(a, str)
            self.assertFalse(callable(a))

    def test_the_loss_rule_refuses_to_recommend_selling_at_a_loss(self):
        """close_open_signals() is in the spec. It books losses by
        machine. It must be present as a refusal, never a recommendation."""
        v = eh.daily_loss_limit(-5.0)
        self.assertTrue(v.fired)
        joined = " ".join(v.recommended_actions)
        self.assertIn("close_open_signals", joined,
                      "the spec's action must be addressed, not dropped")
        self.assertIn("NOT RECOMMENDED", joined,
                      "it must be marked as refused, with the reason")
        recs = [a for a in v.recommended_actions
                if not a.startswith("NOT RECOMMENDED")]
        for a in recs:
            self.assertNotIn("close_open_signals", a)


# ═══ [2] TRAPPED CAPITAL, THE LIVE CASE ═══════════════════════════════

class TrappedCapitalCard(unittest.TestCase):
    """Measured live 2026-10-09T01:40Z against the broker."""

    def setUp(self):
        self.pos = [
            eh.TrappedPosition("DOG", 5.054042, 0.0, 22.15,
                               "2026-10-08T19:53:32.072Z"),
            eh.TrappedPosition("RWM", 18.122486, 9.061243, 14.39,
                               "2026-10-08T19:56:46.846Z"),
        ]

    def test_both_live_positions_read_as_blocked(self):
        for p in self.pos:
            self.assertTrue(p.is_blocked, p.symbol)

    def test_the_two_figures_differ_and_both_are_right(self):
        card = eh.trapped_capital(self.pos, equity=974.47)
        # DOG: all 5.054042 held back. RWM: exactly half.
        self.assertAlmostEqual(card["trapped_at_broker_usd"], 242.34, places=2)
        # Every blocked position's whole value, because the gate is
        # all-or-nothing - RWM's free half cannot leave on its own.
        self.assertAlmostEqual(card["cannot_follow_strategy_usd"], 372.73,
                               places=2)
        self.assertGreater(card["cannot_follow_strategy_usd"],
                           card["trapped_at_broker_usd"],
                           "the honest figure is the larger one")

    def test_it_is_the_whole_invested_balance(self):
        """$974.47 equity, $601.74 cash. Every invested dollar is stuck."""
        card = eh.trapped_capital(self.pos, equity=974.47)
        self.assertAlmostEqual(card["cannot_follow_strategy_usd"],
                               974.47 - 601.74, places=1)
        self.assertAlmostEqual(card["pct_of_equity"], 38.25, places=1)

    def test_past_the_backstop_is_critical_not_a_warning(self):
        card = eh.trapped_capital(self.pos, equity=974.47,
                                  minutes_since=lambda s: 351)
        self.assertEqual(card["oldest_block_minutes"], 351)
        self.assertEqual(card["status"], eh.CRITICAL,
                         "351 minutes is nearly 3x the 120-minute backstop")

    def test_inside_the_backstop_is_a_warning(self):
        card = eh.trapped_capital(self.pos, minutes_since=lambda s: 30)
        self.assertEqual(card["status"], eh.WARNING)

    def test_a_clean_book_is_ok_and_reports_zero(self):
        free = [eh.TrappedPosition("QQQ", 10.0, 10.0, 400.0)]
        card = eh.trapped_capital(free, equity=974.47)
        self.assertEqual(card["status"], eh.OK)
        self.assertEqual(card["trapped_positions"], 0)
        self.assertEqual(card["cannot_follow_strategy_usd"], 0)
        self.assertEqual(card["symbols"], [])

    def test_equal_availability_is_not_blocked(self):
        self.assertFalse(
            eh.TrappedPosition("X", 5.0, 5.0, 10.0).is_blocked,
            "available == owned can sell in full and is not trapped")

    def test_an_unreadable_age_does_not_hide_the_money(self):
        card = eh.trapped_capital(
            self.pos, equity=974.47,
            minutes_since=lambda s: (_ for _ in ()).throw(ValueError("bad")))
        self.assertIsNone(card["oldest_block_minutes"])
        self.assertAlmostEqual(card["cannot_follow_strategy_usd"], 372.73,
                               places=2)
        self.assertEqual(card["trapped_positions"], 2,
                         "a bad clock must not zero out the dollar figure")


# ═══ [3] UNKNOWN IS NOT OK ════════════════════════════════════════════

class AnUnreadableRuleDoesNotReportHealth(unittest.TestCase):
    def test_every_rule_returns_unknown_on_a_none_input(self):
        cases = [
            eh.blocked_exit("DOG", None, 5.0),
            eh.blocked_exit("DOG", 0.0, None),
            eh.stale_order_lock(None, "new"),
            eh.stale_order_lock(45, None),
            eh.drawdown_breach(None),
            eh.daily_loss_limit(None),
            eh.broker_reject_spike(None),
            eh.capital_trapped(None, 1.0),
            eh.utilization_too_low(None),
            eh.win_rate_collapse(None),
        ]
        for v in cases:
            self.assertTrue(v.unknown, f"{v.rule} must report UNKNOWN")
            self.assertFalse(v.fired, f"{v.rule} must not claim it fired")
            self.assertNotEqual(v.severity, eh.OK,
                                f"{v.rule} UNKNOWN must never read as OK")
            self.assertEqual(v.measured, "UNKNOWN")

    def test_an_untrusted_peak_reports_unknown_not_a_breach(self):
        """The whole reason these rules do not auto-act. BTC reads 25.1%
        drawdown on 81 cents of real loss because its stored peak is
        stale. Wired to execute, that halts a healthy account."""
        v = eh.drawdown_breach(25.1, peak_is_trusted=False)
        self.assertTrue(v.unknown)
        self.assertFalse(v.fired, "a known-bad input must not halt trading")
        self.assertIn("25.10%", v.why)
        self.assertIn("stale", v.why)

    def test_a_trusted_peak_at_the_same_reading_does_fire(self):
        v = eh.drawdown_breach(25.1, peak_is_trusted=True)
        self.assertTrue(v.fired)
        self.assertEqual(v.severity, eh.CRITICAL)
        self.assertIn("disable_all_entries", " ".join(v.recommended_actions))

    def test_worst_never_lets_unknown_pass_as_ok(self):
        self.assertEqual(
            eh.worst([eh.utilization_too_low(10.0), eh.drawdown_breach(None)]),
            eh.WARNING)
        self.assertEqual(eh.worst([]), eh.OK)
        self.assertEqual(
            eh.worst([eh.drawdown_breach(1.0), eh.blocked_exit("D", 0.0, 5.0)]),
            eh.CRITICAL)


# ═══ [4] THE SCORE ════════════════════════════════════════════════════

class TheScoreCannotCallALosingBookHealthy(unittest.TestCase):
    def test_the_live_crypto_book_is_gated_red(self):
        r = eh.capital_growth_score(0.519, 86.5, 2.6, 2, 0)
        self.assertEqual(r["colour"], eh.RED)
        self.assertEqual(r["gated_by"], "profit_factor")
        self.assertIn("86.5", r["why"])

    def test_a_high_win_rate_cannot_buy_green(self):
        """100% win rate, no drawdown, nothing blocked - and a profit
        factor under 1. The raw score clears GREEN; the gate refuses."""
        r = eh.capital_growth_score(0.99, 100.0, 0.0, 0, 0)
        self.assertGreater(r["score"], 50)
        self.assertEqual(r["colour"], eh.RED)
        self.assertEqual(r["gated_by"], "profit_factor")

    def test_a_profitable_book_is_scored_normally(self):
        for pf, wr, want in ((1.73, 58.0, eh.YELLOW), (3.5, 90.0, eh.GREEN),
                             (1.05, 20.0, eh.RED)):
            r = eh.capital_growth_score(pf, wr, 0.0, 0, 0)
            self.assertIsNone(r["gated_by"], f"PF {pf} should not be gated")
            self.assertEqual(r["colour"], want, f"PF {pf} WR {wr}")

    def test_exactly_one_is_not_gated(self):
        self.assertIsNone(
            eh.capital_growth_score(1.0, 58.0, 0.0, 0, 0)["gated_by"],
            "PF 1.00 breaks even - it does not lose money per trade")

    def test_the_fraction_trap_is_caught_not_scored(self):
        r = eh.capital_growth_score(0.519, 0.865, 2.6, 2, 0)
        self.assertTrue(r["unknown"])
        self.assertIsNone(r["score"])
        self.assertIn("fraction", r["why"])

    def test_a_missing_input_does_not_score(self):
        for args in ((None, 58.0, 1.0, 0, 0), (1.5, None, 1.0, 0, 0),
                     (1.5, 58.0, None, 0, 0), (1.5, 58.0, 1.0, None, 0),
                     (1.5, 58.0, 1.0, 0, None)):
            r = eh.capital_growth_score(*args)
            self.assertTrue(r["unknown"])
            self.assertIsNone(r["colour"])

    def test_blocked_exits_cost_ten_points_each(self):
        a = eh.capital_growth_score(2.0, 58.0, 0.0, 0, 0)["score"]
        b = eh.capital_growth_score(2.0, 58.0, 0.0, 2, 0)["score"]
        self.assertAlmostEqual(a - b, 20.0, places=2)


# ═══ [5] SIZING: THE THREE BUGS ═══════════════════════════════════════

class TheSizerDoesNotCrashOrLever(unittest.TestCase):
    def test_the_spec_example_is_unchanged(self):
        s = cs.PositionSize(equity=10000, risk_pct=0.01,
                            entry_price=50, stop_price=47.5)
        self.assertAlmostEqual(s.shares(), 40.0, places=6)
        self.assertAlmostEqual(s.position_value(), 2000.0, places=2)
        self.assertFalse(s.report()["capped"])

    def test_a_zero_stop_distance_refuses_instead_of_raising(self):
        s = cs.PositionSize(equity=10000, risk_pct=0.01,
                            entry_price=50, stop_price=50)
        self.assertIsNone(s.shares())
        self.assertIsNone(s.position_value())
        r = s.report()
        self.assertTrue(r["refused"])
        self.assertIn("not a stop", r["why"])

    def test_a_tight_stop_cannot_lever_the_account(self):
        """$10k, 1% risk, 1-cent stop: the uncapped formula sizes 10,000
        shares at $50 - a $500,000 position, 50x leverage, from
        arithmetic that looks entirely reasonable."""
        s = cs.PositionSize(equity=10000, risk_pct=0.01,
                            entry_price=50, stop_price=49.99)
        r = s.report()
        self.assertTrue(r["capped"])
        self.assertAlmostEqual(r["position_value"], 10000.0, places=2)
        self.assertLessEqual(r["position_value"], 10000.0,
                             "NO LEVERAGE - never more than the account")
        self.assertIn("5000.0%", r["why"],
                      "the uncapped size must be named, not hidden")

    def test_the_concentration_ceiling_is_honoured(self):
        s = cs.PositionSize(equity=10000, risk_pct=0.01, entry_price=50,
                            stop_price=49.99, max_pct_of_equity=0.20)
        self.assertAlmostEqual(s.position_value(), 2000.0, places=2)

    def test_atr_sizing_matches_the_spec_example(self):
        self.assertAlmostEqual(cs.atr_size(10000, 0.01, 1.5),
                               100 / 3.0, places=6)

    def test_atr_of_zero_returns_none_not_infinity(self):
        self.assertIsNone(cs.atr_size(10000, 0.01, 0))
        self.assertIsNone(cs.atr_size(10000, 0.01, 1.5, atr_multiple=0))
        self.assertIsNone(cs.atr_size(10000, 0.01, "not a number"))

    def test_atr_sizing_is_also_capped_when_a_price_is_given(self):
        units = cs.atr_size(10000, 0.01, 0.001, entry_price=50)
        self.assertLessEqual(units * 50, 10000.0 + 1e-6)


# ═══ [6] KELLY ON THE REAL BOOK ═══════════════════════════════════════

class KellySaysSizeZero(unittest.TestCase):
    def test_reward_risk_identity(self):
        # PF = R:R * WR / (1 - WR), so R:R = PF * (1 - WR) / WR
        rr = cs.reward_risk_from_profit_factor(0.519, 0.865)
        self.assertAlmostEqual(rr, 0.519 * 0.135 / 0.865, places=9)
        # round trip
        self.assertAlmostEqual(rr * 0.865 / 0.135, 0.519, places=9)

    def test_the_live_crypto_book_kellys_to_zero(self):
        r = cs.kelly_from_book(0.519, 86.5)
        self.assertAlmostEqual(r["reward_risk"], 0.081, places=3)
        self.assertLess(r["raw_kelly_before_floor"], 0,
                        "the raw figure is negative - the edge runs the "
                        "wrong way")
        self.assertAlmostEqual(r["raw_kelly_before_floor"], -0.8017, places=3)
        self.assertEqual(r["kelly"], 0.0)
        self.assertEqual(r["deploy"], 0.0)
        self.assertIn("growth-maximising bet is nothing", r["why"])

    def test_a_real_edge_gets_a_real_size(self):
        r = cs.kelly_from_book(1.73, 58.0)
        self.assertGreater(r["kelly"], 0)
        self.assertAlmostEqual(r["deploy"], r["kelly"] * 0.25, places=9)

    def test_reward_risk_of_zero_returns_none_not_a_crash(self):
        self.assertIsNone(cs.kelly_fraction(0.58, 0))
        self.assertIsNone(cs.kelly_fraction(0.58, -1))
        self.assertIsNone(cs.safe_kelly(0.58, 0))

    def test_a_win_rate_outside_zero_to_one_is_refused(self):
        self.assertIsNone(cs.kelly_fraction(58, 2.0),
                          "58 is a percentage - Kelly takes a fraction")
        self.assertIsNone(cs.kelly_fraction(-0.1, 2.0))

    def test_kelly_is_floored_at_zero_never_negative(self):
        for wr in (0.0, 0.1, 0.2, 0.3):
            k = cs.kelly_fraction(wr, 0.5)
            self.assertGreaterEqual(k, 0.0, f"wr={wr} must not go negative")

    def test_the_safety_factor_is_a_quarter(self):
        self.assertEqual(cs.KELLY_SAFETY, 0.25)


if __name__ == "__main__":
    unittest.main(verbosity=2)

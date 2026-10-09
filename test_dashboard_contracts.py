"""The contracts and the gate. Ordered by what goes wrong.

[1] NOTHING EXECUTES, by AST.
[2] THE SIGN BUG - the spec's rule never fires on a loss, and fires on
    a gain. Both failure modes, demonstrated against the spec's own
    expression.
[3] THE SEVERITY INVERSION - RISK_OFF overwriting NO_GO.
[4] UNKNOWN IS NOT A PASS - a GO issued over unreadable inputs.
[5] THE GROWTH INDEX gate and its breakdown.
[6] THE CONTRACTS: derived fields, the two dollar figures, VaR refusing
    a sample too small to be one.
[7] THE LIVE BOOK end to end.
"""

import ast
import inspect
import unittest
from datetime import datetime, timedelta, timezone

import dashboard_contracts as dc
import trading_gate as tg

UTC = timezone.utc
NOW = datetime(2026, 10, 9, 1, 50, tzinfo=UTC)

# Measured live 2026-10-09T01:50Z against the broker.
DOG = dict(symbol="DOG", qty=5.054042, entry_price=22.1972,
           current_price=22.15, available_qty=0.0,
           opened_at=datetime(2026, 10, 8, 19, 53, 32, tzinfo=UTC))
RWM = dict(symbol="RWM", qty=18.122486, entry_price=14.4372,
           current_price=14.39, available_qty=9.061243,
           opened_at=datetime(2026, 10, 8, 19, 56, 46, tzinfo=UTC))


def pos(**kw):
    kw.setdefault("as_of", NOW)
    return dc.Position(**kw)


# ═══ [1] NOTHING EXECUTES ═════════════════════════════════════════════

class TheseModulesCannotAct(unittest.TestCase):
    MODULES = (dc, tg)
    FORBIDDEN = {"pause_symbol", "pause_trading", "disable_new_entries",
                 "disable_all_entries", "disable_new_trades",
                 "close_open_signals", "reduce_position_size",
                 "close_position", "submit_order", "place_order",
                 "cancel_order", "execute_futures_trade",
                 "post", "put", "patch", "delete", "request"}

    def test_no_call_acts(self):
        for mod in self.MODULES:
            names = set()
            for n in ast.walk(ast.parse(inspect.getsource(mod))):
                if isinstance(n, ast.Call):
                    f = n.func
                    nm = getattr(f, "id", None) or getattr(f, "attr", None)
                    if nm:
                        names.add(nm)
            self.assertEqual(names & self.FORBIDDEN, set(), mod.__name__)

    def test_no_transport_imported(self):
        for mod in self.MODULES:
            imported = set()
            for n in ast.walk(ast.parse(inspect.getsource(mod))):
                if isinstance(n, ast.Import):
                    imported.update(a.name.split(".")[0] for a in n.names)
                elif isinstance(n, ast.ImportFrom) and n.module:
                    imported.add(n.module.split(".")[0])
            for banned in ("aiohttp", "requests", "httpx", "urllib", "socket",
                           "prop_bot", "crypto_grid_bot", "sqlalchemy"):
                self.assertNotIn(banned, imported,
                                 f"{mod.__name__} imports {banned}")

    def test_both_declare_it(self):
        for mod in self.MODULES:
            self.assertTrue(getattr(mod, "EXECUTES_NOTHING", False))

    def test_risk_off_recommends_rather_than_resizes(self):
        d = tg.TradingGate().evaluate(
            drawdown_pct=6.0, daily_pnl_pct=-0.14, blocked_exits=0,
            trapped_capital_count=0, profit_factor=1.5, exposure_pct=0.38,
            exposure_limit=0.60, broker_connected=True,
            database_connected=True, alerts_operational=True, now=NOW)
        self.assertEqual(d.status, tg.RISK_OFF)
        self.assertIn("RECOMMENDED, not applied", " ".join(d.reasons))


# ═══ [2] THE SIGN BUG ═════════════════════════════════════════════════

class TheDailyLossRuleMustActuallyFire(unittest.TestCase):
    """The spec's gate reads `daily_loss_pct > 3`; its alert spec reads
    `daily_pnl_pct < -3`. Only one can be right."""

    @staticmethod
    def _spec_expression(value):
        # Verbatim shape of the spec's gate condition.
        return value > 3

    def test_the_specs_expression_never_fires_on_a_loss(self):
        """Fed this system's real field, no loss of any size fires it."""
        for pnl in (-0.14, -3.0, -3.01, -10.0, -50.0, -99.0):
            self.assertFalse(
                self._spec_expression(pnl),
                f"session_pl_pct={pnl} must fire a -3% rule, and the "
                f"spec's expression does not")

    def test_the_specs_expression_fires_on_a_GAIN(self):
        """The other failure mode: read as an unsigned magnitude, a good
        day halts trading."""
        self.assertTrue(self._spec_expression(5.0),
                        "a +5% day satisfies `> 3` - NO_GO on the best "
                        "day of the month")

    def test_the_fixed_gate_fires_on_a_loss(self):
        g = tg.TradingGate()
        d = g.evaluate(drawdown_pct=0.0, daily_pnl_pct=-3.5,
                       blocked_exits=0, trapped_capital_count=0,
                       profit_factor=1.5, exposure_pct=0.1,
                       exposure_limit=0.6, broker_connected=True,
                       database_connected=True, alerts_operational=True,
                       now=NOW)
        self.assertEqual(d.status, tg.NO_GO)
        self.assertIn("Daily loss limit hit", " ".join(d.reasons))

    def test_the_fixed_gate_does_not_fire_on_a_gain(self):
        g = tg.TradingGate()
        d = g.evaluate(drawdown_pct=0.0, daily_pnl_pct=+5.0,
                       blocked_exits=0, trapped_capital_count=0,
                       profit_factor=1.5, exposure_pct=0.1,
                       exposure_limit=0.6, broker_connected=True,
                       database_connected=True, alerts_operational=True,
                       now=NOW)
        self.assertEqual(d.status, tg.GO)
        self.assertEqual(d.reasons, [])

    def test_the_live_session_figure_does_not_fire(self):
        """-0.14% is noise and must not halt anything."""
        g = tg.TradingGate()
        d = g.evaluate(drawdown_pct=0.0, daily_pnl_pct=-0.14,
                       blocked_exits=0, trapped_capital_count=0,
                       profit_factor=1.5, exposure_pct=0.3825,
                       exposure_limit=0.60, broker_connected=True,
                       database_connected=True, alerts_operational=True,
                       now=NOW)
        self.assertEqual(d.status, tg.GO)

    def test_exactly_at_the_limit_does_not_fire(self):
        g = tg.TradingGate()
        d = g.evaluate(drawdown_pct=0.0, daily_pnl_pct=-3.0,
                       blocked_exits=0, trapped_capital_count=0,
                       profit_factor=1.5, exposure_pct=0.1,
                       exposure_limit=0.6, broker_connected=True,
                       database_connected=True, alerts_operational=True,
                       now=NOW)
        self.assertEqual(d.status, tg.GO, "strictly past the limit, not at")


# ═══ [3] THE SEVERITY INVERSION ═══════════════════════════════════════

class RiskOffCanNeverSoftenANoGo(unittest.TestCase):
    """The spec sets status from reasons, THEN overwrites it with
    RISK_OFF when drawdown > 5. A 9% drawdown is both."""

    def _hard(self, **over):
        kw = dict(drawdown_pct=9.0, daily_pnl_pct=-0.14, blocked_exits=0,
                  trapped_capital_count=0, profit_factor=1.5,
                  exposure_pct=0.1, exposure_limit=0.6,
                  broker_connected=True, database_connected=True,
                  alerts_operational=True, now=NOW)
        kw.update(over)
        return tg.TradingGate().evaluate(**kw)

    def test_a_hard_drawdown_breach_is_no_go_not_risk_off(self):
        d = self._hard()
        self.assertEqual(d.status, tg.NO_GO,
                         "9% is past the 8% hard limit AND past the 5% "
                         "soft one - the hard verdict must win")
        self.assertIn("Drawdown breach", " ".join(d.reasons))

    def test_a_disconnected_broker_at_a_soft_drawdown_is_still_no_go(self):
        d = self._hard(drawdown_pct=6.0, broker_connected=False)
        self.assertEqual(d.status, tg.NO_GO)
        self.assertIn("Broker disconnected", d.reasons)
        self.assertNotIn("half size", " ".join(d.reasons),
                         "RISK_OFF must not be appended over a NO_GO")

    def test_the_soft_band_alone_is_risk_off(self):
        for dd in (5.01, 6.0, 7.99):
            d = self._hard(drawdown_pct=dd)
            self.assertEqual(d.status, tg.RISK_OFF, f"dd={dd}")

    def test_below_the_soft_band_is_go(self):
        self.assertEqual(self._hard(drawdown_pct=5.0).status, tg.GO)
        self.assertEqual(self._hard(drawdown_pct=0.0).status, tg.GO)

    def test_may_trade_is_false_only_for_no_go(self):
        self.assertFalse(self._hard().may_trade)
        self.assertTrue(self._hard(drawdown_pct=6.0).may_trade)
        self.assertTrue(self._hard(drawdown_pct=0.0).may_trade)


# ═══ [4] UNKNOWN IS NOT A PASS ════════════════════════════════════════

class AnUnreadableInputIsNotAPassedCheck(unittest.TestCase):
    def test_a_missing_input_lands_in_unknown_not_in_silence(self):
        d = tg.TradingGate().evaluate(
            drawdown_pct=None, daily_pnl_pct=None, blocked_exits=None,
            trapped_capital_count=None, profit_factor=None,
            exposure_pct=None, exposure_limit=None, broker_connected=None,
            database_connected=None, alerts_operational=None, now=NOW)
        self.assertEqual(d.reasons, [])
        self.assertEqual(d.status, tg.GO)
        # The point: GO with ten unchecked conditions must SAY so.
        self.assertGreaterEqual(len(d.unknown), 8)
        self.assertIsNone(d.score, "no index from unreadable inputs")

    def test_an_untrusted_peak_is_unknown_not_a_breach(self):
        """BTC reads 25.1% on 81 cents. A gate that halts on that halts
        a healthy account."""
        d = tg.TradingGate().evaluate(
            drawdown_pct=25.1, peak_is_trusted=False, daily_pnl_pct=-0.14,
            blocked_exits=0, trapped_capital_count=0, profit_factor=1.5,
            exposure_pct=0.1, exposure_limit=0.6, broker_connected=True,
            database_connected=True, alerts_operational=True, now=NOW)
        self.assertEqual(d.status, tg.GO)
        self.assertIn("stale", " ".join(d.unknown))
        self.assertNotIn("Drawdown breach", " ".join(d.reasons))

    def test_a_trusted_peak_at_the_same_reading_halts(self):
        d = tg.TradingGate().evaluate(
            drawdown_pct=25.1, peak_is_trusted=True, daily_pnl_pct=-0.14,
            blocked_exits=0, trapped_capital_count=0, profit_factor=1.5,
            exposure_pct=0.1, exposure_limit=0.6, broker_connected=True,
            database_connected=True, alerts_operational=True, now=NOW)
        self.assertEqual(d.status, tg.NO_GO)

    def test_alerting_down_is_a_monitoring_outage(self):
        """Live on this system: SendGrid out of credits, SMTP filtered,
        89 rows held. A failure nobody can be told about is a NO_GO."""
        d = tg.TradingGate().evaluate(
            drawdown_pct=0.0, daily_pnl_pct=-0.14, blocked_exits=0,
            trapped_capital_count=0, profit_factor=1.5, exposure_pct=0.1,
            exposure_limit=0.6, broker_connected=True,
            database_connected=True, alerts_operational=False, now=NOW)
        self.assertEqual(d.status, tg.NO_GO)
        self.assertIn("Alerting unavailable", " ".join(d.reasons))


# ═══ [5] THE GROWTH INDEX ═════════════════════════════════════════════

class TheIndexCannotCallALosingBookStable(unittest.TestCase):
    def test_the_spec_arithmetic_is_reproduced_exactly(self):
        r = tg.capital_growth_index(drawdown_pct=2.6, blocked_exits=2,
                                    trapped_capital_count=2,
                                    broker_rejects=0, profit_factor=0.519)
        # 100 - 7.8 - 20 - 16 - 0 + 0
        self.assertAlmostEqual(r["raw_before_clamp"], 56.2, places=2)
        self.assertAlmostEqual(r["index"], 56.2, places=2)
        self.assertAlmostEqual(r["breakdown"]["profit_factor"], 0.0,
                               places=6)

    def test_but_it_is_graded_preservation_not_caution(self):
        r = tg.capital_growth_index(drawdown_pct=2.6, blocked_exits=2,
                                    trapped_capital_count=2,
                                    broker_rejects=0, profit_factor=0.519)
        self.assertEqual(r["grade"], "PRESERVATION")
        self.assertEqual(r["gated_by"], "profit_factor")
        self.assertIn("CAUTION", r["why"],
                      "the ungated grade must be named, not hidden")

    def test_a_losing_book_and_a_breakeven_book_score_identically(self):
        """The flaw, demonstrated: max(0,(pf-1)*20) makes PF 0.1 and
        PF 1.0 contribute the same zero."""
        a = tg.capital_growth_index(drawdown_pct=0, blocked_exits=0,
                                    trapped_capital_count=0,
                                    broker_rejects=0, profit_factor=0.1)
        b = tg.capital_growth_index(drawdown_pct=0, blocked_exits=0,
                                    trapped_capital_count=0,
                                    broker_rejects=0, profit_factor=1.0)
        self.assertEqual(a["index"], b["index"])
        self.assertEqual(a["index"], 100.0)
        # The gate is what separates them.
        self.assertEqual(a["grade"], "PRESERVATION")
        self.assertEqual(b["grade"], "ELITE")

    def test_the_double_count_is_surfaced(self):
        r = tg.capital_growth_index(drawdown_pct=0, blocked_exits=2,
                                    trapped_capital_count=2,
                                    broker_rejects=0, profit_factor=2.0)
        self.assertIsNotNone(r["double_counted"])
        self.assertIn("18", r["double_counted"])
        self.assertAlmostEqual(r["breakdown"]["blocked_exits"], -20.0)
        self.assertAlmostEqual(r["breakdown"]["trapped_capital"], -16.0)

    def test_it_clamps_to_zero_and_a_hundred(self):
        low = tg.capital_growth_index(drawdown_pct=50, blocked_exits=9,
                                      trapped_capital_count=9,
                                      broker_rejects=9, profit_factor=3.0)
        self.assertEqual(low["index"], 0.0)
        self.assertLess(low["raw_before_clamp"], 0)
        high = tg.capital_growth_index(drawdown_pct=0, blocked_exits=0,
                                       trapped_capital_count=0,
                                       broker_rejects=0, profit_factor=10.0)
        self.assertEqual(high["index"], 100.0)

    def test_grade_bands_match_the_spec(self):
        for idx, want in ((100, "ELITE"), (95, "ELITE"), (94, "STRONG"),
                          (80, "STRONG"), (79, "STABLE"), (65, "STABLE"),
                          (64, "CAUTION"), (50, "CAUTION"),
                          (49, "PRESERVATION"), (0, "PRESERVATION")):
            self.assertEqual(tg._grade(idx), want, f"index {idx}")

    def test_an_untrusted_peak_does_not_subtract_points(self):
        r = tg.capital_growth_index(drawdown_pct=25.1, blocked_exits=0,
                                    trapped_capital_count=0,
                                    broker_rejects=0, profit_factor=2.0,
                                    peak_is_trusted=False)
        self.assertAlmostEqual(r["breakdown"]["drawdown"], 0.0)
        self.assertIn("stale", r["why"])

    def test_a_missing_input_does_not_index(self):
        r = tg.capital_growth_index(drawdown_pct=None, blocked_exits=0,
                                    trapped_capital_count=0,
                                    broker_rejects=0, profit_factor=2.0)
        self.assertTrue(r["unknown"])
        self.assertIsNone(r["index"])
        self.assertIsNone(r["grade"])


# ═══ [6] THE CONTRACTS ════════════════════════════════════════════════

class DerivedFieldsComeFromTheLiveRules(unittest.TestCase):
    def test_mean_reversion_stop_and_target(self):
        p = pos(**DOG)
        self.assertAlmostEqual(p.stop_price, 22.1972 * 0.985, places=5)
        self.assertAlmostEqual(p.target_price, 22.1972 * 1.03, places=5)
        self.assertFalse(p.past_stop, "-0.21% is nowhere near a 1.5% stop")

    def test_the_two_hour_backstop_is_the_one_applied(self):
        p = pos(**DOG)
        self.assertEqual(p.rules["max_hold_seconds"], 7200)
        self.assertTrue(p.past_max_hold)
        self.assertEqual(p.age_minutes, 356)

    def test_momentum_trails_off_the_peak_not_the_entry(self):
        """Using entry as the reference reads a position as safe when it
        has given back 3% from a high."""
        kw = dict(RWM, strategy_family="momentum",
                  peak_price_since_entry=16.00, current_price=15.00)
        p = pos(**kw)
        self.assertAlmostEqual(p.stop_price, 16.00 * 0.97, places=5)
        self.assertTrue(p.past_stop, "15.00 is below a 15.52 trailing stop")
        entry_based = pos(**dict(kw, strategy_family="mean_reversion"))
        self.assertFalse(entry_based.past_stop,
                         "the same position reads SAFE off entry - which "
                         "is the bug this distinction prevents")

    def test_a_trailing_family_with_no_peak_refuses_to_guess(self):
        p = pos(**dict(RWM, strategy_family="momentum"))
        self.assertIsNone(p.stop_price)
        self.assertIsNone(p.past_stop)

    def test_momentum_max_hold_is_twenty_four_hours(self):
        p = pos(**dict(DOG, strategy_family="momentum"))
        self.assertEqual(p.rules["max_hold_seconds"], 86400)
        self.assertFalse(p.past_max_hold, "5.9h is inside a 24h limit")

    def test_blocked_exit_is_none_when_availability_was_not_read(self):
        p = pos(**dict(DOG, available_qty=None))
        self.assertIsNone(p.blocked_exit,
                          "None is not False - an unread availability is "
                          "exactly what caused this bug three times")

    def test_equal_availability_is_not_blocked(self):
        p = pos(**dict(DOG, available_qty=5.054042))
        self.assertFalse(p.blocked_exit)


class TheTwoDollarFiguresStaySeparate(unittest.TestCase):
    def test_market_value_and_locked_value_differ(self):
        t = dc.TrappedCapital("RWM", 18.122486, 9.061243, 14.39,
                              lock_age_minutes=353)
        self.assertAlmostEqual(t.locked_qty, 9.061243, places=6)
        self.assertAlmostEqual(t.market_value, 260.78, places=2)
        self.assertAlmostEqual(t.locked_value, 130.39, places=2)
        self.assertNotAlmostEqual(t.market_value, t.locked_value, places=2)

    def test_severity_ladder(self):
        self.assertEqual(dc.TrappedCapital("DOG", 5.054042, 0.0, 22.15)
                         .severity, dc.Severity.CRITICAL)
        self.assertEqual(dc.TrappedCapital("RWM", 18.122486, 9.061243, 14.39)
                         .severity, dc.Severity.WARNING)
        self.assertEqual(dc.TrappedCapital("X", 100.0, 90.0, 1.0)
                         .severity, dc.Severity.INFO)
        self.assertIsNone(dc.TrappedCapital("Y", 10.0, 10.0, 1.0).severity,
                          "nothing locked is not a row")

    def test_cause_defaults_to_unknown_not_to_a_guess(self):
        t = dc.TrappedCapital("DOG", 5.054042, 0.0, 22.15)
        self.assertEqual(t.cause, dc.LockCause.UNKNOWN)
        self.assertEqual(t.as_alert()["cause"], "UNKNOWN")

    def test_the_alert_id_is_stable(self):
        self.assertEqual(
            dc.TrappedCapital("RWM", 18.1, 9.0, 14.39).as_alert()["id"],
            "TRAP_RWM")


class UtilizationAndMargin(unittest.TestCase):
    def test_utilization_is_against_equity_not_buying_power(self):
        p = dc.PortfolioSummary(timestamp=NOW, equity=974.47, cash=601.74,
                                buying_power=601.74, daily_pl=-1.40)
        self.assertAlmostEqual(p.invested, 372.73, places=2)
        self.assertAlmostEqual(p.utilization_pct, 38.25, places=2)
        self.assertFalse(p.is_margin_account)

    def test_the_specs_margin_example_is_flagged(self):
        p = dc.PortfolioSummary(timestamp=NOW, equity=12543.91,
                               cash=5023.77, buying_power=10047.54,
                               daily_pl=127.34)
        self.assertTrue(p.is_margin_account,
                        "buying power at 2x cash is margin, and the "
                        "standing rule is NO LEVERAGE - surface it")
        # Against equity, not the 2x buying power.
        self.assertAlmostEqual(p.utilization_pct, 59.95, places=1)


class VarRefusesASampleTooSmallToBeOne(unittest.TestCase):
    def test_no_series_no_var(self):
        self.assertIsNone(dc.historical_var_95(None))
        self.assertIsNone(dc.historical_var_95([]))

    def test_under_twenty_observations_is_refused(self):
        self.assertIsNone(dc.historical_var_95([-5.0] * 19))

    def test_twenty_is_enough_and_returns_a_loss_magnitude(self):
        series = [-100.0] + [1.0] * 19
        v = dc.historical_var_95(series)
        self.assertIsNotNone(v)
        self.assertGreater(v, 0, "VaR is reported as a positive loss")

    def test_an_all_positive_series_has_no_downside(self):
        self.assertEqual(dc.historical_var_95([1.0] * 40), 0.0)

    def test_the_tail_rank_convention_is_pinned(self):
        """VaR_95 is the threshold 5% of observations fall at or below.
        The first implementation used an interpolation rank and reported
        0.00 for a 20-day series whose worst day lost $100 - a VaR that
        could not see the only loss in the sample."""
        # n=20: 5% is ONE observation, so the worst one is the threshold.
        self.assertAlmostEqual(
            dc.historical_var_95([-100.0] + [1.0] * 19), 100.0, places=2)
        # n=100: 5% is five, so the 5th worst is the threshold.
        series = [-50.0, -40.0, -30.0, -20.0, -10.0] + [5.0] * 95
        self.assertAlmostEqual(dc.historical_var_95(series), 10.0, places=2)
        # n=40: 5% is two, so the 2nd worst.
        self.assertAlmostEqual(
            dc.historical_var_95([-9.0, -3.0] + [1.0] * 38), 3.0, places=2)

    def test_it_is_none_for_this_account_because_no_series_is_exposed(self):
        """/alpaca-overview/equity-curve publishes summary statistics
        only - low, high, percentile, counts - and no raw point list, so
        there is no distribution to compute a VaR from. None with a
        reason is the honest field value; a number here would be
        invented."""
        self.assertIsNone(dc.historical_var_95(None))
        r = dc.RiskMetrics(drawdown_pct=0.0, exposure_pct=0.3825)
        self.assertIsNone(r.var_95)

    def test_nan_is_dropped_not_counted_as_zero(self):
        nan = float("nan")
        self.assertIsNone(dc.historical_var_95([nan] * 25),
                          "25 NaNs is not 25 observations")


# ═══ [7] THE LIVE BOOK, END TO END ════════════════════════════════════

class TheLiveBookThroughTheWholeContract(unittest.TestCase):
    def setUp(self):
        self.positions = [pos(**DOG), pos(**RWM)]
        self.rows = dc.trapped_rows(self.positions)

    def test_both_positions_are_rows(self):
        self.assertEqual([r.symbol for r in self.rows], ["DOG", "RWM"])

    def test_the_card_reports_both_figures_and_both_severities(self):
        card = dc.trapped_capital_card(self.rows)
        self.assertEqual(card["positions_locked"], 2)
        self.assertAlmostEqual(card["locked_value_usd"], 242.34, places=2)
        self.assertAlmostEqual(card["cannot_follow_strategy_usd"], 372.73,
                               places=2)
        self.assertEqual(card["critical"], ["DOG"])
        self.assertEqual(card["warning"], ["RWM"])
        self.assertEqual(card["status"], "ATTENTION REQUIRED")
        self.assertEqual(card["oldest_lock_minutes"], 356)

    def test_the_gate_on_the_live_account_is_no_go(self):
        d = tg.TradingGate().evaluate(
            drawdown_pct=0.0, daily_pnl_pct=-0.14, blocked_exits=2,
            trapped_capital_count=2, profit_factor=0.519,
            exposure_pct=0.3825, exposure_limit=0.60,
            broker_connected=True, database_connected=True,
            alerts_operational=False, now=NOW)
        self.assertEqual(d.status, tg.NO_GO)
        joined = " ".join(d.reasons)
        for expected in ("Alerting unavailable", "Blocked exits detected",
                         "Capital trapped", "Profit factor too low"):
            self.assertIn(expected, joined)
        self.assertNotIn("Exposure exceeded", joined, "38% is under 60%")
        self.assertNotIn("Daily loss limit", joined, "-0.14% is noise")

    def test_the_whole_response_serialises(self):
        import json
        card = dc.trapped_capital_card(self.rows)
        d = tg.TradingGate().evaluate(
            drawdown_pct=0.0, daily_pnl_pct=-0.14, blocked_exits=2,
            trapped_capital_count=2, profit_factor=0.519,
            exposure_pct=0.3825, exposure_limit=0.60,
            broker_connected=True, database_connected=True,
            alerts_operational=False, now=NOW)
        ccc = dc.CapitalCommandCenter(
            portfolio=dc.PortfolioSummary(
                timestamp=NOW, equity=974.47, cash=601.74,
                buying_power=601.74, daily_pl=-1.40, daily_pnl_pct=-0.14),
            risk=dc.RiskMetrics(drawdown_pct=0.0, exposure_pct=0.3825,
                                risk_score=d.score),
            execution=dc.ExecutionHealth(blocked_exits=2,
                                         alerts_operational=False),
            positions=self.positions, trapped_capital=self.rows,
            alerts=[r.as_alert() for r in self.rows],
            growth=card, decision=d.as_dict())
        blob = json.dumps(ccc.as_dict())
        self.assertGreater(len(blob), 500)
        back = json.loads(blob)
        self.assertEqual(set(back.keys()),
                         {"portfolio", "risk", "execution", "positions",
                          "trapped_capital", "alerts", "growth", "decision"})
        self.assertFalse(back["execution"]["clean"])
        self.assertEqual(back["decision"]["status"], "NO_GO")


if __name__ == "__main__":
    unittest.main(verbosity=2)

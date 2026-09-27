"""Tests for the eleven figures that say WHY the profit is small.

The point of this module is diagnosis, so the failures that matter most
are not arithmetic slips - they are a confident rate built on nine
trades, a velocity figure printed without the edge beside it, and a
bottleneck verdict that names the wrong cause and sends the next month
of work in the wrong direction.

So the refusals are tested before the numbers.
"""
import ast
import math
from datetime import datetime, timedelta

import pytest

import capital_kpis as ck

T0 = datetime(2026, 9, 1, 0, 0, 0)


def tr(pnl, *, qty=1.0, entry=100.0, opened=None, closed=None, coin="BTC-USD"):
    return {
        "pnl": pnl,
        "qty": qty,
        "entry_price": entry,
        "exit_price": entry + pnl / qty if qty else None,
        "opened_at": opened,
        "closed_at": closed,
        "product_id": coin,
    }


def series(n, pnl=1.0, *, days=10.0, hold_hours=6.0, qty=1.0, entry=100.0):
    """n trades spread evenly over `days`, each held `hold_hours`."""
    step = timedelta(days=days / max(n - 1, 1))
    out = []
    for i in range(n):
        closed = T0 + step * i
        out.append(tr(pnl if not callable(pnl) else pnl(i),
                      qty=qty, entry=entry,
                      opened=(closed - timedelta(hours=hold_hours)).isoformat(),
                      closed=closed.isoformat()))
    return out


# ------------------------------------------------------ refusals first

def test_no_trades_refuses_every_rate_instead_of_reporting_zero():
    k = ck.compute([], allocated_usd=1000.0)
    assert k["trades"] == 0
    for withheld in ("trades_per_day", "net_per_day_usd", "profit_factor",
                     "net_edge_per_trade_usd", "win_rate_pct", "avg_hold_hours"):
        assert k[withheld] is None, withheld
    assert ck.bottleneck(k)[0] == "NOT_TRADING"


def test_a_rate_is_withheld_below_the_minimum_sample():
    k = ck.compute(series(ck.MIN_TRADES_FOR_RATE - 1), allocated_usd=1000.0)
    assert k["trades_per_day"] is None
    assert k["net_per_day_usd"] is None
    assert k["sample_warning"] and str(ck.MIN_TRADES_FOR_RATE) in k["sample_warning"]
    assert ck.bottleneck(k)[0] == "TOO_EARLY"


def test_a_rate_is_paid_once_the_sample_is_there():
    k = ck.compute(series(ck.MIN_TRADES_FOR_RATE, pnl=1.0, days=10.0),
                   allocated_usd=1000.0)
    assert k["sample_warning"] is None
    assert k["trades_per_day"] == pytest.approx(3.0, abs=0.01)
    assert k["net_per_day_usd"] == pytest.approx(3.0, abs=0.01)


def test_thirty_trades_in_one_hour_is_not_a_per_day_rate():
    """The original divided by a 1e-9 floor and reported 3e10 trades/day.

    Enough trades is not the same as enough time. A rate needs both.
    """
    k = ck.compute(series(40, days=1 / 24.0), allocated_usd=1000.0)
    assert k["trades"] == 40
    assert k["trades_per_day"] is None
    assert k["net_per_day_usd"] is None
    assert k["sample_warning"] and "extrapolation" in k["sample_warning"]
    assert ck.bottleneck(k)[0] == "TOO_EARLY"


def test_every_trade_closed_at_the_same_instant_cannot_produce_a_rate():
    same = [tr(1.0, closed=T0.isoformat()) for _ in range(50)]
    k = ck.compute(same, allocated_usd=1000.0)
    assert k["trades_per_day"] is None
    for v in k.values():
        assert v != float("inf") and v == v if isinstance(v, float) else True


def test_profit_factor_is_withheld_below_its_own_minimum():
    n = ck.MIN_TRADES_FOR_FACTOR - 1
    mixed = series(n, pnl=lambda i: 1.0 if i % 2 else -0.5)
    assert ck.compute(mixed, allocated_usd=1000.0)["profit_factor"] is None


def test_unreadable_rows_are_counted_never_guessed_at():
    rows = series(10) + [{"pnl": None}, {"pnl": "abc"}, "not a dict", {"pnl": float("nan")}]
    k = ck.compute(rows, allocated_usd=1000.0)
    assert k["trades"] == 10
    assert k["unreadable_rows"] == 4


def test_no_allocation_means_no_per_dollar_figure_not_a_crash():
    for alloc in (0.0, None, -5.0, "x"):
        k = ck.compute(series(40), allocated_usd=alloc)
        assert k["profit_per_1000_deployed_usd"] is None, alloc
        assert k["capital_velocity"] is None, alloc


def test_velocity_is_withheld_when_any_trade_is_unsized():
    """A partial sum divided by full capital UNDER-reports velocity.

    Silently cycling only the rows that happen to carry a quantity would
    make the headline figure look better the more data is missing.
    """
    rows = series(40)
    rows[7]["qty"] = None
    k = ck.compute(rows, allocated_usd=1000.0)
    assert k["capital_velocity"] is None
    assert k["exposure_cycled_usd"] is None


# ------------------------------------------- the velocity trap itself

def test_velocity_never_ships_without_the_counter_evidence():
    k = ck.compute(series(40, qty=1.0, entry=100.0), allocated_usd=1000.0)
    note = k["velocity_note"]
    for must in ("0.9%", "3.75%", "-71.1%", "+65.4%", "net_edge_per_trade_usd"):
        assert must in note, must


def test_velocity_is_always_reported_beside_the_edge():
    k = ck.compute(series(40), allocated_usd=1000.0)
    assert k["capital_velocity"] is not None
    assert "net_edge_per_trade_usd" in k


def test_velocity_is_exposure_over_capital():
    k = ck.compute(series(40, qty=2.0, entry=50.0), allocated_usd=1000.0)
    #  40 trades x 2 x $50 = $4,000 cycled on $1,000
    assert k["exposure_cycled_usd"] == pytest.approx(4000.0)
    assert k["capital_velocity"] == pytest.approx(4.0)


# ------------------------------------------------------- the arithmetic

def test_net_is_the_sum_and_the_edge_is_its_mean():
    rows = series(40, pnl=lambda i: 1.0 if i % 2 else -0.5)
    k = ck.compute(rows, allocated_usd=1000.0)
    assert k["net_usd"] == pytest.approx(sum(r["pnl"] for r in rows))
    assert k["net_edge_per_trade_usd"] == pytest.approx(k["net_usd"] / 40)


def test_profit_per_thousand_scales_with_the_capital_not_the_profit():
    rows = series(40, pnl=1.0)
    small = ck.compute(rows, allocated_usd=500.0)
    big = ck.compute(rows, allocated_usd=5000.0)
    assert small["net_usd"] == big["net_usd"]
    assert small["profit_per_1000_deployed_usd"] == pytest.approx(80.0)
    assert big["profit_per_1000_deployed_usd"] == pytest.approx(8.0)


def test_drawdown_is_measured_on_the_realised_curve_and_is_never_positive():
    rows = series(40, pnl=lambda i: 10.0 if i < 10 else (-1.0 if i < 30 else 2.0))
    k = ck.compute(rows, allocated_usd=1000.0)
    assert k["max_drawdown_usd"] == pytest.approx(-20.0)
    assert k["max_drawdown_usd"] <= 0


def test_a_book_that_only_loses_reports_its_whole_loss_as_drawdown():
    k = ck.compute(series(40, pnl=-1.0), allocated_usd=1000.0)
    assert k["max_drawdown_usd"] == pytest.approx(-40.0)


def test_a_scratch_is_neither_a_win_nor_a_loss():
    """pnl == 0 counted as a loss dragged the average loss toward zero
    and made the stop look gentler than it is."""
    rows = (series(20, pnl=1.0, days=10.0)
            + series(10, pnl=-2.0, days=10.0)
            + series(10, pnl=0.0, days=10.0))
    k = ck.compute(rows, allocated_usd=1000.0)
    assert k["trades"] == 40
    assert k["scratch_trades"] == 10
    assert k["win_rate_pct"] == pytest.approx(50.0)
    assert k["avg_loss_usd"] == pytest.approx(-2.0)
    assert k["profit_factor"] == pytest.approx(1.0)


def test_hold_time_comes_only_from_trades_that_have_both_ends():
    rows = series(10, hold_hours=6.0)
    rows[0]["opened_at"] = None
    rows[1]["opened_at"] = "garbage"
    rows[2]["opened_at"] = (T0 + timedelta(days=99)).isoformat()   # closes before it opens
    k = ck.compute(rows, allocated_usd=1000.0)
    assert k["hold_samples"] == 7
    assert k["avg_hold_hours"] == pytest.approx(6.0)


def test_idle_is_a_share_of_the_bots_own_capital():
    k = ck.compute(series(40), allocated_usd=600.0, free_cash_usd=400.0)
    assert k["idle_capital_pct"] == pytest.approx(40.0)


def test_outside_is_a_share_of_the_whole_account():
    k = ck.compute(series(40), allocated_usd=553.89, free_cash_usd=481.52,
                   account_total_usd=11426.73)
    assert k["outside_any_branch_pct"] == pytest.approx(90.94, abs=0.05)


def test_a_stale_snapshot_never_reports_a_negative_share():
    k = ck.compute(series(40), allocated_usd=9000.0, free_cash_usd=9000.0,
                   account_total_usd=11426.73)
    assert k["outside_any_branch_pct"] == 0.0


# --------------------------------------------------- the one verdict

def base(**over):
    k = {"trades": 40, "sample_warning": None, "net_edge_per_trade_usd": 0.5,
         "outside_any_branch_pct": 10.0, "idle_capital_pct": 10.0,
         "capital_velocity": 3.0}
    k.update(over)
    return k


def test_a_negative_edge_outranks_every_placement_problem():
    """More capital and more velocity both SCALE a negative edge."""
    code, why = ck.bottleneck(base(net_edge_per_trade_usd=-0.02,
                                   outside_any_branch_pct=95.0,
                                   idle_capital_pct=90.0,
                                   capital_velocity=0.1))
    assert code == "NO_EDGE"
    assert "scale a loss" in why


def test_a_zero_edge_is_no_edge_not_healthy():
    assert ck.bottleneck(base(net_edge_per_trade_usd=0.0))[0] == "NO_EDGE"


def test_capital_outside_outranks_idle_and_velocity():
    code, why = ck.bottleneck(base(outside_any_branch_pct=90.9,
                                   idle_capital_pct=90.0,
                                   capital_velocity=0.1))
    assert code == "CAPITAL_OUTSIDE"
    assert "90.9" in why


def test_idle_outranks_velocity():
    assert ck.bottleneck(base(idle_capital_pct=46.0,
                              capital_velocity=0.1))[0] == "CAPITAL_IDLE"


def test_low_velocity_is_the_last_cause_not_the_first():
    assert ck.bottleneck(base(capital_velocity=0.4))[0] == "LOW_VELOCITY"


def test_healthy_says_size_is_the_only_lever_left():
    code, why = ck.bottleneck(base())
    assert code == "HEALTHY" and "size" in why.lower()


def test_a_missing_figure_never_becomes_a_verdict():
    """None is not a small number. `0.0 or -999` already cost this fleet
    once - a coin earning nothing sorted below one losing 14% a day."""
    code, _ = ck.bottleneck(base(net_edge_per_trade_usd=None,
                                 outside_any_branch_pct=None,
                                 idle_capital_pct=None,
                                 capital_velocity=None))
    assert code == "HEALTHY"          # nothing known to be wrong, nothing invented


def test_bottleneck_of_nothing_does_not_raise():
    for junk in (None, {}, {"trades": None}, {"trades": 0}):
        assert ck.bottleneck(junk)[0] == "NOT_TRADING"


def test_the_verdict_is_always_a_code_and_a_sentence():
    known = {"NOT_TRADING", "TOO_EARLY", "NO_EDGE", "CAPITAL_OUTSIDE",
             "CAPITAL_IDLE", "LOW_VELOCITY", "HEALTHY"}
    for k in (base(), base(net_edge_per_trade_usd=-1.0),
              base(outside_any_branch_pct=99.0), base(idle_capital_pct=99.0),
              base(capital_velocity=0.1), base(sample_warning="9 trades. Rates are withheld below 30."), {}):
        code, why = ck.bottleneck(k)
        assert code in known
        assert isinstance(why, str) and len(why) > 20


# ------------------------------------------ structural guards

SRC = open("capital_kpis.py").read()
TREE = ast.parse(SRC)


def test_the_verdict_only_reads_keys_the_report_actually_produces():
    """Key drift is silent: bottleneck() asking for a key compute() no
    longer emits reads as None, and the verdict quietly degrades to
    HEALTHY instead of failing."""
    produced = set(ck.compute(series(40), allocated_usd=1000.0))
    fn = next(n for n in TREE.body
              if isinstance(n, ast.FunctionDef) and n.name == "bottleneck")
    asked = set()
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("get", "__getitem__") and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            asked.add(node.args[0].value)
        if (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)):
            asked.add(node.slice.value)
    assert asked, "found no key reads - the walk is broken, not the code"
    assert asked <= produced, asked - produced


def test_nothing_here_annualises_a_two_week_sample():
    for node in ast.walk(TREE):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            assert node.value not in (365, 365.0, 252, 252.0, 8760), node.value


def test_the_report_touches_no_venue_and_no_database():
    for node in ast.walk(TREE):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] + [getattr(node, "module", None)]
            for n in names:
                assert not any(b in (n or "") for b in
                               ("aiohttp", "requests", "coinbase", "sqlalchemy",
                                "models", "database", "os")), n


def test_every_reported_value_survives_json():
    import json
    k = ck.compute(series(40), allocated_usd=1000.0, free_cash_usd=100.0,
                   account_total_usd=11426.73)
    round_tripped = json.loads(json.dumps(k))
    assert round_tripped == k
    for key, v in k.items():
        if isinstance(v, float):
            assert math.isfinite(v), key


def test_the_report_changes_nothing_it_is_given():
    rows = series(40)
    before = [dict(r) for r in rows]
    ck.compute(rows, allocated_usd=1000.0)
    assert rows == before

"""The growth model - and every way a growth number can lie."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import growth_model as gm  # noqa: E402


def T(entry, qty, pnl):
    return {"entry_price": entry, "qty": qty, "pnl": pnl}


def B(pid, alloc, levels, slices, unreal=0.0):
    return {"product_id": pid, "allocated_usd": alloc, "num_levels": levels,
            "slices": [{"entry_price": p, "qty": q} for p, q in slices],
            "total_unrealized_net_usd": unreal}


# ── the edge ───────────────────────────────────────────────────────────

def test_the_edge_is_measured_from_real_trades():
    out = gm.measure_edge([T(100, 1, 2.0), T(100, 1, 3.0), T(100, 1, 1.0)])
    assert out["round_trips"] == 3
    assert out["median_net_pct"] == 2.0
    assert out["win_rate_pct"] == 100.0


def test_an_unpriceable_trade_is_dropped_not_counted_as_zero():
    """Calling it 0% would drag the median toward a number nothing
    produced."""
    out = gm.measure_edge([T(100, 1, 2.0), T(0, 1, 5.0), T(100, 1, 2.0)])
    assert out["round_trips"] == 2
    assert out["dropped_unpriceable"] == 1
    assert out["median_net_pct"] == 2.0


@pytest.mark.parametrize("bad", [None, 0, -1, "x"])
def test_bad_prices_and_quantities_are_dropped(bad):
    out = gm.measure_edge([T(bad, 1, 2.0), T(100, bad, 2.0)])
    assert out["readable"] is False


def test_no_trades_is_unreadable_not_zero_edge():
    assert gm.measure_edge([])["readable"] is False
    assert gm.measure_edge(None)["readable"] is False


def test_the_blended_figure_weights_by_notional():
    """A $1000 trade and a $10 trade are not equal evidence."""
    out = gm.measure_edge([T(1000, 1, 10.0), T(10, 1, 1.0)])
    assert out["blended_net_pct"] == pytest.approx(11 / 1010 * 100, abs=1e-3)
    assert out["median_net_pct"] != out["blended_net_pct"]


def test_losers_lower_the_win_rate():
    out = gm.measure_edge([T(100, 1, 2.0), T(100, 1, -1.0)])
    assert out["win_rate_pct"] == 50.0


# ── the capital ────────────────────────────────────────────────────────

def test_deployed_capital_comes_from_slices_not_allocation():
    """Allocation is an intention; a slice is a position."""
    out = gm.measure_capital([B("X", 300.0, 3, [(100, 1)])], free_cash_usd=0)
    assert out["deployed_usd"] == 100.0
    assert out["idle_in_branch_usd"] == 200.0


def test_free_cash_counts_as_not_working():
    out = gm.measure_capital([B("X", 100.0, 3, [(100, 1)])], free_cash_usd=50.0)
    assert out["not_working_usd"] == 50.0
    assert out["total_capital_usd"] == 150.0


def test_a_branch_deployed_past_its_allocation_does_not_go_negative_idle():
    """Idle is money not at work. A branch over its allocation has none;
    reporting a negative would net off against a real idle balance
    elsewhere and understate the total."""
    out = gm.measure_capital([B("X", 100.0, 3, [(100, 2)])], free_cash_usd=0)
    assert out["idle_in_branch_usd"] == 0.0


def test_concentration_names_the_biggest_holdings_and_their_share():
    out = gm.measure_capital([
        B("BIG1", 2000.0, 3, [(100, 20)], unreal=-100.0),
        B("BIG2", 2000.0, 3, [(100, 20)], unreal=-100.0),
        B("small", 100.0, 3, [(100, 1)], unreal=-1.0),
    ], free_cash_usd=0)
    c = out["concentration"]
    assert set(c["products"]) == {"BIG1", "BIG2"}
    assert c["share_pct"] == pytest.approx(97.6, abs=0.1)
    assert c["share_of_unrealized_pct"] == pytest.approx(99.5, abs=0.1)


def test_a_branch_holding_more_slices_than_levels_is_flagged():
    """The grid holds at most num_levels positions. Anything beyond that
    was opened by some other path."""
    out = gm.measure_capital([B("X", 300.0, 3, [(10, 1)] * 7)], free_cash_usd=0)
    assert len(out["slices_over_levels"]) == 1
    assert out["slices_over_levels"][0]["open_slices"] == 7


def test_a_branch_within_its_levels_is_not_flagged():
    out = gm.measure_capital([B("X", 300.0, 3, [(10, 1)] * 3)], free_cash_usd=0)
    assert out["slices_over_levels"] == []


def test_an_unreadable_allocation_is_named_not_dropped_silently():
    out = gm.measure_capital([{"product_id": "X", "num_levels": 3},
                              B("Y", 100.0, 3, [(100, 1)])], free_cash_usd=0)
    assert out["unreadable_branches"] == ["X"]


def test_no_branches_is_unreadable():
    assert gm.measure_capital([], 0)["readable"] is False


# ── the rate, and the refusal to extrapolate ───────────────────────────

def test_a_short_span_refuses_to_produce_a_rate():
    """$4.30 over 0.04 days became a 0.95%/day claim once. Never again."""
    out = gm.project(4.30, 9000.0, 0.04)
    assert out["readable"] is False
    assert out["verdict"] == "UNKNOWN"
    assert "withdrawn" in out["reason"]


def test_the_span_floor_is_enforced_at_the_boundary():
    assert gm.project(10, 1000, gm.MIN_SPAN_DAYS - 0.1)["readable"] is False
    assert gm.project(10, 1000, gm.MIN_SPAN_DAYS)["readable"] is True


def test_a_real_span_produces_a_rate_and_a_table():
    out = gm.project(71.67, 9011.34, 30.0)
    assert out["verdict"] == "MEASURED"
    assert out["monthly_pct"] == pytest.approx(0.7953, abs=0.001)
    assert len(out["compounding"]) == len(gm.PROJECTION_MONTHS)


def test_the_compounding_is_actually_compounded():
    out = gm.project(100.0, 1000.0, 30.0)          # 10%/month
    twelve = [r for r in out["compounding"] if r["months"] == 12][0]
    assert twelve["value_usd"] == pytest.approx(1000 * 1.1 ** 12, abs=0.5)
    assert twelve["value_usd"] > 1000 * (1 + 0.10 * 12)   # beats simple interest


def test_the_annual_figure_matches_the_monthly_one():
    out = gm.project(100.0, 1000.0, 30.0)
    assert out["annual_pct_compounded"] == pytest.approx((1.1 ** 12 - 1) * 100, abs=0.1)


def test_the_projection_says_it_is_not_a_forecast():
    out = gm.project(71.67, 9011.34, 30.0)
    assert "assume it repeats" in out["this_is_arithmetic_not_a_forecast"]
    assert "unrealised is not in this number" in out["this_is_arithmetic_not_a_forecast"]


@pytest.mark.parametrize("cap", [0, -1, None])
def test_a_zero_or_missing_capital_refuses_rather_than_dividing(cap):
    assert gm.project(10, cap, 30)["readable"] is False


def test_a_loss_produces_a_shrinking_curve_not_an_error():
    out = gm.project(-90.0, 1000.0, 30.0)
    assert out["verdict"] == "MEASURED"
    assert out["monthly_pct"] < 0
    assert out["compounding"][-1]["value_usd"] < 1000


# ── the ceiling ────────────────────────────────────────────────────────

def test_the_ceiling_prices_the_idle_capital_at_the_working_rate():
    cap = gm.measure_capital([B("X", 100.0, 3, [(100, 1)])], free_cash_usd=900.0)
    out = gm.ceiling_if_idle_worked(cap, 1.03)
    assert out["idle_capital_usd"] == 900.0
    assert out["extra_monthly_usd_if_it_worked"] == pytest.approx(9.27, abs=0.01)


def test_the_ceiling_says_out_loud_that_it_is_a_ceiling():
    """Capital can be idle BECAUSE its branch found nothing worth
    buying. Presenting the top of the range as the middle is the
    dishonest move this field exists to block."""
    cap = gm.measure_capital([B("X", 100.0, 3, [(100, 1)])], free_cash_usd=900.0)
    out = gm.ceiling_if_idle_worked(cap, 1.03)
    assert "would not earn the working rate" in out["is_a_ceiling_not_a_forecast"]
    assert "never the middle" in out["is_a_ceiling_not_a_forecast"]


def test_the_ceiling_refuses_on_unreadable_capital():
    assert gm.ceiling_if_idle_worked({"readable": False}, 1.0)["readable"] is False


# ── realised and unrealised are never added ────────────────────────────

def test_realised_and_unrealised_stay_apart():
    """Realised is banked. Unrealised is a price quote that moves while
    you read it. A single 'profit' number that adds them is a lie in
    both directions."""
    cap = gm.measure_capital([B("X", 100.0, 3, [(100, 1)], unreal=-50.0)], free_cash_usd=0)
    proj = gm.project(10.0, 100.0, 30.0)
    assert cap["unrealized_usd"] == -50.0
    assert proj["realised_usd"] == 10.0
    assert "unrealised is not in this number" in proj["this_is_arithmetic_not_a_forecast"]


# ── the endpoint ───────────────────────────────────────────────────────

import ast as _ast  # noqa: E402

_ROUTER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "routers", "trading_dashboard.py")
with open(_ROUTER, encoding="utf-8") as _fh:
    _SRC = _fh.read()
_TREE = _ast.parse(_SRC)


def _endpoint():
    for n in _ast.walk(_TREE):
        if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef)) \
                and n.name == "get_growth_model":
            return n
    raise AssertionError("get_growth_model not found")


def test_the_route_exists():
    assert '@router.get("/growth-model")' in _SRC


def test_the_endpoint_places_nothing():
    src = _ast.get_source_segment(_SRC, _endpoint())
    for forbidden in ("session.post", "session.delete", "/v2/orders",
                      "place_order", "close_position", "free_locked"):
        assert forbidden not in src, f"a read-only endpoint must not use {forbidden}"


def test_the_span_comes_from_the_trades_not_the_request():
    """Dividing by a requested 30 days when the data covers 26 would
    understate the rate on a denominator no data supports."""
    src = _ast.get_source_segment(_SRC, _endpoint())
    assert "span_days = " in src
    assert "max(stamps) - min(stamps)" in src
    assert "span_days = window" not in src


def test_the_endpoint_does_not_recompute_the_model():
    src = _ast.get_source_segment(_SRC, _endpoint())
    for fn in ("measure_edge", "measure_capital", "project", "ceiling_if_idle_worked"):
        assert f"gmod.{fn}(" in src


def test_a_single_trade_cannot_produce_a_span():
    """One timestamp is a point, not a span. len(stamps) > 1 or zero."""
    src = _ast.get_source_segment(_SRC, _endpoint())
    assert "len(stamps) > 1" in src

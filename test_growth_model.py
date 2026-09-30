"""The growth model - and every way a growth number can lie."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import growth_model as gm  # noqa: E402


def T(entry, qty, pnl):
    return {"entry_price": entry, "qty": qty, "pnl": pnl}


def B(pid, alloc, levels, slices, unreal=0.0, adopted=False):
    # opened_at is REQUIRED by the over-levels rule: it asks when a slice was
    # bought, not how many there are. Ordered, one minute apart, so the
    # fixture's list order is also its chronological order.
    return {"product_id": pid, "allocated_usd": alloc, "num_levels": levels,
            "slices": [{"entry_price": p, "qty": q, "adopted": adopted,
                        "opened_at": f"2026-09-27T00:{i:02d}:00Z"}
                       for i, (p, q) in enumerate(slices)],
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
    out = gm.measure_capital([B("X", 300.0, 3, [(10, 1)] * 7)], free_cash_usd=0)
    assert len(out["slices_over_levels"]) == 1
    assert out["slices_over_levels"][0]["open_slices"] == 7


# ── the distinction that makes this rule actionable ────────────────────
#
# The first version called every slices-over-levels branch a structural
# anomaly. Six lit up live, and all six were DELIBERATE: the adoption
# worker pins num_levels to the slice count on an adopted branch so the
# grid cannot average down coin it never chose to buy. The comment in
# crypto_grid_bot names ZEC and says raising the level count without
# changing the sizing "is what would have let ZEC spend 68.5% of the
# wallet averaging down its own worst position".
#
# A rule that fires on code it has no opinion about is a rule nobody can
# act on.

def _mixed(pid, levels, adopted_n, bought_n, buys_first=False):
    """Two real live shapes, and which one it is decides the verdict.

    buys_first=False (default) - the GRANT shape. The adopted slices are
    oldest and the bought ones opened last, while the branch was already
    full. That is the one-rung grant firing: BCH, SOL, LTC and ZEC all
    look like this live.

    buys_first=True - the HBAR shape. The bought slices are OLDEST, opened
    when the branch held 0 and 1 slices and was nowhere near its cap; the
    adoption landed on top afterwards and overshot it. No buy broke a rule,
    and the old count-based test called this one a breach."""
    b = B(pid, 300.0, levels, [(10, 1)] * (adopted_n + bought_n), adopted=True)
    idx = range(bought_n) if buys_first else range(-bought_n, 0)
    for i in idx:
        b["slices"][i]["adopted"] = False
    return b


def test_adopted_plus_exactly_one_bought_is_the_designed_shape():
    """THE LIVE SHAPE, and it took two passes to read correctly.
    crypto_grid_bot grants an adopted-only branch ONE extra rung so it
    can take a single dip; buying it makes the branch mixed and the cap
    snaps back to 3. All six live branches are exactly N adopted + 1
    bought. Requiring ALL slices to be adopted flagged every one of them
    as unexplained - the design guarantees one is bought."""
    out = gm.measure_capital([_mixed("ZEC", 3, 6, 1)], free_cash_usd=0)
    row = out["slices_over_levels"][0]
    assert row["adopted_slices"] == 6
    assert row["bought_slices"] == 1
    assert row["deliberate"] is True
    assert "not a breach" in row["why"]
    assert out["slices_over_levels_unexplained"] == []


def test_two_buys_made_WHILE_FULL_is_unexplained():
    """The one-rung grant only ever allows ONE. Two buys opened while the
    branch was already full means the clamp did not snap back."""
    out = gm.measure_capital([_mixed("X", 3, 5, 2)], free_cash_usd=0)
    row = out["slices_over_levels"][0]
    assert row["bought_slices"] == 2
    assert len(row["buys_past_the_cap"]) == 2
    assert row["deliberate"] is False
    assert len(out["slices_over_levels_unexplained"]) == 1
    assert "did not snap back" in row["why"]


def test_two_bought_slices_that_PREDATE_the_adoption_is_NOT_a_breach():
    """THE HBAR CASE, and the reason this rule was rewritten.

    HBAR live: 5 slices over a cap of 3, two of them bought. The old rule
    was `deliberate = adopted and bought <= 1` - a pure count - so it
    flagged HBAR as a breach and told the owner the clamp had failed.

    The timestamps say otherwise. Its two buys opened at 2026-09-28T18:37
    and 2026-09-29T00:25, when the branch held 0 and then 1 slice, days
    inside a cap of 3. The three ADOPTED slices all landed afterwards in
    one instant at 2026-09-29T13:01:32 and overshot the cap - which
    coin_adoption_worker documents as expected, because the spacing
    override rewrites num_levels back down every cycle.

    Not one buy broke a rule. Counting could never have shown that."""
    out = gm.measure_capital([_mixed("HBAR", 3, 3, 2, buys_first=True)],
                             free_cash_usd=0)
    row = out["slices_over_levels"][0]
    assert row["bought_slices"] == 2
    assert row["buys_past_the_cap"] == []
    assert row["deliberate"] is True
    assert "NOT ONE of the bought slices opened while the branch was full" in row["why"]
    assert out["slices_over_levels_unexplained"] == []


def test_a_missing_opened_at_makes_the_verdict_UNKNOWN_not_a_pass():
    """A gap is not a zero, and it is not a clean bill of health either."""
    b = _mixed("Y", 3, 3, 2, buys_first=True)
    b["slices"][0].pop("opened_at")
    out = gm.measure_capital([b], free_cash_usd=0)
    row = out["slices_over_levels"][0]
    assert row["timestamps_complete"] is False
    assert row["deliberate"] is None
    assert "UNKNOWN" in row["why"]
    # None is not False: an unreadable branch is not reported as a breach,
    # and it does not vanish either - it gets its own list.
    assert out["slices_over_levels_unexplained"] == []
    assert [r["product_id"] for r in out["slices_over_levels_unknown"]] == ["Y"]


def test_a_wholly_unadopted_branch_over_its_levels_is_unexplained():
    """Nothing was adopted, so no one-rung grant ever applied."""
    out = gm.measure_capital(
        [B("X", 300.0, 3, [(10, 1)] * 7, adopted=False)], free_cash_usd=0)
    row = out["slices_over_levels"][0]
    assert row["adopted_slices"] == 0
    assert row["deliberate"] is False
    assert len(out["slices_over_levels_unexplained"]) == 1


def test_all_six_live_branch_shapes_read_as_deliberate():
    """The exact live counts at 19:07Z: BCH 3+1, SOL 3+1, PEPE 6+1,
    LTC 6+1, ZEC 6+1, SHIB 6+1, all over a cap of 3."""
    live = [("BCH", 3, 1), ("SOL", 3, 1), ("PEPE", 6, 1),
            ("LTC", 6, 1), ("ZEC", 6, 1), ("SHIB", 6, 1)]
    out = gm.measure_capital(
        [_mixed(p, 3, a, b) for p, a, b in live], free_cash_usd=0)
    assert len(out["slices_over_levels"]) == 6
    assert out["slices_over_levels_unexplained"] == []


def test_the_adopted_count_is_reported_not_just_the_verdict():
    b = B("X", 300.0, 3, [(10, 1)] * 5, adopted=True)
    b["slices"][0]["adopted"] = False
    row = gm.measure_capital([b], free_cash_usd=0)["slices_over_levels"][0]
    assert row["adopted_slices"] == 4
    assert row["bought_slices"] == 1
    assert row["open_slices"] == 5


def test_a_branch_within_its_levels_is_never_in_either_list():
    out = gm.measure_capital(
        [B("X", 300.0, 3, [(10, 1)] * 3, adopted=False)], free_cash_usd=0)
    assert out["slices_over_levels"] == []
    assert out["slices_over_levels_unexplained"] == []


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

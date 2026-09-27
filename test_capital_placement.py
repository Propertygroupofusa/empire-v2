"""Tests for the thing that decides where the next dollar goes.

The expensive failure here is not a mis-sized branch. It is placing more
capital onto an edge that stopped working, or telling the owner a lever
is open when the engine has no way to pull it. Both read as progress.
"""
import ast

import pytest

import capital_placement as cp

KPI_GOOD = {"net_edge_per_trade_usd": 0.2363, "net_usd": 19.61}
KPI_BAD = {"net_edge_per_trade_usd": -0.02, "net_usd": -40.0}

HOLDINGS = [
    {"asset": "ZEC", "usd": 2250.15}, {"asset": "XRP", "usd": 2212.63},
    {"asset": "BTC", "usd": 1471.50}, {"asset": "ETH", "usd": 1248.58},
    {"asset": "SHIB", "usd": 1055.04}, {"asset": "USD", "usd": 892.30},
    {"asset": "XLM", "usd": 562.34}, {"asset": "QNT", "usd": 191.82},
    {"asset": "PEPE", "usd": 87.00}, {"asset": "DUST", "usd": 0.40},
]
CLAIMED = ("BTC-USD", "NEAR-USD", "BONK-USD", "ONDO-USD", "FLOKI-USD", "TIA-USD")
BRANCHES = [
    {"bot_name": "crypto_grid_1", "product_id": "BTC-USD", "allocated_usd": 69.23,
     "num_levels": 3, "grid_pct": 0.025,
     "slices": [{"qty": 0.0003, "entry_price": 84000.0},
                {"qty": 0.0002, "entry_price": 83000.0}]},
    {"bot_name": "crypto_grid_2", "product_id": "NEAR-USD", "allocated_usd": 207.74,
     "num_levels": 3, "grid_pct": 0.025,
     "slices": [{"qty": 3.0, "entry_price": 2.3}]},
    {"bot_name": "crypto_grid_3", "product_id": "BONK-USD", "allocated_usd": 69.23,
     "num_levels": 3, "grid_pct": 0.025, "slices": []},
]
STAGES = [
    {"product_id": "BTC-USD", "required_realized_pnl": 0.0, "backtested_roi_pct": None,
     "state": "active"},
    {"product_id": "ETH-USD", "required_realized_pnl": 0.0,
     "backtested_roi_pct": -9.61, "state": "below_minimum_edge"},
    {"product_id": "SOL-USD", "required_realized_pnl": 688.0,
     "backtested_roi_pct": 4.80, "state": "waiting_for_realized_profit"},
    {"product_id": "ADA-USD", "required_realized_pnl": 2106.0,
     "backtested_roi_pct": -36.70, "state": "waiting_for_prior_stage"},
    {"product_id": "DOGE-USD", "required_realized_pnl": 0.0,
     "backtested_roi_pct": -23.75, "state": "waiting_for_prior_stage"},
    {"product_id": "XRP-USD", "required_realized_pnl": 0.0,
     "backtested_roi_pct": -12.84, "state": "waiting_for_prior_stage"},
    {"product_id": "LINK-USD", "required_realized_pnl": 0.0,
     "backtested_roi_pct": -12.56, "state": "waiting_for_prior_stage"},
    {"product_id": "AVAX-USD", "required_realized_pnl": 0.0,
     "backtested_roi_pct": 11.06, "state": "waiting_for_prior_stage"},
    {"product_id": "DOT-USD", "required_realized_pnl": 0.0,
     "backtested_roi_pct": -10.95, "state": "waiting_for_prior_stage"},
]


def full(**over):
    kw = dict(kpis=KPI_GOOD, holdings=HOLDINGS, branches=BRANCHES,
              free_cash_usd=481.52, account_total_usd=11397.11,
              claimed_products=CLAIMED, eligible_products=(), stages=STAGES,
              realized_usd=19.61)
    kw.update(over)
    return cp.plan(**kw)


# --------------------------------------------------------- refusals first

def test_a_negative_edge_refuses_every_lever_at_once():
    """This is the one place the system could lose real money at scale:
    more capital onto a losing strategy is the same loss, larger."""
    p = full(kpis=KPI_BAD)
    assert p["ok"] is False
    assert p["refused"] == "EDGE_IS_NOT_POSITIVE"
    assert p["total_addressable_usd"] == 0.0
    assert "larger and sooner" in p["detail"]


def test_a_zero_edge_is_refused_too():
    assert full(kpis={"net_edge_per_trade_usd": 0.0})["refused"] == "EDGE_IS_NOT_POSITIVE"


@pytest.mark.parametrize("k", [None, {}, {"net_edge_per_trade_usd": None},
                               {"net_edge_per_trade_usd": "x"},
                               {"net_edge_per_trade_usd": float("nan")}])
def test_an_unreadable_edge_is_never_treated_as_a_positive_one(k):
    p = full(kpis=k)
    assert p["ok"] is False and p["refused"] == "EDGE_UNKNOWN"


def test_a_refusal_still_shows_the_levers_it_refused():
    """Hiding them would make a refusal look like an empty account."""
    p = full(kpis=KPI_BAD)
    assert {l["lever"] for l in p["levers"]} == {
        "ADOPT_HELD_COIN", "FILL_RUNGS", "FUND_NEW_BRANCH", "COMPOUND_REALIZED"}


def test_the_plan_never_changes_anything():
    assert full()["is_a_plan_not_a_change"] is True


def test_nothing_here_places_an_order_or_touches_a_database():
    tree = ast.parse(open("capital_placement.py").read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] + [getattr(node, "module", None)]
            for n in names:
                assert not any(b in (n or "") for b in
                               ("aiohttp", "requests", "coinbase", "sqlalchemy",
                                "models", "database", "crypto_grid_bot")), n
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            called.add(f.id if isinstance(f, ast.Name) else getattr(f, "attr", ""))
    for banned in ("create_grid_branch", "place_market_sell", "place_market_buy",
                   "commit", "execute"):
        assert banned not in called, f"the planner CALLS {banned}"


# ------------------------------------------------- the adoption lever

def test_held_coin_outside_every_branch_is_the_biggest_lever():
    p = full()
    assert p["biggest_lever"] == "ADOPT_HELD_COIN"
    lev = p["levers"][0]
    #  ZEC + XRP + ETH + SHIB + XLM + QNT + PEPE, minus claimed BTC, minus USD, minus dust
    assert lev["usd_addressable"] == pytest.approx(7607.56, abs=0.01)


def test_cash_is_never_an_adoption_candidate():
    lev = cp.lever_adopt(HOLDINGS, CLAIMED, 11397.11)
    assert not any(c["asset"] in cp.STABLE for c in lev["candidates"])


def test_a_coin_a_branch_already_manages_is_not_adoptable():
    lev = cp.lever_adopt(HOLDINGS, CLAIMED, 11397.11)
    assert "BTC" not in {c["asset"] for c in lev["candidates"]}


def test_coin_too_small_to_carry_a_branch_is_left_out():
    lev = cp.lever_adopt([{"asset": "TINY", "usd": 12.0}], (), 11397.11)
    assert lev["candidates"] == [] and lev["usd_addressable"] == 0.0


def test_a_position_over_the_twenty_percent_rule_is_flagged_not_hidden():
    """Putting a grid on an over-weight position manages it rather than
    reducing it - that is the trimmer's job, and they must not be confused."""
    lev = cp.lever_adopt([{"asset": "ZEC", "usd": 3000.0}], (), 11397.11)
    assert lev["candidates"][0]["over_position_limit"] is True


def test_adoption_is_reported_as_blocked_because_the_engine_cannot_do_it():
    """The engine funds a branch from CASH. Reporting this lever as open
    would be the dashboard lying in the expensive direction."""
    lev = cp.lever_adopt(HOLDINGS, CLAIMED, 11397.11)
    assert lev["blocked_by"] == "NO_ADOPTION_PATH"
    assert "without a single dollar being spent" in lev["what_it_would_take"]


# ---------------------------------------------------- the rungs lever

def test_unspent_earmarks_are_counted_as_the_gap_they_are():
    lev = cp.lever_fill_rungs(BRANCHES)
    #  claimed 346.20, deployed 0.0252+0.0166+6.90 => the difference
    assert lev["claimed_usd"] == pytest.approx(346.20, abs=0.01)
    assert lev["usd_addressable"] == pytest.approx(
        346.20 - lev["deployed_coin_usd"], abs=0.01)


def test_empty_rungs_are_counted_across_the_fleet():
    #  3 branches x 3 levels = 9, with 2 + 1 + 0 open
    assert cp.lever_fill_rungs(BRANCHES)["empty_rungs"] == 6


def test_filling_rungs_never_proposes_a_tighter_step_than_the_floor():
    lev = cp.lever_fill_rungs(BRANCHES)
    assert "-71.1%" in lev["what_it_would_take"]


def test_a_branch_with_unreadable_slices_does_not_inflate_the_gap():
    lev = cp.lever_fill_rungs([{"bot_name": "x", "allocated_usd": 100.0,
                                "num_levels": 3,
                                "slices": [{"qty": None, "entry_price": 5.0}]}])
    assert lev["deployed_coin_usd"] == 0.0
    assert lev["usd_addressable"] == 100.0


# --------------------------------------------------- the funding lever

def test_the_reserve_is_never_spent():
    lev = cp.lever_fund(481.52, reserve_usd=88.0, eligible_products=("AVAX-USD",))
    assert lev["usd_addressable"] == pytest.approx(393.52)


def test_cash_below_a_tradeable_branch_is_refused_not_deployed():
    lev = cp.lever_fund(100.0, reserve_usd=88.0, eligible_products=("AVAX-USD",))
    assert lev["blocked_by"] == "BELOW_A_TRADEABLE_BRANCH"
    assert "waiting" in lev["what_it_would_take"]


def test_cash_with_no_eligible_coin_waits_rather_than_lowering_the_gate():
    lev = cp.lever_fund(481.52, eligible_products=())
    assert lev["blocked_by"] == "NO_COIN_CLEARS_THE_EDGE_GATE"


def test_funding_is_open_when_there_is_cash_and_a_coin():
    lev = cp.lever_fund(481.52, eligible_products=("AVAX-USD",))
    assert lev["blocked_by"] is None


# ------------------------------------------------- the compound lever

def test_banked_profit_is_not_offered_a_second_time():
    """run_grid_branch_cycle already does allocated_usd += pnl on every
    FIFO sell, so the realised $19.61 is inside the $553.89 the branches
    claim. Offering it again deploys the same profit twice - claims up,
    backing flat, which is an unbacked branch by definition."""
    lev = cp.lever_compound(19.61)
    assert lev["usd_addressable"] == 0.0
    assert lev["blocked_by"] == "ALREADY_AUTOMATIC"
    assert "already runs" in lev["what_it_would_take"]


def test_a_missing_compounded_figure_is_never_read_as_none_compounded():
    """The dangerous default. Absent evidence, every banked dollar is
    assumed already compounded, because the trading loop compounds it."""
    assert cp.lever_compound(500.0)["already_compounded_usd"] == 500.0
    assert cp.lever_compound(500.0)["usd_addressable"] == 0.0


def test_profit_the_branches_really_have_not_absorbed_is_flagged():
    lev = cp.lever_compound(500.0, already_compounded_usd=100.0)
    assert lev["usd_addressable"] == pytest.approx(400.0)
    assert lev["blocked_by"] is None
    assert "should not happen" in lev["what_it_would_take"]


def test_a_loss_never_compounds_into_a_negative_placement():
    assert cp.lever_compound(-40.0)["usd_addressable"] == 0.0


def test_the_compound_lever_is_not_counted_as_open_capital():
    p = full()
    assert "COMPOUND_REALIZED" not in (p["open_levers"] or [])


# ----------------------------------------- the ladder ordering defect

def test_five_coins_with_a_zero_gate_are_waiting_on_another_coins_gate():
    """THE BUG. ADAPTIVE_FLEET_STAGES is walked in tuple order, and SOL's
    $688 gate sets sequence_blocked - so DOGE, XRP, LINK, AVAX and DOT,
    whose own gates are $0.00, report waiting_for_prior_stage."""
    d = cp.ladder_diagnosis(STAGES)
    assert d["is_ordering_defect"] is True
    frozen = {r["product_id"] for r in d["frozen_behind_another_coins_gate"]}
    assert frozen == {"DOGE-USD", "XRP-USD", "LINK-USD", "AVAX-USD", "DOT-USD"}


def test_a_coin_with_a_real_gate_of_its_own_is_not_called_frozen():
    """ADA genuinely asks for $2,106. It is not a victim of the ordering."""
    d = cp.ladder_diagnosis(STAGES)
    assert "ADA-USD" not in {r["product_id"] for r in d["frozen_behind_another_coins_gate"]}
    assert "ADA-USD" in {r["product_id"] for r in d["gated_on_their_own_profit_requirement"]}


def test_unfreezing_the_ladder_is_reported_as_opening_nothing():
    """The honest half, and the one that matters. Every frozen coin is
    then refused on its own measured edge, so the fix changes a FALSE
    reason into a TRUE one and adds no branches."""
    d = cp.ladder_diagnosis(STAGES)
    assert d["would_open_if_unfrozen"] == []
    assert "opens NOTHING" in d["detail"]


def test_a_frozen_coin_that_would_really_open_is_named():
    stages = [dict(s) for s in STAGES]
    for s in stages:
        if s["product_id"] == "AVAX-USD":
            s["backtested_roi_pct"] = 34.0
    d = cp.ladder_diagnosis(stages)
    assert [r["product_id"] for r in d["would_open_if_unfrozen"]] == ["AVAX-USD"]
    assert "AVAX-USD" in d["detail"]


def test_a_ladder_with_no_ordering_problem_says_so():
    d = cp.ladder_diagnosis([
        {"product_id": "BTC-USD", "required_realized_pnl": 0.0, "state": "active"},
        {"product_id": "SOL-USD", "required_realized_pnl": 688.0,
         "state": "waiting_for_realized_profit"}])
    assert d["is_ordering_defect"] is False


def test_the_funding_lever_quotes_the_ladder_rather_than_inventing_a_reason():
    p = full()
    fund = next(l for l in p["levers"] if l["lever"] == "FUND_NEW_BRANCH")
    assert fund["blocked_by"] == "NO_COIN_CLEARS_THE_EDGE_GATE"
    assert "waiting_for_prior_stage" in fund["what_it_would_take"]


# ------------------------------------------------------------- shape

def test_levers_come_back_largest_first():
    sizes = [l["usd_addressable"] for l in full()["levers"]]
    assert sizes == sorted(sizes, reverse=True)


def test_every_lever_either_is_open_or_says_what_stops_it():
    for l in full()["levers"]:
        assert l["blocked_by"] is not None or l["what_it_would_take"]
        assert len(l["what_it_would_take"]) > 40


def test_open_usd_never_counts_a_blocked_lever():
    p = full()
    blocked = sum(l["usd_addressable"] for l in p["levers"] if l["blocked_by"])
    assert p["open_usd"] == pytest.approx(p["total_addressable_usd"] - blocked, abs=0.01)


def test_an_empty_account_produces_no_levers_and_no_crash():
    p = cp.plan(kpis=KPI_GOOD, holdings=[], branches=[], free_cash_usd=0.0,
                account_total_usd=0.0, realized_usd=0.0)
    assert p["total_addressable_usd"] == 0.0
    assert p["open_usd"] == 0.0


def test_every_value_survives_json():
    import json
    assert json.loads(json.dumps(full())) == full()

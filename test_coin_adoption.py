"""Tests for the thing that puts held coin under a grid.

Adoption writes slices into the ledger without a trade, so the failures
that matter are bookkeeping ones: a branch claiming more than the coin
behind it, or believing it owns units the venue has locked. Both look
fine on the dashboard and both are real money.
"""
import ast

import pytest

import coin_adoption as ca

TOTAL = 11397.11
HOLDINGS = [
    {"asset": "ZEC", "units": 1.376835, "price": 1634.29, "usd": 2250.15},
    {"asset": "XRP", "units": 1469.016183, "price": 1.5062, "usd": 2212.63},
    {"asset": "BTC", "units": 0.017455, "price": 84300.48, "usd": 1471.50},
    {"asset": "ETH", "units": 0.46383, "price": 2691.88, "usd": 1248.58},
    {"asset": "SHIB", "units": 180658036.0, "price": 0.00000584, "usd": 1055.04},
    {"asset": "USD", "units": 892.30, "price": 1.0, "usd": 892.30},
    {"asset": "XLM", "units": 2641.021867, "price": 0.212925, "usd": 562.34},
    {"asset": "QNT", "units": 1.013973, "price": 189.18, "usd": 191.82},
    {"asset": "TINY", "units": 4.0, "price": 3.0, "usd": 12.00},
]
CLAIMED = ("BTC-USD", "NEAR-USD", "BONK-USD", "ONDO-USD", "FLOKI-USD", "TIA-USD")


def full(**over):
    kw = dict(account_total_usd=TOTAL, claimed_products=CLAIMED)
    kw.update(over)
    return ca.plan(HOLDINGS, **kw)


# ------------------------------------------- the backing invariant

def test_a_branch_claims_exactly_what_its_coin_is_worth():
    """A branch claiming more than the coin behind it is an unbacked
    branch - the exact hole allocation_backing exists to catch."""
    for a in full()["adopt"]:
        backed = sum(s["qty"] * s["entry_price"] for s in a["slices"])
        assert a["allocated_usd"] == pytest.approx(backed, abs=0.01)


def test_the_slices_sum_to_the_units_adopted():
    """A split that loses a fraction leaves coin the branch thinks it
    does not own, and that coin is never sold again."""
    for a in full()["adopt"]:
        assert sum(s["qty"] for s in a["slices"]) == pytest.approx(a["units"], rel=1e-9)


def test_every_slice_is_entered_at_the_price_it_was_adopted_at():
    """Inventing cost bases nobody paid would put false P&L in the ledger."""
    for a in full()["adopt"]:
        assert {s["entry_price"] for s in a["slices"]} == {a["price"]}


def test_every_slice_clears_the_venue_minimum():
    for a in full()["adopt"]:
        for s in a["slices"]:
            assert s["qty"] * s["entry_price"] >= ca.MIN_TRADE_USD - 1e-9


# --------------------------------------------------- the caps hold

def test_the_test_slice_stays_inside_its_budget():
    p = full()
    assert p["total_usd"] <= ca.MAX_TOTAL_ADOPT_USD + 0.01
    assert p["coins"] <= ca.MAX_COINS


def test_no_single_coin_exceeds_its_own_cap():
    for a in full()["adopt"]:
        assert a["allocated_usd"] <= ca.MAX_PER_COIN_USD + 0.01


def test_a_smaller_budget_adopts_less_not_differently():
    p = ca.plan(HOLDINGS, account_total_usd=TOTAL, claimed_products=CLAIMED,
                max_total_usd=200.0)
    assert p["total_usd"] <= 200.01


def test_most_of_each_holding_is_left_alone():
    """The point of a test slice. XRP is $2,212 and at most $400 moves."""
    for a in full()["adopt"]:
        assert a["leaves_held_usd"] > 0


# ------------------------------------------------------- refusals

def test_it_never_buys_and_never_sells():
    p = full()
    assert p["buys_nothing"] is True and p["sells_nothing"] is True
    assert p["is_a_plan_not_a_change"] is True


def test_cash_is_never_adopted():
    assert "USD" not in {a["asset"] for a in full()["adopt"]}
    assert "IS_CASH" in {r["reason"] for r in full()["refusals"]}


def test_a_coin_a_branch_already_holds_is_refused():
    """Two systems tracking their own qty against one pooled balance is
    the structural gap behind this repo's phantom positions."""
    p = full()
    assert "BTC" not in {a["asset"] for a in p["adopt"]}
    assert any(r["asset"] == "BTC" and r["reason"] == "ALREADY_CLAIMED"
               for r in p["refusals"])


def test_zec_is_excluded_by_name_by_default():
    assert "ZEC" not in {a["asset"] for a in full()["adopt"]}
    assert any(r["asset"] == "ZEC" and r["reason"] == "EXCLUDED_BY_NAME"
               for r in full()["refusals"])


def test_a_position_over_the_twenty_percent_rule_is_left_to_the_trimmer():
    """Putting a grid on an overweight position manages it instead of
    reducing it - the two loops would work against each other."""
    p = ca.plan([{"asset": "BIG", "units": 10.0, "price": 300.0, "usd": 3000.0}],
                account_total_usd=TOTAL, claimed_products=())
    assert p["adopt"] == []
    assert p["refusals"][0]["reason"] == "OVER_THE_POSITION_LIMIT"


def test_coin_too_small_to_carry_a_branch_is_refused():
    p = full()
    assert any(r["asset"] == "TINY" and r["reason"] == "TOO_SMALL_TO_TRADE"
               for r in p["refusals"])


def test_a_coin_already_adopted_is_not_adopted_twice():
    first = full()
    taken = [a["asset"] for a in first["adopt"]]
    again = full(already_adopted=taken)
    assert not set(a["asset"] for a in again["adopt"]) & set(taken)


def test_every_skip_is_written_down_with_a_reason():
    """"Why was XLM skipped" is the question asked after a bad week, and
    it only has an answer if the refusals are recorded."""
    p = full()
    considered = {a["asset"] for a in p["adopt"]} | {r["asset"] for r in p["refusals"]}
    assert considered == {h["asset"] for h in HOLDINGS}
    for r in p["refusals"]:
        assert r["reason"]


# ------------------------------------------ units the venue has locked

def test_only_available_units_are_adopted_never_held_ones():
    """The resting-stop worker puts base on HOLD when it places a stop. A
    branch that believes it owns locked units will try to sell coin it
    cannot move - the trimmer learned this the expensive way."""
    h = [{"asset": "XLM", "units": 2641.0, "available_units": 1000.0,
          "price": 0.212925, "usd": 562.34}]
    p = ca.plan(h, account_total_usd=TOTAL, claimed_products=())
    a = p["adopt"][0]
    #  1,000 available at $0.212925 is $212.93 - the branch may claim that,
    #  never the $562.34 the account holds.
    assert a["units"] <= 1000.0 + 1e-9
    assert a["allocated_usd"] == pytest.approx(212.93, abs=0.02)


def test_a_holding_with_no_available_units_is_refused_not_guessed_at():
    h = [{"asset": "XLM", "units": 2641.0, "available_units": 0.0,
          "price": 0.212925, "usd": 562.34}]
    p = ca.plan(h, account_total_usd=TOTAL, claimed_products=())
    assert p["adopt"] == []
    assert p["refusals"][0]["reason"] == "NO_AVAILABLE_UNITS"


def test_a_holding_almost_entirely_on_hold_is_refused_not_part_adopted():
    """A stop on 99% of a position leaves too little to carry a branch.
    Adopting the remainder would create one that logs "waiting" forever."""
    h = [{"asset": "XLM", "units": 2641.0, "available_units": 40.0,
          "price": 0.212925, "usd": 562.34}]
    p = ca.plan(h, account_total_usd=TOTAL, claimed_products=())
    assert p["adopt"] == []
    assert p["refusals"][0]["reason"] == "TOO_LITTLE_AVAILABLE_TO_TRADE"


def test_a_holding_with_no_price_is_refused():
    h = [{"asset": "XLM", "units": 2641.0, "price": None, "usd": 562.34}]
    p = ca.plan(h, account_total_usd=TOTAL, claimed_products=())
    assert p["refusals"][0]["reason"] == "NO_AVAILABLE_UNITS"


@pytest.mark.parametrize("junk", [None, {}, [], "x"])
def test_junk_in_never_produces_an_adoption(junk):
    p = ca.plan(junk, account_total_usd=TOTAL)
    assert p["adopt"] == [] and p["ok"] is False


# -------------------------------------------- structural guards

def test_the_planner_touches_no_venue_and_no_database():
    tree = ast.parse(open("coin_adoption.py").read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] + [getattr(node, "module", None)]
            for n in names:
                assert not any(b in (n or "") for b in
                               ("aiohttp", "requests", "coinbase", "sqlalchemy",
                                "models", "database", "crypto_grid_bot")), n


def test_the_planner_cannot_create_a_branch_or_a_slice_itself():
    tree = ast.parse(open("coin_adoption.py").read())
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            called.add(f.id if isinstance(f, ast.Name) else getattr(f, "attr", ""))
    for banned in ("create_grid_branch", "CryptoGridSlice", "CryptoGridBranch",
                   "add", "commit", "execute", "grid_buy", "grid_sell"):
        assert banned not in called, f"the planner CALLS {banned}"


def test_the_stable_set_matches_the_census():
    import account_census
    assert ca.STABLE == account_census.STABLE


def test_the_plan_survives_json():
    import json
    assert json.loads(json.dumps(full())) == full()

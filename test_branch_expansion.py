"""Tests for the thing that decides how real cash gets earmarked.

A branch funded below levels x MIN_TRADE_USD does not trade - it logs
"waiting" forever. So the failure this file mostly guards against is not
losing money, it is quietly creating branches that cannot act.
"""
import ast

import pytest

import branch_expansion as be


def cand(coin, net=2.0, verdict="WORTH_A_BRANCH"):
    return {"coin": coin, "net_per_day_pct": net, "verdict": verdict,
            "why": f"{coin} why"}


CANDS = [cand(c, net=10 - i) for i, c in enumerate(
    ["ARB", "INJ", "APT", "OP", "AVAX", "SUI", "XLM", "DOT", "LTC", "ATOM"])]


# ------------------------------------------------------------ sizing

def test_levels_shrink_with_the_allocation():
    assert be.levels_for(15) == 3
    assert be.levels_for(10) == 2
    assert be.levels_for(5) == 1
    assert be.levels_for(4) == 1          # floored, never zero


@pytest.mark.parametrize("bad", [None, 0, -5, "x", float("nan")])
def test_levels_of_nothing_is_zero(bad):
    assert be.levels_for(bad) == 0


def test_every_rung_clears_the_venue_minimum():
    p = be.plan(481.52, CANDS)
    assert p["ok"]
    for o in p["open"]:
        assert o["slice_usd"] >= be.MIN_TRADE_USD, o


def test_a_branch_is_never_opened_below_what_can_trade():
    p = be.plan(481.52, CANDS)
    for o in p["open"]:
        assert o["allocated_usd"] >= be.MIN_BRANCH_USD


# ----------------------------------------------------------- reserve

def test_the_reserve_is_never_spent():
    p = be.plan(481.52, CANDS)
    assert p["total_usd"] <= 481.52 - be.RESERVE_USD + 0.01


def test_cash_that_is_all_reserve_opens_nothing():
    p = be.plan(100.0, CANDS, reserve_usd=100.0)
    assert p["ok"] is False and p["reason"] == "RESERVE_LEAVES_NOTHING"


def test_the_reserve_reason_is_stated():
    p = be.plan(481.52, CANDS)
    assert "cannot fill" in p["reserve_note"]


# -------------------------------------------------------- refusals

@pytest.mark.parametrize("bad", [None, 0, -50, "x", float("inf")])
def test_no_cash_opens_nothing(bad):
    p = be.plan(bad, CANDS)
    assert p["ok"] is False and p["open"] == []


def test_a_coin_already_running_is_skipped():
    p = be.plan(481.52, CANDS, existing=["ARB"])
    assert "ARB" not in [o["coin"] for o in p["open"]]
    assert {"coin": "ARB", "reason": "ALREADY_A_BRANCH"} in p["skipped"]


def test_a_coin_the_ranking_rejected_is_never_opened():
    c = CANDS + [cand("MATIC", net=-3.0, verdict="TRENDING")]
    p = be.plan(481.52, c)
    assert "MATIC" not in [o["coin"] for o in p["open"]]
    assert any(s["coin"] == "MATIC" and s["reason"] == "TRENDING" for s in p["skipped"])


def test_too_little_for_even_one_branch_refuses():
    p = be.plan(110.0, CANDS, reserve_usd=100.0)
    assert p["ok"] is False and p["reason"] in ("RESERVE_LEAVES_NOTHING", "CANNOT_AFFORD_ONE")


def test_no_eligible_coins_refuses():
    p = be.plan(481.52, [cand("X", verdict="TOO_STILL")])
    assert p["ok"] is False and p["reason"] == "NO_ELIGIBLE_COINS"


def test_coins_that_did_not_fit_are_named_not_dropped():
    p = be.plan(200.0, CANDS)
    left = [s for s in p["skipped"] if s["reason"] == "NO_CAPITAL_LEFT"]
    assert left, "coins that did not fit must be reported"
    assert len(p["open"]) + len(left) == len(CANDS)


# ------------------------------------------------------- the split

def test_capital_is_split_evenly_not_by_rank():
    p = be.plan(481.52, CANDS)
    sizes = {o["allocated_usd"] for o in p["open"]}
    assert len(sizes) == 1, "an even split must give every branch the same size"


def test_the_even_split_is_justified_by_the_measurement():
    p = be.plan(481.52, CANDS)
    assert "order inside it does not" in p["even_split_note"]


def test_best_ranked_coins_are_the_ones_chosen():
    p = be.plan(120.0, CANDS, reserve_usd=0.0, max_branch_usd=1000)
    opened = [o["coin"] for o in p["open"]]
    assert opened[0] == "ARB"


def test_totals_reconcile():
    p = be.plan(481.52, CANDS)
    assert p["total_usd"] == pytest.approx(
        sum(o["allocated_usd"] for o in p["open"]), abs=0.01)
    assert p["left_unallocated_usd"] == pytest.approx(
        p["free_cash_usd"] - p["total_usd"], abs=0.01)


def test_it_never_allocates_more_than_it_has():
    for cash in (16, 50, 120, 300, 481.52, 5000):
        p = be.plan(cash, CANDS)
        if p["ok"]:
            assert p["total_usd"] <= cash, cash


def test_a_big_balance_opens_more_branches_not_bigger_ones():
    p = be.plan(5000.0, CANDS)
    assert p["per_branch_usd"] <= be.MAX_BRANCH_USD + 0.01


def test_max_new_is_respected():
    p = be.plan(5000.0, CANDS, max_new=3)
    assert len(p["open"]) <= 3


def test_the_plan_changes_nothing():
    assert be.plan(481.52, CANDS)["is_a_plan_not_a_change"] is True


# ------------------------------------------- structural guards

def test_planner_touches_no_venue_and_no_database():
    tree = ast.parse(open("branch_expansion.py").read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] + [getattr(node, "module", None)]
            for n in names:
                assert not any(b in (n or "") for b in
                               ("aiohttp", "requests", "coinbase", "sqlalchemy",
                                "models", "database")), n


def test_it_cannot_create_a_branch_itself():
    """Checked on the parsed tree, not the file text.

    The first version searched the raw source and failed on the module's
    own docstring, which NAMES create_grid_branch to explain what this
    planner deliberately does not do. A test that a comment can break is
    testing the prose, not the code.
    """
    tree = ast.parse(open("branch_expansion.py").read())
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                called.add(f.id)
            elif isinstance(f, ast.Attribute):
                called.add(f.attr)
    for banned in ("create_grid_branch", "add", "commit", "execute"):
        assert banned not in called, f"the planner CALLS {banned}"

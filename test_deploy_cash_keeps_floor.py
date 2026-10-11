"""Deploy cash must keep the owner's cash floor and skip coins it cannot buy.

THE BUG, measured 2026-10-11 ~22:55Z. The plan held back $100 (RESERVE_USD)
and nothing else. The buy path holds back GRID_CASH_RESERVE_USD ($1,820
live). With $3,181.28 unclaimed and $2,987.48 of real free cash, it would
have handed out $2,801.20, $280.12 each to ten branches. That is $1,633.72
more budget than exists above the floor. One of the ten was SOL-USD, whose
coin is staked and which the backing gate refuses to buy at all.
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import idle_cash as ic

FLOOR = 1820.0


def branch(pid, alloc, open_slices=1, levels=3, bot=None):
    return {"bot_name": bot or pid, "product_id": pid, "allocated_usd": alloc,
            "open_slices": open_slices, "num_levels": levels, "active": True}


def trades(*pids):
    return [{"product_id": p, "pnl": 0.5} for p in pids]


def status(unclaimed, free, unbacked=(), branches=()):
    return {"allocation_backing": {"unbacked_usd": -unclaimed},
            "real_free_cash_usd": free,
            "backing_owned": {"unbacked": [{"product_id": p} for p in unbacked]},
            "branches": list(branches)}


# --------------------------------------------------------- the inputs
def test_the_live_evening_keeps_the_floor():
    pot, reserve, unbacked = ic.deploy_inputs(
        status(3181.28, 2987.48, unbacked=("ETH-USD", "SOL-USD")), FLOOR)
    assert pot == 2987.48, "the pot is capped by real free cash"
    assert reserve == FLOOR, "the owner's floor, not the $100 working float"
    assert unbacked == {"ETH-USD", "SOL-USD"}
    assert round(pot - reserve, 2) == 1167.48


def test_the_working_float_still_applies_when_the_floor_is_lower():
    _, reserve, _ = ic.deploy_inputs(status(500, 500), 0.0)
    assert reserve == ic.RESERVE_USD


def test_unreadable_free_cash_deploys_nothing():
    pot, _, _ = ic.deploy_inputs(status(3000, None), FLOOR)
    assert pot == 0.0


def test_the_pot_is_never_more_than_unclaimed():
    pot, _, _ = ic.deploy_inputs(status(400, 2900), FLOOR)
    assert pot == 400


# ---------------------------------------------------------- the plan
def test_an_unbacked_branch_is_refused():
    rows = [branch("SOL-USD", 130.13), branch("LINK-USD", 103.76)]
    p = ic.plan(rows, trades("SOL-USD", "LINK-USD"), unclaimed_usd=2000,
                reserve_usd=FLOOR, unbacked={"SOL-USD"}, max_share_pct=100)
    assert [a["product_id"] for a in p["adds"]] == ["LINK-USD"]
    assert {"product_id": "SOL-USD", "reason": "UNBACKED_CANNOT_BUY"} in p["refusals"]


def test_nothing_is_planned_past_the_floor():
    rows = [branch(f"C{i}-USD", 100.0) for i in range(9)]
    p = ic.plan(rows, trades(*[f"C{i}-USD" for i in range(9)]),
                unclaimed_usd=2987.48, reserve_usd=FLOOR, max_share_pct=100)
    assert p["total_usd"] <= round(2987.48 - FLOOR, 2) + 0.01


def test_without_unbacked_the_plan_is_unchanged():
    """The new argument is refuse-only: omitting it changes nothing."""
    rows = [branch("A-USD", 100.0), branch("B-USD", 100.0)]
    a = ic.plan(rows, trades("A-USD", "B-USD"), unclaimed_usd=600, reserve_usd=100)
    b = ic.plan(rows, trades("A-USD", "B-USD"), unclaimed_usd=600, reserve_usd=100,
                unbacked=set())
    assert a == b


# ------------------------------------------------------ the endpoint
def _endpoint_src():
    src = open(os.path.join(HERE, "routers", "trading_dashboard.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
              and n.name == "idle_capital_deploy_cash")
    return "\n".join(src.splitlines()[fn.lineno - 1:fn.end_lineno])


def test_the_button_uses_the_floor_and_the_backing():
    body = _endpoint_src()
    assert "idle_cash.deploy_inputs(" in body
    assert "GRID_CASH_RESERVE_USD" in body
    assert "reserve_usd=reserve" in body and "unbacked=unbacked" in body
    assert "unclaimed_usd=unclaimed)" not in body, "the old $100-only call is gone"


def test_the_button_still_previews_by_default():
    assert "dry_run: bool = True" in _endpoint_src()

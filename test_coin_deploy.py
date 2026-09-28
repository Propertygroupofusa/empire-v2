"""Fund fewer coins properly, rather than all of them uselessly.

The owner asked for freed cash to go into PRIME, TON and APE. The figure
quoted ($193.26) had fallen to $137.79 by the time branches could be
created, because the branches whose claims the reconciliation had just
lowered spent the released cash on dips. So the plan is computed from the
cash that exists at write time, never from a number carried over.

The one opinion this module holds: a branch too small to place a slice that
clears the venue minimum is worse than no branch. It spends real money on
something that will never trade, which is the stranded capital this fleet
spent a whole day digging out of.
"""
import pytest

import coin_deploy as D

WISH = ["PRIME-USD", "TON-USD", "APE-USD"]


def test_the_live_plan_funds_all_three():
    rows, rep = D.plan(WISH, free_cash_usd=137.79, reserve_usd=88.0,
                       fleet_total_usd=7815.23)
    assert rep["status"] == "FUND"
    assert [r["product_id"] for r in rows] == WISH
    assert all(r["usd"] == pytest.approx(16.59, abs=0.01) for r in rows)


def test_the_split_never_exceeds_what_is_deployable():
    """Three ROUNDED shares of $49.79 come to $49.80 and the last branch
    finds a cent that is not there."""
    rows, rep = D.plan(WISH, 137.79, 88.0)
    assert sum(r["usd"] for r in rows) <= rep["deployable_usd"] + 1e-9


def test_it_funds_fewer_rather_than_all_of_them_too_small():
    rows, rep = D.plan(WISH, free_cash_usd=118.0, reserve_usd=88.0)
    assert len(rows) == 2, rep["detail"]
    assert rows[0]["product_id"] == "PRIME-USD"
    assert rep["deferred"] == ["APE-USD"]
    assert "deferred" in rep["detail"]


def test_the_ranking_decides_who_is_funded_first():
    rows, _ = D.plan(WISH, free_cash_usd=105.0, reserve_usd=88.0)
    assert [r["product_id"] for r in rows] == ["PRIME-USD"]


def test_every_funded_branch_can_actually_place_a_slice():
    for cash in (103.0, 118.0, 137.79, 500.0):
        rows, _ = D.plan(WISH, cash, 88.0)
        for r in rows:
            assert r["usd"] >= D.MIN_VIABLE_BRANCH_USD - 1e-9, (cash, r)
            assert r["usd"] / D.MIN_LEVELS >= D.MIN_TRADE_USD - 1e-9, (cash, r)


def test_too_little_for_even_one_branch_funds_nothing():
    rows, rep = D.plan(WISH, free_cash_usd=95.0, reserve_usd=88.0)
    assert rows == []
    assert rep["status"] == "HOLD"
    assert "cannot trade" in rep["detail"]


def test_unreadable_cash_funds_nothing():
    rows, rep = D.plan(WISH, None, 88.0)
    assert rows == [] and rep["status"] == "UNKNOWN"
    assert "gap is not a zero" in rep["detail"]


def test_a_coin_already_held_is_not_funded_twice():
    rows, rep = D.plan(WISH, 137.79, 88.0, held_product_ids=["TON-USD"])
    assert [r["product_id"] for r in rows] == ["PRIME-USD", "APE-USD"]
    assert rep["already_held"] == ["TON-USD"]


def test_it_is_idempotent_once_everything_is_held():
    rows, rep = D.plan(WISH, 500.0, 88.0, held_product_ids=WISH)
    assert rows == []
    assert rep["status"] == "OK"


def test_the_twenty_percent_rule_still_binds():
    rows, _ = D.plan(["PRIME-USD"], free_cash_usd=5000.0, reserve_usd=88.0,
                     fleet_total_usd=1000.0)
    assert rows[0]["usd"] <= 1000.0 * D.MAX_SINGLE_COIN_SHARE + 1e-9


def test_the_refusals_carry_their_arithmetic():
    _, rep = D.plan(WISH, 95.0, 88.0)
    for piece in ("95.00", "88.00", "7.00"):
        assert piece in rep["detail"], rep["detail"]


# ------------------------------------------------------------ mutation
def test_removing_the_viability_floor_would_fund_untradeable_branches():
    rows, _ = D.plan(WISH, free_cash_usd=105.0, reserve_usd=88.0,
                     min_viable=0.0)
    assert len(rows) == 3
    assert rows[0]["usd"] < D.MIN_VIABLE_BRANCH_USD, (
        "the floor is not what prevents a three-way split here")


# ================== the deployment reserve: first claim, not a monopoly
"""The owner authorised giving the deployer first claim on freed cash so new
coins are not outrun by a loop that checks every 30 seconds while the
deployer checks every 900 - and said in the same breath: make sure the money
keeps flipping.

Those pull against each other. A pure first claim starves every dip buy in
the fleet until three branches exist, and a fleet that stops buying dips
stops selling rises a few hours later. So it is a RESERVATION capped at half
the deployable cash, and these tests are about that cap holding.
"""


def test_the_fleet_always_keeps_at_least_half_to_trade_with():
    """The 'keep flipping' half of the instruction, as arithmetic."""
    for dep in (24.0, 49.79, 100.0, 504.18, 5000.0):
        r, _ = D.deployment_reserve_usd(3, 16.60, dep)
        assert r <= dep * D.MAX_DEPLOYMENT_RESERVE_SHARE + 1e-9, (dep, r)
        assert dep - r >= dep * 0.5 - 0.01, (dep, r)


def test_it_reserves_only_what_the_unfunded_coins_need():
    """With plenty of cash it takes the $49.80 for three branches and leaves
    the other $454 alone - not half of everything."""
    r, why = D.deployment_reserve_usd(3, 16.60, 504.18)
    assert r == pytest.approx(49.80, abs=0.01)
    assert "the full" in why


def test_it_caps_rather_than_taking_everything_when_cash_is_tight():
    r, why = D.deployment_reserve_usd(3, 16.60, 49.79)
    assert r == pytest.approx(24.89, abs=0.01)
    assert "keep buying their dips" in why


def test_it_extinguishes_itself_once_every_coin_is_funded():
    """What makes it safe to leave switched on: a temporary claim on a
    temporary shortage, not a standing tax on the fleet."""
    r, why = D.deployment_reserve_usd(0, 16.60, 504.18)
    assert r == 0.0
    assert "already has a branch" in why


def test_it_shrinks_as_coins_get_funded():
    three, _ = D.deployment_reserve_usd(3, 16.60, 504.18)
    two, _ = D.deployment_reserve_usd(2, 16.60, 504.18)
    one, _ = D.deployment_reserve_usd(1, 16.60, 504.18)
    assert three > two > one > 0


def test_unreadable_cash_reserves_nothing():
    r, why = D.deployment_reserve_usd(3, 16.60, None)
    assert r == 0.0 and "gap is not a zero" in why


def test_no_deployable_cash_reserves_nothing():
    assert D.deployment_reserve_usd(3, 16.60, 0.0)[0] == 0.0
    assert D.deployment_reserve_usd(3, 16.60, -50.0)[0] == 0.0


def test_the_reserve_is_floored_to_the_cent():
    r, _ = D.deployment_reserve_usd(3, 16.666, 504.18)
    assert r == round(r, 2)
    assert r <= 3 * 16.666


def test_removing_the_cap_would_starve_the_fleet():
    """Mutation: at max_share 1.0 and tight cash the deployer takes the lot
    and no branch can buy a dip."""
    dep = 49.79
    capped, _ = D.deployment_reserve_usd(3, 16.60, dep)
    uncapped, _ = D.deployment_reserve_usd(3, 16.60, dep, max_share=1.0)
    assert uncapped > capped
    assert dep - uncapped < 1.0, "the cap is not what leaves cash for dip buys"

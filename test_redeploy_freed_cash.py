"""The ZEC proceeds go to NEAR and JASMY, once, and nowhere else.

The owner closed ZEC-USD - 31% of the fleet, zero completed round trips in
26 days - and chose NEAR-USD and JASMY-USD for the proceeds, the two
branches with the best measured recycling on this account's own record.

Everything here is about what must NOT happen: spending money before the
sale lands, spending it twice, or rebuilding the concentration the sale
undid.
"""
import pytest

import redeploy_freed_cash as rd


def branch(pid, alloc, slices=0):
    return {"product_id": pid, "bot_name": "bot_" + pid.split("-")[0].lower(),
            "allocated_usd": alloc,
            "slices": [{"qty": 1, "entry_price": 1} for _ in range(slices)]}


FLEET_AFTER_SALE = [
    branch("NEAR-USD", 177.12), branch("JASMY-USD", 53.56),
    branch("ETH-USD", 400.00, 4), branch("XRP-USD", 2240.54, 10),
]
# The real fleet cost basis after ZEC-USD is closed - $5,169.06 across 20
# coins. A toy fixture made every share look like a ceiling breach and hid
# what the plan actually does on this account.
BASIS = {
    "NEAR-USD": 130.54, "JASMY-USD": 0.0, "ETH-USD": 438.94, "XRP-USD": 2270.78,
    "XLM-USD": 477.93, "SHIB-USD": 303.39, "BCH-USD": 231.89, "PEPE-USD": 205.14,
    "LINK-USD": 200.82, "SOL-USD": 152.66, "ALGO-USD": 160.72, "ACH-USD": 110.71,
    "QNT-USD": 108.84, "HBAR-USD": 108.92, "LTC-USD": 75.90, "TIA-USD": 42.69,
    "ONDO-USD": 23.34, "BTC-USD": 16.92, "FLOKI-USD": 28.80, "TON-USD": 80.13,
}
PROCEEDS = 2193.42 + 88.0   # the sale plus the reserve already in the wallet


# ── it must not run early ────────────────────────────────────────────────

def test_nothing_moves_while_zec_still_holds_slices():
    fleet = FLEET_AFTER_SALE + [branch("ZEC-USD", 2272.62, 7)]
    rows, rep = rd.plan(fleet, PROCEEDS, BASIS)
    assert rows == []
    assert rep["status"] == "WAITING"
    assert "has not been closed yet" in rep["detail"]


def test_an_emptied_source_branch_counts_as_closed():
    """close-branch may leave the row behind with no slices."""
    fleet = FLEET_AFTER_SALE + [branch("ZEC-USD", 0.0, 0)]
    rows, rep = rd.plan(fleet, PROCEEDS, BASIS)
    assert rep["status"] == "READY"
    assert len(rows) == 2


def test_a_vanished_source_branch_also_counts_as_closed():
    rows, rep = rd.plan(FLEET_AFTER_SALE, PROCEEDS, BASIS)
    assert rep["status"] == "READY"


# ── it must not run twice ────────────────────────────────────────────────

def test_it_is_one_shot():
    rows, rep = rd.plan(FLEET_AFTER_SALE, PROCEEDS, BASIS, already_done=True)
    assert rows == []
    assert rep["status"] == "DONE"


# ── where the money goes ─────────────────────────────────────────────────

def test_the_proceeds_split_between_near_and_jasmy():
    rows, rep = rd.plan(FLEET_AFTER_SALE, PROCEEDS, BASIS)
    assert [r["product_id"] for r in rows] == ["NEAR-USD", "JASMY-USD"]
    assert rep["share_usd"] == pytest.approx(1096.71, abs=0.01)
    assert rows[0]["allocated_after"] == pytest.approx(177.12 + 1096.71, abs=0.01)


def test_the_share_is_floored_so_the_last_branch_finds_its_money():
    """Three rounded shares of $49.79 come to $49.80 and the last branch
    finds a cent that is not there."""
    rows, rep = rd.plan(FLEET_AFTER_SALE, 88.0 + 99.99, BASIS)
    assert rep["total_usd"] <= rep["deployable_usd"]


def test_it_never_spends_the_reserve():
    """$40 above the reserve funds two $20 shares - what must hold is that
    the reserve itself is untouchable, not that small sums are refused."""
    rows, rep = rd.plan(FLEET_AFTER_SALE, rd.RESERVE_USD + 40.0, BASIS)
    assert rep["deployable_usd"] == 40.0
    assert rep["total_usd"] <= 40.0
    assert sum(r["add_usd"] for r in rows) <= 40.0


def test_cash_at_or_below_the_reserve_moves_nothing():
    for cash in (rd.RESERVE_USD, rd.RESERVE_USD - 1, 0.0):
        rows, rep = rd.plan(FLEET_AFTER_SALE, cash, BASIS)
        assert rows == [], cash
        assert rep["status"] == "HOLD", cash


def test_too_little_for_two_funds_one_properly_rather_than_two_uselessly():
    rows, rep = rd.plan(FLEET_AFTER_SALE, 88.0 + 20.0, BASIS)
    assert len(rows) == 1
    assert rows[0]["product_id"] == "NEAR-USD"
    assert rep["deferred"] == ["JASMY-USD"]


# ── it must not rebuild the concentration ────────────────────────────────

def test_a_target_already_over_the_ceiling_gets_nothing():
    """The whole point of closing ZEC was concentration. Putting the money
    straight back into a coin already over is the same mistake."""
    heavy = dict(BASIS, **{"NEAR-USD": 5000.0})
    rows, rep = rd.plan(FLEET_AFTER_SALE, PROCEEDS, heavy)
    funded = {r["product_id"] for r in rows}
    assert "NEAR-USD" not in funded
    assert "JASMY-USD" in funded
    assert rep["refusals"][0]["product_id"] == "NEAR-USD"


def test_a_share_beyond_the_ceiling_is_capped_not_refused():
    """The first build refused an even split outright and would have left
    the whole $2,193 as cash - the ceiling working and the plan wasting
    the result. Size to what passes instead."""
    tight = dict(BASIS, **{"NEAR-USD": 900.0})
    rows, rep = rd.plan(FLEET_AFTER_SALE, PROCEEDS, tight)
    near = next(r for r in rows if r["product_id"] == "NEAR-USD")
    assert near["add_usd"] < rep["share_usd"]
    assert near["add_usd"] <= near["ceiling_headroom_usd"] + 0.01
    assert rep["capped_by_ceiling"][0]["product_id"] == "NEAR-USD"
    assert "capped at" in rep["detail"]


def test_capping_leaves_the_remainder_as_cash_and_says_so():
    tight = dict(BASIS, **{"NEAR-USD": 900.0})
    _, rep = rd.plan(FLEET_AFTER_SALE, PROCEEDS, tight)
    assert rep["left_as_cash_usd"] > 0
    assert "stays as cash" in rep["detail"]


def test_a_capped_add_really_does_pass_the_gate():
    """The cap has to be arithmetic the gate agrees with, not a guess."""
    import concentration_gate as cg
    tight = dict(BASIS, **{"NEAR-USD": 900.0})
    rows, _ = rd.plan(FLEET_AFTER_SALE, PROCEEDS, tight)
    for r in rows:
        ok, _why = cg.concentration_verdict(r["product_id"], tight, r["add_usd"])
        assert ok, r


def test_every_target_refused_means_the_money_stays_as_cash():
    heavy = dict(BASIS, **{"NEAR-USD": 9000.0, "JASMY-USD": 9000.0})
    rows, rep = rd.plan(FLEET_AFTER_SALE, PROCEEDS, heavy)
    assert rows == []
    assert rep["status"] == "REFUSED"
    assert "rather than rebuilding the concentration" in rep["detail"]


# ── it must not invent branches or numbers ───────────────────────────────

def test_a_missing_target_branch_is_skipped_not_created():
    fleet = [branch("NEAR-USD", 177.12), branch("ETH-USD", 400.00, 4)]
    rows, rep = rd.plan(fleet, PROCEEDS, BASIS)
    assert [r["product_id"] for r in rows] == ["NEAR-USD"]
    assert rep["missing"] == ["JASMY-USD"]


def test_no_target_branch_at_all_refuses_rather_than_guessing():
    rows, rep = rd.plan([branch("ETH-USD", 400.00, 4)], PROCEEDS, BASIS)
    assert rows == []
    assert rep["status"] == "REFUSED"


def test_unreadable_cash_is_unknown_never_zero():
    rows, rep = rd.plan(FLEET_AFTER_SALE, None, BASIS)
    assert rows == []
    assert rep["status"] == "UNKNOWN"
    assert "a gap is not a zero" in rep["detail"]


def test_the_plan_places_no_orders_and_says_so():
    _, rep = rd.plan(FLEET_AFTER_SALE, PROCEEDS, BASIS)
    assert "No order is placed" in rep["detail"]


def test_it_reuses_the_one_concentration_ceiling():
    """Not a second copy of 20% living in this module."""
    import ast
    import pathlib
    src = pathlib.Path(rd.__file__).read_text()
    assigned = [t.id for n in ast.parse(src).body if isinstance(n, ast.Assign)
                for t in n.targets if isinstance(t, ast.Name)]
    assert "MAX_SINGLE_COIN_SHARE" not in assigned
    assert "concentration_gate" in src

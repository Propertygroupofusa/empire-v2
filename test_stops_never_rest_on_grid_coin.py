"""A resting stop sold coin the grid still had on its books.

The trimmer was the first seller to do this - $885.43 of ZEC and $244.78
of XRP, fixed in plan_trims by ACTIVELY_TRADED. The resting-stop placer
is the second, and it was never given the same rule.

Live at 2026-09-28T09:17Z, RESTING_STOPS mode=arm, worker last pass
"0 placed, 1 replaced", would_place 6: XLM, NEAR, LINK, ALGO, SOL, ACH.
Five of those six are live grid branches holding open slices. The only
exclusion in force was ZEC, and that was TRIMMERS_COIN - the file knew
about the trimmer and not about the grid.

The endpoint states the cost itself:

    "Resting sells hold the coins. 75% of each position becomes
     unavailable to the trimmer and to you until the order is cancelled."

So each placement locks 75% of a branch's inventory, and if it fires it
sells 75% of a position the grid tracks slice by slice - leaving the
branch claiming units the wallet no longer holds. That is how ETH came
to be short 0.0316 units. coin_tracked_is_held read $510.72 across 7
branches at 09:22Z, up from $473.28 across 5.

It also cannot be reconciled with the standing rule that nothing red is
realised: a stop on an underwater grid position is a scheduled loss.

The exception is ONE-DIRECTIONAL - it can only ever cancel a placement,
never cause one - and it FAILS OPEN: an unreadable grid leaves behaviour
exactly as it was, because a protection that vanishes on a read error is
worse than one that never existed.
"""
import resting_stops as rs

OK = dict(units_available=100.0, price=10.0, stop_price=9.0,
          base_increment="0.00000001", quote_increment="0.01")


def test_the_control_a_stop_is_placed_when_the_grid_does_not_hold_it():
    p = rs.plan_stop("LINK", **OK)
    assert p["ok"] is True, p


def test_no_stop_rests_on_units_a_grid_branch_holds_slices_on():
    p = rs.plan_stop("LINK", actively_traded={"LINK"}, **OK)
    assert p["ok"] is False
    assert p["reason"] == "ACTIVELY_TRADED"
    assert "grid" in p["detail"].lower()


def test_the_refusal_is_checked_before_any_reason_that_could_let_it_through():
    """A grid coin whose stop is also viable must still be refused for the
    grid, not accepted on the viable path."""
    p = rs.plan_stop("SOL", actively_traded={"SOL"}, **OK)
    assert p["reason"] == "ACTIVELY_TRADED"


def test_the_trimmers_coin_still_wins_where_it_already_applied():
    """One-directional means both refusals stand; neither may unblock."""
    p = rs.plan_stop("ZEC", actively_traded=(), excluded={"ZEC"}, **OK)
    assert p["ok"] is False and p["reason"] == "TRIMMERS_COIN"


def test_an_unreadable_grid_fails_open_and_places_as_before():
    for empty in (None, (), set(), frozenset()):
        p = rs.plan_stop("LINK", actively_traded=empty, **OK)
        assert p["ok"] is True, f"{empty!r} made the placer refuse - it must fail open"


def test_matching_is_by_ticker_not_by_case_or_product_id():
    for held in ({"link"}, {"LINK"}, {"Link"}):
        p = rs.plan_stop("LINK", actively_traded=held, **OK)
        assert p["reason"] == "ACTIVELY_TRADED", f"{held!r} did not match"


def test_a_coin_the_grid_does_not_hold_is_untouched_by_this():
    p = rs.plan_stop("XLM", actively_traded={"LINK", "SOL", "ACH"}, **OK)
    assert p["ok"] is True


def test_a_stop_above_the_market_is_still_refused_for_that_reason():
    """The most expensive mistake in the file keeps its own verdict."""
    p = rs.plan_stop("LINK", actively_traded=(), units_available=100.0,
                     price=9.0, stop_price=10.0,
                     base_increment="0.00000001", quote_increment="0.01")
    assert p["reason"] == "STOP_ABOVE_MARKET"


def test_both_sellers_now_read_the_same_rule_name():
    """The trimmer and the stop placer refuse under one name, so a reader
    grepping either answer finds both."""
    import auto_trim
    assert "ACTIVELY_TRADED" in open("auto_trim.py").read()
    assert "ACTIVELY_TRADED" in open("resting_stops.py").read()

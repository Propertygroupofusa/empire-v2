"""A branch cannot be idle for longer than it has existed.

Live, 2026-09-28. The account owner asked for PRIME, TON and APE. They were
deployed at 03:35. Seven minutes later:

    03:42:42  REALLOCATE  PRIME-USD
              Moved $240.38 of its own idle real cash into
              grid branch crypto_grid_10 (XLM-USD)

PRIME landed in slot crypto_grid_3 - bot_name is a recycled slot number -
and inherited the closed trades of whatever coin held that slot before.
idle_capital read those, called PRIME "flat, no completed round trip in 26
days", and emptied it. TON and APE were queued behind it, both reporting
632.7 idle hours at fourteen minutes old.

The destination was no better: XLM-USD has never closed a round trip in the
fleet's entire history, while the worker's own docstring promises stale
money moves "only into a branch that has demonstrably closed a round trip".
"""
from datetime import datetime, timedelta, timezone

import idle_capital as ic

NOW = datetime(2026, 9, 28, 3, 42, 42, tzinfo=timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _branch(bot, product, usd, *, slices=0, created=None):
    return {"bot_name": bot, "product_id": product, "allocated_usd": usd,
            "open_slices": slices, "created_at": _iso(created) if created else None}


def _trade(bot, product, closed):
    return {"bot_name": bot, "product_id": product, "closed_at": _iso(closed)}


# --- the source side -----------------------------------------------------

def test_a_new_coin_does_not_inherit_the_slots_previous_coin():
    """The exact live case: PRIME in a slot DOGE used to hold."""
    prime = _branch("crypto_grid_3", "PRIME-USD", 240.38,
                    created=NOW - timedelta(minutes=7))
    old = _trade("crypto_grid_3", "DOGE-USD", NOW - timedelta(days=26))
    out = ic.report([prime], [old], now=NOW)
    row = out["branches"][0]
    assert row["state"] != ic.STALE, row["why"]
    assert row["state"] == ic.TOO_NEW
    assert out["stale_usd"] == 0.0


def test_the_same_coin_in_the_same_slot_still_counts():
    """The fix must not blind the classifier to a branch's own record."""
    doge = _branch("crypto_grid_3", "DOGE-USD", 240.38,
                   created=NOW - timedelta(days=30))
    own = _trade("crypto_grid_3", "DOGE-USD", NOW - timedelta(days=26))
    row = ic.report([doge], [own], now=NOW)["branches"][0]
    assert row["state"] == ic.STALE
    assert row["idle_hours"] > 72


def test_idle_hours_can_never_exceed_the_branchs_own_age():
    """The blunt backstop, whichever route computed the hours."""
    young = _branch("crypto_grid_3", "PRIME-USD", 240.38,
                    created=NOW - timedelta(hours=10))
    inherited = _trade("crypto_grid_3", "DOGE-USD", NOW - timedelta(days=26))
    row = ic.report([young], [inherited], now=NOW)["branches"][0]
    assert row["idle_hours"] <= 10.1, row
    assert row["state"] != ic.STALE


def test_a_recreated_branch_for_the_SAME_coin_is_still_judged_on_its_own_age():
    """The coin-aware key cannot help here - bot_name AND product_id both
    match. Branches are deleted and recreated in place by
    move_cash_between_grid_branches, so a rebuilt branch would inherit its
    own predecessor's record and read as 26 days idle at ten hours old.
    This is what the age clamp is for, and it is the only thing standing
    between a rebuilt branch and being emptied again.
    """
    rebuilt = _branch("crypto_grid_3", "DOGE-USD", 240.38,
                      created=NOW - timedelta(hours=10))
    own_old_life = _trade("crypto_grid_3", "DOGE-USD", NOW - timedelta(days=26))
    row = ic.report([rebuilt], [own_old_life], now=NOW)["branches"][0]
    assert row["state"] != ic.STALE, row["why"]
    assert row["idle_hours"] <= 10.1, row


def test_a_truncated_window_floor_is_clamped_to_the_branch_age():
    """TON and APE read 632.7 idle hours at fourteen minutes old - the
    window's reach is a floor for the FLEET, not for a branch that did not
    exist for it."""
    ton = _branch("crypto_grid_22", "TON-USD", 240.38,
                  created=NOW - timedelta(minutes=14))
    far = _trade("crypto_grid_1", "BTC-USD", NOW - timedelta(days=26))
    row = ic.report([ton], [far], now=NOW, total_trade_count=9999)["branches"][0]
    assert row["state"] != ic.STALE
    assert (row["idle_hours"] or 0) < 1.0, row


def test_a_branch_with_no_creation_time_is_never_stale_on_an_inherited_trade():
    """Unknown age must not resolve to 'old enough to empty'."""
    unknown = _branch("crypto_grid_3", "PRIME-USD", 240.38, created=None)
    inherited = _trade("crypto_grid_3", "DOGE-USD", NOW - timedelta(days=26))
    row = ic.report([unknown], [inherited], now=NOW)["branches"][0]
    assert row["state"] != ic.STALE, row["why"]


# --- the destination side ------------------------------------------------

def _fleet():
    """Sized so the SHARE RULE is not what decides anything here.

    XLM is small enough that taking the $240.38 leaves it at ~6.6% of the
    fleet, well under the 20% destination cap - so if it is chosen, it was
    chosen on the destination rule, not allowed through by the cap. Under
    the old rule it wins outright: every WORKING branch reports idle_hours
    0.0 and therefore sorts ahead of JASMY, which actually closed a trip.
    """
    stale = _branch("crypto_grid_3", "OLD-USD", 240.38, created=NOW - timedelta(days=30))
    xlm = _branch("crypto_grid_10", "XLM-USD", 100.00, slices=4,
                  created=NOW - timedelta(days=20))
    jasmy = _branch("crypto_grid_20", "JASMY-USD", 53.56, created=NOW - timedelta(days=20))
    filler = _branch("crypto_grid_2", "ZEC-USD", 5000.00, slices=7,
                     created=NOW - timedelta(days=20))
    trades = [
        _trade("crypto_grid_3", "OLD-USD", NOW - timedelta(days=26)),
        _trade("crypto_grid_20", "JASMY-USD", NOW - timedelta(hours=3.9)),
        # XLM has never closed a trip - it appears in no trade row at all.
    ]
    return ic.report([stale, xlm, jasmy, filler], trades, now=NOW)


def test_the_fixture_would_really_let_xlm_through_the_share_rule():
    """Guards the guard: if the cap silently blocked XLM, the two tests
    below would pass no matter what the destination rule did."""
    out = _fleet()
    total = sum(r["allocated_usd"] for r in out["branches"])
    xlm = next(r for r in out["branches"] if r["product_id"] == "XLM-USD")
    assert xlm["state"] == ic.WORKING
    assert xlm["idle_hours"] == 0.0, "WORKING sorts first under the old rule"
    share_after = (xlm["allocated_usd"] + 240.38) / total * 100.0
    assert share_after < ic.MAX_DEST_SHARE_PCT, share_after


def test_money_never_moves_into_a_branch_that_has_never_closed_a_trip():
    plan = ic.rotation_plan(_fleet())
    dests = {m["to_product_id"] for m in plan.get("moves", [])}
    assert "XLM-USD" not in dests, "XLM-USD has never completed a round trip"


def test_the_destination_is_the_branch_that_actually_proved_itself():
    plan = ic.rotation_plan(_fleet())
    assert plan["ok"], plan
    assert [m["to_product_id"] for m in plan["moves"]] == ["JASMY-USD"]


def test_no_proven_destination_means_no_move_rather_than_a_guess():
    """With nothing certified, stale money stays where it is."""
    stale = _branch("crypto_grid_3", "OLD-USD", 240.38, created=NOW - timedelta(days=30))
    xlm = _branch("crypto_grid_10", "XLM-USD", 451.13, slices=4,
                  created=NOW - timedelta(days=20))
    out = ic.report([stale, xlm],
                    [_trade("crypto_grid_3", "OLD-USD", NOW - timedelta(days=26))], now=NOW)
    plan = ic.rotation_plan(out)
    assert not plan.get("moves")
    assert plan["refusals"][0]["reason"] == "NO_DESTINATION_UNDER_THE_SHARE_RULE"

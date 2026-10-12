"""Deploy cash judges a branch on the grid's OWN completed trips only.

An adopted_exit row (coin the owner held before the bot, booked at the
adoption mark) kept ZEC refused as NET_NEGATIVE_SO_FAR after its own trips
were positive. Owner, 2026-10-12: "clear this out ... the bot won't see it."
Runnable as a script or under pytest. No network, no database.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import idle_cash  # noqa: E402


def _branch(pid, alloc=100.0, open_slices=1, levels=3):
    return {"product_id": pid, "bot_name": "b_" + pid, "allocated_usd": alloc,
            "active": True, "locked": False, "open_slices": open_slices,
            "num_levels": levels}


def _plan(trades):
    # The 20% share rule and the minimum add are tested in test_idle_cash;
    # they are opened wide here so only the trip/net judgement is in play.
    return idle_cash.plan([_branch("ZEC-USD"), _branch("XLM-USD")], trades,
                          unclaimed_usd=500.0, reserve_usd=0.0, unbacked=set(),
                          max_share_pct=100.0, min_add_usd=1.0)


def _reasons(p):
    return {r["product_id"]: r["reason"] for r in (p.get("refusals") or [])}


def test_an_inherited_loss_no_longer_blocks_a_branch_whose_own_trips_are_positive():
    p = _plan([
        {"product_id": "ZEC-USD", "pnl": -123.24, "exit_reason": "adopted_exit"},
        {"product_id": "ZEC-USD", "pnl": -123.21, "exit_reason": "adopted_exit"},
        {"product_id": "ZEC-USD", "pnl": 0.50, "exit_reason": "parked_sell"},
        {"product_id": "XLM-USD", "pnl": 2.22, "exit_reason": "profit_target"},
    ])
    assert "ZEC-USD" not in _reasons(p), _reasons(p)
    assert {a["product_id"] for a in p["adds"]} == {"ZEC-USD", "XLM-USD"}


def test_a_branch_whose_own_trips_lose_is_still_refused():
    p = _plan([
        {"product_id": "ZEC-USD", "pnl": -0.40, "exit_reason": "stop_loss"},
        {"product_id": "XLM-USD", "pnl": 2.22, "exit_reason": "profit_target"},
    ])
    assert _reasons(p).get("ZEC-USD") == "NET_NEGATIVE_SO_FAR"


def test_inherited_exits_alone_are_not_a_completed_round_trip():
    p = _plan([
        {"product_id": "ZEC-USD", "pnl": 50.0, "exit_reason": "adopted_exit"},
        {"product_id": "XLM-USD", "pnl": 2.22, "exit_reason": "profit_target"},
    ])
    assert _reasons(p).get("ZEC-USD") == "NO_COMPLETED_ROUND_TRIP_YET"


def test_the_constant_matches_the_bot():
    import crypto_grid_bot
    assert idle_cash._ADOPTED_EXIT_REASON == crypto_grid_bot.ADOPTED_EXIT_REASON


if __name__ == "__main__":
    names = [n for n in sorted(globals()) if n.startswith("test_")]
    failed = 0
    for n in names:
        try:
            globals()[n]()
            print("ok   ", n)
        except Exception as e:  # noqa: BLE001
            failed += 1
            print("FAIL ", n, "-", type(e).__name__, e)
    print(f"{len(names) - failed}/{len(names)} passed")
    sys.exit(1 if failed else 0)

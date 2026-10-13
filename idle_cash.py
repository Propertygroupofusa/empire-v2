"""Deploy cash that no branch has claimed - into branches that can spend it.

A CORRECTION THIS MODULE EXISTS TO ENCODE

I told the account owner the fleet was UNDER-cashed: 21 branches x 3 rungs
against $7,422 allocated needs roughly $2,474 to refill everything, and
only ~$1,021 was in the wallet.

That was wrong, and wrong in a way worth writing down. It assumed every
branch could buy. Thirteen of the twenty-one hold as many slices as they
have levels and cannot buy at all, so they will never call on that cash
until they sell first. Measured properly, only eight branches can buy, and
filling every one of their open rungs costs $591.10 against $1,162.11 in
the wallet. The fleet is not short of ammunition. It has a surplus.

WHAT IS ACTUALLY DEPLOYABLE

Not the wallet balance. allocated_usd already claims the cash behind every
unfilled rung, so most of the wallet is spoken for. The deployable figure
is what no branch claims - allocation_backing's own unbacked number -
which is a smaller and more honest figure than "cash in the wallet".

WHERE IT GOES, AND WHY NOT TO THE BEST COIN

Only into branches that CAN buy. Adding allocation to a parked branch
raises its claim without giving it anything to do - the money would be
just as idle, with a branch's name on it.

And spread evenly across those, not concentrated on the leader. NEAR has
earned more than every other branch combined, and coin_evidence still
calls it HOLD: four closed trips cannot separate a coin from the fleet
when ten trips can only catch one below 17% green. Backing a four-trade
sample with real money is exactly the "talking itself out of its winners"
mistake in reverse. Equal weight is what the evidence supports, and it
says so out loud rather than pretending to a ranking it does not have.

A branch with NO completed trip is skipped. That is not a verdict on the
coin - it is that a branch which has never finished a round trip has not
yet shown it can turn allocation into anything.
"""
from __future__ import annotations

import os as _os

# Below this a branch is not worth a separate bookkeeping entry.
MIN_ADD_USD = float(_os.getenv("GRID_IDLE_CASH_MIN_ADD_USD", "25"))
# Keep a working float back rather than deploying to the last dollar: a
# branch that sells needs somewhere for the proceeds to land, and a wallet
# at zero turns an ordinary rebuy into a failed order.
# crypto_grid_bot.ADOPTED_EXIT_REASON, spelled out here so this module stays
# importable on its own (the tests import it without the bot).
_ADOPTED_EXIT_REASON = "adopted_exit"

RESERVE_USD = float(_os.getenv("GRID_IDLE_CASH_RESERVE_USD", "100"))
# The owner's standing rule, measured on allocation - what this moves.
MAX_SHARE_PCT = float(_os.getenv("GRID_IDLE_CASH_MAX_SHARE_PCT", "20"))


def _f(v, d=0.0):
    try:
        x = float(v)
        return x if x == x else d
    except (TypeError, ValueError):
        return d


def deploy_inputs(status, cash_floor_usd):
    """(pot, reserve, unbacked) for plan(), read from one grid status.

    THE BUG THIS EXISTS FOR, 2026-10-11. The Deploy cash button held back
    RESERVE_USD ($100) and nothing else, while the buy path holds back the
    owner's cash floor (GRID_CASH_RESERVE_USD, $1,820 live). Measured that
    evening: $3,181.28 unclaimed, $2,987.48 real free cash, and a plan of
    $2,801.20 across ten branches - $1,633.72 more budget than exists above
    the floor - including $280.12 for SOL-USD, whose coin is staked and
    which the backing gate refuses to buy at all.

    pot is the SMALLER of the unclaimed figure and the real free cash, so
    a planner can never hand out money the free-cash reading does not see.
    reserve is the larger of this module's working float and the owner's
    floor. unbacked is every product the backing gate (OWNED units) would
    refuse to buy - funding it is idle money with a branch's name on it.
    An unreadable free-cash figure deploys nothing.
    """
    status = status or {}
    backing = status.get("allocation_backing") or {}
    unclaimed = -_f(backing.get("unbacked_usd"))
    free = status.get("real_free_cash_usd")
    pot = 0.0 if free is None else min(unclaimed, _f(free))
    reserve = max(RESERVE_USD, _f(cash_floor_usd))
    unbacked = {u.get("product_id")
                for u in ((status.get("backing_owned") or {}).get("unbacked") or [])
                if hasattr(u, "get") and u.get("product_id")}
    return round(pot, 2), reserve, unbacked


def partial_record(history):
    """Why the trade rows are NOT the whole record, or None when they are.

    plan() refuses a branch whose own trips net negative, and it can only
    judge that from every one of those trips. get_grid_trade_history()
    returns the newest closes first, at most GRID_TRADE_HISTORY_MAX_ROWS of
    them; past that the oldest losses fall out of view and a losing coin
    reads as clean. A 50-row window did exactly that on 2026-10-13: the
    first auto-deploy funded APE, TON and ONDO. A partial record funds
    nobody.
    """
    h = history if hasattr(history, "get") else {}
    if not h.get("recent_trades_truncated"):
        return None
    return (f"only the latest {h.get('recent_trades_returned')} of "
            f"{h.get('total_trade_count')} closed trades could be read, so a "
            f"coin's older losses could be out of view - nothing deployed. "
            f"GRID_TRADE_HISTORY_MAX_ROWS needs to be above "
            f"{h.get('total_trade_count')}.")


def plan(branches, trades, *, unclaimed_usd, min_add_usd=None,
         reserve_usd=None, max_share_pct=None, unbacked=None):
    """What to add, to which branches. Moves nothing."""
    min_add = MIN_ADD_USD if min_add_usd is None else min_add_usd
    reserve = RESERVE_USD if reserve_usd is None else reserve_usd
    max_share = MAX_SHARE_PCT if max_share_pct is None else max_share_pct

    # Trips AND their sign. Counting trips alone treats "has traded" as
    # "is worth funding", and those are not the same claim: measured
    # 2026-09-27..10-03, ONDO-USD completed 5 round trips and lost money on
    # the net of them (-$4.57), then lost again in the following days
    # (-$2.46 per $100, then -$4.56 per $100). It cleared the one-trip bar
    # every time and would have been handed an equal share.
    trips = {}
    net = {}
    for t in (trades or ()):
        if hasattr(t, "get") and t.get("product_id"):
            # INHERITED EXITS DO NOT COUNT, as a trip or as money. An
            # adopted_exit row is coin the owner bought before the bot,
            # booked at the adoption mark rather than at what he paid: ZEC's
            # four read -$311.24 that way and +$297.78 against his real cost
            # (crypto_grid_bot restates them on read). Summed raw, that
            # bookkeeping kept ZEC refused here as NET_NEGATIVE_SO_FAR long
            # after the branch's own trips had turned positive. The owner,
            # 2026-10-12: "clear this out ... that way the bot won't see it."
            # Only the grid's OWN completed trips judge a branch here.
            if t.get("exit_reason") == _ADOPTED_EXIT_REASON:
                continue
            pid = t["product_id"]
            trips[pid] = trips.get(pid, 0) + 1
            net[pid] = net.get(pid, 0.0) + _f(t.get("pnl"))

    rows = [b for b in (branches or ()) if hasattr(b, "get")]
    total_alloc = sum(_f(b.get("allocated_usd")) for b in rows)

    eligible, refusals = [], []
    for b in rows:
        pid = b.get("product_id")
        o = int(_f(b.get("open_slices")))
        lv = int(_f(b.get("num_levels")))
        if not b.get("active", True):
            refusals.append({"product_id": pid, "reason": "BRANCH_IS_INACTIVE"})
            continue
        if b.get("locked"):
            refusals.append({"product_id": pid, "reason": "BRANCH_IS_LOCKED"})
            continue
        if unbacked and pid in unbacked:
            refusals.append({"product_id": pid, "reason": "UNBACKED_CANNOT_BUY"})
            continue
        if lv and o >= lv:
            refusals.append({"product_id": pid, "reason": "PARKED_CANNOT_SPEND_IT",
                             "open": o, "levels": lv})
            continue
        if trips.get(pid, 0) < 1:
            refusals.append({"product_id": pid, "reason": "NO_COMPLETED_ROUND_TRIP_YET"})
            continue
        # REFUSE-ONLY, and deliberately NOT a performance ranking.
        #
        # This module's own reasoning against weighting by performance
        # still holds and is not being reversed: a handful of trips cannot
        # separate a good coin from a lucky one, so the equal split stays.
        # Tested directly on the fleet's own ledger - ranking branches by
        # return per dollar in 2026-09-27..09-30 predicted the same ranking
        # over 09-30..10-04 at Spearman +0.459 across 14 coins, under the
        # ~0.544 that sample needs for significance. Not good enough to
        # size a position on.
        #
        # Losing money is a different and much coarser question than
        # ranking winners, and it needs no significance test: a branch
        # whose completed trips net out NEGATIVE has not yet shown it can
        # do the one thing the money is for. This withholds from it. It can
        # only ever refuse - it never adds to anyone's share, never
        # reorders the split, and a branch that turns positive becomes
        # eligible again on its own.
        if net.get(pid, 0.0) < 0:
            refusals.append({"product_id": pid, "reason": "NET_NEGATIVE_SO_FAR",
                             "net_realised_usd": round(net.get(pid, 0.0), 2),
                             "round_trips": trips.get(pid, 0)})
            continue
        eligible.append(b)

    deployable = round(max(0.0, _f(unclaimed_usd) - reserve), 2)
    if deployable < min_add or not eligible:
        return {
            "ok": False, "adds": [], "refusals": refusals,
            "unclaimed_usd": round(_f(unclaimed_usd), 2),
            "reserve_usd": reserve, "deployable_usd": deployable,
            "total_usd": 0.0, "is_a_plan_not_a_change": True,
            "detail": (f"${deployable:,.2f} deployable after a ${reserve:,.0f} reserve, "
                       f"and {len(eligible)} branch(es) able to spend it - nothing to do."),
        }

    # Equal weight. The evidence cannot rank these, and pretending it can is
    # how a four-trade sample gets funded like a proven one.
    share = round(deployable / len(eligible), 2)
    adds = []
    spent = 0.0
    for b in sorted(eligible, key=lambda x: -_f(x.get("allocated_usd"))):
        pid = b.get("product_id")
        cur = _f(b.get("allocated_usd"))
        amt = min(share, round(deployable - spent, 2))
        if amt < min_add:
            refusals.append({"product_id": pid, "reason": "BELOW_MIN_ADD", "usd": amt})
            continue
        after = cur + amt
        if total_alloc > 0 and (after / (total_alloc + amt) * 100.0) > max_share:
            refusals.append({"product_id": pid, "reason": "WOULD_BREACH_THE_SHARE_RULE",
                             "share_pct_after": round(after / (total_alloc + amt) * 100.0, 2)})
            continue
        adds.append({
            "bot_name": b.get("bot_name"), "product_id": pid,
            "usd": amt,
            "allocated_before": round(cur, 2), "allocated_after": round(after, 2),
            "open_slices": int(_f(b.get("open_slices"))),
            "num_levels": int(_f(b.get("num_levels"))),
            "completed_trips": trips.get(pid, 0),
            "share_pct_after": (round(after / (total_alloc + amt) * 100.0, 2)
                                if total_alloc else None),
        })
        spent = round(spent + amt, 2)

    return {
        "ok": bool(adds), "adds": adds, "refusals": refusals,
        "unclaimed_usd": round(_f(unclaimed_usd), 2),
        "reserve_usd": reserve, "deployable_usd": deployable,
        "total_usd": round(spent, 2),
        "equal_weight": True,
        "is_a_plan_not_a_change": True,
        "detail": (f"Would add ${spent:,.2f} across {len(adds)} branch(es) that can "
                   f"actually buy and have completed at least one round trip, "
                   f"${share:,.2f} each. Equal weight - the evidence cannot rank them."
                   if adds else "Nothing to add."),
    }

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
RESERVE_USD = float(_os.getenv("GRID_IDLE_CASH_RESERVE_USD", "100"))
# The owner's standing rule, measured on allocation - what this moves.
MAX_SHARE_PCT = float(_os.getenv("GRID_IDLE_CASH_MAX_SHARE_PCT", "20"))


def _f(v, d=0.0):
    try:
        x = float(v)
        return x if x == x else d
    except (TypeError, ValueError):
        return d


def plan(branches, trades, *, unclaimed_usd, min_add_usd=None,
         reserve_usd=None, max_share_pct=None):
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

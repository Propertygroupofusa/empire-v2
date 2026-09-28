#!/usr/bin/env python3
"""Where the money from a closed branch goes next.

THE INSTRUCTION THIS EXISTS FOR

The account owner decided to close ZEC-USD - 31% of the fleet, zero
completed round trips in 26 days - and to put the proceeds into NEAR-USD
and JASMY-USD, the two branches with the best measured recycling on this
account's own record:

    NEAR-USD   0.145% net per $1 per day   4 closed trips
    JASMY-USD  0.135% net per $1 per day   3 closed trips

Both samples are small and this module does not pretend otherwise. The
ranking is the owner's call; what is enforced here is that the money
cannot go anywhere else, cannot go twice, and cannot rebuild the
concentration that made ZEC a problem in the first place.

WHAT IT WILL NOT DO

  * It never sells anything. The sale is the owner's own guarded call;
    this only ever runs AFTER it, on cash that is already in the wallet.
  * It never fires before the source is genuinely gone. A plan built
    while ZEC still holds slices would be spending money that has not
    arrived - so the source must be absent or empty, checked against the
    live branch list, not assumed from a timestamp.
  * It never runs twice. One-shot by a stored marker, because "top up
    NEAR again" every hour is how the idle-rotation worker quietly moved
    a freshly-funded branch into a coin that had never traded.
  * It never pushes a target through the 20% concentration ceiling, and
    it re-checks against cost basis, not the allocation it is writing.

WHAT IT ACTUALLY CHANGES

allocated_usd on the target branches, nothing else. Raising a branch's
allocation lets it buy more rungs on its own dips, through the same
gates every other buy passes - the net-edge gate, the spread limit, the
fee floor, the concentration ceiling. No order is placed here. The grid
decides when, this only decides where.
"""
from __future__ import annotations

import os as _os

import concentration_gate

# The owner's two targets, in priority order. A coin that has lost its
# branch is skipped rather than recreated - this module tops up, it does
# not decide the fleet's membership.
DEFAULT_TARGETS = ("NEAR-USD", "JASMY-USD")
DEFAULT_SOURCE = "ZEC-USD"

MIN_TRADE_USD = 5.0
LEVELS = 3
#: Below this a share cannot place a slice on every level and would just
#: hold cash. Same floor coin_deploy uses; stated once, not re-guessed.
MIN_SHARE_USD = MIN_TRADE_USD * LEVELS

RESERVE_USD = float(_os.getenv("GRID_CASH_RESERVE_USD", "88.0"))

__all__ = ["DEFAULT_TARGETS", "DEFAULT_SOURCE", "MIN_SHARE_USD", "plan"]


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def plan(branches, free_cash_usd, cost_basis_by_product,
         source=DEFAULT_SOURCE, targets=DEFAULT_TARGETS,
         reserve_usd=RESERVE_USD, already_done=False,
         min_share_usd=MIN_SHARE_USD):
    """What to add to which branch. Returns (rows, report). Moves nothing.

    `branches` is the live branch list - dicts with product_id,
    allocated_usd and slices. `cost_basis_by_product` is what the
    concentration ceiling measures against.

    Every refusal names its own arithmetic, so nobody has to re-derive it
    from the outside.
    """
    if already_done:
        return [], {"status": "DONE",
                    "detail": f"the {source} proceeds were already redeployed - "
                              f"this runs once, not every pass"}

    cash = _num(free_cash_usd)
    if cash is None:
        return [], {"status": "UNKNOWN",
                    "detail": "free cash unreadable - a gap is not a zero, nothing moved"}

    by_product = {}
    for b in (branches or ()):
        if hasattr(b, "get"):
            by_product[b.get("product_id")] = b

    # 1. The source must genuinely be gone. Absent, or present with no
    #    open slices - either way its money is out of coin and in cash.
    src = by_product.get(source)
    if src is not None and (src.get("slices") or []):
        return [], {"status": "WAITING",
                    "detail": (f"{source} still holds {len(src.get('slices') or [])} open "
                               f"slice(s) - it has not been closed yet, so there are no "
                               f"proceeds to move. Nothing is pre-spent.")}

    deployable = round(cash - max(0.0, float(reserve_usd or 0.0)), 2)
    if deployable < min_share_usd:
        return [], {"status": "HOLD",
                    "detail": (f"${cash:,.2f} free cash less the ${reserve_usd:,.2f} reserve "
                               f"leaves ${deployable:,.2f}, under the ${min_share_usd:,.2f} "
                               f"one branch needs to place a slice on every level"),
                    "deployable_usd": deployable}

    live = [t for t in targets if t in by_product]
    missing = [t for t in targets if t not in by_product]
    if not live:
        return [], {"status": "REFUSED",
                    "detail": (f"neither target has a branch any more "
                               f"({', '.join(targets)}) - this tops up existing branches "
                               f"and does not decide fleet membership"),
                    "missing": missing}

    # 2. Fewer, properly: drop the lowest-priority target rather than
    #    split into shares too small to trade.
    n = len(live)
    while n > 1 and deployable / n < min_share_usd:
        n -= 1
    funded, deferred = live[:n], live[n:]
    share = int((deployable / n) * 100) / 100.0   # floored, never rounded up

    # 3. Size to what the ceiling ALLOWS rather than proposing a number it
    #    will refuse. An even split of the ZEC proceeds came to $1,096.71
    #    a side and both targets were refused outright, which would have
    #    left the whole $2,193 sitting as cash - the ceiling doing its job
    #    and the plan wasting the result. headroom_usd answers "how much
    #    would pass", and the verdict still decides.
    rows, refusals, capped = [], [], []
    for product in funded:
        room = concentration_gate.headroom_usd(product, cost_basis_by_product)
        add = share if room is None else min(share, room)
        if add < min_share_usd:
            refusals.append({
                "product_id": product, "usd": round(add, 2),
                "reason": (f"the 20% ceiling leaves only ${add:,.2f} of room, under the "
                           f"${min_share_usd:,.2f} a branch needs to place a slice on "
                           f"every level")})
            continue
        add = int(add * 100) / 100.0
        ok, why = concentration_gate.concentration_verdict(
            product, cost_basis_by_product, add)
        if not ok:
            refusals.append({"product_id": product, "usd": add, "reason": why})
            continue
        if add < share - 0.005:
            capped.append({"product_id": product, "wanted": share, "allowed": add})
        b = by_product[product]
        rows.append({
            "product_id": product,
            "bot_name": b.get("bot_name"),
            "add_usd": add,
            "ceiling_headroom_usd": room,
            "allocated_before": b.get("allocated_usd"),
            "allocated_after": round((_num(b.get("allocated_usd")) or 0.0) + add, 2),
        })

    if not rows:
        return [], {"status": "REFUSED",
                    "detail": (f"every target was refused by the 20% concentration ceiling "
                               f"at ${share:,.2f} each - the money stays as cash rather "
                               f"than rebuilding the concentration this sale undid"),
                    "refusals": refusals}

    total = round(sum(r["add_usd"] for r in rows), 2)
    detail = (f"${cash:,.2f} free cash less the ${reserve_usd:,.2f} reserve leaves "
              f"${deployable:,.2f}; adding to {len(rows)} branch"
              f"{'es' if len(rows) != 1 else ''} "
              f"({', '.join(r['product_id'] for r in rows)}) - ${total:,.2f} in total. "
              f"No order is placed: each branch buys its own dips through the same gates "
              f"as every other branch.")
    if deferred:
        detail += (f" {', '.join(deferred)} deferred - splitting further would put every "
                   f"share under the ${min_share_usd:,.2f} it needs to trade.")
    if capped:
        detail += (" " + "; ".join(
            f"{c['product_id']} capped at ${c['allowed']:,.2f} of ${c['wanted']:,.2f} by "
            f"the 20% ceiling" for c in capped) + ".")
    left = round(deployable - total, 2)
    if left >= 0.01:
        detail += (f" ${left:,.2f} stays as cash - the ceiling would not take it, and cash "
                   f"is a better place for it than a branch that cannot spend it.")
    if missing:
        detail += f" {', '.join(missing)} has no branch and was skipped."
    if refusals:
        detail += f" {len(refusals)} refused by the concentration ceiling."
    return rows, {"status": "READY", "detail": detail, "total_usd": total,
                  "share_usd": share, "deferred": deferred, "missing": missing,
                  "refusals": refusals, "deployable_usd": deployable,
                  "capped_by_ceiling": capped, "left_as_cash_usd": left}

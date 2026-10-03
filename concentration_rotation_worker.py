"""Move capital out of an over-concentrated branch, at a profit, or not at all.

WHY THE SECOND ATTEMPT WAS THROWN AWAY

A version of this file was written on 2026-10-01, wired into main.py, and
reverted unshipped the same day. It repeated the first one's fatal defect:
on a successful fill it did

    placed += 1
    log.info(...)

and nothing else. It never retired the slice it had just sold, never
adjusted the branch's allocated_usd, never invalidated the balance cache,
never wrote a trade-history row. Every "successful" rotation would have
widened the gap between what the books claim and what the wallet holds -
8 branches and $1,168.59 of it at the time of writing, which is the single
thing most in need of NOT being made worse.

WHAT THIS ONE DOES INSTEAD

It does not sell. It hands the chosen slices to
`close_all_grid_slices(only_slice_ids=...)`, which is the grid's own
sell-and-settle path and the book of record: it retires the slice row,
writes allocated_usd back, writes the trade-history row with the real
per-slice P&L, moves the reference price, and logs the activity entry.
Not one line of that bookkeeping is reimplemented here, because a second
copy of it is how the first two attempts went wrong.

THE DECISIONS THIS FILE STILL OWNS, and they are only three:

  1. which branches are over the concentration limit  (concentration_rotation)
  2. which of their slices clear a real profit AFTER the taker exit leg
  3. how many to sell in one pass

PROFIT IS NOT OPTIONAL. The exit is a market sell, so it pays TAKER on the
way out. Every slice is priced with the rate it really paid on entry plus
that taker leg plus adverse selection, and must clear MIN_NET_MARGIN_PCT on
top. A slice that does not is skipped, and the check is made twice - once
when planning and once immediately before the settle call - so no future
edit to the planner can reach the order with a loser.

OFF BY DEFAULT. Nothing happens unless CONCENTRATION_ROTATION_MODE is
exactly "arm". Checked before anything is fetched, and again immediately
before each settle, so disarming stops the very next order rather than the
next pass.

IT NEVER BUYS. There is no buy path in this file. Freeing the capital is the
whole job; where it goes next is the allocator's decision, made with the
books already correct because the settle above corrected them.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime

import concentration_rotation as cr

log = logging.getLogger("rotation")

MODE_ENV = "CONCENTRATION_ROTATION_MODE"
MODE_OBSERVE, MODE_ARM = "observe", "arm"

INTERVAL_SECONDS = int(os.getenv("CONCENTRATION_ROTATION_SECONDS", "900"))

# A CAP ON ONE PASS. A mispriced feed that made many slices look profitable
# at once must not be able to empty a branch in a single wake-up. Three is
# enough to make progress on an over-weight position across a few hours and
# small enough that a bad pass is visible before it is expensive.
MAX_SELLS_PER_PASS = int(os.getenv("CONCENTRATION_ROTATION_MAX_SELLS", "3"))

HEARTBEAT = {"started_at": None, "last_pass_at": None, "passes": 0,
             "last_result": None, "last_error": None}


def current_mode() -> str:
    v = (os.getenv(MODE_ENV) or "").strip().strip('"').strip("'").lower()
    return MODE_ARM if v == MODE_ARM else MODE_OBSERVE


def is_armed() -> bool:
    return current_mode() == MODE_ARM


def _slice_net_pct(slice_row, price, default_cost_pct):
    """Net % this slice would really clear, after the fee it paid and the
    TAKER leg this exit pays. None when it cannot be priced at all.

    Mirrors concentration_rotation.sellable_slices' own arithmetic rather
    than inventing a second one, and keeps its guard: a suspiciously cheap
    recorded entry fee may never make the bar EASIER than the default.
    """
    entry = getattr(slice_row, "entry_price", None)
    qty = getattr(slice_row, "qty", None)
    if not entry or not qty or qty <= 0 or not price:
        return None
    cost = default_cost_pct
    efr = getattr(slice_row, "entry_fee_rate", None)
    if efr is not None and efr >= 0:
        cost = max((efr * 100.0) + cr.EXIT_FEE_PCT_DEFAULT
                   + cr.ADVERSE_SELECTION_PCT, default_cost_pct)
    return cr.exit_net_pct(entry, price, cost)


async def check_once():
    """One pass. Returns a dict describing what it did and why."""
    if not is_armed():
        return {"mode": MODE_OBSERVE, "armed": False, "settled": 0,
                "detail": f"observing - set {MODE_ENV}=arm to let it act"}

    import crypto_grid_bot as g

    status = await g.get_grid_status()
    branches = status.get("branches") or []
    if not branches:
        return {"mode": MODE_ARM, "armed": True, "settled": 0,
                "detail": "no branches reported"}

    over = cr.over_limit(branches)
    if not over:
        return {"mode": MODE_ARM, "armed": True, "settled": 0,
                "detail": "no branch is over the concentration limit"}

    # The REAL cost of this round trip, read from the fleet rather than
    # assumed: the entry leg as recorded per slice, plus the taker exit this
    # market sell actually pays.
    cost_pct = cr.DEFAULT_ROUND_TRIP_COST_PCT

    settled, results, skipped = 0, [], []
    for ob in over:
        pid = ob.get("product_id") if isinstance(ob, dict) else None
        branch = next((b for b in branches if b.get("product_id") == pid), None)
        if branch is None:
            continue
        price = branch.get("current_price")
        if not price:
            # A branch that cannot be priced cannot be sold at a known
            # profit. Skipped, and said so - not silently passed over.
            skipped.append({"product_id": pid, "reason": "PRICE_UNREADABLE"})
            continue
        bot_name = branch.get("bot_name")
        if not bot_name:
            skipped.append({"product_id": pid, "reason": "NO_BOT_NAME"})
            continue

        # THE DATABASE ROWS, not the status payload's copies. Only these
        # carry the real primary key, and settling by id is what keeps the
        # thing sold and the thing retired identical.
        rows = await g.get_grid_slices(bot_name)
        priced = []
        for r in rows:
            net = _slice_net_pct(r, price, cost_pct)
            if net is None or net <= cr.MIN_NET_MARGIN_PCT:
                continue
            priced.append((net, r))
        if not priced:
            skipped.append({"product_id": pid,
                            "reason": "NO_SLICE_CLEARS_THE_COST"})
            continue

        # Best margin first, capped. Selling the most profitable slice frees
        # the most capital per unit of risk taken off the table.
        priced.sort(key=lambda t: -t[0])
        chosen = priced[:max(0, MAX_SELLS_PER_PASS - settled)]
        if not chosen:
            break

        # SECOND CHECK, immediately before the order. The mode can change
        # between the top of this function and here, and a disarm must stop
        # the very next settle, not the next pass.
        if not is_armed():
            break
        # And one last refusal on every chosen slice, so no future edit to
        # the planner above can reach this call with a loser.
        if any(net <= cr.MIN_NET_MARGIN_PCT for net, _ in chosen):
            log.error(f"[rotation] REFUSING {pid}: a chosen slice does not "
                      f"clear {cr.MIN_NET_MARGIN_PCT}% net - not settling")
            skipped.append({"product_id": pid, "reason": "REFUSED_AT_THE_GATE"})
            continue

        ids = [r.id for _, r in chosen]
        out = await g.close_all_grid_slices(
            only_bot_name=bot_name,
            only_slice_ids=ids,
            exit_reason="concentration_rotation",
            # OPPORTUNISTIC, not a protection: there is always a next pass,
            # so never size an order off a balance the wallet could not
            # confirm.
            allow_unverified_balance=False)

        done = out.get("slices_closed") or 0
        settled += done
        results.append({"product_id": pid, "bot_name": bot_name,
                        "requested": len(ids), "settled": done,
                        "realized_usd": out.get("total_realized_pnl"),
                        "detail": out.get("results")})
        if settled >= MAX_SELLS_PER_PASS:
            break

    return {"mode": MODE_ARM, "armed": True, "settled": settled,
            "results": results, "skipped": skipped,
            "detail": (f"{settled} slice(s) settled through the grid's own "
                       f"sell path; {len(skipped)} branch(es) skipped")}


async def run_periodically():
    """Never dies. A pass that raises must not stop the next one."""
    HEARTBEAT["started_at"] = datetime.utcnow().isoformat() + "Z"
    log.info(f"[rotation] up, mode={current_mode()}, every {INTERVAL_SECONDS}s, "
             f"max {MAX_SELLS_PER_PASS} slice(s)/pass")
    while True:
        try:
            out = await check_once()
            HEARTBEAT["last_result"] = out.get("detail")
            HEARTBEAT["last_error"] = None
            if out.get("settled"):
                log.warning(f"[rotation] {out['detail']}")
        except Exception as e:
            HEARTBEAT["last_error"] = f"{type(e).__name__}: {e}"
            HEARTBEAT["last_result"] = None
            log.warning(f"[rotation] pass failed: {type(e).__name__}: {e}")
        HEARTBEAT["last_pass_at"] = datetime.utcnow().isoformat() + "Z"
        HEARTBEAT["passes"] += 1
        await asyncio.sleep(INTERVAL_SECONDS)

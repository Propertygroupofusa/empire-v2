"""How many rungs a branch may hold, and what changing that actually buys.

WHY THIS EXISTS. A grid branch can hold up to num_levels open slices. At
num_levels it is PARKED: it cannot buy another rung at any price, however
far the coin falls and however much allocation it has. Measured live
2026-10-02, ELEVEN of twenty-three branches were parked, including the two
best earners per dollar on the whole account:

    LINK   3 slices / 3 levels   $137.87 allocated   $11.01 earned
    NEAR   3 slices / 3 levels   $183.30 allocated   $13.00 earned

Both fill their three rungs, park, and wait. More allocation does nothing
for a parked branch - the rung count is the binding constraint, not the
money. There was no way to change it: nothing in the codebase writes
num_levels on an existing branch except coin_adoption_worker, which sets
it to the slice count at adoption (`row.num_levels = len(open_now) +
len(t["slices"])`), so an adopted branch is born full by construction.

WHAT CHANGING IT DOES, AND DOES NOT DO

slice_usd = allocated_usd / num_levels. So raising the level count makes
every FUTURE slice smaller on the same allocation. It does not add money.
A branch with no spare allocation gains room it cannot use, and this says
so rather than reporting a success that buys nothing.

It places no order, moves no cash, and touches no other setting - not the
spacing, not the reference price, not the stop, not the open slices. The
grid still decides WHEN to buy through every gate it already passes; this
only decides how many rungs it is allowed to hold.

NEVER BELOW WHAT IS ALREADY OPEN. Setting levels under the open slice
count would leave a branch holding more rungs than its own configuration
permits - which is exactly the state adoption creates and this module
exists to relieve. Refused.
"""
from __future__ import annotations

# A grid needs at least one rung, and beyond this the slices get too small
# to clear the venue's minimum order size on a modest allocation.
MIN_LEVELS = 1
MAX_LEVELS = 20

# Below this a slice is not worth placing: the venue has its own minimums
# and a slice this small cannot clear the round-trip fee by any step the
# fleet uses. Same floor redeploy_freed_cash states.
MIN_SLICE_USD = 5.0


def _num(v, default=None):
    try:
        out = float(v)
    except (TypeError, ValueError):
        return default
    return out if out == out else default


def plan(branch, new_levels):
    """What setting this branch to `new_levels` would mean. Changes nothing.

    `branch` is a grid-status branch dict (product_id, bot_name,
    allocated_usd, num_levels, slices with qty and entry_price).
    Returns a report dict; `ok` False means it would be refused.
    """
    if not isinstance(branch, dict) or not branch.get("product_id"):
        return {"ok": False, "status": "REFUSED",
                "detail": "no branch was supplied"}

    pid = str(branch.get("product_id"))
    try:
        want = int(new_levels)
    except (TypeError, ValueError):
        return {"ok": False, "status": "REFUSED", "product_id": pid,
                "detail": f"{new_levels!r} is not a whole number of levels"}

    slices = branch.get("slices") or []
    open_n = len(slices)
    old = branch.get("num_levels")
    old_n = int(_num(old, 0) or 0)
    alloc = _num(branch.get("allocated_usd"), 0.0) or 0.0
    spent = 0.0
    unpriced = 0
    for s in slices:
        q, e = _num(s.get("qty")), _num(s.get("entry_price"))
        if q is None or e is None:
            unpriced += 1
            continue
        spent += q * e
    free = round(alloc - spent, 2)

    base = {"product_id": pid, "bot_name": branch.get("bot_name"),
            "levels_before": old_n, "levels_after": want,
            "open_slices": open_n,
            "allocated_usd": round(alloc, 2),
            "already_spent_usd": round(spent, 2),
            "unspent_usd": free,
            "parked_before": open_n >= old_n if old_n else None}

    if want < MIN_LEVELS or want > MAX_LEVELS:
        return {**base, "ok": False, "status": "REFUSED",
                "detail": (f"{want} is outside the {MIN_LEVELS}-{MAX_LEVELS} range a "
                           f"grid branch is allowed")}
    if want == old_n:
        return {**base, "ok": False, "status": "NO_CHANGE",
                "detail": f"{pid} is already set to {want} level(s)"}
    if want < open_n:
        return {**base, "ok": False, "status": "REFUSED",
                "detail": (f"{pid} holds {open_n} open slice(s); setting it to {want} "
                           f"level(s) would leave it holding more rungs than its own "
                           f"configuration permits, which is the condition this is "
                           f"meant to relieve")}
    if unpriced:
        return {**base, "ok": False, "status": "UNKNOWN",
                "unpriced_slices": unpriced,
                "detail": (f"{unpriced} of {pid}'s slices carry no price, so its unspent "
                           f"allocation cannot be totalled. That is UNKNOWN, not zero, "
                           f"and the effect of a change cannot be stated.")}
    if alloc <= 0:
        return {**base, "ok": False, "status": "REFUSED",
                "detail": f"{pid} has no allocation, so a level count changes nothing"}

    slice_before = round(alloc / old_n, 2) if old_n else None
    slice_after = round(alloc / want, 2)
    room = want - open_n                       # rungs the new count permits
    affordable = int(free // slice_after) if slice_after > 0 else 0
    can_open = max(0, min(room, affordable))

    out = {**base, "ok": True, "status": "READY",
           "slice_usd_before": slice_before,
           "slice_usd_after": slice_after,
           "rungs_the_new_count_permits": room,
           "rungs_the_money_affords": affordable,
           "rungs_it_could_actually_open": can_open,
           "parked_after": can_open == 0,
           "places_no_order": True,
           "changes_only_num_levels": True}

    if slice_after < MIN_SLICE_USD:
        out["ok"] = False
        out["status"] = "REFUSED"
        out["detail"] = (f"{want} levels on ${alloc:,.2f} makes each slice "
                         f"${slice_after:,.2f}, under the ${MIN_SLICE_USD:,.2f} minimum "
                         f"a slice must be worth placing")
        return out

    if can_open == 0:
        # THE HONEST CASE. Room without money is not an improvement, and
        # reporting it as one is how a change that buys nothing gets
        # called a success.
        out["detail"] = (
            f"{pid} would go from {old_n} to {want} level(s), but it has only "
            f"${free:,.2f} unspent against a ${slice_after:,.2f} slice - so it still "
            f"cannot open a rung. The level count stops being the constraint; the "
            f"money becomes it. Raising the allocation is the other half.")
        out["buys_nothing_without_more_allocation"] = True
        return out

    out["detail"] = (
        f"{pid} goes from {old_n} to {want} level(s). Each future slice becomes "
        f"${slice_after:,.2f} instead of ${slice_before:,.2f}, and its ${free:,.2f} "
        f"unspent allocation can open {can_open} more rung(s) on its own dips. No "
        f"order is placed here - the grid still decides when.")
    return out


def plan_many(branches, wanted):
    """`wanted` maps product_id -> new level count. Reports every one."""
    by = {}
    for b in (branches or []):
        if isinstance(b, dict) and b.get("product_id"):
            by[str(b["product_id"])] = b
    rows, missing = [], []
    for pid, n in (wanted or {}).items():
        pid = str(pid)
        if pid not in by:
            missing.append(pid)
            continue
        rows.append(plan(by[pid], n))
    return {"plans": rows,
            "missing": missing or None,
            "ready": [r["product_id"] for r in rows if r.get("ok")],
            "refused": [r["product_id"] for r in rows if not r.get("ok")],
            "is_a_plan_not_a_change": True}

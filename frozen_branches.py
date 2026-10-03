"""A branch stuck in BOTH directions, and nothing saying so.

PARKED IS NOT THE PROBLEM. A grid branch buys only while
`len(slices) < num_levels` (crypto_grid_bot.py:6526). Filling its rungs
and waiting is the design working: it stops it buying with cash nobody
earmarked to it. It unparks the moment a slice sells.

FROZEN IS THE PROBLEM. A branch is frozen when it is full AND its
cheapest sell trigger is so far above the market that no sale is
realistically coming. It cannot buy, it cannot sell, and its allocation
does nothing - but every surface reports it as a healthy, funded branch,
because every individual fact about it is fine.

That is how ten branches holding 72.8% of the fleet's allocation sat
parked through nine consecutive silent watch checks. Nothing was
erroring. Nothing was alerting. The question "can this branch still
trade?" was simply never asked.

NOT A TRADING CHANGE. This computes a verdict and nothing else. It places
no order, moves no cash, writes no row and touches no threshold.

UNKNOWN IS A THIRD VERDICT. A branch whose price or reference cannot be
read is UNKNOWN, never "fine" - the gap is reported as a gap.
"""
from __future__ import annotations

# Below this a branch is too small for its being stuck to be worth waking
# anyone over. Same floor the alert queue already uses for money.
MATERIAL_USD = 25.0

# How far above the market the cheapest trigger has to sit before "waiting
# on price" stops being a fair description. A grid step is 1.8-3.0%, so a
# trigger a tenth of the way to double is not a wait, it is a wall.
FAR_FROM_TRIGGER_PCT = 10.0


def _f(v, default=None):
    try:
        out = float(v)
    except (TypeError, ValueError):
        return default
    return out if out == out else default


def assess_branch(b) -> dict:
    """One branch: can it buy, can it sell, and if neither, how stuck."""
    if not isinstance(b, dict) or not b.get("product_id"):
        return {"verdict": "UNKNOWN", "detail": "no branch was supplied"}
    pid = str(b["product_id"])
    slices = b.get("slices") or []
    levels = int(_f(b.get("num_levels"), 0) or 0)
    alloc = _f(b.get("allocated_usd"), 0.0) or 0.0
    price = _f(b.get("current_price"))
    step = _f(b.get("grid_pct"))

    out = {"product_id": pid, "asset": pid.split("-")[0],
           "allocated_usd": round(alloc, 2),
           "open_slices": len(slices), "num_levels": levels,
           "can_buy": (levels > 0 and len(slices) < levels),
           "is_a_measurement_not_a_change": True,
           "readable": True}

    if levels <= 0:
        out.update(verdict="UNKNOWN", detail=f"{pid} has no level count to judge against")
        return out
    if price is None or step is None:
        out.update(verdict="UNKNOWN",
                   detail=(f"{pid} could not be priced this pass, so whether it can sell "
                           f"is UNKNOWN - not that it is fine"))
        return out

    # The cheapest trigger among the slices it actually holds.
    gaps = []
    for s in slices:
        e = _f(s.get("entry_price"))
        if e is None:
            continue
        trigger = e * (1.0 + step)
        gaps.append((trigger / price - 1.0) * 100.0)
    if slices and not gaps:
        out.update(verdict="UNKNOWN",
                   detail=f"{pid} holds {len(slices)} slice(s) with no readable entry price")
        return out

    nearest = min(gaps) if gaps else None
    out["pct_to_nearest_trigger"] = (round(nearest, 2) if nearest is not None else None)
    out["can_sell_now"] = (nearest is not None and nearest <= 0)

    if out["can_buy"]:
        out.update(verdict="OK", detail=f"{pid} has room to buy {levels - len(slices)} more rung(s)")
        return out
    if not slices:
        out.update(verdict="OK", detail=f"{pid} holds nothing, so it is not stuck holding anything")
        return out
    if nearest is not None and nearest <= FAR_FROM_TRIGGER_PCT:
        out.update(verdict="PARKED",
                   detail=(f"{pid} is full ({len(slices)}/{levels}) so it cannot buy, but its "
                           f"cheapest slice is {nearest:.2f}% from selling - a wait, not a wall"))
        return out
    if alloc < MATERIAL_USD:
        out.update(verdict="FROZEN_IMMATERIAL",
                   detail=(f"{pid} is stuck both ways but only ${alloc:,.2f} is involved"))
        return out
    out.update(verdict="FROZEN",
               detail=(f"{pid} cannot buy (full at {len(slices)}/{levels}) and cannot sell - "
                       f"its cheapest trigger is {nearest:.2f}% above the market. "
                       f"${alloc:,.2f} is doing nothing in either direction."))
    return out


def assess(branches) -> dict:
    rows = [assess_branch(b) for b in (branches or [])]
    frozen = [r for r in rows if r.get("verdict") == "FROZEN"]
    unknown = [r for r in rows if r.get("verdict") == "UNKNOWN"]
    stuck_usd = round(sum(r.get("allocated_usd") or 0.0 for r in frozen), 2)
    return {
        "readable": True,
        "is_a_measurement_not_a_change": True,
        "branches": rows,
        "frozen": frozen,
        "frozen_count": len(frozen),
        "frozen_usd": stuck_usd,
        "unknown_count": len(unknown),
        "parked_count": sum(1 for r in rows if r.get("verdict") == "PARKED"),
        "material_usd": MATERIAL_USD,
        "far_from_trigger_pct": FAR_FROM_TRIGGER_PCT,
        "detail": (f"{len(frozen)} branch(es) holding ${stuck_usd:,.2f} can neither buy nor "
                   f"sell." if frozen else "no branch is stuck in both directions"),
    }

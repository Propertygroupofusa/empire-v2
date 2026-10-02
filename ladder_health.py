"""Can this branch actually WORK its position, or is it just holding it?

THE FINDING THIS MAKES PERMANENT, measured 2026-10-01 across all 165 closed
trades and every open slice on the live fleet.

A grid earns by holding rungs at DIFFERENT prices and selling one on a
bounce while keeping the rest. Some branches have that. Some hold one lump
split into equal pieces at ONE price, which is a position, not a ladder -
every piece needs the same move and they all move together.

The split was total:

    coin   adopted  bought   realised per $100 over 32 days
    LINK      0       3        $7.99
    PRIME     0       3        $7.79
    NEAR      0       2        $6.40
    HBAR      3       2        $4.63
    ETH       3       0        $0.93
    ZEC       6       1        $0.00
    XRP       9       0        $0.01

Every top earner had ZERO adopted slices. Every dead position was adopted.
ZEC and XRP held 52.9% of the fleet's capital and produced 0.3% of its
profit. XRP's nine slices sat on three prices spanning 0.86%, written 38
MICROSECONDS apart - one adoption event. LINK's three were 0.01 @ 14.343,
2.99 @ 15.212 and 3.10 @ 14.651, hours apart on different days.

None of that is a bug. coin_adoption.slice_units() says why in its own
docstring: equal entry prices are deliberate, because inventing a ladder
would write cost bases nobody paid. The consequence is simply that ADOPTED
COIN IS NOT GRID INVENTORY, and until now nothing in this system knew it.
The allocator, the league and the dashboard all treated a dead lump and a
working ladder as the same kind of thing.

WHAT THIS DOES AND DOES NOT DO

It measures. It moves no money, places no order and changes no threshold.
Its only job is to let every capital decision downstream tell the two apart,
so freeing money out of a lump cannot quietly put it back into another one.

UNREADABLE IS UNKNOWN, NOT HEALTHY. A branch whose price or slices cannot
be read scores UNKNOWN. It never scores LADDER by default, because a branch
nobody could measure has not earned a good verdict by being unmeasurable -
the same rule that made the concentration ceiling fail closed.
"""
from __future__ import annotations

# WHAT THE DATA ACTUALLY SUPPORTS, and what it does not.
#
# Two candidate signals were tested against realised return per $100 over the
# same 32 days, on all 22 branches holding slices:
#
#   bought_share >= 0.34   n=12 median 2.33  vs  n=10 median 0.59   sep 1.74
#   entry spread >= 5.0%   n= 4 median 6.21  vs  n=18 median 0.79   sep 5.42
#   entry spread >= 1.5%   n=14 median 1.08  vs  n= 8 median 0.84   sep 0.24
#
# The spread cut looks the strongest and is the one to distrust: n=4 is four
# branches, and this repo already has a rule about that - "three samples
# cannot be read as a result". Moving the cut from 5.0% to 1.5% collapses the
# separation from 5.42 to 0.24, which is the signature of a threshold fitted
# to the handful of branches that happen to earn.
#
# So this file does NOT claim to predict earnings. It DESCRIBES shape, and it
# measures its own separation against realised return so the claim can be
# re-checked as trades accumulate instead of being asserted once by whoever
# wrote it. Below MIN_BRANCHES_FOR_A_FINDING it reports the separation and
# refuses to call it established.

# Shape only. A "lump" is slices clustered too tightly to be sold as separate
# rungs; it is a description of the position, not a prediction about it.
MIN_LADDER_SPREAD_PCT = 1.5
MIN_DISTINCT_ENTRIES = 2

# Below this, a separation is reported and explicitly NOT called a finding.
MIN_BRANCHES_FOR_A_FINDING = 30

LADDER, LUMP, THIN, UNKNOWN = "LADDER", "LUMP", "THIN", "UNKNOWN"


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def assess_branch(branch):
    """One branch -> the SHAPE of its position. Pure; no I/O, no prediction."""
    pid = (branch or {}).get("product_id")
    slices = (branch or {}).get("slices") or []
    out = {"product_id": pid,
           "bot_name": (branch or {}).get("bot_name"),
           "allocated_usd": _num((branch or {}).get("allocated_usd")),
           "slices": len(slices),
           "verdict": UNKNOWN, "why": None,
           "distinct_entries": None, "spread_pct": None,
           "adopted_slices": None, "bought_slices": None, "bought_share": None}

    if not slices:
        out["verdict"] = THIN
        out["why"] = ("no open slice - nothing to work. Not a fault; a branch "
                      "holding nothing simply has no shape yet.")
        out["adopted_slices"] = out["bought_slices"] = 0
        return out

    entries = [e for e in (_num(s.get("entry_price")) for s in slices)
               if e is not None and e > 0]
    if not entries:
        out["why"] = ("no slice carries a readable entry price, so the shape "
                      "of this position cannot be measured. UNKNOWN is not a "
                      "clean bill of health.")
        return out

    adopted = sum(1 for s in slices if s.get("adopted"))
    out["adopted_slices"] = adopted
    out["bought_slices"] = len(slices) - adopted
    out["bought_share"] = round((len(slices) - adopted) / len(slices), 4)

    distinct = len({round(e, 10) for e in entries})
    lo, hi = min(entries), max(entries)
    spread = ((hi - lo) / lo * 100.0) if lo else 0.0
    out["distinct_entries"] = distinct
    out["spread_pct"] = round(spread, 4)

    if distinct < MIN_DISTINCT_ENTRIES or spread < MIN_LADDER_SPREAD_PCT:
        out["verdict"] = LUMP
        out["why"] = (f"{len(slices)} slice(s) on {distinct} price(s) spanning "
                      f"{spread:.2f}%. Too tight to sell as separate rungs - "
                      f"a position, not a ladder. DESCRIPTION, not a forecast.")
        return out

    out["verdict"] = LADDER
    out["why"] = (f"{len(slices)} slice(s) across {distinct} price(s) spanning "
                  f"{spread:.2f}% - far enough apart to be sold separately. "
                  f"DESCRIPTION, not a forecast.")
    return out


def headroom(branches):
    """How much of the fleet can still ACT, and how much is just waiting.

    WHY THIS IS THE LEADING INDICATOR, measured 2026-10-02.

    Closed trades per day ran 35, 24, 7, 6 - a collapse, on a fleet whose
    engine wins 86% of what it closes. Nothing broke. The branches FILLED
    UP. A branch holding as many slices as it has levels is "parked": it
    cannot buy another rung, and its only way out is selling a slice at a
    profit, which needs the price to move.

    On that date 9 of 23 branches were parked, holding $3,436.64 - 40.3% of
    the fleet's capital - and the four best earners per dollar were ALL
    among them: LINK ($7.99 per $100), PRIME ($7.79), HBAR ($4.63) and TIA
    ($2.85). The parts of the fleet that work best filled up first, which is
    exactly what you would expect and exactly what nothing was reporting.

    This is not a fault and it is not a stall. A parked branch still sells
    through the parked route the moment a slice clears its floor, and then
    it has room again. It is the fleet breathing. But "40% of the capital
    cannot buy today" is the number that explains a quiet week, and without
    it a quiet week looks like something being broken.

    Pure. Describes; predicts nothing; changes nothing.
    """
    rows, parked_usd, free_usd, unknown_usd = [], 0.0, 0.0, 0.0
    for b in (branches or ()):
        n = len((b or {}).get("slices") or [])
        lv = (b or {}).get("num_levels")
        a = _num((b or {}).get("allocated_usd")) or 0.0
        try:
            lv = int(lv)
        except (TypeError, ValueError):
            lv = None
        if lv is None or lv <= 0:
            # No level count is UNKNOWN, not room. Counting it as free would
            # overstate what the fleet can do, which is the direction that
            # misleads.
            state, why = UNKNOWN, "num_levels unreadable - room is unknown, not free"
            unknown_usd += a
        elif n >= lv and n > 0:
            state = "PARKED"
            why = (f"{n} slice(s) against {lv} level(s) - full. It cannot buy "
                   f"another rung; its way out is selling one at a profit.")
            parked_usd += a
        else:
            state = "ROOM"
            why = f"{n} of {lv} level(s) used - it can still buy."
            free_usd += a
        rows.append({"product_id": (b or {}).get("product_id"),
                     "slices": n, "num_levels": lv, "state": state,
                     "allocated_usd": a, "why": why})
    total = parked_usd + free_usd + unknown_usd
    rows.sort(key=lambda r: -(r.get("allocated_usd") or 0))
    return {
        "branches": len(rows),
        "parked": sum(1 for r in rows if r["state"] == "PARKED"),
        "with_room": sum(1 for r in rows if r["state"] == "ROOM"),
        "unknown": sum(1 for r in rows if r["state"] == UNKNOWN),
        "parked_usd": round(parked_usd, 2),
        "with_room_usd": round(free_usd, 2),
        "unknown_usd": round(unknown_usd, 2),
        "parked_share_pct": (round(parked_usd / total * 100.0, 2)
                             if total > 0 else None),
        "rows": rows,
        "note": ("PARKED is full, not broken: the branch sells through the "
                 "parked route as soon as a slice clears its floor, and then "
                 "has room again. This is the number that explains a quiet "
                 "week without reaching for a fault that is not there."),
    }


def separation(rows, realized_usd_by_product):
    """How well does the shape label actually separate realised return?

    Measures itself rather than asserting. Returns the two medians, their
    gap, and - the point of the function - whether there is enough data to
    call that gap a finding at all.
    """
    import statistics as _st
    lad, lump = [], []
    for r in rows or ():
        a = r.get("allocated_usd")
        pid = r.get("product_id")
        if not a or a <= 0 or pid is None:
            continue
        pnl = (realized_usd_by_product or {}).get(pid)
        if pnl is None:
            continue
        per100 = pnl / a * 100.0
        if r.get("verdict") == LADDER:
            lad.append(per100)
        elif r.get("verdict") == LUMP:
            lump.append(per100)
    n = len(lad) + len(lump)
    if not lad or not lump:
        return {"measured": False, "branches": n,
                "detail": "one side of the comparison is empty - nothing to compare"}
    a_med, b_med = _st.median(lad), _st.median(lump)
    enough = n >= MIN_BRANCHES_FOR_A_FINDING
    return {
        "measured": True,
        "branches": n, "ladders": len(lad), "lumps": len(lump),
        "ladder_median_per_100": round(a_med, 3),
        "lump_median_per_100": round(b_med, 3),
        "separation": round(a_med - b_med, 3),
        "is_a_finding": enough,
        "detail": (
            f"LADDER branches returned a median ${a_med:.2f} per $100 against "
            f"${b_med:.2f} for LUMP."
            + ("" if enough else
               f" NOT a finding: {n} branches is under the {MIN_BRANCHES_FOR_A_FINDING} "
               f"this needs. Reported so it can be re-checked as trades "
               f"accumulate, not acted on as a rule.")),
    }


def assess(branches, realized_usd_by_product=None):
    """The whole fleet's shape, plus how well that shape has separated
    realised return so far.

    `capital_in_lumps_usd` is the figure that matters: money held in
    positions the grid cannot work as rungs.
    """
    rows = [assess_branch(b) for b in (branches or [])]
    by, cap = {}, {LADDER: 0.0, LUMP: 0.0, THIN: 0.0, UNKNOWN: 0.0}
    for r in rows:
        by[r["verdict"]] = by.get(r["verdict"], 0) + 1
        a = r.get("allocated_usd")
        if a is not None:
            cap[r["verdict"]] = cap.get(r["verdict"], 0.0) + a
    known = cap[LADDER] + cap[LUMP]
    rows.sort(key=lambda r: -(r.get("allocated_usd") or 0))
    return {
        "readable": True,
        "branches": len(rows),
        "by_verdict": by,
        "capital_in_ladders_usd": round(cap[LADDER], 2),
        "capital_in_lumps_usd": round(cap[LUMP], 2),
        "capital_unmeasured_usd": round(cap[UNKNOWN] + cap[THIN], 2),
        "lump_share_pct": (round(cap[LUMP] / known * 100.0, 2)
                           if known > 0 else None),
        "rows": rows,
        "headroom": headroom(branches),
        "separation": (separation(rows, realized_usd_by_product)
                       if realized_usd_by_product else
                       {"measured": False,
                        "detail": "no realised P&L supplied, so this label has "
                                  "not been checked against outcomes here"}),
        "note": ("A LUMP is capital held in a position the grid cannot work as "
                 "separate rungs. Adopted coin is the usual cause and that is "
                 "BY DESIGN - coin_adoption writes one honest entry price "
                 "rather than inventing a ladder nobody paid for. This "
                 "describes the consequence. It does not change the adoption, "
                 "and it does not forecast returns."),
        "is_a_measurement_not_a_change": True,
    }

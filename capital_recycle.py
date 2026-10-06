"""How fast capital completes a cycle, and how much of it can cycle at all.

WHY THIS EXISTS. Every profit figure this system reports answers "did this
branch make money". None of them answered "how fast did the money come
back", and the two questions rank the fleet differently enough that acting
on the first alone is a mistake. Measured on the real closed book:

    XRP   $15.69 profit over 8,526.2 capital-days  ->  $0.0018 / cap-day
    HBAR  $15.42 profit over   133.7 capital-days  ->  $0.1153 / cap-day

Nearly identical money. HBAR is sixty-four times the capital engine, and
every dollars-ranked view in this codebase prints them side by side as
equals.

WHAT THIS IS NOT. It is a MEASUREMENT, not a router. Nothing here chooses
a coin, sizes a rung, or places an order, and the payload carries its own
out-of-sample test precisely so that nobody routes capital on it before
that test passes. Three ranking signals have already been tried on this
fleet's real tape and lost money out of sample; this module exists so the
fourth is judged by evidence that re-computes itself as the sample grows,
rather than by how good the idea sounds.

Every function here is pure. They take rows and return numbers, so they
can be tested without a database, a network, or a venue.

NOT capital_velocity.py, which already exists and is a different thing.
That module SCORES and TIERS coins and proposes target allocations - it
decides where money should go. This one only measures what already
happened and publishes the test that says whether any such decision is
supported yet. The names are close enough to confuse, so: this file
measures, that file allocates.
"""

import math
from collections import defaultdict


# A sell whose branch has not bought since is CENSORED, never a zero and
# never dropped: dropping it hides the slowest redeployments, which are
# exactly the ones the metric exists to find.
CENSORED = None


def _hours(a, b):
    """Hours from a to b, or None if either timestamp is missing."""
    if a is None or b is None:
        return None
    return (b - a).total_seconds() / 3600.0


def cycle_records(trades):
    """One record per completed round trip, with its capital-days.

    `trades` are rows with product_id, qty, entry_price, pnl, opened_at and
    closed_at. A row missing either timestamp, or with no capital behind
    it, is SKIPPED rather than given a default - a cycle whose duration is
    unknown cannot contribute to a per-day rate, and inventing one would
    put a fabricated number where a measurement belongs.
    """
    out = []
    for t in trades or []:
        qty = t.get("qty") or 0.0
        entry = t.get("entry_price") or 0.0
        capital = qty * entry
        hrs = _hours(t.get("opened_at"), t.get("closed_at"))
        if capital <= 0 or hrs is None or hrs <= 0:
            continue
        days = hrs / 24.0
        out.append({
            "product_id": t.get("product_id"),
            "pnl": float(t.get("pnl") or 0.0),
            "capital_usd": capital,
            "hold_days": days,
            "hold_hours": hrs,
            # The denominator of every rate below. A dollar held a day and
            # ten dollars held a tenth of a day are the same capital-day,
            # which is the whole point: it prices TIME as well as SIZE.
            "capital_days": capital * days,
            "exit_reason": t.get("exit_reason") or "(none)",
            # Carried through rather than rejoined by position later.
            # predictive_check has to split the book in TIME, and matching
            # records back to their source rows by list index is the kind
            # of silent off-by-one that produces a confident wrong answer.
            "opened_at": t.get("opened_at"),
            "closed_at": t.get("closed_at"),
        })
    return out


def _median(xs):
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def fleet_velocity(records):
    """The headline: profit per dollar-day, and the hold-time distribution."""
    if not records:
        return {"cycles": 0, "usd_per_capital_day": None, "unknown_reason":
                "no completed cycle carries both a capital and a duration"}
    profit = sum(r["pnl"] for r in records)
    cap_days = sum(r["capital_days"] for r in records)
    holds = [r["hold_hours"] for r in records]
    return {
        "cycles": len(records),
        "profit_usd": round(profit, 2),
        "capital_days": round(cap_days, 1),
        "usd_per_capital_day": round(profit / cap_days, 6) if cap_days else None,
        "pct_per_day": round(profit / cap_days * 100, 4) if cap_days else None,
        "hold_hours_median": round(_median(holds), 1),
        "hold_hours_mean": round(sum(holds) / len(holds), 1),
        "hold_hours_longest": round(max(holds), 1),
        "means": ("Profit per dollar-day. A dollar tied up for ten days and ten "
                  "dollars tied up for one day cost the same capital-day, so this "
                  "prices TIME alongside size - which dollars-only ranking cannot."),
    }


def recycle_rate(records):
    """What one dollar became, per completed cycle.

    Deliberately NOT a return on the account: the denominator is CUMULATIVE
    deployment, so the same dollar recycled twenty times is counted twenty
    times. That is the right denominator for "what does a cycle do to a
    dollar" and the wrong one for "what did the account earn" - which is
    why the key is named for the cycle and the note says so.
    """
    deployed = sum(r["capital_usd"] for r in records)
    profit = sum(r["pnl"] for r in records)
    if deployed <= 0:
        return {"cycles": len(records), "multiple": None}
    return {
        "cycles": len(records),
        "deployed_usd": round(deployed, 2),
        "returned_usd": round(deployed + profit, 2),
        "multiple": round((deployed + profit) / deployed, 5),
        "pct_per_cycle": round(profit / deployed * 100, 3),
        "means": ("$1.00 of deployment came back as this. The denominator is "
                  "CUMULATIVE deployment across every cycle, not the account - "
                  "one dollar recycled twenty times is counted twenty times."),
    }


def redeploy_gaps(sell_times_by_branch, buy_times_by_branch):
    """Hours from a profitable exit to that branch's next buy.

    The branch is the unit because the branch is what holds the rung. A
    fleet-wide answer would credit one branch's buy against another's sell
    and read far faster than anything that actually happened.
    """
    gaps, censored = [], 0
    for pid, sells in (sell_times_by_branch or {}).items():
        buys = sorted(buy_times_by_branch.get(pid) or [])
        for t in sells:
            nxt = next((b for b in buys if b > t), CENSORED)
            if nxt is CENSORED:
                censored += 1
            else:
                gaps.append((nxt - t).total_seconds() / 3600.0)
    if not gaps:
        return {"measured": 0, "still_waiting": censored, "median_hours": None,
                "unknown_reason": "no sell in this window has been followed by a buy"}
    return {
        "measured": len(gaps),
        "still_waiting": censored,
        "median_hours": round(_median(gaps), 1),
        "mean_hours": round(sum(gaps) / len(gaps), 1),
        "fastest_minutes": round(min(gaps) * 60, 0),
        "slowest_days": round(max(gaps) / 24.0, 1),
        "within_1h": sum(1 for g in gaps if g <= 1),
        "within_6h": sum(1 for g in gaps if g <= 6),
        "within_24h": sum(1 for g in gaps if g <= 24),
        "within_72h": sum(1 for g in gaps if g <= 72),
        "censoring_note": (f"{censored} sell(s) have had no buy in that branch since. "
                           "They are counted as still waiting, never as zero and never "
                           "dropped - dropping them would hide the slowest "
                           "redeployments, which are the ones this measures for."),
    }


def by_branch(records, min_cycles=3):
    """Per-branch velocity, with the thin samples held back rather than shown.

    A branch with one closed trade produces a rate with no error bar, and a
    table that prints it next to a branch with fifteen invites exactly the
    comparison it cannot support.
    """
    agg = defaultdict(lambda: {"cycles": 0, "profit": 0.0, "capital_days": 0.0,
                               "hold_hours": 0.0})
    for r in records:
        a = agg[r["product_id"]]
        a["cycles"] += 1
        a["profit"] += r["pnl"]
        a["capital_days"] += r["capital_days"]
        a["hold_hours"] += r["hold_hours"]
    shown, withheld = [], []
    for pid, a in agg.items():
        row = {
            "product_id": pid,
            "cycles": a["cycles"],
            "profit_usd": round(a["profit"], 2),
            "capital_days": round(a["capital_days"], 1),
            "usd_per_capital_day": (round(a["profit"] / a["capital_days"], 6)
                                    if a["capital_days"] else None),
            "avg_hold_hours": round(a["hold_hours"] / a["cycles"], 1),
        }
        (shown if a["cycles"] >= min_cycles else withheld).append(row)
    shown.sort(key=lambda r: (r["usd_per_capital_day"] is None,
                              -(r["usd_per_capital_day"] or 0)))
    withheld.sort(key=lambda r: -r["cycles"])
    return {
        "min_cycles": min_cycles,
        "ranked": shown,
        "too_few_cycles": withheld,
        "withheld_note": (f"A branch under {min_cycles} closed cycles is listed "
                          "separately, not ranked. A rate from one or two trades is "
                          "arithmetic, not evidence, and printing it beside a "
                          "fifteen-trade branch invites a comparison it cannot carry."),
    }


# Two-sided 5% critical values for Spearman's rho. Small n only, because
# small n is the whole risk here: at n=6 a rho of +0.77 looks convincing
# and is not significant.
_SPEARMAN_CRIT_5PCT = {
    5: 1.000, 6: 0.886, 7: 0.786, 8: 0.738, 9: 0.700, 10: 0.648,
    11: 0.618, 12: 0.587, 13: 0.560, 14: 0.538, 15: 0.521, 16: 0.503,
    17: 0.485, 18: 0.472, 19: 0.460, 20: 0.447, 21: 0.435, 22: 0.425,
    23: 0.415, 24: 0.406, 25: 0.398, 26: 0.390, 27: 0.382, 28: 0.375,
    29: 0.368, 30: 0.362,
}


def _spearman_critical(n):
    """The bar rho must clear. Exact for small n, t-approximated beyond it."""
    if n < 5:
        return None
    if n in _SPEARMAN_CRIT_5PCT:
        return _SPEARMAN_CRIT_5PCT[n]
    t = 1.96  # large-sample two-sided 5%
    return t / math.sqrt(n - 2 + t * t)


def predictive_check(records, min_cycles_each_half=3):
    """Does a branch's PAST velocity rank predict its FUTURE velocity rank?

    This is the gate between a measurement and a router, and it lives in
    the payload so it re-runs itself as the book grows rather than being a
    number somebody wrote down once.

    It is here because three ranking ideas have already been tested on this
    fleet's real tape and lost money out of sample - the yield-weighted one,
    which is "give the capital to the highest-scoring branch", was the worst
    of them. A signal that has not cleared this bar must not be allowed to
    move a dollar, however sensible it reads.

    Splits the closed book in half chronologically, ranks each branch's
    velocity within each half, and correlates the two rankings.
    """
    ordered = [r for r in records if r.get("closed_at") is not None]
    ordered.sort(key=lambda r: r["closed_at"])
    if len(ordered) < 2 * min_cycles_each_half:
        return {"verdict": "UNKNOWN", "reason": "not enough closed cycles to split"}
    mid = len(ordered) // 2

    def vel(rows):
        a = defaultdict(lambda: [0.0, 0.0, 0])
        for r in rows:
            v = a[r["product_id"]]
            v[0] += r["pnl"]
            v[1] += r["capital_days"]
            v[2] += 1
        return {k: (v[0] / v[1], v[2]) for k, v in a.items() if v[1] > 0}

    first, second = vel(ordered[:mid]), vel(ordered[mid:])
    common = [k for k in first if k in second
              and first[k][1] >= min_cycles_each_half
              and second[k][1] >= min_cycles_each_half]
    n = len(common)
    if n < 5:
        return {
            "verdict": "UNKNOWN",
            "branches_in_both_halves": n,
            "reason": (f"only {n} branch(es) have {min_cycles_each_half}+ closed "
                       "cycles in BOTH halves of the book. The test needs at least "
                       "5 to say anything, and that shortage is itself the finding: "
                       "the sample is not yet large enough to route capital on."),
            "routing_allowed": False,
        }

    def ranks(d):
        s = sorted(common, key=lambda k: d[k][0])
        return {k: i for i, k in enumerate(s)}

    ra, rb = ranks(first), ranks(second)
    dsq = sum((ra[k] - rb[k]) ** 2 for k in common)
    rho = 1 - 6.0 * dsq / (n * (n * n - 1))
    crit = _spearman_critical(n)
    passed = crit is not None and abs(rho) >= crit
    return {
        "verdict": "PREDICTIVE" if passed else "NOT STATISTICALLY SUPPORTED",
        "spearman_rho": round(rho, 3),
        "critical_value_5pct": round(crit, 3) if crit else None,
        "branches_in_both_halves": n,
        "split_at": ordered[mid]["closed_at"].isoformat() if hasattr(
            ordered[mid]["closed_at"], "isoformat") else str(ordered[mid]["closed_at"]),
        # The one field a caller should read before acting. It is False
        # until the correlation clears its own bar, and nothing in this
        # repository may route capital while it reads False.
        "routing_allowed": bool(passed),
        "means": ("Whether a branch's velocity rank in the first half of the book "
                  "survived into the second. Until this says PREDICTIVE, velocity "
                  "is a description of what happened and not a reason to move money."),
    }


# A slice under this much notional cannot be sold at the venue's size
# precision; it is not waiting on price, it is unsellable at any price.
DUST_USD = 1.00
# Both maker legs. A slice must clear this before a sale nets anything.
MAKER_ROUND_TRIP = 0.007


def classify_slices(branches, short_products=None, available_units=None):
    """Why each open slice cannot recycle right now, one reason each.

    The order matters and is not arbitrary. A branch short of coin cannot
    sell at ANY price, so that verdict outranks a price verdict; coin held
    behind a resting order is likewise a fact about inventory, not about
    the market. Only once both are ruled out does price get to explain a
    slice. Reading these in the other order produces the familiar mistake
    of calling a bookkeeping fault a market move.

    ABOVE_ITS_OWN_TRIGGER is the one worth understanding. A branch sells
    when price reaches reference x (1 + step), but the reference RESETS
    DOWN on every dip buy, while the slices bought before those dips keep
    their old, higher entries. So a branch's trigger can sit BELOW an older
    slice's break-even: the trigger fires, the certifier walks the slices
    and refuses every one that would book a loss, and nothing sells. That
    is the no-loss rule working exactly as designed, and it is also why a
    ladder grows a tail of old expensive slices.
    """
    short_products = set(short_products or ())
    available_units = available_units or {}
    out = []
    for b in branches or []:
        pid = b.get("product_id")
        price = b.get("current_price") or 0.0
        ref = b.get("reference_price") or 0.0
        step = b.get("grid_pct") or 0.0
        target = ref * (1 + step)
        avail = available_units.get(pid)
        for s in (b.get("slices") or []):
            qty = s.get("qty") or 0.0
            entry = s.get("entry_price") or 0.0
            cost = qty * entry
            breakeven = entry * (1 + MAKER_ROUND_TRIP)
            net_pct = s.get("unrealized_net_pct") or 0.0
            if pid in short_products:
                state = "ACCOUNTING_HOLD"
            elif cost < DUST_USD:
                state = "DUST"
            elif avail is not None and qty and avail < qty:
                state = "INVENTORY_LOCKED"
            elif price >= target and net_pct > 0:
                state = "CAN_SELL_NOW"
            elif target < breakeven:
                state = "ABOVE_ITS_OWN_TRIGGER"
            else:
                state = "WAITING_ON_PRICE"
            out.append({
                "product_id": pid, "capital_usd": round(cost, 2),
                "entry_price": entry, "break_even_price": breakeven,
                "branch_target_price": target, "current_price": price,
                "net_pct": net_pct, "state": state,
            })
    return out


# The states where price is NOT the thing holding the slice. These are the
# ones a human decision could release today; WAITING_ON_PRICE is not, and
# calling it trapped would overstate what is fixable.
_TRAPPED_STATES = ("ACCOUNTING_HOLD", "INVENTORY_LOCKED", "DUST",
                   "ABOVE_ITS_OWN_TRIGGER")


def trapped_capital(classified):
    """How much deployed capital cannot recycle, split by what is holding it."""
    by_state = defaultdict(lambda: {"slices": 0, "capital_usd": 0.0})
    for c in classified:
        s = by_state[c["state"]]
        s["slices"] += 1
        s["capital_usd"] += c["capital_usd"]
    total = sum(v["capital_usd"] for v in by_state.values())
    trapped = sum(by_state[k]["capital_usd"] for k in _TRAPPED_STATES if k in by_state)
    return {
        "deployed_usd": round(total, 2),
        "trapped_usd": round(trapped, 2),
        "trapped_pct": round(trapped / total * 100, 1) if total else None,
        "by_state": {k: {"slices": v["slices"],
                         "capital_usd": round(v["capital_usd"], 2)}
                     for k, v in sorted(by_state.items(),
                                        key=lambda kv: -kv[1]["capital_usd"])},
        "trapped_means": ("Deployed capital that price alone will not release: a "
                          "branch short of coin, units held behind a resting order, "
                          "a slice too small to sell, or a slice sitting above the "
                          "very trigger meant to sell it. WAITING_ON_PRICE is "
                          "deliberately NOT counted - that capital is working and "
                          "waiting, which is the strategy, not a fault."),
    }


def capital_tree(census, money):
    """Every dollar at the venue, in a bucket that says whether it can trade.

    BUILT ON THE WALLET, NOT ON CLAIM, and that choice is the point. A
    branch's allocation is a NAME ON A RUNG, not money in an account: the
    fleet's claim can exceed - and does exceed - the cash that exists. A
    tree built from allocations would show thousands of dollars of
    "committed cash" that cannot be spent, which is precisely the illusion
    of deployable capital this is meant to prevent. So the buckets sum to
    the venue total, and claim appears once, at the bottom, next to the
    cash that would have to back it.
    """
    cash = census.get("cash_usd")
    coin = census.get("coin_usd")
    total = census.get("total_usd")
    reserve = money.get("reserve_usd")
    free = money.get("free_cash_usd")
    deployed = money.get("deployed_usd")
    earmarked = money.get("earmarked_behind_slices_usd")
    unpriced = census.get("assets_unpriced")

    def sub(a, b):
        return None if a is None or b is None else round(a - b, 2)

    tree = {
        "venue_total_usd": total,
        "verified_available_cash_usd": free,
        "reserved_cash_usd": reserve,
        # Cash that is neither spendable now nor fenced as reserve. It is a
        # real residual, not a rounding gap, and naming it keeps the
        # buckets honest instead of quietly folding it into "available".
        "other_cash_usd": sub(sub(cash, reserve), free),
        "cash_usd": cash,
        "coin_usd": coin,
        "branch_held_coin_usd": deployed,
        "coin_no_branch_holds_usd": sub(coin, deployed),
        "unpriced_assets": unpriced,
        "unpriced_note": ("Assets the census could not price are NOT in the venue "
                          "total and must never be deployed. The real total is "
                          "higher than the figure shown by whatever they are worth."),
        "branch_claim_usd": earmarked,
        "claim_minus_cash_usd": sub(earmarked, cash),
        "claim_note": ("Claim is a NAME ON A RUNG, not money. Where claim exceeds "
                       "cash, the extra rungs cannot be funded no matter what the "
                       "branch allocation says - freeing claim does not create "
                       "spendable cash and never has."),
        "deployable_now_usd": free,
        "deployable_note": ("The only figure here that can buy something this "
                            "minute. Every other number is either already in coin, "
                            "fenced as reserve, or a claim with no cash behind it."),
    }
    return tree

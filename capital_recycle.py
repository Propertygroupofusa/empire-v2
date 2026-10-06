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
from datetime import datetime as _datetime


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


def classify_slices(branches, short_products=None, available_units=None,
                    now_epoch=None):
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
            # AGE, so an open slice can report the capital-days it is
            # accruing. Parsed defensively: a timestamp this code cannot
            # read becomes None, never 0 - a zero age would make an old
            # stuck slice report no capital cost at all, which is the
            # opposite of the truth.
            age_days = None
            opened = s.get("opened_at")
            if opened is not None and now_epoch is not None:
                try:
                    if isinstance(opened, str):
                        dt = _datetime.fromisoformat(opened.replace("Z", "+00:00"))
                        ts = dt.timestamp()
                    else:
                        ts = opened.timestamp()
                    age_days = max(0.0, (now_epoch - ts) / 86400.0)
                except Exception:
                    age_days = None
            out.append({
                "product_id": pid, "capital_usd": round(cost, 2),
                "entry_price": entry, "break_even_price": breakeven,
                "branch_target_price": target, "current_price": price,
                "net_pct": net_pct, "state": state, "age_days": age_days,
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


def _pct(sorted_xs, q):
    """Nearest-rank percentile. Returns None rather than interpolating a
    value out of a sample too small to have one."""
    if not sorted_xs:
        return None
    k = max(0, min(len(sorted_xs) - 1, int(round(q * (len(sorted_xs) - 1)))))
    return sorted_xs[k]


def recycle_distribution(gap_hours):
    """SELL -> next BUY as a DISTRIBUTION, not one number.

    A median alone hides the tail, and the tail is where the money sits:
    a fleet that redeploys half its exits in minutes and the other half in
    days has an execution problem a median of a few hours conceals
    entirely. The account owner asked for the percentiles for exactly that
    reason.
    """
    g = sorted(x for x in (gap_hours or []) if x is not None)
    if not g:
        return {"measured": 0, "unknown_reason": "no sell has been followed by a buy"}
    return {
        "measured": len(g),
        "p50_hours": round(_pct(g, 0.50), 2),
        "p75_hours": round(_pct(g, 0.75), 2),
        "p90_hours": round(_pct(g, 0.90), 2),
        "fastest_minutes": round(g[0] * 60, 1),
        "slowest_days": round(g[-1] / 24.0, 2),
        "means": ("Hours from a completed sell to that branch's next buy. The "
                  "percentiles are the point: a median hides the tail, and the "
                  "tail is where capital actually sits still."),
    }


def open_slice_records(classified, now_epoch=None, opened_at_by_key=None):
    """Capital-days ACCRUING on slices that have not closed.

    An open slice has a capital and an age, so it has capital-days. It does
    NOT have a $/capital-day, because it has no realised profit yet, and
    printing one from an unrealised mark would put a price move where a
    measurement belongs - the single most common way a trading dashboard
    lies. The field is omitted rather than estimated.
    """
    out = []
    for c in classified or []:
        age_days = c.get("age_days")
        cap = c.get("capital_usd") or 0.0
        out.append({
            "product_id": c.get("product_id"),
            "capital_usd": cap,
            "age_days": round(age_days, 3) if age_days is not None else None,
            "capital_days_accrued": round(cap * age_days, 2) if age_days is not None else None,
            "state": c.get("state"),
            "net_pct": c.get("net_pct"),
            "usd_per_capital_day": None,
            "why_no_rate": ("an open slice has no realised profit, so a rate would "
                            "be an unrealised price move wearing a measurement's "
                            "name"),
        })
    return out


def branch_rollup(records, branches, classified):
    """Per branch: what it has closed, what it is holding, and what it could
    still open. Three different questions, kept apart.

    available_rung_capacity answers "could this branch buy at all" and is
    NOT a claim that the cash exists to fund it - the fleet routinely has
    forty-odd free rungs and one rung's worth of cash. Those two facts
    together are the real bottleneck, and they only read correctly when
    neither is presented as the other.
    """
    closed = defaultdict(lambda: {"cycles": 0, "profit": 0.0, "capital_days": 0.0,
                                  "hold_hours": 0.0})
    for r in records or []:
        a = closed[r["product_id"]]
        a["cycles"] += 1
        a["profit"] += r["pnl"]
        a["capital_days"] += r["capital_days"]
        a["hold_hours"] += r["hold_hours"]
    held = defaultdict(lambda: {"deployed": 0.0, "trapped": 0.0, "slices": 0})
    for c in classified or []:
        h = held[c["product_id"]]
        h["deployed"] += c["capital_usd"]
        h["slices"] += 1
        if c["state"] in _TRAPPED_STATES:
            h["trapped"] += c["capital_usd"]
    out = []
    for b in branches or []:
        pid = b.get("product_id")
        sl = [s for s in (b.get("slices") or [])
              if (s.get("qty") or 0) * (s.get("entry_price") or 0) >= DUST_USD]
        levels = b.get("num_levels") or 0
        cl, hd = closed.get(pid), held.get(pid)
        out.append({
            "product_id": pid,
            "deployed_capital_usd": round(hd["deployed"], 2) if hd else 0.0,
            "trapped_capital_usd": round(hd["trapped"], 2) if hd else 0.0,
            "open_slices": hd["slices"] if hd else 0,
            # Rungs, not dollars. See the docstring.
            "available_rung_capacity": max(0, levels - len(sl)),
            "buys_frozen": bool(b.get("drawdown_breached")),
            "completed_cycles": cl["cycles"] if cl else 0,
            "realized_profit_usd": round(cl["profit"], 2) if cl else 0.0,
            "capital_days": round(cl["capital_days"], 1) if cl else 0.0,
            "avg_hold_hours": (round(cl["hold_hours"] / cl["cycles"], 1)
                               if cl and cl["cycles"] else None),
            "usd_per_capital_day": (round(cl["profit"] / cl["capital_days"], 6)
                                    if cl and cl["capital_days"] else None),
        })
    out.sort(key=lambda r: (r["usd_per_capital_day"] is None,
                            -(r["usd_per_capital_day"] or 0)))
    return out


# Every dollar in one of these states is capital the system must not spend,
# route, or add into any figure a buy gate reads. They are listed once,
# here, so a future caller cannot quietly disagree about what counts.
NEVER_DEPLOYABLE_STATES = (
    "unpriced",        # the census could not price it; its value is UNKNOWN
    "coin",            # already converted - it is a position, not purchasing power
    "unbranched",      # no branch monitors, prices or stops it
    "reserved",        # the account owner's floor
)


def never_deployable(census, money):
    """The dollars that may NEVER become buying power, and why each one.

    THE RULE THIS ENFORCES, in the account owner's words: unresolved
    capital never buys, never routes, and never counts as available.

    The danger is specific and has a number on it. The venue holds about
    $6,026 that belongs to no branch, and an accounting table calling it
    "unallocated" is one careless sum away from a buy gate reading it as
    spendable. It is not spendable: most of it is coin, some of it the
    census cannot even price, and none of it is cash with nothing claiming
    it.

    This function does not merely report. assert_deployable_is_clean()
    below turns it into a check that FAILS when the deployable figure has
    been contaminated, so the rule is enforced by the code rather than
    remembered by a reader.
    """
    cash = census.get("cash_usd")
    coin = census.get("coin_usd")
    untracked = census.get("untracked_usd")
    unpriced = census.get("assets_unpriced")
    reserve = money.get("reserve_usd")
    free = money.get("free_cash_usd")
    buckets = {
        "coin_usd": coin,
        "unbranched_usd": untracked,
        "reserved_cash_usd": reserve,
        "unpriced_asset_count": unpriced,
    }
    return {
        "buckets": buckets,
        "deployable_usd": free,
        "deployable_is": ("the USD WALLET balance less what is already claimed. It "
                          "is derived from cash and never from coin, so an "
                          "unpriced or unbranched asset cannot reach it by any "
                          "arithmetic - and assert_deployable_is_clean() fails "
                          "loudly if that ever stops being true."),
        "rule": ("UNRESOLVED CAPITAL: never buy, never route, never count as "
                 "available."),
    }


def assert_deployable_is_clean(census, money):
    """Returns the violations, empty when the rule holds. Never raises.

    A check that raised would take the whole panel down at the moment the
    owner most needs to see it. It returns findings instead, and the page
    shows them.
    """
    problems = []
    cash = census.get("cash_usd")
    free = money.get("free_cash_usd")
    coin = census.get("coin_usd")
    if free is None or cash is None:
        return [{"check": "readable", "detail":
                 "cash or free cash is unreadable on this pass, so the rule is "
                 "UNKNOWN rather than passing"}]
    # Deployable can never exceed the cash that exists. If it does, something
    # priced in coin has been counted as purchasing power.
    if free > cash + 0.005:
        problems.append({
            "check": "deployable_exceeds_cash",
            "detail": (f"deployable ${free:,.2f} is larger than the ${cash:,.2f} of "
                       "cash at the venue, so something that is not cash has been "
                       "counted as buying power"),
        })
    # And it can never be as large as cash plus coin, which would mean the
    # whole account was treated as spendable.
    if coin is not None and free >= (cash + coin) - 0.005 and coin > 0:
        problems.append({
            "check": "whole_account_treated_as_cash",
            "detail": "deployable has reached the entire venue balance, coin included",
        })
    if free < 0:
        problems.append({
            "check": "negative_deployable",
            "detail": f"deployable is ${free:,.2f}; a negative is not a floor to buy from",
        })
    return problems


# EVERY DOLLAR LANDS IN EXACTLY ONE OF THESE. There is deliberately no
# "unallocated" bucket: unallocated is not a state, it is the absence of
# one, and a dollar in it is one careless sum away from being read as
# buying power. Anything that cannot be placed is UNRESOLVED, which is a
# state with a rule attached - never buy, never route, never count as
# available.
DOLLAR_BUCKETS = (
    "VERIFIED_AVAILABLE",   # cash, unclaimed, spendable this minute
    "RESERVED",             # cash, fenced by the owner's floor
    "BRANCH_ALLOCATED",     # cash a branch has already claimed for a rung
    "VERIFIED_COIN",        # coin a branch holds AND the wallet confirms
    "UNRESOLVED",           # everything else, and it may never be spent
)


def classify_every_dollar(census, money, short_usd=0.0):
    """The venue balance, split so the parts sum back to the whole.

    The residual is published rather than absorbed. A classification whose
    parts quietly fail to add up is worse than none: it looks authoritative
    and is wrong by exactly the amount it does not mention. If `residual_usd`
    is not within a cent of zero, one of the inputs disagrees with another
    and the page says so instead of picking a side.

    CASH and COIN are split separately because they are different kinds of
    thing. Cash divides into what is spendable, what the floor holds, and
    what branches have already claimed. Coin divides into what a branch
    holds and can account for, and everything else.
    """
    cash = census.get("cash_usd")
    coin = census.get("coin_usd")
    total = census.get("total_usd")
    free = money.get("free_cash_usd")
    reserve = money.get("reserve_usd")
    deployed = money.get("deployed_usd")
    if cash is None or coin is None or free is None or reserve is None or deployed is None:
        return {"readable": False, "unknown_reason":
                ("one of cash, coin, free cash, reserve or deployed is unreadable "
                 "on this pass. A bucket built on a missing input would be a "
                 "guess wearing a total's name.")}

    # Cash the branches have claimed is whatever is left once the spendable
    # part and the floor are taken out. It is a RESIDUAL by construction,
    # never a figure read from an allocation table - branch claim exceeds
    # the cash that exists, so reading claim here would overstate the
    # account by thousands.
    branch_cash = round(cash - free - reserve, 2)
    # Coin a branch holds and the wallet confirms. The short figure is
    # subtracted because coin a branch CLAIMS but does not hold is not
    # verified coin by any reading.
    verified_coin = round(max(0.0, deployed - (short_usd or 0.0)), 2)
    unresolved = round(coin - verified_coin, 2)

    buckets = {
        "VERIFIED_AVAILABLE": round(free, 2),
        "RESERVED": round(reserve, 2),
        "BRANCH_ALLOCATED": branch_cash,
        "VERIFIED_COIN": verified_coin,
        "UNRESOLVED": unresolved,
    }
    summed = round(sum(buckets.values()), 2)
    residual = round((total if total is not None else cash + coin) - summed, 2)
    notes = {
        "VERIFIED_AVAILABLE": "cash with nothing claiming it - the only bucket that can buy",
        "RESERVED": "cash held by the account owner's floor",
        "BRANCH_ALLOCATED": ("cash a branch has claimed for a rung it has not bought "
                             "yet. A RESIDUAL of cash, never read from the allocation "
                             "table - claim exceeds the cash that exists"),
        "VERIFIED_COIN": "coin a branch holds and the wallet confirms",
        "UNRESOLVED": ("coin no branch holds, coin a branch claims but the wallet "
                       "does not have, and anything the census could not price. "
                       "NEVER buys, NEVER routes, NEVER counts as available"),
    }
    out = {
        "readable": True,
        "buckets": buckets,
        "notes": notes,
        "venue_total_usd": total,
        "sum_of_buckets_usd": summed,
        "residual_usd": residual,
        # Five buckets each rounded to the cent can carry up to two and a
        # half cents of rounding between them, so the tolerance is a few
        # cents and not zero. Anything larger is a real disagreement
        # between two inputs and is reported rather than absorbed.
        "balances": abs(residual) <= 0.05,
        "balance_tolerance_usd": 0.05,
        "no_unallocated_bucket": ("Unallocated is not a state, it is the absence of "
                                  "one. Anything that cannot be placed is UNRESOLVED, "
                                  "which carries a rule."),
    }
    if not out["balances"]:
        out["residual_warning"] = (
            f"the buckets sum to ${summed:,.2f} against a venue total of "
            f"${total:,.2f}. Something disagrees by ${residual:,.2f}; the split "
            "above is not trustworthy until that is explained.")
    return out


def recycle_ledger(trades, buys_by_branch, all_buys=None, limit=200):
    """One row per completed sell: what came back, and where it went next.

    THE ROW IS THE MEASUREMENT. An aggregate says the fleet redeploys in a
    median of eight hours; a row says which sell waited four days and which
    branch finally took the money. Only the second can be acted on.

    TWO "next buy" answers, because they are different questions and
    collapsing them would answer neither. The SAME branch's next buy is
    that branch's own recycle. The FLEET's next buy anywhere is where the
    cash actually went, since cash is shared and a branch that sells does
    not get first claim on its own proceeds.

    A sell with no buy after it is carried with None, never dropped - the
    slowest redeployments are the ones this exists to find.
    """
    rows, cumulative = [], 0.0
    ordered = sorted((t for t in (trades or []) if t.get("closed_at")),
                     key=lambda t: t["closed_at"])
    fleet = sorted(all_buys or [])
    for t in ordered:
        pid = t.get("product_id")
        qty = t.get("qty") or 0.0
        exit_price = t.get("exit_price")
        pnl = float(t.get("pnl") or 0.0)
        basis = qty * (t.get("entry_price") or 0.0)
        # What actually came back as cash. Gross proceeds when the exit
        # price is recorded; basis plus P&L when it is not. The two agree
        # to within the sell-leg fee, and which one was used is stated so
        # nobody reconciles them against each other and finds a phantom gap.
        if exit_price:
            released = qty * exit_price
            released_from = "exit price x quantity (gross of the sell-leg fee)"
        else:
            released = basis + pnl
            released_from = "cost basis plus realised P&L (no exit price recorded)"
        cumulative += released
        own = next((b for b in sorted(buys_by_branch.get(pid) or [])
                    if b > t["closed_at"]), None)
        nxt = next((b for b in fleet if b[0] > t["closed_at"]), None)
        hold_h = (((t["closed_at"] - t["opened_at"]).total_seconds() / 3600.0)
                  if t.get("opened_at") else None)
        cap_days = (basis * (hold_h / 24.0)) if hold_h else None
        own_h = None
        rows.append({
            "entry_time": (t["opened_at"].isoformat() if t.get("opened_at")
                           and hasattr(t["opened_at"], "isoformat")
                           else (str(t["opened_at"]) if t.get("opened_at") else None)),
            "exit_time": t["closed_at"].isoformat() if hasattr(t["closed_at"], "isoformat")
                         else str(t["closed_at"]),
            "sold_at": t["closed_at"].isoformat() if hasattr(t["closed_at"], "isoformat")
                       else str(t["closed_at"]),
            "hold_hours": round(hold_h, 2) if hold_h is not None else None,
            "capital_days": round(cap_days, 3) if cap_days is not None else None,
            # Per CYCLE, so a single fast winner and a single slow one are
            # comparable without averaging them first.
            "usd_per_capital_day": (round(pnl / cap_days, 6)
                                    if cap_days and cap_days > 0 else None),
            "branch": pid,
            "product": pid,
            "product_id": pid,
            "cash_released_usd": round(released, 2),
            "cash_released_from": released_from,
            "realized_profit_usd": round(pnl, 2),
            "capital_usd": round(basis, 2),
            "hours_to_next_buy_same_branch": (
                round((own - t["closed_at"]).total_seconds() / 3600.0, 2)
                if own else None),
            # MINUTES as well as hours, because the interesting end of this
            # distribution is minutes: the fastest redeployment on the real
            # book is under five, and reading that as "0.1 h" buries it.
            "sell_to_next_buy_minutes": (
                round((own - t["closed_at"]).total_seconds() / 60.0, 1)
                if own else None),
            "redeployed_amount_usd": round(nxt[2], 2) if nxt and nxt[2] is not None else None,
            "hours_to_next_buy_anywhere": (
                round((nxt[0] - t["closed_at"]).total_seconds() / 3600.0, 2)
                if nxt else None),
            "next_branch": nxt[1] if nxt else None,
            "next_buy_usd": round(nxt[2], 2) if nxt and nxt[2] is not None else None,
            "cumulative_recycled_usd": round(cumulative, 2),
            "still_waiting": own is None,
        })
    rows.reverse()                      # newest first, like every other feed here
    return {
        "rows": rows[:limit],
        "returned": min(len(rows), limit),
        "total_sells": len(rows),
        "truncated": len(rows) > limit,
        "cumulative_recycled_usd": round(cumulative, 2),
        "means": ("Cumulative recycled is the sum of every dollar that has come "
                  "back from a completed sell. The same dollar recycled twenty "
                  "times is counted twenty times - that is what 'recycled' means "
                  "and it is NOT the size of the account."),
    }

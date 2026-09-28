"""Net profit per $1 of capital per day - and what to do about it.

THE PREMISE, in the account owner's words: the problem is capital velocity,
not win rate. Capital should be allocated by how efficiently each coin
recycles it after fees, not by which coin has the biggest expected move.
Measured on the live book 2026-09-28, that is exactly right:

    coin   trips   net $   capital   per $1k/day
    NEAR       4    6.82       214       31.79
    JASMY      3    1.92        72       26.66
    ETH        9    2.30       400        5.75
    LINK       7    0.25       127        1.97
    ZEC        0    0.00     2,273        0.00
    XRP        0    0.00     2,241        0.00
    fleet     --   15.63     7,429        2.10

LINK ran seven round trips and produced 25 cents. NEAR ran four and produced
$6.82. That is the owner's own "fast but weak vs slower but productive" case,
occurring in their own book, and it is why the centrepiece metric is NET
PROFIT PER DOLLAR PER DAY rather than trips.

TWO CORRECTIONS TO THE SPEC, both from things this fleet has already measured.

1. WIN RATE IS NOT AN INPUT. It was in the measure list. In a grid it carries
   almost no information: a NORMAL exit only sells above entry, so the rate
   sits near 77-79% by construction. Separating a genuinely 70% coin from a
   77% fleet needs 623 round trips; the whole fleet has done 96. Scoring on
   it would be scoring the exit rule, not the coin.

2. EVERY COMPONENT IS SHRUNK TOWARD THE FLEET PRIOR BY THE COIN'S OWN SAMPLE.
   This is the correction that makes the rest usable. Thirteen of twenty
   coins have 0-3 completed trips. A five-component weighted score over three
   observations ranks noise with great confidence - already demonstrated
   twice on this fleet, most recently when per-coin grid steps scored +70.6%
   and exactly one coin of twenty had enough data to support its own answer.
   Shrinkage is also the mechanical form of the owner's own rule, "never
   increase allocation solely because of one winning trade": one trip moves a
   coin's score by 1/(1+PRIOR_STRENGTH) of the distance to its raw value.

THE IDLE TAX NEEDS NO SHRINKAGE and is the part that is actionable today. It
is a COUNT, and zero is not an estimate. "$2,273 produced no completed round
trip in 60 days" is a fact, not a rate with error bars, which is why the tax
can act while the fine-grained ranking is still gathering evidence.
"""

# How many trips of evidence it takes before a coin's own number outweighs
# the fleet's. At 8, a coin with 8 trips sits halfway between its own
# measurement and the fleet average; one trip moves it about 11% of the way.
PRIOR_STRENGTH = 8.0

# THE WEIGHTS, AND WHY THEY ARE NOT THE ONES IN THE SPEC.
#
# The spec put trip frequency at 30% and net-per-dollar at 25%. Run against
# the live book that promotes LINK-USD - 7 trips for 25 cents, $1.97 per
# $1k per day - into the top tier, over NEAR at $31.79. That is precisely
# the "fast but weak beats slower but productive" outcome the spec itself
# says to avoid, so the weights contradict their own stated goal.
#
# The reason is arithmetic, not taste:
#
#     net/$/day  ==  (trips/$/day)  x  (net/trip)
#
# The centrepiece metric ALREADY CONTAINS frequency and edge as its two
# factors. Scoring all three additively counts frequency twice, so a coin can
# win on churn alone: LINK recycles 55 times per $1k per day at 3.6 cents a
# trip, and on a 30% weight that outvoted NEAR's 18.6 recycles at $1.71.
#
# So net_per_dollar carries the plurality, and frequency and edge are kept at
# reduced weight as DIAGNOSTICS - they explain WHICH way a coin earns its
# score, and a coin that is fast-and-thin versus slow-and-rich reads
# differently in the parts even when the totals are close. The hard guarantee
# is not in the weights at all, it is in tier_a_floor() below.
WEIGHTS = {
    "net_per_dollar": 0.45,      # net profit per $1 per day - THE metric
    "fee_adjusted_edge": 0.20,   # net per trip AFTER fees, not gross
    "trip_frequency": 0.20,      # does capital actually recycle
    "time_to_exit": 0.10,        # faster round trips free capital sooner
    "consistency": 0.05,         # steadiness, and UNKNOWN under a real sample
}

# Below this, "consistency" is not a measurement. A coin with two trips has
# no spread worth reporting, so it inherits the fleet's rather than being
# scored on noise or on a zero.
MIN_TRIPS_FOR_CONSISTENCY = 5

# The tiers, and the share of ACTIVE capital each may hold in total.
TIER_CAPITAL_SHARE = {"A": 0.65, "B": 0.25, "C": 0.10, "D": 0.0}

# No single coin over this share of the fleet, whatever it scores. The
# owner's standing rule, and it binds before any tier target.
MAX_SINGLE_COIN_SHARE = 0.20

# The idle tax. Days since the coin's last COMPLETED round trip, against the
# number of trips it has managed in that window.
IDLE_LADDER = (
    (7,  0, "WARN",   "no completed trip in 7 days"),
    (14, 1, "REVIEW", "at most one completed trip in 14 days"),
    (30, 1, "REDUCE", "at most one completed trip in 30 days"),
    (60, 1, "REMOVE", "at most one completed trip in 60 days"),
)


def shrink(coin_value, coin_trips, fleet_value, strength=PRIOR_STRENGTH):
    """The coin's own number pulled toward the fleet's by its own thinness.

    Zero trips returns the fleet value exactly: a coin that has not traded
    has not disagreed with the fleet, and must not be ranked as if it had.
    """
    n = max(0, int(coin_trips or 0))
    if n == 0:
        return float(fleet_value)
    return (float(coin_value) * n + float(fleet_value) * strength) / (n + strength)


def net_per_dollar_per_day(net_usd, capital_usd, days):
    """The centrepiece. None when it cannot be computed - never 0.0, which
    would rank an unmeasurable coin alongside a genuinely flat one."""
    if not capital_usd or not days or capital_usd <= 0 or days <= 0:
        return None
    return float(net_usd) / float(capital_usd) / float(days)


def trips_per_1k_per_day(trips, capital_usd, days):
    if not capital_usd or not days or capital_usd <= 0 or days <= 0:
        return None
    return float(trips or 0) / (float(capital_usd) / 1000.0) / float(days)


def fee_adjusted_edge(net_usd, trips):
    """Net dollars the coin actually kept per completed round trip. net_usd
    is already after fees everywhere in this codebase, so this divides rather
    than subtracting a fee again - double-charging it is a real bug this
    fleet has shipped before."""
    if not trips:
        return None
    return float(net_usd) / float(trips)


def _norm(value, lo, hi):
    """Into 0..1 against the fleet's own observed range, clamped. Ranking is
    relative to this fleet, not to an absolute scale nobody calibrated."""
    if value is None or hi is None or lo is None or hi <= lo:
        return None
    return max(0.0, min(1.0, (float(value) - lo) / (hi - lo)))


def score_coin(coin, fleet, days):
    """One coin's Capital Velocity Score, 0..1, with its parts exposed.

    coin: {product_id, trips, net_usd, capital_usd, mean_hold_hours,
           pnl_stdev} ; fleet: the same aggregates for the whole book.
    Returns a dict carrying every component AND the raw metric, so a ranking
    can always be traced back to the arithmetic that produced it.
    """
    trips = int(coin.get("trips") or 0)
    cap = coin.get("capital_usd")
    npd = net_per_dollar_per_day(coin.get("net_usd") or 0.0, cap, days)
    tpd = trips_per_1k_per_day(trips, cap, days)
    edge = fee_adjusted_edge(coin.get("net_usd") or 0.0, trips)
    hold = coin.get("mean_hold_hours")

    f_npd = fleet.get("net_per_dollar_per_day") or 0.0
    f_tpd = fleet.get("trips_per_1k_per_day") or 0.0
    f_edge = fleet.get("fee_adjusted_edge") or 0.0
    f_hold = fleet.get("mean_hold_hours")

    # Each component shrunk toward the fleet by THIS coin's sample size.
    s_npd = shrink(npd if npd is not None else f_npd, trips, f_npd)
    s_tpd = shrink(tpd if tpd is not None else f_tpd, trips, f_tpd)
    s_edge = shrink(edge if edge is not None else f_edge, trips, f_edge)
    s_hold = shrink(hold if hold is not None else (f_hold or 0.0), trips, f_hold or 0.0)

    if trips >= MIN_TRIPS_FOR_CONSISTENCY and coin.get("pnl_stdev") is not None:
        # Steadier is better: less spread per dollar of mean edge.
        spread = float(coin["pnl_stdev"])
        consistency = 1.0 / (1.0 + (spread / abs(edge) if edge else spread))
        consistency_known = True
    else:
        consistency = 0.5     # the fleet's own middle - not a reward, not a zero
        consistency_known = False

    parts = {
        "trip_frequency": _norm(s_tpd, fleet.get("tpd_lo"), fleet.get("tpd_hi")),
        "net_per_dollar": _norm(s_npd, fleet.get("npd_lo"), fleet.get("npd_hi")),
        # faster out is better, so the range is inverted
        "time_to_exit": (1.0 - x) if (x := _norm(s_hold, fleet.get("hold_lo"),
                                                 fleet.get("hold_hi"))) is not None else None,
        "fee_adjusted_edge": _norm(s_edge, fleet.get("edge_lo"), fleet.get("edge_hi")),
        "consistency": consistency,
    }
    total = 0.0
    used = 0.0
    for name, w in WEIGHTS.items():
        v = parts.get(name)
        if v is None:
            continue          # an unmeasurable component is dropped, not zeroed
        total += w * v
        used += w
    return {
        "product_id": coin.get("product_id"),
        "score": round(total / used, 4) if used else None,
        "weight_used": round(used, 3),
        "trips": trips,
        "capital_usd": cap,
        "net_usd": coin.get("net_usd"),
        "net_per_dollar_per_day": npd,
        "per_1k_per_day": round(npd * 1000, 4) if npd is not None else None,
        "shrunk_net_per_dollar_per_day": s_npd,
        "consistency_measured": consistency_known,
        "parts": parts,
    }


def idle_verdict(days_since_last_trip, trips_in_window, window_days):
    """The idle tax. A COUNT, so zero is a fact and not an estimate.

    Returns (action, why) with action in OK/WARN/REVIEW/REDUCE/REMOVE. The
    harshest rung whose condition is met wins, so a coin silent for 60 days
    reads REMOVE rather than WARN.
    """
    if days_since_last_trip is None:
        return "UNKNOWN", ("no last-trip timestamp - a gap is not a zero, so this "
                           "coin is not taxed on an unknown")
    worst, why = "OK", "trading normally"
    for days, max_trips, action, text in IDLE_LADDER:
        if float(days_since_last_trip) >= days or (
                window_days and float(window_days) >= days
                and int(trips_in_window or 0) <= max_trips):
            worst, why = action, text
    return worst, why


def tier_a_floor(scored):
    """No coin may sit in tier A while earning less per dollar per day than
    the fleet's median.

    The weights alone cannot guarantee this - any additive score lets a big
    enough frequency term carry a weak coin - so the rule that actually
    enforces the owner's "slower but productive beats fast but weak" is
    stated here as a floor rather than hoped for from a weighting. Returns
    the threshold, or None when there is nothing to take a median of.
    """
    # ONLY coins that have actually completed a trip. Including the silent
    # ones drags the median to the floor: with eight of twenty coins at zero,
    # the median came out at 1.44 per $1k per day and LINK-USD held tier A on
    # 1.97 - the exact coin the floor exists to demote. A coin with no
    # evidence cannot set the bar for the coins that have some.
    vals = sorted(s["net_per_dollar_per_day"] for s in scored
                  if s.get("net_per_dollar_per_day") is not None
                  and int(s.get("trips") or 0) > 0)
    if not vals:
        return None
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def assign_tiers(scored, idle_actions):
    """A / B / C / D from score and idle standing. Idle DEMOTES, never
    promotes: a coin can be dropped for not trading, and no amount of
    silence can raise it."""
    ranked = [s for s in scored if s.get("score") is not None]
    ranked.sort(key=lambda s: -s["score"])
    floor = tier_a_floor(ranked)
    out = []
    n = len(ranked)
    for i, s in enumerate(ranked):
        if s["trips"] == 0:
            tier = "D"
        elif i < max(1, round(n * 0.30)):
            tier = "A"
        elif i < max(2, round(n * 0.65)):
            tier = "B"
        else:
            tier = "C"
        # THE FLOOR, applied before the idle tax: a coin below the fleet's
        # median net-per-dollar-per-day cannot hold tier A however it scored.
        if tier == "A" and floor is not None:
            own = s.get("net_per_dollar_per_day")
            if own is None or own < floor:
                tier = "B"
        action = (idle_actions or {}).get(s["product_id"], "OK")
        if action == "REMOVE":
            tier = "D"
        elif action == "REDUCE" and tier in ("A", "B"):
            tier = "C"
        elif action == "REVIEW" and tier == "A":
            tier = "B"
        out.append({**s, "tier": tier, "idle_action": action})
    return out


def target_allocations(tiered, active_capital_usd,
                       max_single_share=MAX_SINGLE_COIN_SHARE):
    """What each coin SHOULD hold. Returns rows with current vs target.

    Tier budgets split evenly inside the tier rather than by score, because
    the scores separating one coin from the next inside a tier are not
    supported by the samples behind them. The tier is the claim the evidence
    can carry; the ordering within it is not.
    """
    if not active_capital_usd or active_capital_usd <= 0:
        return []
    cap = float(active_capital_usd)
    by_tier = {}
    for row in tiered:
        by_tier.setdefault(row["tier"], []).append(row)
    out = []
    per_tier_each = {}
    for tier in ("A", "B", "C", "D"):
        rows = by_tier.get(tier) or []
        budget = cap * TIER_CAPITAL_SHARE.get(tier, 0.0)
        each = budget / len(rows) if rows else 0.0
        each = min(each, cap * max_single_share)
        # A LOWER TIER MAY NEVER OUT-FUND A HIGHER ONE PER COIN. Without this,
        # a tier with a single member collects its whole budget: live, BCH was
        # alone in C at $0.17 per $1k per day and drew a $743 target - more
        # than any tier B coin - purely for being the only one there. The tier
        # ordering is the claim the evidence supports, so it has to survive
        # how many coins happen to land in each.
        for higher in ("A", "B", "C"):
            if higher == tier:
                break
            if higher in per_tier_each:
                each = min(each, per_tier_each[higher])
        per_tier_each[tier] = each
        for r in rows:
            cur = float(r.get("capital_usd") or 0.0)
            out.append({**r, "target_usd": round(each, 2),
                        "delta_usd": round(each - cur, 2)})
    out.sort(key=lambda r: r["delta_usd"])
    return out

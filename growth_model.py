"""What the fleet actually earns, what caps it, and what compounding does.

THE QUESTION. "See what the potential of it is as well. And make sure
you compound them."

Answering that honestly means three separate measurements that are
normally collapsed into one hand-waved number:

  1. THE EDGE      what one completed round trip nets, measured from
                   real closed trades, not from the grid spacing.
  2. THE CAPITAL   how much of the money is actually working, and what
                   the rest is doing instead.
  3. THE RATE      realised profit over a stated span, and only then
                   what compounding does to it.

WHY THIS MODULE REFUSES THINGS.

 - NO RATE FROM A SHORT WINDOW. /edge-rate already shipped a 0.9519%/day
   figure built from $4.30 over 0.04 days - about 50x the real average -
   and it had to be withdrawn. A rate needs MIN_SPAN_DAYS of real span
   or this returns UNKNOWN. Compounding a wrong rate is how a modelling
   error becomes a life plan.
 - NO CEILING THAT ASSUMES CAUSATION. Idle capital earning the working
   capital's rate is a CEILING, not a forecast: capital may be idle
   precisely because its branch found nothing worth buying, and the same
   dollars in a worse setup would not earn the same. The payload says so
   in the field name and in the caveat.
 - NO BLENDING OF REALISED AND UNREALISED. They are different kinds of
   number. Realised is banked; unrealised is a price quote that moves
   while you read it. Both are reported; neither is added to the other.
 - NO PROJECTION PAST WHAT THE EDGE SUPPORTS. The compounding table is
   arithmetic on a measured rate, labelled as such. It is not a promise,
   and the payload says that in words rather than leaving it implied.
"""

from statistics import median

# A rate needs this much real span behind it. Same discipline as
# /edge-rate's MIN_SPAN_DAYS, for the same reason.
MIN_SPAN_DAYS = 7.0

# Months projected in the compounding table.
PROJECTION_MONTHS = (1, 3, 6, 12, 24, 36)


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def measure_edge(trades):
    """What one completed round trip really nets.

    trades: dicts with entry_price, qty, pnl. Anything unpriceable is
    dropped and counted - a trade whose notional cannot be computed has
    no percentage, and calling it 0% would drag the median toward a
    number nothing produced.
    """
    nets, pnls, notionals, dropped = [], [], [], 0
    wins = 0
    for t in (trades or []):
        ep, q, pnl = _num(t.get("entry_price")), _num(t.get("qty")), _num(t.get("pnl"))
        if ep is None or q is None or pnl is None or ep <= 0 or q <= 0:
            dropped += 1
            continue
        notional = ep * q
        nets.append(pnl / notional * 100.0)
        pnls.append(pnl)
        notionals.append(notional)
        if pnl > 0:
            wins += 1
    if not nets:
        return {"readable": False,
                "reason": "no trade had a computable notional",
                "dropped": dropped}
    return {
        "readable": True,
        "round_trips": len(nets),
        "dropped_unpriceable": dropped,
        "median_net_pct": round(median(nets), 4),
        "mean_net_pct": round(sum(nets) / len(nets), 4),
        "win_rate_pct": round(wins / len(nets) * 100.0, 1),
        "median_notional_usd": round(median(notionals), 2),
        "total_pnl_usd": round(sum(pnls), 2),
        "cumulative_notional_usd": round(sum(notionals), 2),
        "blended_net_pct": round(sum(pnls) / sum(notionals) * 100.0, 4),
    }


def measure_capital(branches, free_cash_usd=0.0, concentration_top_n=2):
    """Where the money is: working, idle in a branch, or concentrated.

    branches: dicts with product_id, allocated_usd, num_levels, and
    `slices` (each with entry_price and qty). Deployed capital is summed
    from the slices themselves rather than inferred from allocation -
    allocation is an intention, a slice is a position.
    """
    rows, unreadable = [], []
    for b in (branches or []):
        alloc = _num(b.get("allocated_usd"))
        if alloc is None:
            unreadable.append(b.get("product_id"))
            continue
        deployed = 0.0
        for s in (b.get("slices") or []):
            ep, q = _num(s.get("entry_price")), _num(s.get("qty"))
            if ep is not None and q is not None:
                deployed += ep * q
        rows.append({
            "product_id": b.get("product_id"),
            "allocated_usd": alloc,
            "deployed_usd": round(deployed, 2),
            "idle_usd": round(max(alloc - deployed, 0.0), 2),
            "open_slices": len(b.get("slices") or []),
            "num_levels": b.get("num_levels"),
            "unrealized_usd": _num(b.get("total_unrealized_net_usd")) or 0.0,
        })
    if not rows:
        return {"readable": False, "reason": "no branch had a readable allocation"}

    allocated = sum(r["allocated_usd"] for r in rows)
    deployed = sum(r["deployed_usd"] for r in rows)
    idle_in_branch = sum(r["idle_usd"] for r in rows)
    free = _num(free_cash_usd) or 0.0

    by_size = sorted(rows, key=lambda r: -r["allocated_usd"])
    top = by_size[:concentration_top_n]
    rest = by_size[concentration_top_n:]
    top_usd = sum(r["allocated_usd"] for r in top)
    top_unreal = sum(r["unrealized_usd"] for r in top)
    all_unreal = sum(r["unrealized_usd"] for r in rows)

    # SLICES OVER LEVELS IS NOT AUTOMATICALLY AN ANOMALY.
    #
    # The first version of this flagged every branch with more open
    # slices than rungs and called it structural. Six branches lit up,
    # and reading crypto_grid_bot showed all of it was DELIBERATE:
    # coin_adoption_worker pins num_levels to the slice count on an
    # ADOPTED branch precisely so the grid cannot double down on a
    # position it never chose to buy. The comment there names ZEC and
    # says raising the level count without changing the sizing "is what
    # would have let ZEC spend 68.5% of the wallet averaging down its
    # own worst position".
    #
    # So the check has to ask the question that actually matters: did
    # this branch BUY its way past its rungs, or was it handed coin it
    # never paid for? An adopted slice carries no entry fee - that is
    # how crypto_grid_bot.branch_is_adopted_only tells them apart, and
    # the status payload exposes it per slice as `adopted`.
    #
    # A rule that fires on code it has no opinion about is a rule nobody
    # can act on.
    over = []
    for b in (branches or []):
        pid = b.get("product_id")
        row = next((r for r in rows if r["product_id"] == pid), None)
        if row is None or not row["num_levels"]:
            continue
        if row["open_slices"] <= int(row["num_levels"]):
            continue
        sl = b.get("slices") or []
        adopted = [s for s in sl if s.get("adopted")]
        entry = dict(row)
        entry["adopted_slices"] = len(adopted)
        entry["all_adopted"] = bool(sl) and len(adopted) == len(sl)
        entry["deliberate"] = entry["all_adopted"]
        entry["why"] = (
            "every open slice was adopted - the adoption worker pins "
            "num_levels to the slice count on purpose so the grid cannot "
            "average down coin it never chose. Not an anomaly."
            if entry["all_adopted"] else
            "this branch holds MORE slices than rungs and not all of them "
            "were adopted, so at least one was BOUGHT past the cap. That is "
            "the case worth looking at.")
        over.append(entry)

    return {
        "readable": True,
        "branches": len(rows),
        "allocated_usd": round(allocated, 2),
        "deployed_usd": round(deployed, 2),
        "idle_in_branch_usd": round(idle_in_branch, 2),
        "free_cash_usd": round(free, 2),
        "total_capital_usd": round(allocated + free, 2),
        "not_working_usd": round(idle_in_branch + free, 2),
        "not_working_pct": round((idle_in_branch + free) / (allocated + free) * 100.0, 1)
        if (allocated + free) else None,
        "unrealized_usd": round(all_unreal, 2),
        "concentration": {
            "top_n": concentration_top_n,
            "products": [r["product_id"] for r in top],
            "allocated_usd": round(top_usd, 2),
            "share_pct": round(top_usd / allocated * 100.0, 1) if allocated else None,
            "unrealized_usd": round(top_unreal, 2),
            "share_of_unrealized_pct": round(top_unreal / all_unreal * 100.0, 1)
            if all_unreal else None,
            "rest_allocated_usd": round(sum(r["allocated_usd"] for r in rest), 2),
            "rest_unrealized_usd": round(sum(r["unrealized_usd"] for r in rest), 2),
        },
        "idle_branches": sorted(
            [r for r in rows if r["idle_usd"] > 20],
            key=lambda r: -r["idle_usd"]),
        "slices_over_levels": over,
        "slices_over_levels_unexplained": [r for r in over if not r["deliberate"]],
        "unreadable_branches": unreadable or None,
    }


def project(realised_usd, capital_usd, span_days, months=PROJECTION_MONTHS,
            min_span_days=MIN_SPAN_DAYS):
    """The measured rate, and what compounding does to it. Or UNKNOWN."""
    r, c, d = _num(realised_usd), _num(capital_usd), _num(span_days)
    if r is None or c is None or d is None or c <= 0 or d <= 0:
        return {"readable": False,
                "reason": "realised, capital and span must all be readable and positive"}
    if d < min_span_days:
        return {
            "readable": False,
            "verdict": "UNKNOWN",
            "reason": (f"only {d:.1f} days of span; a rate needs at least "
                       f"{min_span_days:.0f}. Multiplying a short window out to a "
                       f"month or a year is how a $4.30 profit over an hour became "
                       f"a 0.95%/day claim that had to be withdrawn."),
        }
    daily = r / c / d
    monthly = daily * 30.0
    table = []
    for m in months:
        table.append({
            "months": m,
            "value_usd": round(c * ((1.0 + monthly) ** m), 2),
            "gain_usd": round(c * ((1.0 + monthly) ** m) - c, 2),
        })
    return {
        "readable": True,
        "verdict": "MEASURED",
        "span_days": round(d, 2),
        "realised_usd": round(r, 2),
        "capital_usd": round(c, 2),
        "daily_pct": round(daily * 100.0, 5),
        "monthly_pct": round(monthly * 100.0, 4),
        "annual_pct_compounded": round((((1.0 + monthly) ** 12) - 1.0) * 100.0, 2),
        "compounding": table,
        "this_is_arithmetic_not_a_forecast": (
            "These figures compound the rate actually measured over the span "
            "above. They assume it repeats, which nothing guarantees. Realised "
            "profit is banked; unrealised is not in this number at all."),
    }


def ceiling_if_idle_worked(capital, working_monthly_pct):
    """A CEILING, not a forecast.

    Idle capital earning the working capital's rate is the best case,
    and the reason it is only a ceiling is in the name of the caveat:
    capital can be idle precisely because its branch found nothing worth
    buying. The same dollars in a worse setup do not earn the same.
    """
    if not capital.get("readable"):
        return {"readable": False, "reason": "capital was not readable"}
    rate = _num(working_monthly_pct)
    idle = capital.get("not_working_usd")
    if rate is None or idle is None:
        return {"readable": False, "reason": "rate or idle capital unreadable"}
    return {
        "readable": True,
        "idle_capital_usd": idle,
        "working_monthly_pct": round(rate, 4),
        "extra_monthly_usd_if_it_worked": round(idle * rate / 100.0, 2),
        "is_a_ceiling_not_a_forecast": (
            "Capital can be idle BECAUSE its branch found nothing worth buying. "
            "The same dollars deployed into a worse setup would not earn the "
            "working rate. Treat this as the top of the range, never the middle."),
    }

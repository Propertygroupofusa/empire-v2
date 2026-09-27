"""Whether the numbers are actually moving, measured between readings.

WHY THIS EXISTS

Every figure on this dashboard was a point in time. "Is it growing?"
cannot be answered from a point - only from two of them - so the KPI
panel could give a perfect diagnosis of WHY the profit was small and
still not say whether anything had changed since yesterday.

The account owner asked to watch the numbers improve in real time.
Nothing was writing down where they had been, so there was no curve to
watch. This is the module that reads the series and says what moved.

WHAT IT REFUSES TO DO

It will not draw a trend through a gap. A snapshot whose census failed
stores NULL, not zero, and a delta computed against a NULL is withheld -
because a chart that renders an unread account as $0.00 invents a crash
that never happened, and someone acts on that.

It will not extrapolate. Two readings twenty minutes apart produce a
twenty-minute delta, never a daily rate dressed up from one. Rates need
a window this module insists on rather than assumes.

And it separates the two things that both look like "it went up":
EARNED (realised P&L, which only rises) from PLACED (capital moved into
branches, which is arrangement, not income). The header once showed a
balance in green with a plus sign and it rose the day the trimmer sold
$882.68 of holdings. That was liquidation rendered as profit, and
keeping these apart is how it does not happen again.
"""
from __future__ import annotations

from datetime import datetime, timedelta

# A delta needs enough clock to mean anything. Two readings a minute
# apart describe noise and rounding, not a trend.
MIN_WINDOW_MINUTES = 10.0

# Below this many readings there is no series, only points.
MIN_POINTS_FOR_TREND = 3

# Fields that only ever accumulate. A fall in one of these is either a
# ledger being rewritten or a reading that failed - both worth flagging,
# neither worth charting as a loss.
MONOTONIC_FIELDS = ("trades", "net_usd")

# What a reading carries. Kept here so the worker, the endpoint and the
# chart cannot drift apart on field names.
FIELDS = (
    "trades", "net_usd", "net_edge_per_trade_usd", "profit_factor", "win_rate_pct",
    "allocated_usd", "claimed_usd", "free_cash_usd", "account_total_usd",
    "coin_usd", "cash_usd",
    "outside_any_branch_pct", "idle_capital_pct", "capital_velocity",
    "branch_count", "open_slices",
)

# Fields that come from the census rather than the KPI payload, and so are
# passed in explicitly. Named here so a test can hold the boundary.
NON_KPI_FIELDS = ("claimed_usd", "branch_count", "open_slices", "coin_usd", "cash_usd")


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (f != f or f in (float("inf"), float("-inf"))) else f


def _at(row):
    v = row.get("captured_at") if hasattr(row, "get") else None
    if isinstance(v, datetime):
        return v
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return None
    return None


def rows(snapshots):
    """Readings with a usable timestamp, oldest first."""
    out = []
    for s in snapshots or ():
        if not hasattr(s, "get"):
            continue
        at = _at(s)
        if at is None:
            continue
        r = {"captured_at": at, "bottleneck": s.get("bottleneck"), "note": s.get("note")}
        for f in FIELDS:
            r[f] = _num(s.get(f))
        out.append(r)
    out.sort(key=lambda r: r["captured_at"])
    return out


def from_kpis(kpis, *, claimed_usd=None, branch_count=None, open_slices=None,
              coin_usd=None, cash_usd=None,
              bottleneck=None, note=None, captured_at=None):
    """One reading, built from a KPI payload. The worker's only shaping
    step, kept here so a renamed KPI key breaks a test rather than
    silently writing a column of NULLs for a month."""
    k = kpis or {}
    row = {"captured_at": captured_at or datetime.utcnow(),
           "bottleneck": bottleneck, "note": note}
    for f in FIELDS:
        row[f] = _num(k.get(f))
    if claimed_usd is not None:
        row["claimed_usd"] = _num(claimed_usd)
    if branch_count is not None:
        row["branch_count"] = _num(branch_count)
    if open_slices is not None:
        row["open_slices"] = _num(open_slices)
    if coin_usd is not None:
        row["coin_usd"] = _num(coin_usd)
    if cash_usd is not None:
        row["cash_usd"] = _num(cash_usd)
    return row


def delta(snapshots, field, *, hours=24.0, min_window_minutes=MIN_WINDOW_MINUTES):
    """How much `field` moved over the last `hours`, and over what window.

    Anchored on the OLDEST reading inside the window, not on a wall-clock
    time that may have no reading near it - a delta is between two things
    that were actually measured.
    """
    rs = rows(snapshots)
    usable = [r for r in rs if r.get(field) is not None]
    if len(usable) < 2:
        return {"field": field, "change": None, "from": None, "to": None,
                "window_minutes": None,
                "why": ("one reading" if len(usable) == 1 else "no readings")
                       + f" carries {field}, and a change needs two"}

    last = usable[-1]
    cutoff = last["captured_at"] - timedelta(hours=float(hours))
    inside = [r for r in usable if r["captured_at"] >= cutoff]
    first = inside[0] if len(inside) >= 2 else usable[0]

    window = (last["captured_at"] - first["captured_at"]).total_seconds() / 60.0
    if window < min_window_minutes:
        return {"field": field, "change": None,
                "from": first[field], "to": last[field],
                "window_minutes": round(window, 2),
                "why": (f"only {window:.1f} minutes between the two readings; under "
                        f"{min_window_minutes:.0f} that is noise and rounding, not a move")}

    change = round(last[field] - first[field], 6)
    per_hour = round(change / (window / 60.0), 6) if window else None
    return {
        "field": field,
        "change": change,
        "from": first[field],
        "to": last[field],
        "window_minutes": round(window, 2),
        "per_hour": per_hour,
        "from_at": first["captured_at"].isoformat(),
        "to_at": last["captured_at"].isoformat(),
        "why": None,
    }


def integrity(snapshots):
    """Readings that fell in a field that cannot fall.

    Realised P&L and the trade count only accumulate. A drop is a ledger
    being rewritten, a database being restored, or a failed read stored
    as a number - and each of those is a reason to distrust a chart
    rather than to draw a loss on it.
    """
    rs = rows(snapshots)
    problems = []
    for f in MONOTONIC_FIELDS:
        prev = None
        for r in rs:
            v = r.get(f)
            if v is None:
                continue
            if prev is not None and v < prev - 1e-9:
                problems.append({"field": f, "at": r["captured_at"].isoformat(),
                                 "fell_from": prev, "to": v})
            prev = max(prev, v) if prev is not None else v
    gaps = sum(1 for r in rs for f in FIELDS if r.get(f) is None)
    return {"monotonic_breaks": problems, "null_fields": gaps,
            "trustworthy": not problems}


def summarise(snapshots, *, hours=24.0):
    """The whole series in one answer: what was earned, what was placed,
    and whether the reason it is stuck ever changed.

    EARNED and PLACED are reported apart on purpose. Both look like "it
    went up" and only one of them is income.
    """
    rs = rows(snapshots)
    if not rs:
        return {"available": False,
                "reason": "no readings yet - the first snapshot writes one",
                "points": 0}

    first, last = rs[0], rs[-1]
    span_h = (last["captured_at"] - first["captured_at"]).total_seconds() / 3600.0

    earned = delta(snapshots, "net_usd", hours=hours)
    placed = delta(snapshots, "allocated_usd", hours=hours)
    coin = delta(snapshots, "coin_usd", hours=hours)
    outside = delta(snapshots, "outside_any_branch_pct", hours=hours)
    idle = delta(snapshots, "idle_capital_pct", hours=hours)
    trades = delta(snapshots, "trades", hours=hours)

    causes = [r["bottleneck"] for r in rs if r.get("bottleneck")]
    cause_changed = bool(causes) and len(set(causes)) > 1

    moving = [d for d in (earned, placed) if d["change"] is not None and d["change"] != 0]

    return {
        "available": True,
        "points": len(rs),
        "first_at": first["captured_at"].isoformat(),
        "last_at": last["captured_at"].isoformat(),
        "span_hours": round(span_h, 2),
        "window_hours": hours,

        # The two that must never be added together.
        "earned": earned,
        "placed": placed,

        # Reported, never added to "earned". Coin rising is the market
        # moving, not the bots working, and the two have been confused on
        # this dashboard before.
        "coin_usd": coin,
        "outside_any_branch_pct": outside,
        "idle_capital_pct": idle,
        "trades": trades,

        "bottleneck_now": last.get("bottleneck"),
        "bottleneck_first": rs[0].get("bottleneck"),
        "bottleneck_changed": cause_changed,

        "integrity": integrity(snapshots),
        "has_a_trend": len(rs) >= MIN_POINTS_FOR_TREND,
        "is_moving": bool(moving),
        "series": [
            {"at": r["captured_at"].isoformat(),
             **{f: r[f] for f in FIELDS},
             "bottleneck": r.get("bottleneck")}
            for r in rs
        ],
        "headline": _headline(rs, earned, placed, len(rs)),
    }


def _headline(rs, earned, placed, points):
    if points < 2:
        return (f"{points} reading so far. A second one is what makes this a curve - "
                f"nothing can be said about movement from a single point.")
    if earned["change"] is None and placed["change"] is None:
        return (f"{points} readings, and neither what was earned nor what was placed "
                f"could be compared: {earned['why'] or placed['why']}.")
    bits = []
    if earned["change"] is not None:
        bits.append(f"earned {earned['change']:+,.2f} over {earned['window_minutes']/60:.1f}h")
    if placed["change"] is not None:
        bits.append(f"capital placed {placed['change']:+,.2f}")
    tail = ""
    if earned["change"] == 0 and (placed["change"] or 0) == 0:
        tail = (" - nothing moved in either, which is the honest reading and not a "
                "rendering fault")
    return f"{points} readings: " + ", ".join(bits) + tail + "."

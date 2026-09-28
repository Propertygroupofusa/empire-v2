"""The fleet's edge as a rate, against a denominator that is stated.

WHY THIS EXISTS

At 2026-09-28T10:23Z the fleet had realised $50.04 over 26.17 days.
Divided by the working capital of that instant, $1,516.21, that came to
0.1183%/day, and it was reported as "inside the professional band".
Twenty-two minutes later the same arithmetic gave 0.0509%/day - XRP's
$2,240.54 crossed back over the -1% line and the parked share went 81.3%
to 53.6% on price alone, with nothing traded in between.

Neither figure was a rate. Both were a long measurement divided by one
instant's denominator, and the instant supplied the answer.

Nothing recorded working capital over time, so the error was invisible
in stored data - it surfaced only because the same sum was run twice
twenty minutes apart. This module fixes the shape: record the
denominator beside the numerator, and divide by the denominator's
TIME-WEIGHTED AVERAGE across the same window.

WHAT IT REFUSES TO DO

  * One snapshot is never a rate. Two readings at the same instant are
    not either.
  * An unreadable or zero basis is UNKNOWN, never 0.0 and never an
    infinite rate.
  * Cumulative realised cannot fall. If it does, the window spans a
    books reset and the difference is not a loss, so the answer is
    UNKNOWN rather than a negative rate.
  * The basis is named in every answer. "0.05%/day" means nothing
    without the book it was measured against - that ambiguity is the
    whole reason this file exists.

A flat window IS a measurement: nothing traded is 0.0%/day, not UNKNOWN.
"""
from __future__ import annotations

from datetime import datetime, timezone

OK = "OK"
UNKNOWN = "UNKNOWN"

#: The books a rate may be measured against. A name not in here is
#: refused rather than guessed at.
BASES = {
    "account": ("account_usd", "the whole account, cash included"),
    "allocated": ("allocated_usd", "capital allocated to grid branches"),
    "working": ("working_usd", "working capital, the allocated dollars free to trade"),
}

#: One snapshot an hour is enough to average a denominator that moves on
#: price, and small enough that a year of them is ~8,760 rows.
SNAPSHOT_INTERVAL_SECONDS = 3600.0

#: The shortest window that may be reported as a rate PER DAY.
#:
#: FOUND IN THIS MODULE'S OWN FIRST LIVE OUTPUT. Two snapshots an hour
#: apart gave "$4.30 realised over 0.04 day(s) = 0.9519%/day" on the
#: account and 2.6569%/day on working capital - roughly fifty times what
#: the same fleet has averaged over 26 days.
#:
#: The grid trades in bursts. Three round trips landed inside one
#: morning and then nothing for hours, so multiplying any single hour by
#: 24 says nothing about a day. This module exists because a long
#: measurement was divided by an instantaneous denominator; extrapolating
#: a short measurement to a long period is that same error reversed, and
#: it shipped inside the fix for the first one.
#:
#: A day, because "per day" is what is being claimed.
MIN_SPAN_DAYS = 1.0


def _num(v):
    """A finite float, or None. None means UNREADABLE and never 0.0."""
    try:
        if v is None:
            return None
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _at(row):
    v = (row or {}).get("at")
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, str):
        try:
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _v(status, basis, detail, **extra):
    out = {"status": status, "basis": basis, "pct_per_day": None, "detail": detail}
    out.update(extra)
    return out


def _too_short(days, delta, label):
    """Why a short window is not a daily rate, and how much longer it needs."""
    need_h = max(0.0, (MIN_SPAN_DAYS - days) * 24.0)
    return (f"${delta:,.2f} realised across {days * 24:.1f}h of {label}. That is real, "
            f"but it is not a rate PER DAY: this fleet trades in bursts, and "
            f"multiplying one short window by {1.0 / max(days, 1e-9):.0f} would claim "
            f"a daily figure no day has produced. Needs {need_h:.1f} more hours before "
            f"a rate is reported.")


def rate_between(a, b, basis="account"):
    """The edge rate between two snapshots, against the average basis."""
    if basis not in BASES:
        return _v(UNKNOWN, basis,
                  f"{basis!r} is not a book this can measure against "
                  f"({', '.join(sorted(BASES))}) - a rate against an unnamed "
                  f"denominator is the error this module exists to stop")
    field, label = BASES[basis]

    ta, tb = _at(a), _at(b)
    if ta is None or tb is None:
        return _v(UNKNOWN, basis, "a snapshot carries no readable timestamp")
    if tb < ta:
        a, b, ta, tb = b, a, tb, ta
    days = (tb - ta).total_seconds() / 86400.0
    if days <= 0:
        return _v(UNKNOWN, basis,
                  "both snapshots are from the same instant, so no time passed "
                  "to measure a rate over")

    ra, rb = _num((a or {}).get("realized_usd")), _num((b or {}).get("realized_usd"))
    if ra is None or rb is None:
        return _v(UNKNOWN, basis, "realised P&L is unreadable on a snapshot")
    if rb < ra:
        return _v(UNKNOWN, basis,
                  f"cumulative realised fell from ${ra:,.2f} to ${rb:,.2f}, which it "
                  f"cannot do - the window spans a books reset, and the difference "
                  f"is not a loss", realized_delta_usd=round(rb - ra, 2), days=round(days, 4))

    ca, cb = _num((a or {}).get(field)), _num((b or {}).get(field))
    if ca is None or cb is None:
        return _v(UNKNOWN, basis,
                  f"{label} is unreadable on a snapshot, so there is no denominator. "
                  f"Unreadable is not zero.")
    avg = (ca + cb) / 2.0
    if avg <= 0:
        return _v(UNKNOWN, basis,
                  f"{label} averages ${avg:,.2f} across the window - a rate against "
                  f"nothing is not a large rate, it is no rate")

    delta = rb - ra
    if days < MIN_SPAN_DAYS:
        return _v(UNKNOWN, basis, _too_short(days, delta, label),
                  span_days=round(days, 4), realized_delta_usd=round(delta, 2),
                  basis_usd=round(avg, 2))
    pct = (delta / avg) / days * 100.0
    return {"status": OK, "basis": basis, "pct_per_day": pct,
            "basis_usd": round(avg, 2), "realized_delta_usd": round(delta, 2),
            "days": round(days, 4), "span_days": round(days, 4),
            "detail": (f"${delta:,.2f} over {days:.2f} day(s) against ${avg:,.2f} of "
                       f"{label}, averaged across the window = {pct:.4f}%/day")}


def series_rate(snapshots, basis="account"):
    """The edge rate across a whole series, first snapshot to last.

    The denominator is time-weighted: an interval that lasted a day
    counts for a day, so a single reading taken while capital briefly
    tripled cannot dominate the average the way a plain mean of endpoints
    would.
    """
    if basis not in BASES:
        return _v(UNKNOWN, basis,
                  f"{basis!r} is not a book this can measure against "
                  f"({', '.join(sorted(BASES))})")
    field, label = BASES[basis]

    rows = [r for r in (snapshots or ()) if _at(r) is not None]
    rows.sort(key=_at)
    if len(rows) < 2:
        return _v(UNKNOWN, basis,
                  f"{len(rows)} snapshot(s) - a rate needs at least two, taken at "
                  f"different times. One snapshot divided by itself is the mistake "
                  f"this module exists to stop.", snapshots=len(rows))

    first, last = rows[0], rows[-1]
    days = (_at(last) - _at(first)).total_seconds() / 86400.0
    if days <= 0:
        return _v(UNKNOWN, basis, "every snapshot is from the same instant",
                  snapshots=len(rows))

    ra, rb = _num(first.get("realized_usd")), _num(last.get("realized_usd"))
    if ra is None or rb is None:
        return _v(UNKNOWN, basis, "realised P&L is unreadable at an end of the series",
                  snapshots=len(rows))
    if rb < ra:
        return _v(UNKNOWN, basis,
                  f"cumulative realised fell from ${ra:,.2f} to ${rb:,.2f} - the series "
                  f"spans a books reset", snapshots=len(rows))

    # Trapezoid, weighted by how long each interval lasted.
    weighted, span = 0.0, 0.0
    for x, y in zip(rows, rows[1:]):
        cx, cy = _num(x.get(field)), _num(y.get(field))
        if cx is None or cy is None:
            continue
        h = (_at(y) - _at(x)).total_seconds()
        if h <= 0:
            continue
        weighted += (cx + cy) / 2.0 * h
        span += h
    if span <= 0:
        return _v(UNKNOWN, basis,
                  f"{label} could not be read across any interval in this series",
                  snapshots=len(rows))
    avg = weighted / span
    if avg <= 0:
        return _v(UNKNOWN, basis,
                  f"{label} averages ${avg:,.2f} across the series", snapshots=len(rows))

    delta = rb - ra
    if days < MIN_SPAN_DAYS:
        return _v(UNKNOWN, basis, _too_short(days, delta, label),
                  span_days=round(days, 4), realized_delta_usd=round(delta, 2),
                  basis_usd=round(avg, 2), snapshots=len(rows),
                  first_at=_at(first).isoformat(), last_at=_at(last).isoformat())
    pct = (delta / avg) / days * 100.0
    return {"status": OK, "basis": basis, "pct_per_day": pct,
            "basis_usd": round(avg, 2), "realized_delta_usd": round(delta, 2),
            "days": round(days, 4), "span_days": round(days, 4),
            "snapshots": len(rows),
            "first_at": _at(first).isoformat(), "last_at": _at(last).isoformat(),
            "detail": (f"${delta:,.2f} realised over {days:.2f} day(s) against "
                       f"${avg:,.2f} of {label}, time-weighted across "
                       f"{len(rows)} snapshot(s) = {pct:.4f}%/day")}

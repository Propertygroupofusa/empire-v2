"""Turning the watch into an alarm: what to alert on, and when to stop.

WHY THIS IS NOT JUST "IF BREACHED, SEND"

A watch you have to look at is decorative. But the naive producer - send an
alert whenever a coin is below its level - fires every cycle for as long as
the condition holds. PEPE has been below its level for days; that is 96
identical messages a day. The person being alerted learns to ignore the
channel, and then the alarm has failed in the worst possible way: silently,
while still feeling like coverage.

So this alerts on TRANSITIONS, not conditions. A coin becoming breached is
news. A coin still being breached is not. A coin RECOVERING is news again,
because silence after an alarm is ambiguous - it reads the same as a broken
sender.

WHAT ELSE EARNS AN ALERT

Going blind. If coverage falls because a data feed died, the positions that
dropped out are UNWATCHED, not calm, and nothing else in the system will
say so. That is the failure this codebase keeps rediscovering - a rate
limit written down as "no data", a census reporting $79.30 for an $11,292
account - and it is exactly the kind of thing an alarm should be for.

PURE FUNCTIONS. Nothing here touches a database or a network. It decides
what alerts SHOULD exist given a watch payload and the last known state;
the worker does the writing. That split is what lets the interesting part
be tested without a database.
"""
from __future__ import annotations

import hashlib

MAX_ATTEMPTS = 6
# Exponential, capped. A dead webhook should not be retried every second
# for a week, and it should not be given up on after two tries either.
BACKOFF_SECONDS = (30, 120, 600, 1800, 3600, 7200)

# Below this, a breach is true and not worth a notification. ALEO fell
# 43.6% and that is $5.97; waking someone for it teaches them the channel
# is noise.
MIN_ALERT_USD = 25.0

# Coverage falling by more than this many points between cycles means the
# desk lost a feed, not that the market moved.
COVERAGE_DROP_POINTS = 5.0

CRITICAL, HIGH, INFO = "CRITICAL", "HIGH", "INFO"


def _f(x, default=None):
    try:
        v = float(x)
        return v if v == v else default
    except (TypeError, ValueError):
        return default


def dedupe_key(kind: str, asset, marker: str) -> str:
    """Stable identity for one alert, so a retry cannot double-send.

    The marker is what makes an alert DISTINCT from the previous one of the
    same kind for the same asset - normally the transition it represents.
    Hashing keeps the column bounded when the marker is long.
    """
    raw = f"{kind}|{asset or '-'}|{marker}"
    return f"{kind}:{asset or '-'}:{hashlib.sha256(raw.encode()).hexdigest()[:16]}"


def backoff_seconds(attempts: int) -> int:
    if attempts <= 0:
        return BACKOFF_SECONDS[0]
    return BACKOFF_SECONDS[min(attempts - 1, len(BACKOFF_SECONDS) - 1)]


def plan(watch: dict, last_state: dict, last_coverage=None) -> list:
    """Which alerts SHOULD exist, given this watch and what we saw before.

    last_state maps asset -> the status it was last seen in. An asset
    missing from it is NEW: its first sighting is reported only if it is
    already in trouble, because announcing "BTC is fine" on first boot is
    noise, while quietly discovering a breached position and saying nothing
    is the failure this whole thing exists to prevent.
    """
    out = []
    rows = watch.get("rows") or []

    for r in rows:
        asset = r.get("asset")
        now = r.get("status")
        usd = _f(r.get("usd"), 0.0) or 0.0
        was = (last_state or {}).get(asset)

        if now == was:
            continue                      # still true is not news

        marker = f"{was or 'NEW'}->{now}"

        if now == "BREACHED" and usd >= MIN_ALERT_USD:
            out.append({
                "kind": "BREACH", "asset": asset, "severity": CRITICAL,
                "message": (f"{asset} broke its level - "
                            f"${usd:,.2f} exposed"),
                "detail": (f"{abs(_f(r.get('pct_from_peak'), 0) or 0):.1f}% off its peak, "
                           f"past a {(_f(r.get('stop_pct'), 0) or 0) * 100:.2f}% stop. "
                           f"This is an ALERT LEVEL, not an order - nothing has sold."),
                "dedupe_key": dedupe_key("BREACH", asset, marker),
            })
        elif was == "BREACHED" and now in ("OK", "NEAR_STOP") and usd >= MIN_ALERT_USD:
            out.append({
                "kind": "RECOVERY", "asset": asset, "severity": INFO,
                "message": f"{asset} is back above its level",
                "detail": (f"Now {_f(r.get('pct_to_stop'), 0) or 0:.2f}% above it. "
                           f"${usd:,.2f}."),
                "dedupe_key": dedupe_key("RECOVERY", asset, marker),
            })
        elif now in ("UNPRICED", "NO_VOLATILITY_DATA") and was in (
                "OK", "NEAR_STOP", "BREACHED") and usd >= MIN_ALERT_USD:
            # A position that HAD a level and now does not is not calm, it
            # is unwatched, and nothing else reports that.
            out.append({
                "kind": "BLIND", "asset": asset, "severity": HIGH,
                "message": f"{asset} dropped out of coverage",
                "detail": (f"It had a level and no longer does ({now}). "
                           f"${usd:,.2f} is now UNWATCHED, not calm."),
                "dedupe_key": dedupe_key("BLIND", asset, marker),
            })

    # --- the desk going blind as a whole -------------------------------
    cov = _f(watch.get("covered_share_pct"))
    prev = _f(last_coverage)
    if cov is not None and prev is not None and (prev - cov) >= COVERAGE_DROP_POINTS:
        out.append({
            "kind": "BLIND", "asset": None, "severity": HIGH,
            "message": f"Coverage fell {prev - cov:.1f} points, to {cov:.1f}%",
            "detail": ("The holdings did not change that fast - a data feed "
                       "died. Positions that dropped out are UNWATCHED, not "
                       "calm."),
            "dedupe_key": dedupe_key("BLIND", None, f"{prev:.1f}->{cov:.1f}"),
        })

    return out


def next_state(watch: dict) -> dict:
    """The status of every asset in this watch, for the next comparison."""
    return {r.get("asset"): r.get("status")
            for r in (watch.get("rows") or []) if r.get("asset")}

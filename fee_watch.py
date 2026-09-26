"""What trading is costing, measured from the exchange, on a clock.

WHY

On 2026-09-06 this account paid $1,426.39 in commission in a single day -
12.8% of everything in it - across 712 event-contract fills. Nobody noticed
for twenty days. It was not hidden: Coinbase charged it per fill and
reported it per fill. Nothing was looking.

Every strategy question in this repository is worth fractions of a percent.
The measured grid alpha is -0.15%. The stop-loss defect is -8%. Fees over
the last 23 days were 13.1% of the account, a 207% annual pace. The largest
controllable number here is the one with no monitor on it.

WHAT IT CANNOT DO

Stop it. The spending came from a process outside this codebase, holding
its own Coinbase credentials, and no guard in this repository can reach it.
The write guard protects THIS server's endpoints; it has no authority over
a script on someone's desktop.

So this does the thing that is actually available: it reads the venue's own
commission, on a rolling window, against the account's own size, and says
plainly when the pace is destructive. Detection, not prevention - and named
that way so nobody mistakes it for a brake.

THRESHOLDS

Expressed as a share of the ACCOUNT, not as dollars. $50 of fees is trivial
on $100k and ruinous on $500, and this account has been both an $11k and a
$572 account inside one morning depending on which number you believed.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

# A day that costs this share of the account is not a bad day, it is a
# different activity. Sept 6 was 12.8%.
DAY_ALARM_PCT = float(os.getenv("FEE_WATCH_DAY_ALARM_PCT", "1.0"))
DAY_WARN_PCT = float(os.getenv("FEE_WATCH_DAY_WARN_PCT", "0.25"))
WEEK_ALARM_PCT = float(os.getenv("FEE_WATCH_WEEK_ALARM_PCT", "3.0"))
WEEK_WARN_PCT = float(os.getenv("FEE_WATCH_WEEK_WARN_PCT", "1.0"))


def _iso(dt_) -> str:
    return dt_.strftime("%Y-%m-%dT%H:%M:%SZ")


def _bucket(fills, since):
    """Commission and fill count at or after `since`, split spot vs other.

    Split, because the two answer different questions. Spot fees are the
    cost of the strategy under discussion. Everything else - event
    contracts, whatever appears next - is a separate activity that happens
    to share the account, and folding them together is what let one day of
    it hide inside a month of trading.
    """
    total = spot = other = 0.0
    n = n_spot = 0
    for f in fills:
        ts = f.get("trade_time") or f.get("sequence_timestamp") or ""
        if ts < since:
            continue
        try:
            c = float(f.get("commission") or 0)
        except (TypeError, ValueError):
            continue
        pid = (f.get("product_id") or "").upper()
        is_spot = pid.endswith("-USD") and "KALSHI" not in pid
        total += c
        n += 1
        if is_spot:
            spot += c
            n_spot += 1
        else:
            other += c
    return {"commission_usd": round(total, 2), "fills": n,
            "spot_usd": round(spot, 2), "spot_fills": n_spot,
            "non_spot_usd": round(other, 2), "non_spot_fills": n - n_spot}


def _verdict(pct, warn, alarm):
    if pct is None:
        return "unknown"
    if pct >= alarm:
        return "ALARM"
    if pct >= warn:
        return "elevated"
    return "ok"


async def watch(session, account_usd: float = None, days: int = 30) -> dict:
    """Rolling fee load, from Coinbase's own commission figures."""
    import crypto_btc_compound_bot as engine
    now = datetime.now(timezone.utc)
    raw = await engine.fetch_fills_between(
        session, _iso(now - timedelta(days=days)), _iso(now))
    if not raw.get("available"):
        return {"available": False, "error": raw.get("error")}
    fills = raw["fills"]

    windows = {}
    for label, delta in (("24h", timedelta(hours=24)),
                         ("7d", timedelta(days=7)),
                         (f"{days}d", timedelta(days=days))):
        b = _bucket(fills, _iso(now - delta))
        b["pct_of_account"] = (round(100.0 * b["commission_usd"] / account_usd, 3)
                               if account_usd else None)
        windows[label] = b

    d, w = windows["24h"], windows["7d"]
    day_v = _verdict(d["pct_of_account"], DAY_WARN_PCT, DAY_ALARM_PCT)
    week_v = _verdict(w["pct_of_account"], WEEK_WARN_PCT, WEEK_ALARM_PCT)
    worst = "ALARM" if "ALARM" in (day_v, week_v) else (
        "elevated" if "elevated" in (day_v, week_v) else "ok")

    out = {
        "available": True,
        "as_of": _iso(now),
        "account_usd": round(account_usd, 2) if account_usd else None,
        "windows": windows,
        "thresholds_pct_of_account": {
            "day_warn": DAY_WARN_PCT, "day_alarm": DAY_ALARM_PCT,
            "week_warn": WEEK_WARN_PCT, "week_alarm": WEEK_ALARM_PCT},
        "day_verdict": day_v,
        "week_verdict": week_v,
        "status": worst,
        "truncated": raw.get("truncated", False),
        # Named for what it is. A monitor that reads like a brake is worse
        # than none, because it invites the assumption that something will
        # stop the next one.
        "note": ("DETECTION ONLY. This reads what the venue charged; it "
                 "cannot stop spending. The 2026-09-06 session came from a "
                 "process outside this codebase holding its own Coinbase "
                 "credentials, and nothing here can reach it."),
    }
    if worst == "ALARM":
        out["alarm"] = (
            f"Fees are running at {d['pct_of_account']}% of the account in 24h "
            f"and {w['pct_of_account']}% over 7 days. For reference, "
            f"2026-09-06 cost 12.8% in one day and went unnoticed for 20.")
    if d["non_spot_usd"] > d["spot_usd"] and d["non_spot_usd"] > 0:
        out["non_spot_note"] = (
            f"${d['non_spot_usd']:,.2f} of the last 24h came from NON-SPOT "
            f"products ({d['non_spot_fills']} fills) - event contracts or "
            f"similar, not the spot strategy. That is the shape of the "
            f"September 6 session.")
    return out

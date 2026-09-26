"""Stops for the coins nobody is watching.

WHY THIS EXISTS

The account holds ~$11,200 of coin across 42 priced assets. Two of them -
ZEC at 25.5% and XRP at 21.4% - are 47% of everything, against the owner's
own stated rule that no coin should take 20% of the fleet. None of it
belongs to a branch, so nothing prices it, monitors it, or stops it. The
bot's entire lifetime loss is $251 over 249 trades; one bad week in ZEC is
four times that. This is the largest unmanaged exposure in the account and
it has never had a number attached to it.

WHAT THIS IS, AND WHAT IT IS NOT

It is a WATCH. It computes, for every holding, the level at which that coin
would have fallen far enough to matter, and says how close it is. It raises
an alert.

It is NOT a stop-loss order. Nothing here places, holds, or guarantees an
exit. A real stop needs an order resting at the exchange; this is a
calculation that tells a human where one would go. Saying otherwise would
be the most dangerous thing this file could do, so it says so in its own
output, on every response, and the tests assert that it does.

WHY IT HOLDS NO STATE

A trailing stop needs a high-water mark. The obvious design stores one per
coin and updates it on a schedule - which needs database writes, which are
refused while DASHBOARD_WRITE_TOKEN is unset, which is exactly when this is
most needed. So the peak is derived from candle history instead: the
highest high over the trailing window, recomputed every call. Stateless,
reproducible, and it works today. It also cannot drift out of sync with the
market the way a stored mark does when the updater stops running.

THE LEVELS ARE THE FLEET'S OWN

Stop distance comes from adaptive_stop.scaled_stop - the same 2.5x daily
volatility, 3% floor, 25% cap that the six live branches use. A second
sizing rule would mean two answers to one question, which is how this
codebase ended up with three P&L formulas and eleven corrupt ledger rows.
"""
from __future__ import annotations

import adaptive_stop

# Below the venue minimum an alert is noise: there is no exit to take.
# Matches crypto_grid_bot.MIN_TRADE_USD rather than inventing a second one.
MIN_EXITABLE_USD = 5.0

# What fraction of the whole account one coin may be before it is called
# out. The owner's words, 2026-09-26: "What you mean that one coin will
# take up 20% of the fleet? I don't think I want that."
CONCENTRATION_LIMIT_PCT = 20.0

OK = "OK"
NEAR = "NEAR_STOP"
BREACHED = "BREACHED"
UNPRICED = "UNPRICED"
NO_VOL = "NO_VOLATILITY_DATA"
DUST = "BELOW_MIN_TRADE"

# Inside this fraction of the way from peak to stop, call it NEAR. Not a
# second threshold to tune - it is a display band, and it never moves the
# stop itself.
NEAR_BAND = 0.80


def peak_from_highs(highs) -> float:
    """The high-water mark: the highest high in the window.

    None for an empty or unusable series. NOT max(closes) - a wick that
    took the price up and back is still a level the position reached, and
    a trailing stop that ignores it trails from a peak that never happened.
    """
    vals = []
    for h in highs or ():
        try:
            v = float(h)
        except (TypeError, ValueError):
            continue
        if v == v and v > 0:
            vals.append(v)
    return max(vals) if vals else None


def assess(asset: str, units: float, price, usd, peak_price,
           daily_vol_pct, account_total_usd: float = 0.0,
           multiple=None, floor=None, cap=None) -> dict:
    """One holding's stop level and how close it is.

    Every refusal is an explicit status, never a silent omission. A coin
    that cannot be priced, cannot be measured, or cannot be sold still
    appears in the output with the reason - the census bug that reported
    $79.30 for an $11,292 account did so by dropping what it could not
    price, and a watch that hides what it cannot assess fails the same way.
    """
    row = {
        "asset": asset,
        "units": units,
        "price": price,
        "usd": usd,
        "status": None,
        "stop_pct": None,
        "stop_level": None,
        "peak_price": peak_price,
        "pct_from_peak": None,
        "pct_to_stop": None,
        "daily_vol_pct": daily_vol_pct,
        "share_of_account_pct": (round(100.0 * (usd or 0) / account_total_usd, 2)
                                 if account_total_usd else None),
        "over_concentration_limit": False,
        "note": None,
    }
    share = row["share_of_account_pct"]
    if share is not None and share > CONCENTRATION_LIMIT_PCT:
        row["over_concentration_limit"] = True

    if price is None or usd is None:
        row["status"] = UNPRICED
        row["note"] = ("no price from the venue, so no level can be computed. "
                       "This is NOT a small position - it is an unknown one.")
        return row

    if (usd or 0) < MIN_EXITABLE_USD:
        row["status"] = DUST
        row["note"] = (f"${usd:,.2f} is below the ${MIN_EXITABLE_USD:.2f} minimum "
                       f"trade, so there is no exit to alert about.")
        return row

    stop_pct = adaptive_stop.scaled_stop(daily_vol_pct, multiple=multiple,
                                         floor=floor, cap=cap)
    if stop_pct is None or not peak_price:
        row["status"] = NO_VOL
        row["note"] = ("not enough price history to size a stop. Deliberately "
                       "NOT given a default level - an invented number here "
                       "would read as protection that does not exist.")
        return row

    level = peak_price * (1.0 - stop_pct)
    row["stop_pct"] = round(stop_pct, 6)
    row["stop_level"] = round(level, 10)
    row["pct_from_peak"] = round((price / peak_price - 1.0) * 100.0, 3)
    row["pct_to_stop"] = round((price / level - 1.0) * 100.0, 3)

    if price <= level:
        row["status"] = BREACHED
        row["note"] = (f"{asset} is {abs(row['pct_from_peak']):.1f}% below its "
                       f"{peak_price:,.8g} peak, past a {stop_pct * 100:.2f}% stop. "
                       f"${usd:,.2f} is exposed.")
    else:
        travelled = (peak_price - price) / (peak_price - level) if peak_price > level else 0.0
        row["status"] = NEAR if travelled >= NEAR_BAND else OK
        row["note"] = (f"{row['pct_to_stop']:.2f}% above its stop "
                       f"({stop_pct * 100:.2f}% below a {peak_price:,.8g} peak)")
    return row


def summarise(rows, cash_usd: float = 0.0) -> dict:
    """The whole watch, with what it cannot see stated first."""
    by = {}
    for r in rows:
        by.setdefault(r["status"], []).append(r)

    def _usd(k):
        return round(sum(r.get("usd") or 0 for r in by.get(k, ())), 2)

    breached = sorted(by.get(BREACHED, ()), key=lambda r: -(r.get("usd") or 0))
    near = sorted(by.get(NEAR, ()), key=lambda r: -(r.get("usd") or 0))
    over = sorted([r for r in rows if r["over_concentration_limit"]],
                  key=lambda r: -(r.get("usd") or 0))
    covered = [r for r in rows if r["status"] in (OK, NEAR, BREACHED)]
    total = round(sum(r.get("usd") or 0 for r in rows), 2)
    return {
        "is_a_watch_not_an_order": True,
        "disclaimer": ("These are ALERT LEVELS, not stop-loss orders. Nothing "
                       "here rests at the exchange and nothing here will sell. "
                       "A real stop requires an order."),
        "assets": len(rows),
        "coin_usd": total,
        "cash_usd": round(cash_usd, 2),
        "covered_assets": len(covered),
        "covered_usd": round(sum(r.get("usd") or 0 for r in covered), 2),
        "covered_share_pct": (round(100.0 * sum(r.get("usd") or 0 for r in covered) / total, 2)
                              if total else None),
        "breached": breached,
        "near": near,
        "unpriced": by.get(UNPRICED, []),
        "unpriced_assets": len(by.get(UNPRICED, ())),
        "no_vol": by.get(NO_VOL, []),
        "no_vol_usd": _usd(NO_VOL),
        "dust": by.get(DUST, []),
        "dust_usd": _usd(DUST),
        "breached_usd": _usd(BREACHED),
        "near_usd": _usd(NEAR),
        "over_concentration": over,
        "concentration_limit_pct": CONCENTRATION_LIMIT_PCT,
        "rows": sorted(rows, key=lambda r: -(r.get("usd") or 0)),
    }

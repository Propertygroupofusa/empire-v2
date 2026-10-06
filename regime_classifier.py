"""How many kinds of weather this fleet trades in, and which one it is now.

THE OWNER'S INSTRUCTION, 2026-10-05: "Different types of tests to see how
many regimes there are."

WHAT WAS MEASURED, 31 days of 15-minute candles across all 21 fleet coins,
2026-09-05 to 2026-10-05. Two axes, both split on the data's own
distribution rather than on invented thresholds - trend on terciles of the
fleet's median daily return, volatility on the median of its daily range:

    DOWN < -0.47% < FLAT < +1.25% < UP        volatility median 5.54%

All six cells occur over 31 days. Over the 10 days the fleet was actually
trading, five occur and UP/HIVOL does not - so this module reports that
regime as UNOBSERVED and refuses to predict it. An unobserved cell is not a
zero.

THE RESULT THAT MATTERS, live window, 125 own closes:

    DOWN/HIVOL   3 days   27.7 closes/day   $28.46/day   1.436% per close
    FLAT/HIVOL   2 days    9.5 closes/day    $8.11/day   2.653%
    UP/LOVOL     1 day     8.0 closes/day    $8.43/day   2.299%
    FLAT/LOVOL   3 days    4.0 closes/day    $4.82/day   2.687%
    DOWN/LOVOL   1 day     3.0 closes/day    $1.94/day   1.312%

VOLATILITY DRIVES CLOSES; DIRECTION BARELY DOES. The high-volatility days
produce 27.7 and 9.5 closes a day, the low-volatility days 8.0, 4.0 and 3.0.
A grid earns by a price crossing its rungs, and a quiet market does not
cross them whichever way it is drifting. The spread between the best and
worst observed day rate is 14.7x, and the fleet controls none of it.

THE ERROR THIS FILE ALMOST SHIPPED WITH, recorded so it is not repeated.
On the full 31 days, UP/HIVOL showed 7 days and ZERO closes, which reads as
"this strategy cannot earn in a rising volatile market" - a big claim with a
clear fix attached. It was wrong. Sep 10-25 is a contiguous 16-day stretch
in which the fleet closed nothing in ANY regime, because it was not trading,
and all 7 UP/HIVOL days fall inside it. The regime was not the cause; the
dead ledger was. Hence LIVE_WINDOW_ONLY below, and hence `observed` is
reported separately from `profitable` everywhere in this file.

Nothing here places an order, reads an account, or changes anything. It
classifies days and counts what the ledger already recorded.
"""
import collections
import statistics as st

# Trend terciles and the volatility median, measured 2026-10-05 over the
# 31-day candle window described above. They are DEFAULTS, overridable, and
# recomputed by fit() whenever a fresh window is available - a threshold
# frozen into a file ages into a lie as the market's own distribution moves.
DEFAULT_TREND_DOWN_PCT = -0.47
DEFAULT_TREND_UP_PCT = 1.25
DEFAULT_VOL_SPLIT_PCT = 5.54

TRENDS = ("DOWN", "FLAT", "UP")
VOLS = ("LOVOL", "HIVOL")
ALL_REGIMES = tuple(f"{t}/{v}" for t in TRENDS for v in VOLS)

# A day needs this many closes before its net% per close is read as evidence,
# matching harvest_redirect's floor for the same reason: two closes can
# produce any percentage at all.
MIN_CLOSES_FOR_A_RATE = 3


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def day_shape(candles):
    """One day's return and range for ONE coin, from [ts, low, high, open, close, ...].

    Returns None rather than a zero when the day is too thin to describe - a
    coin with two bars has no honest daily range, and calling it 0.0% would
    drag the fleet median toward quiet on exactly the days data is missing.
    """
    rows = [c for c in (candles or []) if c and len(c) >= 5]
    if len(rows) < 4:
        return None
    op = _num(rows[0][3])
    cl = _num(rows[-1][4])
    his = [_num(c[2]) for c in rows]
    los = [_num(c[1]) for c in rows]
    his = [h for h in his if h is not None]
    los = [l for l in los if l is not None]
    if not op or op <= 0 or cl is None or not his or not los:
        return None
    lo = min(los)
    if lo <= 0:
        return None
    return {"return_pct": (cl - op) / op * 100.0,
            "range_pct": (max(his) - lo) / lo * 100.0}


def classify(return_pct, range_pct, *, down=DEFAULT_TREND_DOWN_PCT,
             up=DEFAULT_TREND_UP_PCT, vol=DEFAULT_VOL_SPLIT_PCT):
    """The regime label for one day, or None when either axis is unreadable."""
    r, v = _num(return_pct), _num(range_pct)
    if r is None or v is None:
        return None
    trend = "DOWN" if r < down else ("UP" if r > up else "FLAT")
    return f"{trend}/{'HIVOL' if v > vol else 'LOVOL'}"


def fit(daily):
    """Recompute the thresholds from a window of days.

    daily is [{"return_pct": .., "range_pct": ..}, ...]. Fewer than nine days
    is refused: terciles of a handful of points are not a distribution, and
    a threshold fitted to noise would relabel the whole history on a quiet
    week. Returns None, and the caller keeps the defaults.
    """
    rows = [d for d in (daily or [])
            if _num(d.get("return_pct")) is not None
            and _num(d.get("range_pct")) is not None]
    if len(rows) < 9:
        return None
    rets = sorted(_num(d["return_pct"]) for d in rows)
    rngs = [_num(d["range_pct"]) for d in rows]
    n = len(rets)
    return {"down": round(rets[n // 3], 4),
            "up": round(rets[2 * n // 3], 4),
            "vol": round(st.median(rngs), 4),
            "fitted_on_days": n}


def tally(day_regimes, closes_by_day):
    """What each regime actually produced. Counts; predicts nothing.

    day_regimes:    {"2026-09-28": "DOWN/HIVOL", ...} - ONLY days the fleet
                    was live. Passing a day the fleet was not trading is the
                    error documented at the top of this file.
    closes_by_day:  {"2026-09-28": [{"pnl": .., "notional": ..}, ...]}
    """
    agg = {}
    for day, reg in (day_regimes or {}).items():
        if not reg:
            continue
        a = agg.setdefault(reg, {"regime": reg, "days": 0, "closes": 0,
                                 "realized_usd": 0.0, "notional_usd": 0.0})
        a["days"] += 1
        for t in (closes_by_day or {}).get(day, []):
            a["closes"] += 1
            a["realized_usd"] += _num(t.get("pnl")) or 0.0
            a["notional_usd"] += _num(t.get("notional")) or 0.0

    out = []
    for a in agg.values():
        d, n, p, no = a["days"], a["closes"], a["realized_usd"], a["notional_usd"]
        out.append({
            "regime": a["regime"], "days": d, "closes": n,
            "closes_per_day": round(n / d, 2) if d else None,
            "realized_usd": round(p, 2),
            "usd_per_day": round(p / d, 2) if d else None,
            # None, not 0.0, under the sample floor: "too few closes to say"
            # and "earned nothing per close" are different findings.
            "net_pct_per_close": (round(p / no * 100.0, 3)
                                  if no and n >= MIN_CLOSES_FOR_A_RATE else None),
            "rate_is_evidence": n >= MIN_CLOSES_FOR_A_RATE,
        })
    out.sort(key=lambda r: -(r["usd_per_day"] or 0.0))

    seen = {r["regime"] for r in out}
    unobserved = [r for r in ALL_REGIMES if r not in seen]
    best = out[0] if out else None
    worst = out[-1] if out else None
    spread = None
    if best and worst and worst["usd_per_day"]:
        spread = round(best["usd_per_day"] / worst["usd_per_day"], 1)
    return {
        "regimes": out,
        "observed_count": len(out),
        "possible_count": len(ALL_REGIMES),
        "unobserved": unobserved,
        "unobserved_is": ("NO DATA, not a zero. A regime the fleet has not "
                          "traded through says nothing about how it would "
                          "perform there, and must not be reported as bad."),
        "best": best["regime"] if best else None,
        "worst": worst["regime"] if worst else None,
        "day_rate_spread_x": spread,
        "detail": (
            f"{len(out)} of {len(ALL_REGIMES)} regimes observed. "
            + (f"Best {best['regime']} at ${best['usd_per_day']:,.2f}/day, "
               f"worst {worst['regime']} at ${worst['usd_per_day']:,.2f}/day"
               + (f" - a {spread}x spread the fleet does not control."
                  if spread else ".")
               if best and worst else "")
            + (f" Unobserved: {', '.join(unobserved)}." if unobserved else "")),
    }

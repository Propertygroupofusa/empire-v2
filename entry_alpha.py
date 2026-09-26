"""Do our entries beat a coin flip?

WHY THIS EXISTS

Every measurement so far has asked "does the STRATEGY make money". None has
asked the prior question: does the moment we choose to buy beat a randomly
chosen moment in the same coin over the same stretch?

That distinction is the whole game. A strategy can be profitable purely
because the asset rose, in which case the entry logic contributed nothing
and could be replaced by a dice roll at no cost. It can also be unprofitable
while still picking better moments than chance - which is a cost problem,
not a signal problem, and the two need completely different fixes.

The regime study answered this crudely, by hunting for windows where price
fell. This answers it directly and continuously: for every REAL entry the
bot made, draw random entries in the same coin inside the same window, run
both through the SAME rung arithmetic, and report the difference.

WHAT MAKES IT HONEST

  * Real entries. Actual opened_at and entry_price from the ledger, not a
    re-simulation of what the gate would do today.
  * Matched controls. A random entry is drawn from the same coin and the
    same calendar window as the real one it is compared against, so a coin
    that rose lifts BOTH sides equally and cancels out. An unmatched control
    would just re-measure market drift.
  * One arithmetic. Outcomes come from horizon_study.rung_profile's own
    fill logic, so a real entry and a random one cannot be scored by
    subtly different rules.
  * A seed. The draw is reproducible; an unseeded control cannot be checked.

WHAT IT CANNOT DO

167 entries over 16 calendar days is a small sample from one market regime.
A positive result here is suggestive, not established. A NEGATIVE one is
more informative, because beating chance is a low bar and failing it in a
favourable window is hard to explain away.
"""
from __future__ import annotations

import random

# Fees: the REAL measured maker round trip on this account, 0.35% per leg.
# Not 0.50%, which is neither the maker rate (0.70% RT) nor the taker rate
# (1.50% RT) and would flatter every horizon by a quarter to a full point.
MAKER_ROUND_TRIP_PCT = 0.70
TAKER_ROUND_TRIP_PCT = 1.50

HORIZON_SECONDS = {"2h": 7200, "6h": 21600, "12h": 43200,
                   "24h": 86400, "48h": 172800, "72h": 259200}


def resolve_entry(times, highs, lows, closes, i: int, target_pct: float,
                  horizon_sec: int, fee_pct: float = MAKER_ROUND_TRIP_PCT):
    """One entry's outcome: a resting exit at +target%, marked if unfilled.

    Same shape as horizon_study.rung_profile's inner loop, and deliberately
    so - the unfilled entry is MARKED at the horizon close and charged a
    round trip rather than being dropped. Dropping it is the survivorship
    bias the live ledger already has: winners close and get counted, losers
    stay open and never enter the average.

    Returns None when the window does not contain the full horizon, so a
    truncated entry cannot be scored as a small loss.
    """
    n = len(times)
    if i < 0 or i >= n:
        return None
    entry = closes[i]
    if not entry:
        return None
    if times[-1] - times[i] < horizon_sec:
        return None
    rung = entry * (1.0 + target_pct / 100.0)
    j, filled, worst, last = i + 1, False, entry, entry
    while j < n and times[j] <= times[i] + horizon_sec:
        if lows[j] < worst:
            worst = lows[j]
        if not filled and highs[j] >= rung:
            filled = True
            break
        last = closes[j]
        j += 1
    gross = target_pct if filled else (last / entry - 1.0) * 100.0
    return {"filled": filled, "gross_pct": gross, "net_pct": gross - fee_pct,
            "mae_pct": (worst / entry - 1.0) * 100.0}


def nearest_index(times, when_ts: int):
    """Index of the bar whose close is at or just before when_ts.

    Returns None when when_ts sits outside the series rather than clamping
    to an end - a clamped index silently scores an entry against a moment
    the trade never saw.
    """
    if not times or when_ts < times[0] or when_ts > times[-1]:
        return None
    lo, hi = 0, len(times) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if times[mid] <= when_ts:
            lo = mid
        else:
            hi = mid - 1
    return lo


def control_indices(times, i: int, horizon_sec: int, k: int, rng) -> list:
    """k random entry bars from the same series, matched on window.

    Drawn from the whole series rather than from a neighbourhood of i, so
    the control measures "any moment in this coin over this stretch" - the
    thing a dice roll would actually give you. Bars too close to the end to
    contain a full horizon are excluded, exactly as they are for the real
    entry, so the two sides face the same censoring.
    """
    last_usable = None
    for idx in range(len(times) - 1, -1, -1):
        if times[-1] - times[idx] >= horizon_sec:
            last_usable = idx
            break
    if last_usable is None or last_usable < 1:
        return []
    return [rng.randint(0, last_usable) for _ in range(k)]


def alpha(real: list, control: list) -> dict:
    """Strategy minus chance, on the one metric that decides it."""
    def _agg(rows):
        if not rows:
            return {"n": 0, "net_pct": None, "win_rate_pct": None,
                    "fill_pct": None, "mae_p50_pct": None}
        nets = [r["net_pct"] for r in rows]
        maes = sorted(r["mae_pct"] for r in rows)
        return {
            "n": len(rows),
            "net_pct": round(sum(nets) / len(nets), 4),
            "win_rate_pct": round(100.0 * sum(1 for x in nets if x > 0) / len(nets), 2),
            "fill_pct": round(100.0 * sum(1 for r in rows if r["filled"]) / len(rows), 2),
            "mae_p50_pct": round(maes[len(maes) // 2], 3),
        }
    r, c = _agg(real), _agg(control)
    out = {"real": r, "control": c}
    if r["net_pct"] is not None and c["net_pct"] is not None:
        out["alpha_pct"] = round(r["net_pct"] - c["net_pct"], 4)
        out["verdict"] = (
            "entries beat chance" if out["alpha_pct"] > 0 else
            "entries are WORSE than chance" if out["alpha_pct"] < 0 else
            "entries are indistinguishable from chance")
    return out


def bootstrap_alpha(real: list, control: list, iterations: int = 20000,
                    seed: int = 20260926) -> dict:
    """Is the alpha distinguishable from luck, or is 166 entries too few?

    A difference in means on a small, high-variance sample is exactly the
    kind of number that looks like a finding and is noise. This resamples
    both sides with replacement and reports how often the alpha keeps its
    sign, plus a percentile interval.

    Reported rather than thresholded: "p < 0.05" invites a yes/no reading
    of something that is a matter of degree, and the sample here is one
    16-day window in one regime whatever the interval says.
    """
    import random as _r
    if not real or not control:
        return {"iterations": 0, "share_same_sign_pct": None}
    rng = _r.Random(seed)
    rn = [x["net_pct"] for x in real]
    cn = [x["net_pct"] for x in control]
    point = sum(rn) / len(rn) - sum(cn) / len(cn)
    diffs = []
    for _ in range(iterations):
        a = sum(rng.choice(rn) for _ in range(len(rn))) / len(rn)
        b = sum(rng.choice(cn) for _ in range(len(cn))) / len(cn)
        diffs.append(a - b)
    diffs.sort()
    def _pct(q):
        return diffs[max(0, min(len(diffs) - 1, int(q * (len(diffs) - 1))))]
    same = sum(1 for d in diffs if (d > 0) == (point > 0))
    return {
        "iterations": iterations,
        "point_alpha_pct": round(point, 4),
        "ci90_low_pct": round(_pct(0.05), 4),
        "ci90_high_pct": round(_pct(0.95), 4),
        "share_same_sign_pct": round(100.0 * same / len(diffs), 1),
        "crosses_zero": _pct(0.05) <= 0 <= _pct(0.95),
    }

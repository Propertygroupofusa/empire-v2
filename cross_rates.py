"""Coin-to-coin ratios, their z-scores, and what the z-score is worth.

WHAT THIS IS

A ratio like PEPE/SHIB says how much of one coin buys the other, with the
dollar taken out of it. It is a genuinely useful thing to look at: it is
the only view in this system where a coin's move is measured against its
peers instead of against the market everything in this account rides.

WHAT THIS IS NOT

It is not a rotation signal, and the desk that displays it says so on
screen. The proposal this module was built from had a stretched ratio
flash "GATE UNLOCKED: ROTATE" - sell the rich leg, buy the cheap one.
That was tested against 350 sessions of real Coinbase daily candles over
16 of the account's own coins, 120 pairs, every |z| gate and every
holding period:

    gate       hold   fires     mean   win rate   after 0.70% fees
    |z|>=2.0    21d   16.1%   +0.458%     56.8%            -0.242%
    |z|>=2.5    21d    7.8%   +0.284%     57.1%            -0.416%
    |z|>=3.0    10d    3.9%   +0.701%     57.9%            +0.001%

Every setting loses money once both legs are paid for, and the single
break-even case reaches exactly zero at t=1.83, which is not significant.

Two details are worth keeping in view, because they are why the idea is
seductive:

  The win rates are real. 53-58% of these rotations end up on the right
  side. They still lose money, because the losers are bigger than the
  winners. A signal that is usually right and still unprofitable is the
  most expensive kind to put on a television.

  The gate fires far too often to be what it claims. On a normally
  distributed ratio |z|>=2 occurs on 4.6% of days; here it occurs on
  16.1%. At |z|>=3 the expectation is 0.3% and the observation is 3.9% -
  thirteen times over. Ratios between these coins TREND, so a rolling
  z-score is measuring "this has been moving one way" and labelling it
  "this has stretched and must snap back". Those are opposite claims.

So the status this module returns is descriptive - STRETCHED, DRIFTING,
EQUILIBRIUM - and never imperative. Nothing here tells anyone to trade,
and `tested_edge()` carries the numbers above so the desk can print the
refutation next to the number instead of burying it in a commit message.
"""
from __future__ import annotations

import statistics

WINDOW_DAYS = 30          # the lookback the proposal used, kept for comparability
STRETCHED_Z = 2.0
DRIFTING_Z = 1.0

STRETCHED, DRIFTING, EQUILIBRIUM, UNKNOWN = (
    "STRETCHED", "DRIFTING", "EQUILIBRIUM", "UNKNOWN")

# Two legs. Selling the rich coin and buying the cheap one is a round trip
# at the account's measured maker rate, not a single fee.
ROUND_TRIP_FEE_PCT = 0.70


def tested_edge():
    """What the gate was actually worth, measured. Printed on the desk.

    Kept as data rather than prose so the UI cannot show the ratio
    without also being able to show what it is worth.
    """
    return {
        "tested": True,
        "sessions": 350,
        "assets": 16,
        "pairs": 120,
        "round_trip_fee_pct": ROUND_TRIP_FEE_PCT,
        "rows": [
            {"gate": "|z|>=2.0", "hold_days": 21, "fires_pct": 16.1,
             "mean_pct": 0.458, "win_rate_pct": 56.8, "net_pct": -0.242, "t_stat": 1.69},
            {"gate": "|z|>=2.5", "hold_days": 21, "fires_pct": 7.8,
             "mean_pct": 0.284, "win_rate_pct": 57.1, "net_pct": -0.416, "t_stat": 0.74},
            {"gate": "|z|>=3.0", "hold_days": 10, "fires_pct": 3.9,
             "mean_pct": 0.701, "win_rate_pct": 57.9, "net_pct": 0.001, "t_stat": 1.83},
        ],
        "verdict": ("No setting pays after both legs. The best reaches exactly "
                    "break-even and is not statistically established."),
        "why_it_looks_good": ("53-58% of these rotations end on the right side. They "
                              "still lose, because the losers are bigger than the winners."),
        "why_the_gate_is_wrong": ("|z|>=2 should fire on 4.6% of days and fires on 16.1%; "
                                  "|z|>=3 should fire on 0.3% and fires on 3.9%. These "
                                  "ratios trend, so the z-score is measuring a move, not "
                                  "a stretch."),
    }


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def ratio(price_a, price_b):
    """a per b. None whenever that cannot be computed."""
    a, b = _num(price_a), _num(price_b)
    if a is None or b is None or b == 0:
        return None
    return a / b


def zscore(current, history):
    """How far `current` sits from the mean of `history`, in its own sd.

    None when the history is too short or flat. A flat history has zero
    spread, and dividing by it would make every tick look infinitely
    extreme - which on a display is worse than showing nothing.
    """
    c = _num(current)
    if c is None:
        return None
    vals = [x for x in (_num(v) for v in (history or ())) if x is not None]
    if len(vals) < 5:
        return None
    sd = statistics.pstdev(vals)
    if sd <= 0:
        return None
    return (c - statistics.mean(vals)) / sd


def classify(z):
    """Descriptive, never imperative. There is no ROTATE state."""
    if z is None:
        return UNKNOWN
    a = abs(z)
    if a >= STRETCHED_Z:
        return STRETCHED
    if a >= DRIFTING_Z:
        return DRIFTING
    return EQUILIBRIUM


def pair_row(asset_a, asset_b, series_a, series_b, *, window=WINDOW_DAYS):
    """One row of the matrix: live ratio, its mean, its z, its state.

    `series_a`/`series_b` are closes, oldest first, aligned. Misaligned or
    short inputs return UNKNOWN rather than a number computed from
    whatever happened to line up.
    """
    a = [x for x in (_num(v) for v in (series_a or ()))]
    b = [x for x in (_num(v) for v in (series_b or ()))]
    n = min(len(a), len(b))
    if n < 6:
        return {"pair": f"{asset_a}/{asset_b}", "a": asset_a, "b": asset_b,
                "ratio": None, "mean": None, "z": None, "status": UNKNOWN,
                "detail": f"only {n} aligned sessions; needs at least 6"}
    a, b = a[-n:], b[-n:]
    series = []
    for x, y in zip(a, b):
        r = ratio(x, y)
        if r is not None:
            series.append(r)
    if len(series) < 6:
        return {"pair": f"{asset_a}/{asset_b}", "a": asset_a, "b": asset_b,
                "ratio": None, "mean": None, "z": None, "status": UNKNOWN,
                "detail": "not enough sessions where both coins were priced"}
    live = series[-1]
    hist = series[-(window + 1):-1] or series[:-1]
    z = zscore(live, hist)
    status = classify(z)
    mean = statistics.mean(hist) if hist else None
    return {
        "pair": f"{asset_a}/{asset_b}", "a": asset_a, "b": asset_b,
        "ratio": live, "mean": mean, "z": round(z, 2) if z is not None else None,
        "sessions": len(series), "window": min(window, len(hist)),
        "status": status,
        "rich": (asset_a if (z or 0) > 0 else asset_b) if status == STRETCHED else None,
        "detail": _detail(asset_a, asset_b, z, status),
    }


def _detail(a, b, z, status):
    if status == UNKNOWN:
        return "not enough history to say"
    if status == EQUILIBRIUM:
        return f"{a} is priced against {b} about where it has been"
    rich, cheap = (a, b) if (z or 0) > 0 else (b, a)
    word = "well above" if status == STRETCHED else "above"
    return (f"{rich} is {word} its usual price in {cheap}. Measured, that has not "
            f"paid to act on.")


MAX_ROWS_PER_ASSET = 2


def dominant_asset(rows, multiple=2.0):
    """The coin monopolising the stretched list, if one is.

    When one coin appears in most of the stretched pairs, that is not a
    set of pair opportunities - it is one coin that moved, showing up
    once against every peer. Saying so is the difference between a board
    that reads as nine findings and a board that reads as one.
    """
    stretched = [r for r in rows if r.get("status") == STRETCHED]
    if len(stretched) < 3 or not rows:
        return None
    counts, base = {}, {}
    for r in stretched:
        for k in ("a", "b"):
            counts[r[k]] = counts.get(r[k], 0) + 1
    for r in rows:
        for k in ("a", "b"):
            base[r[k]] = base.get(r[k], 0) + 1
    asset, n = max(counts.items(), key=lambda kv: kv[1])

    # Compared against the coin's OWN baseline, not a flat threshold. In a
    # 12-asset matrix every coin sits in 11 of 66 pairs, so 17% is what
    # "no concentration" looks like and a fixed 60% bar would never fire.
    expected = base.get(asset, 0) / (2.0 * len(rows))
    observed = n / (2.0 * len(stretched))
    if expected <= 0 or observed < expected * multiple:
        return None
    return {"asset": asset, "pairs": n, "of": len(stretched),
            "observed_pct": round(observed * 100, 1),
            "expected_pct": round(expected * 100, 1),
            "note": (f"{asset} is on one side of {n} of the {len(stretched)} stretched "
                     f"pairs - {observed * 100:.0f}% of them against {expected * 100:.0f}% "
                     f"if nothing were concentrated. That is one coin that moved, not {n} "
                     f"findings; it shows up once against every peer it is measured "
                     f"against.")}


def matrix(assets, closes_by_asset, *, window=WINDOW_DAYS, top=None,
           max_per_asset=MAX_ROWS_PER_ASSET):
    """Every pair among `assets`, most stretched first.

    `closes_by_asset` maps ticker to a list of closes, oldest first. An
    asset with no series is reported as missing rather than skipped - a
    matrix quietly built on half the coins is a different matrix.
    """
    have = [a for a in assets if closes_by_asset.get(a)]
    missing = [a for a in assets if not closes_by_asset.get(a)]
    rows = []
    for i, a in enumerate(have):
        for b in have[i + 1:]:
            rows.append(pair_row(a, b, closes_by_asset[a], closes_by_asset[b],
                                 window=window))
    rows.sort(key=lambda r: -(abs(r["z"]) if r["z"] is not None else -1))
    counts = {s: sum(1 for r in rows if r["status"] == s)
              for s in (STRETCHED, DRIFTING, EQUILIBRIUM, UNKNOWN)}
    dominant = dominant_asset(rows)

    # Counts above are over EVERY pair. Only the display is thinned, so a
    # single runaway coin cannot fill the board with the same finding
    # written twelve ways.
    shown, seen = [], {}
    if max_per_asset:
        for r in rows:
            if (seen.get(r["a"], 0) >= max_per_asset
                    or seen.get(r["b"], 0) >= max_per_asset):
                continue
            seen[r["a"]] = seen.get(r["a"], 0) + 1
            seen[r["b"]] = seen.get(r["b"], 0) + 1
            shown.append(r)
    else:
        shown = rows

    return {
        "is_a_measurement_not_a_signal": True,
        "assets": have,
        "missing_assets": missing,
        "pairs": len(rows),
        "counts": counts,
        "rows": shown[:top] if top else shown,
        "rows_shown": len(shown[:top] if top else shown),
        "max_per_asset": max_per_asset,
        "dominant": dominant,
        "edge": tested_edge(),
        "headline": (f"{counts[STRETCHED]} of {len(rows)} pairs are more than "
                     f"{STRETCHED_Z:.0f} standard deviations from their "
                     f"{window}-day average."
                     + (f" {dominant['note']}" if dominant else "")
                     + (f" {len(missing)} coin(s) had no usable history."
                        if missing else "")),
    }

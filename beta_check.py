"""Did the strategy earn that, or did the coin just go up?

THE TABLE THAT PROMPTED THIS

The resting-rung study measured three coins over the same 21 days:

  BONK   window +19.27%   best rung 3.0%@72h  =  +0.88%
  ONDO   window +16.52%   best rung 3.0%@72h  =  positive
  BTC    window  -2.61%   EVERY rung, every target, every horizon = negative

BTC's 3.0%@72h rung filled 0% of the time and returned exactly -2.61% -
the window return - because nothing ever filled and the "expectancy" was
the mark on a leg that never opened.

Two coins rose and the rung made money. One fell and the rung lost at
every setting. That is not an edge behaving differently in different
regimes. That is the rung returning whatever the coin returned, which is
BETA, and beta is available for free by holding.

WHY THIS NEEDS A MODULE AND NOT A GLANCE

A long-only strategy measured in a rising market reports the market's
return and calls it expectancy. The number is real; the attribution is
wrong. Every previous "growth idea" in this account failed out of sample
for some version of this, and the tell was always visible in the data
before the money went in.

So this asks one question of any result set: does the outcome track the
instrument's own return? If it does, the strategy has not been shown to
do anything the instrument was not already doing.

THE ASYMMETRY, WHICH IS THE USEFUL PART

A NEGATIVE result in a FAVOURABLE window is robust. A long-only rung
that loses 1.03% while its coin rose 19.27% really loses - no regime
excuse is available, and that finding can be acted on immediately.

A POSITIVE result in a favourable window proves nothing yet.

So this can rule tight-and-fast OUT with confidence while being unable
to rule wide-and-slow IN. Those are different strengths of evidence and
collapsing them is how a backtest gets promoted into a loss.
"""
from __future__ import annotations

# Below this spread between the best and worst window return, the sample
# contains no regime contrast and cannot separate edge from beta.
MIN_RETURN_SPREAD_PCT = 5.0

# Fewer instruments than this is an anecdote, not a cross-section.
MIN_INSTRUMENTS = 3

# A window return at or below this is a falling market for these purposes.
FALLING_PCT = 0.0


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (f != f or f in (float("inf"), float("-inf"))) else f


def _pairs(results, window_returns):
    """(instrument, result, window_return) for everything readable."""
    out = []
    for k, v in (results or {}).items():
        r, w = _num(v), _num((window_returns or {}).get(k))
        if r is None or w is None:
            continue
        out.append({"instrument": k, "result_pct": r, "window_return_pct": w})
    out.sort(key=lambda p: p["window_return_pct"])
    return out


def _corr(xs, ys):
    """Pearson r, or None when it is not defined."""
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / (sxx * syy) ** 0.5


def verdict(results, window_returns, *, min_instruments=MIN_INSTRUMENTS,
            min_spread_pct=MIN_RETURN_SPREAD_PCT):
    """One answer about one configuration, across the instruments it saw.

    `results` maps instrument -> the strategy's measured expectancy.
    `window_returns` maps instrument -> what the instrument itself did.
    """
    ps = _pairs(results, window_returns)
    if len(ps) < min_instruments:
        return {
            "verdict": "TOO_FEW_INSTRUMENTS",
            "pairs": ps,
            "detail": (f"{len(ps)} instrument(s) with both a result and a window return. "
                       f"Separating edge from beta needs at least {min_instruments} - one "
                       f"coin rising is not a cross-section."),
            "can_act_on_it": False,
        }

    wins = [p["window_return_pct"] for p in ps]
    res = [p["result_pct"] for p in ps]
    spread = round(max(wins) - min(wins), 3)
    r = _corr(wins, res)

    rising = [p for p in ps if p["window_return_pct"] > FALLING_PCT]
    falling = [p for p in ps if p["window_return_pct"] <= FALLING_PCT]
    lost_while_rising = [p for p in rising if p["result_pct"] <= 0]
    won_while_falling = [p for p in falling if p["result_pct"] > 0]

    base = {
        "pairs": ps,
        "return_spread_pct": spread,
        "correlation": round(r, 3) if r is not None else None,
        "rising_instruments": len(rising),
        "falling_instruments": len(falling),
    }

    # THE ROBUST NEGATIVE, checked first because it is the only finding
    # here that can be acted on without waiting for another regime.
    if rising and len(lost_while_rising) == len(rising):
        shown = ", ".join(
            "{} {:+.2f}% window, {:+.4f}% result".format(
                p["instrument"], p["window_return_pct"], p["result_pct"])
            for p in rising[:3])
        return {**base, "verdict": "ROBUST_NEGATIVE",
                "can_act_on_it": True,
                "detail": (
                    f"Lost on every one of the {len(rising)} instrument(s) that ROSE "
                    f"({shown}). A long-only result that is negative in a favourable window "
                    f"is robust - no regime excuse is available. This can be ruled out now.")}

    if spread < min_spread_pct:
        return {**base, "verdict": "NO_REGIME_CONTRAST",
                "can_act_on_it": False,
                "detail": (
                    f"Every instrument did roughly the same thing over this window "
                    f"({spread:.2f} points between best and worst). With no contrast, a "
                    f"result that tracks the market and a result that beats it are "
                    f"indistinguishable.")}

    if not falling:
        strong = r is not None and r >= 0.8
        return {**base,
                "verdict": "BETA_NOT_EDGE" if strong else "UNPROVEN_POSITIVE",
                "can_act_on_it": False,
                "detail": (
                    (f"The result tracks the instrument's own return almost one for one "
                     f"(r={r:.2f}) across {len(ps)} coins, and not one of them fell. That is "
                     f"beta - the return of holding - wearing a strategy's name. "
                     if strong else
                     f"Every one of the {len(ps)} instruments ROSE over this window "
                     f"({min(wins):+.2f}% to {max(wins):+.2f}%). ")
                    + "A positive long-only result measured only in a rising market is not "
                      "yet evidence of an edge. It has not been shown a falling market, and "
                      "that is the sample that decides it.")}

    if won_while_falling:
        return {**base, "verdict": "EDGE_SURVIVES_A_FALL",
                "can_act_on_it": True,
                "detail": (
                    f"Positive on {len(won_while_falling)} instrument(s) that FELL "
                    f"({', '.join(p['instrument'] for p in won_while_falling[:3])}). That is "
                    f"the sample every other finding in this account has been waiting for: a "
                    f"long-only result that survives the regime it is supposed to fail in.")}

    return {**base, "verdict": "FAILED_THE_FALL",
            "can_act_on_it": True,
            "detail": (
                f"It had {len(falling)} falling instrument(s) to prove itself on and was "
                f"negative on all of them. The rising-market numbers describe the market, "
                f"not the strategy.")}


def scan(configs, window_returns, **kw):
    """Every configuration, judged the same way, ranked by what can be acted on.

    Takes {config_name: {instrument: result_pct}} and returns each with
    its verdict, so a table of forty rung settings collapses into the
    handful that mean anything.
    """
    out = []
    for name, results in (configs or {}).items():
        v = verdict(results, window_returns, **kw)
        out.append({"config": name, **v})
    rank = {"EDGE_SURVIVES_A_FALL": 0, "ROBUST_NEGATIVE": 1, "FAILED_THE_FALL": 2,
            "UNPROVEN_POSITIVE": 3, "BETA_NOT_EDGE": 4, "NO_REGIME_CONTRAST": 5,
            "TOO_FEW_INSTRUMENTS": 6}
    out.sort(key=lambda o: (rank.get(o["verdict"], 9), o["config"]))
    actionable = [o for o in out if o["can_act_on_it"]]
    return {
        "configs": out,
        "actionable": [o["config"] for o in actionable],
        "rule_out": [o["config"] for o in out if o["verdict"] in
                     ("ROBUST_NEGATIVE", "FAILED_THE_FALL")],
        "rule_in": [o["config"] for o in out if o["verdict"] == "EDGE_SURVIVES_A_FALL"],
        "detail": (
            f"{len(out)} configuration(s) judged. "
            + (f"{len([o for o in out if o['verdict'] == 'EDGE_SURVIVES_A_FALL'])} survived a "
               f"falling instrument. " if any(o["verdict"] == "EDGE_SURVIVES_A_FALL" for o in out)
               else "None has been shown a falling market, so none can be ruled IN. ")
            + f"{len([o for o in out if o['verdict'] in ('ROBUST_NEGATIVE', 'FAILED_THE_FALL')])} "
              f"can be ruled OUT now, because a long-only result that loses in a rising "
              f"market loses for real."),
        "is_a_measurement_not_a_change": True,
    }

"""Test an idea the way it will be traded: on data the search never saw.

WHY THIS EXISTS

Three ideas were proposed for this account in a single week - hold
positions longer, gate entries on post-only spread, rotate between coins
on a cross-rate z-score. All three sounded right. All three died on
contact with out-of-sample data, and one of them died only AFTER an
in-sample number of +3.252% had already been written down.

That last one is the whole reason for this file. Searching 96
configurations and reporting the best one is not a test, it is a
selection. The best of 96 noise draws looks spectacular by construction.

THE THREE WAYS A BACKTEST LIES, AND WHAT IS DONE ABOUT EACH

1. IT REPORTS THE SEARCH AS THE RESULT.
   Fixed structurally, not by discipline: `Finding.headline` and
   `Finding.net_pct` read from the TEST window only. The training number
   exists on the object as `selection_only`, named so that quoting it as
   a result requires typing the words "selection only".

2. OVERLAPPING WINDOWS INFLATE SIGNIFICANCE.
   A 42-day hold rolled daily is the same bet counted 42 times. The
   cross-rate study's t of 13.14 came from 6,694 observations that
   contained about 526 independent bets; corrected, it was 2.03. So every
   t here is divided by sqrt(hold_days) and the raw one is kept beside it
   as evidence of how much was removed.

3. TESTING MANY THINGS GUARANTEES A WINNER.
   Under the null, the largest |t| among N independent tries grows like
   sqrt(2 ln N). At N=96 that is 3.02, so a t of 2.61 - which looks
   significant against the textbook 1.96 - is BELOW what pure noise
   produces from a search that size. The threshold here scales with how
   many configurations were tried, and a search does not get to forget
   how wide it was.

WHAT A PASS MEANS, AND WHAT IT DOES NOT

A finding passes only if, in the test window alone, it makes money after
the round-trip fee AND its overlap-corrected t clears the search-width
threshold. That is a floor, not a promise: it says the result is unlikely
to be the search finding shapes in noise. It says nothing about whether
the regime that produced it continues, and nothing about whether the
trade is executable - a pair trade needing a short is not placeable in a
spot account no matter how well it tests.
"""
from __future__ import annotations

import math
import statistics

# Two legs at the account's measured maker rate. A one-legged fee is the
# single most common way a losing strategy is published as a winner.
DEFAULT_FEE_PCT = 0.70

# The textbook 1.96 applies to ONE pre-registered test. It is never the
# right bar for a grid search, and it is kept here only to be reported
# alongside the real threshold so the gap is visible.
NAIVE_T = 1.96

PASS, FAIL, INCONCLUSIVE = "PASS", "FAIL", "INCONCLUSIVE"
MIN_TEST_OBSERVATIONS = 30
MIN_INDEPENDENT_ROUNDS = 5.0


def split_index(n, train_frac=0.70):
    """Where the training window ends. Chronological, never shuffled.

    A random split leaks the future into the past through overlapping
    windows and shared market regimes. Time series get a cut, not a
    shuffle.
    """
    if n is None or n < 2:
        raise ValueError(f"need at least 2 periods to split, got {n}")
    f = float(train_frac)
    if not 0.0 < f < 1.0:
        raise ValueError(f"train_frac must be between 0 and 1, got {train_frac}")
    cut = int(n * f)
    return max(1, min(cut, n - 1))


def search_threshold(n_configs):
    """The |t| a search of this width produces from noise alone.

    sqrt(2 ln N) is the expected maximum of N standard normals. Testing
    one thing gives back roughly the familiar bar; testing a hundred
    raises it to about 3.
    """
    n = max(int(n_configs or 1), 1)
    if n == 1:
        return NAIVE_T
    return max(math.sqrt(2.0 * math.log(n)), NAIVE_T)


def independent_rounds(n_observations, hold_days, n_streams=1):
    """How many genuinely separate bets are in a set of overlapping ones.

    Daily entries held `hold_days` overlap by hold_days-1. Counting them
    as independent is what turns a t of 2 into a t of 13.
    """
    n = float(n_observations or 0)
    h = max(float(hold_days or 1), 1.0)
    return n / h


def corrected_t(returns, hold_days):
    """t on the mean, deflated for overlap. None when it cannot be computed."""
    vals = [v for v in (_num(x) for x in (returns or ())) if v is not None]
    if len(vals) < 2:
        return None, None
    sd = statistics.pstdev(vals)
    if sd <= 0:
        return None, None
    raw = statistics.mean(vals) / (sd / math.sqrt(len(vals)))
    h = max(float(hold_days or 1), 1.0)
    return raw, raw / math.sqrt(h)


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (f != f or f in (float("inf"), float("-inf"))) else f


def evaluate(returns, *, hold_days, fee_pct=DEFAULT_FEE_PCT, n_configs=1):
    """One window's performance, fully deflated. Never a verdict on its own."""
    vals = [v for v in (_num(x) for x in (returns or ())) if v is not None]
    raw_t, adj_t = corrected_t(vals, hold_days)
    mean = statistics.mean(vals) if vals else None
    fee = _num(fee_pct)
    fee = DEFAULT_FEE_PCT if fee is None else fee
    return {
        "observations": len(vals),
        "independent_rounds": round(independent_rounds(len(vals), hold_days), 1),
        "gross_pct": round(mean, 4) if mean is not None else None,
        "fee_pct": fee,
        "net_pct": round(mean - fee, 4) if mean is not None else None,
        "win_rate_pct": (round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1)
                         if vals else None),
        "t_raw": round(raw_t, 2) if raw_t is not None else None,
        "t_overlap_corrected": round(adj_t, 2) if adj_t is not None else None,
        "t_threshold": round(search_threshold(n_configs), 2),
        "t_naive_bar": NAIVE_T,
        "hold_days": hold_days,
    }


def verdict(test_stats, n_configs):
    """PASS, FAIL or INCONCLUSIVE, with the reason it was reached.

    INCONCLUSIVE is a real answer and is never rounded to PASS. A test
    window holding 1.5 independent rounds has not tested anything, and
    saying so is more useful than a number that will be read as evidence.
    """
    s = test_stats or {}
    n = s.get("observations") or 0
    rounds = s.get("independent_rounds") or 0
    net = s.get("net_pct")
    t = s.get("t_overlap_corrected")
    bar = s.get("t_threshold") or search_threshold(n_configs)

    if n < MIN_TEST_OBSERVATIONS:
        return INCONCLUSIVE, (f"only {n} out-of-sample observations; "
                              f"{MIN_TEST_OBSERVATIONS} is the floor")
    if rounds < MIN_INDEPENDENT_ROUNDS:
        return INCONCLUSIVE, (f"only {rounds} independent rounds out of sample - "
                              f"overlapping windows, so this has not been tested "
                              f"enough times to mean anything")
    if net is None or t is None:
        return INCONCLUSIVE, "the test window produced no usable statistic"
    if net <= 0:
        return FAIL, (f"loses {abs(net):.3f}% per round out of sample, after the "
                      f"{s.get('fee_pct')}% round trip")
    if abs(t) < bar:
        return FAIL, (f"makes {net:.3f}% out of sample but t={t:.2f} is under the "
                      f"{bar:.2f} a search of {n_configs} configurations produces "
                      f"from noise alone")
    return PASS, (f"makes {net:.3f}% per round out of sample after fees, "
                  f"t={t:.2f} against a {bar:.2f} bar for a {n_configs}-wide search")


def run(configs, run_fn, *, train_hi, test_lo, test_hi, hold_of,
        fee_pct=DEFAULT_FEE_PCT, label="unnamed idea", train_lo=0):
    """Search on the training window, report on the test window only.

    `run_fn(config, lo, hi)` returns the list of per-trade percentage
    returns that config produced between two period indexes. Everything
    statistical happens here so a new idea is a `run_fn` and nothing else.

    The returned Finding puts the test result at the top level and the
    training result under `selection_only`. That naming is the point:
    the training number selected the configuration and is not evidence
    about it.
    """
    if not configs:
        raise ValueError("a search over zero configurations is not a search")

    n_configs = len(configs)
    trained = []
    for cfg in configs:
        try:
            rets = run_fn(cfg, train_lo, train_hi)
        except Exception as exc:
            trained.append({"config": cfg, "error": f"{type(exc).__name__}: {exc}"})
            continue
        st = evaluate(rets, hold_days=hold_of(cfg), fee_pct=fee_pct, n_configs=n_configs)
        trained.append({"config": cfg, "stats": st})

    usable = [t for t in trained if t.get("stats")
              and t["stats"].get("net_pct") is not None]
    if not usable:
        return {"label": label, "verdict": INCONCLUSIVE,
                "reason": "no configuration produced a usable training statistic",
                "configurations_tested": n_configs, "tried": trained}

    best = max(usable, key=lambda t: t["stats"]["net_pct"])
    cfg = best["config"]
    hold = hold_of(cfg)

    try:
        test_rets = run_fn(cfg, test_lo, test_hi)
    except Exception as exc:
        return {"label": label, "verdict": INCONCLUSIVE,
                "reason": f"the test window could not be run: {type(exc).__name__}: {exc}",
                "configurations_tested": n_configs, "chosen": cfg}

    test = evaluate(test_rets, hold_days=hold, fee_pct=fee_pct, n_configs=n_configs)
    v, why = verdict(test, n_configs)

    # How far the in-sample number fell. The single most informative line
    # in the whole report, and the one a selection-as-result would hide.
    decay = None
    if best["stats"]["net_pct"] is not None and test["net_pct"] is not None:
        decay = round(test["net_pct"] - best["stats"]["net_pct"], 4)

    return {
        "label": label,
        "verdict": v,
        "reason": why,
        "headline": f"{label}: {v} - {why}",
        "chosen_config": cfg,
        "configurations_tested": n_configs,
        "net_pct": test["net_pct"],                 # TEST window. The result.
        "out_of_sample": test,
        "selection_only": {
            "warning": ("These numbers CHOSE the configuration. They are not "
                        "evidence about it. The best of a wide search looks "
                        "good by construction."),
            **best["stats"],
        },
        "decay_pct": decay,
        "decay_note": (None if decay is None else
                       (f"the chosen configuration gave "
                        f"{best['stats']['net_pct']:+.3f}% where it was selected and "
                        f"{test['net_pct']:+.3f}% where it was tested, a fall of "
                        f"{abs(decay):.3f} points")),
        "ranked_in_sample": sorted(
            [{"config": t["config"], "net_pct": t["stats"]["net_pct"],
              "t_overlap_corrected": t["stats"]["t_overlap_corrected"]}
             for t in usable], key=lambda r: -(r["net_pct"] or 0))[:10],
    }

"""Does a price series mean-revert? Answered with a test, not a chart.

Written 2026-09-25, after a pasted strategy video claimed a Kalshi/Polymarket
spread was "a mean reverting process" and then, instead of testing it,
explained what mean reversion is. That is a definition, not evidence.

The trap is specific and the video demonstrates it against itself: it shows
that a random walk (lambda = 0) "could operate like a stable process". A
calm-looking chart is exactly what a random walk produces much of the time.
Eyes cannot separate the two. A test can.

THE MODEL (Dickey-Fuller, with constant):

    dy_t = alpha + lambda * y_{t-1} + e_t

    lambda < 0   mean reverting  - deviations get pulled back
    lambda = 0   random walk     - deviations persist forever
    lambda > 0   explosive       - deviations amplify

The null hypothesis is lambda = 0 (a unit root, i.e. NOT mean reverting).
Rejecting it is what licenses a pairs trade.

THE CRITICAL SUBTLETY: under the null, the t-statistic on lambda does NOT
follow a t-distribution. Using normal critical values (+/-1.96) makes a
random walk look mean-reverting far more often than it is. The Dickey-Fuller
distribution is used instead, which is why the thresholds below are near
-2.9 rather than -1.96. Getting this wrong is the single most common way a
pairs strategy is "validated" into existence.

Pure standard library: no numpy, scipy or statsmodels, so this runs on a
laptop, a phone-driven Windows box, or a Railway container unchanged.
"""
import math

# MacKinnon critical values for the Dickey-Fuller t-statistic, constant, no
# trend, large sample. Compared against these, NOT against a normal table.
DF_CRITICAL = {0.01: -3.43, 0.05: -2.86, 0.10: -2.57}


def _ols2(x, y):
    """OLS of y on [1, x]. Returns (intercept, slope, se_slope, n).

    Closed form for two parameters - no matrix library needed.
    """
    n = len(x)
    if n < 3:
        raise ValueError(f"need at least 3 observations, got {n}")
    mx = sum(x) / n
    my = sum(y) / n
    sxx = sum((xi - mx) ** 2 for xi in x)
    if sxx <= 0:
        raise ValueError("no variation in the regressor - cannot estimate a slope")
    sxy = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
    slope = sxy / sxx
    intercept = my - slope * mx
    resid = [yi - (intercept + slope * xi) for xi, yi in zip(x, y)]
    dof = n - 2
    if dof <= 0:
        raise ValueError("not enough degrees of freedom")
    sigma2 = sum(r * r for r in resid) / dof
    se_slope = math.sqrt(sigma2 / sxx) if sigma2 > 0 else 0.0
    return intercept, slope, se_slope, n


def adf_test(series, alpha=0.05):
    """Dickey-Fuller test for a unit root.

    Returns a dict with lambda, its t-statistic, the critical value, whether
    the unit root is rejected (i.e. the series mean-reverts), and the
    half-life of a deviation in observations.

    `reject_unit_root=True` is the only result that supports trading the
    series as mean-reverting. Anything else means "not shown", which is not
    the same as "shown to be a random walk" - it usually means not enough
    data, and saying so plainly is the point of this function.
    """
    y = [float(v) for v in series]
    n = len(y)
    if n < 20:
        return {
            "ok": False,
            "reason": f"only {n} observations - far too few to test. "
                      "Collect more before drawing any conclusion.",
            "n": n,
        }

    lagged = y[:-1]
    deltas = [y[i] - y[i - 1] for i in range(1, n)]

    try:
        intercept, lam, se, nobs = _ols2(lagged, deltas)
    except ValueError as e:
        return {"ok": False, "reason": str(e), "n": n}

    if se == 0:
        return {"ok": False, "reason": "zero residual variance - degenerate series", "n": n}

    tstat = lam / se
    crit = DF_CRITICAL.get(alpha, DF_CRITICAL[0.05])
    reject = tstat < crit

    # Half-life of a deviation: how many observations to close half the gap.
    # Only meaningful when lambda is negative.
    if lam < 0 and (1 + lam) > 0:
        half_life = math.log(0.5) / math.log(1 + lam)
    elif lam < 0:
        half_life = 1.0  # overshoots within one step
    else:
        half_life = None

    if reject:
        verdict = "MEAN-REVERTING (unit root rejected)"
    elif lam > 0:
        verdict = "NOT mean-reverting - lambda is positive (deviations amplify)"
    else:
        verdict = "NOT SHOWN - cannot reject a random walk on this data"

    return {
        "ok": True,
        "n": n,
        "lambda": round(lam, 6),
        "se": round(se, 6),
        "t_stat": round(tstat, 4),
        "critical_value": crit,
        "alpha": alpha,
        "reject_unit_root": reject,
        "mean_reverting": reject,
        "half_life_obs": round(half_life, 2) if half_life is not None else None,
        "mean": round(sum(y) / n, 6),
        "verdict": verdict,
    }


def spread_series(a, b):
    """The differential between two aligned price series.

    Pairs trading trades THIS, not either leg. Both inputs must already be
    aligned in time - see pair_by_window() in the collector for that.
    """
    if len(a) != len(b):
        raise ValueError(f"series must be the same length, got {len(a)} and {len(b)}")
    return [float(x) - float(y) for x, y in zip(a, b)]


def divergence_stats(windows, converge_threshold=0.05):
    """How often does the spread FAIL to converge, and what does it cost?

    The video's own observation, unpriced: in the last five minutes the
    differential sometimes converges to zero and sometimes diverges. A
    divergence means the two venues settled opposite ways, so a "market
    neutral" spread position loses on BOTH legs at once.

    Expected value is undefined without these two numbers:

        EV = P(converge) * avg_gain - P(diverge) * avg_loss - costs

    `windows` is a list of {"final_spread": float} - one per completed
    15-minute market. Convergence means |final_spread| <= threshold.
    """
    finals = [abs(float(w["final_spread"])) for w in windows if w.get("final_spread") is not None]
    if not finals:
        return {"ok": False, "reason": "no completed windows with a final spread"}
    converged = [f for f in finals if f <= converge_threshold]
    diverged = [f for f in finals if f > converge_threshold]
    n = len(finals)
    return {
        "ok": True,
        "windows": n,
        "converged": len(converged),
        "diverged": len(diverged),
        "convergence_rate": round(len(converged) / n, 4),
        "divergence_rate": round(len(diverged) / n, 4),
        "avg_divergence_magnitude": round(sum(diverged) / len(diverged), 4) if diverged else 0.0,
        "worst_divergence": round(max(finals), 4),
        "threshold": converge_threshold,
    }


def expected_value(convergence_rate, avg_gain, divergence_rate, avg_loss, round_trip_cost):
    """The arithmetic the video never does.

    Every term must come from measurement. A strategy whose EV depends on an
    unmeasured term does not have a known EV - it has a hope.
    """
    ev = (convergence_rate * avg_gain) - (divergence_rate * avg_loss) - round_trip_cost
    return {
        "expected_value_per_trade": round(ev, 6),
        "profitable": ev > 0,
        "gross_edge": round(convergence_rate * avg_gain, 6),
        "divergence_drag": round(divergence_rate * avg_loss, 6),
        "cost_drag": round(round_trip_cost, 6),
        "note": ("Costs are two legs on two venues, bid-ask crossed entering AND "
                 "exiting - four crossings per round trip, plus both fee schedules."),
    }


if __name__ == "__main__":
    import random

    print("=" * 72)
    print("  SELF-CHECK - can this tell a mean-reverting series from a random walk?")
    print("=" * 72)

    def ou(n, lam=-0.3, mu=0.0, sigma=1.0, seed=1):
        rng = random.Random(seed)
        y, out = mu, []
        for _ in range(n):
            y += lam * (y - mu) + rng.gauss(0, sigma)
            out.append(y)
        return out

    def walk(n, sigma=1.0, seed=1):
        rng = random.Random(seed)
        y, out = 0.0, []
        for _ in range(n):
            y += rng.gauss(0, sigma)
            out.append(y)
        return out

    for label, series in [("mean-reverting (OU, lambda=-0.3)", ou(500)),
                          ("random walk (lambda=0)", walk(500))]:
        r = adf_test(series)
        print(f"\n  {label}")
        print(f"    lambda   = {r['lambda']}")
        print(f"    t-stat   = {r['t_stat']}  (critical {r['critical_value']})")
        print(f"    half-life= {r['half_life_obs']}")
        print(f"    verdict  = {r['verdict']}")

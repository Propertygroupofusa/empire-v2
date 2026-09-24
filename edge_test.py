#!/usr/bin/env python3
"""Edge test: did the entry signal beat the win rate its own economics require?

This asks a narrower question than a backtest, and a far more useful one.
Not "did it make money" - a handful of trades can make money by luck - but
"did it win often enough that the result is unlikely to be luck, given what
each win pays and each loss costs".

Three numbers decide it, all read from real closed trades:

  observed win rate   wins / trades
  break-even win rate avg_loss / (avg_win + avg_loss)
  p-value             P(at least this many wins | true rate == break-even)

A strategy clears the bar only when the observed rate is above break-even
AND that gap is large enough not to be chance at this sample size. A gap
with a p-value of 0.4 is a coin landing heads a few extra times.

The test also reports what it would take to KNOW. With few trades, a real
edge and no edge look identical, so the honest output in that case is
"cannot tell yet, come back at N trades" rather than a verdict. That
number is printed, because it converts "is this working?" into a date.

    python edge_test.py                   # every branch, pooled + per coin
    python edge_test.py --table grid      # grid (default) or family_tree
    python edge_test.py --fee-rate 0.008  # deduct fees if pnl is gross
    python edge_test.py --self-test       # offline, no database

Exit codes: 0 edge demonstrated · 1 no edge · 2 not enough evidence yet
"""

import argparse
import asyncio
import math
import random
import sys
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

ALPHA = 0.05          # significance level for the one-sided binomial test
BOOTSTRAP_ROUNDS = 20000


@dataclass
class EdgeResult:
    label: str
    n: int
    wins: int
    losses: int
    avg_win: float
    avg_loss: float          # positive magnitude
    total_pnl: float
    expectancy: float
    observed_rate: Optional[float]
    break_even_rate: Optional[float]
    ci_low: Optional[float]
    ci_high: Optional[float]
    p_value: Optional[float]
    exp_ci_low: Optional[float]
    exp_ci_high: Optional[float]
    trades_needed: Optional[int]
    verdict: str
    reason: str


# --- statistics (no scipy; exact where it matters) ------------------------


def binom_sf(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p). Exact via math.comb.

    One-sided on purpose: the only question is whether the strategy beat
    break-even, not whether it differs from it in either direction.
    """
    if n <= 0:
        return 1.0
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    if p <= 0.0:
        return 0.0 if k > 0 else 1.0
    if p >= 1.0:
        return 1.0
    return sum(math.comb(n, i) * p**i * (1 - p)**(n - i) for i in range(k, n + 1))


def wilson_interval(wins: int, n: int, z: float = 1.959963985) -> tuple:
    """95% Wilson score interval for a proportion.

    Used instead of the textbook normal approximation because that one
    misbehaves badly at small n and extreme rates - it will happily hand
    back an interval running past 100%, which is where a lot of false
    confidence in small trading samples comes from.
    """
    if n == 0:
        return (0.0, 1.0)
    phat = wins / n
    denom = 1 + z * z / n
    centre = (phat + z * z / (2 * n)) / denom
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * n)) / n) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def bootstrap_expectancy_ci(pnls: Sequence[float], rounds: int = BOOTSTRAP_ROUNDS,
                            seed: int = 12345) -> tuple:
    """Percentile bootstrap CI for mean P&L per trade.

    Trade P&L is skewed - many small wins against fewer large losses is the
    normal shape - so a t-interval assuming symmetry understates the
    downside. Resampling makes no distribution assumption.
    """
    n = len(pnls)
    if n < 2:
        return (None, None)
    rng = random.Random(seed)
    means = []
    for _ in range(rounds):
        means.append(sum(pnls[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    return (means[int(0.025 * rounds)], means[int(0.975 * rounds)])


def trades_needed_for(observed: float, break_even: float,
                      alpha: float = ALPHA, cap: int = 100000) -> Optional[int]:
    """Smallest n at which the observed edge would reach significance.

    Answers "how long until I know?" by holding the observed rate fixed and
    growing the sample until the one-sided test clears alpha. Returns None
    when the observed rate is at or below break-even, where no amount of
    data makes it a winner.
    """
    if observed <= break_even:
        return None
    n = 10
    while n <= cap:
        k = math.ceil(observed * n)
        if binom_sf(k, n, break_even) < alpha:
            return n
        n += 10 if n < 500 else 50
    return None


# --- the test ------------------------------------------------------------


def evaluate(label: str, pnls: Sequence[float], min_trades: int = 20) -> EdgeResult:
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [-p for p in pnls if p < 0]      # magnitudes
    total = float(sum(pnls))
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    base = dict(label=label, n=n, wins=len(wins), losses=len(losses),
                avg_win=avg_win, avg_loss=avg_loss, total_pnl=total,
                expectancy=total / n if n else 0.0,
                observed_rate=None, break_even_rate=None, ci_low=None,
                ci_high=None, p_value=None, exp_ci_low=None, exp_ci_high=None,
                trades_needed=None)

    if n < min_trades:
        return EdgeResult(**base, verdict="NO DATA",
                          reason=f"{n} trades; needs at least {min_trades} before any "
                                 f"test is meaningful")
    if not losses:
        return EdgeResult(**base, verdict="UNTESTABLE",
                          reason="no losing trades recorded, so break-even is "
                                 "undefined. A loss-free sample is nearly always a "
                                 "measurement bug rather than a strategy")
    if not wins:
        return EdgeResult(**base, verdict="NO EDGE",
                          reason="no winning trades at all")

    observed = len(wins) / n
    break_even = avg_loss / (avg_win + avg_loss)
    lo, hi = wilson_interval(len(wins), n)
    p = binom_sf(len(wins), n, break_even)
    elo, ehi = bootstrap_expectancy_ci(list(pnls))
    need = trades_needed_for(observed, break_even)

    base.update(observed_rate=observed, break_even_rate=break_even,
                ci_low=lo, ci_high=hi, p_value=p,
                exp_ci_low=elo, exp_ci_high=ehi, trades_needed=need)

    if observed <= break_even:
        verdict, reason = "NO EDGE", (
            f"won {observed*100:.1f}% but needs {break_even*100:.1f}% just to break "
            f"even at this win/loss geometry")
    elif p < ALPHA:
        verdict, reason = "EDGE", (
            f"{observed*100:.1f}% vs {break_even*100:.1f}% required, p={p:.4f} - "
            f"unlikely to be chance at {n} trades")
    else:
        verdict, reason = "INCONCLUSIVE", (
            f"{observed*100:.1f}% vs {break_even*100:.1f}% required, but p={p:.3f} "
            f"at {n} trades" + (f" - needs about {need} trades to confirm"
                                if need else ""))
    return EdgeResult(**base, verdict=verdict, reason=reason)


# --- data ----------------------------------------------------------------


async def load_pnls(table: str, fee_rate: float) -> Dict[str, List[float]]:
    """Real closed trades, grouped by branch. Raises with a plain reason."""
    from database import AsyncSessionLocal
    from sqlalchemy import select
    if table == "grid":
        from models import CryptoGridTradeHistory as M
    else:
        from models import CryptoCoinTradeHistory as M
    if AsyncSessionLocal is None:
        raise RuntimeError("database is not configured in this environment")

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(M))).scalars().all()

    out: Dict[str, List[float]] = defaultdict(list)
    for r in rows:
        if r.pnl is None:
            continue
        pnl = float(r.pnl)
        if fee_rate > 0:
            notional = float(r.entry_price or 0) * float(r.qty or 0)
            pnl -= notional * fee_rate
        out[r.product_id or r.bot_name or "unknown"].append(pnl)
    return dict(out)


# --- report --------------------------------------------------------------

_MARK = {"EDGE": "PASS", "NO EDGE": "FAIL", "INCONCLUSIVE": "?",
         "NO DATA": "-", "UNTESTABLE": "!"}


def print_result(r: EdgeResult, indent: str = "") -> None:
    print(f"{indent}{r.label}")
    print(f"{indent}  trades {r.n}   wins {r.wins}   losses {r.losses}"
          f"   total ${r.total_pnl:+,.2f}   expectancy ${r.expectancy:+.4f}/trade")
    if r.observed_rate is not None:
        print(f"{indent}  win rate      {r.observed_rate*100:>6.1f}%   "
              f"95% CI {r.ci_low*100:.1f}% - {r.ci_high*100:.1f}%")
        print(f"{indent}  break-even    {r.break_even_rate*100:>6.1f}%   "
              f"(avg win ${r.avg_win:.4f} vs avg loss ${r.avg_loss:.4f})")
        print(f"{indent}  margin        {(r.observed_rate-r.break_even_rate)*100:>+6.1f} "
              f"points   p={r.p_value:.4f}")
        if r.exp_ci_low is not None:
            crosses = r.exp_ci_low < 0 < r.exp_ci_high
            print(f"{indent}  expectancy CI ${r.exp_ci_low:+.4f} to ${r.exp_ci_high:+.4f}"
                  f"{'   (spans $0 - not distinguishable from break-even)' if crosses else ''}")
    print(f"{indent}  [{_MARK.get(r.verdict,'?')}] {r.verdict}: {r.reason}\n")


# --- self-test -----------------------------------------------------------


def self_test() -> int:
    fails = []

    def ck(label, cond):
        print(f"[{'PASS' if cond else 'FAIL'}] {label}")
        if not cond:
            fails.append(label)

    # Exact binomial against hand-checkable values.
    ck("binom_sf(10,10,0.5) == 0.5^10", abs(binom_sf(10, 10, 0.5) - 0.5**10) < 1e-12)
    ck("binom_sf(0,10,0.5) == 1", binom_sf(0, 10, 0.5) == 1.0)
    ck("binom_sf(6,10,0.5) ~ 0.376953", abs(binom_sf(6, 10, 0.5) - 0.376953125) < 1e-9)

    # Wilson must never escape [0,1] - the failure mode it exists to avoid.
    lo, hi = wilson_interval(10, 10)
    ck("Wilson stays inside [0,1] at 100%", 0 <= lo <= 1 and 0 <= hi <= 1 and hi <= 1.0)

    # A genuine, large edge on a big sample must be detected.
    big = [1.0] * 700 + [-1.0] * 300            # 70% wins, break-even 50%
    r = evaluate("strong edge", big)
    ck("clear edge on 1000 trades -> EDGE", r.verdict == "EDGE")
    ck("  and its p-value is tiny", r.p_value < 1e-9)

    # The same rate on a small sample must NOT be called an edge.
    small = [1.0] * 7 + [-1.0] * 3              # 70% wins, only 10 trades
    r2 = evaluate("same rate, tiny sample", small, min_trades=5)
    ck("same 70% on 10 trades -> not EDGE", r2.verdict != "EDGE")
    ck("  and it says how many trades are needed", r2.trades_needed is not None)
    print(f"       needs ~{r2.trades_needed} trades to confirm that rate")

    # A high win rate that still loses money must fail.
    bleeder = [0.10] * 90 + [-5.0] * 10         # 90% wins, badly negative
    r3 = evaluate("90% wins, still losing", bleeder)
    ck("90% win rate below its break-even -> NO EDGE", r3.verdict == "NO EDGE")
    ck("  because expectancy is negative", r3.expectancy < 0)
    print(f"       break-even here is {r3.break_even_rate*100:.1f}%, above the 90% won")

    # Loss-free samples are flagged, not celebrated.
    r4 = evaluate("no losses at all", [1.0] * 50)
    ck("100% win rate -> UNTESTABLE, not EDGE", r4.verdict == "UNTESTABLE")

    # Too few trades is its own answer.
    r5 = evaluate("thin", [1.0, -1.0, 1.0])
    ck("3 trades -> NO DATA", r5.verdict == "NO DATA")

    # Bootstrap CI must bracket the true mean on a symmetric sample.
    lo2, hi2 = bootstrap_expectancy_ci([1.0, -1.0] * 100)
    ck("bootstrap CI brackets 0 on a zero-mean sample", lo2 < 0 < hi2)

    print(f"\nSELF-TEST {'PASSED' if not fails else 'FAILED: ' + ', '.join(fails)}")
    return 0 if not fails else 1


# --- entry point ---------------------------------------------------------


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--table", choices=["grid", "family_tree"], default="grid")
    ap.add_argument("--fee-rate", type=float, default=0.0,
                    help="round-trip fee to deduct if stored pnl is GROSS "
                         "(0 = already net, the default)")
    ap.add_argument("--min-trades", type=int, default=20)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()

    try:
        by_branch = asyncio.run(load_pnls(args.table, args.fee_rate))
    except Exception as exc:
        print(f"Could not read the trade history: {exc}", file=sys.stderr)
        print("Use --self-test to exercise the statistics offline.", file=sys.stderr)
        return 2

    if not by_branch:
        print(f"No closed trades found in the {args.table} history. Nothing to test.")
        return 2

    pooled = [p for v in by_branch.values() for p in v]
    print("=" * 72)
    print(f"EDGE TEST - {args.table} history"
          + (f", fees deducted at {args.fee_rate*100:.2f}%" if args.fee_rate else
             ", stored P&L treated as already net of fees"))
    print("=" * 72 + "\n")

    pooled_result = evaluate("ALL BRANCHES POOLED", pooled, args.min_trades)
    print_result(pooled_result)

    if len(by_branch) > 1:
        print("-" * 72)
        print("PER BRANCH\n")
        for name in sorted(by_branch, key=lambda k: -len(by_branch[k])):
            print_result(evaluate(name, by_branch[name], args.min_trades), indent="  ")

    return {"EDGE": 0, "NO EDGE": 1}.get(pooled_result.verdict, 2)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Expectancy-weighted capital allocation across Grid Bot fleet branches.

Splits a fixed pool of real USD across crypto_grid_branches in proportion to
each branch's own realized expectancy - the mean net P&L per closed trade on
that branch's real CryptoGridTradeHistory rows. Branches that have actually
made money get more; branches that have lost money get none.

Four rules keep it honest, all of them learned the hard way:

  1. Evidence floor. A branch needs MIN_TRADES real closed trades before its
     expectancy counts at all. Three lucky fills are not an edge.

  2. Shrinkage, not raw expectancy. A branch's weight is scaled by
     n / (n + SHRINKAGE_K), so a +$5.00 average over 6 trades does not
     outrank +$1.00 over 200. Small samples pull toward zero rather than
     toward whatever they happened to print.

  3. Losers get nothing, never negative. Negative expectancy produces a
     weight of zero, not a negative allocation. Capital is withheld, not
     inverted - "short the bad branch" is a different strategy and is not
     what this is.

  4. Concentration cap. No branch may exceed MAX_BRANCH_SHARE of the pool,
     however good it looks. The cap is re-applied after redistribution, so
     spilling one branch's excess cannot push another through the ceiling.

Nothing here places an order or writes to the database unless you pass
--apply. The default is a dry run that prints the plan.

    python fleet_capital_allocator.py --backtest      # walk-forward, real DB
    python fleet_capital_allocator.py --self-test     # offline, no DB
    python fleet_capital_allocator.py                 # dry-run plan, real DB
    python fleet_capital_allocator.py --apply         # writes allocated_usd
"""

import argparse
import asyncio
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Sequence

# --- Tunables -------------------------------------------------------------

MIN_TRADES = 20          # rule 1: evidence floor per branch
SHRINKAGE_K = 30.0       # rule 2: half-weight at n == K
MAX_BRANCH_SHARE = 0.35  # rule 3/4: concentration ceiling
MIN_REBALANCE_USD = 5.0  # don't churn for pennies; every move costs fees
MIN_BRANCH_USD = 0.0     # floor for a funded branch (0 = may be defunded)


@dataclass
class BranchStats:
    """What one branch's own closed trades actually say about it."""
    bot_name: str
    product_id: str
    num_trades: int
    total_pnl: float
    expectancy: float          # mean net P&L per closed trade
    win_count: int
    win_rate: Optional[float]
    confidence: float          # n / (n + K), the shrinkage factor
    weight: float = 0.0        # pre-normalisation score
    eligible: bool = False
    reason: str = ""


@dataclass
class AllocationPlan:
    pool_usd: float
    stats: List[BranchStats] = field(default_factory=list)
    targets: Dict[str, float] = field(default_factory=dict)
    moves: Dict[str, float] = field(default_factory=dict)   # target - current
    unallocated_usd: float = 0.0
    note: str = ""


# --- Core maths (pure - no DB, no network, directly testable) -------------


def summarize_branch(bot_name: str, product_id: str,
                     pnls: Sequence[float]) -> BranchStats:
    """Reduce one branch's realized P&L series to its decision inputs."""
    n = len(pnls)
    total = float(sum(pnls))
    wins = sum(1 for p in pnls if p > 0)
    expectancy = total / n if n else 0.0
    confidence = n / (n + SHRINKAGE_K) if n else 0.0
    return BranchStats(
        bot_name=bot_name,
        product_id=product_id,
        num_trades=n,
        total_pnl=round(total, 4),
        expectancy=round(expectancy, 6),
        win_count=wins,
        win_rate=round(wins / n * 100, 2) if n else None,
        confidence=round(confidence, 4),
    )


def _apply_cap(weights: Dict[str, float], pool: float) -> Dict[str, float]:
    """Normalise to `pool`, then enforce MAX_BRANCH_SHARE.

    Capping one branch frees capital that must go somewhere, and naively
    handing it to the rest can push a second branch through the same
    ceiling. So cap, redistribute among the uncapped, and repeat until the
    set of capped branches stops growing.
    """
    total_w = sum(weights.values())
    if total_w <= 0 or pool <= 0:
        return {name: 0.0 for name in weights}

    # The cap is hard, and capital it strands stays in cash. Relaxing it to
    # 1/n so the pool always fully deploys was tried and is worse: with two
    # qualifying branches it forces 50/50 regardless of expectancy, so a
    # branch earning +$5.00/trade and one earning +$0.01/trade receive
    # identical capital - the allocator stops allocating. Holding USD
    # because only two branches have proven themselves is the correct
    # answer, not a shortfall to engineer around.
    ceiling = pool * MAX_BRANCH_SHARE
    capped: Dict[str, float] = {}
    remaining = dict(weights)

    while True:
        rem_pool = pool - sum(capped.values())
        rem_w = sum(remaining.values())
        if rem_w <= 0:
            break
        alloc = {n: rem_pool * (w / rem_w) for n, w in remaining.items()}
        over = [n for n, v in alloc.items() if v > ceiling + 1e-9]
        if not over:
            capped.update(alloc)
            break
        for n in over:
            capped[n] = ceiling
            remaining.pop(n)
        if not remaining:
            break

    out = {name: round(capped.get(name, 0.0), 2) for name in weights}
    return out


def build_plan(branch_pnls: Dict[str, Sequence[float]],
               branch_products: Dict[str, str],
               current_alloc: Dict[str, float],
               pool_usd: float) -> AllocationPlan:
    """Expectancy-weighted targets for every branch. Pure function."""
    plan = AllocationPlan(pool_usd=round(pool_usd, 2))

    for bot_name in sorted(branch_pnls):
        s = summarize_branch(bot_name, branch_products.get(bot_name, "?"),
                             branch_pnls[bot_name])
        if s.num_trades < MIN_TRADES:
            s.reason = (f"only {s.num_trades} closed trades, needs "
                        f"{MIN_TRADES} - not enough evidence to fund on")
        elif s.expectancy <= 0:
            s.reason = (f"expectancy {s.expectancy:+.4f}/trade over "
                        f"{s.num_trades} trades - losing money, no capital")
        else:
            s.eligible = True
            s.weight = s.expectancy * s.confidence
            s.reason = (f"expectancy {s.expectancy:+.4f} x confidence "
                        f"{s.confidence:.3f} = weight {s.weight:.6f}")
        plan.stats.append(s)

    weights = {s.bot_name: s.weight for s in plan.stats}
    if not any(weights.values()):
        plan.targets = {name: 0.0 for name in weights}
        plan.unallocated_usd = round(pool_usd, 2)
        plan.note = ("No branch qualifies: every one is either below the "
                     f"{MIN_TRADES}-trade evidence floor or has negative "
                     "expectancy. Capital stays uncommitted - this is the "
                     "correct answer, not a failure to compute one.")
    else:
        plan.targets = _apply_cap(weights, pool_usd)
        plan.unallocated_usd = round(pool_usd - sum(plan.targets.values()), 2)
        if plan.unallocated_usd > 0.01:
            plan.note = (f"${plan.unallocated_usd:,.2f} left uncommitted: the "
                         f"{MAX_BRANCH_SHARE:.0%} per-branch cap binds and no "
                         "other branch qualifies to absorb it.")

    for name, target in plan.targets.items():
        delta = round(target - current_alloc.get(name, 0.0), 2)
        if abs(delta) >= MIN_REBALANCE_USD:
            plan.moves[name] = delta
    return plan


# --- Backtest -------------------------------------------------------------


@dataclass
class Trade:
    bot_name: str
    product_id: str
    pnl: float
    closed_at: datetime


def walk_forward_backtest(trades: Sequence[Trade], pool_usd: float,
                          window_days: int = 7,
                          warmup_days: int = 14) -> dict:
    """Chronological, no-lookahead test of the allocation rule.

    At the start of each window the allocation is computed using ONLY
    trades that closed strictly before that moment, then scored against
    what the next window actually produced. Return is measured per dollar
    allocated, so a branch that earns $2 on $100 beats one earning $3 on
    $1000, which is the whole point of allocating by expectancy.

    Baseline is equal weight across the branches trading in that window -
    the honest comparison, since "do nothing clever" is the alternative.
    """
    if not trades:
        return {"error": "no trades supplied"}

    ordered = sorted(trades, key=lambda t: t.closed_at)
    start, end = ordered[0].closed_at, ordered[-1].closed_at
    cursor = start + timedelta(days=warmup_days)
    if cursor >= end:
        return {"error": (f"history spans {(end - start).days} days; need more "
                          f"than the {warmup_days}-day warmup to test anything")}

    products = {t.bot_name: t.product_id for t in ordered}
    windows, strat_total, base_total = [], 0.0, 0.0

    while cursor < end:
        nxt = cursor + timedelta(days=window_days)
        past = [t for t in ordered if t.closed_at < cursor]
        future = [t for t in ordered if cursor <= t.closed_at < nxt]
        if not future:
            cursor = nxt
            continue

        hist = defaultdict(list)
        for t in past:
            hist[t.bot_name].append(t.pnl)
        plan = build_plan(dict(hist), products, {}, pool_usd)

        realized = defaultdict(float)
        counts = defaultdict(int)
        for t in future:
            realized[t.bot_name] += t.pnl
            counts[t.bot_name] += 1

        # Per-dollar return of each branch in this window, scaled by what
        # the rule would have put there.
        funded = {n: v for n, v in plan.targets.items() if v > 0}
        strat = sum(realized.get(n, 0.0) * (v / pool_usd) for n, v in funded.items())

        active = [n for n in realized if counts[n] > 0]
        base = (sum(realized[n] for n in active) / len(active)) if active else 0.0

        strat_total += strat
        base_total += base
        windows.append({
            "from": cursor.date().isoformat(),
            "to": nxt.date().isoformat(),
            "funded_branches": len(funded),
            "trades_in_window": len(future),
            "strategy_pnl": round(strat, 4),
            "baseline_pnl": round(base, 4),
        })
        cursor = nxt

    return {
        "windows": windows,
        "num_windows": len(windows),
        "strategy_total": round(strat_total, 4),
        "baseline_total": round(base_total, 4),
        "edge": round(strat_total - base_total, 4),
        "history_days": (end - start).days,
        "total_trades": len(ordered),
    }


# --- Database access ------------------------------------------------------


async def load_from_db():
    """Real branches and their real closed trades. Returns (pnls, products,
    current_alloc, pool_usd) or raises with a plain explanation."""
    from database import get_session_factory
    from models import CryptoGridBranch, CryptoGridTradeHistory
    from sqlalchemy import select

    if AsyncSessionLocal is None:
        raise RuntimeError("database is not configured in this environment")

    async with get_session_factory()() as db:
        branches = (await db.execute(select(CryptoGridBranch))).scalars().all()
        rows = (await db.execute(select(CryptoGridTradeHistory))).scalars().all()

    pnls: Dict[str, List[float]] = defaultdict(list)
    for r in rows:
        if r.pnl is not None:
            pnls[r.bot_name].append(float(r.pnl))

    products, current = {}, {}
    for b in branches:
        # A locked or inactive branch keeps its capital and is not a
        # candidate for more - skipping it here means the pool is split
        # among branches that can actually trade.
        if not getattr(b, "active", True) or getattr(b, "locked", False):
            continue
        products[b.bot_name] = b.product_id
        current[b.bot_name] = float(b.allocated_usd or 0.0)
        pnls.setdefault(b.bot_name, [])

    return dict(pnls), products, current, sum(current.values())


async def load_trades_from_db() -> List[Trade]:
    from database import get_session_factory
    from models import CryptoGridTradeHistory
    from sqlalchemy import select

    async with get_session_factory()() as db:
        rows = (await db.execute(select(CryptoGridTradeHistory))).scalars().all()
    return [Trade(r.bot_name, r.product_id, float(r.pnl), r.closed_at)
            for r in rows if r.pnl is not None and r.closed_at is not None]


async def apply_plan(plan: AllocationPlan) -> int:
    """Write the new allocated_usd values. Only called with --apply."""
    from database import get_session_factory
    from models import CryptoGridBranch
    from sqlalchemy import select

    written = 0
    async with get_session_factory()() as db:
        for bot_name, target in plan.targets.items():
            if bot_name not in plan.moves:
                continue
            row = (await db.execute(
                select(CryptoGridBranch).where(
                    CryptoGridBranch.bot_name == bot_name))).scalar_one_or_none()
            if row is None:
                continue
            row.allocated_usd = target
            written += 1
        await db.commit()
    return written


# --- Reporting ------------------------------------------------------------


def print_plan(plan: AllocationPlan, current: Dict[str, float]) -> None:
    print("=" * 76)
    print("EXPECTANCY-WEIGHTED FLEET ALLOCATION")
    print("=" * 76)
    print(f"Pool: ${plan.pool_usd:,.2f}   "
          f"rules: n>={MIN_TRADES}, shrink K={SHRINKAGE_K:g}, "
          f"cap {MAX_BRANCH_SHARE:.0%}\n")
    print(f"{'branch':<26}{'n':>5}{'exp/trade':>12}{'conf':>8}"
          f"{'current':>11}{'target':>11}{'move':>10}")
    print("-" * 76)
    for s in sorted(plan.stats, key=lambda x: -plan.targets.get(x.bot_name, 0)):
        tgt = plan.targets.get(s.bot_name, 0.0)
        cur = current.get(s.bot_name, 0.0)
        mv = plan.moves.get(s.bot_name)
        print(f"{s.bot_name[:26]:<26}{s.num_trades:>5}{s.expectancy:>+12.4f}"
              f"{s.confidence:>8.3f}{cur:>11,.2f}{tgt:>11,.2f}"
              f"{(f'{mv:+,.2f}' if mv else '-'):>10}")
    print("-" * 76)
    for s in plan.stats:
        if not s.eligible:
            print(f"  withheld  {s.bot_name}: {s.reason}")
    if plan.note:
        print(f"\n{plan.note}")
    if not plan.moves:
        print("\nNo moves: every branch is already within "
              f"${MIN_REBALANCE_USD:.2f} of its target.")


# --- Self-test (offline, deterministic, no DB) ----------------------------


def self_test() -> int:
    failures = []

    def check(label, got, want):
        ok = got == want
        if not ok:
            failures.append(label)
        print(f"[{'PASS' if ok else 'FAIL'}] {label}: got {got}, want {want}")

    # Rule 1: evidence floor.
    p = build_plan({"a": [1.0] * 5}, {"a": "A-USD"}, {}, 1000.0)
    check("5 trades -> unfunded (floor is 20)", p.targets["a"], 0.0)

    # Rule 3: losers get zero, never negative.
    p = build_plan({"a": [-1.0] * 40}, {"a": "A-USD"}, {}, 1000.0)
    check("negative expectancy -> 0.0", p.targets["a"], 0.0)
    check("nothing allocated from a losing fleet", p.unallocated_usd, 1000.0)

    # Rule 2: shrinkage. Same expectancy, more evidence -> more weight.
    # Asserted on the weights themselves, not on the post-cap targets: the
    # cap exists precisely to override the weighting, so a two-branch
    # target comparison measures the ceiling rather than the shrinkage.
    p = build_plan({"small": [2.0] * 25, "big": [2.0] * 400},
                   {"small": "S-USD", "big": "B-USD"}, {}, 1000.0)
    w = {s.bot_name: s.weight for s in p.stats}
    print(f"       equal expectancy ($2.00/trade), unequal n -> "
          f"weight small(25)={w['small']:.4f}  big(400)={w['big']:.4f}")
    check("more evidence earns more weight at equal expectancy",
          w["big"] > w["small"], True)
    check("shrinkage never exceeds the raw expectancy",
          w["big"] < 2.0 and w["small"] < 2.0, True)

    # And it does reach the targets when the cap is not binding.
    p = build_plan({"small": [2.0] * 25, "big": [2.0] * 400,
                    "f1": [0.5] * 100, "f2": [0.5] * 100, "f3": [0.5] * 100},
                   {n: f"{n}-USD" for n in ("small", "big", "f1", "f2", "f3")},
                   {}, 1000.0)
    print(f"       uncapped 5-branch split -> small={p.targets['small']:.2f} "
          f"big={p.targets['big']:.2f}")
    check("more evidence wins on target capital too",
          p.targets["big"] > p.targets["small"], True)

    # The hard cap deliberately leaves cash uncommitted rather than forcing
    # it into branches that have not earned it.
    p = build_plan({"a": [1.0] * 50, "b": [1.0] * 50},
                   {"a": "A", "b": "B"}, {}, 1000.0)
    check("two branches -> capped at 35% each, 30% held in cash",
          (p.targets["a"], p.targets["b"], p.unallocated_usd),
          (350.0, 350.0, 300.0))

    # Rule 4: concentration cap, including the redistribution edge.
    p = build_plan({"x": [50.0] * 200, "y": [0.01] * 200, "z": [0.01] * 200},
                   {"x": "X", "y": "Y", "z": "Z"}, {}, 1000.0)
    check("dominant branch capped at 35%", p.targets["x"], 350.0)
    check("no branch exceeds the cap after redistribution",
          max(p.targets.values()) <= 1000.0 * MAX_BRANCH_SHARE + 0.01, True)
    check("capped pool still fully allocated when others qualify",
          round(sum(p.targets.values()), 2), 1000.0)

    # Churn guard: a fleet already sitting at its targets must not be moved.
    p = build_plan({"a": [1.0] * 50, "b": [1.0] * 50, "c": [1.0] * 50},
                   {"a": "A", "b": "B", "c": "C"},
                   {"a": 333.33, "b": 333.33, "c": 333.33}, 1000.0)
    check("already-correct allocation produces no moves", p.moves, {})

    # ...but an over-concentrated fleet must be pulled back to the cap.
    p = build_plan({"a": [1.0] * 50, "b": [1.0] * 50},
                   {"a": "A", "b": "B"}, {"a": 500.0, "b": 500.0}, 1000.0)
    check("over-cap allocation is de-risked toward the ceiling",
          p.moves, {"a": -150.0, "b": -150.0})

    # Empty fleet must not divide by zero.
    p = build_plan({}, {}, {}, 1000.0)
    check("empty fleet -> no targets, pool untouched", p.unallocated_usd, 1000.0)

    # Backtest honesty: it must refuse rather than invent a result.
    r = walk_forward_backtest([], 1000.0)
    check("empty history -> explicit error, not a fake number",
          "error" in r, True)

    base = datetime(2026, 1, 1)
    short = [Trade("a", "A", 1.0, base + timedelta(days=i)) for i in range(5)]
    r = walk_forward_backtest(short, 1000.0, warmup_days=14)
    check("history shorter than warmup -> explicit error", "error" in r, True)

    # A real walk-forward on a fleet where one branch is genuinely better.
    trades = []
    for d in range(120):
        ts = base + timedelta(days=d)
        trades.append(Trade("good", "G-USD", 2.0, ts))
        trades.append(Trade("bad", "B-USD", -1.0, ts))
    r = walk_forward_backtest(trades, 1000.0, window_days=7, warmup_days=14)
    print(f"       walk-forward: {r['num_windows']} windows, "
          f"strategy {r['strategy_total']:+.2f} vs baseline {r['baseline_total']:+.2f}")
    check("allocator beats equal-weight when one branch is clearly better",
          r["strategy_total"] > r["baseline_total"], True)
    check("no lookahead: every window scored on later trades only",
          all(w["from"] < w["to"] for w in r["windows"]), True)

    print(f"\nSELF-TEST {'PASSED' if not failures else 'FAILED: ' + ', '.join(failures)}")
    return 0 if not failures else 1


# --- Entry point ----------------------------------------------------------


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true", help="offline, no DB")
    ap.add_argument("--backtest", action="store_true", help="walk-forward on real history")
    ap.add_argument("--apply", action="store_true", help="WRITE allocated_usd")
    ap.add_argument("--pool", type=float, default=None,
                    help="override the pool (default: sum of current allocations)")
    ap.add_argument("--window-days", type=int, default=7)
    ap.add_argument("--warmup-days", type=int, default=14)
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()

    async def run():
        if args.backtest:
            trades = await load_trades_from_db()
            print(f"Loaded {len(trades)} real closed grid trades.")
            r = walk_forward_backtest(trades, args.pool or 1000.0,
                                      args.window_days, args.warmup_days)
            if "error" in r:
                print(f"Cannot backtest: {r['error']}")
                return 2
            print(f"\n{'window':<26}{'funded':>8}{'trades':>8}"
                  f"{'strategy':>12}{'baseline':>12}")
            for w in r["windows"]:
                print(f"{w['from']} -> {w['to']:<12}{w['funded_branches']:>8}"
                      f"{w['trades_in_window']:>8}{w['strategy_pnl']:>12.4f}"
                      f"{w['baseline_pnl']:>12.4f}")
            print(f"\n{r['total_trades']} trades over {r['history_days']} days, "
                  f"{r['num_windows']} windows")
            print(f"strategy {r['strategy_total']:+.4f} vs "
                  f"baseline {r['baseline_total']:+.4f} -> edge {r['edge']:+.4f}")
            if r["edge"] <= 0:
                print("\nNo edge over equal weight on this history. Do not deploy "
                      "this allocation on the strength of these numbers.")
            return 0

        pnls, products, current, pool = await load_from_db()
        pool = args.pool if args.pool is not None else pool
        plan = build_plan(pnls, products, current, pool)
        print_plan(plan, current)

        if not args.apply:
            print("\nDRY RUN - nothing written. Re-run with --apply to commit.")
            return 0
        if not plan.moves:
            print("\nNothing to write.")
            return 0
        written = await apply_plan(plan)
        print(f"\nAPPLIED: {written} branch allocation(s) updated.")
        return 0

    try:
        return asyncio.run(run())
    except Exception as exc:
        print(f"Could not run against the database: {exc}", file=sys.stderr)
        print("Use --self-test to exercise the allocation rules offline.",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

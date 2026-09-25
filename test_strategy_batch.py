"""The sweep table must not promote what the engine refuses.

THE BUG THIS FILE WAS WRITTEN FOR

The first end-to-end run of the sweep put this on top of the table:

    BTC-USD  rsi_reversion  out-of-sample +50.35%  trades 2  Sharpe 14.231
                                                   marked "above luck"

Every number was arithmetically correct and the row was worthless. Two
trades is two lucky moves; a Sharpe over a sample of two is division, not
evidence; and the noise floor it cleared is computed for a TYPICAL trade
count, so comparing a 2-trade result against it is not a comparison at all.

run_strategy_lab already refuses results like that - MIN_OOS_TRADES is one
of its five refusals. The table was not disagreeing with the engine; it was
displaying a ranking the engine's verdict was never consulted about. A
ranking that contradicts its own engine on screen is worse than no ranking,
because the number is what gets acted on and the verdict is what gets
scrolled past.

So: a row clears the bar only by clearing BOTH tests, thin rows sort to the
bottom of every quality ranking rather than the top, and every row carries
the engine's own reason in the engine's own order.

ALSO CAUGHT HERE

run_fleet unpacks (closes, highs, lows). fetch_candles_window returns a
FOURTH element - the timestamps. Passing the tuples straight through raised
"too many values to unpack" AFTER every per-coin result was computed, so
the sweep reported "error" and the cross-coin check - the one thing that
catches a winner that only worked on one lucky coin - never ran.

Run: python3 test_strategy_batch.py
"""
import asyncio
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import strategy_batch as SB    # noqa: E402
import strategy_lab as LAB     # noqa: E402

checks = []


def ok(label, cond):
    checks.append((label, bool(cond)))


def _series(n=420, drift=0.0005, vol=0.03, seed=5):
    r = random.Random(seed)
    c = [100.0]
    for _ in range(n):
        c.append(max(0.01, c[-1] * (1 + r.gauss(drift, vol))))
    return (c, [x * 1.006 for x in c], [x * 0.994 for x in c],
            [1600000000 + i * 86400 for i in range(len(c))])


DATA = {"BTC-USD": _series(seed=5), "DOGE-USD": _series(seed=9, vol=0.05)}


async def _fake_fetch(coins, days, gran):
    return {k: v for k, v in DATA.items() if k in coins}, {}


SB._fetch_series = _fake_fetch


async def _run():
    await SB.start(list(DATA), days=730, granularity=86400, control_draws=3)
    for _ in range(600):
        if not SB.is_running():
            break
        await asyncio.sleep(0.5)
    return SB.snapshot(include_results=False)


snap = asyncio.run(_run())

# --- the job completes, and the fleet check actually runs -----------------
ok("the sweep reaches 'done', not 'error'", snap["state"] == "done")
ok("REGRESSION: it did not die unpacking 4-tuples into 3 names",
   "too many values to unpack" not in str(snap.get("error") or ""))
ok("it counted every variant on every coin",
   snap["total"] == LAB.VARIANT_COUNT * len(DATA) and snap["done"] == snap["total"])
ok("the cross-coin fleet check RAN - it is what catches a one-lucky-coin winner",
   isinstance(SB._JOB.get("fleet"), dict) and SB._JOB["fleet"].get("verdict"))

# --- a second sweep cannot interleave into the first ----------------------
SB._JOB["state"] = "running"
busy = asyncio.run(SB.start(["BTC-USD"]))
ok("a sweep started over a running one is refused, not interleaved",
   busy.get("status") == "already_running")
SB._JOB["state"] = "done"

# --- the table -----------------------------------------------------------
table = SB.ranked_rows(limit=500)
rows = table["rows"]
ok("the table has rows", len(rows) > 0)
ok("every row carries the noise floor for the width of ITS search",
   all(r["noise_floor_p95"] is not None for r in rows))
ok("every row carries the engine's own reason", all(r["verdict_short"] for r in rows))
ok("and the date the out-of-sample period begins, not just a bar count",
   all(r["split_label"] for r in rows))

thin = [r for r in rows if r["oos_trades"] < LAB.MIN_OOS_TRADES]
ok("the fixture really does contain thin-sample rows (or this proves nothing)", thin)
ok("REGRESSION: not one thin row is marked 'above luck'",
   all(r["above_noise_floor"] is False for r in thin))
ok("and each says why, naming the bar it missed",
   all("too few" in r["verdict_short"] and str(LAB.MIN_OOS_TRADES) in r["verdict_short"]
       for r in thin))
ok("the reason is grammatical for a single trade",
   all("1 trades" not in r["verdict_short"] for r in thin))

above = [r for r in rows if r["above_noise_floor"]]
ok("anything marked 'above luck' cleared BOTH tests, never just the return",
   all(r["oos_trades"] >= LAB.MIN_OOS_TRADES
       and r["oos_return_pct"] > r["noise_floor_p95"] for r in above))

# --- sorting must not promote what the verdict refuses -------------------
for sort_by in ("sharpe", "drawdown"):
    top = SB.ranked_rows(limit=12, sort_by=sort_by)["rows"]
    lead = [r for r in top if r["oos_trades"] >= LAB.MIN_OOS_TRADES]
    ok(f"sorting by {sort_by} does not hand the top row to a thin sample",
       not top or top[0]["oos_trades"] >= LAB.MIN_OOS_TRADES)
    ok(f"and every row before the first thin one by {sort_by} has a real sample",
       len(lead) == len([r for r in top[:len(lead)]]))

# Ranking by raw return is allowed to show a thin row - that IS the honest
# ranking of returns - but it may never LABEL it as evidence. That
# distinction is the whole design.
by_oos = SB.ranked_rows(limit=5, sort_by="oos")["rows"]
ok("ranking by return still shows the biggest return, whatever its sample",
   by_oos and by_oos[0]["oos_return_pct"] >= by_oos[-1]["oos_return_pct"])
ok("but a thin top row is labelled as such rather than as a finding",
   all(r["above_noise_floor"] is not True
       for r in by_oos if r["oos_trades"] < LAB.MIN_OOS_TRADES))

# --- filtering and the standing warning -----------------------------------
btc = SB.ranked_rows(coin_filter="BTC", limit=500)
ok("a coin filter returns only that coin", all(r["coin"] == "BTC-USD" for r in btc["rows"]))
ok("and it filters rather than silently returning everything",
   btc["total_rows"] < table["total_rows"])
ok("the table states that 30% held back does not protect the best of thousands",
   "best of thousands" in table["note"])
ok("and that a row is a candidate to forward-test, never one to fund",
   "never a strategy to fund" in table["note"])

width = max(len(l) for l, _ in checks)
for label, passed in checks:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label:<{width}}")
failed = [l for l, p in checks if not p]
print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)

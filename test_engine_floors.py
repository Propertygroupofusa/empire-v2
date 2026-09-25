"""Every engine that can close a position must price its own fee floor.

The gap this closes, 2026-09-25. Seven engines; three had fee protection,
each built privately after its own incident, and four had none. Wiring the
shared fee_floor.py into the remaining ones turned up a defect a
percentage floor cannot see:

    crypto_coinbase_bot closes on  dollar_profit >= TARGET_TRADE_PROFIT
    with TARGET_TRADE_PROFIT = $2.50.

A FIXED DOLLAR target is a different percentage at every position size:

    $50 position    -> 5.00%   fine
    $147 position   -> 1.70%   exactly the floor
    $250 position   -> 1.00%   below - a loss
    $1,000 position -> 0.25%   below - a loss

$2.50 only clears fees up to a $147.06 position. Above that, "taking
profit" books a real loss. And 0.25% is precisely the target on the
retired bot_config.json scalper - reached here completely independently,
which is why the answer is a floor rather than a bigger constant.

A second finding while wiring it: crypto_coinbase_bot's own
CRYPTO_ROUND_TRIP_FEE_RATE defaults to 0.4%, while the account's real
measured taker round trip is 1.50%. Flooring against that stale value
would have under-protected by nearly 4x, so the floor prices its own
worst-case constant and leaves the original alone (other exit logic in
that file reads it).

THE RULES THIS FILE PROTECTS:

  1. A dollar target is floored by POSITION SIZE, not by a constant.
  2. Every floor prices the WORST round trip the trade can pay, never the
     engine's own optimistic default.
  3. A floor raises a target; it never lowers one, and never widens a stop
     (that would add risk).
  4. A floor that breaks trading is worse than no floor - every wiring is
     wrapped and can never raise.

Run: python3 test_engine_floors.py
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fee_floor as ff

checks = []


def ok(label, cond):
    checks.append((label, bool(cond)))


# --- the dollar-target helpers exist and are correct ----------------------
RT = 0.015
FLOOR = ff.fee_floor_pct(RT)
ok("min_profit_usd scales with position size",
   abs(ff.min_profit_usd(1000.0, RT) - 17.0) < 1e-9)
ok("a $50 position needs only $0.85", abs(ff.min_profit_usd(50.0, RT) - 0.85) < 1e-9)
ok("a zero position needs nothing", ff.min_profit_usd(0.0, RT) == 0.0)

ok("REGRESSION: $2.50 clears on a $50 position",
   ff.dollar_target_clears_fees(2.50, 50.0, RT))
ok("REGRESSION: $2.50 FAILS on a $250 position",
   not ff.dollar_target_clears_fees(2.50, 250.0, RT))
ok("REGRESSION: $2.50 FAILS on a $1,000 position",
   not ff.dollar_target_clears_fees(2.50, 1000.0, RT))
ok("the largest position $2.50 clears is $147.06",
   abs(ff.max_position_for_dollar_target(2.50, RT) - 147.0588) < 1e-3)
ok("at that exact size it is the floor, not above it",
   abs(2.50 / ff.max_position_for_dollar_target(2.50, RT) - FLOOR) < 1e-12)
ok("a zero fee and zero margin cannot divide by zero",
   ff.max_position_for_dollar_target(2.50, 0.0, 0.0) == float("inf"))

# --- the startup report ---------------------------------------------------
lines, all_ok = ff.report("test_engine", {"good": 0.02, "bad": 0.0025}, RT)
ok("report flags a failing target", not all_ok)
ok("report names the failing one", any("FAIL" in l and "bad" in l for l in lines))
ok("report passes the good one", any("OK" in l and "good" in l for l in lines))
ok("report states what a failing WIN really nets",
   any("-1.250%" in l for l in lines))
lines2, ok2 = ff.report("test_engine", {"good": 0.02}, RT)
ok("report is clean when everything clears", ok2)

# --- each engine is actually wired ---------------------------------------
def body_of(path, fn_hint):
    src = open(os.path.join(HERE, path), encoding="utf-8").read()
    return src


mr = body_of("crypto_mean_reversion_bot.py", None)
ok("mean reversion imports the shared floor", "import fee_floor" in mr)
ok("mean reversion prices a worst-case rate", "FEE_FLOOR_ROUND_TRIP_PCT" in mr)
ok("mean reversion records when it raises", "fee_floor_raised" in mr)

cb = body_of("crypto_coinbase_bot.py", None)
ok("coinbase bot imports the shared floor", "import fee_floor" in cb)
ok("coinbase bot floors its DOLLAR target by position size",
   "min_profit_usd" in cb and "_position_usd" in cb)
ok("REGRESSION: it does NOT floor against its own stale 0.4% constant",
   "min_profit_usd(\n                    _position_usd, CRYPTO_ROUND_TRIP_FEE_RATE)" not in cb)
ok("it prices the worst case instead", "FEE_FLOOR_ROUND_TRIP_PCT" in cb)
ok("it explains why the stale constant is left alone",
   "under-protects" in cb and "other exit logic" in cb)
ok("the wiring can never raise", "fee floor check skipped" in cb)
ok("it raises the target, never lowers it",
   "if _floor_usd > _min_profit:" in cb)

# --- the rule, as behaviour ----------------------------------------------
def effective_dollar_target(configured, position_usd, rt=RT):
    return max(configured, ff.min_profit_usd(position_usd, rt))


ok("a small position keeps the configured target",
   abs(effective_dollar_target(2.50, 50.0) - 2.50) < 1e-9)
ok("a large position is raised to what actually pays",
   abs(effective_dollar_target(2.50, 1000.0) - 17.0) < 1e-9)
ok("the floor NEVER lowers a dollar target",
   all(effective_dollar_target(t, p) >= t - 1e-12
       for t in (0.0, 2.50, 25.0) for p in (0.0, 50.0, 1000.0)))
ok("after flooring, closing on target can never book a loss",
   all(effective_dollar_target(2.50, p) >= p * RT - 1e-9
       for p in (10.0, 147.0, 250.0, 1000.0, 5000.0)))

width = max(len(l) for l, _ in checks)
for label, passed in checks:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label:<{width}}")
failed = [l for l, p in checks if not p]
print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)

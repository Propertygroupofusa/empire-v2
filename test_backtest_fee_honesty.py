"""A backtest that understates fees recommends losing strategies, confidently.

Every backtest in crypto_selection_backtest.py charged
engine.ROUND_TRIP_FEE_RATE, which is 0.008 - documented in its own source
as "~0.4% each way, taker".

Measured from Coinbase's own fill records on 2026-09-25: the real taker
leg is 0.75%, so a taker round trip is 1.50%. Maker is 0.35%/leg, 0.70%
round trip. The backtests were understating the cost of every completed
round trip by 0.70 percentage points.

For a grid, whose entire edge is (step - fees), that is not a rounding
error - it moves the break-even point by nearly a full percentage point:

    step     backtest net     real taker net
    1.00%         +0.20%             -0.50%
    1.25%         +0.45%             -0.25%
    2.00%         +1.20%             +0.50%    (2.4x overstated)

EVERY STEP BETWEEN 0.80% AND 1.50% BACKTESTED AS PROFITABLE AND LOSES
MONEY LIVE. That band contains the 0.5% spacing and 0.75% profit target
the retracted GRID_BOT_README called "Production Ready", and it is why
tighter spacing has always looked good on paper in this repo.

THE RULES THIS FILE PROTECTS:

  1. No backtest charges the old 0.8% assumption.
  2. The default rate is TAKER, because an unfilled post-only order
     becomes a market order - a backtest must model the worst case it can
     actually hit, the same rule the live floor applies.
  3. A step below the fee floor can never be reported as profitable.

Run: python3 test_backtest_fee_honesty.py
"""

import io
import re
import sys

sys.path.insert(0, ".")

FAILURES = []
CHECKS = 0


def ok(label, cond, detail=""):
    global CHECKS
    CHECKS += 1
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}" + (f"  ({detail})" if detail else ""))
        FAILURES.append(label)


SRC = io.open("crypto_selection_backtest.py", encoding="utf-8").read()

print("test_backtest_fee_honesty.py")
print()

# ── 1. the old assumption is gone ──────────────────────────────────────
print("-- the 0.8% assumption is gone from every backtest --")
ok("no backtest charges engine.ROUND_TRIP_FEE_RATE any more",
   "engine.ROUND_TRIP_FEE_RATE" not in SRC)
ok("a single named rate is used instead",
   SRC.count("BACKTEST_ROUND_TRIP_FEE_RATE") >= 16,
   f"{SRC.count('BACKTEST_ROUND_TRIP_FEE_RATE')} uses")
ok("the real taker rate is defined", "REAL_TAKER_ROUND_TRIP_FEE_RATE" in SRC)
ok("the real maker rate is defined", "REAL_MAKER_ROUND_TRIP_FEE_RATE" in SRC)

# The engine constant itself is still wrong; the point is nothing prices against it.
ENG = io.open("crypto_btc_compound_bot.py", encoding="utf-8").read()
m = re.search(r"^ROUND_TRIP_FEE_RATE\s*=.*$", ENG, re.M)
ok("the old engine constant still exists (other code may read it)", m is not None)

# ── 2. the default models the worst case that can actually happen ──────
print()
print("-- the default is TAKER, because that is what a missed maker order becomes --")
import crypto_selection_backtest as bt

ok("the default round trip is the taker rate",
   abs(bt.BACKTEST_ROUND_TRIP_FEE_RATE - bt.REAL_TAKER_ROUND_TRIP_FEE_RATE) < 1e-12,
   f"{bt.BACKTEST_ROUND_TRIP_FEE_RATE}")
ok("taker is 1.50%, as measured from real fills",
   abs(bt.REAL_TAKER_ROUND_TRIP_FEE_RATE - 0.015) < 1e-9,
   f"{bt.REAL_TAKER_ROUND_TRIP_FEE_RATE}")
ok("maker is 0.70%, as measured from real fills",
   abs(bt.REAL_MAKER_ROUND_TRIP_FEE_RATE - 0.007) < 1e-9,
   f"{bt.REAL_MAKER_ROUND_TRIP_FEE_RATE}")
ok("the default is not the old 0.8%",
   abs(bt.BACKTEST_ROUND_TRIP_FEE_RATE - 0.008) > 1e-9)

# ── 3. a sub-floor step cannot be reported as a win ────────────────────
print()
print("-- a step below the fee floor is an impossibility, not a finding --")
floor = bt.fee_floor_for_backtest()
ok("the taker floor is 1.70%", abs(floor - 0.017) < 1e-9, f"{floor}")
maker_floor = bt.fee_floor_for_backtest(bt.REAL_MAKER_ROUND_TRIP_FEE_RATE)
ok("the maker floor is 0.90%", abs(maker_floor - 0.009) < 1e-9, f"{maker_floor}")

# The exact band that used to backtest as profitable and loses money live.
for step in [0.005, 0.0075, 0.01, 0.0125, 0.015]:
    ok(f"a {step * 100:.2f}% step does not clear taker fees",
       not bt.clears_fees(step))
for step in [0.017, 0.02, 0.025, 0.03]:
    ok(f"a {step * 100:.2f}% step does clear taker fees", bt.clears_fees(step))

# Under maker, the tighter steps become legitimate - which is the whole
# reason the maker question mattered.
for step in [0.01, 0.0125]:
    ok(f"a {step * 100:.2f}% step clears MAKER fees",
       bt.clears_fees(step, bt.REAL_MAKER_ROUND_TRIP_FEE_RATE))

# ── 4. the arithmetic that was being hidden ────────────────────────────
print()
print("-- what the old rate hid --")
OLD = 0.008
# At a step exactly equal to the round-trip rate the trade nets ZERO -
# not a win. The first version of this file asserted 1.50% was a real
# win, which is the same optimism-by-one-comparison the old fee rate
# encoded. Break-even is a loss once any slippage lands.
for step, was_positive, really_positive in [
    (0.010, True, False),
    (0.0125, True, False),
    (0.015, True, False),
    (0.020, True, True),
]:
    ok(f"{step * 100:.2f}%: old rate said {'win' if was_positive else 'loss'}",
       (step - OLD > 0) == was_positive)
    ok(f"{step * 100:.2f}%: real taker says {'win' if really_positive else 'LOSS'}",
       (step - bt.REAL_TAKER_ROUND_TRIP_FEE_RATE > 0) == really_positive)

ok("a step exactly equal to the fee rate nets zero, not a profit",
   abs(0.015 - bt.REAL_TAKER_ROUND_TRIP_FEE_RATE) < 1e-12)
ok("and break-even still fails the floor, which demands a real margin",
   not bt.clears_fees(bt.REAL_TAKER_ROUND_TRIP_FEE_RATE))

ok("the 2.00% step the fleet runs is overstated 2.4x by the old rate",
   abs(((0.02 - OLD) / (0.02 - bt.REAL_TAKER_ROUND_TRIP_FEE_RATE)) - 2.4) < 0.05)

# ── 5. the stale session binding, in a second file ─────────────────────
print()
print("-- the backtest can actually reach the database --")
ok("it imports the factory, not the None-at-import value",
   "from database import AsyncSessionLocal" not in SRC)
ok("it resolves the factory at call time",
   "get_session_factory()()" in SRC and "AsyncSessionLocal()" not in SRC)

print()
print(f"{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
if FAILURES:
    print("FAILED:")
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("All checks passed.")

"""One rate, measured, everywhere. A stale fee is a live loss, not a typo.

Every fee number in this codebase was a guess, and every guess was
optimistic. Measured 2026-09-25 from Coinbase's own fill records
(liquidity_indicator and commission per fill, not inference):

    taker   0.75% per leg   ->  1.50% round trip
    maker   0.35% per leg   ->  0.70% round trip

WHAT THE GUESSES COST

  crypto_coinbase_bot.CRYPTO_ROUND_TRIP_FEE_RATE was 0.004 (0.4%), and it
  is not decorative - three LIVE exit conditions read it:

      rsi_exit = rsi > RSI_SELL_ABOVE and unrealized_pct > CRYPTO_ROUND_TRIP_FEE_RATE

  "sell once unrealized profit clears the round trip." At 0.4% that fired
  on a 0.5% gain, paid 1.50% in fees, and booked a real -1.00% LOSS while
  logging a profitable exit. That is precisely the pattern that cost the
  retired family tree $102.60 across 26 "TARGET HIT" trades: wins that
  lost money.

  crypto_btc_compound_bot.ROUND_TRIP_FEE_RATE was 0.008, documented as
  "~0.4% each way, taker" - about half the truth. crypto_family_tree_bot
  re-exports it to price exit fees, and the dashboard shows a per-trade
  fee estimate from it.

  The backtests charged the same 0.008, which made every step between
  0.80% and 1.50% look profitable when it loses money live.

An earlier pass added FEE_FLOOR_ROUND_TRIP_PCT at the real rate but
deliberately left CRYPTO_ROUND_TRIP_FEE_RATE alone "because other exit
logic reads it". That was the wrong call - the other exit logic reading it
was the problem, not a reason to keep it.

THE RULE: no fee constant anywhere may be below the measured rate. Being
wrong high costs a missed trade. Being wrong low costs money on every
completed round trip.

Run: python3 test_real_fee_rate.py
"""

import io
import re
import sys

FAILURES = []
CHECKS = 0

TAKER_ROUND_TRIP = 0.015
MAKER_ROUND_TRIP = 0.007


def ok(label, cond, detail=""):
    global CHECKS
    CHECKS += 1
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}" + (f"  ({detail})" if detail else ""))
        FAILURES.append(label)


def const(path, name):
    """The default in `X = _safe_float_env("KEY", "0.015")` or `X = 0.015`."""
    src = io.open(path, encoding="utf-8").read()
    # Allow leading indentation: CRYPTO_ROUND_TRIP_FEE_RATE is assigned
    # inside a try/except, so anchoring at column 0 found nothing and the
    # check reported "not defined" for a constant sitting right there.
    m = re.search(rf'^\s*{re.escape(name)}\s*=\s*_safe_float_env\([^,]+,\s*"([\d.]+)"\)', src, re.M)
    if m:
        return float(m.group(1))
    # Some modules read the env directly rather than through the helper.
    m = re.search(rf'^\s*{re.escape(name)}\s*=\s*float\(os\.getenv\([^,]+,\s*"([\d.]+)"\)\)', src, re.M)
    if m:
        return float(m.group(1))
    m = re.search(rf'^\s*{re.escape(name)}\s*=\s*([\d.]+)\s*(?:#.*)?$', src, re.M)
    return float(m.group(1)) if m else None


print("test_real_fee_rate.py")
print()

print("-- no fee constant sits below the measured round trip --")
FEE_CONSTANTS = [
    ("crypto_coinbase_bot.py", "CRYPTO_ROUND_TRIP_FEE_RATE"),
    ("crypto_coinbase_bot.py", "FEE_FLOOR_ROUND_TRIP_PCT"),
    ("crypto_btc_compound_bot.py", "ROUND_TRIP_FEE_RATE"),
    ("crypto_mean_reversion_bot.py", "FEE_FLOOR_ROUND_TRIP_PCT"),
]
for path, name in FEE_CONSTANTS:
    v = const(path, name)
    ok(f"{path}:{name} is defined", v is not None)
    if v is not None:
        ok(f"{path}:{name} = {v} is not below the measured {TAKER_ROUND_TRIP}",
           v >= TAKER_ROUND_TRIP - 1e-12, f"{v}")

# The specific stale values, named so they cannot quietly return.
for path, name, stale in [
    ("crypto_coinbase_bot.py", "CRYPTO_ROUND_TRIP_FEE_RATE", 0.004),
    ("crypto_btc_compound_bot.py", "ROUND_TRIP_FEE_RATE", 0.008),
]:
    v = const(path, name)
    ok(f"{name} is no longer the old {stale}", v is None or abs(v - stale) > 1e-12)

print()
print("-- the live exits that read it now demand a real profit --")
CB = io.open("crypto_coinbase_bot.py", encoding="utf-8").read()
n_exits = len(re.findall(r"unrealized_pct > CRYPTO_ROUND_TRIP_FEE_RATE", CB))
ok("the RSI exits still gate on clearing the round trip", n_exits >= 3, f"{n_exits} found")
rate = const("crypto_coinbase_bot.py", "CRYPTO_ROUND_TRIP_FEE_RATE")
ok("so they now require more than 1.50%, not more than 0.4%",
   rate is not None and rate >= TAKER_ROUND_TRIP - 1e-12)

# The arithmetic those exits were doing.
for gain, old_fires, really_nets in [(0.005, True, -0.010), (0.010, True, -0.005), (0.020, True, 0.005)]:
    ok(f"a {gain * 100:.1f}% gain nets {really_nets * 100:+.1f}% after real fees",
       abs((gain - TAKER_ROUND_TRIP) - really_nets) < 1e-9)
    ok(f"  at the old 0.4% it would have sold there: {old_fires}",
       (gain > 0.004) == old_fires)
    ok(f"  at the measured rate it only sells if that is a real gain",
       (gain > TAKER_ROUND_TRIP) == (really_nets > 0))

print()
print("-- the documented rates match the measured ones --")
# A stale rate must not be DOCUMENTED as current. Quoting the old value
# while explaining what it cost is the opposite - that is the record. So
# check the live constant, not whether the digits appear anywhere: the
# first version of this failed on its own explanation of the fix.
for path, wrong in [
    ("crypto_grid_bot.py", "1.2%/leg"),
    ("routers/trading_dashboard.py", "1.2%/leg"),
]:
    src = io.open(path, encoding="utf-8").read()
    ok(f"{path} no longer documents {wrong!r} as current", wrong not in src)

eng = io.open("crypto_btc_compound_bot.py", encoding="utf-8").read()
decl = re.search(r'^ROUND_TRIP_FEE_RATE\s*=.*$', eng, re.M)
ok("the engine's fee declaration line carries no stale '0.4% each way' note",
   decl is not None and "0.4% each way" not in decl.group(0), decl.group(0) if decl else "")
ok("the engine explains what the old rate was, as history",
   '"~0.4% each way, taker"' in eng)

for path in ["crypto_grid_bot.py", "routers/trading_dashboard.py"]:
    src = io.open(path, encoding="utf-8").read()
    ok(f"{path} names the measured 0.75%/leg taker", "0.75%/leg taker" in src)
    ok(f"{path} names the measured 0.35%/leg maker", "0.35%/leg maker" in src)

print()
print("-- maker remains the cheaper path, and the floor follows the rate --")
ok("maker round trip is less than half the taker round trip",
   MAKER_ROUND_TRIP < TAKER_ROUND_TRIP / 2 + 1e-9)
import sys as _s
_s.path.insert(0, ".")
import fee_floor
ok("the taker floor is 1.70%", abs(fee_floor.fee_floor_pct(TAKER_ROUND_TRIP) - 0.017) < 1e-9)
ok("the maker floor is 0.90%", abs(fee_floor.fee_floor_pct(MAKER_ROUND_TRIP) - 0.009) < 1e-9)
ok("a 2.00% step clears the taker floor", fee_floor.clears_fees(0.02, TAKER_ROUND_TRIP))
ok("a 1.25% step does NOT clear the taker floor", not fee_floor.clears_fees(0.0125, TAKER_ROUND_TRIP))
ok("a 1.25% step DOES clear the maker floor", fee_floor.clears_fees(0.0125, MAKER_ROUND_TRIP))

print()
print(f"{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
if FAILURES:
    print("FAILED:")
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("All checks passed.")

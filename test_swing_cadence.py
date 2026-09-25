"""The Alpaca swing bot's entry cadence must be adjustable and never ragged.

Before 2026-09-25 the intraday check was hardcoded to `now.minute % 15 == 0`
- four entry checks an hour, 26 a trading day, across an 11-symbol universe.
That was the binding constraint on how often the bot could take a setup at
all: a dip that formed and recovered inside a 15-minute gap was never seen.

Loosened to 5 minutes by default, which is safe HERE in a way it would not
be on the crypto side: Alpaca equities are commission-free, so more frequent
entries do not pay the 1% round-trip fee a Coinbase grid trade does.

THE TRAP THIS GUARDS: an interval that does not divide 60 produces uneven
gaps. Seven gives :00 :07 :14 :21 :28 :35 :42 :49 :56 -> :00 - six
seven-minute gaps and one of four. A bot trading on a ragged clock samples
the market unevenly, which quietly biases every entry statistic computed
from it. Bad values fall back to 5 rather than trading on that clock.

Run: python3 test_swing_cadence.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
checks = []


def ok(label, cond):
    checks.append((label, bool(cond)))


# Exec just the parser - importing the whole module pulls heavy trading deps.
src = open(os.path.join(HERE, "alpaca_swing_bot.py"), encoding="utf-8").read()
start = src.index("def _intraday_interval_minutes")
end = src.index("SWING_SYMBOLS = {")
ns = {"os": os}
exec(src[start:end], ns)
interval = ns["_intraday_interval_minutes"]


def with_env(value):
    if value is None:
        os.environ.pop("SWING_INTRADAY_INTERVAL_MINUTES", None)
    else:
        os.environ["SWING_INTRADAY_INTERVAL_MINUTES"] = value
    try:
        return interval()
    finally:
        os.environ.pop("SWING_INTRADAY_INTERVAL_MINUTES", None)


# --- the default ---------------------------------------------------------
ok("defaults to 5 minutes when unset", with_env(None) == 5)
ok("the default is faster than the old hardcoded 15", with_env(None) < 15)
ok("5 minutes means 12 entry checks an hour (was 4)", 60 // with_env(None) == 12)

# --- valid divisors of 60 are honoured ------------------------------------
for v in ["1", "2", "3", "4", "5", "6", "10", "12", "15", "20", "30", "60"]:
    ok(f"{v} divides 60 and is honoured", with_env(v) == int(v))

# --- everything else falls back, never trades on a ragged clock -----------
for bad, why in [("7", "does not divide 60 - uneven gaps"),
                 ("8", "does not divide 60"),
                 ("9", "does not divide 60"),
                 ("0", "would mean every tick"),
                 ("-5", "negative"),
                 ("61", "longer than an hour"),
                 ("abc", "not a number"),
                 ("", "empty"),
                 ("5.5", "not an integer")]:
    ok(f"{bad!r} rejected ({why})", with_env(bad) == 5)

ok("surrounding whitespace is tolerated", with_env("  10  ") == 10)

# --- the call site actually uses it --------------------------------------
ok("the loop uses the constant, not a hardcoded 15",
   "now.minute % SWING_INTRADAY_INTERVAL_MINUTES == 0" in src)
ok("no hardcoded 15-minute modulo remains", "now.minute % 15 == 0" not in src)
ok("the constant is bound at module scope",
   "SWING_INTRADAY_INTERVAL_MINUTES = _intraday_interval_minutes()" in src)

# --- market-hours gating must NOT have been loosened ---------------------
# Faster scanning inside market hours is the goal. Scanning outside them is
# a bug - the orders would rest or reject, and the bot would look broken.
ok("market-hours gating is still present", "is_market_open" in src)
ok("still weekdays only", "now.weekday() < 5" in src)
ok("still bounded by the 09:30 open", '"09:30"' in src)
ok("still bounded by the 16:00 close", '"16:00"' in src)

# --- passive mode must still stop it -------------------------------------
ok("passive mode still short-circuits the loop", "is_alpaca_passive_mode" in src)

width = max(len(l) for l, _ in checks)
for label, passed in checks:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label:<{width}}")
failed = [l for l, p in checks if not p]
print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)

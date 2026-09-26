"""prop_bot's Alpaca order formatting, and the DAY-only rule it broke on.

THE BUG THIS PINS

execute_futures_trade sent `time_in_force = "gtc" if action == "SELL"`.
Alpaca rejects a FRACTIONAL quantity with anything but DAY, and this bot
sizes positions in dollars, so its quantities are fractional nearly
always. Every SELL therefore failed - target exits, stop exits and
max-hold exits alike - and retried forever.

Confirmed live 2026-09-24:
    SH: Max hold time exceeded: 90505s >= 86400s
    Futures order failed: fractional orders must be DAY orders
A position sat open more than a day past its own exit rule.

Run: python3 test_order_qty.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = open(os.path.join(HERE, "prop_bot.py")).read()

ns = {}
exec(re.search(r"def format_order_qty\(qty\):.*?\n    return f\"\{rounded:\.9f\}\""
               r"\.rstrip\(\"0\"\)\.rstrip\(\"\.\"\), True\n", SRC, re.S).group(0), ns)
fmt = ns["format_order_qty"]

checks = []


def ok(label, cond):
    checks.append((label, bool(cond)))


# Comments are stripped before structural checks. The fix's own comment
# quotes the expression it replaced, in order to explain it - matching
# against raw source flagged that explanation as the bug. Match the CODE.
CODE = "\n".join(
    line for line in SRC.splitlines()
    if not line.lstrip().startswith("#")
)

# --- the rule that broke ---------------------------------------------------
ok("time_in_force is no longer conditional on the side",
   '"gtc" if action == "SELL"' not in CODE)
ok("and no gtc survives in this file's order bodies",
   '"gtc"' not in CODE)
ok("every equity market order goes out as DAY", 'time_in_force = "day"' in CODE)
ok("the reason is recorded where the next reader will find it",
   "fractional orders must be DAY orders" in SRC)
ok("the order is refused outright when the quantity is unusable",
   "not a usable" in SRC and "No order was placed" in SRC)
ok("a rejection names the order, not just the API message",
   "tif={time_in_force}" in SRC and "fractional={is_fractional}" in SRC)
ok("the sent quantity is the formatted one, never the raw float",
   '"qty": qty_str' in CODE and '"qty": str(qty)' not in CODE)

# --- formatting ------------------------------------------------------------
s, frac = fmt(12.3456)
ok("a fractional qty formats plainly", s == "12.3456" and frac is True)

s, frac = fmt(3.0)
ok("a whole float sheds its decimal point", s == "3")
ok("and is not treated as fractional", frac is False)

s, frac = fmt(3)
ok("a plain int works too", s == "3" and frac is False)

s, frac = fmt(1e-05)
ok("a tiny qty never goes out in scientific notation", s == "0.00001" and "e" not in s)
ok("and is flagged fractional", frac is True)

s, frac = fmt(12.3456789012)
ok("excess precision is rounded to Alpaca's 9 dp limit",
   len(s.split(".")[1]) <= 9)
ok("and the value survives the rounding", abs(float(s) - 12.3456789012) < 1e-9)

s, frac = fmt(2.500000000001)
ok("float noise past 9 dp collapses to the clean number", s == "2.5")

s, frac = fmt(0.1 + 0.2)
ok("classic float error does not produce a 17-digit string",
   len(s.replace(".", "")) <= 10 and float(s) > 0)

# --- refusals --------------------------------------------------------------
for bad, why in [(0, "zero"), (-1.5, "negative"), (None, "None"), ("abc", "a string"),
                 (float("nan"), "NaN"), (float("inf"), "infinity"),
                 (1e-12, "smaller than 9 dp can express")]:
    s, frac = fmt(bad)
    ok(f"{why} is refused, not sent", s is None)

# --- the property that matters ---------------------------------------------
# Whatever comes back must be parseable and must agree with the flag.
for value in (12.3456, 3.0, 1e-05, 0.5, 100.0, 7.123456789):
    s, frac = fmt(value)
    ok(f"{value!r} round-trips and its flag matches",
       s is not None and abs(float(s) - round(value, 9)) < 1e-9
       and frac == (round(value, 9) != int(round(value, 9))))

width = max(len(l) for l, _ in checks)
for label, passed in checks:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label:<{width}}")
failed = [l for l, p in checks if not p]
print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)

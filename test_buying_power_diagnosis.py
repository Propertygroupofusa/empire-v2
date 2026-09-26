"""A halt that does not say why is the least useful line in the file.

2026-09-26 07:29Z, repeating every prop cycle:

    [CRITICAL] [KILL CONDITION] Halting bot: Buying power critical:
    $77.08 < $150

True, and useless. The account held $810.64 in cash and $1,007.47 in
equity at the time, so the number was not about being broke. Four things
produce that shape and they have completely different remedies:

    unsettled funds        transient, clears on its own
    held for open orders   clears when they fill or cancel
    PDT restriction        persistent under $25k equity; waiting never fixes it
    pending transfer in    money that has not arrived

Alpaca returns every one of those on the SAME call that returns
buying_power. The bot was reading one field and discarding the rest, so
the one line that fires when trading stops carried no diagnosis.

These tests assert the diagnosis is real and cannot break trading.

Run: python3 test_buying_power_diagnosis.py
"""
import ast
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = open(os.path.join(HERE, "prop_bot.py"), encoding="utf-8").read()
_p = _f = 0


def ok(label, cond, detail=""):
    global _p, _f
    if cond:
        _p += 1
        print(f"  PASS  {label}")
    else:
        _f += 1
        print(f"  FAIL  {label}" + (f"  -- {detail}" if detail else ""))


ns = {"log": __import__("logging").getLogger("t")}
exec(re.search(r"def explain_low_buying_power.*?(?=\n\nasync def |\n\ndef )",
               SRC, re.S).group(0), ns)
explain = ns["explain_low_buying_power"]


print("\nit reads the fields that explain the number")
for field in ("multiplier", "pattern_day_trader", "daytrade_count",
              "pending_transfer_in", "non_marginable_buying_power",
              "daytrading_buying_power", "regt_buying_power",
              "trading_blocked", "account_blocked"):
    ok(f"  captures {field}", f'"{field}"' in SRC,
       "Alpaca returns it on the same call; not capturing it was the bug")

print("\nthe signature is unchanged for every existing caller")
tree = ast.parse(SRC)
fn = next(n for n in ast.walk(tree)
          if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
          and n.name == "get_account_buying_power")
ok("detail_out is OPTIONAL, so old call sites still work",
   any(a.arg == "detail_out" for a in fn.args.args) and len(fn.args.defaults) >= 1)
ok("it still returns a float or None, not a tuple",
   "return bp" in SRC and "return bp, " not in SRC,
   "a changed return type would break the kill check itself")

print("\nthe ranking is by what you would DO, not by likelihood")
ok("a blocked account outranks everything",
   "ACCOUNT IS BLOCKED" in explain(0, {"account_blocked": "true",
                                       "pattern_day_trader": "true", "cash": "800"}))
ok("blocked TRADING is distinguished from a capital problem",
   "not a capital problem" in explain(0, {"trading_blocked": "true", "cash": "800"}))
ok("PDT under $25k is named first among capital causes, and as persistent",
   "PATTERN DAY TRADER" in explain(77.08, {"equity": "1007.47",
                                           "pattern_day_trader": "true", "cash": "810.64"})
   and "Waiting does not fix this one" in explain(77.08, {"equity": "1007.47",
                                                          "pattern_day_trader": "true",
                                                          "cash": "810.64"}))
ok("PDT on a LARGE account is not blamed - the rule is about the $25k line",
   "PATTERN DAY TRADER" not in explain(77.08, {"equity": "90000",
                                               "pattern_day_trader": "true", "cash": "810"}))

print("\nit quantifies the gap rather than gesturing at it")
msg = explain(77.08, {"equity": "1007.47", "pattern_day_trader": "false",
                      "cash": "810.64", "multiplier": "1", "daytrade_count": 3})
ok("it states how much of the cash is unavailable", "733.56" in msg, msg)
ok("a cash account is called out, with the settlement reason",
   "CASH account" in msg and "settle" in msg)
ok("the day-trade count travels with it", "day-trade count 3" in msg)
ok("a pending transfer is named when present",
   "transferring in" in explain(0, {"cash": "0", "pending_transfer_in": "500"}))

print("\nit cannot break the halt it describes")
ok("no detail is a plain sentence, not an exception",
   explain(77.08, {}) == "no account detail captured")
ok("garbage in the fields cannot raise",
   isinstance(explain(1, {"equity": "not-a-number", "cash": "x"}), str))
ok("the whole helper is wrapped", "except Exception as e:" in
   re.search(r"def explain_low_buying_power.*?(?=\n\nasync def |\n\ndef )", SRC, re.S).group(0))
ok("the explanation is APPENDED to the halt, never replaces it",
   'f"[KILL CONDITION] Halting bot: {halt_reason}"' in SRC,
   "the original reason must survive verbatim")
ok("and it is only computed for a BUYING POWER halt",
   '"Buying power" in (halt_reason or "")' in SRC,
   "a daily-loss halt has nothing to do with these fields")

print("\nthe halt itself is unchanged - this is diagnosis, not policy")
ok("the thresholds are untouched",
   'if buying_power < capital["critical_buying_power"]' in SRC)
ok("it still returns from the cycle rather than trading on",
   re.search(r"log\.critical\(f?\"\[KILL CONDITION\].*?\n(?:.*?\n)??\s+return", SRC, re.S) is not None)


print("\nthe diagnosis reaches the dashboard, not just the log")
ROUTER = open(os.path.join(HERE, "routers/trading_dashboard.py"), encoding="utf-8").read()
PAGE = open(os.path.join(HERE, "alpaca_dashboard.html"), encoding="utf-8").read()

ok("the endpoint serves buying power", '"buying_power": _num(' in ROUTER)
ok("and the floor it is judged against", '"buying_power_floor"' in ROUTER)
ok("and whether that is actually halting trading", '"buying_power_halted"' in ROUTER)
ok("and the reason", '"buying_power_reason"' in ROUTER)
ok("the floor is read from the mandate, never repeated",
   'APEX_MANDATE["capital"]["critical_buying_power"]' in ROUTER,
   "two copies of a threshold drift, and the dashboard's copy drifts unseen")
ok("the reason DELEGATES to prop_bot rather than re-deriving it",
   "prop_bot_module.explain_low_buying_power(" in ROUTER,
   "a second copy of the reasoning is a second thing to keep correct")
ok("a healthy account returns no reason at all",
   "if bp is None or not _bp_halted(account)" in ROUTER,
   "a permanent explanation on a healthy account is noise")
ok("the reason cannot break the endpoint",
   "log.debug(f\"buying-power reason unavailable" in ROUTER)

ok("the page has a buying-power tile", 'id="stat-bp"' in PAGE)
ok("it is coloured only while actually halting, not permanently",
   "data.buying_power_halted ? ' neg' : ''" in PAGE)
ok("a halt banner exists and is hidden by default",
   'id="bp-banner" hidden' in PAGE)
ok("the banner states the number, the floor AND the cash beside it",
   "buying_power_floor" in PAGE and "data.cash" in PAGE,
   "$77 alone reads as broke; $77 against $810 cash is the real question")
ok("it shows the reason when there is one",
   "data.buying_power_reason" in PAGE)
ok("and says so plainly when no field explains it",
   "No account field explains it" in PAGE,
   "an unexplained halt is itself a finding, not a blank")

print(f"\n{_p} passed, {_f} failed")
sys.exit(1 if _f else 0)

"""An unreadable product must never become an order.

WHY IT EXISTS. The execution path sized orders with
math.floor(qty * 10**decimals) / 10**decimals, where `decimals` came from
a helper that returns 8 on ANY failure. That is float arithmetic against a
decimal rule, a decimal COUNT standing in for an increment, no notional
floor at all, and - the live safety bug - a guess substituted for metadata
it could not read.

WHAT THESE CHECKS PIN. Mostly the refusals. An order that should not exist
is the expensive kind of wrong, so every path that cannot establish a rule
has to refuse rather than assume, and DUST has to stay distinguishable
from both a failure and an empty wallet.

The product figures below are REAL, read from the venue on 2026-09-29:
QNT-USD 0.001, ALGO-USD 0.1, PEPE-USD 1, XLM-USD 0.00000001. The QNT case
is the one from the live log.
"""
import sys
from decimal import Decimal

import execution_quantity as eq

failures = []


def ok(label, cond, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}{(' - ' + detail) if detail else ''}")
        failures.append(label)


def plan(**kw):
    # The default request is deliberately unbounded. An earlier default of
    # 1,000,000 was SMALLER than the PEPE balance under test, so the clamp
    # correctly took the request and the assertion blamed the module for the
    # fixture's number. A fixture that constructs the input decides the
    # answer unless its defaults stay out of the way.
    base = dict(requested_quantity="1E+30", available_quantity="1",
                price="100", base_increment="0.001")
    base.update(kw)
    return eq.plan_order_quantity(**base)


print("== A. the live QNT case: dust, not a failure ==")
# The exact figures from the log: 0.00097323 against a real 0.001 increment.
p = plan(available_quantity="0.00097323", base_increment="0.001", price="230.16")
ok("QNT dust is DUST, not REFUSED", p.decision == eq.DUST, p.decision)
ok("and the reason names the increment", p.reason == eq.BELOW_BASE_INCREMENT, p.reason)
ok("executable quantity is exactly zero", p.executable_quantity == 0)
ok("it must not be executed", p.should_execute is False)
ok("no order size string is produced", p.order_size_string is None)
# The whole point of §11: the raw holding survives, exactly.
ok("the raw available quantity is preserved exactly",
   p.raw_available == Decimal("0.00097323"), str(p.raw_available))
ok("and is NOT rounded into the ledger as zero", p.raw_available != 0)
ok("the detail says it is not a failed sale",
   "not a failed sale" in p.detail)

print("== B. exactly at the minimum must submit ==")
p = plan(available_quantity="0.001", base_increment="0.001", price="230.16")
ok("one whole increment executes", p.decision == eq.EXECUTE, p.decision)
ok("and the size is that increment", p.executable_quantity == Decimal("0.001"))
p = plan(available_quantity="5", base_increment="0.001",
         base_min_size="5", price="230.16")
ok("exactly at base_min_size executes", p.decision == eq.EXECUTE, p.reason)
# One increment below the minimum is the boundary that matters.
p = plan(available_quantity="4.999", base_increment="0.001",
         base_min_size="5", price="230.16")
ok("one increment below base_min_size is DUST", p.decision == eq.DUST, p.decision)
ok("with its own reason, not the increment one",
   p.reason == eq.BELOW_BASE_MIN_SIZE, p.reason)

print("== C. above the minimum, floored to the increment ==")
p = plan(available_quantity="1134.346389", base_increment="0.1", price="0.13591")
ok("ALGO floors to its real 0.1 increment",
   p.executable_quantity == Decimal("1134.3"), str(p.executable_quantity))
ok("and executes", p.decision == eq.EXECUTE, p.reason)
ok("the order string carries exactly one decimal",
   p.order_size_string == "1134.3", p.order_size_string)
p = plan(available_quantity="39093467.068798", base_increment="1", price="0.00000426")
ok("PEPE floors to whole units", p.executable_quantity == Decimal("39093467"),
   str(p.executable_quantity))
ok("and its order string carries no decimal point",
   p.order_size_string == "39093467", p.order_size_string)
p = plan(available_quantity="1980.76640025", base_increment="0.00000001", price="0.2263")
ok("XLM keeps all 8 decimals", p.executable_quantity == Decimal("1980.76640025"),
   str(p.executable_quantity))

print("== the non-power-of-ten increment the old method gets wrong ==")
# A decimal COUNT cannot express this rule. 0.07 has two decimals and is
# not a multiple of 0.05, so the old approach would have shipped it.
p = plan(available_quantity="0.07", base_increment="0.05", price="100")
ok("0.07 floors to 0.05, not to 0.07",
   p.executable_quantity == Decimal("0.05"), str(p.executable_quantity))
ok("the result is a whole multiple of the increment",
   p.executable_quantity % Decimal("0.05") == 0)
p = plan(available_quantity="12", base_increment="5", price="100")
ok("an increment of 5 floors 12 to 10",
   p.executable_quantity == Decimal("10"), str(p.executable_quantity))

print("== exact decimal arithmetic, not float ==")
# 2.675 * 100 is 267.49999999999997 in binary float, so the float method
# floors a whole increment away.
import math
# 0.29 at a 0.01 increment is a REAL float failure, and 0.01 is the live
# increment on APE, TIA, PRIME, LINK, ONDO and TON. An earlier version of
# this check used 2.675 at 0.001 on the assumption that it also fails;
# 2.675 * 1000 rounds to exactly 2675.0, so the float method gets that one
# right. The assumption was never measured before being asserted.
_float_answer = math.floor(0.29 * 100) / 100
ok("the float method really does lose a whole increment here",
   _float_answer != 0.29, str(_float_answer))
p = plan(available_quantity="0.29", base_increment="0.01", price="100")
ok("but the exact method keeps 0.29",
   p.executable_quantity == Decimal("0.29"), str(p.executable_quantity))
ok("and that is one increment more than float would have sold",
   p.executable_quantity > Decimal(str(_float_answer)))
p = plan(available_quantity="2.675", base_increment="0.001", price="100")
ok("2.675 at a 0.001 increment stays 2.675",
   p.executable_quantity == Decimal("2.675"), str(p.executable_quantity))

print("== I. metadata unavailable must FAIL CLOSED ==")
for bad in (None, "", "0", "-0.001", "abc", float("nan")):
    p = plan(available_quantity="10", base_increment=bad, price="100")
    ok(f"base_increment={bad!r} refuses", p.decision == eq.REFUSED,
       f"{p.decision}/{p.reason}")
    ok(f"  and executes nothing for {bad!r}", p.executable_quantity == 0)
p = plan(available_quantity="10", base_increment=None, price="100")
ok("the refusal names metadata as the cause",
   p.reason == eq.METADATA_UNAVAILABLE, p.reason)
# The specific regression: never 8 decimals as a fallback.
ok("a refused plan yields no order string", p.order_size_string is None)

print("== an unreadable balance or price must not become a number ==")
p = plan(available_quantity=None, base_increment="0.001", price="100")
ok("unreadable balance refuses", p.decision == eq.REFUSED, p.decision)
ok("and says so", p.reason == eq.BALANCE_UNREADABLE, p.reason)
ok("UNKNOWN available is not recorded as 0", p.raw_available is None)
# A notional floor cannot be evaluated without a price. Asserting it
# satisfied would be the guess this module exists to remove.
p = plan(available_quantity="10", base_increment="0.001",
         price=None, quote_min_size="1")
ok("a notional rule with no price refuses", p.decision == eq.REFUSED, p.decision)
ok("naming the price as the gap", p.reason == eq.PRICE_UNREADABLE, p.reason)
# But with no notional rule published, a missing price must not block.
p = plan(available_quantity="10", base_increment="0.001", price=None)
ok("no published notional rule and no price still executes",
   p.decision == eq.EXECUTE, f"{p.decision}/{p.reason}")

print("== the notional floor the old code never checked ==")
# min_market_funds is 1 on every product measured.
p = plan(available_quantity="0.002", base_increment="0.001",
         price="100", quote_min_size="1")
ok("0.002 at $100 is $0.20 and is DUST", p.decision == eq.DUST, p.decision)
ok("for the notional reason", p.reason == eq.BELOW_QUOTE_MIN_SIZE, p.reason)
ok("the notional is carried on the result", p.notional == Decimal("0.200"),
   str(p.notional))
p = plan(available_quantity="0.02", base_increment="0.001",
         price="100", quote_min_size="1")
ok("0.02 at $100 is $2.00 and executes", p.decision == eq.EXECUTE, p.reason)

print("== absent rules are not satisfied rules ==")
# The public Exchange API returns neither minimum. Absent must mean the
# rule is not asserted - never that it passed.
p = plan(available_quantity="0.00097323", base_increment="0.001",
         base_min_size=None, quote_min_size=None, price="230.16")
ok("the increment floor still applies with both minimums absent",
   p.decision == eq.DUST and p.reason == eq.BELOW_BASE_INCREMENT, p.reason)
ok("an absent base_min_size is reported as UNKNOWN, not 0",
   p.base_min_size is None)
ok("an absent quote_min_size is reported as UNKNOWN, not 0",
   p.quote_min_size is None)

print("== clamp before floor, never after ==")
# Flooring the request first and clamping second can leave a size the
# wallet cannot cover.
p = plan(requested_quantity="100", available_quantity="1.2345",
         base_increment="0.01", price="100")
ok("the size never exceeds what the wallet holds",
   p.executable_quantity <= Decimal("1.2345"), str(p.executable_quantity))
ok("and is the floored available, not the request",
   p.executable_quantity == Decimal("1.23"), str(p.executable_quantity))
p = plan(requested_quantity="0.5", available_quantity="1000",
         base_increment="0.01", price="100")
ok("a request smaller than the balance is honoured",
   p.executable_quantity == Decimal("0.5"), str(p.executable_quantity))

print("== a request of nothing is not an order ==")
for bad in ("0", "-1", None):
    p = plan(requested_quantity=bad, available_quantity="10",
             base_increment="0.001", price="100")
    ok(f"requested={bad!r} refuses", p.decision == eq.REFUSED, p.decision)
ok("and names the cause", plan(requested_quantity="0", available_quantity="10",
                               base_increment="0.001").reason == eq.NOTHING_REQUESTED)

print("== the formatter cannot disagree with the floor ==")
ok("decimals come from the increment", eq.quantity_decimals("0.001") == 3)
ok("an integer increment means no decimals", eq.quantity_decimals("1") == 0)
ok("8-decimal increments give 8", eq.quantity_decimals("0.00000001") == 8)
ok("an unreadable increment gives None, not a default",
   eq.quantity_decimals(None) is None and eq.quantity_decimals("0") is None)
ok("formatting an unreadable increment yields None, never a guess",
   eq.format_quantity("1.5", None) is None)

print("== the log line replaces 'nothing sellable' ==")
p = plan(available_quantity="0.00097323", base_increment="0.001", price="230.16")
line = eq.log_line("QNT-USD", p, cycle=1842, slice_label="3/3", target_price="235.00")
for token in ("QNT-USD", "cycle=1842", "slice=3/3", "available_quantity=0.00097323",
              "base_increment=0.001", "executable_quantity=0", "price=230.16",
              "maker_only=true", "decision=DUST", "reason=BELOW_BASE_INCREMENT"):
    ok(f"the line carries {token}", token in line, line)
ok("it does not say 'nothing sellable'", "nothing sellable" not in line)
# UNKNOWN must never render as 0 - that is the bug in miniature.
p2 = plan(available_quantity=None, base_increment="0.001", price="100")
line2 = eq.log_line("QNT-USD", p2)
ok("an unreadable figure renders UNKNOWN, not 0",
   "available_quantity=UNKNOWN" in line2, line2)

print("== the floor is not reimplemented ==")
import ast
import inspect
tree = ast.parse(inspect.getsource(eq))
# AST, not substring: the module's docstring QUOTES the buggy
# math.floor(...) line to explain it, so a text search for that name
# matches the explanation and fails a module that is perfectly correct.
_imported = {a.module for a in ast.walk(tree) if isinstance(a, ast.ImportFrom)}
_imported |= {n.name.split(".")[0] for a in ast.walk(tree)
              if isinstance(a, ast.Import) for n in a.names}
_names = {n.name for n in ast.walk(tree)
          if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
ok("it imports from resting_stops", "resting_stops" in _imported, str(_imported))
ok("round_down is imported, not redefined", "round_down" not in _names, str(_names))
ok("and math is never imported, so no float floor can creep back in",
   "math" not in _imported, str(_imported))

print()
if failures:
    print("FAILED %d check(s): %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("all execution-quantity checks passed")

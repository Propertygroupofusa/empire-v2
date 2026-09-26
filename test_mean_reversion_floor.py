"""An ATR-scaled target has no lower bound, so it needs a floor.

The gap this closes, found 2026-09-25 while wiring fee_floor.py into the
four engines that had no fee protection of any kind:

    self.target_price = entry_price + (ATR_MULTIPLIER_TARGET * atr_14)

2.5 x ATR_14 is a sensible target on a volatile coin and a disastrous one
on a quiet coin, because nothing bounds it from below. Worked on the real
round trip (1.50% when both legs fall back to market):

    entry $5.00, ATR 0.004  ->  target +0.200%  ->  a WIN nets -1.300%
    entry $5.00, ATR 0.010  ->  target +0.500%  ->  a WIN nets -1.000%
    entry $100,  ATR 0.20   ->  target +0.500%  ->  a WIN nets -1.000%

Four of five realistic cases were trades that lose money when they are
RIGHT about direction - the one class of loss that is arithmetic rather
than probability. This is the same defect that let a 0.25% profit target
reach a live-flagged config file the same day.

THE RULES THIS FILE PROTECTS:

  1. The target is RAISED to the floor, never lowered, and the position is
     never refused. By the time the position object exists the entry is
     already decided, so refusing would strand a real position with no
     exit. Raising keeps the trade honest: it simply has to travel far
     enough to pay for itself.
  2. The raise is RECORDED (fee_floor_raised) and logged with the
     arithmetic, so it is measurable rather than invisible.
  3. It prices the WORST round trip, not the expected one - an unfilled
     maker order becomes a market order.
  4. A floor that breaks trading is worse than no floor, so the check can
     never raise out of the constructor.
  5. The stop is NOT floored. Widening a stop to clear a fee would
     increase risk, which is the opposite of the point.

Run: python3 test_mean_reversion_floor.py
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fee_floor as ff

src = open(os.path.join(HERE, "crypto_mean_reversion_bot.py"), encoding="utf-8").read()
tree = ast.parse(src)
checks = []


def ok(label, cond):
    checks.append((label, bool(cond)))


ctor = None
for n in ast.walk(tree):
    if isinstance(n, ast.FunctionDef) and n.name == "__init__":
        body = "\n".join(ast.unparse(s) for s in n.body)
        if "target_price" in body:
            ctor = body
            break
ok("the position constructor was found", ctor is not None)
ctor = ctor or ""

# --- it is wired, and to the shared module -------------------------------
ok("it imports the SHARED floor, not a private copy", "import fee_floor" in ctor)
ok("REGRESSION: it prices a round trip at all", "FEE_FLOOR_ROUND_TRIP_PCT" in ctor)
ok("the rate is configurable and defaults to the worst case",
   'CRYPTO_ROUND_TRIP_FEE_PCT", "0.015"' in src)
ok("the rate comment says worst case, not expected", "not the expected one" in src)

# --- it raises, never refuses or lowers ----------------------------------
ok("it raises the target to the floor", "self.target_price = min_target" in ctor)
ok("it only acts when the target is BELOW the floor",
   "if self.target_price < min_target:" in ctor)
ok("the raise is recorded so it can be measured",
   "self.fee_floor_raised = True" in ctor and "self.fee_floor_raised = False" in ctor)
ok("it logs the arithmetic, not just that it acted",
   "net_per_win_pct" in ctor and "WINNING trade would net" in ctor)
ok("it never raises out of the constructor", "except Exception" in ctor)
ok("a broken floor is logged, not fatal", "fee floor check skipped" in ctor)

# --- the stop is deliberately NOT floored --------------------------------
# Only the ASSIGNMENT to stop_loss_price. An earlier version of this check
# scanned every line mentioning stop_loss_price and tripped on
# `emergency_floor = self.stop_loss_price * (1 - SLIPPAGE_COLLAR)` - a
# pre-existing slippage collar that merely contains the word "floor".
_stop_assign = [l.strip() for l in ctor.splitlines()
                if l.strip().startswith("self.stop_loss_price =")]
ok("the stop assignment was found", len(_stop_assign) == 1)
ok("the stop is still pure ATR - widening it would ADD risk",
   _stop_assign and "ATR_MULTIPLIER_STOP" in _stop_assign[0])
ok("REGRESSION: the FEE floor is not applied to the stop",
   _stop_assign and "min_target" not in _stop_assign[0]
   and "fee_floor" not in _stop_assign[0])


# --- the arithmetic, as behaviour ----------------------------------------
RT = 0.015
FLOOR = ff.fee_floor_pct(RT)


def target_after_floor(entry, atr, mult=2.5):
    raw = entry + mult * atr
    return max(raw, entry * (1.0 + FLOOR))


def raised(entry, atr, mult=2.5):
    return (entry + mult * atr) < entry * (1.0 + FLOOR)


ok("REGRESSION: a quiet coin (entry $5, ATR 0.004) is raised",
   raised(5.0, 0.004))
ok("its raw target would have netted -1.30% on a WIN",
   abs(ff.net_per_win_pct((5.0 + 2.5 * 0.004) / 5.0 - 1.0, RT) + 0.013) < 1e-9)
ok("a mid case (entry $100, ATR 0.20) is raised", raised(100.0, 0.20))
ok("a volatile coin (entry $100, ATR 1.50) is left alone", not raised(100.0, 1.50))
ok("a left-alone target keeps its exact ATR value",
   abs(target_after_floor(100.0, 1.50) - 103.75) < 1e-9)

ok("a raised target always clears the floor",
   all(target_after_floor(e, a) >= e * (1 + FLOOR) - 1e-12
       for e in (0.5, 5.0, 100.0, 84000.0) for a in (0.0, 0.001, 0.01, 1.0, 500.0)))
ok("the floor NEVER lowers a target",
   all(target_after_floor(e, a) >= e + 2.5 * a - 1e-12
       for e in (0.5, 5.0, 100.0) for a in (0.0, 0.004, 0.05, 2.0)))
ok("after flooring, a win can never net negative",
   all(ff.net_per_win_pct(target_after_floor(e, a) / e - 1.0, RT) >= 0 - 1e-12
       for e in (0.5, 5.0, 100.0) for a in (0.0, 0.004, 0.05, 2.0)))

width = max(len(l) for l, _ in checks)
for label, passed in checks:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label:<{width}}")
failed = [l for l, p in checks if not p]
print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)

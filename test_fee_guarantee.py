"""No profit target, on either platform, may sit below its own round-trip cost.

Per the account owner, 2026-09-25: fees are not to be a recurring
conversation. The code must simply never permit a target that cannot clear
what a round trip costs. Enforced structurally, so tuning a target for speed
can never accidentally tune it into a loss.

WHY THIS IS A STANDING GUARANTEE AND NOT A ONE-OFF CHECK - the same mistake
showed up three separate ways in a single day:

  * A pasted scalping strategy reported +352% on paper (64% win rate, 2.5%
    target, -3% stop). At the account's real 1.00% round-trip fee its true
    expectancy is -0.48% PER TRADE, and break-even needs a 72.7% win rate.
    Paper mode simply never charged the fee.
  * crypto_grid_bot's PROMOTED SPACING OVERRIDE bypassed fee_safe_floor_pct()
    while winning over both paths that did apply it - so a promoted
    candidate tighter than the floor would have traded every cycle at a
    guaranteed loss, silently.
  * prop_bot had NO fee floor at all. Nothing stopped a target being tuned
    below the spread it had to cross twice.

THE RULE, both platforms:

    target  >=  round_trip_cost + a net margin that survives it

and the cost is counted TWICE - entering and exiting. Halving it is the
single most common way a losing strategy reads as profitable.

Run: python3 test_fee_guarantee.py
"""
import ast
import logging
import os
import sys

logging.basicConfig(level=logging.CRITICAL)
HERE = os.path.dirname(os.path.abspath(__file__))
checks = []


def ok(label, cond):
    checks.append((label, bool(cond)))


# ---------------------------------------------------------------- ALPACA --
src = open(os.path.join(HERE, "prop_bot.py"), encoding="utf-8").read()
ns = {"os": os, "log": logging.getLogger("t")}
exec(src[src.index("def _safe_float_env"):src.index("def _safe_int_env")], ns)
_i = src.index("def _safe_int_env")
exec(src[_i:src.index("\n\n", src.index("return", _i))], ns)
exec(src[src.index("BASE_MAX_POSITIONS"):src.index("# Profit target, in REAL DOLLARS")], ns)
exec(src[src.index("PROFIT_TARGET_DOLLARS_MILESTONES = ["):src.index("# Crypto-specific AGGRESSIVE")], ns)
exec(src[src.index("def get_profit_target_dollars"):src.index("# Track profitable days")], ns)
ns["MAX_RISK_PERCENT"] = 0.50

target_for = ns["get_profit_target_dollars"]
fee_floor = ns["fee_safe_target_dollars"]
COST = ns["ALPACA_ROUND_TRIP_COST_PCT"]


def position_at(equity, slots=8):
    return (equity * 0.50) / slots


ok("the round-trip cost is DOUBLED (crossed entering and exiting)",
   abs(fee_floor(1000.0) - 1000.0 * (COST * 2 + ns["ALPACA_MIN_NET_MARGIN_PCT"])) < 1e-9)
ok("a net margin is required ON TOP of cost, not just break-even",
   ns["ALPACA_MIN_NET_MARGIN_PCT"] > 0)
ok("zero or negative notional yields no floor rather than a negative one",
   fee_floor(0) == 0.0 and fee_floor(-5) == 0.0)

for eq in [300, 750, 1006, 5000, 15000, 50000]:
    pos = position_at(eq)
    t = target_for(eq)
    cost = pos * COST * 2
    ok(f"${eq:,} target ${t:.2f} clears its ${cost:.2f} round-trip cost", t > cost)
    ok(f"${eq:,} target is at or above the fee floor", t >= fee_floor(pos) - 1e-9)

# An operator override is a decision - but no decision makes a loss profitable.
ns["PROFIT_TARGET_DOLLARS_OVERRIDE"] = 0.01
raised = target_for(1006)
ok("an absurdly small override is raised to the floor", raised > 0.01)
ok("and the raised value still clears cost",
   raised > position_at(1006) * COST * 2)
ns["PROFIT_TARGET_DOLLARS_OVERRIDE"] = 2.50
ok("a generous override passes through untouched", target_for(1006) == 2.50)
ns["PROFIT_TARGET_DOLLARS_OVERRIDE"] = 0.0
ok("clearing the override returns to the tiers", target_for(1006) == 1.00)

# The retune must not have been undone.
ok("the $1K tier is faster than the old $3.00", target_for(1006) < 3.00)
ok("upper tiers are unchanged", target_for(15000) == 25.00)

# --------------------------------------------------------------- CRYPTO --
grid = open(os.path.join(HERE, "crypto_grid_bot.py"), encoding="utf-8").read()
tree = ast.parse(grid)
fn = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
      and n.name == "run_grid_branch_cycle"][0]
body = "\n".join(ast.unparse(s) for s in fn.body[1:])

ok("the grid cycle applies a fee-safe floor", "fee_safe_floor_pct" in body)
# It must be applied AFTER the source is chosen, so it covers every path -
# including the promoted override, which previously bypassed it entirely.
ok("the floor is applied after the promoted override is read",
   body.index("override_cfg") < body.index("fee_safe_floor_pct"))
ok("the floor is applied before the new spacing is persisted",
   body.index("fee_safe_floor_pct") < body.rindex("new_grid_pct is not None"))
ok("a below-floor spacing is raised, not merely logged",
   "new_grid_pct = floor" in body)
ok("and the operator is told it happened", "fee-safe floor" in body)

# The floor itself must count both legs.
floorfn = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
           and n.name == "fee_safe_floor_pct"][0]
fbody = "\n".join(ast.unparse(s) for s in floorfn.body[1:])
ok("the crypto floor counts BOTH fee legs", "leg * 2" in fbody)
ok("and adds a target net margin on top", "TARGET_NET_MARGIN_PCT" in fbody)

width = max(len(l) for l, _ in checks)
for label, passed in checks:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label:<{width}}")
failed = [l for l, p in checks if not p]
print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)

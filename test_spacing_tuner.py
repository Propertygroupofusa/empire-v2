"""The step moves where the MEASUREMENT points - not always tighter.

The ask, 2026-09-25: "learn as you go and change it to where it can get
better and faster... then we get tighter and tighter to what it should be."

The word to correct is "tighter". On the real 30-day data, wider wins more
often than tighter:

    35-coin aggregate   2.0% +$306.98   2.5% +$348.21   wider
    STX-USD             2.0% +$25.32    2.5% +$33.77    wider
    POL-USD             2.0% +$18.36    2.5% +$14.71    tighter

Total grid profit is roughly trips x capital x margin / levels. A tighter
step buys more trips and sells margin on every one; which side wins is a
property of how that coin actually moves. A tuner that only tightens is a
machine for walking the fleet to its floor on a hunch - the same mistake as
the 1.25% recommendation made and retracted the same day, automated.

THE RULE THIS FILE PROTECTS: the tuner is a measurement, not a direction.
It may widen. It must clear a minimum number of observed trips before it
believes a candidate, must beat the incumbent by a real margin before it
churns, and may never cross the fee-safe floor - which prices the TAKER
round trip, because an unfilled maker order becomes a market order.

Run: python3 test_spacing_tuner.py
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(HERE, "crypto_grid_bot.py"), encoding="utf-8").read()
tree = ast.parse(src)
checks = []


def ok(label, cond):
    checks.append((label, bool(cond)))


def fn(name):
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


node = fn("tune_spacing_per_coin")
ok("the tuner exists", node is not None)
stmts = node.body if node else []
if stmts and isinstance(stmts[0], ast.Expr) and isinstance(stmts[0].value, ast.Constant):
    stmts = stmts[1:]
body = "\n".join(ast.unparse(s) for s in stmts)

# --- it is a measurement, not a direction --------------------------------
ok("it chooses by measured net P&L", "net_usd" in body and "max(" in body)
ok("it can move the step WIDER, not only tighter", "'wider'" in body or '"wider"' in body)
ok("direction is derived from the comparison, not assumed",
   "to_grid_pct'] < b.grid_pct" in body or "best['grid_pct'] < b.grid_pct" in body)

# --- a held position keeps the terms it was opened under -----------------
# 2026-09-25: this guard was missing and the tuner widened NEAR-USD from
# 2.00% to 3.00% with a real slice open, moving its sell trigger from
# $5.0681 to $5.1178 - 1.24% away to 2.23% away - on committed money. It
# happened to be favourable; on a falling coin the same move pushes an
# exit out of reach. The backtest answers "best step for the NEXT trade",
# never "what to do with a position already open".
ok("REGRESSION: a branch holding a slice is never re-spaced",
   "get_grid_slices" in body and "if held:" in body)
ok("the skip names the real reason - the exit would move",
   "move the exit" in body)
ok("it is not an error, just a deferral", "will re-tune when flat" in body)
ok("the guard runs BEFORE the backtest, so it cannot be overridden by a result",
   body.index("if held:") < body.index("run_grid_level_spacing_comparison"))

# --- evidence gates ------------------------------------------------------
ok("a candidate must clear a minimum number of trips", "min_trips" in body)
ok("thin-evidence candidates are excluded before choosing",
   "trips'] >= min_trips" in body)
ok("the winner must beat the incumbent by a real margin", "min_improvement_usd" in body)
ok("it refuses to churn for a trivial gain", "not worth churning" in body)
ok("it skips a branch already on the measured best", "already on the measured best" in body)

# --- the floor is absolute ----------------------------------------------
ok("it reads the fee-safe floor", "fee_safe_floor_pct" in body)
ok("below-floor candidates are excluded from selection", "below_floor" in body)
ok("the write clamps UP to the floor, never down", "max(plan['to_grid_pct'], floor)" in body)
floor_body = "\n".join(ast.unparse(s) for s in fn("fee_safe_floor_pct").body[1:])
ok("REGRESSION: the floor still ignores any measurement",
   "worst_case_leg_fee_rate" in floor_body and "tune_spacing" not in floor_body)

# --- it is honest about precedence --------------------------------------
ok("it reads the global manual override", "get_live_grid_spacing_override" in body)
ok("it reports when the override would ignore its writes",
   "would_take_effect" in body and "global_override_blocks_per_coin" in body)

# --- dry run is the default and changes nothing --------------------------
ok("dry_run defaults to True",
   any(isinstance(d, ast.Constant) and d.value is True for d in node.args.defaults))
ok("writes happen only when dry_run is False", "if not dry_run:" in body)
ok("every change is logged to the activity feed", "SPACING_TUNED" in body)

# --- a shape error must never look like a careful verdict ----------------
# The first version of the tuner guessed the backtest's result shape wrong,
# found nothing, and reported "no candidate cleared 4 trips above the 1.70%
# floor" for all seven coins. That reads like the evidence gate working. It
# was a parsing bug wearing the gate's clothes, and only the uniformity of
# the seven identical skips gave it away.
ok("it reads the real result shape: comparison rows",
   "'comparison'" in body or '"comparison"' in body)
ok("it reads total_pnl, the key the backtest actually returns", "total_pnl" in body)
ok("it reads num_trades, the key the backtest actually returns", "num_trades" in body)
ok("REGRESSION: it no longer reads an invented per_coin sub-dict",
   "get('per_coin')" not in body and 'get("per_coin")' not in body)
ok("REGRESSION: it no longer reads an invented round_trips key",
   "round_trips" not in body)
ok("REGRESSION: it no longer reads an invented net_pnl_usd key",
   "net_pnl_usd" not in body)
ok("a missing comparison row is flagged as an ERROR, not thin evidence",
   "no comparison row" in body and "is_error" in body)
ok("an empty candidate set is flagged as an error too",
   "shape or data error" in body)
ok("genuine thin evidence is explicitly NOT an error",
   "'is_error': False" in body or '"is_error": False' in body)

router = open(os.path.join(HERE, "routers", "trading_dashboard.py"), encoding="utf-8").read()
ok("an endpoint exposes it", "/grid-status/tune-spacing-per-coin" in router)
ok("the endpoint defaults to dry_run", "dry_run: bool = True" in router)


# --- the arithmetic, as behaviour ---------------------------------------
FLOOR = 0.017


def choose(rows, current_pct, min_trips=4, min_gain=1.0):
    elig = [r for r in rows if r["trips"] >= min_trips and r["grid_pct"] >= FLOOR]
    if not elig:
        return None
    best = max(elig, key=lambda r: r["net_usd"])
    cur = next((r for r in rows if abs(r["grid_pct"] - current_pct) < 1e-9), None)
    if cur and abs(best["grid_pct"] - current_pct) < 1e-9:
        return None
    if cur and (best["net_usd"] - cur["net_usd"]) < min_gain:
        return None
    return best


# STX as really measured: 2.5% beat 2.0%. The tuner must widen here.
stx = [{"grid_pct": 0.020, "net_usd": 25.32, "trips": 25},
       {"grid_pct": 0.025, "net_usd": 33.77, "trips": 23}]
pick = choose(stx, 0.020)
ok("STX: the tuner WIDENS 2.0% -> 2.5%, following the measurement",
   pick and abs(pick["grid_pct"] - 0.025) < 1e-9)

# POL as really measured: 2.0% beat 2.5%. Same tuner, opposite move.
pol = [{"grid_pct": 0.020, "net_usd": 18.36, "trips": 12},
       {"grid_pct": 0.025, "net_usd": 14.71, "trips": 12}]
ok("POL: from 2.5% the same tuner TIGHTENS to 2.0% - direction is not baked in",
   (lambda p: p and abs(p["grid_pct"] - 0.020) < 1e-9)(choose(pol, 0.025)))

# A below-floor candidate can never be chosen, however good it looks.
tempting = [{"grid_pct": 0.015, "net_usd": 999.0, "trips": 500},
            {"grid_pct": 0.020, "net_usd": 25.32, "trips": 25}]
ok("REGRESSION: a below-floor candidate is refused however good its total",
   (lambda p: p is None or p["grid_pct"] >= FLOOR)(choose(tempting, 0.020)))

# One lucky trip is not a verdict.
lucky = [{"grid_pct": 0.030, "net_usd": 500.0, "trips": 1},
         {"grid_pct": 0.020, "net_usd": 25.32, "trips": 25}]
ok("a single-trip candidate cannot win, whatever it earned",
   choose(lucky, 0.020) is None)

# Churn guard.
noise = [{"grid_pct": 0.020, "net_usd": 25.32, "trips": 25},
         {"grid_pct": 0.025, "net_usd": 25.70, "trips": 23}]
ok("a $0.38 edge does not move the fleet", choose(noise, 0.020) is None)
ok("but a $8.45 edge does", choose(stx, 0.020) is not None)

width = max(len(l) for l, _ in checks)
for label, passed in checks:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label:<{width}}")
failed = [l for l, p in checks if not p]
print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)

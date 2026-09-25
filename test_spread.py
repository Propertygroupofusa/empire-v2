"""The spread action: it must actually open branches, and say why when it can't.

WHAT WENT WRONG LIVE

spread_capital_evenly asked pick_best_ranked_coin_for_grid() for each new
branch. That function RAISES when no coin has a backtest run yet or none
clears MIN_REQUIRED_ROI_PCT - a completely normal state on an account
that has not run the backtests. The loop then broke on the first failure,
so it created ZERO branches. The operator pressed the button, saw no
error worth reporting, and the fleet stayed at 2 branches with $578.61
stranded in one coin.

Two fixes are pinned here: a fallback coin source so a fresh account can
still be spread, and a loop that keeps trying instead of giving up on the
first ineligible coin.

Run: python3 test_spread.py
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GRID = open(os.path.join(HERE, "crypto_grid_bot.py")).read()
ROUTER = open(os.path.join(HERE, "routers", "trading_dashboard.py")).read()

checks = []


def ok(label, cond):
    checks.append((label, bool(cond)))


tree = ast.parse(GRID)
fns = {n.name: n for n in ast.walk(tree)
       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

ok("the spread action exists", "spread_capital_evenly" in fns)
ok("a fallback coin source exists", "_spread_candidate_coins" in fns)

# The structural fix: no break inside the creation loop's except handler.
spread = fns["spread_capital_evenly"]
creation_loops = [n for n in ast.walk(spread)
                  if isinstance(n, ast.For)
                  and any(isinstance(h, ast.ExceptHandler) for h in ast.walk(n))]
ok("the creation loop is a for-loop with its own error handling", len(creation_loops) >= 1)

handler_breaks = 0
for loop in creation_loops:
    for handler in [h for h in ast.walk(loop) if isinstance(h, ast.ExceptHandler)]:
        handler_breaks += sum(1 for n in ast.walk(handler) if isinstance(n, ast.Break))
ok("a failed coin does NOT abort the whole spread", handler_breaks == 0)
ok("successes are counted so a shortfall is detectable", "created += 1" in GRID)
ok("and a shortfall is reported with the reason", "ran out of eligible coins" in GRID)

# The fallback itself.
fb = ast.get_source_segment(GRID, fns["_spread_candidate_coins"]) or ""
ok("it tries the ranked picker first", "pick_best_ranked_coin_for_grid()" in fb)
ok("it survives the picker raising", "except Exception" in fb)
ok("it falls back to the nine-coin fleet list", "scanner.NINE_COINS" in fb)
ok("it filters coins another grid branch claims", "get_grid_branch_claimed_coins()" in fb)
ok("it filters coins the family tree holds", "claimed_by_other(claims.GRID)" in fb)
ok("it filters excluded coins", "get_effective_excluded_coins()" in fb)
ok("it normalises before comparing, so BTC/USD cannot slip past BTC-USD",
   "normalize_product" in fb)
ok("it explains itself rather than returning a bare list",
   "return out, note" in fb)
ok("the reason the ranked picker failed is carried forward",
   "Ranked picker had nothing" in fb)

# The plan has to expose all of it.
ok("the plan exposes the candidate coins", 'plan["candidate_coins"]' in GRID)
ok("and the note explaining where they came from", 'plan["candidate_note"]' in GRID)

# The readable diagnostic.
ok("a GET diagnostic exists, openable in a phone browser",
   '@router.get("/grid-status/spread-plan")' in ROUTER)
ok("it is read-only and says so", "nothing has been changed" in ROUTER.lower())
ok("it prints the eligible coins, or NONE", "eligible coins" in ROUTER)
ok("it surfaces a refusal rather than an empty page",
   'status in ("unavailable", "too_thin")' in ROUTER)

# The reserve, which a previous version forgot entirely.
ok("the pool still holds back the reserve before dividing",
   "distributable = round(max(0.0, pool - reserve), 2)" in GRID)

width = max(len(l) for l, _ in checks)
for label, passed in checks:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label:<{width}}")
failed = [l for l, p in checks if not p]
print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)

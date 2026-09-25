"""The fee-safe floor must price the fee the bot can really end up paying.

The bill for not having this, 2026-09-25:

    The dashboard reported, on the live account:

        fee_safe_min_grid_pct        0.90%
        effective_round_trip_fee     0.70%   (both legs MAKER)
        real_round_trip_fee_rate     1.50%   (both legs TAKER)

    fee_safe_floor_pct() called expected_leg_fee_rate(), which returns the
    MAKER rate whenever maker orders are on. So the floor was computed as
    if every leg would fill as a maker.

    But grid_buy() says exactly what it does: "maker first (cheap, may not
    fill), market fallback (always fills, costs more)". After
    MAKER_ORDER_WAIT_SECONDS an unfilled maker order becomes a MARKET
    order and pays taker.

    Everything between 0.90% and 1.70% was therefore certified fee-safe
    while being a guaranteed loss on any cycle that fell back on both
    legs. I nearly recommended tightening the live grid from 2.00% to
    1.25% on the strength of that floor - inside the bad band.

THE RULE THIS FILE PROTECTS: the floor is a SAFETY guarantee, not an
estimate. It is priced against the worst fee a round trip can really pay -
the taker leg - for as long as an unfilled maker order can become a market
order. The maker saving belongs in the margin earned at a given spacing,
never in permission to set a spacing that cannot survive a fallback.

Maker fill rate is not measured anywhere in this codebase. Until something
counts fallbacks there is no evidence for the optimistic assumption, and a
floor may not assume what nothing measures.

Run: python3 test_fee_floor_worst_case.py
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


def body_src(name):
    """Function body WITHOUT its docstring - the docstring here explains the
    very bug being tested and would satisfy every string match on its own."""
    node = fn(name)
    if node is None:
        return ""
    stmts = node.body
    if stmts and isinstance(stmts[0], ast.Expr) and isinstance(stmts[0].value, ast.Constant) \
            and isinstance(stmts[0].value.value, str):
        stmts = stmts[1:]
    return "\n".join(ast.unparse(s) for s in stmts)


# --- the floor exists and prices the worst case ---------------------------
ok("fee_safe_floor_pct still exists", fn("fee_safe_floor_pct") is not None)
ok("a worst-case leg rate helper exists", fn("worst_case_leg_fee_rate") is not None)

floor = body_src("fee_safe_floor_pct")
ok("REGRESSION: the floor no longer prices itself off the maker rate",
   "expected_leg_fee_rate" not in floor)
ok("the floor prices the worst case a leg can pay",
   "worst_case_leg_fee_rate" in floor)
ok("the floor still keeps its absolute minimum", "MIN_DYNAMIC_GRID_PCT" in floor)
ok("the floor still adds a net margin on top of the fee",
   "TARGET_NET_MARGIN_PCT" in floor)

worst = body_src("worst_case_leg_fee_rate")
ok("the worst case is the taker round trip halved, unconditionally",
   "get_effective_round_trip_fee_rate" in worst)
ok("the worst case does NOT consult whether maker mode is on",
   "is_maker_orders_active" not in worst and "maker" not in worst.lower())

# --- the estimator is deliberately left alone -----------------------------
est = body_src("expected_leg_fee_rate")
ok("expected_leg_fee_rate still knows about maker (it estimates, not floors)",
   "is_maker_orders_active" in est)

# --- the premise: the fallback this guards against is real ----------------
buy = body_src("grid_buy")
ok("grid_buy really does fall back to a market order (guards the premise)",
   "place_market_buy" in buy)
ok("the maker attempt is conditional, so it can be skipped entirely",
   "is_maker_orders_active" in buy)

# --- the arithmetic, stated as behaviour ----------------------------------
MIN_DYNAMIC = 0.003


def floor_for(round_trip, target_margin):
    return max(MIN_DYNAMIC, target_margin + (round_trip / 2) * 2)


LIVE_TAKER_ROUND_TRIP = 0.015   # as the live account reported it
LIVE_TARGET_MARGIN = 0.002

f = floor_for(LIVE_TAKER_ROUND_TRIP, LIVE_TARGET_MARGIN)
ok("on the live numbers the floor is now 1.70%, not 0.90%", abs(f - 0.017) < 1e-9)
ok("REGRESSION: 1.25% - the spacing I nearly recommended - is now refused",
   0.0125 < f)
ok("the live 2.00% spacing still clears the corrected floor", 0.02 >= f)
ok("a spacing at exactly the floor earns the target margin against taker",
   abs((f - LIVE_TAKER_ROUND_TRIP) - LIVE_TARGET_MARGIN) < 1e-9)
ok("a maker round trip at the floor earns MORE, which is where the saving belongs",
   (f - 0.007) > LIVE_TARGET_MARGIN)
ok("the absolute minimum still applies when fees are near zero",
   floor_for(0.0, 0.0) == MIN_DYNAMIC)

width = max(len(l) for l, _ in checks)
for label, passed in checks:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label:<{width}}")
failed = [l for l, p in checks if not p]
print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)

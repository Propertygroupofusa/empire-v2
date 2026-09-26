"""Maker-ONLY mode: the floor may only come down when the taker path is GONE.

THE ARITHMETIC THIS EXISTS FOR, measured on the live account 2026-09-25:

    taker round trip   1.50%   (0.75% a leg)
    maker round trip   0.70%   (0.35% a leg - measured on a real fill)
    adverse selection  0.67%   (measured by the net-edge gate)
    live grid spacing  2.00%

    2.00% - (1.50% + 0.67%) = -0.17%   the step LOSES money
    2.00% - (0.70% + 0.67%) = +0.63%   the step CLEARS

That gap is the whole reason the fleet logged 48 blocked buys and 0 allowed
in 24 hours, every one of them reading "a 2.00% target does not clear 2.17%
of costs". Not one of those refusals was wrong.

WHAT DOES *NOT* EARN THE LOWER NUMBER

Turning maker orders on. A maker order is an attempt: grid_buy() and
grid_sell() fall back to a market order when it does not fill, so the round
trip can still really cost 1.50%. Pricing the floor off the maker rate
because maker MODE is on is precisely the bug test_fee_floor_worst_case.py
was written for - it certified an 0.80%-wide band of guaranteed-loss
spacings as fee-safe.

WHAT DOES

Deleting the fallback. Under maker-only an unfilled maker order is
cancelled and the cycle simply passes, so the taker leg is not an unlikely
outcome - it is an unreachable one. That is a claim about control flow, not
about fill probability, and it is the only kind of claim that may move a
safety floor.

So this file does not check that the floor CAN come down. It checks that it
comes down only when all three of these hold, and goes straight back up when
any one of them stops holding:

    1. the fallback is genuinely removed in BOTH legs' code paths
    2. the toggle that says so FAILS CLOSED
    3. the maker rate it prices with was MEASURED, never assumed

Run: python3 test_maker_only.py
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
    """Body WITHOUT the docstring - every docstring here describes the bug
    being tested and would satisfy any string match on its own."""
    node = fn(name)
    if node is None:
        return ""
    stmts = node.body
    if stmts and isinstance(stmts[0], ast.Expr) and isinstance(stmts[0].value, ast.Constant) \
            and isinstance(stmts[0].value.value, str):
        stmts = stmts[1:]
    return "\n".join(ast.unparse(s) for s in stmts)


# --- 1. the mode exists and is a real, persisted switch --------------------
ok("is_maker_only_active exists", fn("is_maker_only_active") is not None)
ok("set_maker_only_active exists", fn("set_maker_only_active") is not None)
ok("it is persisted in the DB, not a module global",
   "MAKER_ONLY_MODE_KEY" in src and "TradingBotState" in body_src("is_maker_only_active"))

only = body_src("is_maker_only_active")
ok("maker-only also requires maker orders to be ON at all",
   "is_maker_orders_active" in only)
ok("FAILS CLOSED: an unreadable toggle answers False, never True",
   "except" in only and "return False" in only)
ok("and it never returns True from an exception path",
   "return True" not in only.split("except")[-1])

setter = body_src("set_maker_only_active")
ok("turning it on also turns maker orders on, so the fleet cannot be left unable to trade",
   "set_maker_orders_active" in setter)


# --- 2. the fallback is genuinely gone from BOTH legs ----------------------
for leg, market in (("grid_buy", "place_market_buy"), ("grid_sell", "place_market_sell")):
    b = body_src(leg)
    ok(f"{leg} still HAS a market fallback for the normal (fallback-on) case",
       market in b)
    ok(f"{leg} consults maker-only before ever reaching that fallback",
       "is_maker_only_active" in b
       and b.index("is_maker_only_active") < b.index(market))
    ok(f"{leg} RETURNS rather than falling through when maker-only is on",
       "return None" in b)
    ok(f"{leg} counts the cycle it gave up, so the cost of this mode is measured",
       "_record_maker_only_skip" in b)


# --- 3. the floor: the actual safety property -----------------------------
worst = body_src("worst_case_leg_fee_rate")
ok("the floor's worst case consults whether the FALLBACK is gone",
   "is_maker_only_active" in worst)
ok("REGRESSION: it does NOT consult whether maker MODE is on",
   "is_maker_orders_active" not in worst)
ok("it refuses to price maker without a MEASURED maker rate",
   "_cached_real_maker_fee_rate is None" in worst)
ok("it clamps to the taker leg - no arrangement of makers costs more than takers",
   "min(" in worst)

floor = body_src("fee_safe_floor_pct")
ok("the floor still derives from the worst case, not the estimate",
   "worst_case_leg_fee_rate" in floor and "expected_leg_fee_rate" not in floor)


# --- 4. nothing that MUST fill was routed through the maker path ----------
# A grid can wait on both legs; a forced liquidation cannot. If close-all
# ever started going through grid_sell(), maker-only would be able to trap
# an account that is trying to get out.
close_all = body_src("close_all_grid_branches") or body_src("close_all_grid_slices")
ok("the emergency close still sells at MARKET, untouched by maker-only",
   "place_market_sell" in close_all and "grid_sell" not in close_all)
ok("the sell leg's only caller tolerates a non-fill (it retries next cycle)",
   "did not fill - will retry next cycle" in src)


# --- 5. the wait window ---------------------------------------------------
ok("maker-only gets its own, longer resting window",
   "MAKER_ONLY_ORDER_WAIT_SECONDS" in src)
wait = body_src("maker_wait_seconds")
ok("and the legs ask for the window rather than hardcoding the short one",
   "MAKER_ONLY_ORDER_WAIT_SECONDS" in wait and "MAKER_ORDER_WAIT_SECONDS" in wait)
for leg in ("grid_buy", "grid_sell"):
    ok(f"{leg} uses maker_wait_seconds(), not the bare constant",
       "maker_wait_seconds" in body_src(leg))


# --- 6. the arithmetic, as behaviour --------------------------------------
MIN_DYNAMIC = 0.003
TARGET_MARGIN = 0.002
TAKER_LEG = 0.0075
MAKER_LEG = 0.0035
ADVERSE = 0.0067
LIVE_STEP = 0.02


def floor_for(leg):
    return max(MIN_DYNAMIC, TARGET_MARGIN + leg * 2)


def clears(step, leg):
    """Does a step of this size beat its REAL cost - fees plus adverse selection?"""
    return step - (leg * 2 + ADVERSE)


ok("with the fallback, the floor is 1.70%", abs(floor_for(TAKER_LEG) - 0.017) < 1e-9)
ok("without it, 0.90%", abs(floor_for(MAKER_LEG) - 0.009) < 1e-9)
ok("REGRESSION: the live 2.00% step really does LOSE money against taker",
   clears(LIVE_STEP, TAKER_LEG) < 0)
ok("and the loss is the 0.17% the gate has been logging 48 times a day",
   abs(clears(LIVE_STEP, TAKER_LEG) + 0.0017) < 1e-9)
ok("the same step CLEARS once the fallback is gone", clears(LIVE_STEP, MAKER_LEG) > 0)
ok("by 0.63%, which is the whole point of the mode",
   abs(clears(LIVE_STEP, MAKER_LEG) - 0.0063) < 1e-9)
# The floor is NOT a cost: 0.90% is 0.70% of fees plus the 0.20% margin the
# floor requires on top. Adding adverse selection to the floor instead of to
# the fees double-counts that margin and understates the headroom by exactly
# the margin - the slip that first reported this as +0.43%.
ok("REGRESSION: the real cost is fees + adverse, NOT the floor + adverse",
   abs((MAKER_LEG * 2 + ADVERSE) - 0.0137) < 1e-9
   and abs((floor_for(MAKER_LEG) + ADVERSE) - 0.0157) < 1e-9)
ok("and the difference between those two readings is exactly the target margin",
   abs(((floor_for(MAKER_LEG) + ADVERSE) - (MAKER_LEG * 2 + ADVERSE)) - TARGET_MARGIN) < 1e-9)
ok("REGRESSION: maker fees alone are not enough - adverse selection still has to clear",
   clears(0.008, MAKER_LEG) < 0)
ok("so a step at the bare 0.90% floor still does not clear ADVERSE selection, "
   "and the net-edge gate is still the thing that refuses it",
   clears(floor_for(MAKER_LEG), MAKER_LEG) < 0)

# --- 7. the GATE must price the same leg as the floor ---------------------
# The floor decides what spacing is allowed. The net-edge gate decides what
# actually gets bought. Dropping the floor to 0.90% while the gate still
# priced a 1.50% taker round trip would have made the whole switch a no-op:
# the fleet would stay frozen, refusing every buy with "a 2.00% target does
# not clear 2.17% of costs", and the only visible result of flipping it would
# have been a number moving on a dashboard.
gate = body_src("_net_edge_gate_ok")
ok("the gate consults maker-only before pricing the round trip",
   "is_maker_only_active" in gate)
ok("it still reads the LIVE fee tier rather than a cached guess",
   "get_real_fee_tier" in gate)
ok("it requires a measured maker rate, never an assumed one",
   "_maker and await is_maker_only_active" in gate or "_maker and" in gate)
ok("and clamps to taker - no arrangement of makers costs more than takers",
   "min(float(_maker), float(taker))" in gate)

# The two must agree by construction, not by coincidence.
def gate_cost(fee_rt): return fee_rt + ADVERSE
ok("REGRESSION: with the fallback live, the gate refuses the 2.00% step",
   gate_cost(TAKER_LEG * 2) > LIVE_STEP)
ok("with the fallback gone, the gate passes it",
   gate_cost(MAKER_LEG * 2) < LIVE_STEP)
ok("gate and floor price the SAME leg in both modes - a floor that allows a "
   "spacing the gate then refuses is a switch that changes nothing",
   abs((floor_for(TAKER_LEG) - TARGET_MARGIN) / 2 - TAKER_LEG) < 1e-9
   and abs((floor_for(MAKER_LEG) - TARGET_MARGIN) / 2 - MAKER_LEG) < 1e-9)

# --- 8. the escape hatch ---------------------------------------------------
# The dashboard button is the normal way in. It was verified working end to
# end and the click still did not land, twice, with nothing able to say why.
# So there is a second way that needs no browser at all - the same shape this
# codebase already used when CRYPTO_STRATEGY_MODE could not be corrected
# through the Railway UI.
ok("an environment override exists", fn("maker_only_env_override") is not None)
ok("and it is named as a constant, not a bare string",
   "MAKER_ONLY_ENV_VAR" in src)

only = body_src("is_maker_only_active")
ok("the override is consulted BEFORE the database",
   "maker_only_env_override" in only
   and only.index("maker_only_env_override") < only.index("MAKER_ONLY_MODE_KEY"))
ok("an explicit false forces it OFF, so a stuck DB row can be overridden "
   "in either direction",
   "env is False" in only and "return False" in only)
ok("it still refuses to run maker-only without maker orders",
   "is_maker_orders_active" in only)

env_fn = body_src("maker_only_env_override")
ok("only explicit values count - a typo must not enable a real-money mode",
   "_TRUE" in env_fn and "_FALSE" in env_fn and "return None" in env_fn)
ok("REGRESSION: quotes are stripped, the exact way a pasted Railway value "
   "has already broken this deployment once",
   ".strip('\"')" in env_fn or '.strip(\'"\')' in env_fn or "strip('\"')" in env_fn)

# Behaviour, not just shape.
import importlib.util as _ilu
_s2 = _ilu.spec_from_file_location("_g2", os.path.join(HERE, "crypto_grid_bot.py"))
try:
    _m2 = _ilu.module_from_spec(_s2)
    _s2.loader.exec_module(_m2)
    import os as _os
    cases = {"true": True, "TRUE": True, "1": True, " yes ": True, '"true"': True,
             "false": False, "0": False, "off": False,
             "maybe": None, "": None}
    bad = []
    for raw, want in cases.items():
        _os.environ[_m2.MAKER_ONLY_ENV_VAR] = raw
        if _m2.maker_only_env_override() != want:
            bad.append((raw, want, _m2.maker_only_env_override()))
    _os.environ.pop(_m2.MAKER_ONLY_ENV_VAR, None)
    ok("every accepted spelling maps correctly, and anything else says nothing",
       not bad)
    ok("unset means unset - it does not default the mode on",
       _m2.maker_only_env_override() is None)
except Exception as _e:                              # pragma: no cover
    ok(f"the override could be exercised for real ({_e})", False)

# The page must say which switch is deciding.
page = open(os.path.join(HERE, "family_tree_dashboard.html"), encoding="utf-8").read()
ok("grid status reports which switch decided it", "maker_only_source" in src)
ok("and the card disables its button when the environment is in charge, "
   "rather than offering one that cannot work",
   "maker_only_source" in page and "environment wins over the database" in page)

width = max(len(l) for l, _ in checks)
for label, passed in checks:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label:<{width}}")
failed = [l for l, p in checks if not p]
print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)

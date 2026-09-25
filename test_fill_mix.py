"""How a leg really filled must be counted, never assumed.

The bill for not having this, 2026-09-25:

    fee_safe_floor_pct() - the function whose entire contract is "a branch
    can never be set to a spacing whose full cycle is a guaranteed real
    loss" - priced the floor off the MAKER rate whenever maker orders were
    on. But grid_buy() says what it does:

        "maker first (cheap, may not fill), market fallback
         (always fills, costs more)"

    After MAKER_ORDER_WAIT_SECONDS an unfilled maker order becomes a
    MARKET order and pays taker. On the live account:

        floor (maker-priced)      0.90%
        round trip, both maker    0.70%
        round trip, both taker    1.50%

    Everything from 0.90% to 1.70% was certified fee-safe while being a
    guaranteed loss on any cycle that fell back. I nearly recommended
    tightening the live fleet to 1.25%, inside that band.

    The floor now prices the taker leg unconditionally. That is correct,
    and it is conservative: if maker legs really do fill almost always,
    the fleet trades wider than it needs to. NOTHING IN THE CODEBASE
    COUNTED, so neither the safe answer nor the tighter one could be
    chosen on evidence.

THE RULE THIS FILE PROTECTS: every filled leg is counted by how it
actually filled, on all four paths (maker buy, market buy, maker sell,
market sell). A rate over zero legs is not a measurement and reports None,
never 0% and never 100%. The counter can never break a trade. And counting
does not by itself license a tighter floor - the floor still prices the
worst case until someone reads this evidence and decides.

Run: python3 test_fill_mix.py
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


def body(name):
    node = fn(name)
    if node is None:
        return ""
    stmts = node.body
    if stmts and isinstance(stmts[0], ast.Expr) and isinstance(stmts[0].value, ast.Constant):
        stmts = stmts[1:]
    return "\n".join(ast.unparse(s) for s in stmts)


ok("the counter exists", fn("_record_fill_leg") is not None)
ok("a reader exists", fn("get_fill_mix") is not None)


# --- all FOUR paths are counted, with the right verdict ------------------
def calls_in(name):
    """(is_maker_arg) for each _record_fill_leg call in this function."""
    node = fn(name)
    found = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_record_fill_leg":
            key = ast.unparse(n.args[0])
            val = n.args[1].value if isinstance(n.args[1], ast.Constant) else None
            found.append((key, val))
    return found


buy, sell = calls_in("grid_buy"), calls_in("grid_sell")
ok("grid_buy counts both of its paths", len(buy) == 2)
ok("grid_sell counts both of its paths", len(sell) == 2)
ok("grid_buy records exactly one maker and one taker outcome",
   sorted(v for _k, v in buy) == [False, True])
ok("grid_sell records exactly one maker and one taker outcome",
   sorted(v for _k, v in sell) == [False, True])
ok("buy legs are counted under the buy key",
   all("BUY" in k for k, _v in buy))
ok("sell legs are counted under the sell key",
   all("SELL" in k for k, _v in sell))

# The maker branch is the one that logs "MAKER" - make sure True is on it,
# not merely that one True and one False exist somewhere.
buy_src = body("grid_buy")
maker_idx = buy_src.index("_record_fill_leg(GRID_FILL_MIX_BUY_KEY, True)")
taker_idx = buy_src.index("_record_fill_leg(GRID_FILL_MIX_BUY_KEY, False)")
ok("the maker count sits on the maker branch, before the market fallback",
   maker_idx < buy_src.index("place_market_buy") < taker_idx)

# --- it can never break a trade -----------------------------------------
rec = body("_record_fill_leg")
ok("the counter swallows its own errors", "except Exception" in rec)
ok("the counter never re-raises", "raise" not in rec)

# --- a rate over zero legs is not a measurement --------------------------
read = body("get_fill_mix")
ok("maker_rate is None when nothing has filled", "if legs else None" in read)
ok("the blended rate is None until something filled",
   "if maker_rate is not None else None" in read)
ok("it still reports the taker round trip as the conservative case",
   "taker_round_trip_fee_rate" in read)

# --- counting is not, by itself, permission ------------------------------
floor = body("fee_safe_floor_pct")
ok("REGRESSION: the floor still prices the WORST case, not the measured mix",
   "worst_case_leg_fee_rate" in floor and "get_fill_mix" not in floor)

# --- it is visible ------------------------------------------------------
ok("the fill mix is on the grid status payload", '"fill_mix"' in src)
router = open(os.path.join(HERE, "routers", "trading_dashboard.py"), encoding="utf-8").read()
ok("an endpoint exposes it", "/grid-status/fill-mix" in router)


# --- the arithmetic, as behaviour ---------------------------------------
def blended(maker_rate, maker_leg, taker_leg):
    return (maker_rate * maker_leg + (1 - maker_rate) * taker_leg) * 2


MAKER_LEG, TAKER_LEG = 0.0035, 0.0075   # live rates, 2026-09-25
ok("all-maker blends to the 0.70% round trip",
   abs(blended(1.0, MAKER_LEG, TAKER_LEG) - 0.007) < 1e-9)
ok("all-taker blends to the 1.50% round trip",
   abs(blended(0.0, MAKER_LEG, TAKER_LEG) - 0.015) < 1e-9)
ok("a half-and-half mix blends to 1.10%",
   abs(blended(0.5, MAKER_LEG, TAKER_LEG) - 0.011) < 1e-9)
ok("even a 90% maker rate still costs more than the all-maker assumption",
   blended(0.9, MAKER_LEG, TAKER_LEG) > 0.007)
# The distinction the floor turns on, and one I got wrong writing this
# file: at a 90% maker rate the AVERAGE round trip is 0.78%, so a 1.25%
# step averages +0.47% and looks fine. It is still not fee-safe, because
# the 10% of cycles that fall back on BOTH legs pay the full 1.50% and
# lose 0.25% every time. The floor's contract is that no cycle is a
# guaranteed loss - not that the mean comes out positive.
ok("at a 90% maker rate a 1.25% step is positive ON AVERAGE",
   0.0125 - blended(0.9, MAKER_LEG, TAKER_LEG) > 0)
ok("REGRESSION: but its all-taker cycles still lose, which is what the floor guards",
   0.0125 - blended(0.0, MAKER_LEG, TAKER_LEG) < 0)
ok("a favourable average never lifts the floor - only the worst case sets it",
   "get_fill_mix" not in floor and "worst_case_leg_fee_rate" in floor)
ok("the live 2.00% step clears even an all-taker mix",
   0.02 - blended(0.0, MAKER_LEG, TAKER_LEG) > 0)

width = max(len(l) for l, _ in checks)
for label, passed in checks:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label:<{width}}")
failed = [l for l, p in checks if not p]
print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)

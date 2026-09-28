"""The maker leg rate has to survive a restart, like the taker rate does.

THE ASYMMETRY

refresh_real_fee_rate() measures BOTH legs in one fee-tier lookup. The
taker round trip was written to TradingBotState and reloaded by
get_effective_round_trip_fee_rate(). The maker leg was assigned to a module
global and nothing else - never written, never reloaded.

WHY THAT MOVED MONEY

worst_case_leg_fee_rate() returns the TAKER leg the moment the maker rate
reads None. With maker-ONLY mode on (it is), the market fallback does not
exist, so the honest worst case IS the maker leg:

    floor with the measured maker leg    0.90%
    floor without it                     1.70%

So a restart, or any worker that had not itself fetched the fee tier,
near-doubled the spacing floor with nothing having changed in the market.
Observed live 2026-09-28 from one deploy in the same second: one worker
computed current_floor_pct 0.009 while another reported
maker_round_trip_fee_rate 0.015 - the taker rate wearing the maker label.

The same rate feeds expected_leg_fee_rate(), which is what
_pick_profitable_slice_to_sell() prices a sale with. So the same slice
looked sellable or not depending on which worker was asked.

WHAT MUST NOT CHANGE: the floor still fails CLOSED. Nothing measured ->
taker. Maker-only off -> taker. Result clamped to the taker leg. Durability
was the bug; optimism was never the fix.
"""
import ast
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = open(os.path.join(HERE, "crypto_grid_bot.py"), encoding="utf-8").read()
TREE = ast.parse(SRC)

GLOBAL = "_cached_real_maker_fee_rate"
ACCESSOR = "get_effective_maker_leg_fee_rate"


def fn(name):
    return next(n for n in ast.walk(TREE)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def body_of(name):
    """The function's code WITHOUT its docstring - every docstring here
    names the global to explain the trap, so a text search would match."""
    node = fn(name)
    stmts = [s for s in node.body
             if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)
                     and isinstance(s.value.value, str))]
    return ast.Module(body=stmts, type_ignores=[])


def names_in(name):
    return {n.id for n in ast.walk(body_of(name)) if isinstance(n, ast.Name)}


def calls_in(name):
    return {n.func.id for n in ast.walk(body_of(name))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}


# ------------------------------------------------- 1. it is written down
def test_the_maker_rate_is_persisted():
    assert "row.starting_capital = float(%s)" % GLOBAL in SRC


def test_it_is_persisted_on_the_insert_path_too():
    """A first-ever write creates the row. If only the update path carried
    the maker rate, an account whose row did not exist yet would persist
    the taker rate and silently drop the maker one."""
    assert "starting_capital=%s" % GLOBAL in SRC


def test_a_failed_tier_fetch_does_not_erase_the_last_good_measurement():
    """A gap is not a zero: maker=None must leave the stored value alone,
    not overwrite it with nothing."""
    assert "if %s is not None:\n                        row.starting_capital" % GLOBAL in SRC


# ------------------------------------------------- 2. it is read back
def test_the_accessor_reads_the_persisted_column():
    b = body_of(ACCESSOR)
    src = ast.dump(b)
    assert "starting_capital" in src
    assert "REAL_FEE_RATE_STATE_KEY" in names_in(ACCESSOR)


def test_the_accessor_returns_none_when_nothing_was_ever_measured():
    """None is the signal every caller turns into 'price the taker leg'.
    A default here would be the optimism this must never introduce."""
    node = fn(ACCESSOR)
    returns = [n for n in ast.walk(node) if isinstance(n, ast.Return)]
    bare = [r for r in returns
            if r.value is None or (isinstance(r.value, ast.Constant) and r.value.value is None)]
    assert bare, "the accessor must have a None return"
    consts = {r.value.value for r in returns
              if isinstance(r.value, ast.Constant) and r.value.value is not None}
    assert not consts, f"the accessor must not return a default rate, found {consts}"


def test_a_db_error_is_not_treated_as_a_measurement():
    b = ast.dump(body_of(ACCESSOR))
    assert "ExceptHandler" in b


# ---------------------------------- 3. the floor uses the durable value
def test_the_floor_consults_the_accessor_not_the_raw_global():
    assert ACCESSOR in calls_in("worst_case_leg_fee_rate")
    assert GLOBAL not in names_in("worst_case_leg_fee_rate")


def test_the_floor_still_fails_closed_on_both_guards():
    """Asserted on the parsed body: two early returns of the taker leg,
    one for 'nothing measured', one for 'maker-only is off'."""
    b = body_of("worst_case_leg_fee_rate")
    rets = [n for n in ast.walk(b) if isinstance(n, ast.Return)]
    taker_returns = [r for r in rets
                     if isinstance(r.value, ast.Name) and r.value.id == "taker_leg"]
    assert len(taker_returns) == 2, "both fail-closed paths must survive"
    assert "is_maker_only_active" in calls_in("worst_case_leg_fee_rate")


def test_the_result_is_still_clamped_to_the_taker_leg():
    b = body_of("worst_case_leg_fee_rate")
    assert any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "min"
               for n in ast.walk(b)), "no arrangement of maker orders can cost MORE than taker"


# --------------------------- 4. no consumer reads the raw global again
@pytest.mark.parametrize("consumer", [
    "expected_leg_fee_rate",     # prices every sell decision
    "get_fill_mix",              # what the dashboard reports
    "grid_buy",                  # the rate stamped on a real maker buy
    "grid_sell",                 # and on a real maker sell
    "worst_case_leg_fee_rate",   # the floor
])
def test_consumer_uses_the_durable_accessor(consumer):
    assert GLOBAL not in names_in(consumer), (
        f"{consumer} still reads the in-process global - that is the bug")
    assert ACCESSOR in calls_in(consumer)


def test_only_the_setter_persister_and_accessor_touch_the_global():
    owners = {"refresh_real_fee_rate", ACCESSOR}
    for node in ast.walk(TREE):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in owners:
                continue
            if GLOBAL in names_in(node.name):
                pytest.fail(f"{node.name} reads {GLOBAL} directly")


# ------------------------------------------------------------ mutation
@pytest.mark.parametrize("old,new", [
    ("    maker_leg = await get_effective_maker_leg_fee_rate()\n    if maker_leg is None:",
     "    maker_leg = _cached_real_maker_fee_rate\n    if maker_leg is None:"),
    ("                    if %s is not None:\n                        row.starting_capital = float(%s)" % (GLOBAL, GLOBAL),
     "                    row.starting_capital = float(%s or 0.0)" % GLOBAL),
])
def test_breaking_it_fails_these_tests(old, new):
    assert SRC.count(old) == 1, "mutation anchor moved - this test is blind"
    mutated = SRC.replace(old, new, 1)
    tree = ast.parse(mutated)
    node = next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == "worst_case_leg_fee_rate")
    reverted = GLOBAL in {x.id for x in ast.walk(node) if isinstance(x, ast.Name)}
    erases = ("row.starting_capital = float(%s or 0.0)" % GLOBAL) in mutated
    assert reverted or erases, "the mutation left the fix intact - the test cannot fail"

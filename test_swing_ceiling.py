"""A step above 3x a coin's hourly swing does not trade wider - it does not
trade at all.

WHAT HAPPENED. The net-edge gate refuses any target over 3.0x the coin's
own average hourly swing: "would sit unfilled". The fleet minimum is
one-directional and knew nothing about that gate, so raising it from 2.50%
to 3.00% pushed SOL-USD over - 2.55x and passing before, 3.06x and refused
after. Found in the live activity feed, where all 40 of the most recent
events were GATE_BLOCK.

Measured across all 20 live coins:

    BTC   0.56% swing -> 1.68% ceiling   blocked at 2.50% too
    ETH   0.75% swing -> 2.25% ceiling   blocked at 2.50% too
    SOL   0.98% swing -> 2.94% ceiling   blocked by the 3.00% change

$597 of capital that could not place a buy at any price. Two of those three
were stranded before this change, so the cap fixes more than it repairs.

WHAT THE CAP MUST NOT BECOME: a way to trade below the fee floor. The floor
is a profitability guarantee; the swing multiple is a fill-probability
statement. Where they disagree the FLOOR wins and the coin keeps refusing,
which is the correct answer for a coin too quiet to grid.
"""
import ast
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = open(os.path.join(HERE, "crypto_grid_bot.py"), encoding="utf-8").read()
TREE = ast.parse(SRC)

# measured 2026-09-28, 14 days of hourly candles
SWING = {"BTC-USD": 0.0056, "ETH-USD": 0.0075, "SOL-USD": 0.0098,
         "SHIB-USD": 0.0119, "JASMY-USD": 0.0155, "NEAR-USD": 0.0282}


def _floor_guard_node():
    """The `if _ceiling >= _hard_floor:` node itself.

    Found by walking the AST rather than slicing characters: the first
    version took a 2200-character window after the else and it ran past the
    block into later code that legitimately mentions new_grid_pct, so the
    test failed on correct code. A character window is not a scope.
    """
    fn = next(n for n in ast.walk(TREE)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "run_grid_branch_cycle")
    for n in ast.walk(fn):
        if (isinstance(n, ast.If) and isinstance(n.test, ast.Compare)
                and isinstance(n.test.left, ast.Name) and n.test.left.id == "_ceiling"):
            return n
    raise AssertionError("the floor guard is gone")


def cycle_src():
    fn = next(n for n in ast.walk(TREE)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "run_grid_branch_cycle")
    return "\n".join(SRC.splitlines()[fn.lineno - 1:fn.end_lineno])


def const(name):
    import re
    m = re.search(name + r" = ([0-9.]+)", SRC)
    return float(m.group(1)) if m else None


# ------------------------------------------- the cap matches the gate
def test_the_multiple_matches_the_gate_it_exists_to_satisfy():
    """Duplicated by value because the scanner imports from this module, so
    the reverse would be circular. If the gate's limit moves and this does
    not, the cap silently stops matching it."""
    import crypto_nine_coin_scanner as scanner
    assert const("SWING_CEILING_MULTIPLE") == scanner.DEFAULT_MAX_TARGET_SWING_MULTIPLE


# ----------------------------------------- the arithmetic, per real coin
@pytest.mark.parametrize("coin,swing", sorted(SWING.items()))
def test_a_capped_step_passes_the_gate_that_refused_it(coin, swing):
    mult = const("SWING_CEILING_MULTIPLE")
    ceiling = mult * swing
    capped = min(0.030, ceiling)
    assert capped / swing <= mult + 1e-9, f"{coin} still over the gate"


def test_the_three_stranded_coins_become_tradeable():
    mult = const("SWING_CEILING_MULTIPLE")
    for coin in ("BTC-USD", "ETH-USD", "SOL-USD"):
        assert 0.030 / SWING[coin] > mult, f"{coin} was not actually blocked at 3.00%"
        assert mult * SWING[coin] >= 0.009, f"{coin}'s ceiling is under the fee floor"


def test_coins_that_were_fine_are_not_touched():
    mult = const("SWING_CEILING_MULTIPLE")
    for coin in ("SHIB-USD", "JASMY-USD", "NEAR-USD"):
        assert 0.030 <= mult * SWING[coin] + 1e-9, f"{coin} would be needlessly capped"


# ------------------------------------- the floor still wins, always
def test_a_ceiling_under_the_fee_floor_does_not_lower_the_step():
    """The one thing this must never become: a route to trading below the
    fee floor. Asserted on the cycle body - the cap is gated on the ceiling
    clearing the floor, and the else branch only logs."""
    node = _floor_guard_node()
    # the `if` body may set the step; the `else` body may only log
    wrote = [n for n in ast.walk(ast.Module(body=node.orelse, type_ignores=[]))
             if isinstance(n, ast.Name) and n.id == "new_grid_pct"]
    assert not wrote, "the too-quiet branch must not set a step"
    sets = [n for n in ast.walk(ast.Module(body=node.body, type_ignores=[]))
            if isinstance(n, ast.Name) and n.id == "new_grid_pct"]
    assert sets, "the clearing branch must set the capped step"


def test_the_cap_only_ever_lowers():
    body = cycle_src()
    i = body.index("_ceiling = SWING_CEILING_MULTIPLE")
    block = body[i:i + 2200]
    assert "if _step_now > _ceiling" in block, "the cap must be conditional on being over"


def test_an_unreadable_swing_changes_nothing():
    body = cycle_src()
    i = body.index("_swing_pct = await engine.get_average_hourly_swing_pct")
    block = body[i - 200:i + 600]
    assert "except Exception:" in block and "_swing_pct = None" in block
    assert "if _swing_pct and _swing_pct > 0:" in block


def test_it_runs_after_the_fleet_minimum_or_it_would_be_undone():
    body = cycle_src()
    assert body.index("new_grid_pct = FLEET_MIN_STEP_PCT") < body.index("SWING_CEILING_MULTIPLE")


def test_it_can_be_switched_off_without_a_deploy():
    import crypto_grid_bot as g
    saved = os.environ.get("GRID_SWING_CEILING")
    try:
        os.environ["GRID_SWING_CEILING"] = "off"
        assert g._swing_ceiling_enabled() is False
        os.environ["GRID_SWING_CEILING"] = ""
        assert g._swing_ceiling_enabled() is True
    finally:
        if saved is None:
            os.environ.pop("GRID_SWING_CEILING", None)
        else:
            os.environ["GRID_SWING_CEILING"] = saved


# ------------------------------------------------------------ mutation
def test_removing_the_floor_guard_would_let_a_quiet_coin_trade_under_it():
    body = cycle_src()
    old = "if _ceiling >= _hard_floor:"
    assert body.count(old) == 1, "mutation anchor moved - this test is blind"
    # a coin whose 3x ceiling sits under the fee floor
    tiny_swing, floor = 0.0020, 0.009
    assert const("SWING_CEILING_MULTIPLE") * tiny_swing < floor

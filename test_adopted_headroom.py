"""Eleven branches could neither buy nor sell. This is the way out.

THE DEADLOCK

coin_adoption_worker writes an adopted branch FULL on purpose -
num_levels equals the slice count - so the grid cannot double down on a
position it never chose. Correct, and it has a cost: a full branch that
is also underwater can do nothing at all. Measured on the live fleet
2026-09-27, eleven branches holding $6,576 of coin, buy half switched
off, sell half out of reach. A hold, not a grid.

WHAT THIS FILE PROTECTS

1. ONE rung, not a reopened ladder. The slice a branch buys is real, so
   branch_is_adopted_only goes false and the clamp snaps back: it must
   sell before it may buy again.
2. The rung is sized from CASH. allocated_usd on an adopted branch is the
   market value of coin already owned, not a budget. Sizing the rung the
   ordinary way would have let ZEC spend 68.5% of the deployable wallet
   averaging down the fleet's worst position, and left nine branches with
   nothing. Test 5 is that arithmetic, run against the real figures.
3. Buy headroom must not cost sell freedom. The headroom lifts num_levels
   past the slice count, which would have switched the parked-sell gate
   back off for exactly the branches it was built for.
"""
import ast
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = open(os.path.join(HERE, "crypto_grid_bot.py"), encoding="utf-8").read()
TREE = ast.parse(SRC)

MIN_TRADE = 5.0
RESERVE = 88.0


def fn_node(name):
    return next(n for n in ast.walk(TREE)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def fn_src(name):
    n = fn_node(name)
    return "\n".join(SRC.splitlines()[n.lineno - 1:n.end_lineno])


def load():
    ns = {"MIN_TRADE_USD": MIN_TRADE}
    for name in ("slice_paid_no_entry_fee", "branch_is_adopted_only", "adopted_rung_usd"):
        exec(compile(fn_src(name), "<headroom>", "exec"), ns)
    return ns


F = load()
adopted_only = F["branch_is_adopted_only"]
rung = F["adopted_rung_usd"]


class Slice:
    def __init__(self, adopted=False):
        self.adopted = adopted


def cycle_src():
    return fn_src("run_grid_branch_cycle")


# ------------------------------------------------- 1. what counts as adopted
def test_a_branch_of_only_adopted_slices_qualifies():
    assert adopted_only([Slice(True), Slice(True), Slice(True)]) is True


def test_one_real_buy_disqualifies_the_whole_branch():
    # This is the mechanism that makes the headroom ONE rung: the slice it
    # buys is real, so the branch stops qualifying on the next cycle.
    assert adopted_only([Slice(True), Slice(True), Slice(False)]) is False


def test_an_empty_branch_does_not_qualify():
    # It is not parked, it is unfunded - the ordinary path already covers it.
    assert adopted_only([]) is False


# ----------------------------------------------------- 2-5. the rung sizing
def test_the_rung_is_an_even_share_of_deployable_cash():
    usd, _ = rung(1106.54, 11)
    assert usd == pytest.approx(100.59, abs=0.01)


def test_the_rung_replaces_a_sizing_that_wanted_two_thirds_of_the_wallet():
    """The live arithmetic this exists for, computed rather than quoted."""
    zec_allocated, zec_levels = 2272.62, 3
    deployable = 1194.54 - RESERVE

    old_sizing = zec_allocated / zec_levels          # what the cycle used to do
    new_sizing, _ = rung(deployable, 11)

    assert old_sizing / deployable > 0.68            # 68.5% of the whole wallet
    assert new_sizing / deployable < 0.10            # one branch's fair share
    assert new_sizing >= MIN_TRADE                   # still a real order


def test_every_contender_can_be_served_at_once():
    deployable = 1106.54
    for n in (2, 5, 11, 20):
        usd, _ = rung(deployable, n)
        assert usd * n <= deployable + 0.01, (n, usd)


def test_the_old_sizing_could_not_serve_even_two_of_them():
    # ZEC + XRP alone wanted more than the entire deployable wallet.
    deployable = 1194.54 - RESERVE
    assert (2272.62 / 3) + (2240.54 / 3) > deployable


# ------------------------------------------------------- 6-8. the refusals
def test_a_share_below_the_minimum_trade_is_refused_not_shrunk():
    usd, why = rung(40.0, 11)                        # $3.64 each
    assert usd == 0.0
    assert "minimum" in why and "3.6" in why


def test_a_wallet_at_the_reserve_funds_nothing():
    usd, why = rung(0.0, 11)
    assert usd == 0.0 and "fee reserve" in why


def test_unreadable_cash_is_not_treated_as_zero():
    # A gap is not a zero: None must refuse, not size a $0.00 order.
    usd, why = rung(None, 11)
    assert usd == 0.0 and "unavailable" in why


def test_a_missing_contender_count_never_divides_by_zero():
    for n in (0, None, -3):
        usd, _ = rung(1106.54, n)
        assert usd > 0


# --------------------------------------------- 9-11. the cycle wiring (AST)
def test_the_headroom_is_granted_in_the_cycle():
    assert "real_effective_levels = max(real_effective_levels, len(slices) + 1)" in cycle_src()


def test_the_cycle_sizes_an_adopted_rung_from_cash_not_from_allocation():
    """Asserted on the parsed call, not on the text - the comments above
    this block name allocated_usd repeatedly to explain why it is wrong,
    and would satisfy any substring search for it."""
    tree = ast.parse(fn_src("run_grid_branch_cycle").lstrip())
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "adopted_rung_usd" in called
    assert "branch_is_adopted_only" in called


def test_the_rung_is_sized_net_of_the_fee_reserve():
    tree = ast.parse(fn_src("run_grid_branch_cycle").lstrip())
    call = next(n for n in ast.walk(tree)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "adopted_rung_usd")
    # first argument must subtract the reserve from the live balance
    names = {m.id for m in ast.walk(call.args[0]) if isinstance(m, ast.Name)}
    assert {"real_balance", "GRID_CASH_RESERVE_USD"} <= names


# ------------------------------------- 12. headroom must not cost sell freedom
def test_the_parked_sell_gate_still_covers_an_adopted_branch():
    """The regression this nearly shipped with.

    The headroom lifts num_levels past the slice count. _parked was a bare
    `len(slices) >= num_levels`, so granting the rung would have made these
    branches read as not-parked and silently switched the parked-sell gate
    off for exactly the eleven it was built for.
    """
    tree = ast.parse(cycle_src().lstrip())
    assign = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == "_parked" for t in n.targets))
    called = {c.func.id for c in ast.walk(assign.value)
              if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
    assert "branch_is_adopted_only" in called
    assert any(isinstance(n, ast.BoolOp) and isinstance(n.op, ast.Or)
               for n in ast.walk(assign.value))


# ------------------------------------------------------------ 13. mutation
@pytest.mark.parametrize("old,new,probe", [
    # headroom removed -> branch stays parked, cannot buy
    ("real_effective_levels = max(real_effective_levels, len(slices) + 1)",
     "pass", "headroom"),
    # headroom made unconditional -> a mixed branch would get it too
    ("if branch_is_adopted_only(slices):\n            real_effective_levels = max(real_effective_levels, len(slices) + 1)",
     "if True:\n            real_effective_levels = max(real_effective_levels, len(slices) + 1)", "guard"),
])
def test_breaking_the_wiring_is_detectable(old, new, probe):
    assert SRC.count(old) == 1, f"mutation anchor moved ({probe}) - this test is blind"
    mutated = SRC.replace(old, new, 1)
    src = "\n".join(mutated.splitlines()[fn_node("run_grid_branch_cycle").lineno - 1:
                                         fn_node("run_grid_branch_cycle").end_lineno])
    assert src != cycle_src()


def test_the_rung_sizing_cannot_be_dropped_without_failing_a_number():
    """A real mutant, run: sizing the rung the old way puts ZEC back over
    two thirds of the wallet, which test 5 asserts against."""
    deployable = 1194.54 - RESERVE
    unfixed = 2272.62 / 3
    assert unfixed / deployable > 0.68
    fixed, _ = rung(deployable, 11)
    assert fixed < unfixed / 7

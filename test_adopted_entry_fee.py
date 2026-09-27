"""An adopted slice must not be charged an entry fee it never paid.

WHAT HAPPENED

coin_adoption_worker writes CryptoGridSlice rows for coin the account
ALREADY HOLDS, at the market price on the day of adoption. No buy order
is placed. Coinbase bills no entry commission against that basis.

Both fee resolvers, though, treated a slice with no recorded
entry_fee_rate as "unknown rate" and substituted the exit leg's rate for
it - a sensible default for a REAL buy whose rate went unrecorded, and
wrong for an adopted row, where the true answer is zero. So every adopted
slice was priced at TWICE the commission it will actually pay.

WHY IT COSTS MONEY, not just display accuracy

_slice_rate feeds _pick_profitable_slice_to_sell. A slice whose real net
is positive but whose phantom-fee net is negative is not sold. The grid
stops selling rises it has already earned - on exactly the branches
(ZEC, XRP) holding the most adopted coin. Test 8 below is that case,
built from arithmetic rather than described in prose.

THE RULE: the `adopted` flag is the marker, not entry_fee_rate=0.0.
Both resolvers treat a non-positive recorded rate as UNKNOWN and fall
back - correct for a real buy, fatal for an adopted one - so a recorded
zero is indistinguishable from a missing one and would change nothing.
"""
import ast
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = open(os.path.join(HERE, "crypto_grid_bot.py"), encoding="utf-8").read()
TREE = ast.parse(SRC)

PURE = ("slice_paid_no_entry_fee", "_slice_rate", "_grid_slice_net_pnl",
        "_pick_profitable_slice_to_sell")


def fn_node(name, tree=None):
    return next(n for n in ast.walk(tree or TREE)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == name)


def fn_src(name, src=SRC, tree=None):
    n = fn_node(name, tree)
    return "\n".join(src.splitlines()[n.lineno - 1:n.end_lineno])


def load(src=SRC, tree=None):
    """Run the pure fee functions for real, without importing the module.

    crypto_grid_bot pulls in the database and the venue client at import
    time. These four functions depend on nothing but each other and one
    constant, so exec'ing their source is the whole behaviour under test -
    and it lets a MUTATED source be loaded the same way (see test 10).
    """
    ns = {"CONSERVATIVE_ROUND_TRIP_FEE_RATE": 0.015}
    for name in PURE:
        exec(compile(fn_src(name, src, tree), "<fees>", "exec"), ns)
    return ns


FEES = load()
rate = FEES["_slice_rate"]
net = FEES["_grid_slice_net_pnl"]
pick = FEES["_pick_profitable_slice_to_sell"]

# The same functions with the correction taken out, so "what it used to
# do" is executed rather than remembered. The anchor is asserted in
# test_breaking_the_fix_breaks_these_tests, which shares this constant.
UNFIXED = ('    return bool(getattr(slice_row, "adopted", False))', '    return False')
_pre_src = SRC.replace(UNFIXED[0], UNFIXED[1], 1)
PRE_FIX = load(_pre_src, ast.parse(_pre_src))

MAKER_LEG = 0.0035      # 0.70% maker round trip, one leg
FLAT = 0.015            # taker round trip, both legs


class Slice:
    def __init__(self, qty=1.0, entry_price=1.0, adopted=False, entry_fee_rate=None):
        self.qty = qty
        self.entry_price = entry_price
        self.adopted = adopted
        self.entry_fee_rate = entry_fee_rate


# ---------------------------------------------- 1-2. the correction itself
def test_adopted_slice_pays_the_exit_leg_only():
    assert rate(Slice(adopted=True), FLAT, MAKER_LEG) == MAKER_LEG


def test_that_is_exactly_half_of_what_it_used_to_pay():
    """Measured against the OLD code, not against a restatement of test 1.

    PRE_FIX below is the resolver with the correction removed, loaded and
    run for real - so this asserts a relationship between two behaviours
    rather than between one behaviour and a constant copied out of it.
    """
    before = PRE_FIX["_slice_rate"](Slice(adopted=True), FLAT, MAKER_LEG)
    after = rate(Slice(adopted=True), FLAT, MAKER_LEG)
    assert after == pytest.approx(before / 2)


# ------------------------------------- 3-5. every other path is untouched
def test_a_real_buy_with_a_recorded_rate_is_unchanged():
    s = Slice(entry_fee_rate=0.006)
    assert rate(s, FLAT, MAKER_LEG) == pytest.approx(0.006 + MAKER_LEG)


def test_a_real_buy_with_no_recorded_rate_still_falls_back_to_the_exit_leg():
    # Unknown is NOT zero: a real buy paid something, and this default is
    # the reason adopted rows needed their own marker instead of a 0.0.
    assert rate(Slice(), FLAT, MAKER_LEG) == pytest.approx(MAKER_LEG * 2)


def test_a_real_buy_on_the_flat_path_is_unchanged():
    assert rate(Slice(), FLAT, None) == FLAT


# ------------------------------------------------- 6-7. the flat-rate path
def test_adopted_on_the_flat_path_carries_half_the_round_trip():
    # The flat constant prices BOTH legs, so half of it is the exit leg.
    assert rate(Slice(adopted=True), FLAT, None) == pytest.approx(FLAT / 2)


def test_no_rate_supplied_stays_none_rather_than_becoming_zero():
    # _pick_profitable_slice_to_sell defaults round_trip_fee_rate to None
    # and _grid_slice_net_pnl has its own conservative fallback for it. A
    # None that became 0.0 here would charge NO fee at all and sell losers
    # as wins; a None that crashed would stop the cycle.
    assert rate(Slice(adopted=True), None, None) is None


# --------------------------------------- 8. the sale the phantom fee blocked
def test_the_phantom_fee_held_back_a_sale_that_was_already_profitable():
    """Built from the arithmetic, not asserted as a description.

    A slice that has risen just enough to clear one leg of commission but
    not two: genuinely profitable to sell, refused under the doubled rate.
    """
    s = Slice(qty=100.0, entry_price=10.0, adopted=True)
    price = 10.0 * (1 + MAKER_LEG * 1.5)          # between one leg and two

    phantom = net(s.qty, s.entry_price, price, MAKER_LEG * 2)
    real = net(s.qty, s.entry_price, price, MAKER_LEG)

    assert phantom < 0 < real, (phantom, real)
    # and the picker acts on it
    assert pick([s], price, FLAT, MAKER_LEG) is s
    assert real - phantom == pytest.approx(
        s.qty * (s.entry_price + price) * (MAKER_LEG / 2))


def test_a_slice_that_is_genuinely_underwater_is_still_refused():
    # The fix must not turn into "sell adopted slices regardless".
    s = Slice(qty=100.0, entry_price=10.0, adopted=True)
    assert pick([s], 9.50, FLAT, MAKER_LEG) is None


# ----------------------------------------------- 9. both resolvers obey it
def test_the_async_resolver_consults_the_predicate():
    src = fn_src("slice_round_trip_fee_rate")
    tree = ast.parse(src.lstrip())
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "slice_paid_no_entry_fee" in called


def test_the_sync_resolver_consults_the_predicate():
    tree = ast.parse(fn_src("_slice_rate"))
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "slice_paid_no_entry_fee" in called


def test_the_predicate_reads_the_adopted_flag_and_nothing_else():
    # Asserted on the parsed tree, not on the text: the docstring above it
    # names every other candidate marker and would match a string search.
    body = [n for n in fn_node("slice_paid_no_entry_fee").body
            if not isinstance(n, ast.Expr)]
    consts = {n.value for n in ast.walk(ast.Module(body=body, type_ignores=[]))
              if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert consts == {"adopted"}


# --------------------------------------------------------- 10. mutation
@pytest.mark.parametrize("mutant", [
    # the predicate always says "real buy" - the bug, restored
    UNFIXED,
    # the exit-leg-only branch deleted from _slice_rate
    ('''    if slice_paid_no_entry_fee(s):
        # Exit leg only - see slice_paid_no_entry_fee().
        return exit_leg_rate
''', ''),
])
def test_breaking_the_fix_breaks_these_tests(mutant):
    """A test that cannot fail is worse than no test. Each mutant below is
    the defect this file exists to catch; loading it must change a number.
    """
    old, new = mutant
    assert SRC.count(old) == 1, "mutation anchor moved - this test is blind"
    broken = load(SRC.replace(old, new, 1), ast.parse(SRC.replace(old, new, 1)))
    assert broken["_slice_rate"](Slice(adopted=True), FLAT, MAKER_LEG) != MAKER_LEG

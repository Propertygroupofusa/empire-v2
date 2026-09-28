"""The worker writes to real allocations, so its guards are the test.

Every assertion here is about what it must NOT do. The one thing it may do -
lower a claim that exceeds the cash behind it - is already covered by
test_reconcile.py; this file is about the rails around the write.
"""
import ast
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = open(os.path.join(HERE, "reconcile_worker.py"), encoding="utf-8").read()
TREE = ast.parse(SRC)


def fn(name):
    return next(n for n in ast.walk(TREE)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def src_of(name):
    n = fn(name)
    return "\n".join(SRC.splitlines()[n.lineno - 1:n.end_lineno])


# ------------------------------------------------- it defaults to OFF
def test_no_env_defers_to_the_db_rather_than_deciding():
    import reconcile_worker as W
    saved = os.environ.pop("GRID_RECONCILE_MODE", None)
    try:
        assert W.env_mode() is None
    finally:
        if saved is not None:
            os.environ["GRID_RECONCILE_MODE"] = saved


def test_a_missing_switch_and_an_unreadable_one_are_different():
    """The trap this avoids: collapsing them would let the worker arm itself
    because a database read hiccupped. It cannot tell a switch nobody has set
    from a switch it failed to read, and one of those means someone turned
    it OFF - so the error path is hardcoded off while the missing-row path
    carries the owner's standing instruction."""
    body = src_of("armed_now")
    assert "if row is None:\n            return ARMED_BY_DEFAULT" in body
    i = body.index("except Exception")
    assert "return False" in body[i:]
    assert "ARMED_BY_DEFAULT" not in body[i:], (
        "the error path must never fall through to the default")


def test_the_default_is_overridable_without_a_deploy():
    """A control that needs a deploy to flip is not a control you can use
    when something is going wrong.

    Asserted on the parsed body with the DOCSTRING STRIPPED. Written first
    against the raw source and it failed - the docstring above armed_now
    explains the precedence and so names ARMED_BY_DEFAULT before any code
    does. Same trap as every other prose-matching test in this repo: never
    search a docstring for the thing the code is supposed to do.
    """
    node = fn("armed_now")
    stmts = [x for x in node.body
             if not (isinstance(x, ast.Expr) and isinstance(x.value, ast.Constant)
                     and isinstance(x.value.value, str))]
    code = ast.dump(ast.Module(body=stmts, type_ignores=[]))
    assert code.index("env_mode") < code.index("ARMED_BY_DEFAULT")
    assert "base_capital" in code


def test_an_unreadable_switch_stays_off():
    """Fail-closed. A worker that writes to allocations must not arm itself
    because a database read hiccupped."""
    body = src_of("armed_now")
    assert "staying OFF" in body
    assert "return False" in body


@pytest.mark.parametrize("raw,expected", [
    ("arm", True), ("armed", True), ("on", True), ("true", True), ("1", True),
    ("observe", False), ("off", False), ("false", False), ("0", False),
    ("", None), ("maybe", None),
])
def test_env_switch_parsing(raw, expected):
    import reconcile_worker as W
    saved = os.environ.get("GRID_RECONCILE_MODE")
    try:
        os.environ["GRID_RECONCILE_MODE"] = raw
        assert W.env_mode() is expected
    finally:
        if saved is None:
            os.environ.pop("GRID_RECONCILE_MODE", None)
        else:
            os.environ["GRID_RECONCILE_MODE"] = saved


# -------------------------------------------------- the write is bounded
def test_the_write_only_ever_lowers():
    """Re-checked against the LIVE row, not the planned one: a branch whose
    allocation moved between plan and write must not be set from a stale
    figure, and one already at or below target is skipped."""
    body = src_of("run_once")
    assert "float(row.allocated_usd) <= c[\"new_allocated_usd\"]" in body
    assert "continue" in body


def test_dry_run_writes_nothing():
    body = src_of("run_once")
    i = body.index("if dry_run or not corrections:")
    j = body.index("row.allocated_usd = c[")
    assert i < j, "the dry-run return must come before any write"
    assert "return report" in body[i:j]


def test_the_default_is_dry_run():
    node = fn("run_once")
    defaults = dict(zip([a.arg for a in node.args.args][-len(node.args.defaults):],
                        node.args.defaults))
    assert isinstance(defaults["dry_run"], ast.Constant)
    assert defaults["dry_run"].value is True


def test_it_never_touches_slices_or_places_orders():
    """Its whole remit is one number. Anything that moves coin is out of
    scope and must stay out."""
    for forbidden in ("grid_buy", "grid_sell", "place_order", "CryptoGridSlice(",
                      "db.delete", "db.add("):
        assert forbidden not in SRC, forbidden


def test_coin_basis_comes_from_slices_not_from_the_allocation():
    """The allocation is the claim being tested. Deriving basis from it
    would make the check agree with itself."""
    body = src_of("_branch_rows")
    assert "s.qty" in body and "s.entry_price" in body
    assert "allocated_usd" not in body.split("basis[s.bot_name]")[1].split("return")[0]


def test_only_active_branches_are_considered():
    assert "CryptoGridBranch.active.is_(True)" in src_of("_branch_rows")


# ----------------------------------------------------- failure is silent
def test_a_failed_pass_writes_nothing_and_keeps_going():
    body = src_of("loop")
    assert "nothing written" in body
    assert "await asyncio.sleep(CHECK_SECONDS)" in body


def test_an_unarmed_pass_still_reports_what_it_would_have_done():
    """Observing must be visible, or nobody can tell whether arming it would
    be safe - which is exactly the question that needed answering first."""
    assert "OBSERVING" in src_of("loop")


# ------------------------------------------------------------ mutation
def test_removing_the_lower_only_check_would_allow_a_raise():
    old = 'if row.allocated_usd is None or float(row.allocated_usd) <= c["new_allocated_usd"]:'
    assert SRC.count(old) == 1, "mutation anchor moved - this test is blind"
    mutated = SRC.replace(old, "if row.allocated_usd is None:", 1)
    assert 'float(row.allocated_usd) <= c["new_allocated_usd"]' not in mutated

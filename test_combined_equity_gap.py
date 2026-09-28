"""A missing side must never be added in as $0.00.

Live on 2026-09-28 the combined $1M card read "$1,007.47 of $1,000,000 -
0.10%" while its own legend said "Coinbase unavailable". The real combined
balance was near $11,800. `(alpaca or 0.0) + (crypto or 0.0)` turned an
unreadable side into a zero and showed the survivor as the total.

Every test here fails if that `or 0.0` comes back.
"""
import ast
import pathlib

import pytest

SRC = pathlib.Path(__file__).with_name("routers") / "trading_dashboard.py"


def _combine():
    """Load combine_equity without importing the whole router (which drags
    in the DB engine, Coinbase client and every model)."""
    tree = ast.parse(SRC.read_text())
    fn = next(
        (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "combine_equity"),
        None,
    )
    assert fn is not None, "combine_equity must exist - the rule has to live somewhere testable"
    ns = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(SRC), "exec"), ns)
    return ns["combine_equity"]


GOAL = 1_000_000.0


def test_both_sides_present_sums_normally():
    combined, pct, reason = _combine()(1007.47, 10781.63, GOAL)
    assert combined == pytest.approx(11789.10)
    assert pct == pytest.approx(1.1789, abs=1e-4)
    assert reason is None


def test_missing_crypto_gives_no_total_not_the_alpaca_leg():
    combined, pct, reason = _combine()(1007.47, None, GOAL)
    assert combined is None, "the surviving leg is not the combined total"
    assert pct is None
    assert reason and "Coinbase" in reason


def test_missing_alpaca_gives_no_total_not_the_crypto_leg():
    combined, pct, reason = _combine()(None, 10781.63, GOAL)
    assert combined is None
    assert pct is None
    assert reason and "Alpaca" in reason


def test_both_missing_still_refuses_rather_than_reporting_zero():
    combined, pct, reason = _combine()(None, None, GOAL)
    assert combined is None and pct is None and reason


def test_a_real_zero_side_is_not_a_gap():
    """An account genuinely holding $0.00 on one side still has a total.
    UNKNOWN and ZERO are different answers and must stay different."""
    combined, pct, reason = _combine()(0.0, 10781.63, GOAL)
    assert combined == pytest.approx(10781.63)
    assert reason is None


def test_progress_is_capped_at_one_hundred_percent():
    combined, pct, _ = _combine()(900_000.0, 900_000.0, GOAL)
    assert combined == pytest.approx(1_800_000.0)
    assert pct == 100.0


def test_the_or_zero_substitution_is_gone_from_the_endpoint():
    """Assert on the parsed tree, not a substring - the docstring above
    quotes the bug verbatim and would match any grep for it."""
    tree = ast.parse(SRC.read_text())
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.BoolOp) or not isinstance(node.op, ast.Or):
            continue
        last = node.values[-1]
        if isinstance(last, ast.Constant) and last.value == 0.0:
            names = [v.id for v in node.values if isinstance(v, ast.Name)]
            if any(n.endswith("_equity") for n in names):
                offenders.append(names)
    assert not offenders, f"an equity side is still being defaulted to 0.0: {offenders}"

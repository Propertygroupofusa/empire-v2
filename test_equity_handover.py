"""Option 1: market_brain owns the equity side, prop_bot stops entering it.

Chosen by the account owner on 2026-09-28 after six prop_bot orders put
$734.57 - 73% of a $1,007 account - into META in one second.

The hazard this guards against is written down in the repo already, in
alpaca_swing_bot.py: "one bot had stopped trading PSQ while the other
kept buying it, on the SAME Alpaca account." Two traders on one account
is how that happens. So the handover is a swap, not an addition.

THE PROPERTY THAT MATTERS MOST HERE: prop_bot must still be able to
EXIT the equity position it is already carrying. The gate is entry-only.
"""
import ast
import inspect
import os

import market_brain as mb

ROOT = os.path.dirname(os.path.abspath(__file__))
PROP = os.path.join(ROOT, "prop_bot.py")


def _fn(name):
    tree = ast.parse(open(PROP, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in prop_bot.py")


def test_the_handover_gate_lives_in_try_open_which_is_entry_only():
    """If this ever moves to a function that also closes positions,
    prop_bot could be blocked from exiting a position it holds - which
    would be far worse than the bug being fixed."""
    src = ast.get_source_segment(open(PROP, encoding="utf-8").read(), _fn("try_open"))
    assert "is_market_brain_equities_active" in src, \
        "the handover check is not in try_open"


def test_no_exit_path_consults_the_handover_flag():
    tree = ast.parse(open(PROP, encoding="utf-8").read())
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        n = node.name.lower()
        if not any(w in n for w in ("close", "exit", "sell", "liquidat")):
            continue
        src = ast.get_source_segment(open(PROP, encoding="utf-8").read(), node) or ""
        assert "is_market_brain_equities_active" not in src, (
            f"{node.name}() consults the handover flag - an exit must never "
            f"be blocked by it")


def test_the_flag_defaults_off_and_fails_off():
    """It can only ever REMOVE prop_bot entries. A read failure must
    leave prop_bot working exactly as before, never silently stop it
    trading - so this one fails OPEN, unlike the money-sizing gates
    which fail closed."""
    src = ast.get_source_segment(open(PROP, encoding="utf-8").read(),
                                 _fn("is_market_brain_equities_active"))
    assert "return False" in src
    assert "except Exception" in src, "a DB hiccup must not halt prop_bot"


def test_only_equities_are_handed_over():
    """The gate's own condition must name the equities list.

    The first draft asserted that the string appeared anywhere in
    try_open - and it does, in the approved_universe list a few lines
    below - so a build whose GATE keyed on ["futures"] passed it. The
    mutant survived and the test was the reason. This walks to the `if`
    that actually contains the handover call and reads ITS condition.
    """
    src = open(PROP, encoding="utf-8").read()
    fn = _fn("try_open")
    gates = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        body = ast.dump(ast.Module(body=node.body, type_ignores=[]))
        if "is_market_brain_equities_active" in body:
            gates.append(ast.get_source_segment(src, node.test))
    assert gates, "no `if` guards the handover call"
    for cond in gates:
        assert '["equities"]' in cond, (
            f"the handover gate's condition is {cond!r} - it must key on the "
            f"equities list")
        for other in ("futures", "crypto", "commodities", "inverse_etfs"):
            assert f'["{other}"]' not in cond, (
                f"the gate also keys on {other} - only equities are handed over")


# ── market_brain's own gate ───────────────────────────────────────────

REAL = [{"symbol": "META", "market_value": 705.90}]
EQ = 980.16


def test_the_gate_refuses_when_the_real_account_is_already_over():
    assert mb.can_open_position({}, 0.20, account_positions=REAL, equity=EQ) is False


def test_the_same_call_without_the_account_still_allows_it():
    # The hazard, stated as a test: this is what running the module
    # unmodified would have done on its first cycle after a redeploy.
    assert mb.can_open_position({}, 0.20) is True


def test_omitting_the_account_keeps_the_original_behaviour_exactly():
    # Backward compatibility, so nothing calling this the old way changes.
    assert mb.can_open_position({"X": {"alloc": 0.5}}, 0.20) is False
    assert mb.can_open_position({"X": {"alloc": 0.2}}, 0.20) is True


def test_it_uses_the_owners_ceiling_not_a_restated_one():
    assert mb.CONFIG["max_exposure"] == 0.60
    src = inspect.getsource(mb.can_open_position)
    assert 'CONFIG["max_exposure"]' in src
    assert "0.60" not in src.replace("60%", ""), "the ceiling is restated as a literal"


if __name__ == "__main__":
    import sys
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"  PASS {name}")
            except AssertionError as e:
                fails += 1; print(f"  FAIL {name}: {e}")
    sys.exit(1 if fails else 0)

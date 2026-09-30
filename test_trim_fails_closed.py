"""An unreadable grid must stop the trimmer, not license it.

THE MECHANISM THIS CLOSES. crypto_grid_bot.fleet_tracked_units_by_product
returns (None, None) when the read fails - deliberately, its docstring
says "never an empty dict, which would read as 'the fleet holds nothing'
and pass every check trivially." The trimmer then did:

    protected = ()
    try:
        _units, _ = await _grid.fleet_tracked_units_by_product()
        if _units:
            protected = {...}
    except Exception:
        log.warning("could not read grid positions - trimming without it")

None is falsy, so `if _units:` collapsed the careful UNKNOWN back into
"protect nothing" - and since returning None raises nothing, the warning
never fired. Silent, on an account that is genuinely rate-limited.

What it produces, measured 2026-09-30: ALGO, TIA and PRIME each carried
REAL grid buys (adopted=False) while holding 0.017%, 0.000% and 0.000%
of the units their slices claim. $1,195.34 unbacked across eight
branches. QNT's exit refused 200 times because the coin behind it was
sold out from under the branch.

A skipped trim costs minutes of concentration. A blind trim costs coin,
leaves a phantom slice row, and needs a manual reconcile.
"""
import ast

SRC = open("auto_trim_worker.py").read()
TREE = ast.parse(SRC)


def _fn(name):
    for n in ast.walk(TREE):
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == name:
            return ast.get_source_segment(SRC, n)
    raise AssertionError(f"{name} not found")


BODY = _fn("check_once")


def _code_only(src):
    """Statements with comments stripped.

    The first version of the next test matched "if _units:" inside this
    file's own comment DESCRIBING the old bug, and failed against correct
    code. Assertions about control flow have to read control flow.
    """
    out = []
    for line in src.splitlines():
        cut = line.split("#", 1)[0].rstrip()
        if cut:
            out.append(cut)
    return "\n".join(out)


CODE = _code_only(BODY)


def test_none_is_tested_explicitly_not_for_truthiness():
    assert "if _units is None:" in CODE, \
        "a falsy test collapses the producer's UNKNOWN back into 'protect nothing'"
    assert "if _units:" not in CODE, "the truthiness test is still live code"


def test_an_unreadable_grid_returns_before_any_sell():
    i = BODY.index("if _units is None:")
    block = BODY[i:i + 900]
    assert "return {" in block, "it must RETURN, not fall through to the trim"
    assert '"acted": 0' in block
    assert "skipped_because" in block and "grid_positions_unreadable" in block
    # and the return must come before anything that places an order
    assert BODY.index("_place_market_sell") > i, \
        "a sell path is reachable before the unreadable-grid return"


def test_a_raised_exception_also_lands_on_the_skip():
    # Both failure shapes - a raise and a None return - must reach the
    # same refusal. Previously only the raise was even logged.
    i = BODY.index("except Exception as exc:")
    j = BODY.index("if _units is None:")
    assert i < j, "the except must fall through to the None check"
    assert "_units = None" in BODY[i:j], \
        "an exception must set _units to None so it reaches the same refusal"


def test_protection_is_built_only_from_a_real_reading():
    i = BODY.index("if _units is None:")
    after = BODY[i:]
    assert "protected = {p.split(" in after, \
        "the protected set must be built AFTER the unreadable check, not before"


def test_the_docstring_contract_it_depends_on_still_holds():
    # If fleet_tracked_units_by_product ever starts returning {} instead
    # of None on failure, this whole guard goes blind again.
    grid = open("crypto_grid_bot.py").read()
    i = grid.index("async def fleet_tracked_units_by_product")
    doc = grid[i:i + 900]
    assert "(None, None) if the read fails" in doc, \
        "the producer no longer promises None on failure; this guard assumes it"
    assert "never an empty dict" in doc


def test_every_other_unreadable_input_already_refuses():
    # This fix makes the grid read consistent with the rest of the
    # function rather than introducing a new posture.
    for marker in ("trim history unreadable", "census failed",
                   "account could not be read"):
        assert marker in SRC, f"expected existing fail-closed path: {marker}"


if __name__ == "__main__":
    import sys
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"  PASS {name}")
            except AssertionError as e:
                fails += 1; print(f"  FAIL {name}: {e}")
            except Exception as e:
                fails += 1; print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{fails} failure(s)")
    sys.exit(1 if fails else 0)

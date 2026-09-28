"""A held position whose stop is never evaluated must not be silent.

prop_bot's exit pass reads this cycle's scan for each held position:

    data = scans.get(contract)
    if not data:
        continue

That `continue` skipped the WHOLE exit evaluation - stop loss,
breakeven ratchet, giveback, max hold - and said nothing. The branch
immediately above it, for an unknown contract, logs an error. This one
did not. A position can sit through any number of cycles with its stop
never once evaluated, and no log line, metric or decision row anywhere
records that it happened.

Found on 2026-09-28 while asking why META, held at -3.90% against a
1.5% stop for over two hours, had not been exited. Everything else was
ruled out first: the cycle was alive (last_cycle_at 36s old), the
kill-condition halt explicitly lets exits run, positions are reloaded
from the DB and reconciled against the broker each cycle, the stop math
returns True at -3.90%, and all four account order-block flags were
false with live_trading on. This silent skip is the one remaining path
by which a configured stop simply never runs.

The skip STAYS - an exit cannot be decided without a price. What these
tests hold is that it can never again be invisible.
"""
import ast
import os

ROOT = os.path.dirname(os.path.abspath(__file__))
PROP = os.path.join(ROOT, "prop_bot.py")


def _src():
    return open(PROP, encoding="utf-8").read()


def _exit_pass():
    """The loop over held positions inside run_prop_cycle."""
    src = _src()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "run_prop_cycle")
    for node in ast.walk(fn):
        if (isinstance(node, ast.For) and isinstance(node.target, ast.Tuple)
                and any(isinstance(e, ast.Name) and e.id == "position"
                        for e in node.target.elts)):
            return node, src
    raise AssertionError("the exit pass over open_prop_positions was not found")


def test_the_exit_pass_never_skips_a_position_silently():
    """Every `continue` in the loop that manages exits must be preceded
    by something that says so - a log call, in the same if-block."""
    loop, src = _exit_pass()
    offenders = []
    for node in ast.walk(loop):
        if not isinstance(node, ast.If):
            continue
        if not any(isinstance(st, ast.Continue) for st in node.body):
            continue
        body = ast.dump(ast.Module(body=node.body, type_ignores=[]))
        if "log" not in body and "_record_trade_decision" not in body:
            offenders.append(ast.get_source_segment(src, node.test))
    assert not offenders, (
        "silent `continue` in the exit pass - a held position would go "
        f"unevaluated with nothing reporting it: {offenders}")


def test_a_missing_scan_says_the_position_is_unprotected():
    loop, src = _exit_pass()
    body = ast.get_source_segment(src, loop)
    assert "NO STOP CHECK" in body
    assert "unprotected" in body, \
        "the warning must say what it means for the position, not just that data is missing"


def test_it_also_reaches_the_decision_log_not_only_the_logfile():
    """Railway stdout rotates. A skipped stop must land somewhere
    queryable - the same decision log the mandate refusals use."""
    loop, src = _exit_pass()
    body = ast.get_source_segment(src, loop)
    assert "no_scan_data" in body
    assert "_record_trade_decision" in body


def test_logging_the_skip_can_never_itself_break_the_cycle():
    loop, src = _exit_pass()
    body = ast.get_source_segment(src, loop)
    assert "except Exception" in body, \
        "a failure while RECORDING a skip must not become a second failure"


def test_the_entry_pass_is_left_alone():
    """Declining to OPEN a position for want of data is correct and
    unremarkable. Only the exit pass needs to shout."""
    src = _src()
    silent = src.count("            data = scans.get(contract)\n"
                       "            if not data:\n"
                       "                continue")
    assert silent == 1, (
        f"expected exactly one remaining silent skip (the entry pass), found {silent}")


def test_the_skip_itself_is_still_there():
    """The fix is visibility, not behaviour: an exit genuinely cannot be
    decided without a price, and pretending otherwise would be worse."""
    loop, src = _exit_pass()
    body = ast.get_source_segment(src, loop)
    assert "continue" in body


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

"""A kill condition must stop NEW risk, never trap OPEN risk.

run_prop_cycle used to `return` the moment check_kill_conditions fired.
That skipped the entire rest of the cycle - including all SEVEN
close_position paths. While halted the bot could not take profit, could
not cut a loser, could not manage an open position at all.

Found live 2026-09-28: buying power $89.08 against a $150 floor, on
UNSETTLED CASH-ACCOUNT PROCEEDS - $721.56 of $810.64 not yet settled.
That halt clears itself on settlement, but it could sit for a day with a
real position open ($197.46 of DOG) and no way out of it.

Every one of the four kill conditions is a reason to stop BUYING. None is
a reason to stop SELLING:

    daily loss limit    you want out, not frozen in
    buying power low    settlement restricts buying only; selling is
                        always allowed in a cash account
    equity below floor  the moment you most need to manage
    too many positions  closing one is the fix
"""
import ast
import pathlib
import re

SRC = pathlib.Path(__file__).with_name("prop_bot.py").read_text()
TREE = ast.parse(SRC)


def _fn(name):
    for n in ast.walk(TREE):
        if isinstance(n, ast.AsyncFunctionDef) and n.name == name:
            return ast.get_source_segment(SRC, n)
    raise AssertionError(f"{name} not found")


CYCLE = _fn("run_prop_cycle")
AFTER_HALT = CYCLE[CYCLE.index("[KILL CONDITION]"):]


def test_the_halt_does_not_end_the_cycle():
    """The regression itself: a bare return inside the halt branch.

    Asserted on the PARSED `if should_halt:` block, not a slice of source.
    The first version searched the first 2000 characters of the text after
    the halt, and the explanatory comment is long enough that the return
    landed just past the window - so restoring the exact bug still passed.
    A test whose reach depends on comment length is not a test.
    """
    fn = next(n for n in ast.walk(TREE)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_prop_cycle")
    halt_ifs = [n for n in ast.walk(fn)
                if isinstance(n, ast.If)
                and isinstance(n.test, ast.Name) and n.test.id == "should_halt"]
    assert halt_ifs, "the `if should_halt:` branch was not found"
    for branch in halt_ifs:
        returns = [n for n in ast.walk(branch) if isinstance(n, ast.Return)]
        assert not returns, (
            "the cycle returns on a kill condition, which skips every exit path")


def test_the_halt_sets_the_entry_flag_instead():
    """The positive half: not returning is only right if something else
    stops new entries."""
    assert "entries_halted = _kill_detail" in CYCLE


def test_every_exit_path_stays_reachable_while_halted():
    assert len(re.findall(r"close_position\(", AFTER_HALT)) == 7, (
        "the number of exit paths after the halt changed - if a close was "
        "added or removed, confirm it is still reachable and update this")


def test_new_entries_are_blocked_at_the_single_chokepoint():
    body = _fn("try_open")
    assert "if entries_halted:" in body
    idx_guard = body.index("if entries_halted:")
    idx_first_return = body.index("return False")
    assert idx_guard < idx_first_return


def test_the_guard_is_the_first_thing_try_open_does():
    """A guard after any order-placing work is not a guard."""
    body = _fn("try_open")
    head = body[:body.index("if entries_halted:")]
    for placing in ("submit_order", "place_order", "close_position("):
        assert placing not in head, f"{placing} runs before the entry guard"


def test_entries_halted_always_exists_even_when_nothing_halts():
    """A NameError inside the cycle would be worse than the bug fixed."""
    assert "entries_halted = None" in CYCLE
    assert CYCLE.index("entries_halted = None") < CYCLE.index("[KILL CONDITION]")


def test_the_halt_is_still_recorded_and_still_shouted():
    """Blocking entries instead of returning must not quieten the halt."""
    assert "log.critical(f\"[KILL CONDITION] Halting bot:" in CYCLE
    assert "_record_trade_decision" in AFTER_HALT[:1800]


def test_check_kill_conditions_itself_is_unchanged():
    """The thresholds are not what is being fixed here. This changes what
    HAPPENS on a halt, not when one fires."""
    body = _fn("check_kill_conditions") if False else None
    fn = next(n for n in ast.walk(TREE)
              if isinstance(n, ast.FunctionDef) and n.name == "check_kill_conditions")
    src = ast.get_source_segment(SRC, fn)
    assert "abs(capital[\"max_daily_loss\"])" in src
    assert "abs(capital[\"critical_buying_power\"])" in src
    assert "equity < 800" in src


def test_the_floor_was_not_lowered_to_make_it_trade():
    """The tempting non-fix. $150 stays $150; the bug was the halt's blast
    radius, not its threshold."""
    import bot_mandates
    assert bot_mandates.APEX_MANDATE["capital"]["critical_buying_power"] == 150

"""An unfilled maker order is not a rejection.

Live, 2026-09-28: three ORDER_REJECTED events in one hour, all reading
"no reason reported" - and every one of them filled minutes later at the
same size.

    02:53:25  PEPE   rejected $44.33  ->  02:58:16  BUY filled
    03:03:45  SHIB   rejected $36.02  ->  03:05:43  BUY filled $36.02
    03:20:30  FLOKI  rejected $17.99  ->  03:26:37  BUY filled $17.99

Nothing had refused those orders. They were maker orders nobody took
inside the 240s window, under a mode whose entire point is to pass rather
than cross the spread.
"""
import ast
import pathlib

import order_outcome as oo

SRC = pathlib.Path(__file__).with_name("crypto_grid_bot.py")


def test_a_maker_expiry_is_not_an_order_rejected_event():
    event, msg = oo.event_for(oo.MAKER_EXPIRED, 17.99, wait_seconds=240)
    assert event == "MAKER_EXPIRED"
    assert event != "ORDER_REJECTED"
    assert "not a rejection" in msg.lower()
    assert "$17.99" in msg and "240s" in msg


def test_a_real_venue_rejection_still_reports_as_one_with_its_reason():
    event, msg = oo.event_for(oo.REJECTED, 36.02, detail="INSUFFICIENT_FUND")
    assert event == "ORDER_REJECTED"
    assert "INSUFFICIENT_FUND" in msg


def test_a_rejection_with_no_reason_says_the_venue_gave_none():
    """Distinct from the old catch-all: this one really was refused, and
    the missing reason is itself the finding."""
    event, msg = oo.event_for(oo.REJECTED, 36.02, detail=None)
    assert event == "ORDER_REJECTED"
    assert "no reason reported by the venue" in msg


def test_an_unknown_cause_counts_as_a_fault_not_as_routine():
    """Silence is not 'probably fine' - same rule as an unreadable balance
    never being a $0.00."""
    assert oo.is_execution_fault(None) is True
    assert oo.is_execution_fault("something_new") is True
    assert oo.is_execution_fault(oo.MAKER_EXPIRED) is False
    assert oo.event_for(None, 5.0)[0] == "ORDER_REJECTED"


def test_an_unpriced_amount_never_prints_as_a_dollar_figure():
    _, msg = oo.event_for(oo.MAKER_EXPIRED, None)
    assert "$" not in msg.split("maker-only")[0]
    assert "unpriced" in msg


def test_the_three_live_cases_all_classify_as_expiries():
    for amount in (44.33, 36.02, 17.99):
        event, _ = oo.event_for(oo.MAKER_EXPIRED, amount, wait_seconds=240)
        assert event == "MAKER_EXPIRED"


def test_the_buy_path_no_longer_hardcodes_order_rejected():
    """Assert on the parsed tree, not a substring: the comments in both
    files quote 'ORDER_REJECTED' verbatim while explaining the bug, and a
    grep for it would match those and pass regardless of the code.

    The live buy path must reach _record_gate_decision with a computed
    event type, never the literal.
    """
    tree = ast.parse(SRC.read_text())
    fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_grid_branch_cycle"),
        None,
    )
    assert fn is not None
    literals = [
        n.value for n in ast.walk(fn)
        if isinstance(n, ast.Constant) and n.value == "ORDER_REJECTED"
    ]
    assert not literals, (
        "run_grid_branch_cycle still names ORDER_REJECTED directly - the event "
        "type has to come from order_outcome.event_for, which knows why"
    )


def test_grid_buy_can_report_why_there_was_no_fill():
    """Without this parameter the caller has nothing to classify and is
    back to guessing."""
    tree = ast.parse(SRC.read_text())
    fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.AsyncFunctionDef) and n.name == "grid_buy"),
        None,
    )
    assert fn is not None
    names = [a.arg for a in fn.args.args] + [a.arg for a in fn.args.kwonlyargs]
    assert "outcome_out" in names

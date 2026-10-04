"""A sender nobody calls is not a feature.

Both notifiers were built and NEITHER was wired. The owner set
TRADE_EMAIL_MODE=send and ALERT_EMAIL_MODE=send and nothing would have
happened, because no loop called trade_notify.send() or
alert_sender.send_digest(). This pins the wiring itself.
"""
import ast

MAIN = open("main.py").read()
AW = open("alert_worker.py").read()
TW = open("trade_notify_worker.py").read()


def _fn(src, name):
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == name:
            return ast.get_source_segment(src, n)
    raise AssertionError(f"{name} not found")


# --- something actually calls them ----------------------------------------

def test_the_trade_notifier_is_started():
    assert "trade_notify_worker.run_periodically" in MAIN
    assert "import trade_notify_worker" in MAIN


def test_the_trade_loop_calls_the_sender():
    assert "trade_notify.send" in _fn(TW, "check_once")


def test_the_alert_loop_calls_the_digest():
    assert "alert_sender.send_digest" in _fn(AW, "drain_as_digest")
    assert "drain_as_digest" in _fn(AW, "run_sender_periodically")


# --- the digest route only runs when it should ----------------------------

def test_a_webhook_still_wins_over_email():
    # A receiver deliberately pointed at this queue should get every row,
    # not a summary of them.
    body = _fn(AW, "run_sender_periodically")
    assert body.index("channel_configured()") < body.index("email_armed()"), \
        "email must be the fallback, not the first choice"


def test_email_route_checks_armed_and_configured():
    body = _fn(AW, "run_sender_periodically")
    assert "email_armed()" in body and "email_configured()" in body


# --- the marks must not lie -----------------------------------------------

def test_a_failed_digest_does_not_mark_rows_sent():
    body = _fn(AW, "drain_as_digest")
    i = body.index("if ok:")
    assert 'r.status = "sent"' in body[i:i + 200], "sent is set outside the ok branch"
    assert body.count('r.status = "sent"') == 1


def test_a_failed_trade_send_holds_the_watermark():
    body = _fn(TW, "check_once")
    # There are TWO _set_mark calls and they are different things: the
    # first-run baseline (which deliberately sends nothing) and the one
    # after a successful send. The first version of this test matched the
    # baseline and failed against correct code. Check the LAST one.
    i = body.index("if not ok:")
    j = body.rindex("_set_mark(session_factory, newest)")
    assert i < j, "the post-send mark is written before the send is known to have worked"
    assert "held_at" in body[i:j], "a failed send must say the mark was held"


def test_the_baseline_mark_is_a_separate_path_that_sends_nothing():
    body = _fn(TW, "check_once")
    i = body.index("FIRST_RUN_IS_A_BASELINE")
    j = body.index("trade_notify.send")
    assert i < j, "the baseline must be decided before any send is attempted"
    baseline = body[i:body.index("if not rows:")]
    assert "trade_notify.send" not in baseline, \
        "the baseline path must not email the existing book"
    assert "return" in baseline, "the baseline path must return, not fall through"


def test_the_watermark_only_moves_forward():
    body = _fn(TW, "_set_mark")
    assert "max(value, int(row.base_capital or 0))" in body, \
        "a stale read could rewind the mark and re-send a batch"


def test_the_first_run_does_not_email_the_whole_book():
    body = _fn(TW, "check_once")
    assert "FIRST_RUN_IS_A_BASELINE" in body and "baselined_at" in body, \
        "162 existing closes would go out as one enormous first email"


def test_the_trade_loop_is_inert_until_armed():
    body = _fn(TW, "check_once")
    i = body.index("if not (trade_notify.armed() or force):")
    assert i < body.index("_get_mark"), \
        "it reads the book before checking whether it may send"


def test_no_route_out_does_not_spend_the_retry_budget():
    """MAX_ATTEMPTS exists to stop a poisoned MESSAGE retrying forever.

    It must not time out the infrastructure. Measured 2026-10-01 01:0xZ:
    79 rows sat at 4 of 6 attempts against a host that blocks outbound
    SMTP. Two more passes and the whole backlog would have been marked
    failed - permanently, before any working route existed - so it would
    have been lost at the exact moment it became deliverable.
    """
    body = _fn(AW, "drain_as_digest")
    assert "is_infrastructure_failure" in body, "no route and a rejection are the same here"
    i = body.index("elif infra:")
    j = body.index("r.attempts = (r.attempts or 0) + 1")
    assert i < j, "the infra branch must come before the attempt is spent"
    infra_branch = body[i:j]
    assert "r.attempts" not in infra_branch, "an unroutable pass still burns an attempt"
    assert 'r.status = "failed"' not in infra_branch, "it can still mark the backlog dead"
    assert "NO_ROUTE_COOLOFF" in infra_branch, "it would hammer a dead route every 30s"


def test_the_marker_is_what_the_sender_actually_returns():
    # The check is a marker match, so the two halves must agree or it
    # silently never fires.
    import alert_sender
    assert alert_sender.is_infrastructure_failure(
        "no transport delivered (sendgrid: no key, smtp:465: X)") is True
    assert alert_sender.is_infrastructure_failure("SMTPAuthenticationError") is False
    assert alert_sender.is_infrastructure_failure(None) is False
    # WHAT THE TWO HALVES AGREEING ACTUALLY LOOKS LIKE.
    #
    # This searched send_email's source for the marker's literal text, so it
    # failed the moment send_email was written the safe way - building its
    # failure from the shared NO_ROUTE_MARKER constant by NAME. That is the
    # construction that cannot drift from is_infrastructure_failure(), and
    # the test was penalising it while a hand-copied literal would have
    # passed. It had been red on main for some time, hiding real failures in
    # this file behind an expected one.
    #
    # So the round trip is asserted instead of the spelling: whatever
    # send_email returns when nothing can deliver must be recognised by the
    # function the worker calls on it. That is the contract.
    src = open("alert_sender.py").read()
    i = src.index("def send_email")
    body = src[i:]
    assert ("NO_ROUTE_MARKER" in body
            or alert_sender.NO_ROUTE_MARKER in body), \
        "send_email no longer builds its no-route failure from the marker"

    # The real proof, run rather than read. Two ways to have no route, and
    # the worker must recognise BOTH - otherwise it spends one of six
    # attempts on a pass that proved nothing about the message, and six
    # quiet cycles mark a perfectly good alert dead.
    import os
    _keys = ("SENDGRID_API_KEY", "GMAIL_EMAIL", "GMAIL_PASSWORD",
             "ALERT_WEBHOOK_URL", "TRADE_ALERT_EMAIL", "DAILY_BRIEF_EMAIL")
    saved = {k: os.environ.pop(k) for k in list(os.environ)
             if k.strip().upper() in _keys}
    try:
        # (a) an address but no transport
        os.environ["TRADE_ALERT_EMAIL"] = "someone@example.com"
        sent, err = alert_sender.send_email("subject", "body")
        assert sent is False, "it cannot have delivered with no credential"
        assert alert_sender.NO_ROUTE_MARKER in str(err), err
        assert alert_sender.is_infrastructure_failure(err), (
            "no transport is not being seen as infrastructure: " f"{err!r}")

        # (b) a transport but no address - this one was NOT recognised, and
        # it is the same loss arriving through the other door.
        os.environ.pop("TRADE_ALERT_EMAIL")
        sent, err = alert_sender.send_email("subject", "body")
        assert sent is False, "it cannot have delivered with no recipient"
        assert alert_sender.is_infrastructure_failure(err), (
            "the worker would spend a retry on a pass that had nowhere to "
            f"go: {err!r}")
    finally:
        for k in _keys:
            os.environ.pop(k, None)
        os.environ.update(saved)

    # A REJECTION IS NOT INFRASTRUCTURE. A transport that answered and
    # refused must still burn its attempt, or a genuinely poisoned message
    # retries forever - the exact thing MAX_ATTEMPTS exists for.
    assert alert_sender.is_infrastructure_failure(
        "SMTPAuthenticationError") is False
    assert alert_sender.is_infrastructure_failure("sendgrid: HTTP 400") is False


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
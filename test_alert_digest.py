"""76 queued alerts must arrive as ONE email, or the sender gets muted.

Measured state: 76 alerts queued, 0 delivered, because ALERT_WEBHOOK_URL
has never been set - while GMAIL_EMAIL and GMAIL_PASSWORD have been
sitting in the same process the whole time, already used by
daily_brief.py, prop_bot.py and notary_bot.py. A working route existed
and nothing used it.

The backlog is not 76 facts. It is dominated by one branch retrying one
order: QNT's exit was refused 200 times in 24 hours. Sending 76 emails
about that gets the sender muted - which is strictly WORSE than the
silence it replaces, because the muting also swallows the next real one.

So: group repeats, count them, send one message. And keep build_digest
pure, so the exact first message can be read before anything is sent.
"""
import os
import alert_sender as a


def A(sev="HIGH", kind="k", asset=None, message="something", detail=None):
    return {"severity": sev, "kind": kind, "asset": asset,
            "message": message, "detail": detail}


def _env(**kw):
    old = {k: os.environ.get(k) for k in kw}
    for k, v in kw.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return old


def _restore(old):
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# --- one message, not a flood ---------------------------------------------

def test_the_live_backlog_becomes_one_message():
    # 76 alerts, dominated by one repeating condition - the real shape.
    alerts = ([A(asset="QNT-USD", message="exit refused")] * 60
              + [A(asset="PEPE-USD", message="exit refused")] * 14
              + [A(sev="CRITICAL", asset="ZEC-USD", message="no stop")] * 2)
    d = a.build_digest(alerts)
    assert d["alerts"] == 76 and d["groups"] == 3, d
    assert "x60" in d["body"] and "x14" in d["body"], "repeats must be counted"
    assert d["body"].count("exit refused") == 2, "a repeat must not be reprinted"


def test_the_worst_severity_leads():
    alerts = [A(sev="INFO", message="quiet")] * 5 + [A(sev="CRITICAL", message="bad")]
    d = a.build_digest(alerts)
    assert d["worst_severity"] == "CRITICAL"
    assert d["subject"].startswith("[CRITICAL]"), d["subject"]
    assert d["body"].index("bad") < d["body"].index("quiet"), \
        "the critical line must come first, whatever the counts"


def test_a_flood_is_truncated_not_unbounded():
    d = a.build_digest([A(message=f"issue {i}") for i in range(200)])
    assert d["groups"] == 200
    assert "further distinct issue(s) not listed" in d["body"]
    assert d["body"].count("[HIGH]") <= a.MAX_DIGEST_GROUPS


def test_an_empty_queue_sends_nothing():
    for empty in ([], None, [None, "junk"]):
        d = a.build_digest(empty)
        assert d["subject"] is None, d
        ok, err = a.send_digest(empty)
        assert ok is False and "nothing" in err.lower()


# --- it must be readable before it is armed --------------------------------

def test_build_digest_is_pure():
    alerts = [A(message="x")]
    before = [dict(x) for x in alerts]
    a.build_digest(alerts)
    assert alerts == before, "build_digest mutated its input"


def test_off_by_default_so_nothing_fires_unasked():
    old = _env(ALERT_EMAIL_MODE=None, GMAIL_EMAIL="x@y.z", GMAIL_PASSWORD="p",
               TRADE_ALERT_EMAIL="to@y.z")
    try:
        assert a.email_armed() is False
        ok, err = a.send_digest([A()])
        assert ok is False and "ALERT_EMAIL_MODE" in err, err
    finally:
        _restore(old)


def test_force_sends_one_without_arming_the_route():
    # The single deliberate first send, so the owner judges it before it
    # starts arriving. It must not depend on the mode being set.
    old = _env(ALERT_EMAIL_MODE="off", GMAIL_EMAIL="", GMAIL_PASSWORD="")
    try:
        ok, err = a.send_digest([A()], force=True)
        assert ok is False and "not configured" in err, err
        assert "ALERT_EMAIL_MODE" not in err, "force must get past the mode gate"
    finally:
        _restore(old)


# --- secrets never leak ----------------------------------------------------

def test_a_missing_credential_is_named_never_valued():
    old = _env(GMAIL_EMAIL="", GMAIL_PASSWORD="", ALERT_EMAIL_MODE="send")
    try:
        ok, err = a.send_digest([A()])
        assert ok is False
        assert "GMAIL_PASSWORD" in err and "no value is ever read out" in err
    finally:
        _restore(old)


def test_a_send_failure_reports_the_type_only():
    # SMTP exception text can echo the conversation, including the login
    # line. Only the exception type is ever returned.
    import smtplib
    src = open("alert_sender.py").read()
    i = src.index("def send_digest")
    body = src[i:]
    assert 'f"{type(e).__name__} sending the digest"' in body
    assert "{e}" not in body.split("except Exception as e:")[-1][:200], \
        "the exception text itself must never be returned"


def test_the_recipient_reuses_the_existing_convention():
    old = _env(TRADE_ALERT_EMAIL="first@x.z", DAILY_BRIEF_EMAIL="second@x.z",
               GMAIL_EMAIL="self@x.z")
    try:
        assert a.recipient() == "first@x.z"
        _env(TRADE_ALERT_EMAIL="")
        assert a.recipient() == "second@x.z"
        _env(DAILY_BRIEF_EMAIL="")
        assert a.recipient() == "self@x.z", "must fall back to a real inbox"
    finally:
        _restore(old)


def test_the_digest_says_it_is_not_an_instruction():
    d = a.build_digest([A()])
    assert "places no order and moves no money" in d["body"]


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

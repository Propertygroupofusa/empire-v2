"""The grid closes trades and tells nobody. This is the telling.

crypto_grid_bot.py - which has done every one of the fleet's 162 round
trips - contains ZERO email code. A trade emailer exists in
crypto_coinbase_bot.py, a different and older bot. So the owner has been
reading a dashboard to find out whether he made money.

Two rules this has to hold:
  ONE MESSAGE PER BATCH. 2026-09-28 closed 35 round trips. Thirty-five
  emails is a muted sender, and a muted sender swallows the next real
  alert too.
  SILENT WHEN NOTHING HAPPENED. Mailing "0 trades" on a schedule teaches
  the reader to ignore the sender, which costs as much as never sending.
"""
import os
import trade_notify as tn


def T(pnl, product="XLM-USD", reason="profit_target"):
    return {"pnl": pnl, "product_id": product, "exit_reason": reason}


def _env(**kw):
    old = {k: os.environ.get(k) for k in kw}
    for k, v in kw.items():
        os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    return old


def _restore(old):
    for k, v in old.items():
        os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


# --- one message per batch -------------------------------------------------

def test_a_busy_day_is_one_message_not_thirty_five():
    d = tn.build([T(0.5)] * 35)
    assert d["count"] == 35
    assert d["subject"] == "empire-v2: 35 trades closed, +$17.50", d["subject"]
    assert "and 15 more" in d["body"], "a long batch must summarise its tail"
    # Count the actual trade lines. Asserting only on the summary line let
    # a removed cap survive: the tail sentence still printed while all 35
    # rows printed above it.
    rows = [l for l in d["body"].splitlines() if l.strip().startswith(("+$", "-$"))]
    assert len(rows) == tn.MAX_LINES, f"{len(rows)} lines printed, cap is {tn.MAX_LINES}"


def test_the_real_recent_batch_reads_correctly():
    # Sep 30's actual closes.
    d = tn.build([T(0.27, "SOL-USD", "parked_sell"), T(1.86, "APE-USD"),
                  T(6.35, "XLM-USD"), T(0.22, "BTC-USD"),
                  T(0.80, "NEAR-USD"), T(-0.004, "FLOKI-USD")])
    assert d["count"] == 6 and d["wins"] == 5 and d["losses"] == 1
    assert d["subject"] == "empire-v2: 6 trades closed, +$9.50", d["subject"]
    assert d["body"].index("XLM-USD") < d["body"].index("BTC-USD"), \
        "biggest winner should lead the lines"


# --- silence and honesty ---------------------------------------------------

def test_nothing_closed_sends_nothing():
    for empty in ([], None, ["junk", None]):
        d = tn.build(empty)
        assert d["subject"] is None
        ok, err = tn.send(empty)
        assert ok is False and "no closed trades" in err


def test_a_losing_batch_is_still_reported():
    # A notifier that only mentions the green ones is a worse lie than
    # silence.
    d = tn.build([T(-2.0), T(-1.5)])
    assert d["subject"] == "empire-v2: 2 trades closed, -$3.50", d["subject"]
    assert d["losses"] == 2 and d["wins"] == 0
    assert "-$    2.00" in d["body"] or "-$2.00" in d["body"].replace(" ", "")


def test_a_single_trade_is_not_pluralised():
    assert tn.build([T(1.0)])["subject"] == "empire-v2: 1 trade closed, +$1.00"


# --- armed deliberately ----------------------------------------------------

def test_off_by_default():
    old = _env(TRADE_EMAIL_MODE=None)
    try:
        assert tn.armed() is False
        ok, err = tn.send([T(1.0)])
        assert ok is False and "TRADE_EMAIL_MODE" in err
    finally:
        _restore(old)


def test_force_bypasses_the_mode_but_not_the_transport():
    """force skips the arming switch, never the need for a real route.

    The wording of the failure is deliberately NOT asserted here: when
    the transport chain gained an HTTPS leg, "not configured" became
    "no recipient resolved" / "no transport delivered", and an assertion
    pinned to the old sentence failed against correct code. What matters
    is that force got PAST the mode gate and still could not invent a
    delivery route.
    """
    old = _env(TRADE_EMAIL_MODE="off", GMAIL_EMAIL="", GMAIL_PASSWORD="",
               SENDGRID_API_KEY="", TRADE_ALERT_EMAIL="", DAILY_BRIEF_EMAIL="")
    try:
        ok, err = tn.send([T(1.0)], force=True)
        assert ok is False, "it claimed success with no transport at all"
        assert tn.MODE_ENV not in err, "force must pass the mode gate"
        assert err, "a failure must say something"
    finally:
        _restore(old)


# --- it must stay separate from the fault alerts ---------------------------

def test_trades_and_alerts_are_different_messages():
    # Merging them buries a real fault under a list of wins, or cries
    # wolf every time a coin sells at a profit.
    import alert_sender
    assert tn.MODE_ENV != alert_sender.EMAIL_MODE_ENV
    trade = tn.build([T(1.0)])["subject"]
    alert = alert_sender.build_digest(
        [{"severity": "HIGH", "message": "x"}])["subject"]
    assert trade != alert and "trade" in trade and "issue" in alert


def test_it_says_it_is_a_record_not_an_instruction():
    assert "places no order and moves no money" in tn.build([T(1.0)])["body"]


def test_garbage_pnl_does_not_crash_or_inflate():
    d = tn.build([T("oops"), T(None), T(2.0)])
    assert d["count"] == 3 and d["net"] == 2.0, d


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

#!/usr/bin/env python3
"""A cycle that never placed an order is not a maker expiry.

THE BUG
-------
grid_buy's maker-ONLY branch set `outcome_out["cause"]` to
order_outcome.MAKER_EXPIRED unconditionally, on every empty return from
place_maker_buy. But place_maker_buy has FOUR ways to return None and only
one of them is an expiry:

    below the minimum trade size   -> no order created
    order book unreadable, no bid  -> no order created
    size floors to zero            -> no order created
    the POST raised                -> UNKNOWN, an order may exist
    the order rested and expired   -> the only real expiry

MAKER_EXPIRED is the single member of order_outcome.BENIGN_CAUSES. So a
branch that was persistently too poor to trade, or reading a book that
would not read, reported as "the mode working exactly as designed" - and
is_execution_fault() said False, which is the counter that exists to make
execution problems findable. The same conflation on the sell side is what
sent a four-hour investigation after a resting order that never existed.

WHAT THIS FILE TESTS, AND HOW
-----------------------------
Behaviour, not source text. Every case below drives the REAL engine
function until it really does set its real _last_order_rested flag, then
puts that real flag through the REAL classifier, and asserts on the cause
that comes out. Nothing here greps crypto_grid_bot.py for a string: a test
that reads source could pass against code that computes the right label and
then throws it away, and the bug it is guarding against was precisely a
correct value being computed (order_rested WAS passed to the ledger) and
ignored one line later.

The three sell-side classes are named individually because they are the
three the live rows came from: BALANCE_READ_FAILED, NO_SELLABLE_INVENTORY,
NO_ASK. None of them may become MAKER_EXPIRED.
"""
import asyncio
import contextlib
import sys

import crypto_btc_compound_bot as engine
import crypto_grid_bot as grid
import order_outcome as oo

PID = "TEST-USD"

_failures = []
_passes = 0


def ok(label, condition, detail=""):
    global _passes
    if condition:
        _passes += 1
        print(f"  ok   {label}")
    else:
        _failures.append(f"{label}{(' - ' + detail) if detail else ''}")
        print(f"  FAIL {label}{(' - ' + detail) if detail else ''}")


@contextlib.contextmanager
def patched(module, **names):
    """Swap module attributes and always put them back."""
    missing = [n for n in names if not hasattr(module, n)]
    if missing:
        raise AssertionError(
            f"{module.__name__} has no {missing} - this test patches names that "
            f"must exist; a rename here is a real finding, not a test bug")
    old = {n: getattr(module, n) for n in names}
    try:
        for n, v in names.items():
            setattr(module, n, v)
        yield
    finally:
        for n, v in old.items():
            setattr(module, n, v)


def aval(value):
    """An async callable returning `value`, whatever it is handed."""
    async def _f(*a, **k):
        return value
    return _f


class _Resp:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def json(self):
        return self._payload


class _Ctx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Stands in for the aiohttp session. `post` either yields a canned
    response or raises, which is how the UNKNOWN path is reached."""

    def __init__(self, resp=None, raises=None):
        self._resp = resp
        self._raises = raises

    def post(self, *a, **k):
        if self._raises is not None:
            raise self._raises
        return _Ctx(self._resp)


ACCEPTED = {"success": True, "success_response": {"order_id": "oid-1"}}


# ─────────────────────────────────────────────────────────────────────────
# grid_buy, end to end: real engine branch -> real flag -> real classifier
# ─────────────────────────────────────────────────────────────────────────

def run_grid_buy(usd, *, engine_patches, session=None, wait_seconds=240):
    """Call the real grid_buy under maker-ONLY and report what it decided.

    Returns (result, outcome_dict, expiry_kwargs). Only the two recorders
    are stubbed, and only to capture: they write to the database, and what
    they WRITE is already covered by test_order_not_placed_split.py. The
    classification is the real thing.
    """
    seen = {}

    async def _record_expiry(session, product_id, side, bot_name=None, **kw):
        seen.update(kw)
        seen["product_id"] = product_id
        seen["side"] = side

    out = {}
    with patched(engine, **engine_patches), patched(
            grid,
            is_maker_orders_active=aval(True),
            is_maker_only_active=aval(True),
            maker_wait_seconds=aval(wait_seconds),
            _record_maker_only_skip=aval(None),
            _record_maker_expiry=_record_expiry):
        engine._last_order_rested.pop(PID, None)
        engine._last_order_error.pop(PID, None)
        engine._last_order_block.pop(PID, None)
        result = asyncio.run(grid.grid_buy(
            session or FakeSession(), usd, PID, "test-bot", outcome_out=out))
    return result, out, seen


def case_below_minimum():
    """$1 available against a $5 floor. Nothing is sent to the venue."""
    return run_grid_buy(
        1.0,
        engine_patches=dict(get_usd_balance=aval((1.0, None)),
                            get_best_bid_ask=aval((100.0, 100.1)),
                            get_product_size_decimals=aval(8)))


def case_no_bid():
    """The book would not read, so there is no bid to rest at."""
    return run_grid_buy(
        50.0,
        engine_patches=dict(get_usd_balance=aval((50.0, None)),
                            get_best_bid_ask=aval((None, None)),
                            get_product_size_decimals=aval(8)))


def case_floors_to_zero():
    """$50 of a $1bn coin at 2 decimals of size floors to nothing buyable."""
    return run_grid_buy(
        50.0,
        engine_patches=dict(get_usd_balance=aval((50.0, None)),
                            get_best_bid_ask=aval((1e9, 1.1e9)),
                            get_product_size_decimals=aval(2)))


def case_post_raised():
    """The POST died in flight. Coinbase may already hold the order."""
    return run_grid_buy(
        50.0,
        engine_patches=dict(get_usd_balance=aval((50.0, None)),
                            get_best_bid_ask=aval((100.0, 100.1)),
                            get_product_size_decimals=aval(8),
                            _auth_headers=lambda *a, **k: {}),
        session=FakeSession(raises=ConnectionResetError("connection reset")))


def case_really_expired():
    """An order rested for its whole window and no seller crossed it."""
    return run_grid_buy(
        50.0,
        engine_patches=dict(get_usd_balance=aval((50.0, None)),
                            get_best_bid_ask=aval((100.0, 100.1)),
                            get_product_size_decimals=aval(8),
                            _auth_headers=lambda *a, **k: {},
                            _await_fill=aval(None),
                            cancel_order=aval(True)),
        session=FakeSession(resp=_Resp(200, ACCEPTED)))


NON_ORDER_CASES = (
    ("below the minimum trade size", case_below_minimum),
    ("order book unreadable (no bid)", case_no_bid),
    ("size floors to zero", case_floors_to_zero),
)


def test_a_buy_that_never_reached_the_venue_is_not_a_maker_expiry():
    for label, case in NON_ORDER_CASES:
        result, out, expiry = case()
        cause = out.get("cause")
        print(f"\n  [{label}]")
        ok(f"{label}: grid_buy returns None", result is None)
        ok(f"{label}: the engine really recorded order_rested False",
           engine._last_order_rested.get(PID, "absent") is False
           or expiry.get("order_rested") is False,
           f"expiry kwargs: {expiry}")
        ok(f"{label}: cause is NOT MAKER_EXPIRED", cause != oo.MAKER_EXPIRED,
           f"cause was {cause!r}")
        ok(f"{label}: cause is NO_ORDER_CREATED", cause == oo.NO_ORDER_CREATED,
           f"cause was {cause!r}")
        ok(f"{label}: it counts as an execution fault",
           oo.is_execution_fault(cause) is True)
        event, msg = oo.event_for(cause, 50.0, detail=out.get("detail"),
                                  wait_seconds=out.get("wait_seconds"))
        ok(f"{label}: the durable event is not MAKER_EXPIRED",
           event != "MAKER_EXPIRED", f"event was {event!r}")
        ok(f"{label}: the durable event is ORDER_NOT_PLACED",
           event == "ORDER_NOT_PLACED", f"event was {event!r}")
        # "nothing rested" contains "rested", so this checks for the CLAIM,
        # not the word. A bare substring test passed the wrong message once.
        ok(f"{label}: the message says nothing rested and nothing filled",
           "nothing rested" in msg.lower() and "the order rested" not in msg.lower(),
           msg)
        ok(f"{label}: the ledger row and the reported cause agree",
           (expiry.get("order_rested") is False) == (cause == oo.NO_ORDER_CREATED),
           f"row order_rested={expiry.get('order_rested')!r} cause={cause!r}")


def test_an_unknown_outcome_stays_unknown():
    """The POST may have created the order before the connection broke.
    MAKER_EXPIRED would assert a resting order; NO_ORDER_CREATED would
    assert there was none. Neither is known, so neither is claimed."""
    print("\n  [the POST raised - UNKNOWN]")
    result, out, expiry = case_post_raised()
    cause = out.get("cause")
    ok("UNKNOWN: grid_buy returns None", result is None)
    ok("UNKNOWN: the engine left order_rested absent, not False",
       PID not in engine._last_order_rested,
       f"was {engine._last_order_rested.get(PID)!r}")
    ok("UNKNOWN: the ledger row carries None, not False and not True",
       expiry.get("order_rested", "missing") is None,
       f"row order_rested={expiry.get('order_rested', 'missing')!r}")
    ok("UNKNOWN: cause is NOT MAKER_EXPIRED", cause != oo.MAKER_EXPIRED,
       f"cause was {cause!r}")
    ok("UNKNOWN: cause is NOT NO_ORDER_CREATED either",
       cause != oo.NO_ORDER_CREATED, f"cause was {cause!r}")
    ok("UNKNOWN: cause is NO_FILL", cause == oo.NO_FILL, f"cause was {cause!r}")
    ok("UNKNOWN: it counts as an execution fault",
       oo.is_execution_fault(cause) is True)
    event, msg = oo.event_for(cause, 50.0, detail=out.get("detail"))
    ok("UNKNOWN: the durable event is not MAKER_EXPIRED",
       event != "MAKER_EXPIRED", f"event was {event!r}")
    ok("UNKNOWN: the message says UNKNOWN out loud", "UNKNOWN" in msg, msg)
    ok("UNKNOWN: the recorded reason does not assert a resting order",
       "rested at the bid" not in (expiry.get("reason") or ""),
       f"reason: {expiry.get('reason')!r}")


def test_a_real_expiry_is_still_benign_and_still_carries_its_window():
    """The fix must not swing the other way. An order that really rested
    for its whole window IS the mode working as designed, and turning
    those into faults would make the fault counter useless again."""
    print("\n  [a real expiry]")
    result, out, expiry = case_really_expired()
    cause = out.get("cause")
    ok("expiry: grid_buy returns None", result is None)
    ok("expiry: the engine really recorded order_rested True",
       engine._last_order_rested.get(PID) is True,
       f"was {engine._last_order_rested.get(PID)!r}")
    ok("expiry: cause is MAKER_EXPIRED", cause == oo.MAKER_EXPIRED,
       f"cause was {cause!r}")
    ok("expiry: it is NOT an execution fault",
       oo.is_execution_fault(cause) is False)
    ok("expiry: the wait window is reported", out.get("wait_seconds") == 240,
       f"wait_seconds={out.get('wait_seconds')!r}")
    event, msg = oo.event_for(cause, 50.0, wait_seconds=out.get("wait_seconds"))
    ok("expiry: the durable event is MAKER_EXPIRED", event == "MAKER_EXPIRED")
    ok("expiry: the message names the window", "240s" in msg, msg)


# ─────────────────────────────────────────────────────────────────────────
# The three sell-side classes, by name. grid_sell takes no outcome_out, so
# the real engine flag is put through the real classifier directly.
# ─────────────────────────────────────────────────────────────────────────

SELL_CASES = (
    # BALANCE_READ_FAILED - the wallet could not be read, so no order is
    # sized off a number nothing confirmed.
    ("BALANCE_READ_FAILED",
     dict(get_asset_balance=aval((None, "HTTP 500 from /accounts")),
          get_product_size_decimals=aval(2),
          get_best_bid_ask=aval((100.0, 100.1)))),
    # NO_SELLABLE_INVENTORY - the balance floors to zero at the product's
    # own precision, so there is nothing to sell.
    ("NO_SELLABLE_INVENTORY",
     dict(get_asset_balance=aval((0.0046, None)),
          get_product_size_decimals=aval(2),
          get_best_bid_ask=aval((100.0, 100.1)))),
    # NO_ASK - the book would not read, so there is no ask to rest at.
    ("NO_ASK",
     dict(get_asset_balance=aval((10.0, None)),
          get_product_size_decimals=aval(2),
          get_best_bid_ask=aval((None, None)))),
)


def test_none_of_the_three_sell_side_classes_becomes_a_maker_expiry():
    for label, patches in SELL_CASES:
        print(f"\n  [{label}]")
        with patched(engine, **patches):
            engine._last_order_rested.pop(PID, None)
            fill = asyncio.run(engine.place_maker_sell(
                FakeSession(), 1134.3, PID, wait_seconds=240))
        ok(f"{label}: place_maker_sell returns None", fill is None)
        rested = engine._last_order_rested.get(PID, "absent")
        ok(f"{label}: the real engine recorded order_rested False",
           rested is False, f"was {rested!r}")
        cause = oo.cause_for_maker_only_no_fill(rested)
        ok(f"{label}: does NOT classify as MAKER_EXPIRED",
           cause != oo.MAKER_EXPIRED, f"cause was {cause!r}")
        ok(f"{label}: classifies as NO_ORDER_CREATED",
           cause == oo.NO_ORDER_CREATED, f"cause was {cause!r}")
        ok(f"{label}: is not in BENIGN_CAUSES", cause not in oo.BENIGN_CAUSES)
        ok(f"{label}: counts as an execution fault",
           oo.is_execution_fault(cause) is True)
        ok(f"{label}: the durable event is not MAKER_EXPIRED",
           oo.event_for(cause, 25.0, detail="x")[0] != "MAKER_EXPIRED")


def test_maker_expired_is_the_only_benign_cause():
    """The guard that keeps the fix from being quietly undone. Adding
    anything else to BENIGN_CAUSES is how these rows get hidden again -
    is_execution_fault() reads this set and nothing else."""
    ok("BENIGN_CAUSES is exactly {MAKER_EXPIRED}",
       oo.BENIGN_CAUSES == frozenset({oo.MAKER_EXPIRED}),
       f"was {set(oo.BENIGN_CAUSES)}")
    for cause in (oo.NO_ORDER_CREATED, oo.NO_FILL, oo.REJECTED, None,
                  "something_invented_later"):
        ok(f"{cause!r} is a fault", oo.is_execution_fault(cause) is True)


def test_the_classifier_never_collapses_the_three_states():
    """Three inputs, three distinct outputs. If any two ever map to the
    same cause, a reader can no longer tell them apart downstream."""
    causes = [oo.cause_for_maker_only_no_fill(v) for v in (True, False, None)]
    ok("True/False/None give three different causes",
       len(set(causes)) == 3, f"got {causes}")
    ok("only the True one is benign",
       [c in oo.BENIGN_CAUSES for c in causes] == [True, False, False],
       f"got {causes}")


def test_zz_nothing_above_failed():
    """Runs last, by name. ok() records rather than raises so one failure
    does not hide the rest of the report; this is what makes the recorded
    failures fail the run - under pytest as well as under __main__."""
    assert not _failures, f"{len(_failures)} checks failed: {_failures}"


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    for t in TESTS:
        print(f"\n== {t.__name__}")
        t()
    print()
    if _failures:
        print(f"{len(_failures)} FAILED, {_passes} passed:")
        for f in _failures:
            print(f"  - {f}")
        sys.exit(1)
    print(f"all {_passes} maker-only cause-split checks passed")

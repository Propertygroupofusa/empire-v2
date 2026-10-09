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
import slice_execution as sx

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

# A 0.01-increment product with a $1 minimum order value - LINK-USD's real
# rules, fetched from Coinbase. Passed as the rules dict the sell path now
# reads instead of a decimal count.
RULES = {"base_increment": "0.01", "base_min_size": None,
         "quote_min_size": "1", "quote_increment": "0.001"}

# NOTE ON WHAT THESE PATCH. The sell path reads get_asset_balance_DETAIL (for
# the hold) and get_product_RULES (for the increment and the value floor).
# These cases used to patch get_asset_balance and get_product_size_decimals,
# which the path no longer calls - so NO_SELLABLE_INVENTORY and NO_ASK were
# both falling through the unreadable-balance branch and passing VACUOUSLY.
# The assertions still held; they had simply stopped testing their own names.
# Each case now also pins the DECISION it must reach, which is what makes a
# future rewiring fail here instead of going quietly green.
# (label, requested qty, expected decision, engine patches)
#
# THE REQUESTED QTY IS PART OF THE CASE. My first version asked to sell
# 1134.3 out of a 0.0046 wallet and expected DUST; the classifier said
# UNBACKED and was right - a book claiming 1134.3 against 0.0046 free with
# nothing on hold is a divergence, not a rounding outcome. Dust is when the
# claim and the wallet AGREE and both are below one tradeable unit.
SELL_CASES = (
    # BALANCE_READ_FAILED - the wallet could not be read, so no order is
    # sized off a number nothing confirmed.
    ("BALANCE_READ_FAILED", 1134.3, None,
     dict(get_asset_balance_detail=aval((None, None, "HTTP 500 from /accounts")),
          get_product_rules=aval((RULES, None)),
          get_best_bid_ask=aval((100.0, 100.1)))),
    # NO_SELLABLE_INVENTORY - the branch holds 0.0046 and that is all it
    # claims. Floors to zero on a 0.01 grid, so there is nothing to sell.
    ("NO_SELLABLE_INVENTORY", 0.0046, sx.DUST,
     dict(get_asset_balance_detail=aval((0.0046, 0.0, None)),
          get_product_rules=aval((RULES, None)),
          get_best_bid_ask=aval((100.0, 100.1)))),
    # NO_ASK - the book would not read, so there is no ask to rest at.
    ("NO_ASK", 10.0, None,
     dict(get_asset_balance_detail=aval((10.0, 0.0, None)),
          get_product_rules=aval((RULES, None)),
          get_best_bid_ask=aval((None, None)))),
    # RESERVED - the coin exists and another order is sitting on it. ALGO's
    # real live shape: 1134.3 on hold, 0.046389 free, 279.4 claimed.
    ("RESERVED", 279.4, sx.RESERVED,
     dict(get_asset_balance_detail=aval((0.046389, 1134.3, None)),
          get_product_rules=aval((RULES, None)),
          get_best_bid_ask=aval((100.0, 100.1)))),
    # UNBACKED - the books claim coin the wallet does not hold and nothing is
    # holding it. QNT's real live shape, to the digit.
    ("UNBACKED", 0.675982153333, sx.UNBACKED,
     dict(get_asset_balance_detail=aval((0.00097323, 0.0, None)),
          get_product_rules=aval((RULES, None)),
          get_best_bid_ask=aval((100.0, 100.1)))),
    # BLOCKED - the venue's rules could not be read. Fails CLOSED; the old
    # path defaulted to 8 decimals, the most permissive value on Coinbase.
    ("NO_PRODUCT_RULES", 2000.0, sx.BLOCKED,
     dict(get_asset_balance_detail=aval((2000.0, 0.0, None)),
          get_product_rules=aval((None, "HTTP 503 reading product rules")),
          get_best_bid_ask=aval((100.0, 100.1)))),
    # A HOLD THAT COULD NOT BE READ. The shortfall is real but whether it is
    # reserved or missing is UNKNOWN, and guessing either way is wrong.
    ("HOLD_UNREADABLE", 279.4, sx.BLOCKED,
     dict(get_asset_balance_detail=aval((0.046389, None, None)),
          get_product_rules=aval((RULES, None)),
          get_best_bid_ask=aval((100.0, 100.1)))),
)


def test_none_of_the_sell_side_classes_becomes_a_maker_expiry():
    for label, ask_qty, want_decision, patches in SELL_CASES:
        print(f"\n  [{label}]")
        with patched(engine, **patches):
            engine._last_order_rested.pop(PID, None)
            engine._last_order_block.pop(PID, None)
            fill = asyncio.run(engine.place_maker_sell(
                FakeSession(), ask_qty, PID, wait_seconds=240))
        ok(f"{label}: place_maker_sell returns None", fill is None)
        rested = engine._last_order_rested.get(PID, "absent")
        ok(f"{label}: the real engine recorded order_rested False",
           rested is False, f"was {rested!r}")
        # The guard against this test going vacuous again: the case must have
        # travelled through the branch its own name describes.
        got = (engine._last_order_block.get(PID) or {}).get("decision")
        ok(f"{label}: reached the {want_decision or 'pre-classifier'} branch, "
           f"not another one", got == want_decision,
           f"decision was {got!r}, expected {want_decision!r}")
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


def test_a_legitimate_sell_still_reaches_the_venue():
    """THE REGRESSION THAT MATTERS MOST. Six new ways to refuse an order are
    worth nothing if one of them also catches the orders that should go. This
    asserts the EXECUTE path end to end: the order is built, it is post-only,
    and its size is the venue-legal floor rather than the raw request."""
    sent = {}

    async def _capture(session, order, wait_seconds):
        sent["order"] = order
        sent["wait"] = wait_seconds
        return (float(order["order_configuration"]["limit_limit_gtc"]["base_size"]),
                100.1)

    with patched(engine,
                 get_asset_balance_detail=aval((6.85, 6.63, None)),
                 get_product_rules=aval((RULES, None)),
                 get_best_bid_ask=aval((100.0, 100.1)),
                 _place_maker_order=_capture):
        engine._last_order_rested.pop(PID, None)
        engine._last_order_block.pop(PID, None)
        # Asking for 0.227 out of 0.22 free: clamps to the wallet, then floors
        # onto the 0.01 grid. Within the drift tolerance, so not UNBACKED.
        fill = asyncio.run(engine.place_maker_sell(
            FakeSession(), 0.2201, PID, wait_seconds=240))

    ok("an executable sell is not refused", fill is not None, repr(fill))
    ok("it actually reached the order builder", "order" in sent, str(sent))
    if "order" not in sent:
        return
    cfg = sent["order"]["order_configuration"]["limit_limit_gtc"]
    ok("it is still POST-ONLY - maker policy intact",
       cfg.get("post_only") is True, str(cfg))
    ok("the side is SELL", sent["order"]["side"] == "SELL")
    ok("the size is floored onto the venue's 0.01 grid, not the raw request",
       cfg["base_size"] == "0.22", cfg["base_size"])
    ok("no refusal was recorded for it",
       (engine._last_order_block.get(PID) or {}).get("decision") is None,
       str(engine._last_order_block.get(PID)))
    ok("and the caller's wait window is passed through", sent["wait"] == 240,
       str(sent["wait"]))


def test_the_value_floor_can_refuse_an_order_the_old_path_would_have_sent():
    """0.05 LINK at $14.986 is $0.74 against a $1 venue minimum. The size is
    legal on the increment, so the old path submitted it and Coinbase refused
    it. `quote_min_size` appeared nowhere in this repo."""
    sent = {}

    async def _capture(session, order, wait_seconds):
        sent["order"] = order
        return (1.0, 100.0)

    with patched(engine,
                 get_asset_balance_detail=aval((0.05, 0.0, None)),
                 get_product_rules=aval((RULES, None)),
                 get_best_bid_ask=aval((14.98, 14.986)),
                 _place_maker_order=_capture):
        engine._last_order_rested.pop(PID, None)
        engine._last_order_block.pop(PID, None)
        fill = asyncio.run(engine.place_maker_sell(
            FakeSession(), 0.05, PID, wait_seconds=240))

    ok("the $0.74 order is refused", fill is None, repr(fill))
    ok("and nothing was sent to the venue", "order" not in sent, str(sent))
    blk = engine._last_order_block.get(PID) or {}
    ok("it is DUST, not a rejection", blk.get("decision") == sx.DUST, str(blk))
    ok("named as the value floor",
       blk.get("reason") == sx.BELOW_QUOTE_MINIMUM, str(blk.get("reason")))
    ok("the legal-on-increment size is still reported, not zeroed",
       blk.get("executable_qty") == 0.05, str(blk.get("executable_qty")))
    ok("and the floor itself is recorded", blk.get("quote_minimum") == "1",
       str(blk.get("quote_minimum")))


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

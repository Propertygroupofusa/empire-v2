"""The in-line maker wait must be visible WHILE it runs, and must not leak.

Run as written: python3 test_maker_wait_is_visible.py

WHAT THIS PROTECTS. _place_maker_order awaits a fill in-line for up to
GRID_MAKER_ONLY_WAIT_SECONDS (3,600 on this account) and the grid loop is
sequential over branches, so that await stops all 21. On 2026-10-08 it ran 64
minutes on one XLM-USD buy; the order filled and paid, and for the whole hour
the heartbeat read `alive: false`, which reads as a dead fleet.

These tests pin three things and nothing else:

  1. While the wait is in progress, loop_wait.in_flight() names the product
     and side. The old code made the wait completely invisible.
  2. When the wait returns - on a fill, on no fill, OR ON AN EXCEPTION - the
     marker is cleared. A leaked marker is worse than no marker: it would
     make a dead loop read as busy forever.
  3. THE ORDER BEHAVIOUR IS UNCHANGED. Same budget passed through, the
     cancel still happens on no fill, and the late-fill re-check still runs.
     This is instrumentation; if it altered an order it would be a bug.

Nothing here touches a venue. _await_fill, cancel_order and the HTTP post are
all replaced with fakes.
"""

import asyncio
import sys

sys.path.insert(0, ".")
import crypto_btc_compound_bot as eng
import loop_wait as lw

FAILS = []


def ok(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)


def section(t):
    print("\n" + t)


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


ORDER = {"client_order_id": "x", "product_id": "XLM-USD", "side": "BUY",
         "order_configuration": {"limit_limit_gtc": {
             "base_size": "100", "limit_price": "0.185", "post_only": True}}}


class _Resp:
    status = 200

    async def json(self):
        return {"success": True, "success_response": {"order_id": "oid-1"}}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    def post(self, *a, **k):
        return _Resp()


class Harness:
    """Swaps the three venue-touching calls, recording what happened."""

    def __init__(self, fill=None, raise_in_wait=False, seen=None):
        self.fill = fill
        self.raise_in_wait = raise_in_wait
        self.seen = seen if seen is not None else []
        self.cancels = []
        self.waits = []

    def __enter__(self):
        self._aw, self._cx = eng._await_fill, eng.cancel_order
        self._ah = eng._auth_headers

        async def _await_fill(session, order_id, secs):
            self.waits.append(secs)
            # Observe the marker from INSIDE the wait - the only moment at
            # which the old code was blind.
            self.seen.append(lw.in_flight())
            if self.raise_in_wait:
                raise RuntimeError("connection reset mid-wait")
            return self.fill if secs > 2 else None

        async def _cancel(session, order_id):
            self.cancels.append(order_id)
            return True, {}

        eng._await_fill = _await_fill
        eng.cancel_order = _cancel
        eng._auth_headers = lambda *a, **k: {}
        return self

    def __exit__(self, *a):
        eng._await_fill, eng.cancel_order = self._aw, self._cx
        eng._auth_headers = self._ah
        return False


section("[1] the wait is visible while it is running")
lw._IN_FLIGHT.clear()
with Harness(fill=(100.0, 0.185)) as h:
    out = run(eng._place_maker_order(_Session(), ORDER, 3600))
seen = h.seen[0]
ok("in_flight() during the wait is not None - the old blind spot",
   seen is not None)
ok("and names the product being waited on", seen and seen["product_id"] == "XLM-USD")
ok("and the side, lowercased for the feed", seen and seen["side"] == "buy")
ok("and carries the real budget, not a guess", seen and seen["budget_seconds"] == 3600.0)
ok("the fill is returned unchanged", out == (100.0, 0.185))

section("[2] the marker is cleared on the FILL path")
ok("nothing left in flight after a fill", lw.in_flight() is None)

section("[3] the marker is cleared on the NO-FILL path, and the order "
        "behaviour is untouched")
lw._IN_FLIGHT.clear()
with Harness(fill=None) as h:
    out = run(eng._place_maker_order(_Session(), ORDER, 3600))
ok("no fill returns None", out is None)
ok("nothing left in flight after no fill", lw.in_flight() is None)
ok("the unfilled order was still cancelled", h.cancels == ["oid-1"])
ok("the budget was passed through unchanged, then the 2s late re-check",
   h.waits == [3600, 2])

section("[4] THE LEAK PATH - an exception inside the wait must still clear")
lw._IN_FLIGHT.clear()
with Harness(raise_in_wait=True):
    raised = False
    try:
        run(eng._place_maker_order(_Session(), ORDER, 3600))
    except RuntimeError:
        raised = True
ok("the exception still propagates - this must not swallow it", raised)
ok("and the marker is cleared anyway, by the finally",
   lw.in_flight() is None)
ok("so a stale marker cannot make a dead loop read as busy",
   lw.verdict(age_seconds=9999.0, holds_loop=True)["verdict"] == "STALLED")

section("[5] a rejected order never enters the wait at all")
lw._IN_FLIGHT.clear()


class _Reject(_Resp):
    status = 400

    async def json(self):
        return {"success": False, "error_response": {"message": "post_only would cross"}}


class _RejSession:
    def post(self, *a, **k):
        return _Reject()


with Harness(fill=(1.0, 1.0)) as h:
    out = run(eng._place_maker_order(_RejSession(), ORDER, 3600))
ok("a rejected order returns None", out is None)
ok("and never reached the wait", h.waits == [])
ok("and marked nothing", lw.in_flight() is None)

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)

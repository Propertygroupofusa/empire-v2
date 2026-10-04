"""The SELL that could not leave: qty held vs qty_available at Alpaca.

THE BUG THIS PINS

Live 2026-10-04, every cycle for most of a weekend:

    MNQ: Max hold time exceeded: 100034s >= 86400s
    Futures order REJECTED (HTTP 403): insufficient qty available for
    order (requested: 0.162956, available: 0) | SELL 0.162956 MNQ (QQQ)

The account HELD 0.162956 QQQ throughout. Alpaca refused anyway:
`qty` is what you own, `qty_available` is what is not already spoken
for by a resting order, and an exit retrying every cycle creates that
gap for itself. `qty_available` appeared nowhere in the repo, so the
bot could not see it coming; the rejection handler changed no state, so
the max-hold rule fired again next pass, forever.

The guard added for it must FAIL OPEN. A check that can trap a position
it cannot read is worse than the storm it replaces.

Run: python3 test_sellable_qty.py
"""
import asyncio
import os
import sys

import prop_bot as pb

checks = []


def ok(label, cond):
    checks.append((label, bool(cond)))


class _Resp:
    def __init__(self, status, payload):
        self.status, self._payload = status, payload

    async def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    """Serves one /v2/positions/<symbol> answer and records every POST."""

    def __init__(self, status=200, payload=None, raise_on_get=None):
        self.status, self.payload, self.raise_on_get = status, payload, raise_on_get
        self.posts = []
        self.gets = []

    def get(self, url, **kw):
        self.gets.append(url)
        if self.raise_on_get:
            raise self.raise_on_get
        return _Resp(self.status, self.payload)

    def post(self, url, **kw):
        # ORDERS ONLY. broadcast_signal_to_subscribers posts through this
        # same session, and counting its calls as orders made every
        # fail-open case read as "two orders sent" on the first run.
        if url.endswith("/v2/orders"):
            self.posts.append(kw.get("json"))
        return _Resp(200, {"id": "ok"})


def sell(session, qty=0.162956, contract="MNQ"):
    return asyncio.run(pb.execute_futures_trade(
        session, contract, "SELL", qty, 749.58, 50.0, "flat", source="test"))


def pos(qty, avail):
    return {"symbol": "QQQ", "qty": str(qty), "qty_available": str(avail),
            "avg_entry_price": "752.81", "current_price": "749.58"}


# --- the incident itself ----------------------------------------------------
pb._unsellable_logged.clear()
s = FakeSession(200, pos("0.162956", "0"))
r = sell(s)
ok("held-but-0-available sends NO order", s.posts == [])
ok("held-but-0-available reports failure", r is False)

# --- and it does not repeat -------------------------------------------------
pb._unsellable_logged.clear()
s = FakeSession(200, pos("0.162956", "0"))
import logging
seen = []
_h = logging.Handler()
_h.emit = lambda rec: seen.append(rec.getMessage())
pb.log.addHandler(_h)
for _ in range(5):
    sell(s)
pb.log.removeHandler(_h)
ok("five blocked cycles send five orders? no - zero", s.posts == [])
ok("five blocked cycles log the condition ONCE",
   sum(1 for m in seen if "0 AVAILABLE" in m) == 1)

# --- PARTIAL availability is refused, NOT sold down -------------------------
#
# The first version of this gate sold the available part. Every sell
# caller treats a True return as "the tracked position is closed" and
# books the FULL quantity's P&L, so a deliberate partial would have
# handed exit_pass, branch_exit and opening_bar_exit a full-size P&L for
# a part-size sale and left the remainder untracked. Whole or nothing.
pb._unsellable_logged.clear()
s = FakeSession(200, pos("0.162956", "0.10"))
r = sell(s)
ok("partial availability sends NO order", s.posts == [])
ok("partial availability reports failure", r is False)

# --- enough available goes through untouched --------------------------------
pb._unsellable_logged.clear()
s = FakeSession(200, pos("0.162956", "0.162956"))
sell(s)
ok("exactly enough available sends the order", len(s.posts) == 1)
ok("exactly enough available sends the FULL size",
   s.posts and float(s.posts[0]["qty"]) == 0.162956)

pb._unsellable_logged.clear()
s = FakeSession(200, pos("5.0", "5.0"))
sell(s, qty=1.0)
ok("more available than asked does NOT enlarge the order",
   s.posts and float(s.posts[0]["qty"]) == 1.0)
ok("more available than asked still sends", len(s.posts) == 1)

# --- FAIL OPEN: every ambiguous read must still send ------------------------
for label, kw in [
    ("a 404 (flat / short entry)", dict(status=404, payload={})),
    ("a 500", dict(status=500, payload={})),
    ("an exception on the read", dict(raise_on_get=RuntimeError("boom"))),
    ("an unparseable qty_available", dict(status=200,
        payload={"qty": "0.162956", "qty_available": "n/a"})),
    ("a missing qty_available field", dict(status=200,
        payload={"qty": "0.162956"})),
    ("a SHORT position", dict(status=200, payload=pos("-3.0", "0"))),
    ("a flat row", dict(status=200, payload=pos("0", "0"))),
]:
    pb._unsellable_logged.clear()
    s = FakeSession(**kw)
    sell(s)
    ok(f"fails OPEN on {label}", len(s.posts) == 1)

# --- BUYS are never consulted ----------------------------------------------
pb._unsellable_logged.clear()
s = FakeSession(200, pos("0.162956", "0"))
asyncio.run(pb.execute_futures_trade(
    s, "MNQ", "BUY", 0.162956, 749.58, 50.0, "up", source="test"))
ok("a BUY never reads the position endpoint",
   not any("/v2/positions/" in u for u in s.gets))

# --- the dedupe key clears when the shares come free ------------------------
pb._unsellable_logged.clear()
s = FakeSession(200, pos("0.162956", "0"))
sell(s)
ok("blocked symbol is remembered", "QQQ" in pb._unsellable_logged)
s2 = FakeSession(200, pos("0.162956", "0.162956"))
sell(s2)
ok("remembered symbol is FORGOTTEN once it can sell again",
   "QQQ" not in pb._unsellable_logged)

# --- a MOVING shortfall is not swallowed by the dedupe ----------------------
pb._unsellable_logged.clear()
seen2 = []
_h2 = logging.Handler()
_h2.emit = lambda rec: seen2.append(rec.getMessage())
pb.log.addHandler(_h2)
for avail in ("0", "0", "0.05", "0.05", "0"):
    sell(FakeSession(200, pos("0.162956", avail)))
pb.log.removeHandler(_h2)
ok("a shortfall that CHANGES logs again (3 distinct, not 5 and not 1)",
   sum(1 for m in seen2 if "NOT SENT" in m) == 3)

# --- the source rule it must not undo --------------------------------------
SRC = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "prop_bot.py")).read()
CODE = "\n".join(l for l in SRC.splitlines() if not l.lstrip().startswith("#"))
ok("qty_available is actually read", "qty_available" in CODE)
ok("the gate is scoped to sells", 'if side == "sell":' in CODE)
ok("the gate never rewrites the quantity it was given",
   "qty = _avail" not in CODE)
ok("time in force is still DAY-only", 'time_in_force = "day"' in CODE)
ok("the gate runs BEFORE the quantity is formatted",
   CODE.index('_sellable_qty(session, symbol)')
   < CODE.index("qty_str, is_fractional = format_order_qty(qty)"))

failed = [l for l, c in checks if not c]
for l, c in checks:
    print(f"  {'PASS' if c else 'FAIL'}  {l}")
print(f"\n{len(checks) - len(failed)}/{len(checks)} passed")
sys.exit(1 if failed else 0)

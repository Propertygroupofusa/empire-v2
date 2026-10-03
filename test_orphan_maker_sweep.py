"""A redeploy must not leave a maker order resting on Coinbase with no owner.

The maker path places a GTC post-only order, waits up to 45s, then cancels.
SIGTERM inside that window used to leave the order resting forever, able to
fill hours later with no slice recording it - and with a bare uuid id it
could not be told apart from an order placed by hand.
"""
import asyncio
from unittest import mock

import crypto_btc_compound_bot as engine
import crypto_grid_bot as grid


def test_both_maker_builders_tag_their_orders():
    src = open("crypto_btc_compound_bot.py").read()
    assert src.count('"client_order_id": GRID_MAKER_COID_PREFIX + str(uuid.uuid4())') == 2


def test_only_own_open_orders_are_selected():
    orders = [
        {"order_id": "a", "client_order_id": "gmk-1", "status": "OPEN", "product_id": "XRP-USD",
         "side": "BUY", "filled_size": "0"},
        {"order_id": "b", "client_order_id": "rstop-1", "status": "OPEN", "product_id": "ETH-USD"},
        {"order_id": "c", "client_order_id": "3f2a-uuid", "status": "OPEN", "product_id": "BTC-USD"},
        {"order_id": "d", "client_order_id": "gmk-2", "status": "FILLED", "product_id": "ZEC-USD"},
        {"order_id": "e", "client_order_id": "gmk-3", "status": "OPEN", "product_id": "ZEC-USD",
         "side": "SELL", "filled_size": "0.4"},
        "garbage",
    ]
    got = engine.select_orphan_maker_orders(orders)
    assert [o["order_id"] for o in got] == ["a", "e"]
    assert got[1]["filled_size"] == 0.4


class _Resp:
    def __init__(self, status, body):
        self.status, self._b = status, body
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def json(self): return self._b


class _Session:
    def __init__(self, status, body): self.status, self.body = status, body
    def get(self, *a, **k): return _Resp(self.status, self.body)


def test_sweep_cancels_orphans_and_reports_partial_fill():
    body = {"orders": [
        {"order_id": "a", "client_order_id": "gmk-1", "status": "OPEN", "product_id": "XRP-USD",
         "side": "BUY", "filled_size": "0"},
        {"order_id": "e", "client_order_id": "gmk-3", "status": "OPEN", "product_id": "ZEC-USD",
         "side": "SELL", "filled_size": "0.4"},
        {"order_id": "m", "client_order_id": "manual", "status": "OPEN", "product_id": "BTC-USD"},
    ]}
    cancelled = []

    async def cancel(_s, oid):
        cancelled.append(oid)
        return True

    with mock.patch.object(engine, "cancel_order", cancel), \
         mock.patch.object(engine, "_auth_headers", lambda *a: {}):
        res = asyncio.run(engine.sweep_orphan_maker_orders(_Session(200, body)))
    assert cancelled == ["a", "e"]            # the manual order is never touched
    assert res["found"] == 2 and not res["failed"]
    assert [o["order_id"] for o in res["partially_filled"]] == ["e"]


def test_unreadable_open_orders_is_none_not_zero():
    with mock.patch.object(engine, "_auth_headers", lambda *a: {}):
        assert asyncio.run(engine.sweep_orphan_maker_orders(_Session(503, {}))) is None


def _run_once(res):
    logged = []

    async def sweep(_s): return res

    async def act(*a): logged.append(a)

    with mock.patch.object(engine, "sweep_orphan_maker_orders", sweep), \
         mock.patch.object(grid, "_log_activity_safe", act):
        return asyncio.run(grid._sweep_orphans_once()), logged


def test_grid_sweep_retries_when_it_could_not_look():
    done, logged = _run_once(None)
    assert done is False and logged == []


def test_grid_sweep_retries_when_a_cancel_failed():
    o = {"order_id": "a", "product_id": "XRP-USD", "side": "BUY", "filled_size": 0.0}
    done, logged = _run_once({"found": 1, "cancelled": [], "failed": [o], "partially_filled": []})
    assert done is False
    assert "FAILED" in logged[0][3]


def test_grid_sweep_logs_partial_fill_and_completes():
    o = {"order_id": "e", "product_id": "ZEC-USD", "side": "SELL", "filled_size": 0.4}
    done, logged = _run_once({"found": 1, "cancelled": [o], "failed": [], "partially_filled": [o]})
    assert done is True
    assert logged[0][2] == "ORPHAN_ORDER" and "0.4" in logged[0][3]


def test_sweep_runs_after_lease_before_anything_else_trades():
    src = open("crypto_grid_bot.py").read()
    lease = src.index("allowed, why = await acquire_grid_lease()")
    sweep = src.index("_orphan_sweep_done = await _sweep_orphans_once()")
    switch = src.index("if not await is_grid_bot_active():", lease)
    assert lease < sweep < switch

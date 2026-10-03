"""The Coinbase net-worth read must not blank because one of ~23 per-coin
candle downloads failed. Prices now come from ONE batched best_bid_ask
call, with the old per-coin fetch only as a fallback for a coin it missed."""
import asyncio
from unittest import mock

import crypto_btc_compound_bot as engine
import crypto_grid_bot as grid


class _R:
    def __init__(self, status, body): self.status, self._b = status, body
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def json(self): return self._b


class _S:
    def __init__(self, status, body): self.status, self.body, self.calls = status, body, []
    def get(self, url, **k):
        self.calls.append(k.get("params"))
        return _R(self.status, self.body)
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


BOOKS = {"pricebooks": [
    {"product_id": "XRP-USD", "bids": [{"price": "1.49"}], "asks": [{"price": "1.51"}]},
    {"product_id": "ZEC-USD", "bids": [], "asks": [{"price": "1400"}]},   # one-sided: not priced
]}


def test_one_request_prices_every_product():
    s = _S(200, BOOKS)
    with mock.patch.object(engine, "_auth_headers", lambda *a: {}):
        out = asyncio.run(engine.get_mid_prices(s, ["XRP-USD", "ZEC-USD", "ETH-USD"]))
    assert len(s.calls) == 1
    assert out["XRP-USD"] == 1.50
    assert out["ZEC-USD"] is None and out["ETH-USD"] is None   # never guessed


def test_failed_batch_returns_none_not_zero():
    with mock.patch.object(engine, "_auth_headers", lambda *a: {}):
        out = asyncio.run(engine.get_mid_prices(_S(429, {}), ["XRP-USD"]))
    assert out == {"XRP-USD": None}


def test_grid_value_falls_back_per_coin_only_for_misses():
    class B:  # minimal branch
        def __init__(self, n, p): self.bot_name, self.product_id = n, p

    class Sl:
        def __init__(self, q): self.qty = q

    branches = [B("g1", "XRP-USD"), B("g2", "ZEC-USD")]
    slices = {"g1": [Sl(10)], "g2": [Sl(2)]}
    fallback_calls = []

    async def branches_fn(): return branches
    async def slices_fn(n): return slices[n]
    async def mids(_s, pids): return {"XRP-USD": 1.5, "ZEC-USD": None}
    async def per_coin(_s, pid):
        fallback_calls.append(pid)
        return 1400.0, 0.01

    with mock.patch.object(grid, "get_grid_branches", branches_fn), \
         mock.patch.object(grid, "get_grid_slices", slices_fn), \
         mock.patch.object(engine, "get_mid_prices", mids), \
         mock.patch.object(engine, "get_price_and_volatility", per_coin), \
         mock.patch.object(engine.aiohttp, "ClientSession", lambda: _S(200, {})):
        total, complete = asyncio.run(grid.get_grid_holdings_market_value())
    assert fallback_calls == ["ZEC-USD"]
    assert complete is True and total == round(10 * 1.5 + 2 * 1400.0, 2)

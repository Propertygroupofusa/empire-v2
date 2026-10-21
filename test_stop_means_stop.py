"""STOP_TRADING stops every job that can buy or sell, on both accounts.

The owner, 2026-10-14: "stop everything. Don't sell anything. Don't buy
anything anymore. Just leave everything like it is."

STOP_TRADING already stopped the grid, prop_bot, the family tree, the
Coinbase bot, auto-deploy and the wheel. Five jobs that can place orders never
read it: the Alpaca swing bot (buys AND sells), Alpaca auto-close (sells),
concentration rotation (sells when armed), auto-trim (sells when armed) and
resting stops (places orders when armed). With the switch on they would have
kept trading. Each now returns before it reads or sends anything.
"""
import asyncio

import pytest

# Imported here, before routers.trading_dashboard: importing that module can put
# another checkout on sys.path in a development sandbox, and a later plain import
# would then test that copy instead of this one.
import alpaca_swing_bot
import auto_trim_worker
import concentration_rotation_worker
import crypto_grid_bot
import resting_stops_worker


def run(c):
    return asyncio.run(c)


@pytest.fixture
def stopped(monkeypatch):
    monkeypatch.setenv("STOP_TRADING", "true")
    # Alpaca's own switch unset: Alpaca follows STOP_TRADING, as it did
    # before it had one (alpaca_stop; test_alpaca_own_switch).
    monkeypatch.delenv("ALPACA_STOP_TRADING", raising=False)


class _NoSession:
    """Any attempt to talk to a venue fails the test."""
    def post(self, *a, **k):
        raise AssertionError("an order was sent while STOP_TRADING was set")

    def get(self, *a, **k):
        raise AssertionError("the venue was read while STOP_TRADING was set")

    def delete(self, *a, **k):
        raise AssertionError("a position was closed while STOP_TRADING was set")


def test_the_swing_bot_neither_buys_nor_sells(stopped):
    import alpaca_swing_bot as sw
    assert run(sw.place_order(_NoSession(), "NVDA", 1, "buy")) is None
    assert run(sw.place_order(_NoSession(), "NVDA", 1, "sell")) is None


def test_alpaca_auto_close_closes_nothing(stopped, monkeypatch):
    import routers.trading_dashboard as td

    def boom(*a, **k):
        raise AssertionError("auto-close opened a session while STOP_TRADING was set")
    monkeypatch.setattr(td.aiohttp, "ClientSession", boom)
    assert run(td.check_and_auto_close_positions()) is None


def test_concentration_rotation_sells_nothing_even_when_armed(stopped, monkeypatch):
    import concentration_rotation_worker as w
    import crypto_grid_bot as g
    monkeypatch.setenv(w.MODE_ENV, "arm")

    async def boom():
        raise AssertionError("the fleet was read while STOP_TRADING was set")
    monkeypatch.setattr(g, "get_grid_status", boom)
    r = run(w.check_once())
    assert r["stopped"] is True and r["settled"] == 0


def test_auto_trim_places_nothing(stopped):
    import auto_trim_worker as w
    r = run(w.check_once(None))
    assert r["stopped"] is True and r["placed"] == 0


def test_resting_stops_contact_nothing_even_when_armed(stopped, monkeypatch):
    import resting_stops_worker as w
    monkeypatch.setattr(w, "is_armed", lambda: True)
    r = run(w.check_once(None))
    assert r["stopped"] is True and r["placed"] == 0


def test_switched_off_the_swing_bot_is_unchanged(monkeypatch):
    """The gate only acts on an explicit true - unset leaves trading as before."""
    import alpaca_stop
    monkeypatch.delenv("STOP_TRADING", raising=False)
    monkeypatch.delenv("ALPACA_STOP_TRADING", raising=False)
    assert alpaca_stop.alpaca_stopped() is False
    src = open("alpaca_swing_bot.py", encoding="utf-8").read()
    fn = src[src.index("async def place_order("):src.index("async def run_intraday_check(")]
    assert "alpaca_stop.alpaca_stopped()" in fn
    assert fn.index("alpaca_stop.alpaca_stopped()") < fn.index("session.post(")


@pytest.mark.parametrize("path", [
    "crypto_grid_bot.py", "crypto_family_tree_bot.py", "crypto_coinbase_bot.py",
    "auto_deploy.py", "idle_rotation_worker.py",
    "concentration_rotation_worker.py", "auto_trim_worker.py",
    "resting_stops_worker.py", "routers/trading_dashboard.py",
])
def test_every_job_that_can_trade_reads_the_switch(path):
    assert 'STOP_TRADING' in open(path, encoding="utf-8").read(), path


@pytest.mark.parametrize("path", [
    "prop_bot.py", "alpaca_wheel_bot.py", "market_brain_runner.py",
    "alpaca_swing_bot.py", "routers/trading_dashboard.py",
])
def test_every_alpaca_job_reads_alpacas_switch(path):
    """Alpaca's jobs read Alpaca's own switch, which follows STOP_TRADING
    until ALPACA_STOP_TRADING is set (alpaca_stop)."""
    assert 'alpaca_stop.alpaca_stopped()' in open(path, encoding="utf-8").read(), path

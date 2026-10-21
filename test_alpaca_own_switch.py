"""Alpaca has its own off switch, and Coinbase never reads it.

THE OWNER: "Alpaca and Coinbase are two different things... never to combine
those." Measured 2026-10-21: STOP_TRADING was the only switch that could stop
Alpaca, and the Coinbase grid, rotation and boot-time fixes read it too - so
Alpaca could not be started or stopped without doing the same to Coinbase.

What is pinned here:
  * unset, ALPACA_STOP_TRADING follows STOP_TRADING - shipping changes nothing
  * set, it decides Alpaca alone, in both directions
  * it fails closed: anything but a plain off word is a stop
  * no Coinbase job reads it, and no Alpaca job reads STOP_TRADING directly

Run as written: python3 test_alpaca_own_switch.py
"""
import asyncio
import sys

import pytest

# Before routers.trading_dashboard, for the reason test_stop_means_stop gives.
import alpaca_stop
import alpaca_swing_bot
import auto_trim_worker


def run(c):
    return asyncio.run(c)


@pytest.fixture
def env(monkeypatch):
    def set_(shared=None, alpaca=None):
        for name, value in (("STOP_TRADING", shared), ("ALPACA_STOP_TRADING", alpaca)):
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
    return set_


class _NoSession:
    def post(self, *a, **k):
        raise AssertionError("an Alpaca order was sent while Alpaca was stopped")

    def get(self, *a, **k):
        raise AssertionError("Alpaca was read while Alpaca was stopped")

    def delete(self, *a, **k):
        raise AssertionError("an Alpaca position was closed while Alpaca was stopped")


# ------------------------------------------------- shipping changes nothing
@pytest.mark.parametrize("shared", ["true", "false", None])
def test_unset_it_follows_the_shared_switch_exactly(env, shared):
    env(shared=shared)
    stopped, source, _ = alpaca_stop.alpaca_stop_state()
    assert stopped is (shared == "true") and source == "STOP_TRADING"


def test_unset_it_reads_the_live_value_today_as_stopped(env):
    """2026-10-21: STOP_TRADING true. After this ships, Alpaca is still stopped."""
    env(shared="true")
    assert alpaca_stop.alpaca_stopped() is True
    assert run(alpaca_swing_bot.place_order(_NoSession(), "SLV", 1, "sell")) is None


def test_a_blank_value_counts_as_unset(env):
    env(shared="true", alpaca="  ")
    assert alpaca_stop.alpaca_stop_state()[:2] == (True, "STOP_TRADING")


# --------------------------------------------- set, it decides Alpaca alone
@pytest.mark.parametrize("off", ["false", "FALSE", " false ", '"false"', "'off'", "0", "no", "off"])
def test_an_off_word_lets_alpaca_run_while_coinbase_stays_stopped(env, off):
    env(shared="true", alpaca=off)
    stopped, source, _ = alpaca_stop.alpaca_stop_state()
    assert stopped is False and source == "ALPACA_STOP_TRADING"


@pytest.mark.parametrize("on", ["true", "TRUE", "1", "yes", "on"])
def test_an_on_word_stops_alpaca_while_coinbase_runs(env, on):
    env(shared="false", alpaca=on)
    stopped, source, _ = alpaca_stop.alpaca_stop_state()
    assert stopped is True and source == "ALPACA_STOP_TRADING"


@pytest.mark.parametrize("typo", ["flase", "fasle", "stop", "maybe", "true false"])
def test_a_value_it_cannot_read_is_a_stop(env, typo):
    env(shared="false", alpaca=typo)
    assert alpaca_stop.alpaca_stopped() is True


def test_alpacas_switch_stops_the_swing_bot_with_coinbase_running(env):
    env(shared="false", alpaca="true")
    assert run(alpaca_swing_bot.place_order(_NoSession(), "NVDA", 1, "buy")) is None
    assert run(alpaca_swing_bot.place_order(_NoSession(), "SLV", 1, "sell")) is None
    assert "ALPACA_STOP_TRADING" in alpaca_stop.alpaca_stop_reason()


def test_alpacas_switch_stops_auto_close_with_coinbase_running(env, monkeypatch):
    import routers.trading_dashboard as td
    env(shared="false", alpaca="true")
    monkeypatch.setattr(td, "ALPACA_KEY", "k")
    monkeypatch.setattr(td, "ALPACA_SECRET", "s")

    def boom(*a, **k):
        raise AssertionError("auto-close opened a session while Alpaca was stopped")
    monkeypatch.setattr(td.aiohttp, "ClientSession", boom)
    assert run(td.check_and_auto_close_positions()) is None


def test_auto_close_runs_on_alpacas_switch_while_coinbase_is_stopped(env, monkeypatch):
    """The other direction: STOP_TRADING on, Alpaca's switch off - the Alpaca
    loop goes past the stop and opens its session."""
    import routers.trading_dashboard as td
    env(shared="true", alpaca="false")
    monkeypatch.setattr(td, "ALPACA_KEY", "k")
    monkeypatch.setattr(td, "ALPACA_SECRET", "s")

    class _Reached(Exception):
        pass

    def reached(*a, **k):
        raise _Reached()
    monkeypatch.setattr(td.aiohttp, "ClientSession", reached)
    with pytest.raises(_Reached):
        run(td.check_and_auto_close_positions())


def test_letting_alpaca_run_does_not_wake_coinbase(env):
    env(shared="true", alpaca="false")
    r = run(auto_trim_worker.check_once(None))
    assert r["stopped"] is True and r["placed"] == 0


# ------------------------------------------------------- never combined
COINBASE_JOBS = [
    "crypto_grid_bot.py", "crypto_family_tree_bot.py", "crypto_coinbase_bot.py",
    "rotation_task.py", "coin_picker.py", "btc_dip_buy.py", "bot_runner.py",
    "trading_silence.py", "idle_rotation_worker.py", "concentration_rotation_worker.py",
    "auto_trim_worker.py", "resting_stops_worker.py", "profit_harvest.py",
    "startup_fix.py", "auto_deploy.py",
]
ALPACA_JOBS = ["prop_bot.py", "alpaca_swing_bot.py", "alpaca_wheel_bot.py",
               "market_brain_runner.py"]


@pytest.mark.parametrize("path", COINBASE_JOBS)
def test_no_coinbase_job_reads_alpacas_switch(path):
    src = open(path, encoding="utf-8").read()
    assert "alpaca_stop" not in src and "ALPACA_STOP_TRADING" not in src, path


@pytest.mark.parametrize("path", ALPACA_JOBS)
def test_no_alpaca_job_reads_the_shared_switch_directly(path):
    src = open(path, encoding="utf-8").read()
    assert 'getenv("STOP_TRADING"' not in src, path
    assert "alpaca_stop.alpaca_stopped()" in src, path


@pytest.mark.parametrize("fn_name", [
    "async def manual_open_prop_position(",
    "async def check_and_auto_close_positions(",
])
def test_the_dashboards_alpaca_paths_read_alpacas_switch(fn_name):
    src = open("routers/trading_dashboard.py", encoding="utf-8").read()
    fn = src[src.index(fn_name):]
    fn = fn[:fn.index("\nasync def ", 10)]
    assert "alpaca_stop.alpaca_stopped()" in fn and 'getenv("STOP_TRADING"' not in fn


def test_the_entry_check_reads_alpacas_switch():
    src = open("routers/trading_dashboard.py", encoding="utf-8").read()
    i = src.index('@router.get("/alpaca-overview/entry-eligibility")')
    fn = src[i:src.index("\n@router.", i + 10)]
    assert "alpaca_stop.alpaca_stopped()" in fn and 'getenv("STOP_TRADING"' not in fn


# --------------------------------------------------------- the dashboard
def test_the_dashboard_says_which_switch_decided(env):
    import routers.trading_dashboard as td
    env(shared="true")
    v = td._alpaca_stop_view()
    assert v["stopped"] is True and v["decided_by"] == "STOP_TRADING"
    assert v["separate_from_coinbase"] is False
    env(shared="true", alpaca="true")
    v = td._alpaca_stop_view()
    assert v["decided_by"] == "ALPACA_STOP_TRADING" and v["separate_from_coinbase"] is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))

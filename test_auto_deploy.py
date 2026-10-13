"""The hourly auto-deploy: the Deploy cash button on a timer, and nothing more.

Driven through the REAL idle_cash.deploy_inputs and idle_cash.plan, with a
fake grid module standing in for the database and the venue, so every rule
the button applies (floor, 20% share, parked/unbacked/net-negative refusals)
is the one actually exercised here.
"""
import asyncio
import os
import types

import pytest

import auto_deploy
import idle_cash


def _branch(n, pid, alloc=1000.0, open_=2, levels=10, **kw):
    b = {"bot_name": f"crypto_grid_{n}", "product_id": pid, "allocated_usd": alloc,
         "open_slices": open_, "num_levels": levels, "active": True, "locked": False}
    b.update(kw)
    return b


BRANCHES = [
    _branch(1, "XLM-USD"), _branch(2, "HBAR-USD"), _branch(3, "BCH-USD"),
    _branch(4, "LINK-USD"), _branch(5, "ALGO-USD"),
    _branch(6, "XRP-USD", open_=10, levels=10),      # parked
    _branch(7, "ETH-USD"),                           # unbacked
]
TRADES = [{"product_id": p, "pnl": 0.8, "exit_reason": "profit_target"}
          for p in ("XLM-USD", "HBAR-USD", "BCH-USD", "LINK-USD", "ALGO-USD",
                    "XRP-USD", "ETH-USD")]


def _status(unclaimed, free):
    return {"branches": BRANCHES,
            "allocation_backing": {"unbacked_usd": -unclaimed},
            "real_free_cash_usd": free,
            "backing_owned": {"unbacked": [{"product_id": "ETH-USD"}]}}


def fake_grid(unclaimed=2882.52, free=2882.52, lease=True, fail_on=None, read_fails=False):
    calls, feed = [], []

    async def read_grid_lease_state():
        return {"held_by_this_process": lease}

    async def get_grid_status():
        if read_fails:
            raise RuntimeError("venue unreadable")
        return _status(unclaimed, free)

    async def get_grid_trade_history():
        return {"recent_trades": TRADES}

    async def add_cash_to_grid_branch(bot_name, amount, caller=None):
        if fail_on and bot_name == fail_on:
            raise ValueError(f"no grid branch named {bot_name}")
        calls.append((bot_name, amount, caller))

    async def _record_gate_decision(bot, pid, verdict, reason):
        feed.append((pid, verdict))

    g = types.SimpleNamespace(
        GRID_CASH_RESERVE_USD=1820.0, read_grid_lease_state=read_grid_lease_state,
        get_grid_status=get_grid_status, get_grid_trade_history=get_grid_trade_history,
        add_cash_to_grid_branch=add_cash_to_grid_branch,
        _record_gate_decision=_record_gate_decision)
    return g, calls, feed


@pytest.fixture(autouse=True)
def _env():
    old = os.environ.get("GRID_AUTO_DEPLOY")
    os.environ.pop("GRID_AUTO_DEPLOY", None)
    yield
    if old is None:
        os.environ.pop("GRID_AUTO_DEPLOY", None)
    else:
        os.environ["GRID_AUTO_DEPLOY"] = old


def run(g):
    return asyncio.run(auto_deploy.run_once(g))


def test_off_unless_switched_on():
    g, calls, _ = fake_grid()
    r = run(g)
    assert r["ran"] is False and r["reason"] == "OFF"
    assert calls == []


@pytest.mark.parametrize("v", ["on", "ON", "true", "arm", '"on"', "1"])
def test_explicit_on_values_enable(v):
    os.environ["GRID_AUTO_DEPLOY"] = v
    assert auto_deploy.enabled() is True


@pytest.mark.parametrize("v", ["", "0", "off", "false", "flase", "maybe"])
def test_anything_else_is_off(v):
    os.environ["GRID_AUTO_DEPLOY"] = v
    assert auto_deploy.enabled() is False


def test_deploys_todays_idle_cash_like_the_button():
    os.environ["GRID_AUTO_DEPLOY"] = "on"
    g, calls, feed = fake_grid()
    r = run(g)
    assert r["ran"] is True and r["reason"] == "DEPLOYED"
    total = round(sum(a for _, a, _ in calls), 2)
    assert total == r["added_usd"]
    # 2,882.52 free - 1,820 floor = 1,062.52, split equally and rounded to
    # the cent by the planner - never more than is idle above the floor.
    assert 1062.40 <= total <= 1062.52
    funded = {b for b, _, _ in calls}
    assert "crypto_grid_6" not in funded                 # parked XRP refused
    assert "crypto_grid_7" not in funded                 # unbacked ETH refused
    assert all(c == "auto_deploy" for _, _, c in calls)  # attributed in the log
    assert len(feed) == len(calls) and all(v == "AUTO_DEPLOY" for _, v in feed)


def test_the_plan_is_exactly_the_buttons_plan():
    os.environ["GRID_AUTO_DEPLOY"] = "on"
    g, calls, _ = fake_grid()
    run(g)
    pot, reserve, unbacked = idle_cash.deploy_inputs(_status(2882.52, 2882.52), 1820.0)
    p = idle_cash.plan(BRANCHES, TRADES, unclaimed_usd=pot, reserve_usd=reserve,
                       unbacked=unbacked)
    assert sorted((a["bot_name"], a["usd"]) for a in p["adds"]) == \
        sorted((b, a) for b, a, _ in calls)


def test_below_the_owners_1000_it_waits():
    os.environ["GRID_AUTO_DEPLOY"] = "on"
    g, calls, _ = fake_grid(unclaimed=2700.0, free=2700.0)   # $880 above the floor
    r = run(g)
    assert r["reason"] == "BELOW_MINIMUM" and r["deployable_usd"] == 880.0
    assert calls == []


def test_the_floor_is_never_spent():
    os.environ["GRID_AUTO_DEPLOY"] = "on"
    g, calls, _ = fake_grid(unclaimed=5000.0, free=3000.0)   # free cash is the binding figure
    r = run(g)
    assert r["ran"] is True
    assert sum(a for _, a, _ in calls) <= 3000.0 - 1820.0 + 0.01


def test_one_check_is_capped():
    os.environ["GRID_AUTO_DEPLOY"] = "on"
    g, calls, _ = fake_grid(unclaimed=20000.0, free=20000.0)
    r = run(g)
    assert r["ran"] is True
    assert sum(a for _, a, _ in calls) <= auto_deploy.MAX_PER_PASS_USD + 0.01


def test_only_the_lease_holder_acts():
    os.environ["GRID_AUTO_DEPLOY"] = "on"
    g, calls, _ = fake_grid(lease=False)
    r = run(g)
    assert r["reason"] == "NOT_LEASE_HOLDER" and calls == []


def test_an_unreadable_status_deploys_nothing():
    os.environ["GRID_AUTO_DEPLOY"] = "on"
    g, calls, _ = fake_grid(read_fails=True)
    r = run(g)
    assert r["reason"] == "READ_FAILED" and calls == []


def test_unreadable_free_cash_deploys_nothing():
    os.environ["GRID_AUTO_DEPLOY"] = "on"
    g, calls, _ = fake_grid(free=None)
    r = run(g)
    assert r["ran"] is False and calls == []


def test_a_refused_branch_is_reported_and_the_rest_still_go():
    os.environ["GRID_AUTO_DEPLOY"] = "on"
    g, calls, feed = fake_grid(fail_on="crypto_grid_3")
    r = run(g)
    assert r["ran"] is True
    assert [f["product_id"] for f in r["failed"]] == ["BCH-USD"]
    assert "crypto_grid_3" not in {b for b, _, _ in calls} and len(calls) == 4
    assert ("BCH-USD", "AUTO_DEPLOY_REFUSED") in feed


def test_no_double_deploy_once_the_cash_is_claimed():
    os.environ["GRID_AUTO_DEPLOY"] = "on"
    g, calls, _ = fake_grid()
    run(g)
    g2, calls2, _ = fake_grid(unclaimed=1820.0, free=2882.52)  # claims now cover it
    r = run(g2)
    assert r["reason"] == "BELOW_MINIMUM" and calls2 == []


def test_status_reports_the_switch_and_the_last_check():
    os.environ["GRID_AUTO_DEPLOY"] = "on"
    g, _, _ = fake_grid()
    run(g)
    s = auto_deploy.status()
    assert s["enabled"] is True and s["env_name"] == "GRID_AUTO_DEPLOY"
    assert s["min_deploy_usd"] == 1000.0 and s["places_no_order"] and s["sells_no_coin"]
    assert s["last"]["reason"] == "DEPLOYED" and s["last"]["checked_at"].endswith("Z")

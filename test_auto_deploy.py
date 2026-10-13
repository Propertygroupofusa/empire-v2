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
    _branch(8, "APE-USD", alloc=8.27, open_=0, levels=1),   # lost money, but long ago
]
TRADES = [{"product_id": p, "pnl": 0.8, "exit_reason": "profit_target"}
          for p in ("XLM-USD", "HBAR-USD", "BCH-USD", "LINK-USD", "ALGO-USD",
                    "XRP-USD", "ETH-USD")]
APE_ALL = [{"product_id": "APE-USD", "pnl": -9.67, "exit_reason": "stop_loss"},
           {"product_id": "APE-USD", "pnl": -9.89, "exit_reason": "stop_loss"},
           {"product_id": "APE-USD", "pnl": 0.71, "exit_reason": "profit_target"}]
APE_RECENT = APE_ALL[-1:]


def _status(unclaimed, free):
    return {"branches": BRANCHES,
            "allocation_backing": {"unbacked_usd": -unclaimed},
            "real_free_cash_usd": free,
            "backing_owned": {"unbacked": [{"product_id": "ETH-USD"}]}}


def fake_grid(unclaimed=2882.52, free=2882.52, lease=True, fail_on=None, read_fails=False,
              book_size=None):
    calls, feed, hist = [], [], []

    async def read_grid_lease_state():
        return {"held_by_this_process": lease}

    async def get_grid_status():
        if read_fails:
            raise RuntimeError("venue unreadable")
        return _status(unclaimed, free)

    async def get_grid_trade_history(limit_recent=50):
        hist.append(limit_recent)
        # The real function returns only the latest `limit_recent` closes.
        # APE's two losses are OLD: a 50-row window hides them and shows
        # only its one recent small win.
        rows = TRADES + (APE_ALL if limit_recent >= 1000 else APE_RECENT)
        if book_size is None:
            return {"recent_trades": rows}
        # The real payload's own account of what the rows leave out.
        return {"recent_trades": rows, "total_trade_count": book_size,
                "recent_trades_returned": min(book_size, limit_recent),
                "recent_trades_truncated": book_size > limit_recent}

    async def add_cash_to_grid_branch(bot_name, amount, caller=None):
        if fail_on and bot_name == fail_on:
            raise ValueError(f"no grid branch named {bot_name}")
        calls.append((bot_name, amount, caller))

    async def _record_gate_decision(bot, pid, verdict, reason):
        feed.append((pid, verdict))

    g = types.SimpleNamespace(
        GRID_CASH_RESERVE_USD=1820.0, GRID_TRADE_HISTORY_MAX_ROWS=1000, read_grid_lease_state=read_grid_lease_state,
        get_grid_status=get_grid_status, get_grid_trade_history=get_grid_trade_history,
        add_cash_to_grid_branch=add_cash_to_grid_branch,
        _record_gate_decision=_record_gate_decision)
    g.hist = hist
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


def test_a_coin_that_lost_money_is_judged_on_its_whole_record():
    """2026-10-13: the first live check funded APE, TON and ONDO because the
    plan saw only the latest 50 closes, and their losses were older."""
    os.environ["GRID_AUTO_DEPLOY"] = "on"
    g, calls, _ = fake_grid()
    r = run(g)
    assert r["ran"] is True
    assert g.hist and min(g.hist) >= 1000, g.hist          # asked for the whole record
    assert "crypto_grid_8" not in {b for b, _, _ in calls}  # APE refused: -$18.85 net
    # and with only the latest closes it WOULD have been funded - the bug itself
    pot, reserve, unbacked = idle_cash.deploy_inputs(_status(2882.52, 2882.52), 1820.0)
    p = idle_cash.plan(BRANCHES, TRADES + APE_RECENT, unclaimed_usd=pot,
                       reserve_usd=reserve, unbacked=unbacked)
    assert "APE-USD" in {a["product_id"] for a in p["adds"]}


def test_a_book_bigger_than_one_read_deploys_nothing():
    """Past GRID_TRADE_HISTORY_MAX_ROWS the oldest losses fall out of view
    again, the same way they did at 50. Then it refuses, it does not guess."""
    os.environ["GRID_AUTO_DEPLOY"] = "on"
    g, calls, feed = fake_grid(book_size=1204)
    r = run(g)
    assert r["ran"] is False and r["reason"] == "PARTIAL_TRADE_RECORD", r
    assert "1204" in r["detail"] and "GRID_TRADE_HISTORY_MAX_ROWS" in r["detail"]
    assert calls == [] and feed == []


def test_todays_296_trade_book_is_read_whole():
    os.environ["GRID_AUTO_DEPLOY"] = "on"
    g, calls, _ = fake_grid(book_size=296)
    r = run(g)
    assert r["reason"] == "DEPLOYED" and calls, r


def test_partial_record_reads_the_payloads_own_flag():
    assert idle_cash.partial_record({"recent_trades": []}) is None
    assert idle_cash.partial_record(None) is None
    assert idle_cash.partial_record({"recent_trades_truncated": False,
                                     "total_trade_count": 296}) is None
    assert idle_cash.partial_record({"recent_trades_truncated": True,
                                     "recent_trades_returned": 1000,
                                     "total_trade_count": 1204})


def test_the_button_route_reads_the_whole_record_and_refuses_a_partial_one():
    """The Deploy cash button and the timer share one rule. Structural: the
    route needs a live database to run."""
    import ast
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "routers", "trading_dashboard.py"), encoding="utf-8").read()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "idle_capital_deploy_cash")
    body = ast.get_source_segment(src, fn)
    assert "GRID_TRADE_HISTORY_MAX_ROWS" in body
    assert "get_grid_trade_history()" not in body
    i_part, i_plan = body.index("idle_cash.partial_record(history)"), body.index("idle_cash.plan(")
    i_add = body.index("add_cash_to_grid_branch(")
    assert i_part < i_plan < i_add

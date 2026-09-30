"""Tests for the Alpaca growth diagnosis.

The failure that costs money here is RAISE_RISK_CAP on an edge that is not
there. So the refusals are tested first; the happy path last.
"""
import asyncio
import os
import tempfile
from datetime import datetime, timedelta
from unittest import mock

import alpaca_growth as ag

T0 = datetime(2026, 9, 1)

# The live snapshot, 2026-09-30: deployed at exactly the 50% cap.
LIVE = dict(equity=978.91, cash=489.09, deployed=489.82, max_risk_pct=0.50)


def trades(pnls, *, sym="QQQ", hours=12):
    return [{"pnl": p, "qty": 0.2, "entry_price": 500.0, "exit_price": 500.0 + p / 0.2,
             "opened_at": None, "closed_at": T0 + timedelta(hours=i * hours),
             "symbol": sym} for i, p in enumerate(pnls)]


def test_live_cap_is_binding():
    cap = ag.cap_state(equity=978.91, deployed=489.82, max_risk_pct=0.50)
    assert abs(cap["cap_usd"] - 489.455) < 0.01
    assert cap["cap_binding"] is True


def test_no_trades_never_raises_cap():
    out = ag.diagnose([], **LIVE)
    assert out["bottleneck"] == "NOT_TRADING"
    assert out["lever"] == "WAIT_FOR_TRADES"


def test_thin_sample_never_raises_cap_even_if_profitable():
    out = ag.diagnose(trades([1.0] * 10), **LIVE)
    assert out["bottleneck"] == "TOO_EARLY"
    assert out["lever"] == "KEEP_CAP_GATHER_SAMPLE"


def test_negative_edge_never_raises_cap_and_names_the_drag():
    ts = trades([0.5] * 20, sym="QQQ") + trades([-2.0] * 20, sym="NVDA")
    out = ag.diagnose(ts, **LIVE)
    assert out["bottleneck"] == "NO_EDGE"
    assert out["lever"] == "FIX_EDGE_FIRST"
    assert "NVDA" in out["lever_detail"]
    assert out["by_symbol"][0]["symbol"] == "NVDA"   # worst first


def test_breakeven_counts_as_no_edge():
    out = ag.diagnose(trades([1.0, -1.0] * 20), **LIVE)
    assert out["lever"] == "FIX_EDGE_FIRST"


def test_positive_edge_with_full_cap_recommends_raising_it():
    out = ag.diagnose(trades([0.6, 0.6, -0.4] * 12), **LIVE)
    assert out["kpis"]["net_edge_per_trade_usd"] > 0
    assert out["lever"] == "RAISE_RISK_CAP"
    assert "PROP_MAX_RISK_PERCENT" in out["lever_detail"]


def test_positive_edge_with_room_left_does_not_blame_the_cap():
    live = dict(LIVE, deployed=200.0, cash=778.91)
    out = ag.diagnose(trades([0.6, 0.6, -0.4] * 12), **live)
    assert out["cap"]["cap_binding"] is False
    assert out["lever"] == "DEPLOY_WITHIN_CAP"


def test_unreadable_equity_is_unknown_not_binding():
    cap = ag.cap_state(equity=None, deployed=100, max_risk_pct=0.5)
    assert cap["cap_binding"] is None


def test_concentration_is_named():
    ts = trades([1.0] * 30, sym="USO") + trades([0.01] * 5, sym="SPY")
    out = ag.diagnose(ts, **LIVE)
    assert out["concentration"] and "USO" in out["concentration"]


def test_endpoint_reads_both_alpaca_bots_and_ignores_crypto():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{path}"
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    import models
    import routers.trading_dashboard as td

    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def run():
        async with engine.begin() as conn:
            await conn.run_sync(models.Base.metadata.create_all)
        now = datetime.utcnow()
        async with factory() as db:
            for i in range(20):
                db.add(models.ClosedTrade(bot="prop_apex", symbol="QQQ", side="long",
                                          entry_price=500, exit_price=502, qty=0.2, pnl=0.4,
                                          closed_at=now - timedelta(hours=10 * i + 1)))
            for i in range(15):
                db.add(models.ClosedTrade(bot="alpaca_swing", symbol="SLV", side="long",
                                          entry_price=30, exit_price=30.5, qty=3, pnl=1.5,
                                          closed_at=now - timedelta(hours=10 * i + 2)))
            db.add(models.ClosedTrade(bot="crypto_coinbase", symbol="BTC-USD", side="long",
                                      entry_price=1, exit_price=1, qty=1, pnl=-999,
                                      closed_at=now - timedelta(hours=3)))
            await db.commit()

        async def acct(_s):
            return {"equity": "978.91", "cash": "489.09"}

        async def pos(_s):
            return [{"market_value": "126.40"}, {"market_value": "129.45"},
                    {"market_value": "131.28"}, {"market_value": "102.69"}]

        td._ALPACA_GROWTH_CACHE.update(at=0.0, key=None, payload=None)
        with mock.patch.object(td, "get_session_factory", lambda: factory), \
             mock.patch.object(td, "_fetch_alpaca_account", acct), \
             mock.patch.object(td, "_fetch_alpaca_positions", pos):
            return await td.alpaca_growth(days=60, fresh=1)

    out = asyncio.run(run())
    assert out["trades_by_bot"] == {"prop_apex": 20, "alpaca_swing": 15}
    assert out["kpis"]["trades"] == 35
    assert out["kpis"]["net_usd"] == round(20 * 0.4 + 15 * 1.5, 4)  # no -999 crypto row
    assert out["cap"]["cap_binding"] is True
    assert out["lever"] == "RAISE_RISK_CAP"
    os.unlink(path)

"""Actually EXECUTE the post-expiry and cohort code against a real SQLite DB.

test_governs_panel_live.py reads the source and proves the code SAYS the
right things. It cannot prove any of it runs. That gap has cost this account
before: a re-anchor patch referenced row.open_slices, a column that does not
exist on CryptoGridBranch, and it parsed perfectly and would have thrown on
every cycle.

So this one runs the real functions against a real database with a fake
order book, and checks the thing most likely to be silently backwards: the
SIGN. Price falls after a cancellation ->

    cancelled BUY  : we did not buy, we can buy cheaper  -> benefit POSITIVE
    cancelled SELL : we still hold, worth less           -> benefit NEGATIVE

A flipped sign here would not crash anything. It would quietly recommend the
opposite timeout.

No network: engine.get_best_bid_ask is replaced. No Coinbase credentials
needed; the import-time warning about them is expected and harmless.

Run: python3 test_expiry_drift_runtime.py
"""
import asyncio, datetime as dt, os, sys
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:////tmp/t_expiry.db"
for f in ("/tmp/t_expiry.db",):
    if os.path.exists(f): os.remove(f)
sys.path.insert(0, ".")

from database import Base, get_engine, get_session_factory
import models, crypto_grid_bot as g

class FakeBook:
    """Stands in for the Coinbase book so no network is touched."""
    def __init__(self): self.mid = 100.0
    async def get_best_bid_ask(self, session, product_id):
        return self.mid * 0.999, self.mid * 1.001

async def main():
    async with get_engine().begin() as c:
        await c.run_sync(Base.metadata.create_all)
    book = FakeBook()
    g.engine.get_best_bid_ask = book.get_best_bid_ask
    g.maker_wait_seconds = lambda: asyncio.sleep(0, result=240)

    # 1. anchor a cancelled BUY and a cancelled SELL
    await g._record_maker_expiry(None, "TIA-USD", "buy", "crypto_grid_7")
    await g._record_maker_expiry(None, "NEAR-USD", "sell", "crypto_grid_2")
    d = await g.get_maker_expiry_drift()
    print("after anchoring :", d["expiries"], "expiries |", d["buy"], "buy,", d["sell"], "sell")
    assert d["expiries"] == 2 and d["buy"] == 1 and d["sell"] == 1

    # 2. backdate them 11 minutes so every horizon is due, then move price DOWN 2%
    async with get_session_factory()() as db:
        from sqlalchemy import select
        for r in (await db.execute(select(models.GridMakerExpiry))).scalars().all():
            r.expired_at = dt.datetime.utcnow() - dt.timedelta(seconds=660)
        await db.commit()
    book.mid = 98.0
    await g._resolve_maker_expiries(None)

    async with get_session_factory()() as db:
        from sqlalchemy import select
        rows = {r.product_id: r for r in (await db.execute(select(models.GridMakerExpiry))).scalars().all()}
    buy, sell = rows["TIA-USD"], rows["NEAR-USD"]
    print("price moved -2.00%%: buy drift %.3f%% benefit %.3f%% | sell drift %.3f%% benefit %.3f%%"
          % (buy.drift_10m_pct, buy.cancel_benefit_10m_pct, sell.drift_10m_pct, sell.cancel_benefit_10m_pct))

    # THE SIGN. Price fell after we cancelled.
    #   cancelled BUY  -> we can buy cheaper now  -> cancelling HELPED  -> positive
    #   cancelled SELL -> we still hold, worth less -> cancelling HURT  -> negative
    assert buy.cancel_benefit_10m_pct > 0, "a cancelled BUY must score a FALL as a benefit"
    assert sell.cancel_benefit_10m_pct < 0, "a cancelled SELL must score a FALL as a cost"
    assert abs(buy.drift_10m_pct + 2.0) < 0.01, buy.drift_10m_pct
    assert buy.resolved_at is not None and sell.resolved_at is not None
    for t in ("1m","3m","5m","10m"):
        assert getattr(buy, f"price_{t}") is not None, f"horizon {t} unresolved"

    # 3. idempotence: a second pass must not touch resolved rows
    before = buy.cancel_benefit_10m_pct
    book.mid = 150.0
    await g._resolve_maker_expiries(None)
    async with get_session_factory()() as db:
        from sqlalchemy import select
        again = {r.product_id: r for r in (await db.execute(select(models.GridMakerExpiry))).scalars().all()}
    assert again["TIA-USD"].cancel_benefit_10m_pct == before, "resolved rows were rewritten"
    print("idempotent      : re-running with price at 150 left the row at %.3f%%" % before)

    d = await g.get_maker_expiry_drift()
    print("verdict         :", d["verdict"])
    print("10m horizon     :", d["horizons"]["10m"])
    assert "not enough data" in d["verdict"], "2 samples must not produce a finding"

    # 4. realized-edge cohorts on an empty book, and with a retired trade
    e = await g.get_realized_edge()
    print("empty book      : current", e["current"]["trades"], "| retired", e["retired"]["trades"],
          "| baseline", e["current_has_baseline"])
    async with get_session_factory()() as db:
        db.add(models.CryptoGridTradeHistory(
            bot_name="crypto_grid_1", product_id="DOGE-USD", entry_price=0.20,
            exit_price=0.21, qty=100.0, pnl=0.86,
            opened_at=dt.datetime(2026, 9, 3), closed_at=dt.datetime(2026, 9, 5)))
        db.add(models.CryptoGridTradeHistory(
            bot_name="crypto_grid_7", product_id="TIA-USD", entry_price=0.49,
            exit_price=0.51, qty=14.0, pnl=0.21,
            opened_at=dt.datetime(2026, 9, 26, 3), closed_at=dt.datetime(2026, 9, 26, 5)))
        await db.commit()
    e = await g.get_realized_edge()
    print("mixed book      : current", e["current"], )
    print("                  retired trades", e["retired"]["trades"], e["retired"]["coins"])
    assert e["current"]["trades"] == 1 and e["current"]["coins"] == ["TIA-USD"]
    assert e["retired"]["trades"] == 1 and e["retired"]["coins"] == ["DOGE-USD"]
    assert e["current_has_baseline"] is False
    assert "no baseline yet" in e["headline"]
    print("\n  ALL RUNTIME CHECKS PASSED")

asyncio.run(main())

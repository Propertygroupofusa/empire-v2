"""_measured_trading_pace: realized Grid Bot profit over 30 days as a yearly
rate on grid capital - never the equity line, never a fabricated zero."""
import asyncio, os, tempfile
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{os.path.join(tempfile.mkdtemp(), 'p.db')}"
from datetime import datetime, timedelta
fails = 0
def check(n, c, d=""):
    global fails
    print(("PASS " if c else "FAIL ") + n + ("" if c else f"  [{d}]")); fails += 0 if c else 1

async def main():
    from database import init_db, get_session_factory
    await init_db()
    import routers.trading_dashboard as td
    from models import CryptoGridTradeHistory, CryptoGridBranch
    async with get_session_factory()() as db:
        db.add(CryptoGridBranch(bot_name="g1", product_id="X-USD", allocated_usd=8523.34, grid_pct=0.02,
                                num_levels=3, reference_price=1.0, active=True))
        for pnl, days in ((50.0, 3), (32.72, 20), (999.0, 45)):   # the 45-day-old trade is outside the window
            db.add(CryptoGridTradeHistory(bot_name="g1", product_id="X-USD", entry_price=1, exit_price=2, qty=1,
                                          pnl=pnl, closed_at=datetime.utcnow() - timedelta(days=days)))
        await db.commit()
    async with get_session_factory()() as db:
        p = await td._measured_trading_pace(db)
    check("only the last 30 days count", p["realized_usd"] == 82.72, p)
    check("capital is grid allocation", p["capital_usd"] == 8523.34, p)
    check("yearly rate = realized/capital x 365/30", abs(p["annual_rate"] - 82.72 / 8523.34 * 365 / 30) < 1e-4, p)
    check("daily figure", p["daily_usd"] == round(82.72 / 30, 2), p)
    saved = td.crypto_grid_bot_module
    td.crypto_grid_bot_module = None
    async with get_session_factory()() as db:
        p = await td._measured_trading_pace(db)
    td.crypto_grid_bot_module = saved
    check("no capital readable -> rate unknown (None), not 0", p["annual_rate"] is None and p["capital_usd"] is None, p)

asyncio.run(main())
print("ALL PASS" if not fails else f"{fails} FAILED"); raise SystemExit(1 if fails else 0)

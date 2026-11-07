"""The silence check's "hours since last fill" must count sales, not only
the buy time of slices that are still open.

Live 2026-11-07: /grid-status read hours_since_last_fill 502.76 (TIA's
2026-10-17 buy, the newest open slice) while the trade ledger's newest
close was 10.2 days old. A slice that sells takes its own opened_at with
it, so a fleet that sold yesterday could read as silent for weeks.
"""
import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

import routers.trading_dashboard as td
from models import Base, CryptoGridTradeHistory


def _fleet(opened_days_ago):
    opened = (datetime.utcnow() - timedelta(days=opened_days_ago)).isoformat() + "Z"
    return {
        "mode_active": True,
        "branches": [{
            "bot_name": "crypto_grid_7", "product_id": "TIA-USD",
            "current_price": 0.44, "reference_price": 0.68, "grid_pct": 0.03,
            "num_levels": 3, "open_slices": 1,
            "slices": [{"opened_at": opened, "qty": 23.19, "entry_price": 0.73,
                        "unrealized_net_usd": -6.9}],
        }],
    }


class SilenceCountsSales(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.path}")
        self.Session = sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

        async def _init():
            async with self.engine.begin() as c:
                await c.run_sync(Base.metadata.create_all)
        asyncio.run(_init())
        td._GRID_STATUS_CACHE["payload"] = None

    def tearDown(self):
        asyncio.run(self.engine.dispose())
        os.unlink(self.path)
        td._GRID_STATUS_CACHE["payload"] = None

    def _close(self, days_ago):
        async def _add():
            async with self.Session() as db:
                db.add(CryptoGridTradeHistory(
                    bot_name="crypto_grid_4", product_id="ONDO-USD",
                    entry_price=0.5, exit_price=0.52, qty=10, pnl=0.1,
                    closed_at=datetime.utcnow() - timedelta(days=days_ago)))
                await db.commit()
        asyncio.run(_add())

    def _silence(self, fleet):
        fake_bot = type("B", (), {"get_grid_status": AsyncMock(return_value=fleet)})()

        async def _run():
            with patch.object(td, "crypto_grid_bot_module", fake_bot), \
                 patch.object(td, "AsyncSessionLocal", self.Session), \
                 patch.object(td, "orders_not_placed", AsyncMock(return_value={"blocked": []})):
                resp = await td.get_grid_status_endpoint(fresh=1)
            import json
            return json.loads(resp.body)["silence"]
        return asyncio.run(_run())

    def test_a_newer_sale_wins_over_an_old_open_buy(self):
        self._close(days_ago=10.2)
        hrs = self._silence(_fleet(opened_days_ago=20.95)).get("hours_since_last_fill")
        self.assertIsNotNone(hrs)
        self.assertAlmostEqual(hrs, 10.2 * 24, delta=0.5)

    def test_an_older_sale_does_not_hide_a_newer_buy(self):
        self._close(days_ago=30)
        hrs = self._silence(_fleet(opened_days_ago=2)).get("hours_since_last_fill")
        self.assertAlmostEqual(hrs, 48, delta=0.5)

    def test_no_sales_on_record_keeps_the_open_slice_figure(self):
        hrs = self._silence(_fleet(opened_days_ago=3)).get("hours_since_last_fill")
        self.assertAlmostEqual(hrs, 72, delta=0.5)


if __name__ == "__main__":
    unittest.main()

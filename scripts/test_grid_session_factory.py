"""Regression check for Grid lazy sessions and legacy status schema repair."""

import asyncio
import os
import pathlib
import sqlite3
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def main():
    with tempfile.TemporaryDirectory() as temp_dir:
        database_path = pathlib.Path(temp_dir) / "grid-session.db"
        os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{database_path.as_posix()}"

        import database
        import crypto_family_tree_bot
        import crypto_grid_bot
        from models import CryptoGridBranch, CryptoGridSlice

        assert database.AsyncSessionLocal is None

        async def check():
            await database.init_db()
            connection = sqlite3.connect(database_path)
            connection.execute(
                "ALTER TABLE crypto_grid_branches DROP COLUMN self_tuned_multiplier"
            )
            connection.execute(
                "ALTER TABLE crypto_grid_slices DROP COLUMN entry_fee_rate"
            )
            connection.commit()
            connection.close()

            await database.ensure_grid_status_schema()
            await database.ensure_grid_status_schema()

            async with database.get_session_factory()() as session:
                session.add(CryptoGridBranch(
                    bot_name="crypto_grid_btc_usd",
                    product_id="BTC-USD",
                    allocated_usd=100.0,
                    active=True,
                    grid_pct=0.01,
                    num_levels=10,
                    reference_price=100.0,
                    self_tuned_multiplier=1.7,
                ))
                session.add(CryptoGridSlice(
                    bot_name="crypto_grid_btc_usd",
                    product_id="BTC-USD",
                    entry_price=100.0,
                    qty=0.1,
                    entry_fee_rate=0.001,
                ))
                await session.commit()

            async def constant(value):
                return value

            class FakeClientSession:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, exc_type, exc, traceback):
                    return False

            crypto_grid_bot.is_grid_bot_active = lambda: constant(True)
            crypto_grid_bot.get_effective_round_trip_fee_rate = lambda: constant(0.002)
            crypto_grid_bot.expected_leg_fee_rate = lambda: constant(0.001)
            crypto_grid_bot.is_maker_orders_active = lambda: constant(False)
            crypto_grid_bot.is_dynamic_spacing_active = lambda: constant(False)
            crypto_grid_bot.is_avg_swing_spacing_active = lambda: constant(True)
            crypto_grid_bot.get_live_grid_spacing_override = lambda: constant(None)
            crypto_grid_bot.is_grid_auto_rotate_active = lambda: constant(False)
            crypto_grid_bot.get_real_free_cash_usd = lambda: constant(50.0)
            crypto_grid_bot.fee_safe_floor_pct = lambda: constant(0.003)
            crypto_grid_bot.engine.aiohttp.ClientSession = FakeClientSession
            crypto_grid_bot.engine.get_price_and_volatility = (
                lambda session, product_id: constant((110.0, 0.0))
            )

            history = await crypto_grid_bot.get_grid_trade_history(limit_recent=1)
            branches = await crypto_grid_bot.get_grid_branches()
            status = await crypto_grid_bot.get_grid_status()

            connection = sqlite3.connect(database_path)
            branch_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(crypto_grid_branches)")
            }
            slice_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(crypto_grid_slices)")
            }
            connection.close()

            assert history["total_trade_count"] == 0
            assert len(branches) == 1
            assert "self_tuned_multiplier" in branch_columns
            assert "entry_fee_rate" in slice_columns
            assert status["branch_count"] == 1
            assert status["branches"][0]["self_tuned_multiplier"] == 1.7
            assert status["branches"][0]["open_slices"] == 1
            assert status["total_unrealized_net_usd"] == 0.98

        asyncio.run(check())

    print("PASS: Grid repairs legacy status schema and resolves lazy sessions")


if __name__ == "__main__":
    main()

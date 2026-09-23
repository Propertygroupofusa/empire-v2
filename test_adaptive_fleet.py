import unittest
import json
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

import adaptive_fleet_orchestrator as orchestrator
import crypto_grid_bot as bot
from models import CryptoGridBranch, CryptoGridSlice


class AdaptiveFleetEvaluationTests(unittest.TestCase):
    def test_splits_150_across_nine_products_without_losing_a_cent(self):
        allocations = bot._split_usd_evenly(150.0, 9)

        self.assertEqual(allocations, [16.67] * 6 + [16.66] * 3)
        self.assertEqual(round(sum(allocations), 2), 150.0)
        self.assertEqual([bot._safe_num_levels_for_allocation(value) for value in allocations], [3] * 9)

    def test_uses_established_nine_coin_universe(self):
        self.assertEqual(
            [product_id for product_id, _ in bot.ADAPTIVE_FLEET_STAGES],
            [
                "BTC-USD", "ETH-USD", "SOL-USD", "ADA-USD", "DOGE-USD",
                "XRP-USD", "LINK-USD", "AVAX-USD", "DOT-USD",
            ],
        )

    def test_missing_backtest_does_not_freeze_later_qualified_candidate(self):
        evaluation = bot.evaluate_adaptive_fleet_stages(
            realized_pnl=3000.0,
            claimed={"ETH-USD"},
            excluded=set(),
            roi_by_coin={"SOL-USD": 25.0},
        )

        self.assertEqual(evaluation["next_product_id"], "SOL-USD")
        self.assertEqual(evaluation["stages"][0]["state"], "waiting_for_backtest")
        self.assertEqual(evaluation["stages"][1]["state"], "active")
        self.assertEqual(evaluation["stages"][2]["state"], "eligible")

    def test_all_qualified_candidates_are_eligible_in_sequence(self):
        evaluation = bot.evaluate_adaptive_fleet_stages(
            realized_pnl=3000.0,
            claimed=set(),
            excluded={"BTC-USD"},
            roi_by_coin={
                "ETH-USD": bot.MIN_REQUIRED_ROI_PCT - 1.0,
                "SOL-USD": bot.MIN_REQUIRED_ROI_PCT + 1.0,
                "ADA-USD": bot.MIN_REQUIRED_ROI_PCT + 2.0,
            },
        )

        self.assertEqual(evaluation["next_product_id"], "SOL-USD")
        self.assertEqual(evaluation["eligible_product_ids"], ["SOL-USD", "ADA-USD"])
        states = {stage["product_id"]: stage["state"] for stage in evaluation["stages"]}
        self.assertEqual(states["BTC-USD"], "blocked_by_exclusion")
        self.assertEqual(states["ETH-USD"], "below_minimum_edge")
        self.assertEqual(states["SOL-USD"], "eligible")
        self.assertEqual(states["ADA-USD"], "eligible")

    def test_unmet_realized_profit_gate_still_blocks_later_candidates(self):
        evaluation = bot.evaluate_adaptive_fleet_stages(
            realized_pnl=100.0,
            claimed={"ETH-USD"},
            excluded=set(),
            roi_by_coin={
                "SOL-USD": bot.MIN_REQUIRED_ROI_PCT + 1.0,
                "DOGE-USD": bot.MIN_REQUIRED_ROI_PCT + 2.0,
            },
        )

        self.assertIsNone(evaluation["next_product_id"])
        self.assertEqual(evaluation["eligible_product_ids"], [])
        states = {stage["product_id"]: stage["state"] for stage in evaluation["stages"]}
        self.assertEqual(states["SOL-USD"], "waiting_for_realized_profit")
        self.assertEqual(states["DOGE-USD"], "waiting_for_prior_stage")


class AdaptiveFleetRegistryTests(unittest.TestCase):
    def test_stale_registry_is_repaired_to_all_nine_coins(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            registry_path = os.path.join(temporary_directory, "fleet_registry.json")
            with open(registry_path, "w", encoding="utf-8") as registry_file:
                json.dump({"coins": {"BTC-USD": {"active": True}}}, registry_file)

            with patch.object(orchestrator, "FLEET_REGISTRY", registry_path):
                registry = orchestrator.get_or_create_fleet_registry()

        self.assertEqual(set(registry["coins"]), set(orchestrator.NINE_COINS))
        self.assertEqual(orchestrator.get_fleet_profit(registry), 0.0)


class AdaptiveFleetDeploymentTests(unittest.IsolatedAsyncioTestCase):
    async def test_reallocates_150_atomically_without_changing_total_reservations(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = os.path.join(temporary_directory, "fleet.db").replace("\\", "/")
            database_engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
            session_factory = sessionmaker(database_engine, class_=AsyncSession, expire_on_commit=False)
            async with database_engine.begin() as connection:
                await connection.run_sync(CryptoGridBranch.__table__.create)
                await connection.run_sync(CryptoGridSlice.__table__.create)
            async with session_factory() as database:
                database.add_all([
                    CryptoGridBranch(
                        bot_name="crypto_grid_2", product_id="ETH-USD", allocated_usd=100.0,
                        active=True, grid_pct=0.01, num_levels=10, reference_price=3000.0,
                    ),
                    CryptoGridBranch(
                        bot_name="crypto_grid_4", product_id="ARB-USD", allocated_usd=816.96,
                        active=True, grid_pct=0.01, num_levels=10, reference_price=0.45, locked=False,
                    ),
                ])
                await database.commit()

            class FakeClientSession:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, exc_type, exc, traceback):
                    return False

            with (
                patch.object(bot, "get_session_factory", return_value=session_factory),
                patch.object(bot.engine.aiohttp, "ClientSession", return_value=FakeClientSession()),
                patch.object(bot.engine, "get_price_and_volatility", AsyncMock(return_value=(100.0, 1.0))),
                patch.object(bot, "get_live_grid_spacing_override", AsyncMock(return_value="live_default")),
                patch.object(bot, "_log_activity_safe", AsyncMock()),
            ):
                result = await bot.reallocate_grid_cash_across_adaptive_fleet("crypto_grid_4", 150.0)

            async with session_factory() as database:
                rows = list((await database.execute(select(CryptoGridBranch))).scalars().all())
            await database_engine.dispose()

        by_product = {row.product_id: row for row in rows}
        fleet_products = [product_id for product_id, _ in bot.ADAPTIVE_FLEET_STAGES]
        self.assertEqual(set(fleet_products), set(by_product) - {"ARB-USD"})
        self.assertAlmostEqual(by_product["ARB-USD"].allocated_usd, 666.96)
        self.assertAlmostEqual(by_product["ETH-USD"].allocated_usd, 116.67)
        self.assertEqual(len(rows), 10)
        self.assertEqual([item["amount"] for item in result["allocations"]], [16.67] * 6 + [16.66] * 3)
        self.assertAlmostEqual(sum(row.allocated_usd for row in rows), 916.96)
        self.assertFalse(result["orders_placed"])

    async def test_open_source_slice_rolls_back_the_entire_reallocation(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = os.path.join(temporary_directory, "fleet.db").replace("\\", "/")
            database_engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
            session_factory = sessionmaker(database_engine, class_=AsyncSession, expire_on_commit=False)
            async with database_engine.begin() as connection:
                await connection.run_sync(CryptoGridBranch.__table__.create)
                await connection.run_sync(CryptoGridSlice.__table__.create)
            async with session_factory() as database:
                database.add(CryptoGridBranch(
                    bot_name="crypto_grid_4", product_id="ARB-USD", allocated_usd=816.96,
                    active=True, grid_pct=0.01, num_levels=10, reference_price=0.45, locked=False,
                ))
                database.add(CryptoGridSlice(
                    bot_name="crypto_grid_4", product_id="ARB-USD", entry_price=0.45, qty=10.0,
                ))
                await database.commit()

            class FakeClientSession:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, exc_type, exc, traceback):
                    return False

            with (
                patch.object(bot, "get_session_factory", return_value=session_factory),
                patch.object(bot.engine.aiohttp, "ClientSession", return_value=FakeClientSession()),
                patch.object(bot.engine, "get_price_and_volatility", AsyncMock(return_value=(100.0, 1.0))),
                patch.object(bot, "get_live_grid_spacing_override", AsyncMock(return_value="live_default")),
            ):
                with self.assertRaisesRegex(ValueError, "real open slices"):
                    await bot.reallocate_grid_cash_across_adaptive_fleet("crypto_grid_4", 150.0)

            async with session_factory() as database:
                rows = list((await database.execute(select(CryptoGridBranch))).scalars().all())
            await database_engine.dispose()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].product_id, "ARB-USD")
        self.assertAlmostEqual(rows[0].allocated_usd, 816.96)

    async def test_deploys_multiple_qualified_coins_with_available_capital(self):
        statuses = [
            {"next_product_id": "SOL-USD"},
            {"next_product_id": "ADA-USD"},
            {"next_product_id": None},
        ]
        create_branch = AsyncMock(side_effect=[
            SimpleNamespace(bot_name="crypto_grid_5", product_id="SOL-USD"),
            SimpleNamespace(bot_name="crypto_grid_6", product_id="ADA-USD"),
        ])

        with (
            patch.object(bot, "GRID_ADAPTIVE_FLEET_ENABLED", True),
            patch.object(bot, "GRID_AUTO_DEPLOY_MAX_NEW_BRANCHES_PER_SWEEP", 3),
            patch.object(bot, "get_real_free_cash_usd", AsyncMock(return_value=500.0)),
            patch.object(bot, "get_adaptive_fleet_status", AsyncMock(side_effect=statuses)),
            patch.object(bot, "create_grid_branch", create_branch),
            patch.object(bot, "_log_activity_safe", AsyncMock()),
        ):
            await bot._auto_deploy_idle_free_cash()

        self.assertEqual(
            [call.args for call in create_branch.await_args_list],
            [
                ("SOL-USD", bot.GRID_AUTO_DEPLOY_AMOUNT_USD),
                ("ADA-USD", bot.GRID_AUTO_DEPLOY_AMOUNT_USD),
            ],
        )


if __name__ == "__main__":
    unittest.main()
import unittest
import json
import os
import tempfile
from unittest.mock import patch

import adaptive_fleet_orchestrator as orchestrator
import crypto_grid_bot as bot


class AdaptiveFleetEvaluationTests(unittest.TestCase):
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

    def test_candidate_failures_are_skipped_but_only_one_candidate_is_eligible(self):
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
        states = {stage["product_id"]: stage["state"] for stage in evaluation["stages"]}
        self.assertEqual(states["BTC-USD"], "blocked_by_exclusion")
        self.assertEqual(states["ETH-USD"], "below_minimum_edge")
        self.assertEqual(states["SOL-USD"], "eligible")
        self.assertEqual(states["ADA-USD"], "waiting_for_prior_stage")

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


if __name__ == "__main__":
    unittest.main()
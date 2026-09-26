"""Focused regression for the opt-in Adaptive Grid Fleet policy."""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import crypto_grid_bot as grid


GOOD_ROI = {
    "BTC-USD": grid.MIN_REQUIRED_ROI_PCT,
    "ETH-USD": grid.MIN_REQUIRED_ROI_PCT,
    "SOL-USD": grid.MIN_REQUIRED_ROI_PCT,
    "ADA-USD": grid.MIN_REQUIRED_ROI_PCT,
}


def stage_states(result):
    return {stage["product_id"]: stage["state"] for stage in result["stages"]}


def test_stage_policy():
    result = grid.evaluate_adaptive_fleet_stages(0.0, {"BTC-USD"}, set(), GOOD_ROI)
    assert result["next_product_id"] == "ETH-USD"

    result = grid.evaluate_adaptive_fleet_stages(687.99, {"BTC-USD", "ETH-USD"}, set(), GOOD_ROI)
    assert result["next_product_id"] is None
    assert stage_states(result)["SOL-USD"] == "waiting_for_realized_profit"

    result = grid.evaluate_adaptive_fleet_stages(688.0, {"BTC-USD", "ETH-USD"}, set(), GOOD_ROI)
    assert result["next_product_id"] == "SOL-USD"

    result = grid.evaluate_adaptive_fleet_stages(
        2106.0, {"BTC-USD", "ETH-USD", "SOL-USD"}, set(), GOOD_ROI
    )
    assert result["next_product_id"] == "ADA-USD"

    result = grid.evaluate_adaptive_fleet_stages(5000.0, set(GOOD_ROI), set(), GOOD_ROI)
    assert result["next_product_id"] is None
    assert all(state == "active" for state in stage_states(result).values())

    weak_roi = {**GOOD_ROI, "ETH-USD": grid.MIN_REQUIRED_ROI_PCT - 0.01}
    result = grid.evaluate_adaptive_fleet_stages(5000.0, {"BTC-USD"}, set(), weak_roi)
    assert result["next_product_id"] is None
    assert stage_states(result)["ETH-USD"] == "below_minimum_edge"
    assert stage_states(result)["SOL-USD"] == "waiting_for_prior_stage"


async def test_deployment_guards():
    originals = {
        "enabled": grid.GRID_ADAPTIVE_FLEET_ENABLED,
        "free_cash": grid.get_real_free_cash_usd,
        "status": grid.get_adaptive_fleet_status,
        "create": grid.create_grid_branch,
        "log": grid._log_activity_safe,
    }
    created = []

    async def status():
        return {"next_product_id": "ETH-USD"}

    async def create(product_id, amount):
        created.append((product_id, amount))
        return SimpleNamespace(bot_name="Grid Fleet Test", product_id=product_id)

    async def no_log(*args, **kwargs):
        return None

    try:
        grid.GRID_ADAPTIVE_FLEET_ENABLED = True
        grid.get_adaptive_fleet_status = status
        grid.create_grid_branch = create
        grid._log_activity_safe = no_log

        async def insufficient_cash():
            return grid.GRID_AUTO_DEPLOY_AMOUNT_USD - 0.01

        grid.get_real_free_cash_usd = insufficient_cash
        await grid._auto_deploy_idle_free_cash()
        assert created == []

        async def sufficient_cash():
            return grid.GRID_AUTO_DEPLOY_AMOUNT_USD * 10

        grid.get_real_free_cash_usd = sufficient_cash
        await grid._auto_deploy_idle_free_cash()
        assert created == [("ETH-USD", grid.GRID_AUTO_DEPLOY_AMOUNT_USD)]
    finally:
        grid.GRID_ADAPTIVE_FLEET_ENABLED = originals["enabled"]
        grid.get_real_free_cash_usd = originals["free_cash"]
        grid.get_adaptive_fleet_status = originals["status"]
        grid.create_grid_branch = originals["create"]
        grid._log_activity_safe = originals["log"]


async def main():
    test_stage_policy()
    await test_deployment_guards()
    print("Adaptive Grid Fleet regression passed")


if __name__ == "__main__":
    asyncio.run(main())
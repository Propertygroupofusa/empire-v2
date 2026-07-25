#!/usr/bin/env python3
"""Real-time P&L tracker for live Alpaca trading account ($980 capital)."""
import os
import json
import asyncio
import logging
from datetime import datetime
from pathlib import Path
import httpx
from collections import defaultdict

# Setup logging
log_dir = Path("logs")
log_dir.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(log_dir / "bot_pl_tracker.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# Configuration
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://api.alpaca.markets")
LIVE_TRADE = os.getenv("ALPACA_LIVE_TRADE", "true").lower() == "true"
STARTING_CAPITAL = 980.0

# Data storage
HISTORY_FILE = Path("bot_pl_history.json")
MILESTONES = [1000, 5000, 10000, 25000, 50000, 100000]
MAX_SNAPSHOTS = 1440  # Keep 1 day of minute-level snapshots


class AlpacaTracker:
    def __init__(self):
        self.base_url = ALPACA_BASE_URL
        self.headers = {
            "APCA-API-KEY-ID": ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
        }
        self.history = self._load_history()
        self.last_summary_time = None
        self.retry_count = 0
        self.max_retries = 3

    def _load_history(self):
        """Load historical data if exists."""
        if HISTORY_FILE.exists():
            try:
                data = json.loads(HISTORY_FILE.read_text())
                if isinstance(data, dict):
                    return data
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"Failed to load history: {e}")
        return {"snapshots": [], "milestones_hit": []}

    def _save_history(self):
        """Save history to file, pruning old snapshots to prevent unbounded growth."""
        if "snapshots" in self.history and len(self.history["snapshots"]) > MAX_SNAPSHOTS:
            self.history["snapshots"] = self.history["snapshots"][-MAX_SNAPSHOTS:]
        try:
            HISTORY_FILE.write_text(json.dumps(self.history, indent=2, default=str))
        except IOError as e:
            logger.error(f"Failed to save history: {e}")

    async def _fetch_with_retry(self, session: httpx.AsyncClient, endpoint: str, retries: int = 3) -> dict:
        """Fetch with exponential backoff retry."""
        for attempt in range(retries):
            try:
                response = await session.get(f"{self.base_url}{endpoint}")
                response.raise_for_status()
                self.retry_count = 0
                return response.json()
            except httpx.HTTPError as e:
                if attempt < retries - 1:
                    delay = 2 ** attempt
                    logger.debug(f"Fetch failed, retrying in {delay}s: {e}")
                    await asyncio.sleep(delay)
                else:
                    logger.error(f"Failed to fetch {endpoint}: {e}")
                    self.retry_count += 1
                    return None
            except Exception as e:
                logger.error(f"Unexpected error fetching {endpoint}: {e}")
                return None

    async def get_account(self):
        """Fetch live account data from Alpaca."""
        async with httpx.AsyncClient(headers=self.headers, timeout=10.0) as session:
            return await self._fetch_with_retry(session, "/v2/account")

    async def get_positions(self):
        """Fetch open positions."""
        async with httpx.AsyncClient(headers=self.headers, timeout=10.0) as session:
            data = await self._fetch_with_retry(session, "/v2/positions")
            return data if isinstance(data, list) else []

    async def track(self):
        """Main tracking loop."""
        logger.info("🤖 P&L Tracker Starting")
        logger.info(f"   Account: {ALPACA_API_KEY[:20]}...")
        logger.info(f"   Mode: {'LIVE' if LIVE_TRADE else 'PAPER'}")
        logger.info(f"   Starting Capital: ${STARTING_CAPITAL:,.2f}")
        logger.info("")

        update_count = 0
        summary_interval = 5  # Summary every 5 minutes (60s * 5 updates)

        try:
            while True:
                account = await self.get_account()
                if not account:
                    await asyncio.sleep(60)
                    continue

                positions = await self.get_positions()

                # Calculate metrics
                portfolio_value = float(account.get("portfolio_value", 0))
                cash = float(account.get("cash", 0))
                buying_power = float(account.get("buying_power", 0))
                unrealized_pl = float(account.get("unrealized_pl", 0))
                unrealized_plpc = float(account.get("unrealized_plpc", 0))

                profit = portfolio_value - STARTING_CAPITAL
                profit_pct = (profit / STARTING_CAPITAL * 100) if STARTING_CAPITAL else 0

                try:
                    snapshot = {
                        "timestamp": datetime.utcnow().isoformat(),
                        "portfolio_value": round(portfolio_value, 2),
                        "cash": round(cash, 2),
                        "profit": round(profit, 2),
                        "profit_pct": round(profit_pct, 2),
                        "unrealized_pl": round(unrealized_pl, 2),
                        "unrealized_plpc": round(unrealized_plpc, 2),
                        "buying_power": round(buying_power, 2),
                        "positions_count": len(positions),
                    }
                    self.history["snapshots"].append(snapshot)
                except (ValueError, TypeError) as e:
                    logger.warning(f"Failed to record snapshot: {e}")
                    continue

                # Log update
                update_count += 1
                timestamp = datetime.utcnow().strftime("%H:%M:%S")
                emoji = "📈" if profit > 0 else "📉"
                logger.info(
                    f"{emoji} [{timestamp}] Portfolio: ${portfolio_value:>10,.2f} | "
                    f"P&L: ${profit:>+8,.2f} ({profit_pct:>+6.2f}%) | "
                    f"Cash: ${cash:>10,.2f}"
                )

                # Detailed summary every 5 updates (5 minutes)
                if update_count % summary_interval == 0:
                    logger.info("")
                    logger.info("=" * 70)
                    logger.info("PORTFOLIO SUMMARY")
                    logger.info("=" * 70)
                    logger.info(f"Portfolio Value:      ${portfolio_value:>12,.2f}")
                    logger.info(f"Starting Capital:     ${STARTING_CAPITAL:>12,.2f}")
                    logger.info(
                        f"Profit/Loss:          ${profit:>12,.2f} ({profit_pct:+.2f}%)"
                    )
                    logger.info(f"Cash Available:       ${cash:>12,.2f}")
                    logger.info(f"Buying Power:         ${buying_power:>12,.2f}")
                    logger.info(f"Unrealized P&L:       ${unrealized_pl:>12,.2f}")
                    logger.info("")

                    # Log positions
                    if positions:
                        logger.info(f"Open Positions ({len(positions)}):")
                        for pos in positions:
                            symbol = pos.get("symbol", "?")
                            qty = float(pos.get("qty", 0))
                            market_value = float(pos.get("market_value", 0))
                            unrealized_pl = float(pos.get("unrealized_pl", 0))
                            unrealized_plpc = float(pos.get("unrealized_plpc", 0))
                            logger.info(
                                f"  {symbol:>6} x {qty:>8.0f} | "
                                f"Value: ${market_value:>10,.2f} | "
                                f"P&L: ${unrealized_pl:>+8,.2f} ({unrealized_plpc:+.2f}%)"
                            )
                        logger.info("")
                    else:
                        logger.info("  No open positions")
                        logger.info("")

                    milestones_hit = self.history.get("milestones_hit", [])
                    for milestone in MILESTONES:
                        if STARTING_CAPITAL < milestone <= portfolio_value and milestone not in milestones_hit:
                            logger.warning("")
                            logger.warning("🎯 " + "=" * 66)
                            logger.warning(f"🎯 MILESTONE HIT: ${milestone:,} 🎯")
                            logger.warning("🎯 " + "=" * 66)
                            logger.warning("")
                            milestones_hit.append(milestone)
                            self.history["milestones_hit"] = milestones_hit

                    logger.info("=" * 70)
                    logger.info("")

                # Save history periodically
                if update_count % 10 == 0:
                    self._save_history()

                # Wait 60 seconds before next update
                await asyncio.sleep(60)

        except KeyboardInterrupt:
            logger.info("")
            logger.info("🛑 Tracker stopped by user")
            self._save_history()
        except Exception as e:
            logger.error(f"❌ Tracker error: {e}")
            self._save_history()
            raise


async def main():
    """Run the tracker."""
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        logger.error("❌ Missing ALPACA_API_KEY or ALPACA_SECRET_KEY in environment")
        return

    tracker = AlpacaTracker()
    await tracker.track()


if __name__ == "__main__":
    asyncio.run(main())

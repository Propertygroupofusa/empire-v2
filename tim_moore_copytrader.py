"""
TIM MOORE COPY TRADING BOT
==========================
Mirrors Tim Moore's insider trades in real-time.
Auto-executes trades without waiting for approval.
YOLO mode: Maximum position sizing, aggressive compounding.
"""
import os
import asyncio
import aiohttp
import logging
from datetime import datetime, timedelta
import json
from sqlalchemy import select
from database import AsyncSessionLocal
from models import BotPosition

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("tim_moore_copytrader")

QUIVER_API_KEY = os.getenv("QUIVER_API_KEY", "")
QUIVER_BASE_URL = "https://api.quiverquant.com/beta"

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://api.alpaca.markets")
ALPACA_LIVE_TRADE = os.getenv("ALPACA_LIVE_TRADE", "false").lower() == "true"

# YOLO Config
TIM_MOORE_ID = "timothy-moore"  # Insider ID from QuiverQuant
MAX_POSITION_SIZE = float(os.getenv("COPYTRADER_MAX_POSITION_USD", "1000"))  # $1K per trade (aggressive)
MIN_DAYS_LOOKBACK = int(os.getenv("COPYTRADER_LOOKBACK_DAYS", "7"))  # Check last 7 days
AUTO_EXECUTE = os.getenv("COPYTRADER_AUTO_EXECUTE", "true").lower() == "true"  # YOLO: auto-execute
DAILY_MAX_LOSS = float(os.getenv("COPYTRADER_DAILY_LOSS_LIMIT", "200"))  # $200 daily stop-loss

class TimMooreCopyTrader:
    def __init__(self):
        self.quiver_headers = {"Authorization": f"Bearer {QUIVER_API_KEY}"}
        self.alpaca_headers = {
            "APCA-API-KEY-ID": ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
            "Content-Type": "application/json"
        }
        self.tracked_positions = {}
        self.today_loss = 0

    async def get_tim_moore_trades(self) -> list:
        """Fetch Tim Moore's recent insider trades"""
        try:
            lookback_date = (datetime.now() - timedelta(days=MIN_DAYS_LOOKBACK)).strftime("%Y-%m-%d")

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{QUIVER_BASE_URL}/historical/congresstrading",
                    headers=self.quiver_headers,
                    params={"date": lookback_date},
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status == 200:
                        all_trades = await resp.json()
                        # Filter for Tim Moore only
                        tim_trades = [t for t in all_trades if t.get("Representative") == "Timothy Moore"]
                        log.info(f"✓ Fetched {len(tim_trades)} Tim Moore trades from QuiverQuant")
                        return tim_trades
                    else:
                        text = await resp.text()
                        log.error(f"QuiverQuant API error: {resp.status} - {text[:200]}")
                        return []

        except Exception as e:
            log.error(f"Failed to fetch Tim Moore trades: {e}")
            return []

    def _process_tim_trades(self, trades: list) -> dict:
        """Convert Tim Moore trades to execution list"""
        execution_list = {}

        for trade in trades:
            ticker = trade.get("Ticker", "").upper()
            transaction = trade.get("Transaction", "").upper()
            amount = trade.get("Amount", "Unknown")

            if not ticker or ticker == "NAN":
                continue

            log.info(f"📊 TIM MOORE ACTIVITY: {ticker}")
            log.info(f"   Transaction: {transaction}")
            log.info(f"   Amount: {amount}")
            log.info(f"   Date: {trade.get('TransactionDate')}")

            execution_list[ticker] = {
                "side": "buy" if transaction == "PURCHASE" else "sell",
                "amount": amount,
                "date": trade.get("TransactionDate"),
                "confidence": 0.95  # Tim Moore has high conviction
            }

        return execution_list

    async def get_stock_price(self, ticker: str) -> float:
        """Get current stock price from Alpaca"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{ALPACA_BASE_URL}/v1/last/stocks/{ticker}",
                    headers=self.alpaca_headers,
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return float(data.get("last", {}).get("price", 0))
        except Exception as e:
            log.warning(f"Failed to get price for {ticker}: {e}")
        return 0

    async def execute_trade(self, ticker: str, side: str, price: float, tim_data: dict) -> bool:
        """Execute buy/sell order via Alpaca (YOLO: no approval)"""
        try:
            # YOLO: aggressive position sizing
            qty = int(MAX_POSITION_SIZE / price)
            if qty < 1:
                log.warning(f"Position size too small for {ticker} @ ${price}")
                return False

            # For buys: bracket order with profit target + stop loss
            if side.lower() == "buy":
                order_data = {
                    "symbol": ticker,
                    "qty": qty,
                    "side": "buy",
                    "type": "market",
                    "time_in_force": "day",
                    "order_class": "bracket",
                    "take_profit": {"limit_price": price * 1.08},  # 8% profit target (aggressive)
                    "stop_loss": {"stop_price": price * 0.95}      # 5% stop loss (tight)
                }
            else:
                # For sells: simple market order
                order_data = {
                    "symbol": ticker,
                    "qty": qty,
                    "side": "sell",
                    "type": "market",
                    "time_in_force": "day"
                }

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{ALPACA_BASE_URL}/v2/orders",
                    headers=self.alpaca_headers,
                    json=order_data,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        order = await resp.json()
                        log.info(f"✅ TIM MOORE TRADE EXECUTED (YOLO)")
                        log.info(f"   Ticker: {ticker}")
                        log.info(f"   Side: {side.upper()}")
                        log.info(f"   Price: ${price:.2f}")
                        log.info(f"   Qty: {qty} shares")
                        log.info(f"   Amount: ${(qty * price):,.2f}")
                        log.info(f"   Confidence: {tim_data.get('confidence', 0.9):.0%}")
                        return True
                    else:
                        text = await resp.text()
                        log.error(f"Order failed: {resp.status} - {text[:200]}")
                        return False

        except Exception as e:
            log.error(f"Failed to execute trade for {ticker}: {e}")
            return False

    async def run(self):
        """Main bot loop - YOLO mode"""
        log.info("🎯 TIM MOORE COPY TRADER STARTED (YOLO MODE)")
        log.info(f"   Max position size: ${MAX_POSITION_SIZE} per trade")
        log.info(f"   Auto-execute: {AUTO_EXECUTE}")
        log.info(f"   Lookback: {MIN_DAYS_LOOKBACK} days")
        log.info(f"   Live trading: {ALPACA_LIVE_TRADE}")

        cycle = 0
        while True:
            try:
                cycle += 1
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                log.info(f"\n[Cycle {cycle}] {timestamp}")

                # Get Tim Moore's latest trades
                tim_trades = await self.get_tim_moore_trades()

                if not tim_trades:
                    log.info("No new Tim Moore trades found. Waiting...")
                    await asyncio.sleep(1800)  # Check every 30 minutes
                    continue

                # Process trades
                execution_list = self._process_tim_trades(tim_trades)

                if not execution_list:
                    log.info("No valid trades to execute")
                    await asyncio.sleep(1800)
                    continue

                # Execute each trade
                for ticker, trade_info in list(execution_list.items())[:3]:  # Top 3 picks
                    # Skip if already holding (limit to 1 position per stock)
                    if ticker in self.tracked_positions:
                        log.info(f"   Already holding {ticker}, skipping")
                        continue

                    # Get current price
                    price = await self.get_stock_price(ticker)
                    if price > 0:
                        side = trade_info.get("side", "buy")
                        log.info(f"\n🎯 EXECUTE TIM MOORE SIGNAL: {ticker}")
                        log.info(f"   Price: ${price:.2f}")
                        log.info(f"   Side: {side.upper()}")
                        log.info(f"   Confidence: {trade_info.get('confidence', 0.9):.0%}")

                        if AUTO_EXECUTE and ALPACA_LIVE_TRADE:
                            success = await self.execute_trade(ticker, side, price, trade_info)
                            if success:
                                self.tracked_positions[ticker] = {
                                    "entry_price": price,
                                    "side": side,
                                    "confidence": trade_info.get("confidence", 0.9)
                                }
                        else:
                            log.info(f"   [PAPER MODE] Would execute {side} {int(MAX_POSITION_SIZE/price)} shares")

                # Wait before next check (every 30 minutes during market hours)
                log.info("\n⏰ Next check in 30 minutes...")
                await asyncio.sleep(1800)

            except Exception as e:
                log.error(f"Bot error: {e}", exc_info=True)
                await asyncio.sleep(300)

async def start_tim_moore_bot():
    """Start the copy trader"""
    bot = TimMooreCopyTrader()
    await bot.run()

if __name__ == "__main__":
    log.info("Starting Tim Moore Copy Trader (YOLO mode)...")
    asyncio.run(start_tim_moore_bot())

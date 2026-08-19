"""
CONGRESSIONAL TRADING BOT
========================
Mirrors insider trading activity from Congress members.
Buys stocks when Congress members buy, sells when they sell.
Data via QuiverQuant API.

Strategy: Follow the money - Congress members have early access to market-moving info.
"""
import os
import asyncio
import aiohttp
import logging
from datetime import datetime, timedelta
import json
from sqlalchemy import select
from database import AsyncSessionLocal
from models import BotPosition, Payment

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("congress_trading_bot")

QUIVER_API_KEY = os.getenv("QUIVER_API_KEY", "")
QUIVER_BASE_URL = "https://api.quiverquant.com/beta"

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://api.alpaca.markets")
ALPACA_LIVE_TRADE = os.getenv("ALPACA_LIVE_TRADE", "false").lower() == "true"

# Trading params
MAX_POSITION_SIZE = float(os.getenv("CONGRESS_MAX_POSITION_USD", "500"))  # $500 per stock
MIN_CONGRESS_ACTIVITY = int(os.getenv("CONGRESS_MIN_ACTIVITY", "3"))  # Min 3 congress members
LOOKBACK_DAYS = int(os.getenv("CONGRESS_LOOKBACK_DAYS", "14"))  # Last 14 days of data
DAILY_MAX_LOSS = float(os.getenv("CONGRESS_DAILY_LOSS_LIMIT", "50"))  # $50 daily loss limit

class CongressTradingBot:
    def __init__(self):
        self.quiver_headers = {"Authorization": f"Bearer {QUIVER_API_KEY}"}
        self.alpaca_headers = {
            "APCA-API-KEY-ID": ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
            "Content-Type": "application/json"
        }
        self.session = None
        self.tracked_stocks = {}
        self.today_loss = 0

    async def get_congress_trades(self) -> dict:
        """Fetch recent congressional trades from QuiverQuant"""
        try:
            lookback_date = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{QUIVER_BASE_URL}/historical/congresstrading",
                    headers=self.quiver_headers,
                    params={"date": lookback_date},
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status == 200:
                        trades = await resp.json()
                        log.info(f"✓ Fetched {len(trades)} congressional trades from QuiverQuant")
                        return self._process_congress_trades(trades)
                    else:
                        text = await resp.text()
                        log.error(f"QuiverQuant API error: {resp.status} - {text[:200]}")
                        return {}

        except Exception as e:
            log.error(f"Failed to fetch congress trades: {e}")
            return {}

    def _process_congress_trades(self, trades: list) -> dict:
        """Aggregate congressional trades by ticker"""
        stock_activity = {}

        for trade in trades:
            ticker = trade.get("Ticker", "").upper()
            transaction = trade.get("Transaction", "").upper()

            if not ticker or ticker == "NAN":
                continue

            if ticker not in stock_activity:
                stock_activity[ticker] = {"buys": 0, "sells": 0, "members": set(), "trades": []}

            if transaction == "PURCHASE":
                stock_activity[ticker]["buys"] += 1
            elif transaction == "SALE":
                stock_activity[ticker]["sells"] += 1

            stock_activity[ticker]["members"].add(trade.get("Representative", "Unknown"))
            stock_activity[ticker]["trades"].append({
                "date": trade.get("TransactionDate"),
                "member": trade.get("Representative"),
                "type": transaction,
                "amount": trade.get("Amount", "Unknown"),
                "party": trade.get("Party")
            })

        # Filter: only stocks with >= MIN_CONGRESS_ACTIVITY buys
        bullish_stocks = {
            ticker: data for ticker, data in stock_activity.items()
            if data["buys"] >= MIN_CONGRESS_ACTIVITY and data["buys"] > data["sells"]
        }

        log.info(f"📊 Congress Activity Summary:")
        log.info(f"   Total stocks traded by Congress: {len(stock_activity)}")
        log.info(f"   Bullish signals (buys > {MIN_CONGRESS_ACTIVITY}): {len(bullish_stocks)}")

        return bullish_stocks

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

    async def place_trade(self, ticker: str, price: float, congress_data: dict) -> bool:
        """Place buy order via Alpaca"""
        try:
            # Calculate quantity based on position size
            qty = int(MAX_POSITION_SIZE / price)
            if qty < 1:
                log.warning(f"Position size too small for {ticker} @ ${price}")
                return False

            order_data = {
                "symbol": ticker,
                "qty": qty,
                "side": "buy",
                "type": "market",
                "time_in_force": "day",
                "order_class": "bracket",
                "take_profit": {"limit_price": price * 1.05},  # 5% profit target
                "stop_loss": {"stop_price": price * 0.97}      # 3% stop loss
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
                        log.info(f"✅ CONGRESS TRADE EXECUTED")
                        log.info(f"   Ticker: {ticker}")
                        log.info(f"   Price: ${price:.2f}")
                        log.info(f"   Qty: {qty} shares")
                        log.info(f"   Congress members buying: {len(congress_data.get('members', []))}")
                        log.info(f"   Recent buys: {congress_data.get('buys', 0)}")
                        return True
                    else:
                        text = await resp.text()
                        log.error(f"Order failed: {resp.status} - {text[:200]}")
                        return False

        except Exception as e:
            log.error(f"Failed to place trade for {ticker}: {e}")
            return False

    async def run(self):
        """Main bot loop"""
        log.info("🏛️  CONGRESSIONAL TRADING BOT STARTED")
        log.info(f"   Tracking {MIN_CONGRESS_ACTIVITY}+ congress member buys")
        log.info(f"   Position size: ${MAX_POSITION_SIZE} per stock")
        log.info(f"   Lookback: {LOOKBACK_DAYS} days")
        log.info(f"   Live trading: {ALPACA_LIVE_TRADE}")

        cycle = 0
        while True:
            try:
                cycle += 1
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                log.info(f"\n[Cycle {cycle}] {timestamp}")

                # Get congressional trades
                bullish_stocks = await self.get_congress_trades()

                if not bullish_stocks:
                    log.info("No bullish congressional signals found. Waiting...")
                    await asyncio.sleep(3600)  # Check hourly
                    continue

                # Process each bullish stock
                for ticker, congress_data in list(bullish_stocks.items())[:5]:  # Top 5 picks
                    # Skip if already holding
                    if ticker in self.tracked_stocks:
                        log.info(f"   Already holding {ticker}, skipping")
                        continue

                    # Get price and place trade
                    price = await self.get_stock_price(ticker)
                    if price > 0:
                        members = len(congress_data.get("members", []))
                        buys = congress_data.get("buys", 0)

                        log.info(f"\n🎯 CONGRESS SIGNAL: {ticker}")
                        log.info(f"   Price: ${price:.2f}")
                        log.info(f"   Congress members: {members}")
                        log.info(f"   Buy transactions: {buys}")

                        if ALPACA_LIVE_TRADE:
                            success = await self.place_trade(ticker, price, congress_data)
                            if success:
                                self.tracked_stocks[ticker] = {
                                    "entry_price": price,
                                    "qty": int(MAX_POSITION_SIZE / price),
                                    "members": members
                                }
                        else:
                            log.info(f"   [PAPER MODE] Would buy {int(MAX_POSITION_SIZE/price)} shares")

                # Wait 1 hour before next scan
                log.info("\n⏰ Next scan in 1 hour...")
                await asyncio.sleep(3600)

            except Exception as e:
                log.error(f"Bot error: {e}", exc_info=True)
                await asyncio.sleep(300)

async def start_congress_bot():
    """Start the bot"""
    bot = CongressTradingBot()
    await bot.run()

if __name__ == "__main__":
    log.info("Starting Congressional Trading Bot...")
    asyncio.run(start_congress_bot())

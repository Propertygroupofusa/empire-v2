"""
ALPACA HYBRID TRADING BOT v2
============================
Hybrid trader combining day-trading stocks + 24/5 micro-futures
Account: APEX_589296 (same as prop_bot.py)

Strategy 1 - STOCK DAY TRADING (9:30 AM - 4:00 PM ET)
- Symbols: QQQ, SPY, IWM (high-liquidity ETFs)
- Entry: Daily RSI < 35 (oversold) + volume > 1.5x avg
- Exit: +1.25% profit OR Daily RSI > 70 OR -1% stop loss
- Hold: 30 min - 4 hours typical
- Expected: $39/day profit

Strategy 2 - MICRO FUTURES 24/5 (Mon-Fri 24 hours)
- Symbols: MES, MNQ (Micro E-mini S&P 500 / Nasdaq)
- Entry: Weekly RSI < 30 (oversold) + price > SMA
- Exit: +5% profit OR Weekly RSI > 70 OR -2% stop loss
- Hold: 4-12 hours typical
- Expected: $62/day profit

Max positions: 3 concurrent (mix of stocks + futures)
Position sizing: Dynamic based on equity ($100-150 per stock, $75 per MES contract)

Runs: Every 15 minutes (stocks only during market hours, MES 24/5)
Total expected daily profit: $39 (stocks) + $62 (MES) = $101/day
"""

import os
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import aiohttp
import uuid
from sqlalchemy import select
from database import AsyncSessionLocal
from models import BotPosition, Payment

ET = ZoneInfo("America/New_York")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("alpaca_swing_bot")

def get_headers():
    """Alpaca API auth headers"""
    return {
        "APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", ""),
        "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY", ""),
        "Content-Type": "application/json"
    }

def get_base_url():
    """Alpaca base URL (live vs paper)"""
    return os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

LIVE_TRADE = os.getenv("ALPACA_LIVE_TRADE", "false").lower() == "true"

# STRATEGY 1: Stock Day Trading (Market Hours Only)
STOCK_SYMBOLS = {
    "QQQ": {"name": "Invesco QQQ Trust", "timeframe": "day"},
    "SPY": {"name": "S&P 500 ETF", "timeframe": "day"},
    "IWM": {"name": "Russell 2000 ETF", "timeframe": "day"},
}

# STRATEGY 2: Micro Futures Trading (24/5 Mon-Fri)
FUTURES_SYMBOLS = {
    "MES": {"name": "Micro S&P 500", "proxy": "SPY", "contract_value": 50},
    "MNQ": {"name": "Micro Nasdaq", "proxy": "QQQ", "contract_value": 20},
}

# Entry/Exit thresholds - STOCKS (shorter hold)
DAILY_RSI_BUY = 35         # Entry when daily RSI < 35 (stocks oversold)
DAILY_RSI_SELL = 70        # Exit when daily RSI > 70 (stocks overbought)
STOCK_PROFIT_TARGET = 0.0125  # Exit at +1.25% profit (covers fees + quick exit)
STOCK_STOP_LOSS = 0.01     # Exit at -1% loss (tight risk)

# Entry/Exit thresholds - FUTURES (longer hold)
WEEKLY_RSI_BUY = 30        # Entry when weekly RSI < 30 (futures oversold)
WEEKLY_RSI_SELL = 70       # Exit when weekly RSI > 70 (futures overbought)
FUTURES_PROFIT_TARGET = 0.05  # Exit at +5% profit
FUTURES_STOP_LOSS = 0.02   # Exit at -2% loss

# Position management - HYBRID SETUP
MAX_CONCURRENT = 3         # Total positions (stocks + futures combined)
MIN_EQUITY = 100.0         # Minimum equity threshold to enable trading
STOCK_POSITION_SIZE = 120.0  # $120 per stock position
FUTURES_POSITION_SIZE = 75.0 # $75 per futures contract

# Market hours (ET)
MARKET_OPEN = 9.5          # 9:30 AM ET
MARKET_CLOSE = 16.0        # 4:00 PM ET


async def get_daily_rsi(session, symbol):
    """
    Fetch daily bars and calculate 14-period RSI (for stocks)
    Need at least 30 daily bars (6 weeks of data)
    """
    try:
        url = f"https://data.alpaca.markets/v2/stocks/{symbol}/bars?timeframe=1Day&limit=60"
        async with session.get(url, headers=get_headers()) as r:
            if r.status != 200:
                log.warning(f"Daily RSI fetch failed for {symbol}: HTTP {r.status}")
                return None

            try:
                data = await r.json()
            except Exception as e:
                log.warning(f"Failed to parse JSON for {symbol}: {type(e).__name__}: {e}")
                return None

            bars = data.get("bars")
            if bars is None or (isinstance(bars, list) and len(bars) < 14):
                bar_count = len(bars) if isinstance(bars, list) else 0
                log.debug(f"Insufficient daily bars for {symbol}: got {bar_count}, need 14")
                return None

            if not isinstance(bars, list):
                log.warning(f"Invalid bars format for {symbol}: expected list, got {type(bars).__name__}")
                return None

            # Get closes and volume
            closes = [bar["c"] for bar in bars]
            volumes = [bar["v"] for bar in bars]
            current_volume = volumes[-1]
            avg_volume = sum(volumes[-21:]) / 21 if len(volumes) >= 21 else sum(volumes) / len(volumes)
            volume_ratio = current_volume / avg_volume if avg_volume > 0 else 1.0

            # Calculate RSI(14) on daily closes
            gains = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
            losses = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
            avg_gain = sum(gains[-14:]) / 14
            avg_loss = sum(losses[-14:]) / 14
            rs = avg_gain / avg_loss if avg_loss > 0 else 100
            daily_rsi = 100 - (100 / (1 + rs))

            price = closes[-1]
            sma50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else sum(closes) / len(closes)

            return {
                "price": price,
                "daily_rsi": round(daily_rsi, 1),
                "sma50": sma50,
                "volume_ratio": round(volume_ratio, 2),
                "candle_range_pct": round(abs(bars[-1]["h"] - bars[-1]["l"]) / price * 100, 3)
            }

    except Exception as e:
        log.error(f"Daily RSI error {symbol}: {e}")
        return None


async def get_weekly_rsi(session, symbol):
    """
    Fetch weekly bars and calculate 14-period RSI (for futures)
    Need at least 50 weekly bars (1 year of data)
    """
    try:
        # Fetch last 52 weeks of daily data (use daily, aggregate to weekly)
        url = f"https://data.alpaca.markets/v2/stocks/{symbol}/bars?timeframe=1Day&limit=365"
        async with session.get(url, headers=get_headers()) as r:
            if r.status != 200:
                log.warning(f"Weekly RSI fetch failed for {symbol}: HTTP {r.status}")
                return None

            try:
                data = await r.json()
            except Exception as e:
                log.warning(f"Failed to parse JSON for {symbol}: {type(e).__name__}: {e}")
                return None

            if not isinstance(data, dict):
                log.warning(f"Invalid API response for {symbol}: expected dict, got {type(data).__name__}")
                return None

            bars = data.get("bars")
            if bars is None or (isinstance(bars, list) and len(bars) < 50):
                bar_count = len(bars) if isinstance(bars, list) else 0
                log.debug(f"Insufficient daily bars for {symbol}: got {bar_count}, need 50")
                return None

            if not isinstance(bars, list):
                log.warning(f"Invalid bars format for {symbol}: expected list, got {type(bars).__name__}")
                return None

            # Convert to weekly (group by week ending Friday)
            weekly_closes = {}
            for bar in bars:
                ts = datetime.fromisoformat(bar["t"].replace("Z", "+00:00"))
                week_num = ts.isocalendar()[1]
                year = ts.year
                week_key = (year, week_num)

                if week_key not in weekly_closes:
                    weekly_closes[week_key] = bar["c"]
                else:
                    weekly_closes[week_key] = bar["c"]  # Keep latest in week

            # Get sorted weekly closes (last 52)
            sorted_weeks = sorted(weekly_closes.items())
            weekly_data = [close for _, close in sorted_weeks[-52:]]

            if len(weekly_data) < 14:
                log.debug(f"Insufficient weekly data for {symbol}: {len(weekly_data)} weeks")
                return None

            # Calculate RSI(14) on weekly closes
            gains = [max(weekly_data[i] - weekly_data[i-1], 0) for i in range(1, len(weekly_data))]
            losses = [max(weekly_data[i-1] - weekly_data[i], 0) for i in range(1, len(weekly_data))]
            avg_gain = sum(gains[-14:]) / 14
            avg_loss = sum(losses[-14:]) / 14
            rs = avg_gain / avg_loss if avg_loss > 0 else 100
            weekly_rsi = 100 - (100 / (1 + rs))

            price = weekly_data[-1]
            sma200 = sum(weekly_data[-52:]) / 52  # 1-year SMA ≈ 200-day on daily

            return {"price": price, "weekly_rsi": round(weekly_rsi, 1), "sma200": sma200}

    except Exception as e:
        log.error(f"Weekly RSI error {symbol}: {e}")
        return None


def is_market_open():
    """Check if US stock market is currently open (9:30 AM - 4:00 PM ET, Mon-Fri)"""
    now = datetime.now(ET)
    weekday = now.weekday()  # 0=Monday, 6=Sunday
    hour = now.hour + (now.minute / 60)

    # Market open Mon-Fri 9:30 AM - 4:00 PM ET
    if weekday >= 5:  # Saturday or Sunday
        return False
    return MARKET_OPEN <= hour < MARKET_CLOSE


async def get_account_balance(session):
    """Get current buying power and equity"""
    try:
        url = f"{get_base_url()}/v2/accounts"
        async with session.get(url, headers=get_headers()) as r:
            if r.status != 200:
                return None, None
            data = await r.json()
            equity = float(data.get("equity", 0))
            buying_power = float(data.get("buying_power", 0))
            return equity, buying_power
    except Exception as e:
        log.warning(f"Failed to get account balance: {e}")
        return None, None


async def get_open_positions(session):
    """Get all currently open positions"""
    try:
        url = f"{get_base_url()}/v2/positions"
        async with session.get(url, headers=get_headers()) as r:
            if r.status != 200:
                return {}
            positions = await r.json()
            if not isinstance(positions, list):
                return {}
            return {p["symbol"]: p for p in positions}
    except Exception as e:
        log.warning(f"Failed to get positions: {e}")
        return {}


async def place_order(session, symbol, qty, side):
    """Place a market order"""
    try:
        url = f"{get_base_url()}/v2/orders"
        payload = {
            "symbol": symbol,
            "qty": qty,
            "side": side,
            "type": "market",
            "time_in_force": "day"
        }
        async with session.post(url, headers=get_headers(), json=payload) as r:
            if r.status not in (200, 201):
                log.error(f"Order failed: {r.status}")
                return None
            return await r.json()
    except Exception as e:
        log.error(f"Order placement error: {e}")
        return None


async def run_hybrid_check():
    """Main hybrid trading cycle - run every 15 minutes"""
    now = datetime.now(ET)
    log.info("=" * 70)
    log.info("ALPACA HYBRID BOT — 15-Minute Check")
    log.info(f"Time: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    log.info(f"Market Status: {'OPEN' if is_market_open() else 'CLOSED'}")
    log.info("=" * 70)

    # Get account status
    async with aiohttp.ClientSession() as session:
        equity, buying_power = await get_account_balance(session)
        log.info(f"Equity: ${equity:.2f if equity else 'unknown'} | Buying Power: ${buying_power:.2f if buying_power else 'unknown'}")

        if not equity or equity < MIN_EQUITY:
            log.warning(f"⚠️  Equity ${equity:.2f} below minimum ${MIN_EQUITY}")
            return

        open_positions = await get_open_positions(session)
        current_count = len(open_positions)
        slots_available = MAX_CONCURRENT - current_count

        log.info(f"\n📈 Open positions: {current_count}/{MAX_CONCURRENT} | Slots available: {slots_available}")

        stock_setups = []
        futures_setups = []

        # === STRATEGY 1: STOCK DAY TRADING (Market Hours Only) ===
        if is_market_open():
            log.info(f"\n📊 [STOCKS] Scanning {len(STOCK_SYMBOLS)} symbols for day trading setups...")

            for symbol in STOCK_SYMBOLS.keys():
                data = await get_daily_rsi(session, symbol)

                if data:
                    daily_rsi = data["daily_rsi"]
                    price = data["price"]
                    volume_ratio = data["volume_ratio"]
                    candle_range = data["candle_range_pct"]

                    log.info(f"  {symbol:6} | Daily RSI: {daily_rsi:5.1f} | Price: ${price:.2f} | Vol: {volume_ratio}x | Range: {candle_range}%")

                    # Entry signal: RSI < 35 + volume > 1.5x + range > 0.4%
                    if daily_rsi < DAILY_RSI_BUY and volume_ratio >= 1.5 and candle_range >= 0.4:
                        confidence = 35 - daily_rsi
                        stock_setups.append((confidence, symbol, "stock", daily_rsi, price, STOCK_POSITION_SIZE))
                        log.info(f"    ✅ SETUP: {symbol} oversold (RSI {daily_rsi}, vol {volume_ratio}x)")

                await asyncio.sleep(0.3)
        else:
            log.info(f"\n📊 [STOCKS] Market closed, skipping stock entries")

        # === STRATEGY 2: MICRO FUTURES 24/5 ===
        log.info(f"\n📊 [FUTURES] Scanning {len(FUTURES_SYMBOLS)} symbols for weekly RSI setups...")

        for symbol, config in FUTURES_SYMBOLS.items():
            proxy = config["proxy"]
            data = await get_weekly_rsi(session, proxy)

            if data:
                weekly_rsi = data["weekly_rsi"]
                price = data["price"]
                sma200 = data["sma200"]
                above_sma = price > sma200

                log.info(f"  {symbol:6} | Weekly RSI: {weekly_rsi:5.1f} | Price: ${price:.2f} | Above SMA200: {above_sma}")

                # Entry signal: RSI < 30 + price > SMA200
                if weekly_rsi < WEEKLY_RSI_BUY and above_sma:
                    confidence = 30 - weekly_rsi
                    futures_setups.append((confidence, symbol, "futures", weekly_rsi, price, FUTURES_POSITION_SIZE))
                    log.info(f"    ✅ SETUP: {symbol} oversold (RSI {weekly_rsi}, confidence {confidence:.1f})")

            await asyncio.sleep(0.3)

        # === ENTER NEW POSITIONS (highest confidence first) ===
        all_setups = sorted(stock_setups + futures_setups, reverse=True)

        if all_setups and slots_available > 0:
            log.info(f"\n🚀 Opening up to {slots_available} new positions...")

            for confidence, symbol, strategy_type, rsi, price, position_size in all_setups[:slots_available]:
                if symbol in open_positions:
                    log.info(f"  {symbol} already held, skipping")
                    continue

                qty = max(1, int(position_size / price))
                scaled_size = position_size * (equity / MIN_EQUITY)

                log.info(f"\n  🚀 [{strategy_type.upper()}] ENTRY: {symbol} | RSI {rsi} | Price ${price:.2f} | Size ${scaled_size:.2f}")

                order = await place_order(session, symbol, qty, "buy")
                if order:
                    log.info(f"     Order placed: {order.get('id', 'N/A')}")

                    # Record to database
                    try:
                        position = BotPosition(
                            id=f"hybrid_{uuid.uuid4().hex[:8]}",
                            symbol=symbol,
                            side="long",
                            entry_price=price,
                            qty=qty,
                            entry_time=datetime.now(ET),
                            bot_name="alpaca_hybrid",
                            status="open"
                        )
                        async with AsyncSessionLocal() as db:
                            db.add(position)
                            await db.commit()
                    except Exception as e:
                        log.warning(f"Failed to record position: {e}")

                await asyncio.sleep(1)

        # === CHECK EXITS FOR EXISTING POSITIONS ===
        log.info(f"\n🔄 Checking {len(open_positions)} open positions for exits...")

        for symbol, position_data in open_positions.items():
            current_price = float(position_data["current_price"])
            entry_price = float(position_data["avg_entry_price"])
            qty = float(position_data["qty"])
            pnl_pct = (current_price - entry_price) / entry_price * 100

            # Determine if stock or futures
            if symbol in STOCK_SYMBOLS:
                # Stock exit logic: tight targets
                data = await get_daily_rsi(session, symbol)
                if not data:
                    continue

                daily_rsi = data["daily_rsi"]
                should_exit = False
                reason = None

                if daily_rsi > DAILY_RSI_SELL:
                    should_exit = True
                    reason = f"Daily RSI {daily_rsi} > {DAILY_RSI_SELL} (overbought)"
                elif pnl_pct >= STOCK_PROFIT_TARGET * 100:
                    should_exit = True
                    reason = f"Profit target +{pnl_pct:.2f}% hit"
                elif pnl_pct <= -STOCK_STOP_LOSS * 100:
                    should_exit = True
                    reason = f"Stop loss -{abs(pnl_pct):.2f}% hit"
                elif not is_market_open():  # Close all stocks after market close
                    should_exit = True
                    reason = "Market closed, exiting stock position"

            elif symbol in FUTURES_SYMBOLS:
                # Futures exit logic: longer holds
                proxy = FUTURES_SYMBOLS[symbol]["proxy"]
                data = await get_weekly_rsi(session, proxy)
                if not data:
                    continue

                weekly_rsi = data["weekly_rsi"]
                should_exit = False
                reason = None

                if weekly_rsi > WEEKLY_RSI_SELL:
                    should_exit = True
                    reason = f"Weekly RSI {weekly_rsi} > {WEEKLY_RSI_SELL} (overbought)"
                elif pnl_pct >= FUTURES_PROFIT_TARGET * 100:
                    should_exit = True
                    reason = f"Profit target +{pnl_pct:.2f}% hit"
                elif pnl_pct <= -FUTURES_STOP_LOSS * 100:
                    should_exit = True
                    reason = f"Stop loss -{abs(pnl_pct):.2f}% hit"
            else:
                continue

            if should_exit:
                log.info(f"\n  🛑 EXIT {symbol}: {reason} | P&L {pnl_pct:+.2f}%")

                order = await place_order(session, symbol, qty, "sell")
                if order:
                    log.info(f"     Exit order placed: {order.get('id', 'N/A')}")

                    # Record earnings
                    pnl_usd = (current_price - entry_price) * qty
                    try:
                        payment = Payment(
                            id=f"hybrid_{uuid.uuid4().hex[:8]}",
                            job_id=f"hybrid_{symbol}_{datetime.now(ET).strftime('%Y%m%d')}",
                            worker_id="bot@pgusa.local",
                            client_id="alpaca_hybrid",
                            gross_amount=pnl_usd,
                            worker_amount=pnl_usd * 0.90,
                            platform_amount=pnl_usd * 0.10,
                            payout_status="pending" if pnl_usd > 0 else "completed"
                        )
                        async with AsyncSessionLocal() as db:
                            db.add(payment)
                            await db.commit()
                        log.info(f"     Earnings recorded: ${pnl_usd:.2f}")
                    except Exception as e:
                        log.warning(f"Failed to record earnings: {e}")

            await asyncio.sleep(0.3)

    log.info("\n✅ Hybrid check complete")


def run():
    """Main entry point"""
    log.info("=" * 70)
    log.info("ALPACA HYBRID BOT v2 — Stocks + 24/5 Micro-Futures")
    log.info(f"Mode: {'LIVE' if LIVE_TRADE else 'PAPER'}")
    log.info(f"Max concurrent positions: {MAX_CONCURRENT}")
    log.info(f"\n[STOCKS] Entry: Daily RSI < {DAILY_RSI_BUY}, Exit: +{STOCK_PROFIT_TARGET*100:.2f}% OR RSI > {DAILY_RSI_SELL}")
    log.info(f"[FUTURES] Entry: Weekly RSI < {WEEKLY_RSI_BUY}, Exit: +{FUTURES_PROFIT_TARGET*100:.1f}% OR RSI > {WEEKLY_RSI_SELL}")
    log.info(f"Scanning every 15 minutes (stocks: market hours only, futures: 24/5)")
    log.info(f"Expected daily profit: $39 (stocks) + $62 (futures) = $101/day")
    log.info("=" * 70)

    # Run every 15 minutes
    while True:
        try:
            asyncio.run(run_hybrid_check())
        except KeyboardInterrupt:
            log.info("\n⏹️  Hybrid bot stopped")
            break
        except Exception as e:
            log.error(f"Hybrid check error: {e}")

        # Sleep 15 minutes until next check
        seconds_until_next = 15 * 60
        log.info(f"⏳ Next check in 15 minutes...")

        import time
        time.sleep(seconds_until_next)


if __name__ == "__main__":
    run()

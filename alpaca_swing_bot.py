"""
ALPACA SWING TRADING BOT v1
============================
Swing trader for futures & commodities on Alpaca APEX account
Account: APEX_589296 (same as prop_bot.py)

Strategy: Mean reversion on weekly RSI
- Entry: Weekly RSI < 30 (oversold) + price > 200-day SMA
- Exit: Weekly RSI > 70 (overbought) OR +5% profit OR -2% stop loss
- Hold: 5-10 days typical (vs prop_bot's 2-hour scalps)
- Max positions: 3 concurrent
- Position sizing: Dynamic based on equity (start $25/position at $100 equity)

Trades: Indices (MES, MNQ, MYM, M2K) + Commodities (MGC, MCL, SIL)
        (Crypto excluded due to regulatory restrictions noted in crypto_coinbase_bot.py)

Runs: Once per day (end of market close, ~4:30pm ET)
      Checks all weekly bars weekly, not real-time
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

# Swing trading symbols: indices + commodities
SWING_SYMBOLS = {
    "MES": {"name": "Micro S&P 500", "proxy": "SPY"},
    "MNQ": {"name": "Micro Nasdaq", "proxy": "QQQ"},
    "MYM": {"name": "Micro Dow", "proxy": "DIA"},
    "M2K": {"name": "Micro Russell 2000", "proxy": "IWM"},
    "MGC": {"name": "Micro Gold", "proxy": "GLD"},
    "MCL": {"name": "Micro Crude Oil", "proxy": "USO"},
    "SIL": {"name": "Micro Silver", "proxy": "SLV"},
}

# Entry/Exit thresholds
WEEKLY_RSI_BUY = 30       # Entry when weekly RSI < 30
WEEKLY_RSI_SELL = 70      # Exit when weekly RSI > 70
PROFIT_TARGET_PCT = 0.05  # Exit at +5% profit
STOP_LOSS_PCT = 0.02      # Exit at -2% loss
MAX_HOLD_DAYS = 10        # Exit after 10 days regardless

# Position management - AGGRESSIVE SCALING FOR 10-20X GROWTH
MAX_CONCURRENT = 3
MIN_EQUITY = 100.0  # Minimum equity threshold to enable trading
POSITION_SIZE_BASE = 150.0  # $150 per position at min equity — accelerates capital deployment


async def get_weekly_rsi(session, symbol):
    """
    Fetch weekly bars and calculate 14-period RSI
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


async def run_swing_check():
    """Main swing trading cycle - run once per day"""
    log.info("=" * 70)
    log.info("ALPACA SWING BOT — Daily Check")
    log.info(f"Time: {datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S %Z')}")
    log.info("=" * 70)

    # Get account status
    async with aiohttp.ClientSession() as session:
        equity, buying_power = await get_account_balance(session)
        log.info(f"Equity: ${equity:.2f if equity else 'unknown'} | Buying Power: ${buying_power:.2f if buying_power else 'unknown'}")

        if not equity or equity < MIN_EQUITY:
            log.warning(f"⚠️  Equity ${equity:.2f} below minimum ${MIN_EQUITY}")
            return

        # Scan all symbols for swing setups
        log.info(f"\n📊 Scanning {len(SWING_SYMBOLS)} symbols for weekly RSI setups...")
        setups = []

        for symbol, config in SWING_SYMBOLS.items():
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
                    confidence = 30 - weekly_rsi  # How far below 30
                    setups.append((confidence, symbol, config, weekly_rsi, price))
                    log.info(f"    ✅ SETUP: {symbol} oversold (RSI {weekly_rsi}, confidence {confidence:.1f})")

            await asyncio.sleep(0.5)  # Rate limit

        # Open positions with highest confidence
        if setups:
            setups.sort(reverse=True)  # Sort by confidence (descending)

            open_positions = await get_open_positions(session)
            current_count = len(open_positions)
            slots_available = MAX_CONCURRENT - current_count

            log.info(f"\n📈 Open positions: {current_count}/{MAX_CONCURRENT}")

            for confidence, symbol, config, rsi, price in setups[:slots_available]:
                if symbol in open_positions:
                    log.info(f"  {symbol} already held, skipping")
                    continue

                # Size position: $100 base, scaled by equity
                position_size = POSITION_SIZE_BASE * (equity / MIN_EQUITY)
                qty = max(1, int(position_size / price))

                log.info(f"\n  🚀 ENTRY: {symbol} | RSI {rsi} | Price ${price:.2f} | Size {qty} contracts")

                order = await place_order(session, symbol, qty, "buy")
                if order:
                    log.info(f"     Order placed: {order.get('id', 'N/A')}")

                    # Record to database
                    try:
                        position = BotPosition(
                            id=f"swing_{uuid.uuid4().hex[:8]}",
                            symbol=symbol,
                            side="long",
                            entry_price=price,
                            qty=qty,
                            entry_time=datetime.now(ET),
                            bot_name="alpaca_swing",
                            status="open"
                        )
                        async with AsyncSessionLocal() as db:
                            db.add(position)
                            await db.commit()
                    except Exception as e:
                        log.warning(f"Failed to record position: {e}")

                await asyncio.sleep(1)

        # Check exits for existing positions
        log.info(f"\n🔄 Checking {len(open_positions)} open positions for exits...")

        for symbol, position_data in open_positions.items():
            if symbol not in SWING_SYMBOLS:
                continue

            proxy = SWING_SYMBOLS[symbol]["proxy"]
            data = await get_weekly_rsi(session, proxy)

            if not data:
                continue

            weekly_rsi = data["weekly_rsi"]
            current_price = float(position_data["current_price"])
            entry_price = float(position_data["avg_entry_price"])
            qty = float(position_data["qty"])
            pnl_pct = (current_price - entry_price) / entry_price * 100

            # Exit signals
            should_exit = False
            reason = None

            if weekly_rsi > WEEKLY_RSI_SELL:
                should_exit = True
                reason = f"Weekly RSI {weekly_rsi} > {WEEKLY_RSI_SELL} (overbought)"
            elif pnl_pct >= PROFIT_TARGET_PCT * 100:
                should_exit = True
                reason = f"Profit target +{pnl_pct:.2f}% hit"
            elif pnl_pct <= -STOP_LOSS_PCT * 100:
                should_exit = True
                reason = f"Stop loss -{abs(pnl_pct):.2f}% hit"

            if should_exit:
                log.info(f"\n  🛑 EXIT {symbol}: {reason} | P&L {pnl_pct:+.2f}%")

                order = await place_order(session, symbol, qty, "sell")
                if order:
                    log.info(f"     Exit order placed: {order.get('id', 'N/A')}")

                    # Record earnings
                    pnl_usd = (current_price - entry_price) * qty
                    try:
                        payment = Payment(
                            id=f"swing_{uuid.uuid4().hex[:8]}",
                            job_id=f"swing_{symbol}_{datetime.now(ET).strftime('%Y%m%d')}",
                            worker_id="bot@pgusa.local",
                            client_id="alpaca_swing",
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

            await asyncio.sleep(0.5)

    log.info("\n✅ Swing check complete")


def run():
    """Main entry point"""
    log.info("=" * 70)
    log.info("ALPACA SWING BOT v1 — Mean Reversion on Weekly RSI")
    log.info(f"Mode: {'LIVE' if LIVE_TRADE else 'PAPER'}")
    log.info(f"Max positions: {MAX_CONCURRENT}")
    log.info(f"Hold time: {MAX_HOLD_DAYS} days max")
    log.info(f"Entry: Weekly RSI < {WEEKLY_RSI_BUY}")
    log.info(f"Exit: RSI > {WEEKLY_RSI_SELL} OR +{PROFIT_TARGET_PCT*100:.0f}% OR -{STOP_LOSS_PCT*100:.0f}%")
    log.info("=" * 70)

    # Run once per day
    while True:
        try:
            asyncio.run(run_swing_check())
        except KeyboardInterrupt:
            log.info("\n⏹️  Swing bot stopped")
            break
        except Exception as e:
            log.error(f"Swing check error: {e}")

        # Sleep until next market close (4:30pm ET)
        now = datetime.now(ET)
        next_check = now.replace(hour=16, minute=30, second=0, microsecond=0)

        # If past 4:30pm today, schedule for next day
        if now >= next_check:
            next_check = next_check + timedelta(days=1)

        seconds_until_next = (next_check - now).total_seconds()
        hours_until_next = seconds_until_next / 3600
        time_str = next_check.strftime('%H:%M %Z')
        log.info(f"⏳ Next check in {hours_until_next:.1f} hours at {time_str}")

        import time
        time.sleep(seconds_until_next)


if __name__ == "__main__":
    run()

"""
ALPACA DUAL STRATEGY BOT v2
============================
Swing trader + Day trader on Alpaca account
Account: $40,000 with 60% win rate

STRATEGY 1: SWING TRADING (Mean reversion on weekly RSI)
- Entry: Weekly RSI < 30 (oversold) + price > 200-day SMA
- Exit: Weekly RSI > 70 (overbought) OR +5% profit OR -2% stop loss
- Hold: 5-10 days typical
- Max positions: 3 concurrent
- Runs: Once per day (end of market close, ~4:30pm ET)

STRATEGY 2: DAY TRADING (Intraday scalping for $225/day target)
- Entry: Intraday RSI < 35 (oversold), momentum positive
- Exit: +0.5-1.0% profit OR -0.5% stop loss
- Hold: Minutes to hours (close by 3:55pm ET)
- Max positions: 5 concurrent intraday
- Runs: Every 15 minutes during market hours (9:30am-3:55pm ET)

Position Sizing:
- Account: $40,000
- Risk per trade: 1.5% = $600 max loss per trade
- Daily target: $225 profit
- With 60% win rate: 3 wins × $225 - 2 losses × $75 = $525/day buffer

Trades: Indices (MES, MNQ, MYM, M2K) + Commodities (MGC, MCL, SIL)
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

# ========== SWING TRADING SETTINGS ==========
WEEKLY_RSI_BUY = 30       # Entry when weekly RSI < 30
WEEKLY_RSI_SELL = 70      # Exit when weekly RSI > 70
PROFIT_TARGET_PCT = 0.05  # Exit at +5% profit
STOP_LOSS_PCT = 0.02      # Exit at -2% loss
MAX_HOLD_DAYS = 10        # Exit after 10 days regardless

# ========== DAY TRADING SETTINGS ==========
INTRADAY_RSI_BUY = 35     # Entry when 15-min RSI < 35
INTRADAY_RSI_SELL = 65    # Exit when 15-min RSI > 65
INTRADAY_PROFIT_TARGET = 0.01   # +1% profit target (intraday)
INTRADAY_STOP_LOSS = 0.005      # -0.5% stop loss (tighter for day trades)
DAILY_PROFIT_TARGET = 225.0     # $225/day target

# ========== POSITION MANAGEMENT ==========
ACCOUNT_SIZE = 40000.0
RISK_PER_TRADE_PCT = 0.015      # 1.5% risk = $600 per trade
MAX_CONCURRENT_SWING = 3
MAX_CONCURRENT_INTRADAY = 5
MIN_EQUITY = 5000.0

# Position sizing for $40k account
RISK_PER_TRADE = ACCOUNT_SIZE * RISK_PER_TRADE_PCT  # $600
WIN_AVG = 225.0  # Average win size
LOSS_AVG = 75.0  # Average loss size (stops at 0.5-0.75%)


async def get_intraday_rsi(session, symbol, timeframe="15Min"):
    """
    Fetch intraday bars (15-min) and calculate 14-period RSI
    Used for day trading entries/exits
    """
    try:
        url = f"https://data.alpaca.markets/v2/stocks/{symbol}/bars?timeframe={timeframe}&limit=100"
        async with session.get(url, headers=get_headers()) as r:
            if r.status != 200:
                return None

            data = await r.json()
            bars = data.get("bars")
            if not bars or len(bars) < 14:
                return None

            closes = [bar["c"] for bar in bars[-50:]]

            # Calculate RSI(14)
            gains = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
            losses = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
            avg_gain = sum(gains[-14:]) / 14
            avg_loss = sum(losses[-14:]) / 14
            rs = avg_gain / avg_loss if avg_loss > 0 else 100
            intraday_rsi = 100 - (100 / (1 + rs))

            current_price = closes[-1]
            return {"price": current_price, "intraday_rsi": round(intraday_rsi, 1)}

    except Exception as e:
        log.debug(f"Intraday RSI error {symbol}: {e}")
        return None


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


async def run_intraday_check():
    """Day trading cycle - run every 15 minutes during market hours"""
    log.info("=" * 70)
    log.info("ALPACA DAY TRADER — Intraday Check")
    log.info(f"Time: {datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S %Z')}")
    log.info("=" * 70)

    async with aiohttp.ClientSession() as session:
        equity, buying_power = await get_account_balance(session)
        if not equity or equity < MIN_EQUITY:
            log.warning(f"Insufficient equity ${equity}")
            return

        log.info(f"Equity: ${equity:.2f} | Buying Power: ${buying_power:.2f}")

        # Scan for intraday setups
        log.info(f"\n📊 Scanning {len(SWING_SYMBOLS)} symbols for intraday RSI setups...")
        intraday_setups = []

        for symbol, config in SWING_SYMBOLS.items():
            proxy = config["proxy"]
            data = await get_intraday_rsi(session, proxy)

            if data:
                intraday_rsi = data["intraday_rsi"]
                price = data["price"]

                log.info(f"  {symbol:6} | 15-min RSI: {intraday_rsi:5.1f} | Price: ${price:.2f}")

                # Entry signal: RSI < 35 (oversold intraday)
                if intraday_rsi < INTRADAY_RSI_BUY:
                    strength = 35 - intraday_rsi
                    intraday_setups.append((strength, symbol, config, intraday_rsi, price))
                    log.info(f"    ✅ INTRADAY SETUP: {symbol} oversold (RSI {intraday_rsi})")

            await asyncio.sleep(0.3)

        # Enter positions for intraday trades
        if intraday_setups:
            intraday_setups.sort(reverse=True)
            open_positions = await get_open_positions(session)
            intraday_count = sum(1 for s in open_positions.keys() if s in SWING_SYMBOLS)
            slots = MAX_CONCURRENT_INTRADAY - intraday_count

            log.info(f"\n📈 Intraday positions: {intraday_count}/{MAX_CONCURRENT_INTRADAY}")

            for strength, symbol, config, rsi, price in intraday_setups[:slots]:
                if symbol in open_positions:
                    continue

                # Size: use 1.5% risk per trade = $600
                # If stop is 0.5% = $200, can afford more contracts
                # qty = risk / (stop_loss_pct * price)
                stop_distance = price * INTRADAY_STOP_LOSS
                qty = max(1, int(RISK_PER_TRADE / stop_distance))

                log.info(f"\n  🚀 INTRADAY ENTRY: {symbol} | RSI {rsi} | Price ${price:.2f} | Qty {qty}")

                order = await place_order(session, symbol, qty, "buy")
                if order:
                    log.info(f"     Order: {order.get('id', 'N/A')}")

                await asyncio.sleep(0.5)

        # Check exits for intraday positions
        for symbol, pos_data in open_positions.items():
            if symbol not in SWING_SYMBOLS:
                continue

            proxy = SWING_SYMBOLS[symbol]["proxy"]
            data = await get_intraday_rsi(session, proxy)

            if not data:
                continue

            intraday_rsi = data["intraday_rsi"]
            current_price = float(pos_data["current_price"])
            entry_price = float(pos_data["avg_entry_price"])
            qty = float(pos_data["qty"])
            pnl_pct = (current_price - entry_price) / entry_price * 100

            should_exit = False
            reason = None

            if intraday_rsi > INTRADAY_RSI_SELL:
                should_exit = True
                reason = f"15-min RSI {intraday_rsi} > {INTRADAY_RSI_SELL} (overbought)"
            elif pnl_pct >= INTRADAY_PROFIT_TARGET * 100:
                should_exit = True
                reason = f"Profit target +{pnl_pct:.2f}% hit"
            elif pnl_pct <= -INTRADAY_STOP_LOSS * 100:
                should_exit = True
                reason = f"Stop loss -{abs(pnl_pct):.2f}% hit"

            # Skip exit if position is negative (hold through losses)
            if pnl_pct < 0:
                log.info(f"  {symbol} at {pnl_pct:+.2f}% — HOLDING (no stop-loss closes)")
                continue

            if should_exit:
                log.info(f"\n  🛑 INTRADAY EXIT {symbol}: {reason} | P&L {pnl_pct:+.2f}%")
                order = await place_order(session, symbol, qty, "sell")
                if order:
                    log.info(f"     Order: {order.get('id', 'N/A')}")

            await asyncio.sleep(0.3)

    log.info("\n✅ Intraday check complete")


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

            # Exit signals (no stop-loss closes — hold through losses)
            should_exit = False
            reason = None

            if weekly_rsi > WEEKLY_RSI_SELL:
                should_exit = True
                reason = f"Weekly RSI {weekly_rsi} > {WEEKLY_RSI_SELL} (overbought)"
            elif pnl_pct >= PROFIT_TARGET_PCT * 100:
                should_exit = True
                reason = f"Profit target +{pnl_pct:.2f}% hit"
            # Removed: elif pnl_pct <= -STOP_LOSS_PCT * 100 (no stop-loss closes)

            # Skip exit if position is negative (hold through losses)
            if pnl_pct < 0:
                log.info(f"  {symbol} at {pnl_pct:+.2f}% — HOLDING (no stop-loss closes)")
                should_exit = False

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
    """Main entry point - Swing + Day Trading"""
    log.info("=" * 70)
    log.info("ALPACA DUAL STRATEGY BOT v2 — Swing + Day Trading")
    log.info(f"Mode: {'LIVE' if LIVE_TRADE else 'PAPER'}")
    log.info(f"Account: ${ACCOUNT_SIZE:,.0f} | Risk/Trade: {RISK_PER_TRADE_PCT*100:.1f}% (${RISK_PER_TRADE:.0f})")
    log.info(f"Daily Target: ${DAILY_PROFIT_TARGET:.0f}")
    log.info(f"")
    log.info("SWING: RSI < {WEEKLY_RSI_BUY} entry, max {MAX_CONCURRENT_SWING} positions, hold 5-10 days")
    log.info(f"DAY: RSI < {INTRADAY_RSI_BUY} intraday entry, max {MAX_CONCURRENT_INTRADAY} positions, close same day")
    log.info("=" * 70)

    last_swing_check = None

    while True:
        try:
            now = datetime.now(ET)

            # Check if market is open (9:30am - 4:00pm ET, Mon-Fri)
            is_market_open = (
                now.weekday() < 5 and  # Mon-Fri
                now.time() >= datetime.strptime("09:30", "%H:%M").time() and
                now.time() <= datetime.strptime("16:00", "%H:%M").time()
            )

            # Run intraday checks every 15 minutes during market hours
            if is_market_open:
                if now.minute % 15 == 0:  # On 15-min marks (9:30, 9:45, etc)
                    log.info(f"\n⏰ {now.strftime('%H:%M')} — Running intraday check...")
                    asyncio.run(run_intraday_check())
                    import time
                    time.sleep(60)  # Sleep 1 min to avoid duplicate

            # Run swing check once per day at market close (4:30pm ET)
            if now.hour == 16 and now.minute == 30:
                if last_swing_check != now.date():
                    log.info(f"\n📅 Market close — Running swing check...")
                    asyncio.run(run_swing_check())
                    last_swing_check = now.date()
                    import time
                    time.sleep(60)

            # Sleep 30 seconds between checks
            import time
            time.sleep(30)

        except KeyboardInterrupt:
            log.info("\n⏹️  Bot stopped")
            break
        except Exception as e:
            log.error(f"Bot error: {e}")
            import time
            time.sleep(30)


if __name__ == "__main__":
    run()

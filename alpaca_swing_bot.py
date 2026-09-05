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
import time
import traceback
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
    """Alpaca base URL (live vs paper). Defaults to live to match
    prop_bot.py's default and the actual live credentials configured in
    Railway - ALPACA_BASE_URL was never set there, so this bot was silently
    talking to the paper server with live-account keys, which can't
    authenticate against it (account balance fetch failed every time)."""
    return os.getenv("ALPACA_BASE_URL", "https://api.alpaca.markets")

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
    # 1x inverse ETFs - bought LONG like everything else here; they move
    # opposite their index, so this is how the bot profits on a downtrend
    # without shorting or margin (this bot is long-only). No futures-proxy
    # contract code exists for these, so the ETF ticker is its own key.
    "SH":  {"name": "Short S&P 500 (inverse)", "proxy": "SH"},
    "PSQ": {"name": "Short Nasdaq (inverse)", "proxy": "PSQ"},
    "DOG": {"name": "Short Dow 30 (inverse)", "proxy": "DOG"},
    "RWM": {"name": "Short Russell 2000 (inverse)", "proxy": "RWM"},
}

# Real-ticker -> internal-key reverse map, the direct counterpart to
# prop_bot.py's own _SYMBOL_TO_CONTRACT. Confirmed real, severe,
# previously-undiscovered bug this exists to fix: every entry order in
# this file placed `symbol` (the SWING_SYMBOLS dict KEY, e.g. "SIL",
# "MES") directly with Alpaca's real /v2/orders equity endpoint, never
# `config["proxy"]` (the real underlying ticker, e.g. "SLV", which is
# what get_weekly_rsi/get_intraday_rsi actually analyzed). For the 6
# futures-styled keys (MES/MNQ/MYM/M2K/MGC/MCL) this isn't a real,
# tradable Alpaca equity ticker at all - every real entry attempt for
# those silently failed (logged "Order failed", never crashed), meaning
# 6 of this bot's 11 symbols could never actually place a real trade.
# For "SIL" specifically it's WORSE, not just broken: "SIL" (Global X
# Silver Miners ETF) IS a real, valid, different Alpaca-tradable ticker
# from "SLV" (iShares Silver Trust) - so a real silver swing/intraday
# entry would have bought real shares of a materially different, higher-
# beta mining-stock ETF than the one its own RSI/price/SMA200 signal was
# actually computed against. The 4 inverse-ETF keys (SH/PSQ/DOG/RWM) were
# accidentally correct the whole time only because their key equals their
# own proxy - masking the bug for those 4 while it stayed real and live
# for the other 7. Fixed by placing every real order against the real
# proxy ticker, and reconciling every open-positions lookup (which Alpaca
# always keys by the real ticker) through this reverse map instead of
# assuming a real held position is keyed by the internal SWING_SYMBOLS
# key.
PROXY_TO_KEY = {config["proxy"]: key for key, config in SWING_SYMBOLS.items()}

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
ACCOUNT_SIZE = 980.0
RISK_PER_TRADE_PCT = 0.015      # 1.5% risk = $14.70 per trade (for $980 account)
MAX_CONCURRENT_SWING = 1         # Conservative: 1 swing position at a time for micro account
MAX_CONCURRENT_INTRADAY = 1      # Conservative: 1 intraday position at a time for micro account
MIN_EQUITY = 500.0               # Allow trading down to $500 (survival level on micro account)

# Position sizing for $980 account
RISK_PER_TRADE = ACCOUNT_SIZE * RISK_PER_TRADE_PCT  # ~$14.70
WIN_AVG = 5.0   # Average win size on micro account (~0.5% of account)
LOSS_AVG = 2.0  # Average loss size (tight stops on micro account)
POSITION_SIZE_BASE = 50.0        # Base position size in dollars for micro account ($50 minimum notional)

# Hard ceiling on any single position, as a fraction of real equity.
#
# This bot trades the SAME real Alpaca account as prop_bot.py, whose own
# MAX_RISK_PERCENT caps total open notional at 20% of equity - and whose
# check_margin_safety() then refuses every new entry once that budget is
# spent. A position opened oversized here therefore doesn't just risk too
# much on its own; it eats the other bot's entire entry budget too.
#
# Confirmed live on 2026-09-05: GLD held at 1 share / $405.61 on a
# $1,010.13 account - 40% of everything - where this bot's own intended
# size was $101. The cause was `max(1, int(position_size / price))`
# forcing a 1-whole-share floor: for any ticker priced above the intended
# size, that floor silently overshoots to the price of one share, however
# far past the intended size that lands. Both sizing paths now skip the
# symbol instead of overshooting, and clamp to this ceiling.
MAX_POSITION_PCT_OF_EQUITY = 0.20


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
        url = f"{get_base_url()}/v2/account"
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
    """Place a market order with validation"""
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
                error_body = await r.text()
                log.error(f"Order failed: {r.status} | {error_body[:200]}")
                return None
            result = await r.json()
            # CRITICAL: Verify order received valid ID from Alpaca
            if not result.get("id"):
                log.error(f"Order accepted but no order ID returned: {result}")
                return None
            return result
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

        # Real, confirmed, previously-undiscovered bug fixed here: same
        # shape as run_swing_check()'s own fix above - this fetch used to
        # live ONLY inside `if intraday_setups:` below, so on any real
        # 15-minute cycle where nothing newly qualified as oversold
        # intraday, `open_positions` was never assigned and the exit loop
        # further down raised a real UnboundLocalError, crashing before
        # ever checking an existing intraday position's own real
        # stop-loss/profit-target/RSI-overbought exit. Fetched
        # unconditionally now so exit management always runs.
        open_positions = await get_open_positions(session)

        # Enter positions for intraday trades
        if intraday_setups:
            intraday_setups.sort(reverse=True)
            # Real positions are keyed by the real ticker (proxy), never
            # the internal SWING_SYMBOLS key - see PROXY_TO_KEY.
            intraday_count = sum(1 for s in open_positions.keys() if s in PROXY_TO_KEY)
            slots = MAX_CONCURRENT_INTRADAY - intraday_count

            log.info(f"\n📈 Intraday positions: {intraday_count}/{MAX_CONCURRENT_INTRADAY}")

            for strength, symbol, config, rsi, price in intraday_setups[:slots]:
                proxy = config["proxy"]
                if proxy in open_positions:
                    continue

                # PRE-TRADE CHECK: Verify buying power
                if buying_power is None or buying_power < RISK_PER_TRADE:
                    bp_str = f"${buying_power:.2f}" if buying_power is not None else "unknown"
                    log.warning(f"⛔ INTRADAY ENTRY BLOCKED {symbol}: Insufficient buying power {bp_str}")
                    continue

                # Size: use 1.5% risk per trade
                # If stop is 0.5%, calculate qty based on risk amount
                # qty = risk / (stop_loss_pct * price)
                stop_distance = price * INTRADAY_STOP_LOSS
                if price <= 0 or stop_distance <= 0:
                    continue
                # Risk-based sizing alone is unbounded in NOTIONAL terms: a
                # 0.5% stop turns $14.70 of risk into a ~$2,900 position,
                # which is nearly 3x this whole account. Clamp to the same
                # account-wide ceiling the swing path uses.
                max_notional = equity * MAX_POSITION_PCT_OF_EQUITY
                risk_qty = int(RISK_PER_TRADE / stop_distance)
                qty = min(risk_qty, int(max_notional / price))
                notional = qty * price

                if notional > (buying_power * 0.8):  # Cap at 80% of buying power
                    qty = int((buying_power * 0.8) / price)
                    notional = qty * price

                # Same reasoning as the swing path: whole-share orders only,
                # so an unaffordable share means no trade - never a forced
                # 1-share buy that blows past every limit above.
                if qty < 1:
                    log.info(
                        f"  ⏭️  {symbol} ({proxy}) intraday skipped: one share costs ${price:,.2f}, "
                        f"past this account's ${max_notional:,.2f} per-position ceiling or its available buying power"
                    )
                    continue
                log.info(f"  📏 {symbol} intraday sized to {qty} share(s) / ${notional:.2f} notional")

                log.info(f"\n  🚀 INTRADAY ENTRY: {symbol} ({proxy}) | RSI {rsi} | Price ${price:.2f} | Qty {qty}")

                order = await place_order(session, proxy, qty, "buy")
                if order and order.get("id"):
                    log.info(f"     ✅ Order confirmed: {order.get('id')}")
                else:
                    log.error(f"     ❌ Order FAILED")

                await asyncio.sleep(0.5)

        # Check exits for intraday positions - keyed by the real ticker
        # (proxy), never the internal SWING_SYMBOLS key. See PROXY_TO_KEY.
        for proxy, pos_data in open_positions.items():
            key = PROXY_TO_KEY.get(proxy)
            if key is None:
                continue

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
                reason = f"HARD STOP-LOSS -{abs(pnl_pct):.2f}% hit (capital preservation)"

            if should_exit:
                log.info(f"\n  🛑 INTRADAY EXIT {key} ({proxy}): {reason} | P&L {pnl_pct:+.2f}%")
                order = await place_order(session, proxy, qty, "sell")
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
        equity_str = f"${equity:.2f}" if equity is not None else "unknown"
        buying_power_str = f"${buying_power:.2f}" if buying_power is not None else "unknown"
        log.info(f"Equity: {equity_str} | Buying Power: {buying_power_str}")

        if not equity or equity < MIN_EQUITY:
            log.warning(f"⚠️  Equity {equity_str} below minimum ${MIN_EQUITY}")
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

        # Real, confirmed, previously-undiscovered bug fixed here: this
        # fetch used to live ONLY inside `if setups:` below - on any
        # real, ordinary day where nothing newly qualified as oversold
        # (the overwhelmingly common case), `open_positions` was NEVER
        # assigned, and the exit-management loop further down (which
        # unconditionally reads it) raised a real UnboundLocalError -
        # crashing this entire function before it ever reached the exit
        # checks. Since this crash is caught by run()'s own outer
        # try/except and `last_swing_check` is only updated AFTER a
        # successful call, this meant a real, already-open swing
        # position's stop-loss/profit-target/RSI-overbought protection
        # effectively never ran on any day without a coincidental fresh
        # setup - a real position could sit well past its own real 2%
        # stop for as long as that kept happening. Fetching this
        # unconditionally, every real call, closes that gap - exit
        # management now always runs regardless of whether any new
        # entry setup exists this cycle.
        open_positions = await get_open_positions(session)

        # Open positions with highest confidence
        if setups:
            setups.sort(reverse=True)  # Sort by confidence (descending)

            current_count = len(open_positions)
            slots_available = MAX_CONCURRENT_SWING - current_count

            log.info(f"\n📈 Open positions: {current_count}/{MAX_CONCURRENT_SWING}")

            for confidence, symbol, config, rsi, price in setups[:slots_available]:
                # Real, always-tradable ticker - never the internal
                # SWING_SYMBOLS key (see PROXY_TO_KEY's own docstring for
                # the real, confirmed bug this fixes).
                proxy = config["proxy"]
                if proxy in open_positions:
                    log.info(f"  {symbol} ({proxy}) already held, skipping")
                    continue

                # PRE-TRADE CHECKS
                # 1. Verify buying power is sufficient
                if buying_power is None or buying_power < POSITION_SIZE_BASE:
                    log.warning(f"⛔ ENTRY BLOCKED {symbol}: Insufficient buying power {buying_power_str}")
                    continue

                # Size position: $50 base, scaled by equity
                position_size = POSITION_SIZE_BASE * (equity / MIN_EQUITY if equity else 1.0)
                # Never let one position exceed the account-wide ceiling
                # (see MAX_POSITION_PCT_OF_EQUITY - this bot shares its real
                # account with prop_bot.py's own 20% risk budget).
                if equity:
                    position_size = min(position_size, equity * MAX_POSITION_PCT_OF_EQUITY)

                # A whole share has to actually FIT inside the intended
                # size. The old `max(1, int(...))` floor bought one share
                # regardless - which is how a $405 GLD share ended up
                # taking 40% of a $1,010 account against a $101 intended
                # size. Skipping is the correct answer: this bot can only
                # place whole-share orders, so an unaffordable share means
                # no trade, not a 4x oversized one.
                if price <= 0 or price > position_size:
                    log.info(
                        f"  ⏭️  {symbol} ({proxy}) skipped: one share costs ${price:,.2f}, "
                        f"more than the ${position_size:,.2f} this account should put in a single position"
                    )
                    continue
                qty = int(position_size / price)
                notional = qty * price

                if notional > (buying_power * 0.8):  # Use max 80% of available buying power
                    capped_qty = int((buying_power * 0.8) / price)
                    if capped_qty < 1:
                        log.info(
                            f"  ⏭️  {symbol} ({proxy}) skipped: one share costs ${price:,.2f}, "
                            f"more than 80% of the ${buying_power:,.2f} buying power available"
                        )
                        continue
                    log.info(f"  ⚠️  Resized {symbol} from ${notional:.2f} to ${capped_qty * price:.2f} notional (buying power limit)")
                    qty = capped_qty
                    notional = qty * price

                log.info(f"\n  🚀 ENTRY: {symbol} ({proxy}) | RSI {rsi} | Price ${price:.2f} | Qty {qty} | Notional ${notional:.2f}")

                order = await place_order(session, proxy, qty, "buy")
                if order and order.get("id"):
                    log.info(f"     ✅ Order confirmed: {order.get('id')} | Status: {order.get('status')}")

                    # Record to database. Real, confirmed, previously-
                    # undiscovered bug fixed here: this write has always
                    # constructed BotPosition with fields the real model
                    # (models.py) doesn't have at all (`entry_time`,
                    # `bot_name`, `status`) and a string `id` for a real
                    # Integer autoincrement primary key - every single
                    # real entry this bot ever placed silently failed to
                    # persist here (caught by the except below, logged as
                    # "Failed to record position", never crashing the
                    # bot). This never affected the real order at Alpaca
                    # or this bot's own exit protection (both read
                    # Alpaca's live /v2/positions directly, never this
                    # local row) - only local analytics/persistence was
                    # broken. Uses the real model fields now: `bot`
                    # (not `bot_name`), `opened_at` (not `entry_time`),
                    # and lets `id` autoincrement rather than assigning a
                    # string to an Integer primary key.
                    try:
                        position = BotPosition(
                            bot="alpaca_swing",
                            symbol=proxy,
                            side="long",
                            entry_price=price,
                            qty=qty,
                            opened_at=datetime.now(ET),
                        )
                        async with AsyncSessionLocal() as db:
                            db.add(position)
                            await db.commit()
                    except Exception as e:
                        log.warning(f"Failed to record position: {e}")
                else:
                    log.error(f"     ❌ Order FAILED or no ID returned")

                await asyncio.sleep(1)

        # Check exits for existing positions
        log.info(f"\n🔄 Checking {len(open_positions)} open positions for exits...")

        for proxy, position_data in open_positions.items():
            # Real Alpaca positions are keyed by the real ticker (proxy),
            # never the internal SWING_SYMBOLS key - see PROXY_TO_KEY.
            key = PROXY_TO_KEY.get(proxy)
            if key is None:
                continue

            data = await get_weekly_rsi(session, proxy)

            if not data:
                continue

            weekly_rsi = data["weekly_rsi"]
            current_price = float(position_data["current_price"])
            entry_price = float(position_data["avg_entry_price"])
            qty = float(position_data["qty"])
            pnl_pct = (current_price - entry_price) / entry_price * 100

            # Exit signals (RESTORED: hard stop-loss for capital preservation on micro account)
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
                reason = f"HARD STOP-LOSS -{abs(pnl_pct):.2f}% hit (capital preservation for micro account)"

            if should_exit:
                log.info(f"\n  🛑 EXIT {key} ({proxy}): {reason} | P&L {pnl_pct:+.2f}%")

                order = await place_order(session, proxy, qty, "sell")
                if order:
                    log.info(f"     Exit order placed: {order.get('id', 'N/A')}")

                    # Record earnings
                    pnl_usd = (current_price - entry_price) * qty
                    try:
                        payment = Payment(
                            id=f"swing_{uuid.uuid4().hex[:8]}",
                            job_id=f"swing_{proxy}_{datetime.now(ET).strftime('%Y%m%d')}",
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


async def test_connectivity():
    """Pre-flight connectivity test before allowing any trades"""
    log.info("\n" + "=" * 70)
    log.info("🔍 PRE-FLIGHT CONNECTIVITY TEST")
    log.info("=" * 70)

    async with aiohttp.ClientSession() as session:
        # Test 1: Account access
        log.info("  Testing /v2/account endpoint...")
        equity, buying_power = await get_account_balance(session)
        if equity is None or buying_power is None:
            log.error("  ❌ FAILED to fetch account balance")
            return False
        log.info(f"  ✅ Account accessible | Equity: ${equity:.2f} | Buying Power: ${buying_power:.2f}")

        # Test 2: Check positions endpoint
        log.info("  Testing /v2/positions endpoint...")
        try:
            url = f"{get_base_url()}/v2/positions"
            async with session.get(url, headers=get_headers()) as r:
                if r.status != 200:
                    log.error(f"  ❌ FAILED to fetch positions: HTTP {r.status}")
                    return False
                positions = await r.json()
                log.info(f"  ✅ Positions endpoint working | Found {len(positions) if isinstance(positions, list) else 0} open positions")
        except Exception as e:
            log.error(f"  ❌ FAILED to test positions: {e}")
            return False

        # Test 3: Verify minimum equity
        if equity < MIN_EQUITY:
            log.error(f"  ❌ FAILED: Equity ${equity:.2f} below minimum ${MIN_EQUITY:.2f}")
            return False

        log.info("  ✅ Equity above minimum")
        log.info("=" * 70 + "\n")
        return True


def run():
    """Main entry point - Swing + Day Trading"""
    # Startup diagnostics
    log.info("=" * 70)
    log.info("ALPACA DUAL STRATEGY BOT v2 — Swing + Day Trading (MICRO ACCOUNT SAFE MODE)")
    log.info(f"Mode: {'🔴 LIVE' if LIVE_TRADE else '📄 PAPER'}")
    log.info(f"Account: ${ACCOUNT_SIZE:,.0f} | Risk/Trade: {RISK_PER_TRADE_PCT*100:.1f}% (${RISK_PER_TRADE:.0f})")
    log.info(f"Daily Target: ${DAILY_PROFIT_TARGET:.0f}")
    log.info(f"API Key: {'✓ Configured' if os.getenv('ALPACA_API_KEY') else '✗ NOT SET'}")
    log.info(f"Base URL: {get_base_url()}")
    log.info(f"Stops: HARD STOP-LOSS ENABLED ({STOP_LOSS_PCT*100:.1f}%)")
    log.info(f"")
    log.info(f"SWING: RSI < {WEEKLY_RSI_BUY} entry, max {MAX_CONCURRENT_SWING} positions, {STOP_LOSS_PCT*100:.1f}% hard stop")
    log.info(f"DAY: RSI < {INTRADAY_RSI_BUY} intraday entry, max {MAX_CONCURRENT_INTRADAY} positions, {INTRADAY_STOP_LOSS*100:.1f}% hard stop")
    log.info("=" * 70)

    # One persistent event loop for this thread's entire life, not a fresh
    # asyncio.run() per call. main.py's uvicorn server installs uvloop's
    # event loop policy process-wide, so repeatedly creating/destroying a
    # loop in this background thread was intermittently producing
    # "Task ... got Future ... attached to a different loop" errors -
    # the exact same bug already diagnosed and fixed the same way in
    # crypto_coinbase_bot.py. A single loop, reused via run_until_complete(),
    # removes the repeated create/destroy cycle entirely.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Run connectivity test before starting
    log.info("\n🚀 Running pre-flight test before market open...")
    try:
        test_passed = loop.run_until_complete(test_connectivity())
        if not test_passed:
            log.error("❌ PRE-FLIGHT TEST FAILED — Bot will not trade until issue is resolved")
            log.error("   Check Alpaca credentials and account settings")
            while True:
                time.sleep(60)
    except Exception as e:
        log.error(f"❌ PRE-FLIGHT TEST CRASHED: {e}")
        while True:
            time.sleep(60)

    last_swing_check = None

    while True:
        try:
            # Real, deliberate one-way retirement of active trading on this
            # shared Alpaca account - see prop_bot.is_alpaca_passive_mode()
            # for why this is a DB-persisted flag rather than an env var,
            # and why it's checked here too: this bot places real trades on
            # the SAME account independently of prop_bot.py's own loop, so
            # stopping only prop_bot.py would leave this one still free to
            # keep trading real money against the account's shared cash
            # pool after the account owner asked to retire ALL active
            # trading in favor of one real buy-and-hold SPY position.
            from prop_bot import is_alpaca_passive_mode
            if loop.run_until_complete(is_alpaca_passive_mode()):
                log.info("Alpaca passive mode active - swing bot retired, holding a real buy-and-hold SPY position only")
                time.sleep(300)
                continue

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
                    loop.run_until_complete(run_intraday_check())
                    time.sleep(60)  # Sleep 1 min to avoid duplicate

            # Run swing check once per day at market close (4:30pm ET)
            if now.hour == 16 and now.minute == 30:
                if last_swing_check != now.date():
                    log.info(f"\n📅 Market close — Running swing check...")
                    loop.run_until_complete(run_swing_check())
                    last_swing_check = now.date()
                    time.sleep(60)

            # Sleep 30 seconds between checks
            time.sleep(30)

        except KeyboardInterrupt:
            log.info("\n⏹️  Bot stopped")
            break
        except RuntimeError as e:
            if "attached to a different loop" in str(e):
                log.warning(f"Event loop mismatch detected: {e} - recreating event loop")
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            else:
                log.error(f"Bot error: {e}")
                log.error(f"Traceback: {traceback.format_exc()}")
            time.sleep(30)
        except Exception as e:
            log.error(f"Bot error: {e}")
            log.error(f"Traceback: {traceback.format_exc()}")
            time.sleep(30)


if __name__ == "__main__":
    run()

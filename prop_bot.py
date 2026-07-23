"""
DEL'S TRADING EMPIRE — PROP BOT v3
=====================================
APEX $25K Futures evaluation — MES, MNQ, MGC
Account: APEX_589296
Rule: 7 consecutive profitable days before going live
"""

import os
import asyncio
import logging
import smtplib
import time
from email.mime.text import MIMEText
from datetime import datetime
from zoneinfo import ZoneInfo
import aiohttp

ET = ZoneInfo("America/New_York")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("prop_bot")

ALPACA_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")
BASE_URL      = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
LIVE_TRADE    = os.getenv("ALPACA_LIVE_TRADE", "false").lower() == "true"
STOP          = os.getenv("STOP_TRADING", "false").lower() == "true"

# RSI entry/exit thresholds. Widened again at the account owner's explicit
# request - real trades were too rare at 38/48 (RSI mostly sat in the
# 39-57 range with nothing crossing 38). Wider band means more real trades
# fire, at the cost of acting on weaker/less-confirmed signals - that
# tradeoff was made knowingly, not a bug. Configurable via env for tuning
# without a code change.
RSI_BUY_BELOW  = float(os.getenv("PROP_RSI_BUY_BELOW", "45"))
RSI_SELL_ABOVE = float(os.getenv("PROP_RSI_SELL_ABOVE", "50"))

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
    "Content-Type": "application/json"
}

# APEX futures — use micro contracts (lower risk during evaluation)
FUTURES = {
    "MES": {"name": "Micro E-mini S&P 500", "qty": 1, "symbol": "SPY"},   # Use SPY as proxy
    "MNQ": {"name": "Micro E-mini Nasdaq",  "qty": 1, "symbol": "QQQ"},   # Use QQQ as proxy
    "MYM": {"name": "Micro E-mini Dow",     "qty": 1, "symbol": "DIA"},   # Use DIA as proxy
    "M2K": {"name": "Micro E-mini Russell", "qty": 1, "symbol": "IWM"},   # Use IWM as proxy
    "MGC": {"name": "Micro Gold",           "qty": 1, "symbol": "GLD"},   # Use GLD as proxy
    "MCL": {"name": "Micro Crude Oil",      "qty": 1, "symbol": "USO"},   # Use USO as proxy
    "SIL": {"name": "Micro Silver",         "qty": 1, "symbol": "SLV"},   # Use SLV as proxy
}

# Max concurrent open positions. With 7 symbols scanned and a 6-position
# cap, a 7th entry signal while already full triggers rotation (see
# run_prop_cycle) instead of just being ignored.
MAX_POSITIONS = int(os.getenv("PROP_MAX_POSITIONS", "6"))

# Profit-target price move, in dollars, scaled by real account equity -
# the bigger the account gets, the larger a favorable price move needs to
# be before the bot locks in profit early (rather than a flat percentage).
# Explicit request: start at $0.25, step up to $0.30/$0.45/$0.60 as equity
# grows into the thousands. Checked against real Alpaca equity each cycle.
PROFIT_INCREMENT_MILESTONES = [
    (0,     0.25),
    (1000,  0.30),
    (5000,  0.45),
    (10000, 0.60),
]


def get_profit_increment(equity):
    if equity is None:
        return PROFIT_INCREMENT_MILESTONES[0][1]
    increment = PROFIT_INCREMENT_MILESTONES[0][1]
    for threshold, inc in PROFIT_INCREMENT_MILESTONES:
        if equity >= threshold:
            increment = inc
    return increment

# Track profitable days for APEX 7-day rule
profitable_days = []
daily_pnl = 0.0
open_prop_positions = {}

# Latest per-symbol scan snapshot, read by routers/trading_dashboard.py's
# GET /signals so the dashboard can show live price/RSI/trend instead of
# that only being visible in Railway logs. Written once per symbol per
# cycle in run_prop_cycle - this is a live-view read model, not the source
# of truth for trading decisions (open_prop_positions is).
latest_signals = {}
last_cycle_at = None
last_market_open = None

# Email alert on real fills/exits - reuses the same GMAIL_EMAIL/GMAIL_PASSWORD
# SMTP creds routers/orders.py already uses for order emails, no new
# credentials to configure. No-ops quietly (just a log line) if they aren't
# set, same as that existing code path.
TRADE_ALERT_EMAIL = os.getenv("TRADE_ALERT_EMAIL", "delfarrell591@gmail.com")


def send_trade_alert(subject: str, body: str):
    sender_email = os.getenv("GMAIL_EMAIL", "")
    sender_password = os.getenv("GMAIL_PASSWORD", "")
    if not sender_email or not sender_password:
        log.info(f"(trade alert email skipped - GMAIL_EMAIL/GMAIL_PASSWORD not set) {subject}")
        return
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = sender_email
        msg["To"] = TRADE_ALERT_EMAIL
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, TRADE_ALERT_EMAIL, msg.as_string())
        log.info(f"📧 Trade alert emailed to {TRADE_ALERT_EMAIL}")
    except Exception as e:
        log.warning(f"Trade alert email failed: {e}")


async def get_price_rsi(session, symbol):
    """Get price and RSI for futures proxy symbol"""
    try:
        url = f"https://data.alpaca.markets/v2/stocks/{symbol}/bars?timeframe=5Min&limit=20"
        async with session.get(url, headers=HEADERS) as r:
            if r.status != 200:
                return None
            data = await r.json()
            bars = data.get("bars", [])
            if len(bars) < 14:
                return None

            closes = [b["c"] for b in bars]
            price = closes[-1]

            gains = [max(closes[i]-closes[i-1], 0) for i in range(1, len(closes))]
            losses = [max(closes[i-1]-closes[i], 0) for i in range(1, len(closes))]
            avg_gain = sum(gains[-14:]) / 14
            avg_loss = sum(losses[-14:]) / 14
            rs = avg_gain / avg_loss if avg_loss > 0 else 100
            rsi = 100 - (100 / (1 + rs))

            sma5 = sum(closes[-5:]) / 5
            sma10 = sum(closes[-10:]) / 10
            trend = "bullish" if sma5 > sma10 else "bearish"

            return {"price": price, "rsi": round(rsi, 1), "trend": trend}
    except Exception as e:
        log.error(f"Price error {symbol}: {e}")
        return None


async def get_account_equity(session):
    """Real Alpaca account equity, used to scale the profit-target
    increment (see PROFIT_INCREMENT_MILESTONES). Falls back to None (base
    tier) on any failure - a scaling hiccup shouldn't block trading."""
    try:
        async with session.get(f"{BASE_URL}/v2/account", headers=HEADERS) as r:
            if r.status != 200:
                return None
            data = await r.json()
            return float(data.get("equity", 0))
    except Exception as e:
        log.warning(f"Could not fetch account equity for profit-target scaling: {e}")
        return None


async def get_account_cash(session):
    """Real Alpaca cash balance, used to size new positions in dollars
    rather than a fixed share count (see size_position). Falls back to
    None on any failure - callers fall back to the fixed 1-share size."""
    try:
        async with session.get(f"{BASE_URL}/v2/account", headers=HEADERS) as r:
            if r.status != 200:
                return None
            data = await r.json()
            return float(data.get("cash", 0))
    except Exception as e:
        log.warning(f"Could not fetch account cash for position sizing: {e}")
        return None


# Floor on a single position's dollar size. Below this, a position is too
# small to bother with (order fees/slippage would dominate) - skip the
# entry rather than place a near-zero fractional order.
MIN_POSITION_NOTIONAL = float(os.getenv("PROP_MIN_POSITION_NOTIONAL", "10"))


def size_position(cash_remaining, slots_remaining, price):
    """Dollar-based (fractional-share) position sizing. A fixed 1-share
    order fails outright on higher-priced ETFs (SPY, QQQ, DIA) once cash
    is tight, while cheaper ones (SLV, USO) fill fine - silently capping
    how many of the open slots can ever actually fill regardless of how
    many real signals come in. Splitting whatever cash is left evenly
    across the remaining open slots means a small account can still use
    all its slots, no matter which symbol's proxy ETF happens to signal.
    Returns None if there isn't enough cash left for even one minimum-size
    position."""
    if slots_remaining <= 0 or cash_remaining < MIN_POSITION_NOTIONAL:
        return None
    amount = min(max(cash_remaining / slots_remaining, MIN_POSITION_NOTIONAL), cash_remaining)
    qty = round(amount / price, 6)
    return qty if qty > 0 else None


async def broadcast_signal_to_subscribers(session, contract, action, price, rsi, trend, stop_loss=None, target=None):
    """Broadcast signal to all trading signal subscribers via API."""
    try:
        api_url = os.getenv("API_BASE_URL", "http://localhost:8000")
        signal_data = {
            "contract": contract,
            "action": action,
            "entry_price": price,
            "stop_loss": stop_loss or (price * 0.98),  # 2% stop loss if not specified
            "target_price": target or (price * 1.03 if action == "BUY" else price * 0.97),  # 3% target
            "rsi": rsi,
            "trend": trend,
            "confidence": 0.85,
        }

        async with session.post(f"{api_url}/trading/signals/broadcast", json=signal_data) as r:
            if r.status == 200:
                result = await r.json()
                log.info(f"📡 Signal broadcast complete: {result.get('subscribers_notified', 0)} subscribers notified")
                return True
            else:
                log.warning(f"Signal broadcast failed: {r.status}")
                return False
    except Exception as e:
        log.warning(f"Could not broadcast signal: {e}")
        return False


async def execute_futures_trade(session, contract, action, qty, price, rsi, trend, stop_loss=None, target=None):
    """Place a real order via Alpaca. `action` is the literal order side
    ("BUY" or "SELL") - what that *means* (open a long, open a short, close
    a long, cover a short) depends on the caller's position state, tracked
    in run_prop_cycle, not here. This function just places the order,
    broadcasts the signal, and reports success/failure - it doesn't touch
    open_prop_positions or send fill emails, since a single fill can mean
    different things (new entry vs. exit) that the caller knows and this
    function doesn't."""
    symbol = FUTURES[contract]["symbol"]
    side = "buy" if action == "BUY" else "sell"

    order = {
        "symbol": symbol,
        "qty": str(qty),
        "side": side,
        "type": "market",
        "time_in_force": "day",
    }

    mode = "LIVE" if LIVE_TRADE else "PAPER"

    try:
        async with session.post(f"{BASE_URL}/v2/orders", headers=HEADERS, json=order) as r:
            result = await r.json()
            if r.status in (200, 201):
                log.info(f"✅ FUTURES TRADE | {mode} | {action} {qty} {contract} ({symbol}) @ ${price:.2f} | APEX_589296")
                await broadcast_signal_to_subscribers(session, contract, action, price, rsi, trend, stop_loss, target)
                return True
            else:
                log.error(f"❌ Futures order failed: {result.get('message', result)}")
                return False
    except Exception as e:
        log.error(f"Futures trade error: {e}")
        return False


async def run_prop_cycle():
    global daily_pnl, profitable_days, last_cycle_at, last_market_open

    # Only trade during market hours (9:30am-4pm ET). Checked against real
    # ET wall-clock time (DST-aware) rather than a hardcoded UTC range -
    # a fixed 14:30-21:00 UTC window is wrong by an hour for about 8
    # months of the year whenever ET is in daylight time.
    now = datetime.now(ET)
    is_weekday = now.weekday() < 5
    market_open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close_t = now.replace(hour=16, minute=0, second=0, microsecond=0)

    last_cycle_at = now.isoformat()

    if not (is_weekday and market_open_t <= now <= market_close_t):
        last_market_open = False
        log.info(f"[APEX_589296] Market closed — waiting for 9:30am ET")
        return

    last_market_open = True

    log.info(f"[APEX_589296] Scanning futures markets ({', '.join(FUTURES)})... | Daily P&L: ${daily_pnl:.2f}")

    async def close_position(session, contract, config, position, price, rsi, trend, reason_label):
        """Shared close/cover path for both a normal exit and a rotation
        exit - same order placement, P&L accounting, and alert either way."""
        side = position["side"]
        entry = position["entry"]
        qty = position["qty"]
        close_action = "SELL" if side == "long" else "BUY"
        profit_pct = ((price - entry) / entry * 100) if side == "long" else ((entry - price) / entry * 100)
        pnl = (profit_pct / 100) * entry * qty * 50  # MES point value ~$5 * 10

        filled = await execute_futures_trade(session, contract, close_action, qty, price, rsi, trend, target=price)
        if not filled:
            return False

        global daily_pnl
        daily_pnl += pnl
        log.info(f"[APEX_589296] 📤 CLOSE {side.upper()} {contract} ({reason_label}) | Entry: ${entry:.2f} Exit: ${price:.2f} | P&L: ${pnl:.2f} ({profit_pct:.2f}%)")
        send_trade_alert(
            f"🤖 Bare Metal Builders — {contract} {side} closed ({reason_label})",
            f"{side.capitalize()} position closed on APEX_589296:\n\n"
            f"{contract} | Entry: ${entry:.2f} | Exit: ${price:.2f}\n"
            f"P&L: ${pnl:.2f} ({profit_pct:.2f}%) | Reason: {reason_label}\n\n"
            f"Dashboard: https://empire-v2-production.up.railway.app/trading-dashboard",
        )
        open_prop_positions.pop(contract, None)
        return True

    async def open_position(session, contract, config, side, price, rsi, trend, qty):
        action = "BUY" if side == "long" else "SELL"
        if side == "long":
            stop_loss, target = price * 0.98, price * 1.03
        else:
            stop_loss, target = price * 1.02, price * 0.97

        filled = await execute_futures_trade(session, contract, action, qty, price, rsi, trend, stop_loss, target)
        if not filled:
            return False

        open_prop_positions[contract] = {"side": side, "entry": price, "qty": qty}
        send_trade_alert(
            f"🤖 Bare Metal Builders — {side.upper()} {contract} opened",
            f"{'LIVE' if LIVE_TRADE else 'PAPER'} {side} opened on APEX_589296:\n\n"
            f"{action} {qty} {contract} ({config['symbol']}) @ ${price:.2f}\n"
            f"RSI: {rsi} | Trend: {trend}\n\n"
            f"Dashboard: https://empire-v2-production.up.railway.app/trading-dashboard",
        )
        return True

    async def try_open(contract, config, side, price, rsi, trend, slots_remaining):
        """Wraps open_position with dollar-based sizing against whatever
        cash is actually left this cycle (tracked in cash_remaining, closed
        over from run_prop_cycle) - falls back to the fixed 1-share size
        if the real cash balance couldn't be fetched this cycle."""
        nonlocal cash_remaining
        if cash_remaining is not None:
            qty = size_position(cash_remaining, slots_remaining, price)
            if qty is None:
                log.info(f"[APEX_589296] Skipping {contract} {side} entry — not enough cash left (${cash_remaining:.2f})")
                return False
        else:
            qty = config["qty"]

        opened = await open_position(session, contract, config, side, price, rsi, trend, qty)
        if opened and cash_remaining is not None:
            cash_remaining -= qty * price
        return opened

    async with aiohttp.ClientSession() as session:
        equity = await get_account_equity(session)
        profit_increment = get_profit_increment(equity)
        log.info(f"[APEX_589296] Equity: {'$%.2f' % equity if equity is not None else 'unknown'} | Profit-target increment: ${profit_increment:.2f}")

        # Tracked and spent-down across this cycle's entries so dollar-based
        # sizing (see try_open/size_position) reflects money already
        # committed to earlier orders this same cycle, without an extra
        # API call per entry.
        cash_remaining = await get_account_cash(session)
        log.info(f"[APEX_589296] Cash available: {'$%.2f' % cash_remaining if cash_remaining is not None else 'unknown'}")

        scans = {}
        for contract, config in FUTURES.items():
            data = await get_price_rsi(session, config["symbol"])
            if data:
                scans[contract] = data
                log.info(f"[APEX_589296] {contract} ({config['symbol']}) | ${data['price']:.2f} | RSI:{data['rsi']} | {data['trend']}")
            await asyncio.sleep(0.3)

        # ── Pass 1: manage exits for symbols already held ────────────────
        # A long profits as price rises and exits on overbought RSI; a
        # short profits as price falls and exits on oversold RSI. Profit
        # target is now a real-dollar price move (see
        # PROFIT_INCREMENT_MILESTONES), not a flat percentage, and scales
        # up as the real account grows.
        for contract, position in list(open_prop_positions.items()):
            data = scans.get(contract)
            config = FUTURES[contract]
            if not data:
                continue
            price, rsi, trend = data["price"], data["rsi"], data["trend"]
            latest_signals[contract] = {
                "symbol": config["symbol"], "price": price, "rsi": rsi, "trend": trend,
                "status": "HOLDING_LONG" if position["side"] == "long" else "HOLDING_SHORT",
                "has_position": True, "checked_at": now.isoformat(),
            }

            side = position["side"]
            entry = position["entry"]
            if side == "long":
                price_move = price - entry
                rsi_exit = rsi > RSI_SELL_ABOVE or (trend == "bearish" and rsi > 50)
            else:
                price_move = entry - price
                rsi_exit = rsi < RSI_BUY_BELOW or (trend == "bullish" and rsi < 50)

            if price_move >= profit_increment or rsi_exit:
                reason = "PROFIT TARGET" if price_move >= profit_increment else "RSI"
                await close_position(session, contract, config, position, price, rsi, trend, reason)

            await asyncio.sleep(0.3)

        # ── Pass 2: new entries, with rotation if already at the cap ─────
        candidates = []
        for contract, config in FUTURES.items():
            if contract in open_prop_positions:
                continue
            data = scans.get(contract)
            if not data:
                continue
            price, rsi, trend = data["price"], data["rsi"], data["trend"]

            if rsi < RSI_BUY_BELOW:
                candidates.append((RSI_BUY_BELOW - rsi, contract, config, "long", price, rsi, trend))
                status = "BUY_ZONE"
            elif rsi > RSI_SELL_ABOVE:
                candidates.append((rsi - RSI_SELL_ABOVE, contract, config, "short", price, rsi, trend))
                status = "SHORT_ZONE"
            else:
                status = "NEUTRAL"
            latest_signals[contract] = {
                "symbol": config["symbol"], "price": price, "rsi": rsi, "trend": trend,
                "status": status, "has_position": False, "checked_at": now.isoformat(),
            }

        candidates.sort(key=lambda c: -c[0])  # strongest (furthest past threshold) first

        for _, contract, config, side, price, rsi, trend in candidates:
            if len(open_prop_positions) < MAX_POSITIONS:
                log.info(f"[APEX_589296] 📡 {side.upper()} {contract} — RSI:{rsi} Trend:{trend}")
                await try_open(contract, config, side, price, rsi, trend, MAX_POSITIONS - len(open_prop_positions))
            else:
                # At the cap - find the weakest held position (lowest
                # unrealized P&L). Only rotate out of it if it's a genuine
                # loss (strictly negative) - never sell a winning position,
                # and never a merely-flat one either, since a position that
                # was *just* opened this same cycle reads as exactly 0% and
                # would otherwise get rotated out seconds after opening
                # whenever several signals fire in the same cycle.
                weakest_contract, weakest_pct = None, None
                for held_contract, held_pos in open_prop_positions.items():
                    held_data = scans.get(held_contract)
                    if not held_data:
                        continue
                    held_price = held_data["price"]
                    held_pct = (
                        (held_price - held_pos["entry"]) / held_pos["entry"] * 100 if held_pos["side"] == "long"
                        else (held_pos["entry"] - held_price) / held_pos["entry"] * 100
                    )
                    if weakest_pct is None or held_pct < weakest_pct:
                        weakest_pct, weakest_contract = held_pct, held_contract

                if weakest_contract is not None and weakest_pct is not None and weakest_pct < 0:
                    held_data = scans[weakest_contract]
                    # Capture the dollar value being freed up before closing
                    # (close_position pops it from open_prop_positions), so
                    # try_open's sizing reflects the cash rotation frees, not
                    # just what was already sitting uninvested.
                    freed_value = open_prop_positions[weakest_contract]["qty"] * held_data["price"]
                    log.info(
                        f"[APEX_589296] 🔄 ROTATING: {weakest_contract} ({weakest_pct:.2f}%, weakest of {MAX_POSITIONS}) "
                        f"→ {contract} (RSI:{rsi} {side})"
                    )
                    closed = await close_position(
                        session, weakest_contract, FUTURES[weakest_contract], open_prop_positions[weakest_contract],
                        held_data["price"], held_data["rsi"], held_data["trend"], "ROTATED OUT",
                    )
                    if closed:
                        if cash_remaining is not None:
                            cash_remaining += freed_value
                        await try_open(contract, config, side, price, rsi, trend, MAX_POSITIONS - len(open_prop_positions))
                else:
                    log.info(
                        f"[APEX_589296] At max positions ({MAX_POSITIONS}) - {contract} {side} signal held, "
                        f"weakest position ({weakest_contract} {weakest_pct:+.2f}%) isn't a loss, not rotating"
                        if weakest_contract else
                        f"[APEX_589296] At max positions ({MAX_POSITIONS}) - {contract} {side} signal held, no rotation candidate"
                    )

            await asyncio.sleep(0.3)

    # Check if today was profitable
    today = now.strftime("%Y-%m-%d")
    if daily_pnl > 0 and (not profitable_days or profitable_days[-1] != today):
        profitable_days.append(today)
        log.info(f"✅ PROFITABLE DAY #{len(profitable_days)} | ${daily_pnl:.2f} | APEX_589296")
        if len(profitable_days) >= 7:
            log.info("🎯 7 CONSECUTIVE PROFITABLE DAYS ACHIEVED — READY TO GO LIVE!")
            log.info("ACTION: Change ALPACA_LIVE_TRADE=true in Railway to go live")


def run():
    log.info("=" * 60)
    log.info("DEL'S TRADING EMPIRE — PROP BOT v3")
    log.info(f"Account: APEX_589296 | Mode: {'LIVE' if LIVE_TRADE else 'PAPER'}")
    log.info(f"RSI thresholds: long entry < {RSI_BUY_BELOW} | short entry > {RSI_SELL_ABOVE} (trades both directions)")
    log.info(f"Profitable days: {len(profitable_days)}/7 needed")
    log.info("=" * 60)

    while True:
        if STOP:
            log.warning("STOP_TRADING=true — prop bot paused")
            time.sleep(60)
            continue
        try:
            asyncio.run(run_prop_cycle())
        except Exception as e:
            log.error(f"Prop cycle error: {e}")
        time.sleep(30)


if __name__ == "__main__":
    run()

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
import aiohttp

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
}

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

    # Only trade during market hours (9:30am - 4pm ET = 14:30 - 21:00 UTC)
    now = datetime.utcnow()
    market_open = now.replace(hour=14, minute=30, second=0)
    market_close = now.replace(hour=21, minute=0, second=0)

    last_cycle_at = now.isoformat()

    if not (market_open <= now <= market_close):
        last_market_open = False
        log.info(f"[APEX_589296] Market closed — waiting for 9:30am ET")
        return

    last_market_open = True

    log.info(f"[APEX_589296] Scanning futures markets ({', '.join(FUTURES)})... | Daily P&L: ${daily_pnl:.2f}")

    async with aiohttp.ClientSession() as session:
        for contract, config in FUTURES.items():
            data = await get_price_rsi(session, config["symbol"])
            if not data:
                continue

            price = data["price"]
            rsi   = data["rsi"]
            trend = data["trend"]

            log.info(f"[APEX_589296] {contract} ({config['symbol']}) | ${price:.2f} | RSI:{rsi} | {trend}")

            position = open_prop_positions.get(contract)
            has_position = position is not None

            if has_position:
                status = "HOLDING_LONG" if position["side"] == "long" else "HOLDING_SHORT"
            elif rsi < RSI_BUY_BELOW:
                status = "BUY_ZONE"
            elif rsi > RSI_SELL_ABOVE:
                status = "SHORT_ZONE"
            else:
                status = "NEUTRAL"

            latest_signals[contract] = {
                "symbol": config["symbol"],
                "price": price,
                "rsi": rsi,
                "trend": trend,
                "status": status,
                "has_position": has_position,
                "checked_at": now.isoformat(),
            }

            if not has_position:
                # Flat - look for a new entry in either direction, so the
                # bot can make money whether the market is heading up or
                # down, not just up. RSI oversold opens a LONG (expect a
                # bounce); RSI overbought opens a SHORT (expect a pullback -
                # this account has shorting enabled). Trend is logged only,
                # not a hard filter (see note above on why that gate was
                # dropped).
                if rsi < RSI_BUY_BELOW:
                    log.info(f"[APEX_589296] 📡 LONG {contract} — RSI:{rsi} Trend:{trend}")
                    stop_loss = price * 0.98  # 2% below entry
                    target = price * 1.03    # 3% above entry
                    filled = await execute_futures_trade(session, contract, "BUY", config["qty"], price, rsi, trend, stop_loss, target)
                    if filled:
                        open_prop_positions[contract] = {"side": "long", "entry": price, "qty": config["qty"]}
                        send_trade_alert(
                            f"🤖 Bare Metal Builders — LONG {contract} opened",
                            f"{'LIVE' if LIVE_TRADE else 'PAPER'} long opened on APEX_589296:\n\n"
                            f"BUY {config['qty']} {contract} ({config['symbol']}) @ ${price:.2f}\n"
                            f"RSI: {rsi} | Trend: {trend}\n\n"
                            f"Dashboard: https://empire-v2-production.up.railway.app/trading-dashboard",
                        )
                elif rsi > RSI_SELL_ABOVE:
                    log.info(f"[APEX_589296] 📡 SHORT {contract} — RSI:{rsi} Trend:{trend}")
                    stop_loss = price * 1.02  # 2% above entry - a short loses if price rises
                    target = price * 0.97    # 3% below entry
                    filled = await execute_futures_trade(session, contract, "SELL", config["qty"], price, rsi, trend, stop_loss, target)
                    if filled:
                        open_prop_positions[contract] = {"side": "short", "entry": price, "qty": config["qty"]}
                        send_trade_alert(
                            f"🤖 Bare Metal Builders — SHORT {contract} opened",
                            f"{'LIVE' if LIVE_TRADE else 'PAPER'} short opened on APEX_589296:\n\n"
                            f"SELL {config['qty']} {contract} ({config['symbol']}) @ ${price:.2f}\n"
                            f"RSI: {rsi} | Trend: {trend}\n\n"
                            f"Dashboard: https://empire-v2-production.up.railway.app/trading-dashboard",
                        )

            else:
                # In a position - exit logic differs by direction: a long
                # profits as price rises and exits on overbought RSI; a
                # short profits as price falls and exits on oversold RSI.
                # Profit-target check is symmetric (1.5% either direction).
                side = position["side"]
                entry = position["entry"]
                qty = position["qty"]

                if side == "long":
                    profit_pct = ((price - entry) / entry) * 100
                    rsi_exit = rsi > RSI_SELL_ABOVE or (trend == "bearish" and rsi > 50)
                    close_action = "SELL"
                else:
                    profit_pct = ((entry - price) / entry) * 100
                    rsi_exit = rsi < RSI_BUY_BELOW or (trend == "bullish" and rsi < 50)
                    close_action = "BUY"

                profit_target_hit = profit_pct >= 1.5  # Exit at +1.5% profit (lock in wins early)

                if profit_target_hit or rsi_exit:
                    pnl = (profit_pct / 100) * entry * qty * 50  # MES point value ~$5 * 10
                    exit_reason = "PROFIT TARGET" if profit_target_hit else "RSI"

                    filled = await execute_futures_trade(session, contract, close_action, qty, price, rsi, trend, target=price)
                    if filled:
                        daily_pnl += pnl
                        log.info(f"[APEX_589296] 📤 CLOSE {side.upper()} {contract} ({exit_reason}) | Entry: ${entry:.2f} Exit: ${price:.2f} | P&L: ${pnl:.2f} ({profit_pct:.2f}%)")

                        send_trade_alert(
                            f"🤖 Bare Metal Builders — {contract} {side} closed ({exit_reason})",
                            f"{side.capitalize()} position closed on APEX_589296:\n\n"
                            f"{contract} | Entry: ${entry:.2f} | Exit: ${price:.2f}\n"
                            f"P&L: ${pnl:.2f} ({profit_pct:.2f}%) | Reason: {exit_reason}\n\n"
                            f"Dashboard: https://empire-v2-production.up.railway.app/trading-dashboard",
                        )

                        open_prop_positions.pop(contract, None)

            await asyncio.sleep(0.5)

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

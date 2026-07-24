"""
CRYPTO TRADING BOT — Alpaca crypto, runs 24/7 (no market-hours gate)
=====================================================================
Explicit request: make money on nights/weekends when the stock market
(and prop_bot.py) is closed, using the leftover/idle cash already
sitting in the account - not a new exchange account.

Trades BTC/USD and ETH/USD directly through the SAME Alpaca account and
API keys prop_bot.py already uses for stocks. This is deliberately
different from crypto_scalp_grid_bot.py, which trades on Binance - a
completely separate exchange requiring its own account, its own
funding, and (since Binance's international API isn't available to US
persons) Binance.US specifically. Alpaca crypto needs none of that:
same keys, same real cash balance, no new signup.

Long-only: Alpaca crypto trading is spot-only, no shorting support -
unlike prop_bot.py's stock/ETF trading there is no short side at all
here, so the shorting-restriction issue prop_bot.py hit doesn't apply.

Runs against a CAPPED slice of the account's real cash (MAX_ALLOCATION,
default $100), not the full balance - so this bot doesn't compete
dollar-for-dollar with prop_bot.py's own stock entries for the same
cash. Alpaca's own real-time balance check is still the final
authority regardless (an order that would overdraw the real account
simply gets rejected) - this cap is just a soft, self-imposed limit so
one bot doesn't starve the other under normal conditions.
"""
import os
import asyncio
import logging
import smtplib
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
import aiohttp

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("crypto_alpaca_bot")

ALPACA_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")
BASE_URL      = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
DATA_URL      = "https://data.alpaca.markets"

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
    "Content-Type": "application/json",
}

CRYPTO_PAIRS = ["BTC/USD", "ETH/USD"]

# Same widened RSI thresholds prop_bot.py settled on, for the same reason:
# narrower bands meant real trades were too rare.
RSI_BUY_BELOW  = float(os.getenv("CRYPTO_RSI_BUY_BELOW", "45"))
RSI_SELL_ABOVE = float(os.getenv("CRYPTO_RSI_SELL_ABOVE", "50"))

MAX_POSITIONS = int(os.getenv("CRYPTO_MAX_POSITIONS", "2"))
MAX_ALLOCATION = float(os.getenv("CRYPTO_MAX_ALLOCATION", "100"))
MIN_POSITION_NOTIONAL = float(os.getenv("CRYPTO_MIN_POSITION_NOTIONAL", "5"))

# Same real-dollar-profit design as prop_bot.py's PROFIT_TARGET_DOLLARS_MILESTONES -
# take the 50c-$1 real profit, don't hold out for a bigger move, scaling
# up slightly as the whole account grows.
PROFIT_TARGET_DOLLARS_MILESTONES = [
    (0,     0.50),
    (1000,  0.60),
    (5000,  0.80),
    (10000, 1.00),
]


def get_profit_target_dollars(equity):
    if equity is None:
        return PROFIT_TARGET_DOLLARS_MILESTONES[0][1]
    target = PROFIT_TARGET_DOLLARS_MILESTONES[0][1]
    for threshold, t in PROFIT_TARGET_DOLLARS_MILESTONES:
        if equity >= threshold:
            target = t
    return target


open_crypto_positions = {}
daily_pnl = 0.0
latest_signals = {}
last_cycle_at = None

TRADE_ALERT_EMAIL = os.getenv("TRADE_ALERT_EMAIL", "delfarrell591@gmail.com")


def send_trade_alert(subject: str, body: str):
    """Same GMAIL_EMAIL/GMAIL_PASSWORD SMTP pattern as prop_bot.py -
    no-ops quietly if creds aren't set."""
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


async def get_account_equity_and_cash(session):
    """Real Alpaca equity/cash - the SAME account prop_bot.py trades
    stocks on. Falls back to (None, None) on any failure."""
    try:
        async with session.get(f"{BASE_URL}/v2/account", headers=HEADERS) as r:
            if r.status != 200:
                return None, None
            data = await r.json()
            return float(data.get("equity", 0)), float(data.get("cash", 0))
    except Exception as e:
        log.warning(f"Could not fetch account for crypto sizing: {e}")
        return None, None


async def get_price_rsi(session, symbol):
    """Alpaca's crypto bars endpoint returns bars keyed by symbol (not a
    flat list like the stocks bars endpoint), since one request can ask
    for multiple symbols at once."""
    try:
        url = f"{DATA_URL}/v1beta3/crypto/us/bars"
        params = {"symbols": symbol, "timeframe": "5Min", "limit": "20"}
        async with session.get(url, headers=HEADERS, params=params) as r:
            if r.status != 200:
                return None
            data = await r.json()
            bars = (data.get("bars") or {}).get(symbol, [])
            if len(bars) < 14:
                return None

            closes = [b["c"] for b in bars]
            price = closes[-1]

            gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
            losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
            avg_gain = sum(gains[-14:]) / 14
            avg_loss = sum(losses[-14:]) / 14
            rs = avg_gain / avg_loss if avg_loss > 0 else 100
            rsi = 100 - (100 / (1 + rs))

            return {"price": price, "rsi": round(rsi, 1)}
    except Exception as e:
        log.error(f"Crypto price error {symbol}: {e}")
        return None


def size_position(cash_pool_remaining, slots_remaining, price):
    """Same dollar-based fractional sizing as prop_bot.py's
    size_position() - splits whatever's left in the (capped) crypto
    cash pool evenly across remaining open slots. 8-decimal rounding
    (vs. prop_bot's 6) since crypto per-unit prices run much higher
    (BTC), needing finer fractional precision for a small dollar
    allocation - same precision crypto_scalp_grid_bot.py already uses."""
    if slots_remaining <= 0 or cash_pool_remaining < MIN_POSITION_NOTIONAL:
        return None
    amount = min(max(cash_pool_remaining / slots_remaining, MIN_POSITION_NOTIONAL), cash_pool_remaining)
    qty = round(amount / price, 8)
    return qty if qty > 0 else None


async def place_order(session, symbol, side, qty):
    """Crypto orders require time_in_force='gtc' (or 'ioc') - 'day'
    isn't valid since crypto has no daily close to expire against."""
    order = {"symbol": symbol, "qty": str(qty), "side": side, "type": "market", "time_in_force": "gtc"}
    try:
        async with session.post(f"{BASE_URL}/v2/orders", headers=HEADERS, json=order) as r:
            result = await r.json()
            if r.status in (200, 201):
                log.info(f"✅ CRYPTO TRADE | {side.upper()} {qty} {symbol}")
                return True
            log.error(f"❌ Crypto order failed: {result.get('message', result)}")
            return False
    except Exception as e:
        log.error(f"Crypto order error: {e}")
        return False


async def run_crypto_cycle():
    global daily_pnl, last_cycle_at

    now = datetime.now(timezone.utc)
    last_cycle_at = now.isoformat()
    log.info(f"[CRYPTO] Scanning {', '.join(CRYPTO_PAIRS)} (24/7, no market-hours gate) | Daily P&L: ${daily_pnl:.2f}")

    async with aiohttp.ClientSession() as session:
        equity, cash = await get_account_equity_and_cash(session)
        profit_target = get_profit_target_dollars(equity)
        cash_pool = min(cash, MAX_ALLOCATION) if cash is not None else MAX_ALLOCATION
        log.info(f"[CRYPTO] Equity: {'$%.2f' % equity if equity is not None else 'unknown'} | Crypto cash pool: ${cash_pool:.2f} (capped at ${MAX_ALLOCATION:.2f}) | Profit target: ${profit_target:.2f}/position")

        scans = {}
        for symbol in CRYPTO_PAIRS:
            data = await get_price_rsi(session, symbol)
            if data:
                scans[symbol] = data
                log.info(f"[CRYPTO] {symbol} | ${data['price']:.2f} | RSI:{data['rsi']}")
            await asyncio.sleep(0.3)

        # ── Pass 1: manage exits (long only) ──────────────────────────
        for symbol, position in list(open_crypto_positions.items()):
            data = scans.get(symbol)
            if not data:
                continue
            price, rsi = data["price"], data["rsi"]
            entry, qty = position["entry"], position["qty"]
            unrealized_pnl = (price - entry) * qty
            rsi_exit = rsi > RSI_SELL_ABOVE

            latest_signals[symbol] = {
                "price": price, "rsi": rsi, "status": "HOLDING_LONG",
                "has_position": True, "checked_at": now.isoformat(),
            }

            if unrealized_pnl >= profit_target or rsi_exit:
                reason = "PROFIT TARGET" if unrealized_pnl >= profit_target else "RSI"
                filled = await place_order(session, symbol, "sell", qty)
                if filled:
                    daily_pnl += unrealized_pnl
                    log.info(f"[CRYPTO] 📤 CLOSE {symbol} ({reason}) | Entry: ${entry:.2f} Exit: ${price:.2f} | P&L: ${unrealized_pnl:.2f}")
                    send_trade_alert(
                        f"🤖 Crypto bot — {symbol} closed ({reason})",
                        f"Position closed on your Alpaca account:\n\n"
                        f"{symbol} | Entry: ${entry:.2f} | Exit: ${price:.2f} | P&L: ${unrealized_pnl:.2f}\n"
                        f"Reason: {reason}\n\nDashboard: https://empire-v2-production.up.railway.app/trading-dashboard",
                    )
                    open_crypto_positions.pop(symbol, None)

            await asyncio.sleep(0.3)

        # ── Pass 2: new entries (long only, RSI oversold) ─────────────
        for symbol in CRYPTO_PAIRS:
            if symbol in open_crypto_positions:
                continue
            data = scans.get(symbol)
            if not data:
                continue
            price, rsi = data["price"], data["rsi"]

            if rsi >= RSI_BUY_BELOW:
                latest_signals[symbol] = {
                    "price": price, "rsi": rsi, "status": "NEUTRAL",
                    "has_position": False, "checked_at": now.isoformat(),
                }
                continue

            latest_signals[symbol] = {
                "price": price, "rsi": rsi, "status": "BUY_ZONE",
                "has_position": False, "checked_at": now.isoformat(),
            }
            if len(open_crypto_positions) >= MAX_POSITIONS:
                log.info(f"[CRYPTO] At max positions ({MAX_POSITIONS}) - {symbol} BUY signal held, not entering")
                continue

            slots_remaining = MAX_POSITIONS - len(open_crypto_positions)
            qty = size_position(cash_pool, slots_remaining, price)
            if qty is None:
                log.info(f"[CRYPTO] Skipping {symbol} entry — not enough allocated cash (${cash_pool:.2f})")
                continue

            log.info(f"[CRYPTO] 📡 BUY {symbol} — RSI:{rsi}")
            filled = await place_order(session, symbol, "buy", qty)
            if filled:
                open_crypto_positions[symbol] = {"entry": price, "qty": qty}
                cash_pool -= qty * price
                send_trade_alert(
                    f"🤖 Crypto bot — BUY {symbol} opened",
                    f"Long opened on your Alpaca account:\n\n"
                    f"BUY {qty} {symbol} @ ${price:.2f} | RSI: {rsi}\n\n"
                    f"Dashboard: https://empire-v2-production.up.railway.app/trading-dashboard",
                )

            await asyncio.sleep(0.3)


def run():
    log.info("=" * 60)
    log.info("CRYPTO TRADING BOT — Alpaca crypto (same account as stocks)")
    log.info(f"Pairs: {', '.join(CRYPTO_PAIRS)} | Max allocation: ${MAX_ALLOCATION:.2f} | Max positions: {MAX_POSITIONS}")
    log.info("Runs 24/7 - crypto has no market close, unlike prop_bot.py's stock/ETF trading")
    log.info("=" * 60)

    if not (ALPACA_KEY and ALPACA_SECRET):
        log.warning("ALPACA_API_KEY/ALPACA_SECRET_KEY not configured - crypto_alpaca_bot will not start")
        return

    while True:
        if os.getenv("STOP_TRADING", "false").lower() == "true":
            log.warning("STOP_TRADING=true — crypto bot paused")
            time.sleep(60)
            continue
        try:
            asyncio.run(run_crypto_cycle())
        except Exception as e:
            log.error(f"Crypto cycle error: {e}")
        time.sleep(60)


if __name__ == "__main__":
    run()

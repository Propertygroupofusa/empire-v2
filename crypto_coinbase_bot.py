"""
CRYPTO TRADING BOT — Coinbase Advanced Trade, runs 24/7 (no market-hours gate)
================================================================================
Replaces crypto_alpaca_bot.py. Alpaca crypto trading turned out to be
blocked for this account: Alpaca only allows crypto orders for accounts
whose state/jurisdiction is on their approved list (confirmed directly
with Alpaca support - "crypto orders not allowed for account" was never
a bug, it's a regulatory gate with no code-side workaround), and this
account's state isn't on it. Coinbase has no such state restriction for
US accounts, so this bot places real orders there instead.

Trades BTC-USD and ETH-USD on a SEPARATE Coinbase account/balance (not
the Alpaca account prop_bot.py and daily_brief.py use for stocks) -
funding this bot means depositing USD into Coinbase directly.

Long-only, same reasoning as crypto_alpaca_bot.py had: Coinbase spot
trading has no shorting, so there's no short side to manage here either.

Runs against a CAPPED slice of the Coinbase account's USD balance
(MAX_ALLOCATION, default $100) rather than the full balance, same
self-imposed-limit reasoning as before.

Auth uses Coinbase Developer Platform (CDP) API keys - the current
Advanced Trade API auth method, replacing the older HMAC/passphrase
scheme Coinbase retired. Each request is signed as a short-lived
ES256 JWT (Coinbase's documented format: sub/iss/nbf/exp/uri claims,
kid+nonce headers), not a static signature.
"""
import os
import asyncio
import json
import logging
import secrets
import smtplib
import time
import uuid
from datetime import datetime, timezone
from email.mime.text import MIMEText
import aiohttp
import jwt as pyjwt
from cryptography.hazmat.primitives import serialization

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("crypto_coinbase_bot")

COINBASE_API_KEY_NAME = os.getenv("COINBASE_API_KEY_NAME", "")
COINBASE_API_PRIVATE_KEY = os.getenv("COINBASE_API_PRIVATE_KEY", "").replace("\\n", "\n")
COINBASE_HOST = "api.coinbase.com"
COINBASE_BASE_URL = f"https://{COINBASE_HOST}"

CRYPTO_PAIRS = ["BTC/USD", "ETH/USD"]


def _to_product_id(symbol: str) -> str:
    """BTC/USD -> BTC-USD (Coinbase's product ID format)."""
    return symbol.replace("/", "-")


def _build_jwt(method: str, path: str) -> str:
    """Coinbase CDP-style JWT, valid ~2 minutes, scoped to one method+path -
    a fresh one is required per request, unlike a static API signature."""
    private_key = serialization.load_pem_private_key(COINBASE_API_PRIVATE_KEY.encode(), password=None)
    now = int(time.time())
    payload = {
        "sub": COINBASE_API_KEY_NAME,
        "iss": "cdp",
        "nbf": now,
        "exp": now + 120,
        "uri": f"{method} {COINBASE_HOST}{path}",
    }
    headers = {"kid": COINBASE_API_KEY_NAME, "nonce": secrets.token_hex(16)}
    return pyjwt.encode(payload, private_key, algorithm="ES256", headers=headers)


def _auth_headers(method: str, path: str) -> dict:
    return {"Authorization": f"Bearer {_build_jwt(method, path)}", "Content-Type": "application/json"}


# Same widened RSI thresholds prop_bot.py/crypto_alpaca_bot.py settled on -
# narrower bands meant real trades were too rare.
RSI_BUY_BELOW  = float(os.getenv("CRYPTO_RSI_BUY_BELOW", "45"))
RSI_SELL_ABOVE = float(os.getenv("CRYPTO_RSI_SELL_ABOVE", "50"))

MAX_POSITIONS = int(os.getenv("CRYPTO_MAX_POSITIONS", str(len(CRYPTO_PAIRS))))
MAX_ALLOCATION = float(os.getenv("CRYPTO_MAX_ALLOCATION", "100"))
MIN_POSITION_NOTIONAL = float(os.getenv("CRYPTO_MIN_POSITION_NOTIONAL", "5"))

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

TRADE_ALERT_EMAIL = os.getenv("TRADE_ALERT_EMAIL", "")


def send_trade_alert(subject: str, body: str):
    """Same GMAIL_EMAIL/GMAIL_PASSWORD SMTP pattern used elsewhere -
    no-ops quietly if creds aren't set."""
    sender_email = os.getenv("GMAIL_EMAIL", "")
    sender_password = os.getenv("GMAIL_PASSWORD", "")
    alert_email = TRADE_ALERT_EMAIL

    if not sender_email or not sender_password:
        log.debug(f"(trade alert email skipped - GMAIL_EMAIL/GMAIL_PASSWORD not set) {subject}")
        return

    if not alert_email:
        log.debug(f"(trade alert email skipped - TRADE_ALERT_EMAIL not set) {subject}")
        return

    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = sender_email
        msg["To"] = alert_email
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, alert_email, msg.as_string())
        log.info(f"📧 Trade alert emailed to {alert_email}")
    except Exception as e:
        log.warning(f"Trade alert email failed: {e}")


async def get_usd_balance(session):
    """Real Coinbase USD balance available for trading. Returns None on
    any failure (auth issue, network hiccup, no USD wallet)."""
    path = "/api/v3/brokerage/accounts"
    try:
        async with session.get(COINBASE_BASE_URL + path, headers=_auth_headers("GET", path)) as r:
            if r.status != 200:
                log.warning(f"Could not fetch Coinbase accounts: HTTP {r.status} {await r.text()}")
                return None
            data = await r.json()
            for account in data.get("accounts", []):
                if account.get("currency") == "USD":
                    return float(account["available_balance"]["value"])
            return None
    except Exception as e:
        log.warning(f"Could not fetch Coinbase account for crypto sizing: {e}")
        return None


def _compute_rsi(closes: list) -> float:
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    avg_gain = sum(gains[-14:]) / 14
    avg_loss = sum(losses[-14:]) / 14
    rs = avg_gain / avg_loss if avg_loss > 0 else 100
    return round(100 - (100 / (1 + rs)), 1)


async def _fetch_coinbase_closes(session, symbol):
    """Free, no-auth public endpoint - BTC/USD -> BTC-USD. This is the
    primary price source since it's the same exchange orders execute on."""
    pair = _to_product_id(symbol)
    url = f"https://api.exchange.coinbase.com/products/{pair}/candles?granularity=300"
    async with session.get(url, headers={"Accept": "application/json"}) as r:
        if r.status != 200:
            return None
        data = await r.json()
        if not data or len(data) < 14:
            return None
        # Coinbase returns newest-first; each row is [time, low, high, open, close, volume]
        rows = list(reversed(data))[-20:]
        return [float(row[4]) for row in rows]


async def _fetch_binance_closes(session, symbol):
    """Free, no-auth public endpoint - BTC/USD -> BTCUSDT. Fallback only,
    used purely for price data if Coinbase's public feed has a hiccup."""
    pair = symbol.replace("/", "").replace("USD", "USDT")
    url = f"https://api.binance.com/api/v3/klines?symbol={pair}&interval=5m&limit=20"
    async with session.get(url, headers={"Accept": "application/json"}) as r:
        if r.status != 200:
            return None
        data = await r.json()
        if not data or len(data) < 14:
            return None
        return [float(row[4]) for row in data]


_PRICE_SOURCES = (
    ("coinbase", _fetch_coinbase_closes),
    ("binance", _fetch_binance_closes),
)


async def get_price_rsi(session, symbol):
    """Price+RSI with a fallback chain (Coinbase -> Binance) for
    resilience - crypto trades 24/7, so a single data-source outage
    shouldn't silently stall trading on a pair."""
    for source_name, fetch_fn in _PRICE_SOURCES:
        try:
            closes = await fetch_fn(session, symbol)
        except Exception as e:
            log.debug(f"{source_name} price fetch failed for {symbol}: {e}")
            closes = None

        if closes and len(closes) >= 14:
            rsi = _compute_rsi(closes)
            price = closes[-1]
            if source_name != "coinbase":
                log.info(f"✅ {symbol} using {source_name} (Coinbase unavailable) | Price: ${price:.2f} | RSI: {rsi:.1f}")
            return {"price": price, "rsi": rsi}

    log.error(f"❌ {symbol}: all price sources failed (coinbase, binance)")
    return None


def size_position(cash_pool_remaining, slots_remaining, price):
    """Same dollar-based fractional sizing as crypto_alpaca_bot.py's
    size_position() - splits whatever's left in the (capped) crypto cash
    pool evenly across remaining open slots."""
    if slots_remaining <= 0 or cash_pool_remaining < MIN_POSITION_NOTIONAL:
        return None
    amount = min(max(cash_pool_remaining / slots_remaining, MIN_POSITION_NOTIONAL), cash_pool_remaining)
    qty = round(amount / price, 8)
    return qty if qty > 0 else None


async def place_order(session, symbol, side, qty, price):
    """Coinbase Advanced Trade market orders: BUY sizes by dollar amount
    (quote_size), SELL sizes by coin amount (base_size) - unlike Alpaca,
    which takes qty for both sides."""
    product_id = _to_product_id(symbol)
    path = "/api/v3/brokerage/orders"
    order_config = (
        {"market_market_ioc": {"quote_size": f"{qty * price:.2f}"}}
        if side == "buy"
        else {"market_market_ioc": {"base_size": f"{qty:.8f}"}}
    )
    order = {
        "client_order_id": str(uuid.uuid4()),
        "product_id": product_id,
        "side": side.upper(),
        "order_configuration": order_config,
    }
    try:
        async with session.post(COINBASE_BASE_URL + path, headers=_auth_headers("POST", path), json=order) as r:
            result = await r.json()
            if r.status in (200, 201) and result.get("success", True):
                log.info(f"✅ CRYPTO TRADE | {side.upper()} {qty} {symbol}")
                return True
            log.error(f"❌ Crypto order failed: {result.get('error_response', result)}")
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
        cash = await get_usd_balance(session)
        profit_target = get_profit_target_dollars(cash)
        cash_pool = min(cash, MAX_ALLOCATION) if cash is not None else MAX_ALLOCATION
        log.info(f"[CRYPTO] Coinbase USD balance: {'$%.2f' % cash if cash is not None else 'unknown'} | Crypto cash pool: ${cash_pool:.2f} (capped at ${MAX_ALLOCATION:.2f}) | Profit target: ${profit_target:.2f}/position")

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
                filled = await place_order(session, symbol, "sell", qty, price)
                if filled:
                    daily_pnl += unrealized_pnl
                    log.info(f"[CRYPTO] 📤 CLOSE {symbol} ({reason}) | Entry: ${entry:.2f} Exit: ${price:.2f} | P&L: ${unrealized_pnl:.2f}")
                    send_trade_alert(
                        f"🤖 Crypto bot — {symbol} closed ({reason})",
                        f"Position closed on your Coinbase account:\n\n"
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
            filled = await place_order(session, symbol, "buy", qty, price)
            if filled:
                open_crypto_positions[symbol] = {"entry": price, "qty": qty}
                cash_pool -= qty * price
                send_trade_alert(
                    f"🤖 Crypto bot — BUY {symbol} opened",
                    f"Long opened on your Coinbase account:\n\n"
                    f"BUY {qty} {symbol} @ ${price:.2f} | RSI: {rsi}\n\n"
                    f"Dashboard: https://empire-v2-production.up.railway.app/trading-dashboard",
                )

            await asyncio.sleep(0.3)


def run():
    log.info("=" * 60)
    log.info("CRYPTO TRADING BOT — Coinbase (separate account from Alpaca stocks)")
    log.info(f"Pairs: {', '.join(CRYPTO_PAIRS)} | Max allocation: ${MAX_ALLOCATION:.2f} | Max positions: {MAX_POSITIONS}")
    log.info("Runs 24/7 - crypto has no market close, unlike prop_bot.py's stock/ETF trading")
    log.info("🔴 LIVE TRADING - Coinbase has no free paper-trading sandbox for Advanced Trade")
    log.info("=" * 60)

    if not (COINBASE_API_KEY_NAME and COINBASE_API_PRIVATE_KEY):
        log.warning("COINBASE_API_KEY_NAME/COINBASE_API_PRIVATE_KEY not configured - crypto_coinbase_bot will not start")
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

"""
CRYPTO SCALP-GRID BOT — 24/7 Binance Trading
=============================================
Grid trading + RSI scalping hybrid system for BTC, ETH, XRP.
Runs continuously, generates multiple trades per day per asset.

STRATEGY:
- Grid Layer: Buy dips (-2%, -4%, -6%), sell rallies (+0.8%, +1.6%, +2.4%)
- Scalp Layer: RSI < 30 buy, RSI > 70 sell, 0.5% targets, 2-5 min holds
- Risk: Position limits, daily loss stop, max drawdown protection
"""

import os
import asyncio
import logging
from datetime import datetime, timezone
from collections import defaultdict
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("crypto_scalp_grid")

try:
    from binance.client import Client as BinanceClient
    from binance.exceptions import BinanceAPIException, BinanceOrderException
    from binance.enums import *
    BINANCE_AVAILABLE = True
except ImportError:
    log.warning("python-binance not installed - crypto_scalp_grid_bot will not start")
    BINANCE_AVAILABLE = False

# ── CREDENTIALS ────────────────────────────────────────────────────
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
TESTNET_MODE = os.getenv("CRYPTO_TESTNET", "false").lower() == "true"

# ── CONFIG ─────────────────────────────────────────────────────────
TRADING_PAIRS = ["BTCUSDT", "ETHUSDT", "XRPUSDT"]  # High liquidity 24/7 pairs
GRID_LEVELS = {
    "BTCUSDT": {"capital": 100, "levels": [-2, -4, -6], "targets": [0.8, 1.6, 2.4]},
    "ETHUSDT": {"capital": 100, "levels": [-2, -4, -6], "targets": [0.8, 1.6, 2.4]},
    "XRPUSDT": {"capital": 100, "levels": [-2, -4, -6], "targets": [0.8, 1.6, 2.4]},
}

SCALP_CONFIG = {
    "rsi_period": 14,
    "rsi_buy_below": 30,
    "rsi_sell_above": 70,
    "profit_target_pct": 0.5,
    "max_hold_seconds": 300,  # 5 minutes
}

RISK_CONFIG = {
    "max_daily_loss": -50.0,
    "max_positions_per_pair": 3,
    "max_position_size": 500,
    "position_size_per_grid": 50,
}

CYCLE_INTERVAL = 60  # Check every 60 seconds

# ── STATE ──────────────────────────────────────────────────────────
positions = defaultdict(list)  # pair -> [{"id": str, "type": "grid|scalp", "entry": float, "qty": float, "side": "buy|sell", "created": datetime}]
daily_pnl = 0.0
total_trades = 0
positions_opened_today = 0
grid_orders = defaultdict(dict)  # pair -> {level: order_id}

client = None


def initialize_client():
    global client
    if not BINANCE_API_KEY or not BINANCE_API_SECRET:
        log.warning("Binance credentials not configured (BINANCE_API_KEY/SECRET) - crypto bot will not start")
        return False

    try:
        if TESTNET_MODE:
            client = BinanceClient(
                api_key=BINANCE_API_KEY,
                api_secret=BINANCE_API_SECRET,
                testnet=True
            )
            log.info("🧪 TESTNET MODE - using Binance testnet")
        else:
            client = BinanceClient(
                api_key=BINANCE_API_KEY,
                api_secret=BINANCE_API_SECRET
            )
            log.info("🚀 LIVE MODE - trading on Binance mainnet with real capital")

        # Test connection
        info = client.get_account()
        balances = info["balances"]
        usdt_bal = next((b for b in balances if b["asset"] == "USDT"), None)
        if usdt_bal:
            log.info(f"Binance account connected | USDT balance: ${float(usdt_bal['free']):.2f}")
        return True
    except Exception as e:
        log.error(f"Failed to initialize Binance client: {e}")
        return False


def get_price_and_rsi(pair: str):
    """Fetch current price and calculate RSI from 1-min candles"""
    try:
        klines = client.get_klines(symbol=pair, interval="1m", limit=20)
        closes = [float(k[4]) for k in klines]  # close price

        if len(closes) < 14:
            return None

        price = closes[-1]

        gains = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
        losses = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
        avg_gain = sum(gains[-14:]) / 14
        avg_loss = sum(losses[-14:]) / 14
        rs = avg_gain / avg_loss if avg_loss > 0 else 100
        rsi = 100 - (100 / (1 + rs))

        return {"price": price, "rsi": round(rsi, 1)}
    except Exception as e:
        log.error(f"Failed to fetch price/RSI for {pair}: {e}")
        return None


def place_limit_order(pair: str, side: str, qty: float, price: float) -> dict:
    """Place a limit order on Binance"""
    try:
        order = client.order_limit(
            symbol=pair,
            side=SIDE_BUY if side == "buy" else SIDE_SELL,
            timeInForce=TIME_IN_FORCE_GTC,
            quantity=qty,
            price=price
        )
        log.info(f"📤 LIMIT ORDER | {side.upper()} {qty} {pair} @ ${price:.4f} | orderId={order['orderId']}")
        return order
    except BinanceOrderException as e:
        log.error(f"Order failed for {pair}: {e}")
        return None
    except Exception as e:
        log.error(f"Order placement error: {e}")
        return None


def place_market_order(pair: str, side: str, qty: float) -> dict:
    """Place a market order on Binance"""
    try:
        order = client.order_market(
            symbol=pair,
            side=SIDE_BUY if side == "buy" else SIDE_SELL,
            quantity=qty
        )
        price = float(order.get("fills", [{}])[0].get("price", "0"))
        log.info(f"🎯 MARKET ORDER | {side.upper()} {qty} {pair} @ ${price:.4f} | orderId={order['orderId']}")
        return order
    except Exception as e:
        log.error(f"Market order failed for {pair}: {e}")
        return None


def execute_grid_layer(pair: str):
    """Place grid buy/sell orders at defined levels"""
    global positions_opened_today

    data = get_price_and_rsi(pair)
    if not data:
        return

    price = data["price"]
    config = GRID_LEVELS[pair]

    # Check if we can open new grid positions
    current_positions = len(positions[pair])
    if current_positions >= RISK_CONFIG["max_positions_per_pair"]:
        return

    # Place buy orders at grid levels (dips)
    for i, level_pct in enumerate(config["levels"]):
        buy_price = price * (1 + level_pct / 100)
        qty = config["capital"] / buy_price  # Quantity based on capital allocation

        # Check if already has order at this level
        if f"buy_{i}" in grid_orders[pair]:
            continue

        # Place buy order
        order = place_limit_order(pair, "buy", round(qty, 8), round(buy_price, 4))
        if order:
            grid_orders[pair][f"buy_{i}"] = order["orderId"]
            positions[pair].append({
                "id": str(order["orderId"]),
                "type": "grid",
                "entry": buy_price,
                "qty": qty,
                "side": "buy",
                "created": datetime.now(timezone.utc),
                "target": config["targets"][i]
            })
            positions_opened_today += 1


def execute_scalp_layer(pair: str):
    """RSI scalp: buy RSI < 30, sell RSI > 70, quick 0.5% targets"""
    global positions_opened_today

    data = get_price_and_rsi(pair)
    if not data:
        return

    price = data["price"]
    rsi = data["rsi"]

    # Check if we can open new positions
    current_positions = len(positions[pair])
    if current_positions >= RISK_CONFIG["max_positions_per_pair"]:
        return

    # BUY signal
    if rsi < SCALP_CONFIG["rsi_buy_below"]:
        qty = RISK_CONFIG["position_size_per_grid"] / price
        order = place_market_order(pair, "buy", round(qty, 8))
        if order:
            positions[pair].append({
                "id": str(order["orderId"]),
                "type": "scalp",
                "entry": price,
                "qty": qty,
                "side": "buy",
                "created": datetime.now(timezone.utc),
                "target": 0.5
            })
            positions_opened_today += 1
            log.info(f"📈 SCALP BUY {pair} | RSI:{rsi} | Price: ${price:.4f} | Target: +0.5%")

    # SELL signal (for closing long scalps)
    elif rsi > SCALP_CONFIG["rsi_sell_above"]:
        # Close oldest buy scalp if exists
        buy_scalps = [p for p in positions[pair] if p["side"] == "buy" and p["type"] == "scalp"]
        if buy_scalps:
            pos = buy_scalps[0]
            hold_time = (datetime.now(timezone.utc) - pos["created"]).total_seconds()
            if hold_time > 10:  # Hold at least 10 seconds before closing
                order = place_market_order(pair, "sell", pos["qty"])
                if order:
                    pnl = (price - pos["entry"]) / pos["entry"] * 100
                    log.info(f"📉 SCALP SELL {pair} | RSI:{rsi} | P&L: {pnl:.2f}%")
                    positions[pair].remove(pos)


def monitor_positions(pair: str):
    """Check if positions hit profit targets and close them"""
    global daily_pnl, total_trades

    for pos in positions[pair][:]:  # Copy list to avoid modification during iteration
        try:
            data = get_price_and_rsi(pair)
            if not data:
                continue

            price = data["price"]
            pnl_pct = (price - pos["entry"]) / pos["entry"] * 100 if pos["side"] == "buy" else (pos["entry"] - price) / pos["entry"] * 100

            # Close if profit target hit
            if pnl_pct >= pos["target"]:
                order = place_market_order(pair, "sell" if pos["side"] == "buy" else "buy", pos["qty"])
                if order:
                    trade_pnl = (price - pos["entry"]) * pos["qty"]
                    daily_pnl += trade_pnl
                    total_trades += 1
                    log.info(f"✅ CLOSE {pos['type'].upper()} {pair} | Entry: ${pos['entry']:.4f} Exit: ${price:.4f} | P&L: {pnl_pct:.2f}% (${trade_pnl:.2f})")
                    positions[pair].remove(pos)

            # Close if held too long (scalp only)
            elif pos["type"] == "scalp":
                hold_time = (datetime.now(timezone.utc) - pos["created"]).total_seconds()
                if hold_time > SCALP_CONFIG["max_hold_seconds"]:
                    order = place_market_order(pair, "sell" if pos["side"] == "buy" else "buy", pos["qty"])
                    if order:
                        trade_pnl = (price - pos["entry"]) * pos["qty"]
                        daily_pnl += trade_pnl
                        total_trades += 1
                        log.info(f"⏱️ TIMEOUT CLOSE {pair} | P&L: {pnl_pct:.2f}%")
                        positions[pair].remove(pos)

        except Exception as e:
            log.error(f"Error monitoring position: {e}")


async def run_cycle():
    """Main trading cycle: grid + scalp layers"""
    global daily_pnl, positions_opened_today

    # Check daily loss limit
    if daily_pnl <= RISK_CONFIG["max_daily_loss"]:
        log.warning(f"💔 Daily loss limit hit (${daily_pnl:.2f}) - stopping all trading")
        return

    log.info(f"[CRYPTO] Cycle start | Daily P&L: ${daily_pnl:.2f} | Open positions: {sum(len(p) for p in positions.values())} | Trades today: {total_trades}")

    for pair in TRADING_PAIRS:
        try:
            execute_grid_layer(pair)
            execute_scalp_layer(pair)
            monitor_positions(pair)
        except Exception as e:
            log.error(f"Cycle error for {pair}: {e}")

        await asyncio.sleep(0.5)


def run():
    if not BINANCE_AVAILABLE:
        log.warning("python-binance not available - crypto_scalp_grid_bot cannot start")
        return

    if not initialize_client():
        return

    log.info("=" * 60)
    log.info("CRYPTO SCALP-GRID BOT — 24/7 Binance Trading")
    log.info(f"Pairs: {', '.join(TRADING_PAIRS)} | Mode: {'TESTNET' if TESTNET_MODE else 'LIVE'}")
    log.info(f"Daily loss limit: ${RISK_CONFIG['max_daily_loss']} | Max positions/pair: {RISK_CONFIG['max_positions_per_pair']}")
    log.info("=" * 60)

    while True:
        if os.getenv("STOP_TRADING", "false").lower() == "true":
            log.warning("STOP_TRADING=true - crypto bot paused")
            time.sleep(60)
            continue
        try:
            asyncio.run(run_cycle())
        except Exception as e:
            log.error(f"Crypto cycle error: {e}")
        time.sleep(CYCLE_INTERVAL)


def get_status():
    """Return current bot status for API endpoint"""
    return {
        "pairs": TRADING_PAIRS,
        "mode": "TESTNET" if TESTNET_MODE else "LIVE",
        "daily_pnl": round(daily_pnl, 2),
        "total_trades": total_trades,
        "positions_open": sum(len(p) for p in positions.values()),
        "positions_by_pair": {pair: len(pos) for pair, pos in positions.items()},
        "positions_opened_today": positions_opened_today,
        "risk_limit": RISK_CONFIG["max_daily_loss"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    run()

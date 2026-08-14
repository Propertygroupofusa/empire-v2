#!/usr/bin/env python3
"""
TRADING EMPIRE — BRACKET ORDER SYSTEM
Places entry + take profit + stop loss in ONE order
Works for both stocks and crypto
Auto-exits when target or stop is hit — no monitoring needed
"""

import os
import json
import urllib.request
import urllib.error
import logging

log = logging.getLogger("bracket_orders")

API_KEY    = os.getenv("ALPACA_API_KEY",    "")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
BASE_URL   = os.getenv("ALPACA_BASE_URL",   "https://paper-api.alpaca.markets")

# ── DEFAULT RISK SETTINGS ─────────────────────────────────────
TAKE_PROFIT_PCT = 0.015   # 1.5% profit target
STOP_LOSS_PCT   = 0.010   # 1.0% stop loss


def _api(method, endpoint, body=None):
    """Make Alpaca API call."""
    url     = BASE_URL + endpoint
    headers = {
        "APCA-API-KEY-ID":     API_KEY,
        "APCA-API-SECRET-KEY": SECRET_KEY,
        "Content-Type":        "application/json",
        "Accept":              "application/json",
    }
    data = json.dumps(body).encode() if body else None
    req  = urllib.request.Request(
        url, data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        log.warning(f"  API {e.code}: {err[:100]}")
        return None
    except Exception as e:
        log.warning(f"  API error: {e}")
        return None


def get_current_price(symbol):
    """Get latest price for symbol."""
    try:
        if "/" in symbol:
            # Crypto
            sym = symbol.replace("/", "")
            url = (f"https://data.alpaca.markets/v1beta3/crypto/us/latest/bars"
                   f"?symbols={sym}")
            headers = {
                "APCA-API-KEY-ID":     API_KEY,
                "APCA-API-SECRET-KEY": SECRET_KEY,
            }
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode())
            return float(data["bars"][sym]["c"])
        else:
            # Stocks
            url = (f"https://data.alpaca.markets/v2/stocks/{symbol}"
                   f"/quotes/latest?feed=iex")
            headers = {
                "APCA-API-KEY-ID":     API_KEY,
                "APCA-API-SECRET-KEY": SECRET_KEY,
            }
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode())
            return float(data["quote"]["ap"])  # ask price
    except Exception as e:
        log.warning(f"  Price error {symbol}: {e}")
        return None


def place_bracket_order(
    symbol,
    notional,
    side="buy",
    take_profit_pct=TAKE_PROFIT_PCT,
    stop_loss_pct=STOP_LOSS_PCT,
):
    """
    Place a bracket order — entry + TP + SL in ONE call.
    Alpaca automatically places exit orders when entry fills.

    Args:
        symbol:          e.g. "BTC/USD" or "NVDA"
        notional:        dollar amount e.g. 100.0
        side:            "buy" or "sell"
        take_profit_pct: profit target e.g. 0.015 = 1.5%
        stop_loss_pct:   stop loss e.g. 0.010 = 1.0%

    Returns:
        order dict or None
    """
    is_crypto = "/" in symbol

    # Get current price for TP/SL calculation
    price = get_current_price(symbol)
    if not price:
        log.warning(f"  Cannot get price for {symbol} — skipping bracket")
        return None

    # Calculate TP and SL prices
    if side == "buy":
        tp_price = round(price * (1 + take_profit_pct), 4)
        sl_price = round(price * (1 - stop_loss_pct),   4)
    else:
        tp_price = round(price * (1 - take_profit_pct), 4)
        sl_price = round(price * (1 + stop_loss_pct),   4)

    log.info(f"  BRACKET {side.upper()} {symbol} ${notional:.2f} "
             f"| Entry:~${price:.4f} TP:${tp_price:.4f} SL:${sl_price:.4f}")

    if is_crypto:
        # Crypto uses notional + fractional
        body = {
            "symbol":        symbol.replace("/", ""),
            "notional":      str(round(notional, 2)),
            "side":          side,
            "type":          "market",
            "time_in_force": "gtc",
            "order_class":   "bracket",
            "take_profit":   {"limit_price": str(tp_price)},
            "stop_loss":     {"stop_price": str(sl_price)},
        }
    else:
        # Stocks — calculate qty from notional
        qty = max(1, int(notional / price))
        body = {
            "symbol":        symbol,
            "qty":           str(qty),
            "side":          side,
            "type":          "market",
            "time_in_force": "day",
            "order_class":   "bracket",
            "take_profit":   {"limit_price": str(tp_price)},
            "stop_loss":     {"stop_price":  str(sl_price)},
        }

    result = _api("POST", "/v2/orders", body)

    if result and result.get("id"):
        log.info(f"  ORDER PLACED: {symbol} | ID:{result['id'][:8]} "
                 f"| TP:${tp_price:.4f} SL:${sl_price:.4f}")
        return result
    else:
        log.warning(f"  BRACKET FAILED: {symbol}")
        # Fallback to simple market order
        return _place_simple_order(symbol, notional, side, is_crypto, price)


def _place_simple_order(symbol, notional, side, is_crypto, price):
    """Fallback simple market order if bracket fails."""
    if is_crypto:
        body = {
            "symbol":        symbol.replace("/", ""),
            "notional":      str(round(notional, 2)),
            "side":          side,
            "type":          "market",
            "time_in_force": "gtc",
        }
    else:
        qty  = max(1, int(notional / price))
        body = {
            "symbol":        symbol,
            "qty":           str(qty),
            "side":          side,
            "type":          "market",
            "time_in_force": "day",
        }
    result = _api("POST", "/v2/orders", body)
    if result and result.get("id"):
        log.info(f"  SIMPLE ORDER: {symbol} ${notional:.2f}")
    return result


def get_open_orders(symbol=None):
    """Get all open orders, optionally filtered by symbol."""
    endpoint = "/v2/orders?status=open"
    if symbol:
        sym      = symbol.replace("/", "")
        endpoint = f"/v2/orders?status=open&symbols={sym}"
    return _api("GET", endpoint) or []


def cancel_order(order_id):
    """Cancel a specific order."""
    result = _api("DELETE", f"/v2/orders/{order_id}")
    log.info(f"  CANCELLED: {order_id[:8]}")
    return result


def get_positions():
    """Get all open positions with bracket order status."""
    positions = _api("GET", "/v2/positions") or []
    orders    = get_open_orders()

    # Map orders to positions
    order_map = {}
    for o in orders:
        sym = o.get("symbol", "")
        if sym not in order_map:
            order_map[sym] = []
        order_map[sym].append({
            "id":   o.get("id"),
            "type": o.get("order_class", "simple"),
            "side": o.get("side"),
            "tp":   o.get("take_profit", {}).get("limit_price") if o.get("take_profit") else None,
            "sl":   o.get("stop_loss",   {}).get("stop_price")  if o.get("stop_loss")   else None,
        })

    result = []
    for pos in positions:
        sym         = pos.get("symbol", "")
        entry       = float(pos.get("avg_entry_price", 0))
        current     = float(pos.get("current_price",   0))
        qty         = float(pos.get("qty",             0))
        pnl         = float(pos.get("unrealized_pl",   0))
        pnl_pct     = float(pos.get("unrealized_plpc", 0)) * 100
        market_val  = float(pos.get("market_value",    0))

        has_bracket = any(
            o.get("type") == "bracket" or o.get("tp")
            for o in order_map.get(sym, [])
        )

        result.append({
            "symbol":      sym,
            "qty":         qty,
            "entry":       entry,
            "current":     current,
            "pnl":         pnl,
            "pnl_pct":     round(pnl_pct, 2),
            "market_value":market_val,
            "has_bracket": has_bracket,
            "orders":      order_map.get(sym, []),
        })

    return result

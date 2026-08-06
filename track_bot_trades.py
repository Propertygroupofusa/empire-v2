#!/usr/bin/env python3
"""
Trade execution tracker for Alpaca bot
Monitors actual trades, positions, and P&L
"""

import os
import json
from datetime import datetime
import requests

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "AKLHVXHXMFKZ62UOZVZ22TNwQE")
BASE_URL = "https://api.alpaca.markets"

def get_account_equity():
    """Get current portfolio value and cash"""
    headers = {"APCA-API-KEY-ID": ALPACA_API_KEY}
    try:
        resp = requests.get(f"{BASE_URL}/v2/account", headers=headers, timeout=5)
        if resp.status_code == 200:
            account = resp.json()
            return {
                "portfolio_value": float(account.get("portfolio_value", 0)),
                "cash": float(account.get("cash", 0)),
                "buying_power": float(account.get("buying_power", 0)),
                "equity": float(account.get("equity", 0)),
                "timestamp": datetime.now().isoformat()
            }
    except Exception as e:
        print(f"Error fetching account: {e}")
    return None

def get_positions():
    """Get current open positions"""
    headers = {"APCA-API-KEY-ID": ALPACA_API_KEY}
    try:
        resp = requests.get(f"{BASE_URL}/v2/positions", headers=headers, timeout=5)
        if resp.status_code == 200:
            positions = resp.json()
            return [{
                "symbol": p.get("symbol"),
                "qty": float(p.get("qty", 0)),
                "entry_price": float(p.get("avg_fill_price", 0)),
                "current_price": float(p.get("current_price", 0)),
                "unrealized_pl": float(p.get("unrealized_pl", 0)),
                "unrealized_plpc": float(p.get("unrealized_plpc", 0))
            } for p in positions]
    except Exception as e:
        print(f"Error fetching positions: {e}")
    return []

def get_trades_today():
    """Get trades executed today"""
    headers = {"APCA-API-KEY-ID": ALPACA_API_KEY}
    try:
        resp = requests.get(f"{BASE_URL}/v2/orders", headers=headers, params={"status": "filled", "limit": 50}, timeout=5)
        if resp.status_code == 200:
            orders = resp.json()
            return [{
                "symbol": o.get("symbol"),
                "side": o.get("side"),
                "qty": float(o.get("filled_qty", 0)),
                "price": float(o.get("filled_avg_price", 0)),
                "created_at": o.get("created_at"),
                "filled_at": o.get("filled_at")
            } for o in orders if o.get("status") == "filled"]
    except Exception as e:
        print(f"Error fetching trades: {e}")
    return []

if __name__ == "__main__":
    print("=== ALPACA BOT TRADE METRICS ===")
    print()

    # Account status
    account = get_account_equity()
    if account:
        print(f"Portfolio Value: ${account['portfolio_value']:,.2f}")
        print(f"Cash Available: ${account['cash']:,.2f}")
        print(f"Buying Power: ${account['buying_power']:,.2f}")
        print()

    # Positions
    positions = get_positions()
    if positions:
        print(f"Open Positions: {len(positions)}")
        for pos in positions:
            print(f"  {pos['symbol']}: {pos['qty']} shares @ ${pos['current_price']:.2f} (unrealized P/L: ${pos['unrealized_pl']:.2f})")
        print()
    else:
        print("No open positions")
        print()

    # Recent trades
    trades = get_trades_today()
    if trades:
        print(f"Trades Today: {len(trades)}")
        for trade in trades[:5]:
            print(f"  {trade['side'].upper()} {trade['qty']} {trade['symbol']} @ ${trade['price']:.2f}")
        print()
    else:
        print("No trades executed")

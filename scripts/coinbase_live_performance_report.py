"""
Real live trading performance for the Coinbase side, pulled from Coinbase's
own order history - same reasoning as scripts/live_performance_report.py
(the Alpaca version): a backtest measures a strategy, not execution. This
measures what actually happened to real money on the real account.

Read-only. GET requests only (accounts, orders history) - no order-placing
path at all.

Auth is Coinbase CDP's JWT scheme (same as crypto_coinbase_bot.py's
_build_jwt/_auth_headers) but reimplemented standalone here rather than
importing the bot module, since that module pulls in the app's database
layer (SQLAlchemy engine, measurement_system, network_config) that has no
reason to exist in a throwaway read-only report and would just be dead
weight / a needless failure surface in CI.
"""
import base64
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import jwt as pyjwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

COINBASE_API_KEY_NAME = os.getenv("COINBASE_API_KEY_NAME", "")
COINBASE_API_PRIVATE_KEY = os.getenv("COINBASE_API_PRIVATE_KEY", "").replace("\\n", "\n")
COINBASE_HOST = "api.coinbase.com"
COINBASE_BASE_URL = f"https://{COINBASE_HOST}"
DAYS = int(os.getenv("REPORT_DAYS", "1"))


def _load_signing_key():
    raw = COINBASE_API_PRIVATE_KEY.strip()
    if raw.startswith("-----BEGIN"):
        return serialization.load_pem_private_key(raw.encode(), password=None), "ES256"
    decoded = base64.b64decode(raw, validate=True)
    if len(decoded) != 64:
        raise ValueError(f"Ed25519 key must be 64 bytes decoded, got {len(decoded)}")
    return Ed25519PrivateKey.from_private_bytes(decoded[:32]), "EdDSA"


def _auth_headers(method, path):
    private_key, algorithm = _load_signing_key()
    now = int(time.time())
    payload = {
        "sub": COINBASE_API_KEY_NAME,
        "iss": "cdp",
        "nbf": now,
        "exp": now + 120,
        "uri": f"{method} {COINBASE_HOST}{path}",
    }
    headers = {"kid": COINBASE_API_KEY_NAME, "nonce": secrets.token_hex(16)}
    token = pyjwt.encode(payload, private_key, algorithm=algorithm, headers=headers)
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def get(path, params=None):
    url = COINBASE_BASE_URL + path
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(url, headers=_auth_headers("GET", path))
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        print(f"  ! HTTP {e.code} on {path}: {e.read()[:300].decode(errors='replace')}", file=sys.stderr)
    except Exception as e:
        print(f"  ! {path}: {e}", file=sys.stderr)
    return None


def money(x):
    return f"${x:,.2f}"


def main():
    if not COINBASE_API_KEY_NAME or not COINBASE_API_PRIVATE_KEY:
        print("FAIL: COINBASE_API_KEY_NAME / COINBASE_API_PRIVATE_KEY not set.", file=sys.stderr)
        return 1

    print("=" * 70)
    print("  LIVE CRYPTO PERFORMANCE - from Coinbase's own records")
    print(f"  window: last {DAYS} days")
    print("=" * 70)

    # USD balance (paginate through accounts, USD wallet may not be page 1)
    usd_balance = None
    cursor = None
    for _ in range(20):
        params = {"limit": 250}
        if cursor:
            params["cursor"] = cursor
        data = get("/api/v3/brokerage/accounts", params)
        if not data:
            break
        for acct in data.get("accounts", []):
            if acct.get("currency") == "USD":
                usd_balance = float(acct.get("available_balance", {}).get("value", 0))
        if not data.get("has_next"):
            break
        cursor = data.get("cursor")

    print(f"\n  ACCOUNT")
    print(f"    USD balance      {money(usd_balance) if usd_balance is not None else 'unknown'}")

    # Filled orders in the window
    start = (datetime.now(timezone.utc) - timedelta(days=DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    orders = []
    cursor = None
    for _ in range(20):
        params = {"order_status": "FILLED", "start_date": start, "limit": 250}
        if cursor:
            params["cursor"] = cursor
        data = get("/api/v3/brokerage/orders/historical/batch", params)
        if not data:
            break
        orders.extend(data.get("orders", []))
        if not data.get("has_next"):
            break
        cursor = data.get("cursor")

    if not orders:
        print(f"\n  ORDERS: 0 filled in this window")
        print("    No fills in this window - nothing further to measure.")
        return 0

    # Pair BUY/SELL fills per product (FIFO) into round trips, same approach
    # as the Alpaca report - real entry/exit pairs, not just a raw fill list.
    orders.sort(key=lambda o: o.get("created_time", ""))
    open_buys = defaultdict(list)
    trades = []
    for o in orders:
        product = o.get("product_id", "?")
        side = o.get("side", "").upper()
        filled_size = float(o.get("filled_size", 0) or 0)
        filled_value = float(o.get("filled_value", 0) or 0)
        if filled_size <= 0:
            continue
        avg_price = filled_value / filled_size
        created = o.get("created_time", "")

        if side == "BUY":
            open_buys[product].append({"qty": filled_size, "price": avg_price, "at": created})
        elif side == "SELL" and open_buys[product]:
            buy = open_buys[product].pop(0)
            qty = min(buy["qty"], filled_size)
            pnl = (avg_price - buy["price"]) * qty
            pnl_pct = ((avg_price - buy["price"]) / buy["price"] * 100) if buy["price"] > 0 else 0
            trades.append({
                "product": product, "entry": buy["price"], "exit": avg_price,
                "qty": qty, "entry_at": buy["at"], "exit_at": created,
                "pnl": pnl, "pnl_pct": pnl_pct,
            })

    total_pnl = sum(t["pnl"] for t in trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else float("inf") if gross_win > 0 else 0.0

    print(f"\n  ORDERS: {len(orders)} filled, {len(trades)} closed round-trips")
    print(f"\n  PERFORMANCE")
    print(f"    total P&L        {money(total_pnl)}")
    print(f"    win rate         {(len(wins)/len(trades)*100) if trades else 0:.1f}%  ({len(wins)}W / {len(losses)}L)")
    print(f"    profit factor    {profit_factor:.2f}")
    if wins:
        print(f"    avg win          {money(gross_win/len(wins))}")
    if losses:
        print(f"    avg loss         {money(gross_loss/len(losses))}")

    print(f"\n  ROUND TRIPS (most recent first)")
    for t in sorted(trades, key=lambda t: t["exit_at"], reverse=True)[:20]:
        sign = "+" if t["pnl"] >= 0 else ""
        print(f"    {t['product']:10} entry {money(t['entry']):>12}  exit {money(t['exit']):>12}  "
              f"P&L {sign}{money(t['pnl']):>10} ({sign}{t['pnl_pct']:.2f}%)")

    return 0


if __name__ == "__main__":
    sys.exit(main())

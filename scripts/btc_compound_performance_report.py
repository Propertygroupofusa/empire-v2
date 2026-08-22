"""
Real performance for crypto_btc_compound_bot.py, pulled from Coinbase's own
order history for BTC-USD - same reasoning as
scripts/coinbase_live_performance_report.py: this measures what the account
actually did, not a simulation or a guess. Since this bot only ever trades
BTC-USD (single position at a time), every BTC-USD round trip in the window
belongs to this strategy (or the one-off manual buy from
scripts/coinbase_manual_trade.py, if it's sold in the same window - shown
same as any other round trip, since it was a real trade on the same account).

Read-only. GET requests only - no order-placing path.

In addition to the usual P&L/win-rate/profit-factor numbers, this reports
PACE: the average time between a buy and its matching sell, and the average
% return per round trip - because "how fast could this grow" is a question
about realized cadence and realized return, not a guess. With few or no
completed round trips yet, it says so plainly instead of drawing a
conclusion from too little data.
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
PRODUCT_ID = "BTC-USD"
DAYS = int(os.getenv("REPORT_DAYS", "7"))


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
    print("  BTC COMPOUNDING BOT — real performance from Coinbase's own records")
    print(f"  window: last {DAYS} days | product: {PRODUCT_ID}")
    print("=" * 70)

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

    btc_price = None
    ticker = get(f"/api/v3/brokerage/products/{PRODUCT_ID}")
    if ticker:
        btc_price = float(ticker.get("price", 0)) or None

    print(f"\n  ACCOUNT")
    print(f"    USD balance      {money(usd_balance) if usd_balance is not None else 'unknown'}")
    print(f"    BTC price now    {money(btc_price) if btc_price is not None else 'unknown'}")

    start = (datetime.now(timezone.utc) - timedelta(days=DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    orders = []
    cursor = None
    for _ in range(20):
        params = {"order_status": "FILLED", "start_date": start, "product_id": PRODUCT_ID, "limit": 250}
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
        print("    No BTC-USD fills in this window yet - nothing to measure. Once the bot")
        print("    completes at least a few full buy-to-sell round trips, re-run this to")
        print("    see real win rate and pace instead of a guess.")
        return 0

    orders.sort(key=lambda o: o.get("created_time", ""))
    open_buys = []
    trades = []
    for o in orders:
        side = o.get("side", "").upper()
        filled_size = float(o.get("filled_size", 0) or 0)
        filled_value = float(o.get("filled_value", 0) or 0)
        if filled_size <= 0:
            continue
        avg_price = filled_value / filled_size
        created = o.get("created_time", "")

        if side == "BUY":
            open_buys.append({"qty": filled_size, "price": avg_price, "at": created})
        elif side == "SELL" and open_buys:
            buy = open_buys.pop(0)
            qty = min(buy["qty"], filled_size)
            pnl = (avg_price - buy["price"]) * qty
            pnl_pct = ((avg_price - buy["price"]) / buy["price"] * 100) if buy["price"] > 0 else 0
            try:
                entry_dt = datetime.fromisoformat(buy["at"].replace("Z", "+00:00"))
                exit_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                hours_held = (exit_dt - entry_dt).total_seconds() / 3600
            except Exception:
                hours_held = None
            trades.append({
                "entry": buy["price"], "exit": avg_price, "qty": qty,
                "entry_at": buy["at"], "exit_at": created,
                "pnl": pnl, "pnl_pct": pnl_pct, "hours_held": hours_held,
            })

    total_pnl = sum(t["pnl"] for t in trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else float("inf") if gross_win > 0 else 0.0

    print(f"\n  ORDERS: {len(orders)} filled, {len(trades)} closed round-trips, {len(open_buys)} still open (unmatched buy)")

    if len(trades) < 3:
        print(f"\n  NOTE: only {len(trades)} completed round-trip(s) so far - too few to draw any")
        print("    conclusion about win rate or pace from. Numbers below are the real")
        print("    trades that happened, not a statistically meaningful sample yet.")

    print(f"\n  PERFORMANCE")
    print(f"    total P&L        {money(total_pnl)}")
    print(f"    win rate         {(len(wins)/len(trades)*100) if trades else 0:.1f}%  ({len(wins)}W / {len(losses)}L)")
    print(f"    profit factor    {profit_factor:.2f}")
    if wins:
        print(f"    avg win          {money(gross_win/len(wins))}  ({sum(t['pnl_pct'] for t in wins)/len(wins):+.2f}% avg)")
    if losses:
        print(f"    avg loss         {money(gross_loss/len(losses))}  ({sum(t['pnl_pct'] for t in losses)/len(losses):+.2f}% avg)")

    held_times = [t["hours_held"] for t in trades if t["hours_held"] is not None]
    if held_times:
        avg_hours = sum(held_times) / len(held_times)
        print(f"\n  PACE")
        print(f"    avg time per round-trip   {avg_hours:.1f} hours ({avg_hours/24:.2f} days)")
        avg_pct = sum(t["pnl_pct"] for t in trades) / len(trades)
        print(f"    avg return per round-trip {avg_pct:+.2f}%")
        if avg_hours > 0 and len(trades) >= 3:
            trips_per_day = 24 / avg_hours
            print(f"    -> roughly {trips_per_day:.2f} round-trips/day at this pace (from {len(trades)} real trades, not a forecast)")

    print(f"\n  ROUND TRIPS (most recent first)")
    for t in sorted(trades, key=lambda t: t["exit_at"], reverse=True)[:20]:
        sign = "+" if t["pnl"] >= 0 else ""
        held = f"{t['hours_held']:.1f}h" if t["hours_held"] is not None else "?"
        print(f"    entry {money(t['entry']):>12}  exit {money(t['exit']):>12}  "
              f"P&L {sign}{money(t['pnl']):>10} ({sign}{t['pnl_pct']:.2f}%)  held {held}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

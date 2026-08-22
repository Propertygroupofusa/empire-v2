"""
One-shot manual Coinbase trade: buy a fixed USD amount of BTC now, then
later check whether to sell for a profit. This places REAL orders on your
REAL Coinbase account - it is not part of crypto_coinbase_bot.py's
automated strategy, and does not touch that bot's state/database.

Why two steps instead of one: nothing in this sandbox can stay running
for 3 days to watch the position, so "buy now, sell in 3 days if
profitable" has to be two separate runs of this script, with the position
tracked in a small local JSON file in between. You run `buy` once today,
then run `check-sell` again in ~3 days (re-run it as many times as you
want in the meantime - it only sells when there's actually a profit).

Auth is Coinbase's CDP JWT scheme, same reimplementation used in
scripts/coinbase_live_performance_report.py.

USAGE (run this on a machine/shell that has your real Coinbase API
credentials set - e.g. a Railway shell, or your own machine with
COINBASE_API_KEY_NAME / COINBASE_API_PRIVATE_KEY exported):

    # step 1, today: place the buy
    python scripts/coinbase_manual_trade.py buy --usd 30

    # step 2, in ~3 days: sell only if it's currently profitable
    python scripts/coinbase_manual_trade.py check-sell

    # step 2, forced: sell now regardless of P&L (e.g. you want out early)
    python scripts/coinbase_manual_trade.py check-sell --force

State is stored next to this script in coinbase_manual_trade_state.json.
Only one open position is tracked at a time - `buy` refuses to run again
while a position is already open.
"""
import argparse
import base64
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt as pyjwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

COINBASE_API_KEY_NAME = os.getenv("COINBASE_API_KEY_NAME", "")
COINBASE_API_PRIVATE_KEY = os.getenv("COINBASE_API_PRIVATE_KEY", "").replace("\\n", "\n")
COINBASE_HOST = "api.coinbase.com"
COINBASE_BASE_URL = f"https://{COINBASE_HOST}"
PRODUCT_ID = "BTC-USD"
MIN_HOLD_DAYS = 3
STATE_FILE = Path(__file__).parent / "coinbase_manual_trade_state.json"


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


def request(method, path, body=None):
    url = COINBASE_BASE_URL + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=_auth_headers(method, path))
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        print(f"! HTTP {e.code} on {method} {path}: {e.read()[:500].decode(errors='replace')}", file=sys.stderr)
        return None


def get_current_price():
    data = request("GET", f"/api/v3/brokerage/products/{PRODUCT_ID}")
    if not data:
        return None
    return float(data["price"])


def load_state():
    if not STATE_FILE.exists():
        return None
    return json.loads(STATE_FILE.read_text())


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def cmd_buy(usd_amount):
    if not COINBASE_API_KEY_NAME or not COINBASE_API_PRIVATE_KEY:
        print("FAIL: COINBASE_API_KEY_NAME / COINBASE_API_PRIVATE_KEY not set.", file=sys.stderr)
        return 1

    existing = load_state()
    if existing and existing.get("status") == "open":
        print(f"FAIL: a position is already open ({existing}). Run check-sell first, "
              f"or delete {STATE_FILE.name} if you know that's stale.", file=sys.stderr)
        return 1

    client_order_id = str(uuid.uuid4())
    body = {
        "client_order_id": client_order_id,
        "product_id": PRODUCT_ID,
        "side": "BUY",
        "order_configuration": {"market_market_ioc": {"quote_size": str(usd_amount)}},
    }
    print(f"Placing market BUY for ${usd_amount} of {PRODUCT_ID}...")
    resp = request("POST", "/api/v3/brokerage/orders", body)
    if not resp or not resp.get("success"):
        print(f"FAIL: order not accepted: {resp}", file=sys.stderr)
        return 1

    order_id = resp["success_response"]["order_id"]

    # Poll briefly for the fill to settle so we can record a real entry price.
    filled = None
    for _ in range(10):
        time.sleep(1)
        detail = request("GET", f"/api/v3/brokerage/orders/historical/{order_id}")
        if detail and detail.get("order", {}).get("status") in ("FILLED", "DONE"):
            filled = detail["order"]
            break

    if not filled:
        print(f"Order {order_id} placed but not confirmed filled yet - check Coinbase directly.")
        return 1

    btc_qty = float(filled.get("filled_size", 0))
    usd_spent = float(filled.get("filled_value", 0))
    entry_price = usd_spent / btc_qty if btc_qty else 0

    state = {
        "status": "open",
        "order_id": order_id,
        "product_id": PRODUCT_ID,
        "btc_qty": btc_qty,
        "usd_spent": usd_spent,
        "entry_price": entry_price,
        "bought_at": datetime.now(timezone.utc).isoformat(),
        "eligible_to_sell_at": (datetime.now(timezone.utc) + timedelta(days=MIN_HOLD_DAYS)).isoformat(),
    }
    save_state(state)
    print(f"BOUGHT {btc_qty:.8f} BTC for ${usd_spent:.2f} (entry price ${entry_price:,.2f})")
    print(f"Will be eligible for the profit-check sell after {state['eligible_to_sell_at']}")
    return 0


def cmd_check_sell(force):
    state = load_state()
    if not state or state.get("status") != "open":
        print("No open position tracked - nothing to check. Run `buy` first.")
        return 0
    if not COINBASE_API_KEY_NAME or not COINBASE_API_PRIVATE_KEY:
        print("FAIL: COINBASE_API_KEY_NAME / COINBASE_API_PRIVATE_KEY not set.", file=sys.stderr)
        return 1

    eligible_at = datetime.fromisoformat(state["eligible_to_sell_at"])
    now = datetime.now(timezone.utc)
    if now < eligible_at and not force:
        remaining = eligible_at - now
        print(f"Still within the 3-day hold ({remaining} left). Not selling. "
              f"Re-run later, or pass --force to sell now regardless of P&L.")
        return 0

    price = get_current_price()
    if price is None:
        print("FAIL: could not fetch current BTC price.", file=sys.stderr)
        return 1

    current_value = state["btc_qty"] * price
    pnl = current_value - state["usd_spent"]
    pnl_pct = (pnl / state["usd_spent"] * 100) if state["usd_spent"] else 0

    print(f"Entry: ${state['entry_price']:,.2f}   Current: ${price:,.2f}   "
          f"Unrealized P&L: {'+' if pnl >= 0 else ''}${pnl:,.2f} ({pnl_pct:+.2f}%)")

    if pnl <= 0 and not force:
        print("Not in profit yet - holding. Re-run this command again later to re-check.")
        return 0

    client_order_id = str(uuid.uuid4())
    body = {
        "client_order_id": client_order_id,
        "product_id": PRODUCT_ID,
        "side": "SELL",
        "order_configuration": {"market_market_ioc": {"base_size": f"{state['btc_qty']:.8f}"}},
    }
    reason = "forced" if pnl <= 0 else "profit target hit"
    print(f"Placing market SELL for {state['btc_qty']:.8f} BTC ({reason})...")
    resp = request("POST", "/api/v3/brokerage/orders", body)
    if not resp or not resp.get("success"):
        print(f"FAIL: sell order not accepted: {resp}", file=sys.stderr)
        return 1

    state["status"] = "closed"
    state["sold_at"] = now.isoformat()
    state["exit_price"] = price
    state["realized_pnl"] = pnl
    save_state(state)
    print(f"SOLD. Realized P&L: {'+' if pnl >= 0 else ''}${pnl:,.2f} ({pnl_pct:+.2f}%)")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_buy = sub.add_parser("buy", help="Place the initial market buy")
    p_buy.add_argument("--usd", type=float, required=True, help="USD amount to spend, e.g. 30")

    p_sell = sub.add_parser("check-sell", help="Sell the tracked position if it's currently profitable")
    p_sell.add_argument("--force", action="store_true", help="Sell now even if not in profit / hold period not elapsed")

    args = parser.parse_args()
    if args.command == "buy":
        return cmd_buy(args.usd)
    return cmd_check_sell(args.force)


if __name__ == "__main__":
    sys.exit(main())

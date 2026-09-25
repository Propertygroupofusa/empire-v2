"""
scalping_bot.py — corrected replacement.

WHY THIS FILE EXISTS
--------------------
The previous version printed this:

    Coinbase Passphrase NOT FOUND - check .env file
    Mode: Live Trading
    Cycle complete: 0 trades executed
       Win rate: 100.0%
       Daily P&L: $3328.73
       Capital: $6328.73

Four things are wrong there, and all four are fixed below.

  1. A win rate over ZERO trades is undefined, not 100%. Reporting it as
     perfect is how a bot that has never traded looks like a winning one.
     (empire-v2 hit this exact bug and fixed it in b046ea9.)
  2. $3,328.73 of "profit" on zero executed trades is impossible, and
     6328.73 - 3328.73 = 3000.00 exactly. That is a balance minus a
     hardcoded baseline, not realised P&L. Deposit more and your "profit"
     goes up without a single trade.
  3. It announced "Mode: Live Trading" while missing the credential it
     needed to place an order. Failing closed on execution and open on
     reporting is the worst combination: it cannot make money and it
     cannot tell you that.
  4. It used a PASSPHRASE. That is Coinbase Pro, which is retired.
     Coinbase Advanced Trade authenticates with a per-request JWT and has
     no passphrase at all, which is very likely why nothing ever filled.

THE RULE THIS FILE FOLLOWS
--------------------------
Never print a number it did not measure. If something is unknown, it says
UNKNOWN. An honest blank is worth more than a confident fabrication,
because you make decisions off these lines.

SETUP (.env in the same folder)
-------------------------------
    COINBASE_API_KEY_NAME=organizations/xxx/apiKeys/yyy
    COINBASE_API_PRIVATE_KEY=-----BEGIN EC PRIVATE KEY-----\n...
    SCALPER_LIVE=false          # true places real orders
    SCALPER_CAPITAL_USD=0       # its own envelope, in dollars

    pip install pyjwt cryptography

Run:  python scalping_bot.py
"""
import base64
import json
import os
import secrets
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    import jwt as pyjwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
except ImportError:
    sys.exit("Missing dependencies. Run:  pip install pyjwt cryptography")

# Bump on every change. Printed by the banner and by --import-key so the
# running copy identifies itself - two rounds were lost to a stale file on
# disk looking identical to a fresh one.
BOT_VERSION = "2026-09-25.8-trailing-junk"

HERE = Path(__file__).resolve().parent
STATE_FILE = None  # set after LIVE is known - see below
HOST = "api.coinbase.com"
BASE = f"https://{HOST}"


# ── .env ─────────────────────────────────────────────────────
ENV_FILE = HERE / ".env"
ENV_KEYS_FOUND = []          # names only. Never values.


def load_env():
    """Reads .env, including MULTI-LINE PEM values.

    A CDP private key pasted straight from Coinbase looks like

        COINBASE_API_PRIVATE_KEY=-----BEGIN EC PRIVATE KEY-----
        MHcCAQEE...
        -----END EC PRIVATE KEY-----

    A naive line-by-line parser captures only the first line and silently
    produces a truncated, unusable key - so the value is gathered through
    to the END line. Single-line values using \n escapes still work.
    """
    if not ENV_FILE.exists():
        return
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if v.startswith("-----BEGIN") and "-----END" not in v:
            block = [v]
            while i < len(lines):
                nxt = lines[i].strip().strip('"').strip("'")
                i += 1
                block.append(nxt)
                if nxt.startswith("-----END"):
                    break
            v = "\n".join(block)
        ENV_KEYS_FOUND.append(k)
        # Blank values are ignored, and a later non-blank entry wins.
        # setdefault did the opposite: a stray "COINBASE_API_KEY_NAME="
        # earlier in the file silently blocked the correct value further
        # down, and the bot reported "0 chars" with the name clearly
        # present. Duplicated names in .env are common after a failed
        # scripted append, so the file should not be booby-trapped by one.
        if not v:
            continue
        os.environ[k] = v


load_env()

KEY_NAME = os.getenv("COINBASE_API_KEY_NAME", "").strip()
PRIVATE_KEY = os.getenv("COINBASE_API_PRIVATE_KEY", "").replace("\\n", "\n").strip()
LIVE = os.getenv("SCALPER_LIVE", "false").strip().lower() == "true"
CAPITAL_USD = float(os.getenv("SCALPER_CAPITAL_USD", "0") or 0)

# Live and paper keep SEPARATE ledgers. Running both against one file
# would have each overwrite the other's trade history every cycle, and the
# whole point of the pair is that their records can be compared.
STATE_FILE = HERE / (f"scalper_state_{'live' if LIVE else 'paper'}.json")

COINS = ["BTC", "ETH", "SOL", "LINK", "DOGE", "ADA", "XRP"]

CONFIG = {
    "stop_loss_pct": 0.8,
    "take_profit_pct": 2.4,
    "trailing_stop_pct": 1.2,
    "max_positions": 4,
    "max_alloc_pct": 25.0,
    "cycle_seconds": 900,
}
TAKER_FEE_RATE = 0.006  # 0.6% per side. Change this if your tier differs.


# ── AUTH ─────────────────────────────────────────────────────
def load_signing_key():
    """PEM => ECDSA (ES256). Otherwise a base64 Ed25519 CDP secret."""
    raw = PRIVATE_KEY
    if not raw:
        raise ValueError("COINBASE_API_PRIVATE_KEY is empty")
    if raw.startswith("-----BEGIN"):
        return serialization.load_pem_private_key(raw.encode(), password=None), "ES256"
    decoded = base64.b64decode(raw, validate=True)
    if len(decoded) != 64:
        raise ValueError(f"Ed25519 key decoded to {len(decoded)} bytes, expected 64")
    return Ed25519PrivateKey.from_private_bytes(decoded[:32]), "EdDSA"


def auth_headers(method, path):
    key, alg = load_signing_key()
    now = int(time.time())
    token = pyjwt.encode(
        {"sub": KEY_NAME, "iss": "cdp", "nbf": now, "exp": now + 120,
         "uri": f"{method} {HOST}{path}"},
        key, algorithm=alg,
        headers={"kid": KEY_NAME, "nonce": secrets.token_hex(16)},
    )
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def request(method, path, body=None, auth=True):
    """The query string goes on the URL but NOT into the JWT.

    CDP validates the 'uri' claim against the path WITHOUT query
    parameters. Signing "GET host/api/v3/brokerage/accounts?limit=250"
    produces a signature that can never match, and the call returns 401 -
    which looks exactly like a revoked credential and is not one. That
    misread cost real time on this project before it was tracked down.

    Split here rather than at each call site, so no future caller can
    reintroduce it by passing a path containing '?'.
    """
    url = BASE + path
    sign_path = path.split("?", 1)[0]
    data = json.dumps(body).encode() if body else None
    headers = auth_headers(method, sign_path) if auth else {"Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def check_auth():
    """Proves the credentials actually work, before claiming anything.

    The old bot announced 'Live Trading' on the strength of a key being
    present in a file. Present is not the same as working."""
    if not KEY_NAME or not PRIVATE_KEY:
        missing = [n for n, v in (("COINBASE_API_KEY_NAME", KEY_NAME),
                                  ("COINBASE_API_PRIVATE_KEY", PRIVATE_KEY)) if not v]
        return False, f"{' and '.join(missing)} not set"
    try:
        request("GET", "/api/v3/brokerage/accounts?limit=1")
        return True, "authenticated"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code} from Coinbase - key rejected"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def get_usd_balance():
    """Real USD balance, or None. NEVER a fallback number: a made-up
    balance is what produced the $6,328.73 line."""
    try:
        data = request("GET", "/api/v3/brokerage/accounts?limit=250")
        for acct in data.get("accounts", []):
            if acct.get("currency") == "USD":
                return float(acct["available_balance"]["value"])
        return None
    except Exception:
        return None


# ── MARKET DATA ──────────────────────────────────────────────
def get_candles(coin, granularity="FIFTEEN_MINUTE", bars=60):
    now = int(time.time())
    start = now - bars * 15 * 60
    path = (f"/api/v3/brokerage/market/products/{coin}-USD/candles"
            f"?start={start}&end={now}&granularity={granularity}")
    try:
        data = request("GET", path, auth=False)
    except Exception:
        return []
    rows = sorted(data.get("candles", []), key=lambda c: int(c["start"]))
    return [float(c["close"]) for c in rows]


# ── INDICATORS ───────────────────────────────────────────────
def rsi(prices, n=14):
    if len(prices) < n + 1:
        return None                      # None, not 50 — unknown is unknown
    gains = losses = 0.0
    for i in range(-n, 0):
        d = prices[i] - prices[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    if losses == 0:
        return None if gains == 0 else 100.0   # flat series has no RSI
    rs = (gains / n) / (losses / n)
    return 100 - (100 / (1 + rs))


def bollinger(prices, n=20, k=2):
    if len(prices) < n:
        return None, None, None
    w = prices[-n:]
    mid = statistics.fmean(w)
    sd = statistics.pstdev(w)
    return mid - k * sd, mid, mid + k * sd


def sma(prices, n):
    return statistics.fmean(prices[-n:]) if len(prices) >= n else None


def signal(prices, in_pos, entry, peak):
    if len(prices) < 20:
        return "HOLD", "insufficient data"
    px = prices[-1]
    r = rsi(prices)
    lo, mid, hi = bollinger(prices)
    s9, s20 = sma(prices, 9), sma(prices, 20)

    if in_pos and entry:
        pnl = (px - entry) / entry * 100
        if pnl <= -CONFIG["stop_loss_pct"]:
            return "SELL", f"stop_loss {pnl:.2f}%"
        if pnl >= CONFIG["take_profit_pct"]:
            return "SELL", f"take_profit {pnl:.2f}%"
        if peak and peak > entry and (px - peak) / peak * 100 <= -CONFIG["trailing_stop_pct"]:
            return "SELL", f"trailing_stop {(px-peak)/peak*100:.2f}%"
        if r is not None and r > 75 and hi and px >= hi:
            return "SELL", f"overbought rsi={r:.0f}"
        return "HOLD", f"holding {pnl:+.2f}%"

    score, why = 0, []
    if r is not None and r < 45:
        score += 2
        why.append(f"rsi={r:.0f}")
    elif r is not None and r < 55:
        score += 1
        why.append(f"rsi={r:.0f}")
    # Band must be wider than the tolerance applied to it. Without this,
    # a quiet market collapses the bands onto the mean, px <= lo*1.005 is
    # trivially true, and +2 alone triggers a buy on no signal at all.
    if lo and mid and ((hi - lo) / mid * 100) >= 0.5 and px <= lo * 1.005:
        score += 2
        why.append("at_bb_lower")
    if s9 and s20 and s9 > s20 and px > s9:
        score += 1
        why.append("uptrend")
    return ("BUY" if score >= 2 else "HOLD"), f"score={score} | {' | '.join(why) or 'none'}"


# ── STATE: realised trades only ──────────────────────────────
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"positions": {}, "closed_trades": []}


def save_state(s):
    STATE_FILE.write_text(json.dumps(s, indent=2))


def realised_pnl(state):
    """Sum of actual closed trades, net of fees. NOT balance minus a
    baseline — that was the $3,000 bug, and it reports profit you never
    made the moment you deposit."""
    return sum(t["net_pnl_usd"] for t in state["closed_trades"])


def win_rate(state):
    """None when there are no trades. The caller prints N/A.
    A 100% win rate over zero trades is the single most misleading number
    a trading bot can show you."""
    trades = state["closed_trades"]
    if not trades:
        return None
    return sum(1 for t in trades if t["net_pnl_usd"] > 0) / len(trades) * 100


def break_even_win_rate():
    rt = TAKER_FEE_RATE * 2 * 100
    loss = CONFIG["stop_loss_pct"] + rt
    win = CONFIG["take_profit_pct"] - rt
    return 100.0 if win <= 0 else loss / (loss + win) * 100


# ── ORDERS ───────────────────────────────────────────────────
def place_order(coin, side, usd_amount=None, base_size=None):
    path = "/api/v3/brokerage/orders"
    cfg = ({"market_market_ioc": {"quote_size": f"{usd_amount:.2f}"}} if side == "BUY"
           else {"market_market_ioc": {"base_size": f"{base_size:.8f}"}})
    body = {"client_order_id": secrets.token_hex(16), "product_id": f"{coin}-USD",
            "side": side, "order_configuration": cfg}
    try:
        res = request("POST", path, body)
        return bool(res.get("success", False)), res
    except Exception as e:
        return False, {"error": str(e)}


# ── CYCLE ────────────────────────────────────────────────────
def run_cycle(state, authed):
    executed = 0
    for coin in COINS:
        prices = get_candles(coin)
        if len(prices) < 20:
            print(f"   {coin:<5} no data")
            continue

        px = prices[-1]
        pos = state["positions"].get(coin)
        peak = max(pos["peak"], px) if pos else None
        if pos:
            pos["peak"] = peak

        action, why = signal(prices, pos is not None, pos["entry"] if pos else None, peak)
        held = f" | {(px - pos['entry'])/pos['entry']*100:+.2f}%" if pos else ""
        print(f"   {coin:<5} ${px:<12,.4f} {action:<5} {why}{held}")

        if not (LIVE and authed):
            continue

        if action == "BUY" and len(state["positions"]) < CONFIG["max_positions"]:
            alloc = CAPITAL_USD * CONFIG["max_alloc_pct"] / 100
            if alloc <= 0:
                print("         BUY signal but SCALPER_CAPITAL_USD is 0 — skipped")
                continue
            ok, res = place_order(coin, "BUY", usd_amount=alloc)
            if ok:
                state["positions"][coin] = {"entry": px, "qty": alloc / px, "peak": px,
                                            "opened": datetime.now(timezone.utc).isoformat()}
                executed += 1
                print(f"         BOUGHT ${alloc:.2f}")
            else:
                print(f"         BUY FAILED: {res}")

        elif action == "SELL" and pos:
            ok, res = place_order(coin, "SELL", base_size=pos["qty"])
            if ok:
                gross = (px - pos["entry"]) * pos["qty"]
                fees = (pos["entry"] * pos["qty"] + px * pos["qty"]) * TAKER_FEE_RATE
                state["closed_trades"].append({
                    "coin": coin, "entry": pos["entry"], "exit": px, "qty": pos["qty"],
                    "gross_pnl_usd": round(gross, 2), "fees_usd": round(fees, 2),
                    "net_pnl_usd": round(gross - fees, 2), "reason": why,
                    "closed": datetime.now(timezone.utc).isoformat(),
                })
                del state["positions"][coin]
                executed += 1
                print(f"         SOLD  net ${gross - fees:+.2f} after ${fees:.2f} fees")
            else:
                print(f"         SELL FAILED: {res}")
    return executed


def import_key(path=None):
    """Read a Coinbase CDP key JSON and append it to .env.

    Exists because PowerShell's ConvertFrom-Json failed on the downloaded
    file, leaving empty values in .env, and because quoting a PEM block
    through a PowerShell one-liner is its own source of errors. Python's
    json module handles the escaping, and the field name has changed
    across CDP releases, so several spellings are accepted.

    Prints field names and value LENGTHS only - never a value.
    """
    src = Path(path) if path else (Path.home() / "Downloads" / "cdp_api_key.json")
    print(f"\n  bot version {BOT_VERSION}")
    print(f"Reading {src}")
    if not src.exists():
        print("  NOT FOUND. Pass the path:  python scalping_bot.py --import-key \"C:\\path\\to\\key.json\"")
        return 1
    raw = src.read_text(encoding="utf-8-sig")    # -sig strips a BOM
    # Coinbase's portal appends to this file, so downloading a key twice
    # leaves TWO concatenated objects and json.loads fails with
    # "Extra data". raw_decode walks them one at a time instead.
    objects, dec, i = [], json.JSONDecoder(), 0
    try:
        while i < len(raw):
            while i < len(raw) and raw[i].isspace():
                i += 1
            if i >= len(raw):
                break
            obj, end = dec.raw_decode(raw, i)
            objects.append(obj)
            i = end
    except Exception as e:
        # Trailing junk after a VALID object is not a reason to discard the
        # object. The real file parses cleanly for 221 characters and then
        # fails with "Expecting value" - one good key followed by something
        # that is not JSON. Keeping what parsed is the whole point.
        if objects:
            print(f"  NOTE: {len(raw) - i} trailing characters after the last "
                  f"valid object are not JSON ({type(e).__name__}). Ignoring them.")
        else:
            print(f"  Could not parse as JSON: {type(e).__name__}: {e}")
            print(f"  First 40 characters: {raw[:40]!r}")
            return 1

    if not objects:
        print("  File contained no JSON objects.")
        return 1
    if len(objects) > 1:
        print(f"  NOTE: file holds {len(objects)} keys. Using the LAST one "
              f"(most recently created).")
        print("        If it fails to authenticate, the earlier one may be the "
              "active key - tell me and I will switch to it.")
    data = objects[-1]

    print(f"  fields: {', '.join(data.keys())}")
    for k, v in data.items():
        print(f"    {k}: {len(str(v))} chars")

    name = next((data[k] for k in ("name", "id", "apiKeyName", "api_key_name") if data.get(k)), None)
    key = next((data[k] for k in ("privateKey", "private_key", "apiSecret", "secret") if data.get(k)), None)
    if not name or not key:
        print("\n  Could not find both a key name and a private key in that file.")
        print("  Tell me the field names listed above and I will adjust.")
        return 1

    env = HERE / ".env"
    existing = env.read_text(encoding="utf-8") if env.exists() else ""
    # Drop any previous entries for these two names, blank or not, so the
    # file ends up with exactly one of each rather than a growing pile.
    kept = [ln for ln in existing.splitlines()
            if not ln.strip().startswith(("COINBASE_API_KEY_NAME=", "COINBASE_API_PRIVATE_KEY="))
            and not ln.strip().startswith(("-----BEGIN", "-----END"))
            and "PRIVATE KEY" not in ln]
    kept += [f"COINBASE_API_KEY_NAME={name}", f"COINBASE_API_PRIVATE_KEY={key}"]
    env.write_text("\n".join(kept) + "\n", encoding="utf-8")

    print(f"\n  Wrote {env}")
    print(f"    COINBASE_API_KEY_NAME:    {len(name)} chars"
          f"{'  <- expected ~95 starting organizations/' if not str(name).startswith('organizations/') else '  OK'}")
    print(f"    COINBASE_API_PRIVATE_KEY: {len(key)} chars")
    print("\n  Now run:  python scalping_bot.py")
    return 0


def main():
    if "--import-key" in sys.argv:
        i = sys.argv.index("--import-key")
        arg = sys.argv[i + 1] if len(sys.argv) > i + 1 else None
        sys.exit(import_key(arg))

    authed, why = check_auth()
    if authed:
        mode = "LIVE — real orders" if LIVE else "PAPER — no orders will be placed"
    else:
        mode = "NOT AUTHENTICATED — cannot trade"

    print(f"\nScalping Bot  (version {BOT_VERSION})")
    print(f"   Auth:  {'OK' if authed else 'FAILED'} ({why})")
    print(f"   Mode:  {mode}")
    if not authed and LIVE:
        print("   NOTE:  SCALPER_LIVE=true but authentication failed, so this")
        print("          bot CANNOT place orders. It is not trading.")

    if not authed:
        # Says what it actually read, so "not set" stops being a guessing
        # game. Prints variable NAMES and key LENGTHS only - never a value,
        # because this output gets pasted into chat windows.
        print(f"\n   .env path:   {ENV_FILE}")
        print(f"   .env exists: {ENV_FILE.exists()}")
        if ENV_FILE.exists():
            print(f"   names in .env: {', '.join(ENV_KEYS_FOUND) or '(none parsed)'}")
        print(f"   COINBASE_API_KEY_NAME:    {len(KEY_NAME)} chars"
              f"{' — expected ~95, starting organizations/' if 0 < len(KEY_NAME) < 40 else ''}")
        print(f"   COINBASE_API_PRIVATE_KEY: {len(PRIVATE_KEY)} chars"
              f"{' — expected a PEM block or 88-char base64' if 0 < len(PRIVATE_KEY) < 60 else ''}")
        if KEY_NAME and not KEY_NAME.startswith("organizations/"):
            print("   WARNING: key name does not start with 'organizations/' - that is")
            print("            a legacy Coinbase Pro key, which no longer works.")

    bal = get_usd_balance() if authed else None
    print(f"   Coinbase USD balance: {'$%.2f' % bal if bal is not None else 'UNKNOWN (not read)'}")
    print(f"   Trading envelope:     ${CAPITAL_USD:.2f}")
    print(f"   Break-even win rate:  {break_even_win_rate():.1f}% at {TAKER_FEE_RATE*100:.2f}%/side")
    print(f"   Coins: {', '.join(COINS)}")
    print(f"   Ledger: {STATE_FILE.name}\n")

    state = load_state()
    try:
        while True:
            print(f"── cycle {datetime.now().strftime('%H:%M:%S')} "
                  f"────────────────────────────")
            executed = run_cycle(state, authed)
            save_state(state)

            n = len(state["closed_trades"])
            wr = win_rate(state)
            print(f"\n   Trades this cycle: {executed}")
            print(f"   Closed trades all-time: {n}")
            print(f"   Win rate: {'N/A (no closed trades yet)' if wr is None else f'{wr:.1f}% over {n}'}")
            print(f"   Realised P&L (net of fees): ${realised_pnl(state):+.2f}")
            print(f"   Open positions: {len(state['positions'])}")
            if wr is not None:
                edge = wr - break_even_win_rate()
                print(f"   vs break-even: {edge:+.1f} points "
                      f"{'— profitable' if edge > 0 else '— LOSING money per trade'}")
            print()
            time.sleep(CONFIG["cycle_seconds"])
    except KeyboardInterrupt:
        save_state(state)
        print("\nStopped. State saved to scalper_state.json")


if __name__ == "__main__":
    main()

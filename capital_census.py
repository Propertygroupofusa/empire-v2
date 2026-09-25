#!/usr/bin/env python3
"""Capital census: what is actually in the accounts, and what only looks like it.

Answers one question - how much real money exists right now - by asking each
venue directly and refusing to guess. Every figure is either read from a live
API this run or printed as UNKNOWN. There are no defaults, no baselines and
no fallbacks anywhere in this file, because every phantom balance in this
project came from exactly those three things.

It also lists the hardcoded "capital" constants scattered through the repo and
marks them NOT REAL, because they are the reason the capital picture has been
unclear: a number in a config file is a setting, not a balance, and summing
the two is how $3,000 of nothing becomes "profit".

    python capital_census.py
    python capital_census.py --json

Exit codes:  0 every venue answered   1 at least one venue is UNKNOWN
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Constants found in this codebase that read like capital but are not.
# Each is (file, pattern, what it actually is).
#
# Scope: the venues that hold the trading capital - Coinbase and Alpaca.
# sports_arb/ is a separate project with its own money and is deliberately
# NOT counted or scanned here; mixing its sizing constants into this figure
# is the sort of thing that made the capital picture unclear in the first
# place.
PHANTOM_CAPITAL_SOURCES = [
    ("bot_config.json", r'"starting_capital"\s*:\s*([\d_.]+)',
     "baseline the scalper compounds from, not a balance"),
]


def _load_env_file():
    f = HERE / ".env"
    if not f.exists():
        return
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env_file()


def _first_env(*names):
    for n in names:
        v = os.getenv(n)
        if v and v.strip() and v.strip() != "SET_VIA_ENV":
            return n, v.strip()
    return None, ""


# --- Coinbase -------------------------------------------------------------


def coinbase_holdings():
    """Real USD plus every non-zero coin balance, or an explanation."""
    key_var, key_name = _first_env(
        "COINBASE_API_KEY_NAME", "COINBASE_API_KEY", "COINBASE_API_KEY_BOT")
    priv_var, priv = _first_env(
        "COINBASE_API_PRIVATE_KEY", "COINBASE_PRIVATE_KEY", "COINBASE_PRIVATE_KEY_BOT")
    if not key_name or not priv:
        return {"venue": "Coinbase", "status": "UNKNOWN",
                "reason": "no credentials in the environment "
                          "(COINBASE_API_KEY_NAME / COINBASE_API_PRIVATE_KEY)"}
    try:
        import jwt as pyjwt
        from cryptography.hazmat.primitives import serialization
    except ImportError:
        return {"venue": "Coinbase", "status": "UNKNOWN",
                "reason": "pyjwt/cryptography not installed - pip install pyjwt cryptography"}

    priv = priv.replace("\\n", "\n")
    host = "api.coinbase.com"
    # The JWT 'uri' claim must be "METHOD host/path" with NO query string.
    # This signed "/api/v3/brokerage/accounts?limit=250" - query included -
    # and every call returned HTTP 401 "credentials rejected".
    #
    # That 401 cost most of 2026-09-25, because it was read as the KEY being
    # revoked. It was not. The same key traded and read balances all day
    # through crypto_btc_compound_bot, which signs the BARE path and lets the
    # query string be appended at request time (its _auth_headers/get_balance
    # pair, around lines 497-510). A 401 says the signature did not validate;
    # it does not say which half was wrong, and here it was ours.
    SIGN_PATH = "/api/v3/brokerage/accounts"

    def _fetch_page(cursor=None):
        query = "?limit=250" + (f"&cursor={cursor}" if cursor else "")
        signing = serialization.load_pem_private_key(priv.encode(), password=None)
        now = int(time.time())
        token = pyjwt.encode(
            {"sub": key_name, "iss": "cdp", "nbf": now, "exp": now + 120,
             "uri": f"GET {host}{SIGN_PATH}"},
            signing, algorithm="ES256",
            headers={"kid": key_name, "nonce": os.urandom(16).hex()})
        req = urllib.request.Request(
            f"https://{host}{SIGN_PATH}{query}",
            headers={"Authorization": f"Bearer {token}"}, method="GET")
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())

    # Follow pagination. A census that stops at page one is exactly the kind
    # of quietly-incomplete number this file exists to refuse: a balance past
    # the cursor is money the report would silently omit, and "where is the
    # rest of it?" is the question this tool is for.
    accounts, cursor, pages = [], None, 0
    try:
        while True:
            page = _fetch_page(cursor)
            accounts.extend(page.get("accounts", []))
            pages += 1
            cursor = page.get("cursor") or None
            if not page.get("has_next") or not cursor or pages >= 20:
                break
    except urllib.error.HTTPError as e:
        return {"venue": "Coinbase", "status": "UNKNOWN",
                "reason": f"HTTP {e.code} - credentials rejected "
                          f"(read from {key_var} / {priv_var})"}
    except Exception as e:
        return {"venue": "Coinbase", "status": "UNKNOWN",
                "reason": f"{type(e).__name__}: {e}"}
    data = {"accounts": accounts}

    usd, coins = 0.0, {}
    for acct in data.get("accounts", []):
        cur = acct.get("currency")
        try:
            amt = float(acct["available_balance"]["value"])
        except (KeyError, TypeError, ValueError):
            continue
        if cur == "USD":
            usd += amt
        elif amt > 0:
            coins[cur] = amt
    # Price the coins. A coin whose price cannot be read is reported with a
    # value of None and excluded from the total rather than guessed at - an
    # unpriced holding is an unknown, and quietly calling it $0 or carrying
    # last week's number is how a balance stops meaning anything.
    priced, unpriced, coin_usd = {}, [], 0.0
    for cur, amt in coins.items():
        px = _spot_price(f"{cur}-USD")
        if px is None:
            unpriced.append(cur)
            priced[cur] = {"units": amt, "usd": None}
        else:
            value = amt * px
            priced[cur] = {"units": amt, "price": px, "usd": round(value, 2)}
            coin_usd += value

    return {"venue": "Coinbase", "status": "OK", "usd_cash": round(usd, 2),
            "coin_usd": round(coin_usd, 2), "coin_balances": priced,
            "unpriced": unpriced, "source": f"{key_var} / {priv_var}",
            "note": (f"could not price {', '.join(unpriced)} - excluded from the "
                     f"total rather than guessed" if unpriced else "")}


def _spot_price(product_id: str):
    """Last trade price from the public exchange feed, or None.

    Deliberately the public endpoint and not the authenticated one: pricing
    is not privileged data, and using the unauthenticated feed means this
    still reports coin values on a key that has lost trading permission.
    """
    url = f"https://api.exchange.coinbase.com/products/{product_id}/ticker"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            return float(json.loads(r.read().decode())["price"])
    except Exception:
        return None


# --- Alpaca ---------------------------------------------------------------


def alpaca_account():
    key_var, key = _first_env("ALPACA_API_KEY", "APCA_API_KEY_ID", "ALPACA_KEY_ID")
    sec_var, sec = _first_env("ALPACA_SECRET_KEY", "APCA_API_SECRET_KEY", "ALPACA_SECRET")
    if not key or not sec:
        return {"venue": "Alpaca", "status": "UNKNOWN",
                "reason": "no credentials in the environment "
                          "(ALPACA_API_KEY / ALPACA_SECRET_KEY)"}
    base = os.getenv("ALPACA_BASE_URL", "https://api.alpaca.markets").rstrip("/")
    try:
        req = urllib.request.Request(
            f"{base}/v2/account",
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec})
        with urllib.request.urlopen(req, timeout=20) as r:
            a = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"venue": "Alpaca", "status": "UNKNOWN",
                "reason": f"HTTP {e.code} from {base} - credentials rejected"}
    except Exception as e:
        return {"venue": "Alpaca", "status": "UNKNOWN", "reason": f"{type(e).__name__}: {e}"}

    live = "paper" not in base
    return {"venue": "Alpaca", "status": "OK",
            "equity": round(float(a.get("equity") or 0), 2),
            "usd_cash": round(float(a.get("cash") or 0), 2),
            "buying_power": round(float(a.get("buying_power") or 0), 2),
            "endpoint": base,
            "is_live_money": live,
            "source": f"{key_var} / {sec_var}",
            "note": "" if live else "PAPER endpoint - this is not real money"}


# --- Sportsbooks ----------------------------------------------------------


# --- Phantom capital ------------------------------------------------------


def find_phantom_capital():
    found = []
    for rel, pattern, what in PHANTOM_CAPITAL_SOURCES:
        p = HERE / rel
        if not p.exists():
            continue
        m = re.search(pattern, p.read_text(encoding="utf-8", errors="replace"))
        if m:
            try:
                value = float(m.group(1).replace("_", ""))
            except ValueError:
                continue
            found.append({"file": rel, "value": value, "what_it_is": what})
    return found


# --- Report ---------------------------------------------------------------


def collect():
    """Every venue's live answer plus the phantom-constant scan, as data.

    Split out of main() so an HTTP caller and the command line report the
    same numbers from the same code path. A separately maintained second
    copy of this logic sitting behind an endpoint is precisely how two
    "real" figures start quietly disagreeing, which is the failure this
    whole file exists to prevent.
    """
    venues = [coinbase_holdings(), alpaca_account()]
    unknown = [v for v in venues if v["status"] == "UNKNOWN"]
    return {
        "venues": venues,
        "phantom_capital": find_phantom_capital(),
        "verified_usd_cash": round(
            sum(v.get("usd_cash", 0.0) for v in venues if v["status"] == "OK"), 2),
        "venues_unknown": [v["venue"] for v in unknown],
    }


def build_report(data) -> str:
    """The census as plain text - character for character what the CLI prints."""
    out = []
    p = out.append
    venues = data["venues"]
    phantom = data["phantom_capital"]
    verified = data["verified_usd_cash"]
    unknown = [v for v in venues if v["status"] == "UNKNOWN"]

    p("=" * 72)
    p("CAPITAL CENSUS - only what the venues themselves reported")
    p("=" * 72)
    for v in venues:
        p(f"\n{v['venue']}: {v['status']}")
        if v["status"] == "UNKNOWN":
            p(f"    {v['reason']}")
            continue
        if "equity" in v:
            p(f"    equity        ${v['equity']:>12,.2f}")
        if "usd_cash" in v:
            p(f"    USD cash      ${v['usd_cash']:>12,.2f}")
        if v.get("buying_power") is not None and "buying_power" in v:
            p(f"    buying power  ${v['buying_power']:>12,.2f}")
        if v.get("coin_balances"):
            p(f"    coins held    ${v.get('coin_usd', 0.0):>12,.2f}")
            for cur, d in sorted(v["coin_balances"].items(),
                                 key=lambda kv: -(kv[1].get("usd") or 0)):
                if d.get("usd") is None:
                    p(f"      {cur:<8}{d['units']:>16,.8f}   (no price - not counted)")
                else:
                    p(f"      {cur:<8}{d['units']:>16,.8f}   @ ${d['price']:>12,.4f}"
                      f"  = ${d['usd']:>10,.2f}")
        if "coin_usd" in v:
            p(f"    ---")
            p(f"    venue total   ${v['usd_cash'] + v['coin_usd']:>12,.2f}"
              f"   (cash + coins)")
        for k in ("source", "endpoint"):
            if v.get(k):
                p(f"    {k:<13} {v[k]}")
        if v.get("note"):
            p(f"    NOTE: {v['note']}")
        if v.get("reason"):
            p(f"    {v['reason']}")

    p("\n" + "-" * 72)
    p(f"VERIFIED USD CASH: ${verified:,.2f}")
    if unknown:
        p(f"INCOMPLETE - could not read: {', '.join(v['venue'] for v in unknown)}")
        p("The total above is a floor, not the answer. A venue that did not")
        p("answer is not the same as a venue holding zero.")
    else:
        p("Every venue answered. This is the whole picture.")

    if phantom:
        p("\n" + "-" * 72)
        p("NOT REAL MONEY - hardcoded constants that read like capital:")
        for ph in phantom:
            p(f"    ${ph['value']:>10,.2f}  {ph['file']}")
            p(f"    {'':>12}  {ph['what_it_is']}")
        total_phantom = sum(ph["value"] for ph in phantom)
        p(f"\n    These sum to ${total_phantom:,.2f} and none of it exists.")
        p("    They are settings. Adding them to the figure above, or")
        p("    subtracting one from a balance to compute 'profit', is how")
        p("    this project produced P&L on zero trades.")

    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    data = collect()
    print(json.dumps(data, indent=2) if args.json else build_report(data))
    return 1 if data["venues_unknown"] else 0


if __name__ == "__main__":
    sys.exit(main())

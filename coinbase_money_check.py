"""Where the Coinbase cash is - a read-only money check. Coinbase only.

The owner, 2026-10-31, after the cash balance fell $891.05 with no crypto
trade behind it: "make sure it's like not no anonymous person that's stealing
out of my account ... Dig deeper into that."

The bots' fills feed (/orders/historical/fills) shows trades and nothing
else, and the account census reads the spot accounts only. Two things move
cash without a trade, and neither was visible anywhere:

  * THE PREDICTIONS BALANCE. Coinbase pays for event-contract bets out of a
    separate futures (CFM) cash balance, moving money into it from the main
    balance, and moves winnings back only during business hours - so over a
    weekend hundreds of dollars can sit there while the main cash reads low.
    GET /api/v3/brokerage/cfm/balance_summary: cfm_usd_balance is that money.
  * MOVES THAT ARE NOT TRADES. Sends, withdrawals, transfers, card payments,
    staking moves. GET /v2/accounts/{id}/transactions lists every one, with
    its type, amount, time and - for a send - where it went.

The report puts every non-trade USD/USDC movement in one of four kinds:
  left_coinbase   money that went OUT of Coinbase (a send to an address or
                  email, a bank withdrawal). This is the one to worry about.
  inside_coinbase money moved between the owner's own Coinbase balances
                  (predictions/futures, staking, vaults, portfolios).
  came_in         deposits, receives, rewards.
  other           anything Coinbase types that this file does not know -
                  shown, never hidden.

GET only. It places nothing, moves nothing and changes nothing.
"""
import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger("coinbase_money_check")

HOST = "api.coinbase.com"
CFM_PATH = "/api/v3/brokerage/cfm/balance_summary"
V2_ACCOUNTS = "/v2/accounts"
CASH_CURRENCIES = ("USD", "USDC")
MAX_PAGES = 5
PAGE_LIMIT = 100

TRADE_TYPES = {"advanced_trade_fill", "buy", "sell", "trade", "retail_simple_dust"}
LEFT_TYPES = {"send", "fiat_withdrawal", "intx_withdrawal"}
INSIDE_TYPES = {"transfer", "staking_transfer", "unstaking_transfer", "vault_withdrawal",
                "derivatives_settlement", "fcm_futures_usdc_sell",
                "fcm_futures_usdc_sell_additional_encumberment_rollup", "wrap_asset", "unwrap_asset"}
CAME_IN_TYPES = {"receive", "fiat_deposit", "intx_deposit", "earn_payout", "incentives_rewards_payout",
                 "subscription_rebate", "unsupported_asset_recovery"}


def _headers(method, path):
    import crypto_btc_compound_bot as engine
    return engine._auth_headers(method, path)


async def _get(session, path):
    """(HTTP status, parsed JSON) for one signed GET; (None, None) with no answer."""
    import aiohttp
    try:
        async with session.get(f"https://{HOST}{path}", headers=_headers("GET", path),
                               timeout=aiohttp.ClientTimeout(total=25)) as r:
            try:
                data = await r.json(content_type=None)
            except Exception:
                data = None
            return r.status, data
    except Exception as e:
        log.warning(f"[MONEY-CHECK] GET {path.split('?')[0]} got no answer ({type(e).__name__})")
        return None, None


def _num(x):
    try:
        if isinstance(x, dict):
            x = x.get("value", x.get("amount"))
        return float(x)
    except (TypeError, ValueError):
        return None


def _mask(s):
    """An address or email, recognisable but not copyable: d***@gmail.com, 0x12...abcd34."""
    s = str(s or "")
    if "@" in s:
        name, _, dom = s.partition("@")
        return f"{name[:1]}***@{dom}"
    if len(s) > 12:
        return f"{s[:4]}...{s[-6:]}"
    return s


def kind(tx_type, amount):
    """Which of the four kinds a non-trade movement is; None for a trade."""
    t = str(tx_type or "")
    if t in TRADE_TYPES:
        return None
    if t in LEFT_TYPES:
        return "left_coinbase" if (amount is None or amount < 0) else "came_in"
    if t in INSIDE_TYPES or t.startswith(("fcm_", "cfm_", "derivatives_", "staking_")):
        return "inside_coinbase"
    if t in CAME_IN_TYPES:
        return "came_in"
    return "other"


def describe(tx):
    """One movement as plain fields."""
    amount = _num(tx.get("amount"))
    native = _num(tx.get("native_amount"))
    det = tx.get("details") or {}
    to = tx.get("to") or {}
    net = tx.get("network") or {}
    where = to.get("address") or to.get("email") or to.get("resource") or None
    return {
        "at": tx.get("created_at"),
        "type": tx.get("type"),
        "status": tx.get("status"),
        "kind": kind(tx.get("type"), amount),
        "amount": amount,
        "currency": (tx.get("amount") or {}).get("currency"),
        "usd": native,
        "title": det.get("title"),
        "subtitle": det.get("subtitle"),
        "to": _mask(where) if where else None,
        "network": net.get("network_name") or net.get("name"),
    }


def _ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


async def cash_accounts(session):
    """[(currency, v2 account id)] for the USD and USDC wallets, or an error string."""
    out, path, pages = [], f"{V2_ACCOUNTS}?limit=300", 0
    while path and pages < MAX_PAGES:
        status, data = await _get(session, path)
        if status != 200 or not isinstance(data, dict):
            return f"accounts HTTP {status}"
        for a in data.get("data") or []:
            code = ((a.get("currency") or {}).get("code") if isinstance(a.get("currency"), dict)
                    else a.get("currency"))
            if code in CASH_CURRENCIES and a.get("id"):
                out.append((code, a["id"]))
        path = ((data.get("pagination") or {}).get("next_uri")) or None
        pages += 1
    return out


async def movements(session, account_id, since):
    """Every movement on one wallet since `since`, newest first, or an error string."""
    out, pages = [], 0
    path = f"{V2_ACCOUNTS}/{account_id}/transactions?limit={PAGE_LIMIT}&order=desc"
    while path and pages < MAX_PAGES:
        status, data = await _get(session, path)
        if status != 200 or not isinstance(data, dict):
            return f"transactions HTTP {status}"
        older = False
        for tx in data.get("data") or []:
            t = _ts(tx.get("created_at"))
            if t is not None and t < since:
                older = True
                break
            out.append(tx)
        if older:
            break
        path = ((data.get("pagination") or {}).get("next_uri")) or None
        pages += 1
    return out


async def predictions_balance(session):
    """The futures/predictions cash next to the main cash, or an error string."""
    status, data = await _get(session, CFM_PATH)
    if status != 200 or not isinstance(data, dict):
        return f"futures balance HTTP {status}"
    b = data.get("balance_summary") or {}
    return {
        "predictions_cash_usd": _num(b.get("cfm_usd_balance")),
        "main_cash_usd": _num(b.get("cbi_usd_balance")),
        "both_usd": _num(b.get("total_usd_balance")),
        "on_hold_for_open_orders_usd": _num(b.get("total_open_orders_hold_amount")),
        "pending_transfers_usd": _num(b.get("total_pending_transfers_amount")),
    }


def summarize(rows):
    """Totals per kind and the one-line answer."""
    totals = {}
    for r in rows:
        if r["kind"] is None:
            continue
        v = r["usd"] if r["usd"] is not None else (r["amount"] or 0.0)
        totals[r["kind"]] = round(totals.get(r["kind"], 0.0) + v, 2)
    left = [r for r in rows if r["kind"] == "left_coinbase"]
    if left:
        verdict = (f"${-totals.get('left_coinbase', 0.0):,.2f} LEFT Coinbase in {len(left)} "
                   f"movement(s) - check each one below")
    else:
        verdict = "No USD or USDC left Coinbase in this window - no send, no withdrawal"
    return totals, verdict


async def check(session, days=3, now=None):
    """The whole report. Never raises."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    out = {"since": since.isoformat(), "days": days, "places_orders": False, "moves_money": False,
           "predictions": None, "movements": [], "totals": {}, "verdict": None, "problems": []}
    try:
        p = await predictions_balance(session)
        if isinstance(p, str):
            out["problems"].append(p)
        else:
            out["predictions"] = p
        accounts = await cash_accounts(session)
        if isinstance(accounts, str):
            out["problems"].append(accounts)
            accounts = []
        rows = []
        for cur, aid in accounts:
            txs = await movements(session, aid, since)
            if isinstance(txs, str):
                out["problems"].append(f"{cur}: {txs}")
                continue
            rows.extend(describe(tx) for tx in txs)
        rows.sort(key=lambda r: str(r["at"] or ""), reverse=True)
        out["movements"] = [r for r in rows if r["kind"] is not None]
        out["trades_skipped"] = sum(1 for r in rows if r["kind"] is None)
        out["totals"], out["verdict"] = summarize(rows)
        unread = [p for p in out["problems"] if p.startswith(CASH_CURRENCIES) or p.startswith("accounts")]
        if out["problems"] and not rows:
            out["verdict"] = "Coinbase did not answer - nothing can be said yet: " + "; ".join(out["problems"])
        elif unread:
            # A wallet that could not be read is not a wallet where nothing happened.
            out["verdict"] = "Not all movements could be read (" + "; ".join(unread) + ") - " + out["verdict"]
    except Exception as e:
        out["problems"].append(f"failed: {type(e).__name__}")
        out["verdict"] = "check failed - nothing can be said yet"
    return out

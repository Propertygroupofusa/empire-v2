"""Paper test of the daily_stock_analysis (DSA) picks - pretend $1,000, no orders.

DSA posts its daily report to /api/trading-dashboard/dsa-paper/webhook. This module turns that
report into paper trades and nothing else. It never talks to a broker's order
endpoint; the only outside reads are live prices, done by the router.

Rules (fixed up front so the result cannot be tuned after the fact):
  * start with START_CAPITAL cash;
  * Buy / Strong Buy on a ticker not held -> buy one slot: equity / MAX_POSITIONS,
    capped at available cash, at the live price plus SLIPPAGE;
  * Sell / Strong Sell on a held ticker -> sell all of it at the live price
    minus SLIPPAGE;
  * Hold / Watch / Reduce -> nothing;
  * a ticker with no live price is skipped and says so - never a guessed price.

Pure functions only - no database, no network.
"""
import re

START_CAPITAL = 1000.0
MAX_POSITIONS = 5
SLIPPAGE = 0.001   # 0.1% per leg; Alpaca charges no commission on stocks

BUY_ACTIONS = {"buy", "strong buy"}
SELL_ACTIONS = {"sell", "strong sell"}
# "Reduce" is logged but not traded: a partial sale needs a size DSA does not give.
KNOWN_ACTIONS = BUY_ACTIONS | SELL_ACTIONS | {"hold", "watch", "reduce"}

# Matches DSA's summary lines in English, bold or not:
#   "🟢 **Apple(AAPL)**: Buy | Score 72 | Bullish"
#   "⚪ Apple(AAPL): Watch | Score 55"
_LINE = re.compile(
    r"\(([A-Za-z][A-Za-z0-9.\-]{0,9})\)\**\s*:\s*\**\s*([A-Za-z][A-Za-z ]*?)\s*\|\s*[^|\d]*?(\d{1,3})")


def parse_report(text):
    """[(ticker, action, score)], one per ticker, first mention wins.

    Only actions DSA actually emits are accepted, so a prose line that
    happens to contain "(XYZ): something |" cannot become a trade.
    """
    seen, out = set(), []
    for m in _LINE.finditer(text or ""):
        ticker, action, score = m.group(1).upper(), m.group(2).strip().lower(), int(m.group(3))
        if action not in KNOWN_ACTIONS or ticker in seen:
            continue
        seen.add(ticker)
        out.append((ticker, action, score))
    return out


def new_account(now_iso):
    return {"cash": START_CAPITAL, "start_capital": START_CAPITAL,
            "started_at": now_iso, "positions": {}}


def equity(account, prices):
    """Cash plus positions at live prices; None if any held ticker is unpriced."""
    total = account["cash"]
    for t, p in account["positions"].items():
        px = prices.get(t)
        if not px:
            return None
        total += p["qty"] * px
    return total


def apply_signals(account, signals, prices, now_iso):
    """Returns (account, trades, skipped). Sells run before buys so a switch
    frees its cash the same day."""
    acct = {**account, "positions": {k: dict(v) for k, v in account["positions"].items()}}
    trades, skipped = [], []
    for ticker, action, _ in signals:
        if action in SELL_ACTIONS and ticker in acct["positions"]:
            px = prices.get(ticker)
            if not px:
                skipped.append((ticker, "sell: no live price"))
                continue
            pos = acct["positions"].pop(ticker)
            fill = px * (1 - SLIPPAGE)
            acct["cash"] += pos["qty"] * fill
            trades.append({"at": now_iso, "ticker": ticker, "side": "sell", "qty": pos["qty"],
                           "price": round(fill, 4), "pnl": round(pos["qty"] * (fill - pos["entry"]), 2),
                           "reason": action})
    held_px = {t: prices.get(t) for t in acct["positions"]}
    eq = equity(acct, {**prices, **held_px})
    for ticker, action, _ in signals:
        if action not in BUY_ACTIONS or ticker in acct["positions"]:
            continue
        if len(acct["positions"]) >= MAX_POSITIONS:
            skipped.append((ticker, "buy: all slots full"))
            continue
        px = prices.get(ticker)
        if not px:
            skipped.append((ticker, "buy: no live price"))
            continue
        slot = (eq if eq is not None else acct["cash"]) / MAX_POSITIONS
        spend = min(slot, acct["cash"])
        if spend < 1.0:
            skipped.append((ticker, "buy: no cash left"))
            continue
        fill = px * (1 + SLIPPAGE)
        qty = spend / fill
        acct["cash"] -= spend
        acct["positions"][ticker] = {"qty": qty, "entry": fill, "opened_at": now_iso}
        trades.append({"at": now_iso, "ticker": ticker, "side": "buy", "qty": qty,
                       "price": round(fill, 4), "pnl": None, "reason": action})
    return acct, trades, skipped

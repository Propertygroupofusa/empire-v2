"""The tape: individual trades printing as they happen, on the coins you hold.

WHAT THIS IS

What Kalshi shows beside its price chart - the +$19, +$16, +$24 floating up
the left edge - is a TRADE TAPE, also called time & sales, or just "the
tape". Each number is one trade executing, printed the instant it fills. It
is the oldest display in trading and it is still there because a price alone
tells you where the market is, while the tape tells you whether anything is
actually happening at that price.

A chart at 25.35% and a chart at 25.35% with nothing trading are the same
picture and completely different situations.

WHOSE TRADES

The venue's, not yours. Coinbase publishes every fill on every product -
/products/{id}/trades, public, no key - so this is the whole market's flow
on the coins you own, which is the same thing Kalshi is showing: other
people's money moving.

That distinction matters here more than usual, because this account is not
currently trading. A tape of YOUR fills would be an empty box. A tape of the
market on your holdings is alive, real, and tells you something you cannot
get anywhere else on the dashboard: which of your 45 coins anyone else is
touching right now.

WHAT IT REFUSES TO DO

  * It never invents a print. An empty response is an empty tape with a
    reason, not a placeholder row.
  * It never implies a print is yours. Every row is market flow and the
    panel says so - a tape that let someone read market volume as their own
    activity would be worse than no tape.
  * It does not aggregate away the size. A $310 print and a $3 print are
    different events and averaging them is how a tape stops being a tape.
"""
from __future__ import annotations

# Below this a print is dust and crowds out the ones worth seeing. Coinbase
# fills a lot of sub-dollar slices; showing them means the tape scrolls
# without saying anything.
MIN_PRINT_USD = 1.0

# The tape is a window, not a log. Older than this and it is history, which
# the charts already cover.
MAX_ROWS = 60


def _f(x, default=None):
    try:
        v = float(x)
        return v if v == v else default
    except (TypeError, ValueError):
        return default


def normalise(product_id: str, raw) -> list:
    """Coinbase trade rows -> printable ones. Never raises on bad input.

    Coinbase reports `side` from the MAKER's perspective: a row marked
    "buy" means the resting order was a buy, so the taker SOLD into it.
    That is backwards from how a tape is read - the tape shows aggressor
    direction - so it is flipped here, once, rather than at each call site.
    """
    out = []
    for t in raw or ():
        if not isinstance(t, dict):
            continue
        size, price = _f(t.get("size")), _f(t.get("price"))
        if not size or not price or size <= 0 or price <= 0:
            continue
        usd = size * price
        if usd < MIN_PRINT_USD:
            continue
        maker_side = str(t.get("side") or "").lower()
        if maker_side == "buy":
            aggressor = "sell"
        elif maker_side == "sell":
            aggressor = "buy"
        else:
            aggressor = None            # unknown, and said so, not guessed
        out.append({
            "product_id": product_id,
            "asset": product_id.replace("-USD", ""),
            "time": t.get("time"),
            "trade_id": t.get("trade_id"),
            "size": size,
            "price": price,
            "usd": round(usd, 2),
            "side": aggressor,
        })
    return out


def merge(streams) -> list:
    """Many coins' prints into one tape, newest first, deduplicated.

    A trade_id is unique per product, not globally, so the key is the pair.
    Without that, two coins that happen to share an id silently drop one
    another's prints.
    """
    seen, rows = set(), []
    for s in streams or ():
        for r in s or ():
            key = (r.get("product_id"), r.get("trade_id"))
            if r.get("trade_id") is not None and key in seen:
                continue
            seen.add(key)
            rows.append(r)
    rows.sort(key=lambda r: (str(r.get("time") or ""), r.get("usd") or 0),
              reverse=True)
    return rows[:MAX_ROWS]


def summarise(rows) -> dict:
    """What the tape says, for the line above it."""
    rows = list(rows or ())
    buys = [r for r in rows if r.get("side") == "buy"]
    sells = [r for r in rows if r.get("side") == "sell"]
    buy_usd = round(sum(r["usd"] for r in buys), 2)
    sell_usd = round(sum(r["usd"] for r in sells), 2)
    per = {}
    for r in rows:
        a = r.get("asset")
        per[a] = round(per.get(a, 0.0) + r["usd"], 2)
    busiest = max(per.items(), key=lambda kv: kv[1])[0] if per else None
    return {
        "prints": len(rows),
        "buy_prints": len(buys),
        "sell_prints": len(sells),
        "buy_usd": buy_usd,
        "sell_usd": sell_usd,
        # Not a forecast and not a signal. It is what just traded.
        "net_usd": round(buy_usd - sell_usd, 2),
        "by_asset_usd": per,
        "busiest": busiest,
        "biggest_print": max(rows, key=lambda r: r["usd"]) if rows else None,
        "note": ("Market flow on the coins you hold, from Coinbase's public "
                 "trade feed. These are OTHER PEOPLE'S trades, not yours - "
                 "this account is not currently placing orders."),
    }

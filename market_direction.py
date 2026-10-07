"""Which way a held position bets on the US stock market.

Added 2026-10-07 after the account held SH, DOG and RWM (each rises when the
market FALLS) alongside AAPL (rises when it RISES) on the same day. Two
positions betting opposite ways on the same market cancel: whatever one
gains the other loses, and the account keeps only the spread it paid on
both. That cannot grow anything.

So no bot on the shared Alpaca account opens a position whose direction
opposes one already held. It only ever REFUSES an entry - it never closes
or changes an existing position, and it never applies to gold, silver or
oil, which are not bets on the stock market's direction.

Dependency-free on purpose: prop_bot and alpaca_swing_bot both import it,
and they must agree on the same map.
"""
from __future__ import annotations

# Rise when the US stock market falls.
BEARISH = frozenset({"SH", "PSQ", "DOG", "RWM", "SDS", "SQQQ", "SPXU", "SDOW", "SRTY", "TZA"})
# Rise when it rises: the index funds and the large caps the bots trade.
BULLISH = frozenset({"SPY", "QQQ", "DIA", "IWM", "VOO", "VTI",
                     "AAPL", "AMZN", "GOOGL", "GOOG", "META", "MSFT", "NVDA", "TSLA"})


def direction(ticker):
    t = (ticker or "").upper()
    if t in BEARISH:
        return "bear"
    if t in BULLISH:
        return "bull"
    return None


def opposing_holdings(ticker, held_tickers):
    """Held tickers that bet the opposite way to `ticker`. Empty if the new
    position is direction-neutral or nothing opposes it."""
    d = direction(ticker)
    if d is None:
        return []
    return sorted({h.upper() for h in held_tickers
                   if direction(h) not in (None, d)})

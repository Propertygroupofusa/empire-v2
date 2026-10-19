"""The owner's no-sell list: coins nothing in this service may sell.

The owner, 2026-10-19, after the grid sold a BTC slice at its profit target
under GRID_SELL_ONLY: "Yes, leave BTC out of selling." Earlier, in their
own words: "I don't want to sale btc."

NO_SELL_COINS is a comma-separated list of coins - "BTC" or "BTC-USD" both
work. It is matched on the BASE coin, so every BTC market (BTC-USD,
BTC-USDC, ...) counts. Unset or blank means BTC: the rule holds the moment
this deploys, without a variable to set first. "none" (or off/false/0/no)
turns the list off.

Every Coinbase sell path in the service asks blocks() before an order is
built - the engine's market and maker legs, the grid's sell step, auto-trim,
resting stops, the legacy bot and the dashboard's manual sell endpoints - so
a coin on the list is held, never sold, whatever asked for the sale. Buys
are untouched. Nothing here cancels an order that is already on the book.
Alpaca is a different venue and never reads this.
"""
import os

NO_SELL_COINS_ENV = "NO_SELL_COINS"
DEFAULT_COINS = ("BTC",)
_OFF = {"none", "off", "false", "0", "no"}


def base_of(product_id) -> str:
    """"BTC-USD" / "btc/usd" / "BTC" -> "BTC". Empty for anything unreadable."""
    return str(product_id or "").strip().upper().replace("/", "-").split("-")[0]


def coins() -> tuple:
    """The coins on the list, read fresh on every call so a variable change
    applies on the next order without a code path to reload it."""
    raw = os.getenv(NO_SELL_COINS_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_COINS
    if raw.strip().lower() in _OFF:
        return ()
    out = []
    for part in raw.replace(";", ",").split(","):
        coin = base_of(part)
        if coin and coin not in out:
            out.append(coin)
    # A value that names nothing readable ("," or "-") is not a request to
    # sell BTC. It falls back to the default rather than to an empty list.
    return tuple(out) or DEFAULT_COINS


def blocks(product_id) -> bool:
    """True when product_id's coin is on the list and must not be sold."""
    coin = base_of(product_id)
    return bool(coin) and coin in coins()


def reason(product_id) -> str:
    return (f"{base_of(product_id)} is on the owner's no-sell list ({NO_SELL_COINS_ENV}) - "
            f"held, not sold")

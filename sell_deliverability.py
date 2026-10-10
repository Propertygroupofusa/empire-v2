#!/usr/bin/env python3
"""Stop placing sell orders the venue is certain to reject.

THE LOOP THIS ENDS
------------------
A branch whose coin is not in the trading balance still passes every sell
gate: the rise trigger fires on price, _pick_profitable_slice_to_sell
certifies on P&L, and neither asks whether the venue will release the
units. So the order goes out, the venue rejects it, the cycle returns
having done nothing, and ~50 seconds later it does it again. BCH ran 200
of those. ZEC ran a retry loop that dominated a lifetime counter.

Measured 2026-10-10: the owner staked 0.19440136 ETH and 1.55675300 SOL.
Both branches still hold slices and still claim that coin. SOL sits 2.52%
under its rise trigger and ETH 4.51% - so this is a loop waiting on an
ordinary day's move, not a hypothetical.

WHAT THIS IS NOT
----------------
It is NOT a new reason to refuse a sale, and it must never become one.
This repo's standing design is that sells are never gated - the drawdown
breaker pauses buys only, and the comment at the breaker says so. Nothing
here changes WHAT gets sold, because an order the venue rejects sells
nothing either way. It changes only how often a doomed order is sent.

AVAILABLE, NOT OWNED - AND THIS IS THE OPPOSITE OF THE BUY GATE
---------------------------------------------------------------
The buy-side backing gate deliberately reads OWNED units, because coin
sitting under the fleet's own resting sell order is present and a buy
should not be refused over it (wallet_owned_units' docstring records the
four hours of false refusals that taught that).

A SELL asks the other question. The venue sizes a sell against what it
will RELEASE. Staked coin is owned and not available. Coin under a resting
order is owned and not available. Reading OWNED here would reproduce the
exact bug in mirror image - and that mirror image is the Alpaca QQQ bug,
where the account held 0.162956 shares against 0 available and
`qty_available` appeared nowhere in the repository.

THREE RULES THAT KEEP IT FROM EVER STRANDING A SALE
---------------------------------------------------
1. UNKNOWN NEVER BLOCKS. An unreadable balance sends the order exactly as
   before. A sell must not be stopped by a missing reading; that is the
   one direction of failure this codebase cannot afford.

2. ONLY A REAL SHORTFALL BLOCKS. Covered within float tolerance sends.
   A rounding crumb must never look like a shortfall.

3. IT PROBES. After PROBE_EVERY consecutive skips it sends one anyway and
   lets the venue be the final authority. So a persistently wrong balance
   read costs a delay, never a stranded slice - the worst case degrades to
   "tries every ~17 minutes instead of every ~50 seconds", which is still
   the loop gone.

Default ON, with GRID_SELL_DELIVERABILITY_CHECK=0 to disable, because the
only behaviour it removes is an order that cannot fill.
"""

import os

from env_config import env_float, env_int

# One send attempt every this many consecutive skips, so the venue always
# gets the last word. 20 cycles at ~50s is ~17 minutes.
PROBE_EVERY = max(1, env_int("GRID_SELL_PROBE_EVERY", 20))

# Relative slack on the covered test. Quantities are floats that have been
# through a fee calculation and a round trip to the venue; a shortfall of
# one part in a billion is arithmetic, not a missing coin.
COVER_TOLERANCE = max(0.0, env_float("GRID_SELL_COVER_TOLERANCE", 1e-9))

SEND, SKIP, PROBE, UNKNOWN = "SEND", "SKIP", "PROBE", "UNKNOWN"


ENV_NAME = "GRID_SELL_DELIVERABILITY_CHECK"
_OFF = ("0", "false", "no", "off")
_ON = ("", "1", "true", "yes", "on")


def is_enabled() -> bool:
    """On unless explicitly turned off. Read at call time, not at import.

    ONE definition of "off" (_OFF), shared with status() below.
    """
    return os.getenv(ENV_NAME, "1").strip().lower() not in _OFF


def status() -> dict:
    """What the RUNNING PROCESS sees, for /grid-status. Pure, never raises.

    The mirror image of inventory_depth_gate.status(): that one defaults OFF
    and the typo that matters is "meant to arm, did not". This one defaults
    ON, so the typo that matters is "meant to disable, did not" - "disable"
    or "false " with the wrong word matches nothing in _OFF and the check
    silently stays on. value_recognised catches it.

    The raw value is not echoed, for the same reason.
    """
    try:
        raw = os.getenv(ENV_NAME)
        norm = (raw or "").strip().lower()
        return {
            "enabled": norm not in _OFF,
            "env_name": ENV_NAME,
            "env_is_set": raw is not None,
            "value_recognised": norm in _OFF or norm in _ON,
            "accepted_values_to_disable": list(_OFF),
            "probe_every_skips": PROBE_EVERY,
            "scope": ("skips only a sell the venue would reject; unknown "
                      "balance always sends; never blocks a sellable order"),
        }
    except Exception as exc:
        return {"readable": False, "error": type(exc).__name__}


def asset_of(product_id) -> str:
    """'ETH' from 'ETH-USD'. The venue keys balances by asset, not pair."""
    return str(product_id or "").strip().upper().split("-")[0]


def verdict(product_id, qty_needed, available_map, consecutive_skips=0,
            probe_every=None, tolerance=None):
    """(send_order, state, reason). Pure: no clock, no network, no database.

    available_map is account_census.available_units_map's output, or None
    when the balance could not be read. An asset ABSENT from a readable map
    is UNKNOWN, not zero - available_units_map's own docstring is explicit
    that it adds a 0.0 only for an asset a zero-inclusive read confirms is
    empty, so absence means nobody looked, not that nothing is there.
    """
    if not is_enabled():
        return True, SEND, "deliverability check disabled"

    try:
        qty = float(qty_needed)
    except (TypeError, ValueError):
        return True, UNKNOWN, "order size unreadable - sent, never blocked on UNKNOWN"
    if qty <= 0:
        return True, SEND, "nothing to size"

    if available_map is None:
        return True, UNKNOWN, ("balance unreadable - order sent unchanged. "
                               "A sell is never blocked on a missing reading.")

    asset = asset_of(product_id)
    if asset not in available_map:
        return True, UNKNOWN, (f"{asset} absent from the balance read - UNKNOWN, "
                               f"not zero. Order sent unchanged.")

    try:
        avail = float(available_map[asset] or 0.0)
    except (TypeError, ValueError):
        return True, UNKNOWN, f"{asset} balance unreadable - order sent unchanged"

    tol = COVER_TOLERANCE if tolerance is None else float(tolerance)
    if avail >= qty - abs(qty) * tol:
        return True, SEND, f"{asset} {avail:.8f} available covers {qty:.8f}"

    every = PROBE_EVERY if probe_every is None else max(1, int(probe_every))
    short = qty - avail
    if int(consecutive_skips or 0) >= every - 1:
        return True, PROBE, (
            f"{asset} shows {avail:.8f} available against {qty:.8f} needed "
            f"(short {short:.8f}), but {int(consecutive_skips)+1} skips have passed - "
            f"sending one anyway so the venue, not this check, has the last word")

    return False, SKIP, (
        f"{asset} has {avail:.8f} available against {qty:.8f} needed - short "
        f"{short:.8f}. The venue would reject this order, so it is not sent. "
        f"Nothing is cancelled and nothing is held; the slice stays open and "
        f"the next attempt is at most {every} cycles away.")

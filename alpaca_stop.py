"""Alpaca's own off switch, apart from Coinbase's.

THE OWNER: "Alpaca and Coinbase are two different things... never to
combine those, never to combine the codes." Until now one switch held both.
STOP_TRADING is read by the Coinbase grid, the rotation and the boot-time
fixes AND by every Alpaca bot, so the only way to let Alpaca run was to wake
the stopped parts of Coinbase with it, and the only way to stop Alpaca was to
stop them too. Measured 2026-10-21: STOP_TRADING true, every Alpaca ticker
refused with "STOP_TRADING is set", and nothing that could change Alpaca
without changing Coinbase.

ALPACA_STOP_TRADING is Alpaca's alone. Every Alpaca path reads it through
alpaca_stopped(); no Coinbase path reads it at all.

    unset or blank          follows STOP_TRADING, exactly as before - so
                            shipping this changes nothing on either side
    false / 0 / no / off    Alpaca runs, whatever STOP_TRADING says
    anything else           Alpaca stopped, whatever STOP_TRADING says

IT FAILS CLOSED. A value that is not plainly one of the off words - a typo,
a pasted quote mark - reads as a stop, never as permission to trade.
"""
import os

ENV = "ALPACA_STOP_TRADING"
SHARED_ENV = "STOP_TRADING"
_OFF = ("false", "0", "no", "off")


def _clean(raw):
    # A hand-pasted Railway value can carry quote marks or spaces; the stray
    # quote is a failure this codebase has already paid for once.
    return (raw or "").strip().strip("'\"").strip().lower()


def alpaca_stop_state():
    """(stopped, source, detail) - the answer and where it came from."""
    own = _clean(os.getenv(ENV))
    if own:
        stopped = own not in _OFF
        return stopped, ENV, (f"{ENV}={own}: Alpaca is "
                              f"{'stopped' if stopped else 'free to trade'} on its own switch")
    shared = _clean(os.getenv(SHARED_ENV, "false")) == "true"
    return shared, SHARED_ENV, (f"{ENV} is not set, so Alpaca follows {SHARED_ENV} "
                                f"({'true' if shared else 'false'}) - the switch Coinbase also reads")


def alpaca_stopped():
    """True when no Alpaca order of any kind may be placed."""
    return alpaca_stop_state()[0]


def alpaca_stop_reason():
    """One line naming the switch that stopped Alpaca, for logs and refusals."""
    stopped, source, _detail = alpaca_stop_state()
    if not stopped:
        return ""
    return f"{source} is set - Alpaca is stopped"

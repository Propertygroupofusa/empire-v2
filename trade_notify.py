"""Tell the owner when a trade actually closes.

THE GAP THIS FILLS. crypto_grid_bot.py - the bot that has done every one
of the fleet's 162 round trips - contains ZERO email code. A trade
emailer does exist, in crypto_coinbase_bot.py, which is a different and
older bot. So the grid has been closing trades and telling nobody, and
the owner has been reading a dashboard to find out whether he made money
today.

This is deliberately SEPARATE from alert_sender's digest. Those two
messages answer different questions and must not be blended:

    alerts  - something is WRONG. Read it and act.
    trades  - money MOVED. Read it and enjoy it, or notice it stopped.

Merging them would mean either burying a real fault under a list of wins
or crying wolf every time a coin sold at a profit.

ONE MESSAGE PER BATCH, NOT PER TRADE. 2026-09-28 closed 35 round trips.
Thirty-five emails is a muted sender, and a muted sender swallows the
next real one too. So a run of closes becomes one note.

SILENT WHEN NOTHING HAPPENED. A notifier that mails "0 trades" every
hour teaches the reader to ignore it, which costs exactly as much as
never sending at all.
"""
import logging
import os

log = logging.getLogger(__name__)

MODE_ENV = "TRADE_EMAIL_MODE"

# Past this many in one batch the individual lines stop earning their
# space; the totals already say what happened.
MAX_LINES = 20


def mode() -> str:
    return (os.getenv(MODE_ENV) or "off").strip().strip('"').strip("'").lower()


def armed() -> bool:
    return mode() == "send"


def _f(v, default=0.0):
    try:
        out = float(v)
    except (TypeError, ValueError):
        return default
    return out if out == out else default


def build(trades: list) -> dict:
    """(subject, body) for a batch of closed round trips. Pure, no I/O.

    Wins and losses are both reported. A notifier that only mentions the
    green ones is a worse lie than silence.
    """
    rows = [t for t in (trades or []) if isinstance(t, dict)]
    if not rows:
        return {"subject": None, "body": None, "count": 0, "net": 0.0,
                "reason": "no closed trades in this batch"}

    net = round(sum(_f(t.get("pnl")) for t in rows), 2)
    wins = sum(1 for t in rows if _f(t.get("pnl")) > 0)
    losses = sum(1 for t in rows if _f(t.get("pnl")) < 0)
    n = len(rows)
    sign = "+" if net >= 0 else "-"
    subject = (f"empire-v2: {n} trade{'' if n == 1 else 's'} closed, "
               f"{sign}${abs(net):,.2f}")

    lines = [f"{n} round trip{'' if n == 1 else 's'} closed.",
             f"Net {sign}${abs(net):,.2f}   ({wins} up, {losses} down)", ""]
    for t in sorted(rows, key=lambda r: -_f(r.get("pnl")))[:MAX_LINES]:
        p = _f(t.get("pnl"))
        lines.append(f"  {'+' if p >= 0 else '-'}${abs(p):>8,.2f}   "
                     f"{str(t.get('product_id') or '?'):<10} "
                     f"{str(t.get('exit_reason') or '')}".rstrip())
    if n > MAX_LINES:
        lines.append(f"  ... and {n - MAX_LINES} more, included in the total above.")
    lines += ["", "This is a record of trades that already happened. "
                  "It places no order and moves no money."]
    return {"subject": subject, "body": "\n".join(lines),
            "count": n, "net": net, "wins": wins, "losses": losses}


def send(trades: list, *, force: bool = False) -> tuple:
    """(ok, error). One message for the whole batch. Never raises."""
    d = build(trades)
    if not d.get("subject"):
        return False, d.get("reason", "nothing to send")
    if not (armed() or force):
        return False, (f"{MODE_ENV} is '{mode()}' - nothing sent. "
                       f"Set it to 'send' to arm trade notifications.")
    import alert_sender
    ok, err = alert_sender.send_email(d["subject"], d["body"])
    if ok:
        log.info(f"[trades] notified: {d['count']} close(s), net {d['net']:+.2f}")
    return ok, err

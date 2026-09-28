#!/usr/bin/env python3
"""Writing down why a trade did, or did not, happen.

THE GAP THIS CLOSES

ClosedTrade has carried entry_rsi, entry_trend and entry_atr_pct since it
was created, and prop_bot writes none of them - every one is NULL. The
outcome of each trade is durable; the reasoning behind it is not.

And validate_entry could not have filled them even if somebody wired it
up: it short-circuits on the first failing rule and returns the bare
string "OK" when a trade is admitted. An admitted trade therefore
recorded nothing at all about why it qualified.

REFUSALS ARE THE MORE VALUABLE HALF. "Why has nothing traded for six
hours" has been unanswerable this whole time, while the answer - which
rule kept saying no, and by how much - was computed on every cycle and
thrown away.

WHAT THIS MODULE IS

Pure. It shapes a verdict from bot_mandates.validate_entry_verbose into a
row and a sentence. It opens no session and imports no database, so it
can be tested without one and cannot itself be the thing that breaks a
trading cycle. The caller persists.
"""
from __future__ import annotations

import json as _json

__all__ = ["MAX_CHECKS_JSON", "row_from_verdict", "explain", "summarise"]

#: A decision carries a handful of rules. A payload far past that is a
#: symptom, not a record, and is truncated rather than allowed to bloat
#: every row in the table.
MAX_CHECKS_JSON = 8000


def row_from_verdict(verdict, *, bot, rsi=None, buying_power=None,
                     open_positions=None, total_notional=None, equity=None):
    """The fields of one TradeDecision, as a plain dict.

    Returns None when handed something that is not a verdict - a caller
    that cannot describe its decision must write nothing rather than a row
    of NULLs that looks like a recorded decision.
    """
    if not isinstance(verdict, dict) or "admitted" not in verdict:
        return None
    checks = verdict.get("checks") or []
    try:
        blob = _json.dumps(checks, default=str)
    except (TypeError, ValueError):
        blob = None
    if blob is not None and len(blob) > MAX_CHECKS_JSON:
        blob = _json.dumps({"truncated": True, "check_count": len(checks),
                            "names": [c.get("name") for c in checks]})
    failed = verdict.get("failed") or []
    return {
        "bot": bot,
        "symbol": verdict.get("symbol"),
        "direction": verdict.get("direction"),
        "mandate": verdict.get("mandate"),
        "admitted": bool(verdict.get("admitted")),
        "reason": verdict.get("reason"),
        "failed_rules": ",".join(failed) if failed else None,
        "checks_json": blob,
        "rsi": rsi,
        "buying_power": buying_power,
        "open_positions": open_positions,
        "total_notional": total_notional,
        "equity": equity,
    }


def explain(verdict):
    """One sentence a person can read, naming the rules that decided it.

    An admitted trade names what it CLEARED, not just that it passed -
    "OK" is the string this whole module exists because of.
    """
    if not isinstance(verdict, dict) or "admitted" not in verdict:
        return "no decision was recorded"
    checks = verdict.get("checks") or []
    sym = verdict.get("symbol") or "?"
    if not verdict.get("admitted"):
        failed = [c for c in checks if c.get("passed") is False]
        why = "; ".join(c.get("detail") or c.get("name") for c in failed)
        return f"{sym} REFUSED - {why or verdict.get('reason') or 'no reason recorded'}"
    passed = [c for c in checks if c.get("passed") is True]
    blocked = [c.get("name") for c in checks if c.get("passed") is None]
    line = (f"{sym} ADMITTED - cleared "
            + "; ".join(c.get("detail") or c.get("name") for c in passed)
            if passed else f"{sym} ADMITTED - but no rule was actually evaluated")
    if blocked:
        line += (f". {len(blocked)} rule(s) were NOT evaluated ({', '.join(blocked)}) - "
                 f"a rule that never ran is not a rule that was satisfied")
    return line


def summarise(rows):
    """Why a bot has or has not been trading, over a set of decisions."""
    rows = list(rows or ())
    if not rows:
        return {"decisions": 0, "admitted": 0, "refused": 0, "top_blockers": [],
                "detail": ("no decisions recorded in this window - which is itself a "
                           "finding if the bot was meant to be running")}
    admitted = sum(1 for r in rows if (r.get("admitted") if hasattr(r, "get")
                                       else getattr(r, "admitted", None)))
    counts = {}
    for r in rows:
        get = r.get if hasattr(r, "get") else (lambda k, d=None: getattr(r, k, d))
        for name in (get("failed_rules") or "").split(","):
            if name:
                counts[name] = counts.get(name, 0) + 1
    top = sorted(counts.items(), key=lambda kv: -kv[1])
    return {
        "decisions": len(rows),
        "admitted": admitted,
        "refused": len(rows) - admitted,
        "top_blockers": [{"rule": k, "count": v} for k, v in top],
        "detail": (
            f"{admitted} of {len(rows)} decision(s) admitted a trade."
            + (f" The rule refusing most often was {top[0][0]} ({top[0][1]} time(s))."
               if top else " Nothing was refused by a named rule.")),
    }

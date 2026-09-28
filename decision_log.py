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

__all__ = ["MAX_CHECKS_JSON", "refusal", "row_from_verdict", "explain",
           "summarise"]

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
    # A fleet-wide refusal - a kill condition - has no symbol. "?" read
    # like a missing field rather than a deliberate absence.
    sym = verdict.get("symbol") or "the account"
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


def summarise(rows, returned=None, capped=False, sample_reason=None):
    """Why a bot has or has not been trading, over a set of decisions.

    `rows` is the WHOLE WINDOW, not the page the caller is displaying.
    `returned` is how many of them that page actually lists, and is
    reported beside the window count rather than in place of it.

    THE BUG THIS SHAPE EXISTS TO STOP. The endpoint used to hand this
    function its limited page, so the same 24-hour window read:

        limit=3   -> "0 of 3 decision(s) admitted a trade."
        limit=25  -> "0 of 25 decision(s) admitted a trade."
        limit=200 -> "0 of 89 decision(s) admitted a trade."

    89 was the truth and the default limit is 100, so any busier day
    silently reported exactly 100. top_blockers was worse than the
    total: tallied over the page, the rule that refused most across a
    day could be under-counted, or one that happened to fill the newest
    page promoted to the top of the list a reader uses to decide what
    to fix.

    `capped` says the window was larger than the caller's safety cap, in
    which case every figure here is a FLOOR and says so - reporting a
    slice as an exact total is the same lie one layer down.
    """
    rows = list(rows or ())
    n = len(rows)
    shown = n if returned is None else int(returned)
    if not rows:
        return {"decisions": 0, "admitted": 0, "refused": 0, "top_blockers": [],
                # Present on every verdict, same rule the other summary
                # fields follow - a caller should never have to tell
                # "no single condition" from "this shape has no such key".
                "blocking_condition": None,
                "returned": shown, "capped": bool(capped),
                "detail": ("no decisions recorded in this window - which is itself a "
                           "finding if the bot was meant to be running")}
    admitted = sum(1 for r in rows if (r.get("admitted") if hasattr(r, "get")
                                       else getattr(r, "admitted", None)))
    counts = {}
    for r in rows:
        get = r.get if hasattr(r, "get") else (lambda k, d=None: getattr(r, k, d))
        # TWO SHAPES REACH HERE AND THEY ARE NOT THE SAME TYPE.
        # row_from_verdict stores failed_rules as a comma-joined STRING,
        # which is how it goes into the column. TradeDecision.to_dict
        # splits it back into a LIST - and the endpoint passes to_dict
        # output. Calling .split on the list raised AttributeError and
        # returned a 500.
        #
        # It only ever fired once a row with failed_rules existed, so the
        # endpoint worked perfectly while the table was empty and broke
        # the moment the kill-condition logging gave it something to
        # read. My tests fed it row_from_verdict output and never the
        # to_dict output the caller actually sends.
        raw = get("failed_rules")
        names = raw.split(",") if isinstance(raw, str) else (raw or ())
        for name in names:
            name = str(name).strip()
            if name:
                counts[name] = counts.get(name, 0) + 1
    top = sorted(counts.items(), key=lambda kv: -kv[1])
    at_least = "at least " if capped else ""
    detail = (f"{admitted} of {at_least}{n} decision(s) admitted a trade."
              + (f" The rule refusing most often was {top[0][0]} "
                 f"({at_least}{top[0][1]} time(s))."
                 if top else " Nothing was refused by a named rule."))

    blocking = _one_blocking_condition(rows, counts, admitted,
                                       sample_reason=sample_reason)
    if blocking:
        detail += " " + blocking["detail"]

    if shown < n:
        detail += f" {shown} of them are shown below."
    if capped:
        detail += (" The window holds more than the count cap, so these are floors, "
                   "not totals.")
    return {
        "decisions": n,
        "admitted": admitted,
        "refused": n - admitted,
        "top_blockers": [{"rule": k, "count": v} for k, v in top],
        "blocking_condition": blocking,
        "returned": shown,
        "capped": bool(capped),
        "detail": detail,
    }


def _one_blocking_condition(rows, counts, admitted, sample_reason=None):
    """When every refusal in the window is the SAME rule, say so once -
    and say which way the blocking number is moving.

    WHY THIS EXISTS. On 2026-09-28 /mandates/decisions read "106
    decisions, 0 admitted, the rule refusing most often was
    kill_condition (106 times)" for three consecutive review passes. It
    reads like 106 independent failures of an unnamed rule, so each pass
    re-flagged it as unexplained and nobody looked.

    All 106 were one condition on the prop_apex mandate: buying power
    under the $150 floor, because $732.60 of $810.64 cash was unsettled
    sale proceeds in a CASH account. The guard was working perfectly.
    But the summary could not distinguish that from a stuck flag, and
    it could not show the part that actually mattered: across 5h13m
    buying power went $92.98 -> $78.04 while unsettled rose by exactly
    the same $14.94. The block was getting FURTHER away from clearing,
    and the alarm read identically either way.

    Same shape as the maker_only recency fix. An alarm that reads the
    same whether a condition is clearing or worsening is one people
    stop reading - and this one had already been stopped reading.

    Only fires when the picture is unambiguous: nothing admitted, one
    rule, every refusal carrying it. Otherwise returns None and the
    summary is unchanged, because a mixed window has no single story.
    """
    if admitted or len(counts) != 1:
        return None
    rule, count = next(iter(counts.items()))
    if count != len(rows):
        return None          # some refusal carried a different rule

    def g(r, k, d=None):
        return (r.get(k, d) if hasattr(r, "get") else getattr(r, k, d))

    stamped = []
    for r in rows:
        at = g(r, "decided_at")
        val = g(r, "buying_power")
        if at is None:
            continue
        stamped.append((str(at), None if val is None else float(val)))
    stamped.sort()

    # The caller may pass the reason separately: the window projection
    # that feeds this is deliberately narrow and does not carry free
    # text. Falling back to the row keeps the pure-function tests honest.
    reason = sample_reason if sample_reason else g(rows[0], "reason")
    out = {"rule": rule, "count": count,
           "reason": str(reason or "")[:400],
           "first_at": stamped[0][0] if stamped else None,
           "last_at": stamped[-1][0] if stamped else None}

    vals = [v for _, v in stamped if v is not None]
    if len(vals) >= 2:
        first, last = vals[0], vals[-1]
        out.update(first_value=round(first, 2), last_value=round(last, 2),
                   change=round(last - first, 2),
                   min_value=round(min(vals), 2), max_value=round(max(vals), 2))
        if last > first:
            way = f"IMPROVING: {first:,.2f} -> {last:,.2f}"
        elif last < first:
            way = f"WORSENING: {first:,.2f} -> {last:,.2f}"
        else:
            way = f"FLAT at {last:,.2f}"
        out["trend"] = way
        out["detail"] = (f"Every one of the {count} refusals is the SAME condition "
                         f"({rule}), not {count} separate problems. The blocking "
                         f"value is {way} across the window, best it reached was "
                         f"{max(vals):,.2f}. Reason given: {out['reason']}")
    else:
        out["trend"] = "UNKNOWN"
        out["detail"] = (f"Every one of the {count} refusals is the SAME condition "
                         f"({rule}), not {count} separate problems. The blocking "
                         f"value was not recorded, so whether it is clearing cannot "
                         f"be established. Reason given: {out['reason']}")
    return out


def refusal(symbol, rule, detail, *, mandate=None, direction=None,
            value=None, threshold=None):
    """A verdict for a refusal decided BEFORE the mandate check runs.

    THE HOLE THIS FILLS. The first hook went in at the mandate check,
    which is the LAST gate in try_open - two refusals return above it:

        contract not in approved_universe   -> return False
        symbol in excluded_symbols          -> return False

    Both are ordinary, both are frequent, and neither wrote a row. So the
    decision log recorded only the decisions that survived far enough to
    be interesting, and stayed empty the rest of the time - which reads
    exactly like a bot that is not running. Checked live an hour after
    shipping: the endpoint returned zero rows.

    Shaped identically to validate_entry_verbose's output so one row
    format covers every refusal, wherever in the path it was decided.
    """
    return {
        "admitted": False,
        "reason": detail,
        "checks": [{"name": rule, "passed": False, "value": value,
                    "threshold": threshold, "detail": detail}],
        "failed": [rule],
        "blocked": None,
        "mandate": mandate,
        "direction": direction,
        "symbol": symbol,
    }

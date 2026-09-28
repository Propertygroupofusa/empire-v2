#!/usr/bin/env python3
"""Did each bot stay inside its mandate? Measured, not assumed.

THE FAILURE THIS IS BUILT TO AVOID

.claude/MANDATE_INTEGRATION_PLAN.md sketches this check as

    for bot_name, mandate in ALL_MANDATES.items():
        off_universe = db.query(ClosedTrade).filter(
            ClosedTrade.bot == bot_name,
            ~ClosedTrade.symbol.in_(mandate["universe"])
        ).all()

Written that way it would have reported ZERO VIOLATIONS FOREVER, for
three independent reasons, and a compliance report that always says
"compliant" is worse than no report - it is a false assurance somebody
will act on.

  1. THE BOT NAMES DO NOT MATCH. Three namespaces are in play:

         mandate key   mandate["name"]        ClosedTrade.bot
         apex          prop_bot               prop_apex
         crypto        crypto_coinbase_bot    crypto_coinbase
         alpaca        alpaca_bot             alpaca_swing

     `ClosedTrade.bot == bot_name` compares against the KEY, which no row
     ever carries. Neither does mandate["name"].

  2. THE UNIVERSE IS NOT A LIST. It is a dict of categories - futures,
     crypto, commodities, inverse_etfs, equities - plus a `restriction`
     key whose value is the English sentence "No other symbols allowed".
     Passing it to .in_() iterates its KEYS, so the allowed set would
     have been {"futures", "crypto", ...} and every real symbol would
     have been a violation - or, read the other way, the prose string
     would have been an allowed symbol.

  3. THE SYMBOL FORMATS DIFFER. The mandate says BTC/USD, the fleet
     records BTC-USD, and a venue may give BTCUSD. Compared raw, every
     crypto trade is off-universe.

So the rule this module is built on: WHEN IT CANNOT IDENTIFY SOMETHING,
IT SAYS SO. An unmappable bot, an unparseable universe and a symbol it
cannot normalise are each reported as UNKNOWN - never silently counted
as compliant, and never counted as a violation either.
"""
from __future__ import annotations

__all__ = ["normalise_symbol", "allowed_symbols", "bot_identities",
           "universe_violations", "capital_violations", "compliance"]

#: Keys inside a mandate's universe that are prose, not symbols.
_NON_SYMBOL_KEYS = {"restriction", "note", "notes"}


def normalise_symbol(sym):
    """BTC/USD, BTC-USD, btcusd -> BTC-USD. None when unreadable.

    Quote currencies are stripped to a canonical pair so the mandate's
    format and the venue's format compare equal. A bare equity or future
    (MES, AAPL) is returned uppercased and unchanged.
    """
    if sym is None:
        return None
    s = str(sym).strip().upper()
    if not s:
        return None
    for sep in ("/", "-", "_"):
        if sep in s:
            base, _, quote = s.partition(sep)
            return f"{base}-{quote}" if base and quote else None
    for quote in ("USDT", "USDC", "USD"):
        if s.endswith(quote) and len(s) > len(quote):
            return f"{s[:-len(quote)]}-{quote}"
    return s


def allowed_symbols(mandate):
    """Every symbol a mandate permits, normalised. None if unreadable.

    None means "this mandate does not state a universe" and must not be
    read as "nothing is allowed" - that would make every trade a
    violation.
    """
    universe = (mandate or {}).get("universe")
    if not isinstance(universe, dict):
        return None
    out = set()
    for key, value in universe.items():
        if key in _NON_SYMBOL_KEYS or isinstance(value, str):
            continue
        if not isinstance(value, (list, tuple, set)):
            continue
        for sym in value:
            norm = normalise_symbol(sym)
            if norm:
                out.add(norm)
    return out or None


def bot_identities(mandate):
    """Every name this mandate's bot might be recorded under.

    The mandate key, its `name`, and - because those two disagree with
    what the bots actually write - any `records_as` the mandate declares.
    Matching is done on this whole set rather than on one field, so a
    rename in either namespace cannot silently stop the check working.
    """
    out = set()
    for key in ("name", "key", "records_as"):
        v = (mandate or {}).get(key)
        if isinstance(v, str) and v.strip():
            out.add(v.strip())
        elif isinstance(v, (list, tuple, set)):
            out.update(str(x).strip() for x in v if str(x).strip())
    return out


def _trade(t):
    get = t.get if hasattr(t, "get") else (lambda k, d=None: getattr(t, k, d))
    return get


def universe_violations(trades, mandate):
    """(violations, unknown) - trades outside the mandate's universe.

    `unknown` holds trades whose symbol could not be normalised. They are
    neither a pass nor a violation, and they are reported.
    """
    allowed = allowed_symbols(mandate)
    if allowed is None:
        return None, None
    bad, unknown = [], []
    for t in (trades or ()):
        get = _trade(t)
        norm = normalise_symbol(get("symbol"))
        if norm is None:
            unknown.append({"symbol": get("symbol"), "id": get("id")})
        elif norm not in allowed:
            bad.append({"symbol": get("symbol"), "normalised": norm, "id": get("id"),
                        "closed_at": str(get("closed_at") or "")})
    return bad, unknown


def capital_violations(trades, mandate):
    """Trades whose notional exceeded the mandate's per-position limit."""
    cap = ((mandate or {}).get("capital") or {}).get("max_per_position")
    try:
        limit = abs(float(cap))
    except (TypeError, ValueError):
        return None
    if limit <= 0:
        return None
    out = []
    for t in (trades or ()):
        get = _trade(t)
        try:
            notional = abs(float(get("qty")) * float(get("entry_price")))
        except (TypeError, ValueError):
            continue
        if notional > limit:
            out.append({"symbol": get("symbol"), "id": get("id"),
                        "notional_usd": round(notional, 2), "limit_usd": limit,
                        "over_by_usd": round(notional - limit, 2)})
    return out


def compliance(trades, mandate, bot_key=None):
    """One bot's adherence, with every figure it was derived from.

    `score` is the share of trades that broke no rule this module could
    actually evaluate. It is None - never 100 - when nothing could be
    evaluated, because a check that could not run is not a pass.
    """
    rows = list(trades or ())
    uni, uni_unknown = universe_violations(rows, mandate)
    cap = capital_violations(rows, mandate)

    checks_ran = [c for c in (uni, cap) if c is not None]
    blocked = []
    if uni is None:
        blocked.append("universe: the mandate states no readable symbol list")
    if cap is None:
        blocked.append("capital: the mandate states no max_per_position")

    offending = {v["id"] for c in checks_ran for v in c if v.get("id") is not None}
    score = None
    if rows and checks_ran:
        score = round((len(rows) - len(offending)) / len(rows) * 100, 2)

    return {
        "bot": bot_key,
        "identities": sorted(bot_identities(mandate)),
        "trades_examined": len(rows),
        "universe_violations": uni,
        "universe_unreadable": uni_unknown or None,
        "capital_violations": cap,
        "checks_blocked": blocked or None,
        "compliance_pct": score,
        "status": ("UNKNOWN" if score is None
                   else "COMPLIANT" if score >= 95.0 else "VIOLATING"),
        "detail": (
            f"{len(rows)} trade(s) examined against {len(checks_ran)} of 2 checks."
            + (f" {len(uni)} outside the universe." if uni else "")
            + (f" {len(cap)} over the per-position limit." if cap else "")
            + (f" {len(uni_unknown)} symbol(s) could not be normalised and are in "
               f"neither figure." if uni_unknown else "")
            + (" " + "; ".join(blocked) + "." if blocked else "")
            + ("" if score is not None else
               " No check could run, so there is no score - a check that could not "
               "run is not a pass.")),
    }

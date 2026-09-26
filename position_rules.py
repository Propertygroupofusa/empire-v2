"""A rule for every holding, not just the two that broke the 20% rule.

WHY

The concentration limit is a CEILING. It has something to say about ZEC
at 25% and XRP at 21% and nothing whatsoever to say about the other
forty-one positions, which is most of the account by count and 15% of it
by value. On 2026-09-26 the book looked like this:

    2 coins   $5,231.45   46.5%   over the 20% ceiling
    4 coins   $4,348.11   38.7%   the real portfolio
    6 coins     $894.96    8.0%   small but meaningful
   31 coins     $768.15    6.8%   24 of them under $50

A rule that only fires on the top two leaves thirty-one positions in a
state where nothing can ever happen to them. They are not held for a
reason; they are held because no rule reaches them.

WHAT A TIER IS FOR

Size decides what a position can DO, so size decides which rule applies:

  ANCHOR      over the ceiling. One bad week here moves the account.
              The rule is: trim it back under.
  CORE        big enough to matter, small enough to keep. The rule is a
              stop. There is nothing to trim and nothing to consolidate.
  SATELLITE   a real position but not a driver. Same rule as CORE; the
              tier exists so the difference is visible rather than implied.
  TAIL        under a percent of the account. A 40% move here changes the
              book by less than half a percent, and it still costs a stop
              to watch and a line to read. The rule is: consolidate it
              into cash, where it can be deployed as one usable amount
              instead of thirty unusable ones.
  STRANDED    worth less than the venue will let you sell. There is no
              rule, because there is no available action. Naming it is
              the whole contribution - a position that cannot be exited
              should never be counted as risk that is being managed.
  BLIND       no price. Not small, UNKNOWN. Nothing is decided here.

WHAT THIS DOES NOT CLAIM

Consolidating the tail does not make money. It converts thirty positions
that cannot be traded into cash that can be, and it stops the watch list
pretending to manage $3 of AERGO. Whether that cash then earns anything
depends entirely on the entry rule, which as of today has qualified 0 of
1,080 scans. Simplifying the book is a precondition for trading it, not
a substitute for having something worth trading.
"""
from __future__ import annotations

# Tier boundaries, as a share of the whole account.
ANCHOR_MIN_PCT = 20.0     # the owner's concentration ceiling
CORE_MIN_PCT = 5.0
SATELLITE_MIN_PCT = 1.0

# Below this a market sell is refused by the venue or eaten by the fee.
# Matches holdings_watch.MIN_EXITABLE_USD deliberately: two modules
# disagreeing about what "too small to sell" means is how a position ends
# up with a stop that can never be acted on.
MIN_EXITABLE_USD = 5.0

ANCHOR, CORE, SATELLITE, TAIL, STRANDED, BLIND, CASH = (
    "ANCHOR", "CORE", "SATELLITE", "TAIL", "STRANDED", "BLIND", "CASH")

# Cash is not a small position. It is the thing the tail is consolidated
# INTO, and a tier check that sorted $66 of USD into TAIL would have the
# trimmer sell dollars for dollars. Kept identical to
# account_census.STABLE so the two cannot drift apart.
STABLE = {"USD", "USDC", "USDT", "DAI", "PYUSD", "USDS"}

TRIM, HOLD_WITH_STOP, CONSOLIDATE, NO_ACTION_POSSIBLE, DECIDE_NOTHING = (
    "TRIM", "HOLD_WITH_STOP", "CONSOLIDATE", "NO_ACTION_POSSIBLE", "DECIDE_NOTHING")

RULES = {
    ANCHOR: {
        "action": TRIM,
        "rule": f"over {ANCHOR_MIN_PCT:.0f}% of the account",
        "does": f"sells back to {ANCHOR_MIN_PCT - 0.5:.1f}%",
        "why": "one bad week in a position this size moves the whole account",
    },
    CORE: {
        "action": HOLD_WITH_STOP,
        "rule": f"{CORE_MIN_PCT:.0f}-{ANCHOR_MIN_PCT:.0f}% of the account",
        "does": "keeps a trailing level and alerts on a break",
        "why": "big enough to matter, under the ceiling, nothing to trim",
    },
    SATELLITE: {
        "action": HOLD_WITH_STOP,
        "rule": f"{SATELLITE_MIN_PCT:.0f}-{CORE_MIN_PCT:.0f}% of the account",
        "does": "keeps a trailing level and alerts on a break",
        "why": "a real position, but not one that drives the account",
    },
    TAIL: {
        "action": CONSOLIDATE,
        "rule": f"under {SATELLITE_MIN_PCT:.0f}% of the account, still sellable",
        "does": "sells into cash",
        "why": ("too small to change the account whatever it does, and it still "
                "costs a level to watch and a line to read"),
    },
    STRANDED: {
        "action": NO_ACTION_POSSIBLE,
        "rule": f"under ${MIN_EXITABLE_USD:.0f}, the smallest the venue will sell",
        "does": "nothing - it cannot be sold",
        "why": ("a position with no available exit is not risk being managed, "
                "and counting it as covered would be a lie"),
    },
    CASH: {
        "action": DECIDE_NOTHING,
        "rule": "dollars, or a token pegged to them",
        "does": "nothing - this is what the tail is sold into",
        "why": "cash is the destination, never a position to be trimmed",
    },
    BLIND: {
        "action": DECIDE_NOTHING,
        "rule": "no price available",
        "does": "nothing until it can be priced",
        "why": "unknown is not the same as small, and nothing is decided on a guess",
    },
}


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def classify(usd, total_usd, *, min_exitable_usd=MIN_EXITABLE_USD, asset=None):
    """Which tier a holding is in. BLIND whenever that cannot be decided.

    BLIND is the answer for an unreadable holding AND an unreadable total.
    Neither is the same as "small", and the whole module exists to stop a
    missing number being read as a harmless one.
    """
    if asset and str(asset).upper() in STABLE:
        return CASH
    u = _num(usd)
    t = _num(total_usd)
    if u is None or t is None or t <= 0 or u < 0:
        return BLIND
    floor = _num(min_exitable_usd)
    if floor is not None and u < floor:
        return STRANDED
    share = u / t * 100.0
    if share > ANCHOR_MIN_PCT:
        return ANCHOR
    if share >= CORE_MIN_PCT:
        return CORE
    if share >= SATELLITE_MIN_PCT:
        return SATELLITE
    return TAIL


def assess(holding, total_usd, *, min_exitable_usd=MIN_EXITABLE_USD):
    """One holding's tier, rule and available action."""
    asset = (holding.get("asset") or "").upper() if hasattr(holding, "get") else ""
    usd = _num(holding.get("usd")) if hasattr(holding, "get") else None
    t = _num(total_usd)
    tier = classify(usd, total_usd, min_exitable_usd=min_exitable_usd, asset=asset)
    r = RULES[tier]
    return {
        "asset": asset,
        "usd": round(usd, 2) if usd is not None else None,
        "share_pct": round(usd / t * 100.0, 2) if (usd is not None and t) else None,
        "tier": tier,
        "action": r["action"],
        "rule": r["rule"],
        "does": r["does"],
        "why": r["why"],
    }


def book(holdings, total_usd, *, min_exitable_usd=MIN_EXITABLE_USD, unpriced=()):
    """Every holding, tiered, plus what each tier adds up to.

    `unpriced` is the census's list of assets it could not price. They are
    folded in as BLIND rather than left out: a holding missing from the
    rule book reads as a holding with no risk, which is the opposite of
    what an unpriced position is.
    """
    rows = [assess(h, total_usd, min_exitable_usd=min_exitable_usd)
            for h in (holdings or ())]
    for u in unpriced or ():
        rows.append({
            "asset": (u.get("asset") or "").upper(), "usd": None, "share_pct": None,
            "tier": BLIND, "action": DECIDE_NOTHING, "rule": RULES[BLIND]["rule"],
            "does": RULES[BLIND]["does"], "why": RULES[BLIND]["why"],
        })
    rows.sort(key=lambda r: -(r["usd"] or 0))

    tiers = {}
    for name in (ANCHOR, CORE, SATELLITE, TAIL, STRANDED, CASH, BLIND):
        sel = [r for r in rows if r["tier"] == name]
        tiers[name] = {
            "count": len(sel),
            "usd": round(sum(r["usd"] or 0 for r in sel), 2),
            "share_pct": (round(sum(r["usd"] or 0 for r in sel) / _num(total_usd) * 100.0, 2)
                          if _num(total_usd) else None),
            "action": RULES[name]["action"],
            "rule": RULES[name]["rule"],
            "does": RULES[name]["does"],
            "why": RULES[name]["why"],
            "assets": [r["asset"] for r in sel],
        }

    actionable = [r for r in rows if r["action"] in (TRIM, CONSOLIDATE)]
    return {
        "as_of_total_usd": _num(total_usd),
        "positions": len(rows),
        "tiers": tiers,
        "rows": rows,
        "actionable_count": len(actionable),
        "actionable_usd": round(sum(r["usd"] or 0 for r in actionable), 2),
        "headline": _headline(tiers, total_usd),
    }


def _headline(tiers, total_usd):
    t = _num(total_usd)
    tail, stranded, blind = tiers[TAIL], tiers[STRANDED], tiers[BLIND]
    anchor = tiers[ANCHOR]
    bits = []
    if anchor["count"]:
        bits.append(f"{anchor['count']} over the ceiling holding "
                    f"${anchor['usd']:,.2f}")
    if tail["count"]:
        bits.append(f"{tail['count']} in the tail holding ${tail['usd']:,.2f}"
                    + (f", {tail['share_pct']}% of the account" if t else ""))
    if stranded["count"]:
        bits.append(f"{stranded['count']} stranded below the venue minimum "
                    f"(${stranded['usd']:,.2f}, no exit exists)")
    if blind["count"]:
        bits.append(f"{blind['count']} that cannot be priced")
    return "; ".join(bits) if bits else "Every position is in a tier with a rule."

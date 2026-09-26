"""The Empire Excellence League: every holding as a competitor.

WHY THE FRAMING EARNS ITS PLACE

A wallet is a list. A league table is a standing, and a standing invites
the questions that actually matter about a portfolio: who is leading, who
is slipping, who has improved, who does not belong in this division. The
numbers are identical. The framing is what gets them looked at.

THE RULES ARE THE SAME RULES

Division and rank come from newsroom_brief.quant_score - the same measured
inputs already on air: headroom above the coin's own stop level, inverse
daily volatility, and whether it respects the owner's 20% concentration
rule. No new scoring system, because two scoring systems means two answers
to one question, and this codebase has already paid for that lesson with
three P&L formulas and eleven corrupt ledger rows.

HOW "MOST IMPROVED" WORKS WITHOUT A DATABASE

Movement needs history, and this system cannot write one - DASHBOARD_WRITE_
TOKEN is unset, which is exactly when the league is most useful. So the
past standing is RECOMPUTED from candles: the score each coin would have
had N days ago, using the price, peak and volatility that existed then.
Stateless, reproducible, and it cannot drift the way a stored snapshot does
when the updater stops running.

The one assumption, stated because it is load-bearing: unit holdings are
taken as unchanged over the window. If a position was bought or sold inside
it, that coin's movement is wrong. The fleet has placed no trades in this
period - 910 scans, 0 qualified - so it currently holds. It will not hold
forever, and any caller that knows better should pass units_then.

WHAT THE TABLE WILL NOT DO

It will not promote a coin the desk cannot measure. UNPRICED, dust and
no-volatility holdings go to an explicit UNRATED section rather than being
dropped or given a placeholder rank - a league that quietly omits its worst
members is a trophy cabinet, not a standing.
"""
from __future__ import annotations

import newsroom_brief

# Score bands. Chosen to describe the distribution the account actually
# has, not to flatter it: on 2026-09-26 the live book put nothing above 85.
ELITE_MIN = 80
CHALLENGER_MIN = 60
PROSPECT_MIN = 0

ELITE = "ELITE DIVISION"
CHALLENGER = "CHALLENGER DIVISION"
PROSPECT = "PROSPECT DIVISION"
UNRATED = "UNRATED"

# A coin has to move this many points for the desk to call it a story.
# Below it, movement is the score's own arithmetic wobbling.
MIN_IMPROVEMENT_POINTS = 3


def division_for(score):
    if score is None:
        return UNRATED
    if score >= ELITE_MIN:
        return ELITE
    if score >= CHALLENGER_MIN:
        return CHALLENGER
    return PROSPECT


def standings(rows, account_total_usd: float = 0.0) -> list:
    """Rank every holding. Unmeasurable ones are listed, not hidden."""
    table = []
    for r in rows:
        q = newsroom_brief.quant_score(r)
        score = (q or {}).get("score")
        table.append({
            "asset": r.get("asset"),
            "score": score,
            "components": (q or {}).get("components"),
            "division": division_for(score),
            "usd": newsroom_brief._f(r.get("usd"), 0.0),
            "share_pct": newsroom_brief._f(r.get("share_of_account_pct")),
            "status": r.get("status"),
            "trend": newsroom_brief.trend_of(r),
            "unrated_reason": (None if score is not None else
                               _unrated_reason(r)),
        })
    rated = [t for t in table if t["score"] is not None]
    unrated = [t for t in table if t["score"] is None]
    rated.sort(key=lambda t: (-t["score"], -(t["usd"] or 0)))
    for i, t in enumerate(rated, 1):
        t["rank"] = i
    unrated.sort(key=lambda t: -(t["usd"] or 0))
    for t in unrated:
        t["rank"] = None
    return rated + unrated


def _unrated_reason(row) -> str:
    st = row.get("status")
    return {
        "UNPRICED": "cannot be priced - unknown, not small",
        "BELOW_MIN_TRADE": "below the venue minimum - no exit exists",
        "NO_VOLATILITY_DATA": "too little price history to score",
    }.get(st, "missing an input the score needs")


def movement(now: list, then: list) -> list:
    """Rank and score change per coin, with the ones that can't move said so.

    A coin absent from either standing gets movement of None rather than a
    fabricated zero. "No change" and "we could not compute it" are different
    statements and a league that renders them identically is lying by
    omission.
    """
    then_by = {t["asset"]: t for t in then or ()}
    out = []
    for t in now:
        prev = then_by.get(t["asset"])
        row = dict(t)
        if not prev or prev.get("score") is None or t.get("score") is None:
            row["score_change"] = None
            row["rank_change"] = None
            row["previous_score"] = (prev or {}).get("score")
            row["previous_division"] = (prev or {}).get("division")
            row["movement_note"] = (
                "no comparable standing for this coin in the earlier window"
                if not prev or prev.get("score") is None else
                "not scoreable now")
        else:
            row["previous_score"] = prev["score"]
            row["previous_division"] = prev["division"]
            row["score_change"] = t["score"] - prev["score"]
            row["rank_change"] = ((prev["rank"] - t["rank"])
                                  if prev.get("rank") and t.get("rank") else None)
            row["movement_note"] = None
        row["promoted"] = bool(
            row.get("previous_division") and row["previous_division"] != t["division"]
            and _rank_of_division(t["division"]) < _rank_of_division(row["previous_division"]))
        row["relegated"] = bool(
            row.get("previous_division") and row["previous_division"] != t["division"]
            and _rank_of_division(t["division"]) > _rank_of_division(row["previous_division"]))
        out.append(row)
    return out


def _rank_of_division(d):
    return {ELITE: 0, CHALLENGER: 1, PROSPECT: 2, UNRATED: 3}.get(d, 9)


def most_improved(moved: list):
    """The medal - or None, honestly, when nothing moved enough to earn it.

    A "Most Improved" that always finds a winner is a participation trophy.
    On a quiet week the right answer is that nobody earned it.
    """
    cands = [m for m in moved
             if m.get("score_change") is not None
             and m["score_change"] >= MIN_IMPROVEMENT_POINTS]
    if not cands:
        return None
    cands.sort(key=lambda m: (-m["score_change"], -(m["usd"] or 0)))
    w = cands[0]
    reasons = []
    c = w.get("components") or {}
    if c.get("headroom", 0) >= 0.7:
        reasons.append("clear of its stop level")
    if c.get("steadiness", 0) >= 0.5:
        reasons.append("low daily volatility")
    if c.get("weight_ok"):
        reasons.append("inside the 20% concentration rule")
    if w.get("promoted"):
        reasons.append(f"promoted to {w['division'].split()[0].title()}")
    return {
        "asset": w["asset"],
        "improvement_points": w["score_change"],
        "from_score": w["previous_score"],
        "to_score": w["score"],
        "rank_change": w.get("rank_change"),
        "reasons": reasons or ["improved on the measured inputs"],
    }


def build(rows_now, rows_then=None, window_days: int = 7,
          account_total_usd: float = 0.0) -> dict:
    """The whole league, ready for air."""
    now = standings(rows_now, account_total_usd)
    then = standings(rows_then, account_total_usd) if rows_then else []
    moved = movement(now, then) if then else [dict(t, score_change=None,
                                                   rank_change=None,
                                                   previous_score=None,
                                                   previous_division=None,
                                                   promoted=False,
                                                   relegated=False,
                                                   movement_note="no earlier window available")
                                              for t in now]
    divisions = {}
    for m in moved:
        divisions.setdefault(m["division"], []).append(m)
    rated = [m for m in moved if m.get("score") is not None]
    leader = rated[0] if rated else None
    chaser = rated[1] if len(rated) > 1 else None
    return {
        "window_days": window_days,
        "has_history": bool(then),
        "table": moved,
        "divisions": divisions,
        "division_bands": {ELITE: f"{ELITE_MIN}+",
                           CHALLENGER: f"{CHALLENGER_MIN}-{ELITE_MIN - 1}",
                           PROSPECT: f"under {CHALLENGER_MIN}"},
        "leader": leader,
        "gap_to_leader": ((leader["score"] - chaser["score"])
                          if leader and chaser else None),
        "chaser": chaser,
        "most_improved": most_improved(moved),
        "promoted": [m for m in moved if m.get("promoted")],
        "relegated": [m for m in moved if m.get("relegated")],
        "unrated": [m for m in moved if m.get("score") is None],
        "assumption": ("Unit holdings are taken as unchanged over the window. "
                       "The fleet has placed no trades in this period, so it "
                       "holds today; a caller that knows better should supply "
                       "the earlier units."),
        "basis": ("Rank and division come from the same quant score already on "
                  "air - headroom above the coin's own stop, inverse daily "
                  "volatility, and the 20% concentration rule. No second "
                  "scoring system."),
    }

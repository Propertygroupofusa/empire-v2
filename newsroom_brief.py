"""The Empire Newsroom's editorial desk: real data, written as news.

WHY THIS EXISTS

Reading a balance sheet is work nobody does daily. A newscast is something
you watch. Same numbers, and the framing is the point - every coin is a
story, every breach is breaking news, every allocation is an editorial
decision.

THE ONE RULE

A newsroom that reports numbers nobody measured is a propaganda outlet.
Every figure on air here traces to a live endpoint. Where there is no
number, the desk SAYS there is no number, on air, rather than filling the
silence. Specifically:

  * The portfolio total is the census total. Not a target, not a goal.
  * The allocation is the REAL allocation. The gap between it and any
    intended one is itself a story, and the more interesting one.
  * Daily P&L is reported only if something actually measured it. It is
    not derived, not annualised, and not estimated from price drift.
  * The quant score is composed of stated, inspectable inputs, each of
    which is a real measurement, and the components ship alongside it.
    A bare 0-100 that nobody can decompose is a horoscope.

WHAT THE DESKS ARE

  Risk Desk        holdings_watch - where each position sits against a
                   2.5x-daily-volatility stop level
  Quant Desk       a transparent score from measured inputs only
  Producer Notes   the stories ranked by DOLLARS at stake, not by drama
  Breaking News    status transitions worth interrupting for
  Capital Flow     the census, and the concentration rule the owner set
  Reporter Script  all of the above, read aloud
"""
from __future__ import annotations

# The owner's own rule, 2026-09-26: "What you mean that one coin will take
# up 20% of the fleet? I don't think I want that."
CONCENTRATION_LIMIT_PCT = 20.0

# A story has to move a real amount of money to lead. Below this a breach
# is true and still not news - ALEO fell 43.6% and that is $5.97.
MIN_STORY_USD = 25.0


def _f(x, default=None):
    try:
        v = float(x)
        return v if v == v else default
    except (TypeError, ValueError):
        return default


def quant_score(row: dict):
    """0-100 from measured inputs, with every component returned beside it.

    Three things this account has actually measured, and nothing else:

      headroom   how far the position sits above its own stop level, as a
                 share of the stop distance. Real, current, per-coin.
      steadiness inverse daily volatility. A coin that moves 2% a day
                 survives a wobble that takes out one moving 15%.
      weight     whether the position respects the owner's 20% rule.

    Deliberately NOT included: price targets, sentiment, on-chain metrics,
    anything from a model this repository has not validated. A score is
    only as honest as its worst input, and an invented input makes the
    whole number fiction no matter how good the others are.

    Returns None when the inputs do not exist. A scoreless coin prints
    "NO DATA" on air, which is information; a defaulted 50 is not.
    """
    to_stop = _f(row.get("pct_to_stop"))
    stop_pct = _f(row.get("stop_pct"))
    vol = _f(row.get("daily_vol_pct"))
    if to_stop is None or stop_pct is None or vol is None or stop_pct <= 0:
        return None

    # Headroom: 0 at the stop, 100 once a full stop-distance clear of it.
    headroom = max(0.0, min(1.0, (to_stop / 100.0) / stop_pct))
    # Steadiness: 1% daily vol -> ~1.0, 10% -> ~0.1. Clamped, not tuned.
    steadiness = max(0.0, min(1.0, 2.0 / max(vol, 0.5) / 2.0 * 1.0)) if vol else 0.0
    steadiness = max(0.0, min(1.0, 2.0 / max(vol, 0.5)))
    share = _f(row.get("share_of_account_pct"), 0.0) or 0.0
    weight = 0.0 if share > CONCENTRATION_LIMIT_PCT else 1.0

    score = 100.0 * (0.55 * headroom + 0.30 * steadiness + 0.15 * weight)
    return {
        "score": int(round(max(0.0, min(100.0, score)))),
        "components": {
            "headroom": round(headroom, 3),
            "steadiness": round(steadiness, 3),
            "weight_ok": bool(weight),
        },
        "basis": ("0.55 x headroom above its own stop + 0.30 x inverse daily "
                  "volatility + 0.15 x respecting the 20% concentration rule. "
                  "No sentiment, no price targets, no unvalidated model."),
    }


def trend_of(row: dict) -> str:
    """A word for where the position sits. Not a forecast."""
    st = row.get("status")
    if st == "BREACHED":
        return "BREAKDOWN"
    if st == "NEAR_STOP":
        return "UNDER PRESSURE"
    to_stop = _f(row.get("pct_to_stop"))
    stop_pct = _f(row.get("stop_pct"))
    if to_stop is None or not stop_pct:
        return "NO DATA"
    headroom = (to_stop / 100.0) / stop_pct
    if headroom >= 0.75:
        return "HOLDING WELL"
    if headroom >= 0.4:
        return "STEADY"
    return "WATCH"


def risk_level(row: dict) -> tuple:
    """(level, action) - newsroom language for a measured position."""
    st = row.get("status")
    if st == "UNPRICED":
        return "UNKNOWN", "Cannot be priced - not a small position, an unknown one"
    if st == "BELOW_MIN_TRADE":
        return "STRANDED", "Below the venue minimum - no exit exists"
    if st == "NO_VOLATILITY_DATA":
        return "UNMEASURED", "Too little history to size a level"
    if st == "BREACHED":
        return "CRITICAL", "Past its level - decide, do not drift"
    if st == "NEAR_STOP":
        return "HIGH", "Within reach of its level"
    vol = _f(row.get("daily_vol_pct"))
    share = _f(row.get("share_of_account_pct"), 0.0) or 0.0
    if share > CONCENTRATION_LIMIT_PCT:
        return "CONCENTRATION", f"{share:.1f}% of the account, over the {CONCENTRATION_LIMIT_PCT:.0f}% rule"
    if vol is not None and vol >= 8.0:
        return "HIGH", "Speculative volatility - size accordingly"
    if vol is not None and vol >= 4.0:
        return "MEDIUM", "Normal volatility"
    return "LOW", "Core holding"


def producer_notes(watch: dict) -> list:
    """Stories ranked by DOLLARS at stake, never by how dramatic they read."""
    notes = []
    for r in watch.get("breached", []):
        usd = _f(r.get("usd"), 0.0) or 0.0
        if usd < MIN_STORY_USD:
            continue
        notes.append({"kind": "BREACH", "asset": r["asset"], "usd": usd,
                      "headline": f"{r['asset']} broke its level",
                      "detail": (f"${usd:,.2f} is {abs(_f(r.get('pct_from_peak'), 0)):.1f}% "
                                 f"off its peak, past a {_f(r.get('stop_pct'), 0) * 100:.2f}% stop.")})
    for r in watch.get("near", []):
        usd = _f(r.get("usd"), 0.0) or 0.0
        if usd < MIN_STORY_USD:
            continue
        notes.append({"kind": "PRESSURE", "asset": r["asset"], "usd": usd,
                      "headline": f"{r['asset']} is within reach of its level",
                      "detail": f"${usd:,.2f} sitting {_f(r.get('pct_to_stop'), 0):.2f}% above it."})
    for r in watch.get("over_concentration", []):
        usd = _f(r.get("usd"), 0.0) or 0.0
        notes.append({"kind": "CONCENTRATION", "asset": r["asset"], "usd": usd,
                      "headline": f"{r['asset']} is {_f(r.get('share_of_account_pct'), 0):.1f}% of the account",
                      "detail": f"Over the {CONCENTRATION_LIMIT_PCT:.0f}% rule you set."})
    unp = watch.get("unpriced") or []
    if unp:
        notes.append({"kind": "BLIND", "asset": ", ".join(x["asset"] for x in unp),
                      "usd": 0.0,
                      "headline": f"{len(unp)} asset(s) cannot be priced",
                      "detail": "Unknown, not small. Nothing can be said about them."})
    notes.sort(key=lambda n: -n["usd"])
    return notes


def breaking(watch: dict) -> list:
    """The ticker. Every line is a measured fact or it does not run."""
    out = []
    for r in watch.get("breached", []):
        out.append(f"{r['asset']} BREAKS LEVEL - {abs(_f(r.get('pct_from_peak'), 0)):.1f}% off peak, "
                   f"${_f(r.get('usd'), 0):,.0f} exposed")
    for r in watch.get("near", []):
        out.append(f"{r['asset']} approaching its level - {_f(r.get('pct_to_stop'), 0):.2f}% away, "
                   f"${_f(r.get('usd'), 0):,.0f}")
    for r in watch.get("over_concentration", []):
        out.append(f"CONCENTRATION - {r['asset']} at {_f(r.get('share_of_account_pct'), 0):.1f}% "
                   f"of the account, rule is {CONCENTRATION_LIMIT_PCT:.0f}%")
    cov = watch.get("covered_share_pct")
    if cov is not None:
        out.append(f"COVERAGE - {cov:.1f}% of coin value now has a level attached")
    if watch.get("dust_usd"):
        out.append(f"{len(watch.get('dust') or [])} positions stranded below the "
                   f"venue minimum, ${watch['dust_usd']:,.2f} total")
    if not out:
        out.append("No position is near its level. Quiet tape.")
    return out


def reporter_script(watch: dict, anchor: str = "Delfine") -> list:
    """What the anchor reads. Plain sentences, all of them checkable."""
    coin = _f(watch.get("coin_usd"), 0.0) or 0.0
    cash = _f(watch.get("cash_usd"), 0.0) or 0.0
    lines = [
        f"Good morning. This is {anchor}, reporting live from Empire Crypto News.",
        f"The book stands at ${coin + cash:,.2f} - ${coin:,.2f} in coin, "
        f"${cash:,.2f} in cash.",
    ]
    cov = watch.get("covered_share_pct")
    if cov is not None:
        lines.append(f"{cov:.1f} percent of that coin value now has a level attached to it. "
                     f"Until today, none of it did.")
    br = [r for r in watch.get("breached", []) if (_f(r.get("usd"), 0) or 0) >= MIN_STORY_USD]
    if br:
        names = ", ".join(r["asset"] for r in br)
        tot = sum(_f(r.get("usd"), 0) or 0 for r in br)
        lines.append(f"Our top story: {names} {'has' if len(br) == 1 else 'have'} broken "
                     f"below the level, ${tot:,.2f} in play.")
    nr = [r for r in watch.get("near", []) if (_f(r.get("usd"), 0) or 0) >= MIN_STORY_USD]
    if nr:
        names = ", ".join(f"{r['asset']} at {_f(r.get('pct_to_stop'), 0):.2f} percent" for r in nr)
        tot = sum(_f(r.get("usd"), 0) or 0 for r in nr)
        lines.append(f"Under pressure and worth watching: {names}. "
                     f"That is ${tot:,.2f} close to the line.")
    over = watch.get("over_concentration") or []
    if over:
        names = " and ".join(f"{r['asset']} at {_f(r.get('share_of_account_pct'), 0):.1f} percent"
                             for r in over)
        lines.append(f"On the editorial desk: {names}, against the twenty percent rule "
                     f"you set yourself.")
    unp = watch.get("unpriced") or []
    if unp:
        lines.append(f"And a correction we have to make on air: {len(unp)} holding"
                     f"{'s' if len(unp) != 1 else ''} cannot be priced right now. "
                     f"We are not going to guess at them.")
    lines.append("Capital preservation stays the lead. Compounding comes after the "
                 "profit is made, not before.")
    return lines


def current_story(watch: dict) -> str:
    """One sentence for the ON AIR card, written from the largest real story."""
    notes = producer_notes(watch)
    if not notes:
        return ("No position is near its level and nothing is over the "
                "concentration rule. Quiet tape.")
    lead = notes[0]
    second = notes[1]["headline"] if len(notes) > 1 else None
    return lead["headline"] + ". " + lead["detail"] + (f" Also developing: {second}." if second else "")


def build(watch: dict, anchor: str = "Delfine") -> dict:
    """The whole broadcast, from one live watch payload."""
    rows = [r for r in (watch.get("rows") or [])
            if r.get("status") in ("OK", "NEAR_STOP", "BREACHED")]
    desk = []
    for r in rows:
        q = quant_score(r)
        lvl, action = risk_level(r)
        desk.append({
            "asset": r["asset"], "usd": _f(r.get("usd"), 0.0),
            "share_pct": _f(r.get("share_of_account_pct")),
            "score": (q or {}).get("score"),
            "components": (q or {}).get("components"),
            "trend": trend_of(r),
            "risk_level": lvl, "action": action,
            "status": r.get("status"),
            "pct_to_stop": _f(r.get("pct_to_stop")),
            "stop_level": _f(r.get("stop_level")),
            "stop_pct": _f(r.get("stop_pct")),
            "daily_vol_pct": _f(r.get("daily_vol_pct")),
        })
    scored = [d for d in desk if d["score"] is not None]
    scored.sort(key=lambda d: -d["score"])
    coin = _f(watch.get("coin_usd"), 0.0) or 0.0
    cash = _f(watch.get("cash_usd"), 0.0) or 0.0
    return {
        "anchor": anchor,
        "network": "Empire Crypto News",
        "on_air": True,
        "current_story": current_story(watch),
        "producer_notes": producer_notes(watch),
        "breaking": breaking(watch),
        "quant_desk": scored,
        "risk_desk": sorted(desk, key=lambda d: -(d["usd"] or 0)),
        "capital": {
            "coin_usd": round(coin, 2),
            "cash_usd": round(cash, 2),
            "total_usd": round(coin + cash, 2),
            "covered_share_pct": watch.get("covered_share_pct"),
            "dust_usd": watch.get("dust_usd"),
            "unpriced_assets": watch.get("unpriced_assets"),
            "allocation": [
                {"asset": d["asset"], "usd": d["usd"], "share_pct": d["share_pct"]}
                for d in sorted(desk, key=lambda d: -(d["usd"] or 0))[:8]
            ],
            "daily_pnl_usd": None,
            "daily_pnl_note": ("Not reported. No measurement of today's P&L exists "
                               "that separates trading from price drift, and an "
                               "estimate here would be the one number on this "
                               "screen that nobody measured."),
        },
        "script": reporter_script(watch, anchor),
        "disclaimer": watch.get("disclaimer"),
        "as_of": watch.get("as_of"),
    }

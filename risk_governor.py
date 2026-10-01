"""Risk Governor - decides how much risk the Alpaca side is allowed.

WHAT THIS IS, AND WHAT IT DELIBERATELY IS NOT.

The design this implements proposed four risk tiers with automatic
PROMOTION between them, gated on:

    min_live_trades: 30, max_drawdown_pct: 7.0,
    min_rule_compliance_pct: 95.0, require_new_equity_high,
    require_database_healthy, require_reconciliation_healthy,
    require_no_circuit_breaker

Two of those gates were measured against this account's own 232 closed
round trips before any of this was written, and they do not do what they
look like they do:

    mean          -$0.0138 / trade
    sd             $1.7847
    95% CI        -$0.2434 .. +$0.2159   <- SPANS ZERO

After 232 trades the SIGN of this strategy's edge is still unresolved.
It is not established as losing; it is not established as winning. On
that distribution:

  * a 30-trade window shows a PROFIT 44.0% of the time. A gate that
    opens on a coin flip is not a gate.
  * the equity curve made a NEW HIGH 10 times across 232 trades while
    the whole record was -$3.20, so require_new_equity_high adds almost
    nothing on top.

Together those two would promote to a higher risk tier after a lucky
streak - increasing size precisely when recent results are most likely
to be noise. That is an anti-martingale running on randomness, and it is
the opposite of what a risk governor is for.

Detecting an edge of the measured size at 95% needs ~64,500 trades.
Even a $0.10/trade edge needs ~1,224. So promotion here cannot be a
trade COUNT; it has to be a statement about the confidence interval.

THEREFORE this module implements the asymmetry the design itself states
- "may automatically reduce risk, never automatically increase risk
unless every promotion gate passes" - by taking it literally:

  * DE-RISKING is automatic and needs no proof. Cutting size when
    drawdown deepens is safe in the direction it is wrong.
  * PROMOTION is computed and REPORTED, never applied. It additionally
    requires the 95% confidence interval on per-trade P&L to sit
    entirely above zero, which the record does not currently satisfy
    and will not for a long time.
  * UNKNOWN IS BLOCKED. A health field that could not be read is never
    treated as healthy.

Nothing in this file places, sizes or cancels a live order. It returns a
verdict. Wiring that verdict to real position sizing is a money-moving
change and is the account owner's to make.
"""
from __future__ import annotations

import math
from typing import Optional

# --------------------------------------------------------------------
# Tiers. risk_pct is the fraction of equity put at risk per position.
# --------------------------------------------------------------------
RISK_TIERS = {
    "STAGE_1": 0.004,
    "STAGE_2": 0.005,
    "STAGE_3": 0.006,
    "STAGE_4": 0.007,
}
TIER_ORDER = ["STAGE_1", "STAGE_2", "STAGE_3", "STAGE_4"]
BASE_TIER = "STAGE_1"

# De-risking ladder, by current drawdown from peak equity. Ordered
# deepest-first so the worst matching rung wins however it is scanned -
# a shallow-first scan with an early return is the classic way a ladder
# like this silently applies the mildest cut during the worst drawdown.
DERISK_LADDER = (
    (10.0, 0.25),   # >=10% drawdown -> quarter size
    (7.0, 0.50),    # >= 7%          -> half
    (5.0, 0.75),    # >= 5%          -> three-quarters
)

# Promotion gates. min_live_trades is kept from the original design, but
# it is a FLOOR, not the test - see MIN_TRADES_NOTE.
PROMOTION = {
    "min_live_trades": 30,
    "max_drawdown_pct": 7.0,
    "min_rule_compliance_pct": 95.0,
    "require_new_equity_high": True,
    "require_database_healthy": True,
    "require_reconciliation_healthy": True,
    "require_no_circuit_breaker": True,
    # The gate the original design was missing entirely: none of its
    # checks ask whether the strategy makes money.
    "require_edge_ci_above_zero": True,
}
MIN_TRADES_NOTE = (
    "30 trades is kept as a floor but is not the real test: on this "
    "account's measured distribution a 30-trade window shows a profit "
    "44% of the time. The binding gate is the confidence interval."
)

# Health fields that must each be explicitly True. Anything else -
# False, None, missing, unreadable - blocks.
REQUIRED_HEALTH = (
    "database_healthy",
    "reconciliation_healthy",
    "broker_reachable",
)


def derisk_multiplier(drawdown_pct: Optional[float]) -> float:
    """Size multiplier for the current drawdown. 1.0 means full size.

    An UNREADABLE drawdown returns the deepest cut, not full size. The
    fail-open version of this - treating a missing number as 0% drawdown
    - hands out full size at exactly the moment nobody can see how bad
    things are.
    """
    if drawdown_pct is None or not isinstance(drawdown_pct, (int, float)):
        return DERISK_LADDER[0][1]
    if isinstance(drawdown_pct, float) and math.isnan(drawdown_pct):
        return DERISK_LADDER[0][1]
    if drawdown_pct < 0:
        # A negative drawdown means equity is above its peak, which is
        # not a drawdown at all. Treat as none rather than as a reading
        # to act on.
        drawdown_pct = 0.0
    for threshold, mult in DERISK_LADDER:   # deepest first
        if drawdown_pct >= threshold:
            return mult
    return 1.0


def trading_approved(state: Optional[dict]) -> tuple:
    """(approved, reasons). Fail-closed.

    UNKNOWN STATE IS A BLOCKED STATE. Every required health field must
    be exactly True. A field that is missing, None, or not a bool is
    reported as unreadable and blocks - it is never read as healthy.

    This is the same shape as the auto_trim_worker fix: the bug there
    was `if _units:` collapsing an unreadable fleet into "protect
    nothing" without saying so.
    """
    reasons = []
    if state is None:
        return False, ["no risk state could be read at all - blocked"]
    if not isinstance(state, dict):
        return False, [f"risk state is a {type(state).__name__}, not a mapping - blocked"]

    for field in REQUIRED_HEALTH:
        if field not in state:
            reasons.append(f"{field} is missing from the state - unknown, so blocked")
        elif state[field] is not True:
            v = state[field]
            reasons.append(
                f"{field} is {v!r}" if isinstance(v, bool)
                else f"{field} is {v!r} - not a readable yes, so blocked")

    if state.get("circuit_breaker_tripped") is True:
        reasons.append("a circuit breaker is tripped")
    elif "circuit_breaker_tripped" in state and state["circuit_breaker_tripped"] is not False:
        reasons.append("circuit breaker status is unreadable - blocked")

    if state.get("trading_blocked") is True:
        reasons.append("the broker reports trading_blocked")
    if state.get("suspended_by_user") is True:
        reasons.append("trading is suspended by the account owner")

    eq, floor = state.get("equity"), state.get("equity_floor")
    if isinstance(eq, (int, float)) and isinstance(floor, (int, float)) and eq < floor:
        reasons.append(f"equity ${eq:,.2f} is below the ${floor:,.2f} floor")

    return (not reasons), reasons


def edge_interval(mean: Optional[float], sd: Optional[float], n: Optional[int],
                  z: float = 1.96) -> Optional[dict]:
    """95% confidence interval on per-trade P&L, or None if unknowable.

    Returned as its own object because the INTERVAL is the finding, not
    the mean. A mean of -$0.0138 reads as "slightly losing"; the same
    record expressed as -$0.2434 .. +$0.2159 reads correctly as "we
    cannot yet tell", which is the true state and leads to a different
    decision.
    """
    if not isinstance(n, int) or n < 2:
        return None
    if not isinstance(mean, (int, float)) or not isinstance(sd, (int, float)):
        return None
    if sd < 0 or math.isnan(sd) or math.isnan(mean):
        return None
    se = sd / math.sqrt(n)
    lo, hi = mean - z * se, mean + z * se
    return {
        "mean": mean, "sd": sd, "n": n, "stderr": se, "lo": lo, "hi": hi,
        "sign_resolved": not (lo < 0 < hi),
        "positive": lo > 0,
        "negative": hi < 0,
        # What it would take to settle it, at the observed dispersion.
        "trades_needed": (int(math.ceil((z * sd / abs(mean)) ** 2))
                          if mean else None),
    }


def promotion_verdict(record: Optional[dict]) -> dict:
    """Would the next tier be earned? Reports; never applies.

    Returns {"promote": bool, "blocked_by": [...], "interval": {...}}.
    """
    blocked = []
    record = record if isinstance(record, dict) else {}

    n = record.get("trades")
    if not isinstance(n, int):
        blocked.append("trade count is unreadable")
    elif n < PROMOTION["min_live_trades"]:
        blocked.append(f"only {n} live trades, floor is {PROMOTION['min_live_trades']}")

    iv = edge_interval(record.get("mean_pnl"), record.get("sd_pnl"),
                       n if isinstance(n, int) else None)
    if iv is None:
        blocked.append("per-trade edge could not be measured - unknown, so blocked")
    elif not iv["positive"]:
        if iv["negative"]:
            blocked.append(
                f"the 95% interval on per-trade P&L is entirely BELOW zero "
                f"(${iv['lo']:+.4f} .. ${iv['hi']:+.4f}) - this is a measured loser")
        else:
            need = iv["trades_needed"]
            blocked.append(
                f"the 95% interval on per-trade P&L spans zero "
                f"(${iv['lo']:+.4f} .. ${iv['hi']:+.4f}) after {iv['n']} trades, "
                f"so the edge is UNRESOLVED"
                + (f" - about {need:,} trades would settle it at this dispersion"
                   if need else ""))

    dd = record.get("drawdown_pct")
    if not isinstance(dd, (int, float)):
        blocked.append("drawdown is unreadable - blocked")
    elif dd > PROMOTION["max_drawdown_pct"]:
        blocked.append(f"drawdown {dd:.1f}% exceeds {PROMOTION['max_drawdown_pct']}%")

    comp = record.get("rule_compliance_pct")
    if not isinstance(comp, (int, float)):
        blocked.append("rule compliance is unreadable - blocked")
    elif comp < PROMOTION["min_rule_compliance_pct"]:
        blocked.append(f"rule compliance {comp:.1f}% is under "
                       f"{PROMOTION['min_rule_compliance_pct']}%")

    if PROMOTION["require_new_equity_high"] and record.get("at_equity_high") is not True:
        blocked.append("not at a new equity high")

    ok, why = trading_approved(record.get("state"))
    if not ok:
        blocked.extend(why)

    return {"promote": not blocked, "blocked_by": blocked, "interval": iv,
            "min_trades_note": MIN_TRADES_NOTE}


def decide(tier: Optional[str], record: Optional[dict]) -> dict:
    """The whole chain, as one verdict.

    An unrecognised tier falls back to the BASE tier rather than to the
    highest or to whatever was passed in - an unknown tier name must
    never resolve to more risk than the floor.
    """
    record = record if isinstance(record, dict) else {}
    known = tier in RISK_TIERS
    tier = tier if known else BASE_TIER
    base = RISK_TIERS[tier]

    approved, why = trading_approved(record.get("state"))
    mult = derisk_multiplier(record.get("drawdown_pct"))
    prom = promotion_verdict(record)

    effective = 0.0 if not approved else base * mult
    notes = []
    if not known:
        notes.append("tier name was not recognised, so the floor tier was used")
    if mult < 1.0:
        notes.append(f"de-risked to {mult:.0%} of tier size by drawdown")
    if not approved:
        notes.append("trading is NOT approved, so the effective risk is zero")

    return {
        "tier": tier,
        "tier_risk_pct": base,
        "derisk_multiplier": mult,
        "effective_risk_pct": effective,
        "approved": approved,
        "blocked_because": why,
        "promotion": prom,
        # Stated explicitly so no caller has to infer it.
        "applied_to_live_sizing": False,
        "notes": notes,
    }

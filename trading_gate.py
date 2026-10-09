"""The Go / No-Go engine and the Capital Growth Index.

One authoritative decision service, as the spec asks. It DECIDES and it
REPORTS. It does not act: nothing here resizes a position, disables an
entry or pauses a symbol. The status and reasons are the deliverable,
and the lever stays with the owner - see execution_health.py for why
that is a measured decision on this account and not caution.

THREE BUGS IN THE SPEC'S VERSION, FIXED HERE
============================================

1. THE SIGN BUG, and it silently disables the rule.

   The spec's gate reads `if daily_loss_pct > 3`. Its own earlier alert
   spec read `if daily_pnl_pct < -3`. Those are different fields with
   opposite signs.

   Fed this system's actual field - session_pl_pct, which is -0.14 on a
   down day - `-0.14 > 3` is False, and so is -50 > 3. The rule NEVER
   FIRES, on any loss, of any size. Fed an unsigned magnitude instead,
   a +5% GAIN day passed in as pnl gives 5 > 3 = True and the engine
   says NO_GO on the account's best day of the month.

   Pinned to `daily_pnl_pct`, SIGNED, fired on `< -limit`. Both failure
   modes are tested.

2. RISK_OFF OVERWRITES NO_GO - a severity inversion.

   The spec computes status = GO/NO_GO from the reasons, THEN applies
   `if drawdown_pct > 5: status = "RISK_OFF"` as a separate statement.
   A 9% drawdown appends a "Drawdown breach" reason (NO_GO) and is also
   above 5, so the later line overwrites it and the engine reports
   RISK_OFF - the intermediate mode - for a hard breach. Every other
   NO_GO reason present at the same time is overwritten with it too: a
   disconnected broker at a 6% drawdown reports RISK_OFF.

   Fixed by precedence, not by ordering: NO_GO always wins, RISK_OFF
   can only ever soften a GO.

3. PROFIT FACTOR CANNOT HURT THE GROWTH INDEX.

   `growth_index += max(0, (profit_factor - 1) * 20)` means a PF of
   0.519 - every trade losing money on average - contributes exactly
   the same ZERO as a PF of 1.0 that breaks even. A book bleeding on
   every round trip cannot be distinguished from a flat one.

   With this account's live figures the spec's index returns 56.2,
   which its own scale calls "Caution". The same gate used in
   execution_health.capital_growth_score is applied: below 1.0 the
   grade is PRESERVATION whatever the arithmetic says, and the reason
   says so in words.

NOTE ON DOUBLE COUNTING, left as specified but surfaced: blocked_exits
and trapped_capital_count describe the SAME condition - DOG and RWM are
each both - so each such position costs 10 + 8 = 18 index points. That
may be the intent (an execution failure is worth weighting twice) but it
should be a choice, so the breakdown is returned itemised.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

EXECUTES_NOTHING = True

UTC = timezone.utc

GO = "GO"
RISK_OFF = "RISK_OFF"
NO_GO = "NO_GO"

# Grade bands for the Capital Growth Index.
GRADES = ((95, "ELITE"), (80, "STRONG"), (65, "STABLE"),
          (50, "CAUTION"), (0, "PRESERVATION"))


@dataclass
class TradingDecision:
    status: str
    score: Optional[float]
    reasons: List[str] = field(default_factory=list)
    timestamp: str = ""
    # Reasons that could not be evaluated because an input was missing.
    # Separate from `reasons`: an unchecked condition is not a passed
    # one, and a GO issued over unreadable inputs is the lie this field
    # exists to prevent.
    unknown: List[str] = field(default_factory=list)

    @property
    def may_trade(self):
        return self.status != NO_GO

    def as_dict(self):
        return {"status": self.status, "score": self.score,
                "reasons": list(self.reasons), "unknown": list(self.unknown),
                "timestamp": self.timestamp, "may_trade": self.may_trade}


def capital_growth_index(drawdown_pct=None, blocked_exits=None,
                         trapped_capital_count=None, broker_rejects=None,
                         profit_factor=None, peak_is_trusted=True):
    """0-100, with an itemised breakdown and a profit-factor gate."""
    missing = [n for n, v in (("drawdown_pct", drawdown_pct),
                              ("blocked_exits", blocked_exits),
                              ("trapped_capital_count", trapped_capital_count),
                              ("broker_rejects", broker_rejects),
                              ("profit_factor", profit_factor)) if v is None]
    if missing:
        return {"index": None, "grade": None, "unknown": True,
                "gated_by": None, "breakdown": {},
                "why": f"cannot index: {', '.join(missing)} unreadable"}

    dd = 0.0 if not peak_is_trusted else float(drawdown_pct)
    pf = float(profit_factor)
    items = {
        "base": 100.0,
        "drawdown": -float(drawdown_pct) * 3 if peak_is_trusted else 0.0,
        "blocked_exits": -float(blocked_exits) * 10,
        "trapped_capital": -float(trapped_capital_count) * 8,
        "broker_rejects": -float(broker_rejects) * 2,
        "profit_factor": max(0.0, (pf - 1) * 20),
    }
    raw = sum(items.values())
    idx = round(max(0.0, min(100.0, raw)), 2)

    gated = None
    why = f"profit factor {pf:.3f}; index {idx}"
    if pf < 1.0:
        gated = "profit_factor"
        grade = "PRESERVATION"
        why = (f"profit factor {pf:.3f} is below 1.00, so the average "
               f"trade loses money. The spec's index adds "
               f"max(0, (pf-1)*20), which is {items['profit_factor']:+.2f} "
               f"here - identical to what a break-even book scores - so "
               f"the arithmetic alone reads {idx} "
               f"({_grade(idx)}). Graded PRESERVATION until the wins "
               f"cover the losses.")
    else:
        grade = _grade(idx)

    if not peak_is_trusted:
        why += (" Drawdown scored as 0: the stored peak behind it is "
                "known stale and would subtract points for a loss that "
                "did not happen.")

    return {"index": idx, "grade": grade, "unknown": False,
            "gated_by": gated, "breakdown": {k: round(v, 2)
                                             for k, v in items.items()},
            "raw_before_clamp": round(raw, 2),
            "double_counted": (
                "blocked_exits and trapped_capital describe the same "
                "condition; each such position costs 10 + 8 = 18 points"
                if blocked_exits and trapped_capital_count else None),
            "why": why}


def _grade(idx):
    for floor, name in GRADES:
        if idx >= floor:
            return name
    return "PRESERVATION"


class TradingGate:
    """The one place that answers 'may trading proceed'."""

    def __init__(self, drawdown_soft=5.0, drawdown_hard=8.0,
                 daily_loss_limit_pct=3.0, profit_factor_floor=1.2):
        self.drawdown_soft = drawdown_soft
        self.drawdown_hard = drawdown_hard
        # Stored as a POSITIVE magnitude; compared against a SIGNED
        # daily_pnl_pct as `< -limit`. See bug 1.
        self.daily_loss_limit_pct = abs(daily_loss_limit_pct)
        self.profit_factor_floor = profit_factor_floor

    def evaluate(self, drawdown_pct=None, daily_pnl_pct=None,
                 blocked_exits=None, trapped_capital_count=None,
                 profit_factor=None, exposure_pct=None,
                 exposure_limit=None, broker_connected=None,
                 database_connected=None, alerts_operational=None,
                 peak_is_trusted=True, now=None):
        """A NO_GO reason is a hard stop. RISK_OFF only softens a GO."""
        reasons = []
        unknown = []

        def need(name, value):
            if value is None:
                unknown.append(f"{name} unreadable - not checked")
                return False
            return True

        # Infrastructure first: a disconnected broker makes every other
        # reading below it stale, so it is named before anything else.
        if need("broker_connected", broker_connected) and not broker_connected:
            reasons.append("Broker disconnected")
        if need("database_connected", database_connected) and not database_connected:
            reasons.append("Database unavailable")
        if need("alerts_operational", alerts_operational) and not alerts_operational:
            reasons.append("Alerting unavailable - a failure would go unseen")

        if need("blocked_exits", blocked_exits) and int(blocked_exits) > 0:
            reasons.append(f"Blocked exits detected ({int(blocked_exits)})")
        if (need("trapped_capital_count", trapped_capital_count)
                and int(trapped_capital_count) > 0):
            reasons.append(f"Capital trapped ({int(trapped_capital_count)} "
                           f"position(s))")

        # Drawdown. An untrusted peak is UNKNOWN, never a breach: on this
        # account BTC reads 25.1% on 81 cents of real loss, and a gate
        # that halts on that halts a healthy book.
        if drawdown_pct is None:
            unknown.append("drawdown_pct unreadable - not checked")
        elif not peak_is_trusted:
            unknown.append(
                f"drawdown_pct reads {float(drawdown_pct):.2f}% but its "
                f"stored peak is known stale - not checked")
        elif float(drawdown_pct) > self.drawdown_hard:
            reasons.append(f"Drawdown breach "
                           f"({float(drawdown_pct):.2f}% > "
                           f"{self.drawdown_hard}%)")

        # THE SIGN. Signed field, negative is a loss, fired on < -limit.
        if need("daily_pnl_pct", daily_pnl_pct):
            if float(daily_pnl_pct) < -self.daily_loss_limit_pct:
                reasons.append(f"Daily loss limit hit "
                               f"({float(daily_pnl_pct):+.2f}% < "
                               f"-{self.daily_loss_limit_pct}%)")

        if need("profit_factor", profit_factor):
            pf = float(profit_factor)
            if pf < self.profit_factor_floor:
                reasons.append(f"Profit factor too low "
                               f"({pf:.3f} < {self.profit_factor_floor})")

        if exposure_pct is None or exposure_limit is None:
            unknown.append("exposure unreadable - not checked")
        elif float(exposure_pct) > float(exposure_limit):
            reasons.append(f"Exposure exceeded ({float(exposure_pct):.2%} > "
                           f"{float(exposure_limit):.2%})")

        # PRECEDENCE, not ordering. A hard reason can never be softened
        # into RISK_OFF by a later statement.
        if reasons:
            status = NO_GO
        elif (peak_is_trusted and drawdown_pct is not None
              and float(drawdown_pct) > self.drawdown_soft):
            status = RISK_OFF
            reasons.append(f"Drawdown above {self.drawdown_soft}% "
                           f"({float(drawdown_pct):.2f}%) - half size, "
                           f"half positions. RECOMMENDED, not applied.")
        else:
            status = GO

        idx = capital_growth_index(
            drawdown_pct=drawdown_pct, blocked_exits=blocked_exits,
            trapped_capital_count=trapped_capital_count,
            broker_rejects=0 if broker_connected else None,
            profit_factor=profit_factor, peak_is_trusted=peak_is_trusted)

        return TradingDecision(
            status=status, score=idx["index"], reasons=reasons,
            unknown=unknown,
            timestamp=(now or datetime.now(UTC)).isoformat())

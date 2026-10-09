"""The dashboard data contracts. Types first, so everything downstream
is predictable.

Read-only by construction: these are shapes and the arithmetic that
derives one field from another. Nothing here reaches a venue, a database
or a network. test_dashboard_contracts.py pins that by AST.

FOUR THINGS THE SPEC LEFT AMBIGUOUS, PINNED HERE
================================================

1. SIGN OF THE DAILY P&L FIELD. The alert spec said
   `daily_pnl_pct < -3`; the gate spec said `daily_loss_pct > 3`. Those
   are different fields with opposite signs and only one can be right.
   Pinned: this system carries `daily_pnl_pct`, SIGNED, negative for a
   loss, because that is what /alpaca-overview actually returns
   (session_pl_pct = -0.14 on a down day). A rule reading it as an
   unsigned magnitude either never fires or fires on a GAIN - see
   trading_gate.py, where both failure modes are tested.

2. BUYING POWER IS NOT 2x CASH. The spec's example shows buying_power
   10047.54 against cash 5023.77 - margin. This account's live figures
   are buying_power == cash == $601.74, no margin, and the standing rule
   is NO LEVERAGE. utilization_pct is therefore computed against EQUITY,
   never buying_power, which would understate it by half on a margin
   account and silently change meaning between accounts.

3. MARKET VALUE vs LOCKED VALUE. The spec's TrappedCapital example gives
   RWM market_value 294.33 with locked_qty 9.06. Those do not come from
   the same price: 18.122486 shares at the live $14.39 is $260.78, and
   the locked half is $130.39. Both are kept as separate named fields so
   neither can stand in for the other.

4. CAUSE IS NOT ASSUMED. The spec hardcodes cause "OPEN_SELL_ORDER".
   That is the most likely cause and it is not a measured one unless the
   open orders were actually read. LockCause.UNKNOWN is the default, and
   OPEN_SELL_ORDER requires evidence to be passed in.
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

EXECUTES_NOTHING = True

UTC = timezone.utc


# ── LIVE EXIT RULES, BY STRATEGY FAMILY ───────────────────────────────
#
# A Position's stop_price and target_price are DERIVED, not stored, and
# which rule set derives them depends on strategy_family - read fresh
# every pass, because a flip is retroactive and immediately re-governs
# positions already open. Figures traced to prop_bot.describe_live_rules.
FAMILY_RULES = {
    "mean_reversion": {
        "stop_pct": 0.015,          # FIXED off entry, not trailing
        "target_pct": 0.03,
        "max_hold_seconds": 7200,   # TWO hours, not twenty-four
        "stop_reference": "entry",
    },
    "momentum": {
        "stop_pct": 0.03,           # TRAILING off the peak since entry
        "target_pct": None,
        "max_hold_seconds": 86400,
        "stop_reference": "peak",
    },
}


class LockCause(str, Enum):
    """Why shares are unavailable. UNKNOWN is the default and the honest
    answer until the open orders have actually been read."""

    UNKNOWN = "UNKNOWN"
    OPEN_SELL_ORDER = "OPEN_SELL_ORDER"
    OPEN_ORDER = "OPEN_ORDER"
    SETTLEMENT = "SETTLEMENT"


class Severity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


# ── PORTFOLIO ─────────────────────────────────────────────────────────

@dataclass
class PortfolioSummary:
    timestamp: datetime
    equity: float
    cash: float
    buying_power: float
    daily_pl: float
    # Signed. Negative is a loss. See note 1 in the module docstring.
    daily_pnl_pct: Optional[float] = None
    weekly_pl: Optional[float] = None
    monthly_pl: Optional[float] = None
    equity_change_pct: Optional[float] = None

    @property
    def invested(self):
        return round(self.equity - self.cash, 2)

    @property
    def utilization_pct(self):
        """Invested as a share of EQUITY, never of buying power."""
        if not self.equity:
            return None
        return round(100.0 * self.invested / self.equity, 2)

    @property
    def is_margin_account(self):
        """True when the venue lends. Worth surfacing: the standing rule
        is NO LEVERAGE, so a buying power above cash is a fact the
        dashboard should show rather than quietly spend."""
        if self.buying_power is None or self.cash is None:
            return None
        return self.buying_power > self.cash + 0.01

    def as_dict(self):
        d = asdict(self)
        d["timestamp"] = (self.timestamp.isoformat()
                          if isinstance(self.timestamp, datetime)
                          else self.timestamp)
        d["invested"] = self.invested
        d["utilization_pct"] = self.utilization_pct
        d["is_margin_account"] = self.is_margin_account
        return d


# ── RISK ──────────────────────────────────────────────────────────────

def historical_var_95(returns_usd):
    """Historical 95% VaR in dollars, as a POSITIVE magnitude of loss.

    The spec asks for var_95. There is no way to compute one from a
    single equity reading - it needs a distribution - so this takes the
    series and returns None without it. A VaR invented from one number
    is a number with a Greek letter next to it, not a risk measure.

    Needs at least 20 observations: the 5th percentile of fewer than
    that is one or two data points wearing a percentile's name.
    """
    if not returns_usd:
        return None
    xs = sorted(float(r) for r in returns_usd
                if r is not None and r == r)      # drops NaN
    if len(xs) < 20:
        return None
    # VaR_95 is the threshold that 5% of observations fall AT OR BELOW,
    # so the rank counts tail observations: ceil(0.05 * n) of them, and
    # the threshold is the last one. For n=20 that is the single worst
    # reading (1/20 = 5%), not the second worst.
    #
    # The first version here used round(0.05 * (n - 1)), which is the
    # interpolation rank used for a distribution percentile. On 20
    # observations it picks index 1 and so reports 0.00 for a series
    # whose worst day lost 100 dollars - a VaR that cannot see the only
    # loss in the sample. Caught by the test, not by reading it back.
    import math
    idx = max(0, math.ceil(0.05 * len(xs)) - 1)
    return round(abs(min(0.0, xs[idx])), 2)


@dataclass
class RiskMetrics:
    drawdown_pct: Optional[float]
    exposure_pct: Optional[float]
    exposure_limit_pct: float = 0.60
    max_drawdown_pct: Optional[float] = None
    var_95: Optional[float] = None
    # Signed, negative for a loss. Same pinning as PortfolioSummary.
    daily_pnl_pct: Optional[float] = None
    # The Capital Growth Index, 0-100. ONE definition of "how healthy",
    # shared with trading_gate.capital_growth_index, so the dashboard
    # cannot show two different health numbers.
    risk_score: Optional[float] = None
    # False when the stored peak_equity behind drawdown_pct is known
    # stale. Carried in the contract because a consumer that cannot see
    # this will act on a drawdown that is not real.
    peak_is_trusted: bool = True

    @property
    def exposure_breached(self):
        if self.exposure_pct is None:
            return None
        return self.exposure_pct > self.exposure_limit_pct

    def as_dict(self):
        d = asdict(self)
        d["exposure_breached"] = self.exposure_breached
        if not self.peak_is_trusted:
            d["drawdown_caveat"] = (
                "the stored peak_equity behind this reading is known "
                "stale; verify against allocated_usd + unrealized before "
                "acting on it")
        return d


# ── POSITIONS ─────────────────────────────────────────────────────────

@dataclass
class Position:
    symbol: str
    qty: float
    entry_price: float
    current_price: float
    available_qty: Optional[float] = None
    opened_at: Optional[datetime] = None
    strategy_family: str = "mean_reversion"
    # Only meaningful for a trailing family; None means "not tracked".
    peak_price_since_entry: Optional[float] = None
    as_of: Optional[datetime] = None

    @property
    def market_value(self):
        return round(self.qty * self.current_price, 2)

    @property
    def unrealized_pl(self):
        return round((self.current_price - self.entry_price) * self.qty, 2)

    @property
    def unrealized_pl_pct(self):
        if not self.entry_price:
            return None
        return round(100.0 * (self.current_price - self.entry_price)
                     / self.entry_price, 4)

    @property
    def rules(self):
        return FAMILY_RULES.get(self.strategy_family)

    @property
    def age_minutes(self):
        if not self.opened_at:
            return None
        now = self.as_of or datetime.now(UTC)
        return int((now - self.opened_at).total_seconds() / 60)

    @property
    def stop_price(self):
        """DERIVED from the live family. None when it cannot be derived.

        For a trailing family the reference is the PEAK since entry, not
        the entry - using entry there would read a position as safe when
        it has given back 3% from a high.
        """
        r = self.rules
        if not r:
            return None
        if r["stop_reference"] == "entry":
            return round(self.entry_price * (1 - r["stop_pct"]), 6)
        if self.peak_price_since_entry is None:
            return None            # trailing stop with no peak tracked
        return round(self.peak_price_since_entry * (1 - r["stop_pct"]), 6)

    @property
    def target_price(self):
        r = self.rules
        if not r or r["target_pct"] is None:
            return None
        return round(self.entry_price * (1 + r["target_pct"]), 6)

    @property
    def past_stop(self):
        sp = self.stop_price
        if sp is None:
            return None
        return self.current_price < sp

    @property
    def past_max_hold(self):
        r = self.rules
        if not r or self.age_minutes is None:
            return None
        return self.age_minutes * 60 > r["max_hold_seconds"]

    @property
    def blocked_exit(self):
        """The venue will not release the WHOLE position.

        Strictly less-than, matching the gate in prop_bot.py. None when
        available_qty was not read - and None is not False: an unread
        availability is exactly the condition that produced this bug
        three times.
        """
        if self.available_qty is None:
            return None
        return self.available_qty < self.qty

    def as_dict(self):
        return {
            "symbol": self.symbol,
            "qty": self.qty,
            "market_value": self.market_value,
            "entry_price": self.entry_price,
            "current_price": self.current_price,
            "unrealized_pl": self.unrealized_pl,
            "unrealized_pl_pct": self.unrealized_pl_pct,
            "age_minutes": self.age_minutes,
            "stop_price": self.stop_price,
            "target_price": self.target_price,
            "available_qty": self.available_qty,
            "blocked_exit": self.blocked_exit,
            "past_stop": self.past_stop,
            "past_max_hold": self.past_max_hold,
            "strategy_family": self.strategy_family,
            "max_hold_seconds": (self.rules or {}).get("max_hold_seconds"),
        }


# ── TRAPPED CAPITAL ───────────────────────────────────────────────────

@dataclass
class TrappedCapital:
    """Its own row, as the spec asks. Derived from a Position plus the
    availability read, so the two can never disagree."""

    symbol: str
    position_qty: float
    available_qty: float
    price: float
    lock_age_minutes: Optional[int] = None
    cause: LockCause = LockCause.UNKNOWN

    @property
    def locked_qty(self):
        return round(max(0.0, self.position_qty - self.available_qty), 9)

    @property
    def market_value(self):
        """The WHOLE position's value."""
        return round(self.position_qty * self.price, 2)

    @property
    def locked_value(self):
        """Only the shares the venue is holding. Not the same number as
        market_value, and the spec's example conflated them."""
        return round(self.locked_qty * self.price, 2)

    @property
    def severity(self):
        """CRITICAL at zero available; WARNING when any is held back;
        INFO when more than 80% is still free."""
        if self.available_qty <= 0:
            return Severity.CRITICAL
        if self.available_qty >= self.position_qty:
            return None            # nothing locked, not a row at all
        if self.position_qty and self.available_qty / self.position_qty > 0.80:
            return Severity.INFO
        return Severity.WARNING

    def as_alert(self):
        sev = self.severity
        return {
            "id": f"TRAP_{self.symbol}",
            "severity": None if sev is None else sev.value,
            "symbol": self.symbol,
            "position_qty": self.position_qty,
            "available_qty": self.available_qty,
            "locked_qty": self.locked_qty,
            "market_value": self.market_value,
            "locked_value": self.locked_value,
            "age_minutes": self.lock_age_minutes,
            "cause": self.cause.value,
        }

    def as_dict(self):
        d = self.as_alert()
        d.pop("id")
        return d


def trapped_rows(positions):
    """Every blocked Position as a TrappedCapital row.

    A position whose availability was never read is NOT silently
    dropped - it comes back with cause UNKNOWN and available_qty None
    handled as a refusal upstream, because "I did not look" must not
    render as "nothing is trapped".
    """
    rows = []
    for p in positions:
        if p.blocked_exit:
            rows.append(TrappedCapital(
                symbol=p.symbol, position_qty=p.qty,
                available_qty=p.available_qty, price=p.current_price,
                lock_age_minutes=p.age_minutes))
    return rows


# ── EXECUTION HEALTH ──────────────────────────────────────────────────

@dataclass
class ExecutionHealth:
    blocked_exits: int = 0
    stale_orders: int = 0
    broker_rejects: int = 0
    api_errors: int = 0
    position_mismatches: int = 0
    heartbeat_ok: bool = True
    # Separate from heartbeat: the loop can be alive while nothing can
    # tell anyone about it. Live on this system - SendGrid is out of
    # credits, SMTP 465/587 are filtered, and 89 alert rows are held
    # with no route out. An alerting channel that cannot deliver is a
    # monitoring outage, and the Go/No-Go engine treats it as one.
    alerts_operational: bool = True

    @property
    def clean(self):
        return (self.blocked_exits == 0 and self.stale_orders == 0
                and self.broker_rejects == 0 and self.api_errors == 0
                and self.position_mismatches == 0
                and self.heartbeat_ok and self.alerts_operational)

    def as_dict(self):
        d = asdict(self)
        d["clean"] = self.clean
        return d


# ── THE SINGLE RESPONSE ───────────────────────────────────────────────

@dataclass
class CapitalCommandCenter:
    """The shape of /api/dashboard/capital-command-center."""

    portfolio: PortfolioSummary
    risk: RiskMetrics
    execution: ExecutionHealth
    positions: List[Position] = field(default_factory=list)
    trapped_capital: List[TrappedCapital] = field(default_factory=list)
    alerts: List[dict] = field(default_factory=list)
    growth: dict = field(default_factory=dict)
    decision: dict = field(default_factory=dict)

    def as_dict(self):
        return {
            "portfolio": self.portfolio.as_dict(),
            "risk": self.risk.as_dict(),
            "execution": self.execution.as_dict(),
            "positions": [p.as_dict() for p in self.positions],
            "trapped_capital": [t.as_dict() for t in self.trapped_capital],
            "alerts": list(self.alerts),
            "growth": dict(self.growth),
            "decision": dict(self.decision),
        }


def trapped_capital_card(rows):
    """The dashboard card, summed from rows rather than seeded.

    Reports BOTH dollar figures for the reason given in
    execution_health.trapped_capital: locked_value is what the venue
    holds, cannot_follow is every blocked position's whole value,
    because the sell gate is all-or-nothing and a half-free position
    cannot exit at all.
    """
    crit = [r for r in rows if r.severity is Severity.CRITICAL]
    warn = [r for r in rows if r.severity is Severity.WARNING]
    info = [r for r in rows if r.severity is Severity.INFO]
    ages = [r.lock_age_minutes for r in rows if r.lock_age_minutes is not None]
    return {
        "positions_locked": len(rows),
        "locked_value_usd": round(sum(r.locked_value for r in rows), 2),
        "cannot_follow_strategy_usd": round(
            sum(r.market_value for r in rows), 2),
        "oldest_lock_minutes": max(ages) if ages else None,
        "critical": [r.symbol for r in crit],
        "warning": [r.symbol for r in warn],
        "info": [r.symbol for r in info],
        "status": ("ATTENTION REQUIRED" if (crit or warn)
                   else ("PARTIALLY RESERVED" if info else "CLEAR")),
    }

"""Position sizing: fixed-risk, ATR, and the Kelly overlay.

Pure arithmetic. Reads nothing, writes nothing, places nothing. It
answers "how big SHOULD this be" and the answer is a number, not an
order.

THREE BUGS IN THE SPEC'S VERSION, FIXED HERE
============================================

1. shares() divides by stop_distance, which is ZERO when entry == stop.
   The spec's version raises ZeroDivisionError. A sizer that crashes
   inside an entry pass takes the pass down with it, so it returns a
   refusal instead: a stop at the entry price is not a stop, it is a
   missing input.

2. kelly_fraction() divides by reward_risk, which is ZERO for a strategy
   whose average win is zero. Same treatment.

3. shares() has NO CAP. risk_amount / stop_distance grows without bound
   as the stop tightens: a $10,000 account risking 1% with a 1-cent stop
   sizes 10,000 shares - at $50 that is a $500,000 position on a $10,000
   account, 50x leverage, from arithmetic that looks completely
   reasonable. position_value() would dutifully report it. Capped at the
   equity actually available, and the cap is REPORTED rather than
   silently applied, because a size that had to be capped means the stop
   is too tight for the risk budget and that is worth knowing.

NO LEVERAGE. The cap above is the whole reason point 3 exists.
"""

from dataclasses import dataclass

EXECUTES_NOTHING = True


@dataclass
class PositionSize:
    """Fixed-fractional risk sizing.

    equity      - account equity in USD
    risk_pct    - fraction, NOT percent: 0.01 means 1%
    entry_price - intended entry
    stop_price  - where the trade is wrong
    max_pct_of_equity - the leverage cap. 1.0 = never more than the
                  account. Lower it for a concentration ceiling (this
                  fleet's is 0.20).
    """

    equity: float
    risk_pct: float
    entry_price: float
    stop_price: float
    max_pct_of_equity: float = 1.0

    def risk_amount(self):
        return self.equity * self.risk_pct

    def stop_distance(self):
        return abs(self.entry_price - self.stop_price)

    def _raw_shares(self):
        d = self.stop_distance()
        if d <= 0:
            return None
        return self.risk_amount() / d

    def shares(self):
        """Share count, capped. None when it cannot be computed."""
        s = self._raw_shares()
        if s is None or self.entry_price <= 0:
            return None
        ceiling_usd = self.equity * self.max_pct_of_equity
        return min(s, ceiling_usd / self.entry_price)

    def position_value(self):
        s = self.shares()
        return None if s is None else s * self.entry_price

    def report(self):
        """Everything a caller needs, including whether it hit the cap."""
        raw = self._raw_shares()
        if raw is None:
            return {
                "shares": None, "position_value": None, "capped": False,
                "refused": True,
                "why": (f"stop {self.stop_price} equals entry "
                        f"{self.entry_price}, so the stop distance is zero. "
                        f"A stop at the entry price is not a stop, it is a "
                        f"missing input - no size can be computed from it."),
            }
        if self.entry_price <= 0:
            return {"shares": None, "position_value": None, "capped": False,
                    "refused": True,
                    "why": f"entry price {self.entry_price} is not tradeable"}
        s = self.shares()
        capped = s < raw - 1e-12
        out = {
            "shares": round(s, 6),
            "position_value": round(s * self.entry_price, 2),
            "risk_amount": round(self.risk_amount(), 2),
            "stop_distance": round(self.stop_distance(), 6),
            "capped": capped,
            "refused": False,
        }
        if capped:
            out["why"] = (
                f"fixed-risk sizing wanted {raw:.6f} shares "
                f"(${raw * self.entry_price:,.2f}, "
                f"{100 * raw * self.entry_price / self.equity:.1f}% of a "
                f"${self.equity:,.2f} account) because the stop is only "
                f"${self.stop_distance():.4f} away. Capped at "
                f"{100 * self.max_pct_of_equity:.0f}% of equity. A size "
                f"that needs capping means the stop is too tight for the "
                f"risk budget - widen the stop or cut risk_pct, do not "
                f"raise the cap.")
        else:
            out["why"] = (f"${self.risk_amount():,.2f} at risk over a "
                          f"${self.stop_distance():.4f} stop")
        return out


def atr_size(equity, risk_pct, atr, atr_multiple=2, entry_price=None,
             max_pct_of_equity=1.0):
    """Volatility-based sizing. Units, or None when uncomputable.

    entry_price is optional and only needed to apply the cap - without
    it there is no way to turn units into dollars, so no cap can be
    enforced and the raw figure comes back.
    """
    try:
        a = float(atr)
        m = float(atr_multiple)
    except (TypeError, ValueError):
        return None
    stop_distance = a * m
    if stop_distance <= 0:
        return None
    units = (float(equity) * float(risk_pct)) / stop_distance
    if entry_price and float(entry_price) > 0:
        ceiling = float(equity) * float(max_pct_of_equity)
        units = min(units, ceiling / float(entry_price))
    return units


def reward_risk_from_profit_factor(profit_factor, win_rate):
    """The reward:risk ratio implied by a book's PF and win rate.

    Needed because this system measures profit factor, not R:R, and
    Kelly wants R:R. The identity:

        PF = (wins_total) / (losses_total)
           = (WR * avg_win) / ((1 - WR) * avg_loss)
           = R:R * WR / (1 - WR)

    so  R:R = PF * (1 - WR) / WR.

    win_rate is a FRACTION here (0.865), not a percentage.
    """
    try:
        pf = float(profit_factor)
        wr = float(win_rate)
    except (TypeError, ValueError):
        return None
    if not (0 < wr < 1) or pf <= 0:
        return None
    return pf * (1 - wr) / wr


def kelly_fraction(win_rate, reward_risk):
    """Kelly, floored at zero. None when it cannot be computed.

    win_rate is a FRACTION (0.58), not a percentage. reward_risk of zero
    or less returns None rather than raising: a strategy with no average
    win has no Kelly size, which is a different statement from "bet
    nothing" and should not be silently rounded into it.
    """
    try:
        wr = float(win_rate)
        rr = float(reward_risk)
    except (TypeError, ValueError):
        return None
    if rr <= 0:
        return None
    if not (0 <= wr <= 1):
        return None
    k = wr - ((1 - wr) / rr)
    return max(k, 0.0)


# Kelly is the theoretical growth-maximising fraction and it is far too
# large to bet in practice - it assumes the edge is known exactly, which
# it never is. A quarter is the conventional deployment.
KELLY_SAFETY = 0.25


def safe_kelly(win_rate, reward_risk, safety=KELLY_SAFETY):
    k = kelly_fraction(win_rate, reward_risk)
    return None if k is None else k * safety


def kelly_from_book(profit_factor, win_rate_pct, safety=KELLY_SAFETY):
    """Kelly straight from the figures this system actually reports.

    Takes win rate as a PERCENTAGE, because that is how every endpoint
    in this repo reports it, and converts internally. Returns the whole
    derivation so the answer can be checked rather than trusted.
    """
    try:
        wr = float(win_rate_pct) / 100.0
        pf = float(profit_factor)
    except (TypeError, ValueError):
        return {"kelly": None, "deploy": None,
                "why": "profit factor or win rate unreadable"}
    rr = reward_risk_from_profit_factor(pf, wr)
    if rr is None:
        return {"kelly": None, "deploy": None, "reward_risk": None,
                "why": (f"cannot derive a reward:risk ratio from profit "
                        f"factor {pf} and win rate {win_rate_pct}%")}
    k = kelly_fraction(wr, rr)
    if k is None:
        return {"kelly": None, "deploy": None, "reward_risk": round(rr, 6),
                "why": "reward:risk is zero or negative - no Kelly size"}
    raw = wr - ((1 - wr) / rr)
    return {
        "kelly": round(k, 6),
        "deploy": round(k * safety, 6),
        "reward_risk": round(rr, 6),
        "raw_kelly_before_floor": round(raw, 6),
        "why": (
            f"win rate {100 * wr:.1f}% with profit factor {pf:.3f} implies "
            f"reward:risk of {rr:.4f} - the average win is {rr:.4f} times "
            f"the average loss. Kelly = {wr:.4f} - ({1 - wr:.4f} / "
            f"{rr:.4f}) = {raw:+.4f}"
            + (f", floored to 0. A NEGATIVE raw Kelly means the edge runs "
               f"the wrong way: at this reward:risk, a {100 * wr:.1f}% win "
               f"rate is not enough to pay for the {100 * (1 - wr):.1f}% of "
               f"losses. The growth-maximising bet is nothing."
               if raw < 0 else f". Deploy {100 * k * safety:.2f}% at "
                               f"{safety:g}x safety.")),
    }

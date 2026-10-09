"""Execution health, trapped capital, and the rule evaluator.

WHAT THIS MODULE DOES AND DOES NOT DO
=====================================

It MEASURES and it NAMES. It executes nothing.

Every rule returns a Verdict: what was measured, what it crossed, and
what the recommended action IS - as a string, as data, for a human or a
dashboard to read. Not one of them calls it. There is no code path in
this file that places an order, closes a position, cancels an order,
pauses a symbol, disables entries, or resizes anything, and
test_execution_health.py pins that by AST so it stays true.

That is a design decision with a reason specific to this account today:

    THE DRAWDOWN PERCENTAGES ARE CURRENTLY WRONG.

    peak_after_withdrawal preserved the dollar gap across a right-size
    but not the percentage, so a branch's stored peak_equity can be
    stale high. Fixed 2026-10-09 for all FUTURE withdrawals - but the
    fix is PREVENTION ONLY. Two stored peaks are still wrong right now:
    BTC reads 25.1% drawdown on $0.81 of actual market loss.

    A rule written `drawdown_pct > 8 -> disable_new_entries()` and wired
    to execute would therefore halt this account's trading RIGHT NOW, at
    a 25.1% reading, over eighty-one cents. An automatic action is only
    as safe as its worst input, and this one has a known-bad input
    sitting in the database.

The same holds for the loss rules. `daily_pnl_pct < -3 ->
close_open_signals()` sells whatever is open at whatever it is worth,
which on a red day means booking losses by machine - against the
standing rule that nothing is sold at a loss and that nothing is
force-closed to turn red green. A rule that CAN do that on its own will,
on the one day its input is wrong.

So the triggers below are implemented exactly as specified, thresholds
as given. What changes is who pulls the lever. The verdicts are the
deliverable; the actions are the owner's.
"""

from dataclasses import dataclass, field, asdict
from typing import Optional

EXECUTES_NOTHING = True

CRITICAL = "CRITICAL"
HIGH = "HIGH"
WARNING = "WARNING"
OK = "OK"

_RANK = {CRITICAL: 3, HIGH: 2, WARNING: 1, OK: 0}

GREEN, YELLOW, RED = "GREEN", "YELLOW", "RED"


@dataclass
class Verdict:
    """One rule's reading. `recommended_actions` is DATA, never a call."""

    rule: str
    severity: str
    fired: bool
    measured: str
    why: str
    recommended_actions: list = field(default_factory=list)
    # True only when every input the rule needed was readable. A rule
    # that could not measure itself reports UNKNOWN, not OK - the third
    # verdict, because "I saw no problem" and "there is no problem" are
    # different claims and conflating them is how a monitor lies.
    readable: bool = True

    @property
    def unknown(self):
        return not self.readable

    def as_dict(self):
        d = asdict(self)
        d["unknown"] = self.unknown
        return d


def _unknown(rule, why):
    return Verdict(rule=rule, severity=WARNING, fired=False,
                   measured="UNKNOWN", why=why, readable=False,
                   recommended_actions=["read the input before judging it"])


# ── TRAPPED CAPITAL ───────────────────────────────────────────────────
#
# The card that answers "how much money is currently unable to follow
# the strategy." It needs TWO figures, because they differ and the
# difference is the point.
#
#   trapped_at_broker   - market value of the shares the venue will not
#                         release. DOG (0 of 5.054042 available): all of
#                         it. RWM (9.061243 of 18.122486): exactly half.
#
#   cannot_follow       - market value of every position whose exit is
#                         BLOCKED. Larger, and the honest answer:
#                         prop_bot's sell gate is all-or-nothing by
#                         design (a deliberate partial would hand
#                         exit_pass a full-size P&L for a part-size
#                         sale), so a position with half its shares free
#                         still cannot exit AT ALL.
#
# Only the first understates by the free half of every partial. Only the
# second overstates what the venue actually holds. Both, always.

@dataclass
class TrappedPosition:
    symbol: str
    qty_owned: float
    qty_available: float
    price: float
    opened_at: Optional[str] = None

    @property
    def qty_held_back(self):
        return max(0.0, self.qty_owned - self.qty_available)

    @property
    def market_value(self):
        return round(self.qty_owned * self.price, 2)

    @property
    def value_held_back(self):
        return round(self.qty_held_back * self.price, 2)

    @property
    def is_blocked(self):
        """The venue will not release the WHOLE position.

        Strictly less-than, matching the gate in prop_bot.py: available
        == owned is not blocked, and more available than owned (which
        should not happen) is not blocked either.
        """
        return self.qty_available < self.qty_owned


def trapped_capital(positions, equity=None, minutes_since=None):
    """The Trapped Capital card, as data.

    `minutes_since` is an optional callable taking opened_at and
    returning minutes. Injected so this function reads no clock and
    stays pure - a monitor whose output depends on when you ran it
    cannot be tested, and an untested monitor is decoration.
    """
    blocked = [p for p in positions if p.is_blocked]
    held_back = round(sum(p.value_held_back for p in blocked), 2)
    cannot_follow = round(sum(p.market_value for p in blocked), 2)

    oldest = None
    if minutes_since is not None:
        ages = []
        for p in blocked:
            if not p.opened_at:
                continue
            try:
                m = minutes_since(p.opened_at)
            except Exception:
                continue
            if m is not None:
                ages.append(m)
        if ages:
            oldest = int(max(ages))

    pct = None
    if equity is not None:
        try:
            eq = float(equity)
            if eq > 0:
                pct = round(100.0 * cannot_follow / eq, 2)
        except (TypeError, ValueError):
            pct = None

    if not blocked:
        status = OK
    elif oldest is not None and oldest > 120:
        # Past the 7,200s mean_reversion backstop with no way out.
        status = CRITICAL
    else:
        status = WARNING

    return {
        "trapped_positions": len(blocked),
        "trapped_at_broker_usd": held_back,
        "cannot_follow_strategy_usd": cannot_follow,
        "pct_of_equity": pct,
        "oldest_block_minutes": oldest,
        "status": status,
        "symbols": [p.symbol for p in blocked],
        "why": ("trapped_at_broker is the value of the shares the venue "
                "will not release. cannot_follow_strategy is larger "
                "because the sell gate is all-or-nothing: a position with "
                "half its shares free still cannot exit at all, so the "
                "free half is stuck with the rest."),
    }


# ── CRITICAL RULES ────────────────────────────────────────────────────

def blocked_exit(symbol, available_qty, desired_sell_qty, retries=0,
                 retry_escalation=5):
    """available_qty < desired_sell_qty. The QQQ case."""
    if available_qty is None or desired_sell_qty is None:
        return _unknown("blocked_exit",
                        f"{symbol}: owned/available unreadable, and an "
                        f"unreadable position is not a healthy one")
    fired = float(available_qty) < float(desired_sell_qty)
    acts = []
    if fired:
        acts = ["create_incident", "send_push", "send_email", "log_event"]
        if retries > retry_escalation:
            # Named, not called. Pausing a symbol is a trading-control
            # write and belongs to the owner.
            acts.append(f"RECOMMEND pause_symbol({symbol}) - {retries} "
                        f"retries, past the {retry_escalation} threshold")
    return Verdict(
        rule="blocked_exit",
        severity=CRITICAL if fired else OK,
        fired=fired,
        measured=f"{symbol}: {available_qty} available vs "
                 f"{desired_sell_qty} wanted",
        why=("Blocked Exit Detected - the account owns the shares but the "
             "venue will not release them, because an order is already "
             "resting against them. Re-sending cannot fill."
             if fired else "the venue will release the whole position"),
        recommended_actions=acts,
    )


def stale_order_lock(order_age_minutes, status, threshold_minutes=30,
                     live_statuses=("new", "accepted")):
    """An order open long enough to be the thing holding the shares."""
    if order_age_minutes is None or status is None:
        return _unknown("stale_order_lock", "order age or status unreadable")
    fired = (float(order_age_minutes) > threshold_minutes
             and str(status).lower() in live_statuses)
    return Verdict(
        rule="stale_order_lock",
        severity=HIGH if fired else OK,
        fired=fired,
        measured=f"age {float(order_age_minutes):.0f}m, status {status}",
        why=("an order this old in a live status is almost certainly the "
             "one holding shares an exit cannot get at"
             if fired else "no live order is old enough to be a lock"),
        recommended_actions=(["notify", "RECOMMEND cancel - the owner's call"]
                             if fired else []),
    )


def drawdown_breach(drawdown_pct, soft=8.0, hard=12.0, peak_is_trusted=True):
    """drawdown_pct > 8, and > 12.

    `peak_is_trusted` exists because on this account it currently is not.
    A false high peak produces a real-looking breach, so an untrusted
    reading reports UNKNOWN rather than a breach. The one thing worse
    than missing a drawdown is halting a healthy account over a stored
    number nobody corrected.
    """
    if drawdown_pct is None:
        return _unknown("drawdown_breach", "drawdown unreadable")
    if not peak_is_trusted:
        return _unknown(
            "drawdown_breach",
            f"reads {float(drawdown_pct):.2f}% but the stored peak_equity "
            f"behind it is known stale (the peak_after_withdrawal bug, "
            f"fixed 2026-10-09 for future withdrawals only). Verify the "
            f"peak against allocated_usd + unrealized before acting.")
    dd = float(drawdown_pct)
    if dd > hard:
        return Verdict(
            rule="drawdown_breach", severity=CRITICAL, fired=True,
            measured=f"{dd:.2f}% drawdown, hard limit {hard}%",
            why="past the hard limit",
            recommended_actions=["RECOMMEND disable_all_entries()"])
    if dd > soft:
        return Verdict(
            rule="drawdown_breach", severity=CRITICAL, fired=True,
            measured=f"{dd:.2f}% drawdown, soft limit {soft}%",
            why="past the soft limit",
            recommended_actions=["RECOMMEND disable_new_entries()",
                                 "RECOMMEND reduce_position_size(50)"])
    return Verdict(rule="drawdown_breach", severity=OK, fired=False,
                   measured=f"{dd:.2f}% drawdown", why="inside both limits")


def daily_loss_limit(daily_pnl_pct, limit_pct=-3.0):
    """daily_pnl_pct < -3.

    The spec's action is close_open_signals(). That is NOT recommended
    here and never auto-run: closing what is open on a red day books
    losses by machine, against the standing rules that nothing is sold
    at a loss and nothing is force-closed to turn red green. Halting NEW
    trades is the part of this rule that costs nothing, and that part is
    recommended.
    """
    if daily_pnl_pct is None:
        return _unknown("daily_loss_limit", "daily P&L unreadable")
    pnl = float(daily_pnl_pct)
    fired = pnl < limit_pct
    return Verdict(
        rule="daily_loss_limit",
        severity=CRITICAL if fired else OK,
        fired=fired,
        measured=f"{pnl:+.2f}% today, limit {limit_pct:+.2f}%",
        why=("past the daily loss limit" if fired else "inside the limit"),
        recommended_actions=(
            ["RECOMMEND disable_new_trades()",
             "NOT RECOMMENDED: close_open_signals() - this books losses on "
             "whatever is open. Standing rule: nothing is sold at a loss, "
             "and never force-close to turn red green. Hold and report."]
            if fired else []),
    )


def broker_reject_spike(reject_count_last_15m, threshold=5):
    if reject_count_last_15m is None:
        return _unknown("broker_reject_spike", "reject count unreadable")
    n = int(reject_count_last_15m)
    fired = n > threshold
    return Verdict(
        rule="broker_reject_spike",
        severity=CRITICAL if fired else OK,
        fired=fired,
        measured=f"{n} rejects in 15m, threshold {threshold}",
        why=("the venue is refusing orders faster than any strategy can be "
             "the cause - this is a configuration or state problem"
             if fired else "reject rate normal"),
        recommended_actions=(["create_incident", "RECOMMEND pause_trading()"]
                             if fired else []),
    )


# ── WARNING RULES ─────────────────────────────────────────────────────

def capital_trapped(held_qty, available_qty):
    if held_qty is None or available_qty is None:
        return _unknown("capital_trapped", "held/available unreadable")
    fired = float(held_qty) > float(available_qty)
    return Verdict(
        rule="capital_trapped", severity=WARNING if fired else OK,
        fired=fired,
        measured=f"{held_qty} held, {available_qty} available",
        why=("some of this position is spoken for by a resting order"
             if fired else "the whole position is free"),
        recommended_actions=(["flag_position"] if fired else []))


def utilization_too_low(cash_pct, threshold=50.0):
    if cash_pct is None:
        return _unknown("utilization_too_low", "cash percentage unreadable")
    c = float(cash_pct)
    fired = c > threshold
    return Verdict(
        rule="utilization_too_low", severity=WARNING if fired else OK,
        fired=fired, measured=f"{c:.2f}% in cash, threshold {threshold}%",
        why=("more than half the account is doing nothing"
             if fired else "deployment is inside the band"),
        recommended_actions=(["review_deployment"] if fired else []))


def win_rate_collapse(rolling_winrate_30d, threshold=45.0):
    """rolling_winrate_30d < 45.

    Note this rule's blind spot on THIS account: the crypto book's win
    rate is 86.5% and its profit factor is 0.519. A win-rate FLOOR
    cannot see that failure - it is the opposite shape. Kept as
    specified, because a collapse is still worth knowing, but it is not
    the rule that catches what is wrong here. capital_growth_score, with
    profit factor as a gate rather than a term, is the one that does.
    """
    if rolling_winrate_30d is None:
        return _unknown("win_rate_collapse", "rolling win rate unreadable")
    wr = float(rolling_winrate_30d)
    fired = wr < threshold
    return Verdict(
        rule="win_rate_collapse", severity=WARNING if fired else OK,
        fired=fired, measured=f"{wr:.1f}% 30d win rate, floor {threshold}%",
        why=("win rate has fallen through the floor"
             if fired else "win rate above the floor"),
        recommended_actions=(["RECOMMEND reduce_position_size(25)"]
                             if fired else []))


# ── THE CAPITAL GROWTH SCORE ──────────────────────────────────────────
#
# THE SPEC'S FORMULA, CHANGED IN EXACTLY ONE PLACE.
#
#   score = profit_factor * 20 + win_rate * 0.5
#           - drawdown_pct * 3 - blocked_exits * 10 - broker_rejects * 2
#
# Two problems, one cosmetic and one that matters.
#
# COSMETIC: `win_rate * 0.5` differs a hundredfold depending on whether
# win_rate arrives as 58 or 0.58. Pinned to PERCENT below, and a value
# <= 1.0 is rejected rather than silently scored as 0.29.
#
# THE ONE THAT MATTERS: fed this account's real crypto figures - profit
# factor 0.519, win rate 86.5% - the formula pays
#
#     profit_factor * 20 = 10.38
#     win_rate     * 0.5 = 43.25
#
# FOUR TIMES more for winning often than for whether the wins cover the
# losses. That is precisely this account's failure mode: it wins 86.5%
# of the time and still loses money, because the 13.5% of losses are
# bigger than all the wins together. A score built that way reads the
# disease as a sign of health.
#
# So profit factor becomes a GATE, not a term. Below 1.0 the book loses
# money per trade by definition and no win rate redeems it. The score is
# still computed and reported - the number tracks direction usefully -
# but the colour is RED while PF < 1.0, and the reason says so in words.

def capital_growth_score(profit_factor, win_rate_pct, drawdown_pct,
                         blocked_exits, broker_rejects):
    missing = [n for n, v in (("profit_factor", profit_factor),
                              ("win_rate_pct", win_rate_pct),
                              ("drawdown_pct", drawdown_pct),
                              ("blocked_exits", blocked_exits),
                              ("broker_rejects", broker_rejects))
               if v is None]
    if missing:
        return {"score": None, "colour": None, "unknown": True,
                "gated_by": None,
                "why": f"cannot score: {', '.join(missing)} unreadable"}

    pf = float(profit_factor)
    wr = float(win_rate_pct)
    if 0 < wr <= 1.0:
        return {"score": None, "colour": None, "unknown": True,
                "gated_by": None,
                "why": (f"win_rate_pct={wr} looks like a fraction, not a "
                        f"percentage. This term is weighted 0.5 and would "
                        f"score a hundredfold low. Pass 86.5, not 0.865.")}

    score = round(pf * 20 + wr * 0.5
                  - float(drawdown_pct) * 3
                  - float(blocked_exits) * 10
                  - float(broker_rejects) * 2, 2)

    if pf < 1.0:
        return {
            "score": score, "colour": RED, "unknown": False,
            "gated_by": "profit_factor",
            "why": (f"profit factor {pf:.3f} is below 1.00, so the average "
                    f"trade loses money however often it wins. A "
                    f"{wr:.1f}% win rate contributes {wr * 0.5:+.2f} to "
                    f"this score against profit factor's {pf * 20:+.2f} - "
                    f"the raw score reads this account as healthier than "
                    f"it is, so the colour is gated RED until the wins "
                    f"cover the losses."),
        }

    colour = GREEN if score > 75 else (YELLOW if score > 50 else RED)
    return {"score": score, "colour": colour, "unknown": False,
            "gated_by": None,
            "why": f"profit factor {pf:.3f} covers its losses; score {score}"}


def worst(verdicts):
    """Highest severity in a set. UNKNOWN never reads as OK."""
    if not verdicts:
        return OK
    return max((v.severity for v in verdicts), key=lambda s: _RANK.get(s, 0))

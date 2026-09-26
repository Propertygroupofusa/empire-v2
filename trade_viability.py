"""Can a proposed trade setup clear its own costs? Answer before sizing it.

Built 2026-09-26 after a 5-minute leveraged scalping model was proposed
with a take-profit of 0.20% and an assumed round-trip fee of 0.05%. This
account pays 0.70% maker both legs, 1.19% measured blended, 1.50% taker
both legs - between fourteen and thirty times the assumed figure. At the
real rate that setup loses 0.50% on every WINNING trade, so no win rate
saves it. It is not a risky plan, it is an arithmetically impossible one,
and the arithmetic fits on one line.

The general lesson, which is why this is a module and not a one-off
answer: a fee is a FIXED toll per round trip, and any setup whose target
is smaller than the toll is already decided before the market opens. The
step study found the same thing for grid spacing. This answers it for any
entry/exit pair.

    breakeven_win_rate is the number to look at. When the net on a win is
    negative it does not exist - reported as None, never as 100%, because
    "you need a 100% win rate" reads as hard-but-possible and this is not.
"""

MEASURED_MAKER_RT_PCT = 0.70
MEASURED_BLENDED_RT_PCT = 1.1931
MEASURED_TAKER_RT_PCT = 1.50


def net_on_win(tp_pct, fee_pct):
    """What a winning trade actually keeps, after the round trip."""
    return round(float(tp_pct) - float(fee_pct), 6)


def net_on_loss(sl_pct, fee_pct):
    """What a losing trade actually costs. The fee is paid on a loss too -
    forgetting that is how a '1% risk' becomes a larger one."""
    return round(-abs(float(sl_pct)) - float(fee_pct), 6)


def breakeven_win_rate(tp_pct, sl_pct, fee_pct):
    """Fraction of trades that must win for the setup to break even.

    None when a winning trade is itself unprofitable: there is then no win
    rate that works, and saying "100%" would understate that by implying
    a perfect record would be enough. It would not.
    """
    w = net_on_win(tp_pct, fee_pct)
    l = net_on_loss(sl_pct, fee_pct)
    if w <= 0:
        return None
    return round(-l / (w - l), 6)


def min_tp_for_fee(fee_pct, min_net_pct=0.0):
    """The smallest target that clears the toll, plus any margin wanted."""
    return round(float(fee_pct) + float(min_net_pct), 6)


def max_leverage_for_risk(bankroll_risk_pct, sl_price_pct):
    """Leverage that puts exactly `bankroll_risk_pct` at risk for a stop
    `sl_price_pct` away. The identity the proposed matrix inverted."""
    sl = abs(float(sl_price_pct))
    if sl <= 0:
        return None
    return round(float(bankroll_risk_pct) / sl, 4)


def position_and_loss(bankroll, leverage, sl_price_pct):
    """What a stop actually costs, from position size rather than intent.

    The proposed matrix listed a flat $1,000 position at every leverage
    and a flat $10 loss beside it. A $1,000 position stopped at 0.20% loses
    $2.00, not $10.00 - the loss column was out by the leverage factor on
    every row. Position size is bankroll x leverage, and the loss follows
    from it.
    """
    position = float(bankroll) * float(leverage)
    return {
        "position_usd": round(position, 2),
        "margin_usd": round(position / float(leverage), 2) if leverage else None,
        "loss_usd": round(position * abs(float(sl_price_pct)) / 100, 2),
    }


def assess(tp_pct, sl_pct, fee_pct, *, bankroll=None, leverage=None,
           bankroll_risk_pct=None):
    """One setup, judged. `ok` False means do not trade it at this fee."""
    w = net_on_win(tp_pct, fee_pct)
    l = net_on_loss(sl_pct, fee_pct)
    be = breakeven_win_rate(tp_pct, sl_pct, fee_pct)

    out = {
        "tp_pct": float(tp_pct), "sl_pct": float(sl_pct), "fee_pct": float(fee_pct),
        "net_on_win_pct": w,
        "net_on_loss_pct": l,
        "breakeven_win_rate_pct": None if be is None else round(be * 100, 2),
        "min_viable_tp_pct": min_tp_for_fee(fee_pct),
    }

    if w <= 0:
        out["ok"] = False
        out["verdict"] = (
            f"IMPOSSIBLE at this fee. A winning trade nets {w:+.4f}% - the "
            f"{fee_pct:.4f}% round trip is larger than the {float(tp_pct):.4f}% "
            f"target. Every win is a loss, so no win rate rescues it. The target "
            f"must exceed {out['min_viable_tp_pct']:.4f}% before anything else "
            f"about the setup matters.")
    elif be is not None and be >= 0.95:
        out["ok"] = False
        out["verdict"] = (f"Needs a {be * 100:.1f}% win rate to break even. "
                          f"Technically possible, practically not.")
    else:
        out["ok"] = True
        out["verdict"] = (f"Clears costs: a win nets {w:+.4f}%, a loss costs "
                          f"{l:+.4f}%, break-even win rate {be * 100:.1f}%.")

    if bankroll is not None and leverage is not None:
        out.update(position_and_loss(bankroll, leverage, sl_pct))
    if bankroll_risk_pct is not None:
        out["max_leverage_for_risk"] = max_leverage_for_risk(bankroll_risk_pct, sl_pct)
    return out

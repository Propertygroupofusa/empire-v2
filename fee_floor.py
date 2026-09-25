"""One rule, stated once: a target below the round-trip fee cannot win.

WHY THIS EXISTS, and why it is a SHARED module rather than a fourth
private copy.

On 2026-09-25 the same defect was found in four separate places in one
evening:

  1.25%  a spacing I recommended for the live grid fleet, against a
         1.70% floor. Caught by a check before it shipped.
  1.00%  five grid branches were CREATED at this against the same floor,
         because the floor only ran when a dynamic source set the value
         and all three dynamic sources had just been switched off.
  0.25%  a committed, live-flagged config (bot_config.json, SHIB/PEPE/
         LINK, paper_trading false, live_trading true, $1,200) whose
         profit target was SIX TIMES SMALLER than the taker round trip.
         Every winning trade would have lost 1.25%.
  1.00%  crypto_grid_bot's own create-branch default, the source of the
         five branches above.

Three engines already had fee protection. Each had built its own, with a
different name and a different shape, and each had built it REACTIVELY
after its own incident:

  crypto_grid_bot.fee_safe_floor_pct     percentage floor on grid spacing
  prop_bot.fee_safe_target_dollars       dollar floor on an Alpaca target
  crypto_btc_compound.min_profit_target_pct  dollar floor expressed as a pct

None of them knew about the others, and four engines had nothing at all:
crypto_mean_reversion_bot, crypto_coinbase_bot, crypto_family_tree_bot,
alpaca_swing_bot. That is how a 0.25% target reached a live config file -
not because anyone argued for it, but because nothing in the path it took
had ever been given the rule.

WHAT A FLOOR ACTUALLY BUYS YOU. It does not make losing impossible;
nothing does. It removes one specific class of loss - the trade that
cannot win even when it is RIGHT about direction. Everything else in
trading is a probability. This is arithmetic: if the move you are
targeting is smaller than the fee you pay to capture it, a correct
prediction still loses money. A floor converts that from a silent,
repeating loss into a refusal at configuration time.

"WE'LL NEVER REACH THE FLOOR" is the argument this module exists to
answer, and the record answers it: it was reached four times in one
evening, once in a file already flagged for live trading. A floor is not
insurance against an unlikely event. It is a guard rail on the edge that
was actually walked over.

THE RULE: a target must clear the WORST round trip that trade can really
pay, plus a margin worth trading for. Worst, not expected - a maker order
that does not fill becomes a market order, so the optimistic rate is an
estimate and the pessimistic one is the guarantee.
"""

# The smallest net margin worth putting capital at risk for, on top of
# fees. Not a profit goal - a floor beneath which the trade is noise.
DEFAULT_MIN_NET_MARGIN_PCT = 0.002  # 0.20%


class TargetBelowFeeFloor(ValueError):
    """Raised when a configured target cannot clear its own round trip."""


def fee_floor_pct(round_trip_fee_pct, min_net_margin_pct=DEFAULT_MIN_NET_MARGIN_PCT):
    """The smallest target that clears `round_trip_fee_pct` and still pays.

    round_trip_fee_pct is BOTH legs together, as a fraction (0.015 = 1.5%).
    Pass the worst rate the trade can really pay, not the expected one.
    """
    fee = max(0.0, float(round_trip_fee_pct or 0.0))
    margin = max(0.0, float(min_net_margin_pct or 0.0))
    return fee + margin


def clears_fees(target_pct, round_trip_fee_pct,
                min_net_margin_pct=DEFAULT_MIN_NET_MARGIN_PCT):
    """True when this target can actually pay. No side effects."""
    return float(target_pct or 0.0) >= fee_floor_pct(round_trip_fee_pct, min_net_margin_pct)


def net_per_win_pct(target_pct, round_trip_fee_pct):
    """What a WINNING trade actually nets after fees. Negative means the
    win is a loss - the case a floor exists to make unreachable."""
    return float(target_pct or 0.0) - max(0.0, float(round_trip_fee_pct or 0.0))


def raise_to_floor(target_pct, round_trip_fee_pct,
                   min_net_margin_pct=DEFAULT_MIN_NET_MARGIN_PCT):
    """The target, raised to the floor if it sits below it.

    Use where a bad value must not stop trading - a grid branch already
    running, say. Never lowers a target.
    """
    floor = fee_floor_pct(round_trip_fee_pct, min_net_margin_pct)
    return max(float(target_pct or 0.0), floor)


def require_clears_fees(target_pct, round_trip_fee_pct, context="",
                        min_net_margin_pct=DEFAULT_MIN_NET_MARGIN_PCT):
    """Refuse a target that cannot win. Returns it unchanged when it can.

    Use at CONFIGURATION time, where refusing loudly is better than
    silently trading something that loses on every cycle. The 0.25%
    config would have been stopped here, before an order existed.
    """
    target = float(target_pct or 0.0)
    floor = fee_floor_pct(round_trip_fee_pct, min_net_margin_pct)
    if target < floor:
        net = net_per_win_pct(target, round_trip_fee_pct)
        raise TargetBelowFeeFloor(
            f"{context or 'target'} {target * 100:.3f}% cannot clear a "
            f"{float(round_trip_fee_pct) * 100:.3f}% round trip: a WINNING trade "
            f"nets {net * 100:+.3f}%. Minimum is {floor * 100:.3f}% "
            f"(fee + {min_net_margin_pct * 100:.2f}% margin)."
        )
    return target

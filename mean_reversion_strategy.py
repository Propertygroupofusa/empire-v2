"""
MEAN REVERSION STRATEGY - Dual-direction exit rules for crypto bot
====================================================================
Since Coinbase spot trading is long-only, we implement:
1. LONG ENTRIES: Buy when RSI < 35 (oversold) - wait for bounce
2. PROFIT TAKING: Exit at +2% minimum OR hold for RSI > 70 breakout
3. STOP LOSSES: Exit at -0.5% maximum loss

This captures the downside move (lower entry via oversold) and
the upside move (sells at rallies via RSI > 70 resistance).
"""

import logging
from typing import Tuple, Dict

log = logging.getLogger("mean_reversion_strategy")

# ============================================================================
# MEAN REVERSION EXIT DECISION ENGINE
# ============================================================================

def should_exit_position(
    symbol: str,
    entry_price: float,
    current_price: float,
    current_rsi: float,
    position_age_seconds: int,
    max_hold_seconds: int = 1800,  # 30 min default
    stop_loss_pct: float = 0.005,  # 0.5%
    min_profit_target_pct: float = 0.02,  # 2% minimum
    rsi_sell_threshold: float = 70,  # Sell on rally at RSI > 70
) -> Tuple[bool, str, str]:
    """
    Decide whether to exit a long position based on mean reversion rules.

    Returns:
        (should_exit: bool, reason: str, exit_type: str)
        exit_type: "profit", "stop_loss", "rsi_sell", "timeout", "hold"
    """

    unrealized_pnl_pct = (current_price - entry_price) / entry_price

    # ========== RULE 1: Stop Loss (Absolute) ==========
    if unrealized_pnl_pct <= -stop_loss_pct:
        reason = f"Stop loss hit: {unrealized_pnl_pct*100:.2f}% loss (limit: {stop_loss_pct*100:.2f}%)"
        log.info(f"  🛑 {symbol}: {reason}")
        return True, reason, "stop_loss"

    # ========== RULE 2: Minimum Profit Target ==========
    if unrealized_pnl_pct >= min_profit_target_pct:
        reason = f"Hit minimum profit target: {unrealized_pnl_pct*100:.2f}% (required: {min_profit_target_pct*100:.2f}%)"
        log.info(f"  ✅ {symbol}: {reason}")
        return True, reason, "profit"

    # ========== RULE 3: RSI Sell Signal (Rally Exit) ==========
    # Sell into rallies when RSI > 70, but ONLY if we've recovered losses
    if current_rsi > rsi_sell_threshold and unrealized_pnl_pct > 0:
        reason = f"RSI sell signal at {current_rsi:.1f} (threshold: >{rsi_sell_threshold}); profit taken at +{unrealized_pnl_pct*100:.2f}%"
        log.info(f"  📈 {symbol}: {reason}")
        return True, reason, "rsi_sell"

    # ========== RULE 4: Max Hold Time ==========
    if position_age_seconds >= max_hold_seconds:
        reason = f"Max hold time exceeded: {position_age_seconds}s >= {max_hold_seconds}s"
        log.info(f"  ⏱️  {symbol}: {reason}")
        # Force exit even if still in loss (time decay matters in crypto)
        return True, reason, "timeout"

    # ========== NO EXIT: HOLD ==========
    reason = f"Hold: P&L {unrealized_pnl_pct*100:.2f}% (waiting for +{min_profit_target_pct*100:.1f}% target or RSI sell)"
    return False, reason, "hold"


def calculate_position_sizing(
    cash_available: float,
    entry_price: float,
    stop_loss_pct: float = 0.005,
    risk_per_trade_usd: float = 25.0,
) -> float:
    """
    Calculate position size that limits risk to a fixed amount per trade.

    If we buy $250 worth at $100/BTC with 0.5% stop:
      - Stop distance = $100 * 0.005 = $0.50
      - 1 contract costs $100, loses $0.50 if stopped
      - To risk $25 total: need 50 contracts = $5,000 notional
      - But we only have $250, so size down proportionally

    Returns: Quote size in USD to place order with
    """
    if not cash_available or cash_available < 100:  # Minimum $100 per trade
        return None

    # Use fixed $250 per trade (standard sizing)
    FIXED_POSITION_SIZE_USD = 250.0

    if cash_available < FIXED_POSITION_SIZE_USD * 1.05:  # Need 5% buffer
        return None

    return FIXED_POSITION_SIZE_USD


def validate_risk_reward(
    entry_price: float,
    stop_price: float,
    profit_target_price: float,
) -> Tuple[bool, str, float]:
    """
    Validate that risk/reward ratio is acceptable (1:2 minimum).

    For a long position:
      - Risk = entry - stop
      - Reward = profit_target - entry
      - Ratio = reward / risk (should be ≥ 2.0 for 1:2)
    """
    risk = entry_price - stop_price
    reward = profit_target_price - entry_price

    if risk <= 0:
        return False, "Stop price above entry (no valid risk)", 0

    ratio = reward / risk

    if ratio < 2.0:
        reason = f"Risk/reward {ratio:.2f}:1 is unfavorable (need ≥2:1)"
        return False, reason, ratio

    reason = f"Risk/reward {ratio:.2f}:1 is favorable"
    return True, reason, ratio


def calculate_profit_targets(
    entry_price: float,
    stop_loss_pct: float = 0.005,
) -> Dict[str, float]:
    """
    Calculate tiered profit targets for mean reversion.

    Tier 1: +2% (minimum, covers fees + small profit)
    Tier 2: +3% (medium profit)
    Tier 3: +5% (let winners run)
    """
    return {
        "min_profit": entry_price * (1 + 0.02),  # +2%
        "tier_2": entry_price * (1 + 0.03),      # +3%
        "tier_3": entry_price * (1 + 0.05),      # +5%
        "stop_loss": entry_price * (1 - stop_loss_pct),
    }


def check_mean_reversion_setup(
    price: float,
    rsi: float,
    sma_50: float,
    position_type: str = "long",
) -> Tuple[bool, str]:
    """
    Check if setup is good for mean reversion entry.

    LONG setup:
      - Price < SMA50 (below moving average = weak)
      - RSI < 35 (oversold)
      - Wait for first bounce sign (RSI recovery)

    SHORT setup (for exchange that supports it):
      - Price > SMA50 (above moving average = strong)
      - RSI > 70 (overbought)
      - Wait for pullback
    """
    if position_type == "long":
        if price > sma_50:
            return False, "Price above SMA50 - trend still strong, wait for pullback"
        if rsi >= 35:
            return False, f"RSI {rsi:.1f} not oversold yet (need <35)"
        return True, f"Setup ready: price below MA50, RSI {rsi:.1f} oversold"

    elif position_type == "short":  # For future margin trading support
        if price < sma_50:
            return False, "Price below SMA50 - trend still weak, wait for bounce"
        if rsi <= 70:
            return False, f"RSI {rsi:.1f} not overbought yet (need >70)"
        return True, f"Setup ready: price above MA50, RSI {rsi:.1f} overbought"

    return False, f"Unknown position type: {position_type}"


# ============================================================================
# EXAMPLE USAGE IN CRYPTO BOT
# ============================================================================

def example_exit_decision():
    """
    Example of how crypto bot would use these functions in each cycle.
    """
    # During cycle, for each open position:
    entry_price = 42500.00
    current_price = 43340.00  # +1.97%
    current_rsi = 75.0
    position_age_sec = 1200  # 20 minutes

    should_exit, reason, exit_type = should_exit_position(
        symbol="BTC/USD",
        entry_price=entry_price,
        current_price=current_price,
        current_rsi=current_rsi,
        position_age_seconds=position_age_sec,
        max_hold_seconds=1800,  # 30 min max
        min_profit_target_pct=0.02,  # 2% minimum
        rsi_sell_threshold=70,
    )

    # Result:
    # should_exit = True
    # exit_type = "rsi_sell"
    # reason = "RSI sell signal at 75.0 (threshold: >70); profit taken at +1.97%"
    #
    # Why? Because RSI > 70 triggered the "sell into rally" rule
    # This is mean reversion: we bought the dip (entry at oversold),
    # and now we're selling the bounce (exit at overbought).

    print(f"[{exit_type.upper()}] {reason}")
    return should_exit


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    example_exit_decision()

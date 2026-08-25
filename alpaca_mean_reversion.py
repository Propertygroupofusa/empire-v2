"""
ALPACA MEAN REVERSION STRATEGY — Dual-direction trading (long + short)
========================================================================
Alpaca supports actual shorting via margin trading, so we implement:

1. LONG ENTRIES: Buy when RSI < 30 (oversold) + capture downside
2. SHORT ENTRIES: Short when RSI > 70 (overbought) + capture downside
3. PROFIT TARGETS: Exit at +2% minimum OR hold for 2-5% gains
4. STOP LOSSES: Long -1.5%, Short -1.5% (symmetrical)

This captures BOTH sides of mean reversion:
- Oversold bounces (long entries)
- Overbought pullbacks (short entries)

Key difference from crypto bot: Alpaca supports real shorting,
so we don't need to fake it with exits. We can actually short overheated stocks.
"""

import logging
from typing import Tuple, Dict

log = logging.getLogger("alpaca_mean_reversion")

# ============================================================================
# DUAL-DIRECTION EXIT DECISION ENGINE (STOCKS)
# ============================================================================

def should_exit_position(
    symbol: str,
    entry_price: float,
    current_price: float,
    current_rsi: float,
    position_age_seconds: int,
    direction: str = "long",  # "long" or "short"
    max_hold_seconds: int = 7200,  # 2 hours default
    stop_loss_pct: float = 0.015,  # 1.5%
    min_profit_target_pct: float = 0.02,  # 2% minimum
    rsi_profit_threshold_long: float = 60,  # Take profit on rally
    rsi_profit_threshold_short: float = 40,  # Take profit on bounce
    peak_pnl_pct: float = 0.0,  # highest unrealized % this position has ever reached - caller persists this (see BotPosition.peak_pct)
    breakeven_trigger_pct: float = 0.01,  # once peak reaches +1%, the stop can no longer go below breakeven
    max_giveback_pct: float = 0.005,  # once ANY real profit has been reached, cap how much of the peak can be given back before forcing an exit
) -> Tuple[bool, str, str, float]:
    """
    Decide whether to exit a position (long or short) based on mean reversion rules.

    Returns:
        (should_exit: bool, reason: str, exit_type: str, new_peak_pnl_pct: float)
        exit_type: "profit", "stop_loss", "rsi_exit", "timeout", "trail", "hold"
        new_peak_pnl_pct: the caller MUST persist this (see BotPosition.peak_pct) and
        pass it back in as peak_pnl_pct on the next call - a Railway restart wiping an
        in-memory-only peak would silently disarm both new rules below on exactly the
        positions that had run up the most.
    """

    if direction == "long":
        unrealized_pnl_pct = (current_price - entry_price) / entry_price
        rsi_exit_threshold = rsi_profit_threshold_long
    elif direction == "short":
        unrealized_pnl_pct = (entry_price - current_price) / entry_price
        rsi_exit_threshold = rsi_profit_threshold_short
    else:
        return False, f"Unknown direction: {direction}", "error", peak_pnl_pct

    new_peak_pnl_pct = max(peak_pnl_pct, unrealized_pnl_pct)

    # ========== RULE 0: Breakeven ratchet ==========
    # Once this position has ever been up by breakeven_trigger_pct or more,
    # the hard stop can no longer be allowed to close it below its own
    # entry price - only the peak-giveback rule below governs a pullback
    # from there. A position that's never reached the trigger keeps the
    # full, unmodified stop_loss_pct room to work.
    effective_stop_loss_pct = 0.0 if new_peak_pnl_pct >= breakeven_trigger_pct else stop_loss_pct

    # ========== RULE 1: Stop Loss (Absolute / Breakeven) ==========
    if unrealized_pnl_pct <= -effective_stop_loss_pct:
        if effective_stop_loss_pct == 0.0:
            reason = f"Breakeven stop hit: {unrealized_pnl_pct*100:.2f}% (was up {new_peak_pnl_pct*100:.2f}% at peak, can't close below breakeven)"
        else:
            reason = f"Stop loss hit: {unrealized_pnl_pct*100:.2f}% loss (limit: {stop_loss_pct*100:.2f}%)"
        log.info(f"  🛑 {symbol} ({direction.upper()}): {reason}")
        return True, reason, "stop_loss", new_peak_pnl_pct

    # ========== RULE 1b: Peak-profit giveback ==========
    # Once this position has ever shown a real profit, it can't give back
    # more than max_giveback_pct from its best point without forcing an
    # exit - independent of whether min_profit_target_pct or an RSI exit
    # has fired yet. Catches exactly the case a fixed stop/target alone
    # misses: up 1.8%, never quite reaching the 2% target, then reversing
    # hard - previously rode all the way down to -stop_loss_pct, giving
    # back the entire gain plus more.
    peak_giveback = new_peak_pnl_pct - unrealized_pnl_pct
    if new_peak_pnl_pct > 0 and peak_giveback >= max_giveback_pct:
        reason = f"Peak profit giveback: gave back {peak_giveback*100:.2f}% from a {new_peak_pnl_pct*100:.2f}% peak (limit: {max_giveback_pct*100:.2f}%)"
        log.info(f"  💰 {symbol} ({direction.upper()}): {reason}")
        return True, reason, "trail", new_peak_pnl_pct

    # ========== RULE 2: Minimum Profit Target ==========
    if unrealized_pnl_pct >= min_profit_target_pct:
        reason = f"Hit minimum profit target: {unrealized_pnl_pct*100:.2f}% (required: {min_profit_target_pct*100:.2f}%)"
        log.info(f"  ✅ {symbol} ({direction.upper()}): {reason}")
        return True, reason, "profit", new_peak_pnl_pct

    # ========== RULE 3: RSI Exit Signal ==========
    if direction == "long" and current_rsi >= rsi_exit_threshold and unrealized_pnl_pct > 0:
        # Long: Sell on rally when RSI gets hot
        reason = f"RSI exit signal at {current_rsi:.1f} (threshold: {rsi_exit_threshold}); profit taken at +{unrealized_pnl_pct*100:.2f}%"
        log.info(f"  📈 {symbol} (LONG): {reason}")
        return True, reason, "rsi_exit", new_peak_pnl_pct
    elif direction == "short" and current_rsi <= rsi_exit_threshold and unrealized_pnl_pct > 0:
        # Short: Cover on bounce when RSI recovers
        reason = f"RSI exit signal at {current_rsi:.1f} (threshold: {rsi_exit_threshold}); profit taken at +{unrealized_pnl_pct*100:.2f}%"
        log.info(f"  📉 {symbol} (SHORT): {reason}")
        return True, reason, "rsi_exit", new_peak_pnl_pct

    # ========== RULE 4: Max Hold Time ==========
    if position_age_seconds >= max_hold_seconds:
        reason = f"Max hold time exceeded: {position_age_seconds}s >= {max_hold_seconds}s"
        log.info(f"  ⏱️  {symbol} ({direction.upper()}): {reason}")
        return True, reason, "timeout", new_peak_pnl_pct

    # ========== NO EXIT: HOLD ==========
    reason = f"Hold: P&L {unrealized_pnl_pct*100:.2f}% (waiting for +{min_profit_target_pct*100:.1f}% target or RSI exit)"
    return False, reason, "hold", new_peak_pnl_pct


def should_exit_position_momentum(
    symbol: str,
    entry_price: float,
    current_price: float,
    position_age_seconds: int,
    peak_pnl_pct: float = 0.0,
    max_hold_seconds: int = 86400,  # 24 real hours - a backstop only, much longer than mean-reversion's 2-hour default
    trail_pct: float = 0.03,  # exits on a pullback this large from the real peak since entry, not a fixed target off entry
) -> Tuple[bool, str, str, float]:
    """Momentum's real exit rule - the direct live counterpart to
    alpaca_selection_backtest.py's _replay_symbol_momentum(), which this
    mirrors exactly (not a reimplementation with different math): a
    trailing stop measured off the real PEAK price reached since entry,
    not a small fixed target off entry. peak_pnl_pct starts at 0.0 (the
    peak "price" starts at the entry price itself, same as the backtest's
    own position["peak"] = price at entry) - a position that never once
    goes positive still has a real effective stop at trail_pct below its
    own entry, exactly matching the backtest's behavior.

    Returns (should_exit, reason, exit_type, new_peak_pnl_pct) - same
    shape as should_exit_position() above, so callers persist the peak
    the same way (BotPosition.peak_pct)."""
    unrealized_pnl_pct = (current_price - entry_price) / entry_price
    new_peak_pnl_pct = max(peak_pnl_pct, unrealized_pnl_pct)
    trailing_stop_pct = new_peak_pnl_pct - trail_pct

    if unrealized_pnl_pct <= trailing_stop_pct:
        reason = (
            f"Momentum trailing stop: pulled back to {unrealized_pnl_pct*100:.2f}% from a "
            f"{new_peak_pnl_pct*100:.2f}% peak (trail: {trail_pct*100:.1f}%)"
        )
        log.info(f"  📉 {symbol}: {reason}")
        return True, reason, "trail", new_peak_pnl_pct

    if position_age_seconds >= max_hold_seconds:
        reason = f"Max hold time exceeded: {position_age_seconds}s >= {max_hold_seconds}s"
        log.info(f"  ⏱️  {symbol}: {reason}")
        return True, reason, "timeout", new_peak_pnl_pct

    reason = f"Hold: P&L {unrealized_pnl_pct*100:.2f}% (peak {new_peak_pnl_pct*100:.2f}%, trailing stop at {trailing_stop_pct*100:.2f}%)"
    return False, reason, "hold", new_peak_pnl_pct


def calculate_position_sizing_stocks(
    cash_available: float,
    entry_price: float,
    stop_loss_pct: float = 0.015,
    risk_per_trade_usd: float = 50.0,
    account_equity: float = None,
) -> float:
    """
    Calculate position size that scales with account growth (exponential compounding).

    Aggressive scaling model:
    - $1K account: 20% per trade ($200)
    - $5K account: 25% per trade ($1,250)
    - $10K account: 30% per trade ($3,000)
    - $25K account: 35% per trade ($8,750)
    - $50K+ account: 40% per trade ($20K+)

    This ensures profit-taking generates exponentially larger gains as capital compounds.

    Returns: Number of shares to purchase
    """
    if not cash_available or cash_available < 100:
        return None

    # Determine position size percentage based on account equity
    if account_equity is None:
        account_equity = cash_available

    # Dynamic position sizing: scale up as account grows
    if account_equity < 5000:
        position_size_pct = 0.20  # 20% at $1K-$5K
    elif account_equity < 10000:
        position_size_pct = 0.25  # 25% at $5K-$10K
    elif account_equity < 25000:
        position_size_pct = 0.30  # 30% at $10K-$25K
    elif account_equity < 50000:
        position_size_pct = 0.35  # 35% at $25K-$50K
    else:
        position_size_pct = 0.40  # 40% at $50K+

    position_size = cash_available * position_size_pct

    # Minimum position size to avoid dust trades
    if position_size < 200:
        return None

    # Calculate shares from position size
    shares = position_size / entry_price
    return int(shares) if shares >= 1 else None


def validate_dual_direction(
    symbol: str,
    current_rsi: float,
    sma_50: float,
    current_price: float,
    cash_available: float,
    open_positions: int,
    max_open: int = 6,
) -> Tuple[str, bool, str]:
    """
    Determine if we should enter LONG, SHORT, or HOLD.
    AGGRESSIVE MODE: Lower RSI thresholds + no SMA requirement (catch MORE entries).

    Returns:
        (direction, should_enter, reason)
        direction: "long", "short", or "hold"
    """
    import os
    import logging
    log = logging.getLogger("alpaca_mean_reversion")

    def _safe_float_env(name: str, default: float) -> float:
        """A malformed Railway variable (e.g. someone pastes 'default: 40'
        instead of '40') used to raise ValueError here and crash the whole
        trading cycle, every cycle, forever - falling back to the numeric
        default and logging a warning instead means a typo in one env var
        degrades to 'ignored' rather than 'bot can't trade at all'."""
        raw = os.getenv(name)
        if raw is None:
            return default
        try:
            return float(raw)
        except ValueError:
            log.warning(f"{name}={raw!r} is not a valid number - using default {default} instead. Fix this in Railway's Variables tab.")
            return default

    # Configurable aggressive thresholds (can override via env vars)
    RSI_LONG_THRESHOLD = _safe_float_env("RSI_LONG_THRESHOLD", 40.0)   # More aggressive: 40 instead of 30
    RSI_SHORT_THRESHOLD = _safe_float_env("RSI_SHORT_THRESHOLD", 60.0)  # More aggressive: 60 instead of 70
    REQUIRE_MA_CONFIRMATION = os.getenv("REQUIRE_MA_CONFIRMATION", "false").lower() == "true"

    # Check position limit
    if open_positions >= max_open:
        return "hold", False, f"At position limit ({open_positions}/{max_open})"

    # Check cash available
    if cash_available < 1000:
        return "hold", False, f"Insufficient cash (${cash_available:.0f} < $1,000 min)"

    # LONG setup: Oversold (aggressive: RSI < 40) — optional MA confirmation
    if current_rsi < RSI_LONG_THRESHOLD:
        if REQUIRE_MA_CONFIRMATION and current_price >= sma_50:
            return "hold", False, f"Long RSI {current_rsi:.1f} but price above MA50"
        return "long", True, f"Long entry: RSI {current_rsi:.1f} < {RSI_LONG_THRESHOLD} (oversold)"

    # SHORT setup: Overbought (aggressive: RSI > 60) — optional MA confirmation
    if current_rsi > RSI_SHORT_THRESHOLD:
        if REQUIRE_MA_CONFIRMATION and current_price <= sma_50:
            return "hold", False, f"Short RSI {current_rsi:.1f} but price below MA50"
        return "short", True, f"Short entry: RSI {current_rsi:.1f} > {RSI_SHORT_THRESHOLD} (overbought)"

    # HOLD: No setup
    return "hold", False, f"No setup: RSI {current_rsi:.1f} (need < {RSI_LONG_THRESHOLD} for long or > {RSI_SHORT_THRESHOLD} for short)"


def calculate_profit_targets_stocks(
    entry_price: float,
    direction: str = "long",
    stop_loss_pct: float = 0.015,
) -> Dict[str, float]:
    """
    Calculate tiered profit targets for stocks (wider than crypto).

    Tier 1: +2% (minimum, covers fees + small profit)
    Tier 2: +5% (medium profit)
    Tier 3: +10% (let winners run)
    """
    if direction == "long":
        return {
            "min_profit": entry_price * (1 + 0.02),  # +2%
            "tier_2": entry_price * (1 + 0.05),      # +5%
            "tier_3": entry_price * (1 + 0.10),      # +10%
            "stop_loss": entry_price * (1 - stop_loss_pct),
        }
    elif direction == "short":
        return {
            "min_profit": entry_price * (1 - 0.02),  # -2% (profit when price falls)
            "tier_2": entry_price * (1 - 0.05),      # -5%
            "tier_3": entry_price * (1 - 0.10),      # -10%
            "stop_loss": entry_price * (1 + stop_loss_pct),  # Stop is above entry
        }
    else:
        return {}


# ============================================================================
# EXAMPLE USAGE
# ============================================================================

def example_stock_trade():
    """
    Example: Mean reversion trade on SPY.

    Scenario 1: LONG
    ================
    SPY oversold (RSI 28), below MA50 → BUY
    Entry: $450
    Stop: $443.20 (1.5% below)
    Target 1: $459 (+2%)
    Target 2: $473 (+5%)
    """

    # Check if we should exit the long
    should_exit, reason, exit_type, _peak = should_exit_position(
        symbol="SPY",
        entry_price=450.00,
        current_price=459.50,  # +2.1%
        current_rsi=65.0,      # Getting hot but not at threshold yet
        position_age_seconds=1800,  # 30 min old
        direction="long",
        rsi_profit_threshold_long=60,  # Sell when RSI > 60
    )

    # Result: should_exit=True, exit_type="profit"
    # Why: Current price $459.50 is +2.1%, exceeds 2% minimum
    print(f"[{exit_type.upper()}] {reason}")

    # Scenario 2: SHORT
    # ================
    # SPY overbought (RSI 75), above MA50 → SHORT
    # Entry: $455
    # Stop: $461.85 (1.5% above)
    # Target 1: $446 (-2%)
    # Target 2: $432 (-5%)

    should_exit_short, reason_short, exit_type_short, _peak_short = should_exit_position(
        symbol="SPY",
        entry_price=455.00,
        current_price=446.00,  # -1.98%
        current_rsi=35.0,      # Bounced from oversold
        position_age_seconds=900,  # 15 min old
        direction="short",
        rsi_profit_threshold_short=40,  # Cover when RSI < 40
    )

    # Result: should_exit=True, exit_type="profit"
    # Why: Current price $446 is -1.98%, close to 2% profit target
    print(f"[{exit_type_short.upper()}] {reason_short}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    example_stock_trade()

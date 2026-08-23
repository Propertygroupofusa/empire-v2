"""
Bot Mandates - Hard boundaries and success criteria for each trading bot.
These are NOT suggestions; they're enforced limits and decision rules.
"""

from enum import Enum
from typing import List, Dict

# ============================================================================
# BOT MANDATE DEFINITIONS
# ============================================================================

class BotRole(Enum):
    """Each bot has a distinct role, not just 'make money'"""
    APEX_EVALUATION = "APEX: Pass $1,500 target, respect $1,000 drawdown"
    CRYPTO_CAPTURE = "CRYPTO: Short-term opportunity capture, high frequency"
    ALPACA_GROWTH = "ALPACA: Grow small account, maintain capital availability"
    MONITORING = "MONITORING: Independent oversight, no trading"

# ============================================================================
# APEX PROP BOT MANDATE
# ============================================================================

APEX_MANDATE = {
    "role": BotRole.APEX_EVALUATION,
    "name": "prop_bot",
    "description": "Pass funded-account evaluation ($1,500 target, $1,000 max drawdown)",

    # Universe: What it can trade
    "universe": {
        "futures": ["MES", "MNQ", "MYM", "M2K"],
        "crypto": ["BTC/USD", "ETH/USD", "SOL/USD", "ADA/USD", "DOGE/USD"],
        # THE REAL BUG: this used to list the underlying tickers ("GLD",
        # "USO", "SLV"), but prop_bot.py's MANDATE CHECK 1 (and
        # validate_entry, MANDATE CHECK 2) both compare against the
        # CONTRACT CODE - the FUTURES dict key ("MGC", "MCL", "SIL"), the
        # same identifier space "futures" above already correctly uses.
        # A ticker can never equal a contract code, so every commodity
        # signal has been silently rejected at MANDATE CHECK 1 with "NOT
        # in approved universe - SKIPPING" since this mandate existed -
        # gold/oil/silver have never actually been able to place a real
        # order. Fixed to use contract codes, matching "futures".
        "commodities": ["MGC", "MCL", "SIL"],
        # 1x (non-leveraged) inverse ETFs, one per index already traded
        # above - per the account owner's explicit request to profit on
        # downtrends without taking on margin/shorting risk. These are
        # bought LONG like any other symbol here; they just move opposite
        # their underlying index, so a long entry on SH profits when SPY
        # falls. Deliberately NOT the 2x/3x leveraged versions (SDS, SQQQ,
        # SDOW, SRTY) - those add leveraged-decay risk the account owner
        # didn't ask for.
        "inverse_etfs": ["SH", "PSQ", "DOG", "RWM"],  # inverse of SPY, QQQ, DIA, IWM
        "restriction": "No other symbols allowed"
    },

    # Entry: When it can open a position
    "entry": {
        "rsi_threshold": 30,  # RSI < 30 = oversold
        "trend_required": True,  # Must confirm with SMA5/SMA10
        "min_buying_power": 150,
        "min_position_size": 50,
        "max_open_positions": 6,  # 3 longs + 3 shorts for dual-direction mean reversion
        "max_total_notional_pct": 0.50,  # 50% of equity
        "require_live_data": True,
        "data_staleness_max_sec": 120,
    },

    # Exit: When it must close positions
    "exit": {
        "profit_tiers": [0.50, 1.00, 1.50],  # Exit 1/3 at each level
        "tier_exit_pct": [0.333, 0.333, 0.334],
        "stop_loss_pct": 0.003,  # 0.3% baseline
        "stop_loss_tight_scale": 1.5,  # Tighten to 0.2% at 1.5x+
        "stop_loss_tight_pct": 0.002,
        "aggressive_loss_pct": 0.005,  # Exit at 0.5% loss
        "max_hold_time_sec": 14400,  # 4 hours
        "no_fill_timeout_sec": 30,
    },

    # Capital: How much it can deploy
    "capital": {
        "total_account": 980,
        "locked_reserve": 150,
        "deployable": 830,
        "max_per_position": 240,
        "max_open_positions": 6,  # 3 longs + 3 shorts for dual-direction mean reversion
        "max_total_notional": 490,  # 50% of 980
        "critical_buying_power": 100,  # Halt at this level
        "max_daily_loss": 10,  # $ per day
        "scale_multiplier_1x": 1.0,
        "scale_multiplier_15x": 1.5,
        "scale_multiplier_20x": 2.0,
    },

    # Kill Conditions: Stop trading if ANY of these trigger
    "kill_conditions": [
        "daily_loss_limit_hit",
        "buying_power_below_critical",
        "equity_below_survival",
        "api_connectivity_lost",
        "database_error",
        "market_data_stale_5min",
        "unexpected_position_found",
        "order_rejection_5plus",
        "risk_calculation_error",
        "margin_call_risk",
    ],

    # Success Metrics
    "kpi": {
        "primary": "Probability of passing evaluation without violating drawdown",
        "profit_target": 1500,  # Hard target
        "drawdown_limit": 1000,  # Hard limit (trailing)
        "win_rate_target": 0.40,
        "expectancy_target": 10,  # $ per trade
        "max_avg_hold_hours": 2,
    },

    "active": True,
}

# ============================================================================
# COINBASE CRYPTO BOT MANDATE
# ============================================================================

CRYPTO_MANDATE = {
    "role": BotRole.CRYPTO_CAPTURE,
    "name": "crypto_coinbase_bot",
    "description": "Dual-direction mean reversion: long on oversold (RSI<35), short on overbought (RSI>70)",

    # Universe: What it can trade
    "universe": {
        "approved_pairs": [
            "BTC/USD", "ETH/USD", "SOL/USD", "ADA/USD", "DOGE/USD",
            "XRP/USD", "LINK/USD", "AVAX/USD", "NEAR/USD", "MATIC/USD",
            "ARB/USD", "OP/USD", "APT/USD", "SEI/USD", "SUI/USD",
        ],
        "restrictions": {
            "min_24h_volume": 10000000,  # $10M min
            "max_spread_pct": 0.002,  # 0.2% max
            "require_liquidity_check": True,
        }
    },

    # Entry: DUAL DIRECTION - Long on oversold, Short on overbought
    "entry": {
        "long": {
            "rsi_threshold_oversold": 35,  # RSI < 35 = buy signal
            "volume_ratio_min": 1.5,
            "min_buying_power": 150,
            "min_position_size": 50,
        },
        "short": {
            "rsi_threshold_overbought": 70,  # RSI > 70 = sell signal (NEW)
            "volume_ratio_min": 1.5,
            "min_buying_power": 150,
            "min_position_size": 50,
        },
        "shared": {
            "max_open_positions": 4,  # 2 longs + 2 shorts
            "max_total_notional_pct": 0.50,
            "max_spread_pct": 0.001,
            "require_live_data": True,
            "data_staleness_max_sec": 120,
        }
    },

    # Exit: Mean reversion with MINIMUM 2% profit targets
    "exit": {
        "min_profit_target_pct": 0.02,  # MUST profit at least 2% or exit at loss
        "profit_tiers_pct": [0.02, 0.03, 0.05],  # 2%, 3%, 5% targets
        "tier_exit_pct": [0.33, 0.33, 0.34],  # Exit 1/3 at each level
        "stop_loss_pct": 0.005,  # 0.5% max loss
        "aggressive_loss_pct": 0.01,  # 1% loss = full exit
        "max_hold_time_sec": 1800,  # 30 minutes
        "no_movement_timeout_sec": 900,  # 15 min with no change = exit
        "no_fill_timeout_sec": 15,
        "spread_expansion_max_pct": 0.0015,
        "mean_reversion_exit": True,  # Exit when price hits 50% retracement
        "risk_reward_ratio_min": 1.0,  # Risk $1 to make $2 minimum
    },

    # Capital: Separate account
    "capital": {
        "max_per_position": 240,
        "max_open_positions": 2,
        "max_total_notional_pct": 0.50,
        "critical_cash_balance": 100,
        "max_daily_loss_pct": 0.05,  # 5% of session
        "fee_assumption": 0.001,  # 0.1% per trade (maker/taker)
    },

    # Kill Conditions
    "kill_conditions": [
        "daily_loss_limit_hit",
        "cash_below_critical",
        "volume_dried_up",
        "spread_permanently_wide",
        "api_rate_limited",
        "database_error",
        "market_data_stale_2min",
        "order_rejection_3plus",
    ],

    # Success Metrics
    "kpi": {
        "primary": "Net P&L + Expectancy",
        "win_rate_target": 0.50,  # Crypto is noisy
        "expectancy_target": 5,  # $ per trade
        "max_avg_hold_minutes": 20,
        "trades_per_day_target": 3,
        "max_loss_per_trade_pct": 0.02,  # Never lose > 2% on 1 trade
    },

    "active": True,
}

# ============================================================================
# ALPACA STOCK/ETF BOT MANDATE
# ============================================================================

ALPACA_MANDATE = {
    "role": BotRole.ALPACA_GROWTH,
    "name": "alpaca_bot",
    "description": "Dual-direction mean reversion: long on oversold (RSI<30), short on overbought (RSI>70)",

    # Universe
    "universe": {
        "approved": ["SPY", "QQQ", "IWM", "VTI", "VOO", "VGT", "XLK", "XLE", "XLF"],
        "excluded": ["TSLA", "GME"],  # Too volatile for small positions
        "restrictions": {
            "min_spread_pct": 0.0005,  # 0.05% max
            "min_volume_shares": 1000000,
        }
    },

    # Entry: DUAL DIRECTION - Long on oversold, Short on overbought
    "entry": {
        "long": {
            "rsi_threshold_oversold": 30,  # RSI < 30 = buy signal
            "volume_ratio_min": 1.5,
            "min_buying_power": 100,
            "min_position_size": 30,
        },
        "short": {
            "rsi_threshold_overbought": 70,  # RSI > 70 = sell/short signal
            "volume_ratio_min": 1.5,
            "min_buying_power": 100,
            "min_position_size": 30,
        },
        "shared": {
            "max_open_positions": 6,  # 3 longs + 3 shorts
            "max_total_notional_pct": 0.60,
        }
    },

    # Exit: Mean reversion with MINIMUM 2% profit targets
    "exit": {
        "min_profit_target_pct": 0.02,  # MUST profit at least 2% or exit at loss
        "profit_tiers_pct": [0.02, 0.05, 0.10],  # 2%, 5%, 10% targets
        "tier_exit_pct": [0.33, 0.33, 0.34],  # Exit 1/3 at each level
        "stop_loss_pct": 0.015,  # 1.5% max loss (wider for stocks)
        "aggressive_loss_pct": 0.03,  # 3% loss = full exit
        "max_hold_time_sec": 7200,  # 2 hours
        "mean_reversion_exit": True,  # Exit when price hits 50% retracement
        "risk_reward_ratio_min": 1.0,  # Risk $1 to make $2 minimum
    },

    # Capital
    "capital": {
        "max_per_position": 200,
        "max_open_positions": 6,  # 3 longs + 3 shorts (matches entry)
        "max_total_notional_pct": 0.60,
        "critical_cash_balance": 50,
        "max_daily_loss_pct": 0.03,  # 3%
    },

    # Kill Conditions
    "kill_conditions": [
        "daily_loss_limit_hit",
        "cash_below_critical",
        "market_closed",
        "api_down",
    ],

    # Success Metrics
    "kpi": {
        "primary": "Capital Turnover + Net P&L",
        "win_rate_target": 0.50,
        "trades_per_day_target": 5,
        "expectancy_target": 8,
        "avg_hold_minutes": 45,
    },

    "active": False,  # Not yet deployed
}

# ============================================================================
# MONITORING BOT (NON-TRADING)
# ============================================================================

MONITORING_MANDATE = {
    "role": BotRole.MONITORING,
    "name": "analytics_bot",
    "description": "Independent oversight - read-only monitoring",

    "restrictions": {
        "no_orders": True,
        "no_parameter_changes": True,
        "read_only": True,
    },

    "monitors": [
        "closed_trades",
        "open_positions",
        "pnl",
        "win_rate",
        "expectancy",
        "capital_deployment",
        "risk_exposure",
        "api_health",
        "database_health",
        "data_freshness",
    ],

    "alert_conditions": [
        "no_trades_2hours",
        "daily_loss_exceeds_5pct",
        "api_error_rate_5pct",
        "database_query_timeout",
        "position_count_mismatch",
        "buying_power_discrepancy",
        "stale_market_data",
        "mandate_violation",
    ],

    "active": True,
}

# ============================================================================
# UNIFIED MANDATE MAP
# ============================================================================

ALL_MANDATES = {
    "apex": APEX_MANDATE,
    "crypto": CRYPTO_MANDATE,
    "alpaca": ALPACA_MANDATE,
    "monitoring": MONITORING_MANDATE,
}

# ============================================================================
# MANDATE ENFORCEMENT HELPERS
# ============================================================================

def get_bot_mandate(bot_name: str) -> Dict:
    """Retrieve mandate for a specific bot"""
    mandates_map = {
        "prop_bot": APEX_MANDATE,
        "crypto_coinbase_bot": CRYPTO_MANDATE,
        "alpaca_bot": ALPACA_MANDATE,
        "analytics": MONITORING_MANDATE,
    }
    return mandates_map.get(bot_name, {})

def validate_entry(bot_name: str, symbol: str, rsi: float, volume_ratio: float,
                   buying_power: float, open_positions: int, total_notional: float,
                   equity: float, direction: str = "long") -> tuple[bool, str]:
    """Validate entry against bot's mandate (supports dual-direction)

    direction: "long" (RSI < threshold) or "short" (RSI > threshold)
    """
    mandate = get_bot_mandate(bot_name)
    if not mandate:
        return False, f"Unknown bot: {bot_name}"

    # Check universe
    if "universe" in mandate:
        # NOTE: "commodities" was missing from this list until it was
        # caught here - MANDATE CHECK 1 in prop_bot.py already allowed
        # MGC/MCL/SIL (gold/oil/silver) through via its own separate
        # futures+crypto+commodities concat, but this independent check
        # only ever recognized futures/crypto/approved/approved_pairs, so
        # every commodity entry was silently rejected right here with
        # "not in prop_bot's approved universe" - real trades never placed.
        approved = (mandate["universe"].get("futures", []) +
                   mandate["universe"].get("crypto", []) +
                   mandate["universe"].get("commodities", []) +
                   mandate["universe"].get("inverse_etfs", []) +
                   mandate["universe"].get("approved", []) +
                   mandate["universe"].get("approved_pairs", []))
        if approved and symbol not in approved:
            return False, f"{symbol} not in {bot_name}'s approved universe"

    # Check entry conditions
    entry = mandate.get("entry", {})

    # Handle dual-direction (crypto bot)
    if isinstance(entry, dict) and "long" in entry and "short" in entry:
        if direction == "long":
            entry_rules = entry.get("long", {})
            rsi_threshold = entry_rules.get("rsi_threshold_oversold", 35)
            if rsi > rsi_threshold:
                return False, f"RSI {rsi:.1f} not oversold for LONG (threshold: <{rsi_threshold})"
        elif direction == "short":
            entry_rules = entry.get("short", {})
            rsi_threshold = entry_rules.get("rsi_threshold_overbought", 70)
            if rsi < rsi_threshold:
                return False, f"RSI {rsi:.1f} not overbought for SHORT (threshold: >{rsi_threshold})"

        shared = entry.get("shared", {})
        min_bp = shared.get("min_buying_power", 150)
        max_pos = shared.get("max_open_positions", 4)
        max_notional_pct = shared.get("max_total_notional_pct", 0.50)
    else:
        # Single-direction entry
        rsi_threshold = entry.get("rsi_threshold", 30)
        if rsi > rsi_threshold:
            return False, f"RSI {rsi:.1f} not oversold (threshold: {rsi_threshold})"
        min_bp = entry.get("min_buying_power", 150)
        max_pos = entry.get("max_open_positions", 2)
        max_notional_pct = entry.get("max_total_notional_pct", 0.50)

    if buying_power < min_bp:
        return False, f"Buying power ${buying_power:.0f} < minimum ${min_bp}"

    if open_positions >= max_pos:
        return False, f"Already at max {max_pos} positions"

    max_notional = equity * max_notional_pct
    if total_notional >= max_notional:
        return False, f"Notional ${total_notional:.0f} would exceed {max_notional_pct*100:.0f}% of equity"

    return True, "OK"

def check_kill_condition(bot_name: str, condition: str) -> bool:
    """Check if a kill condition has been triggered"""
    mandate = get_bot_mandate(bot_name)
    kill_conditions = mandate.get("kill_conditions", [])
    return condition in kill_conditions

def validate_exit_target(bot_name: str, entry_price: float, current_price: float,
                         stop_price: float, direction: str = "long") -> tuple[bool, str]:
    """Enforce minimum profit targets and risk/reward ratios

    Returns: (should_exit, reason)
    - For longs: profit = (current_price - entry_price) / entry_price
    - For shorts: profit = (entry_price - current_price) / entry_price
    """
    mandate = get_bot_mandate(bot_name)
    exit_rules = mandate.get("exit", {})

    # Calculate P&L
    if direction == "long":
        current_pnl_pct = (current_price - entry_price) / entry_price
        risk_pnl_pct = (stop_price - entry_price) / entry_price
    else:  # short
        current_pnl_pct = (entry_price - current_price) / entry_price
        risk_pnl_pct = (entry_price - stop_price) / entry_price

    # Check minimum profit target (2% minimum)
    min_profit = exit_rules.get("min_profit_target_pct", 0.02)
    if current_pnl_pct >= min_profit:
        return True, f"Hit minimum profit target {min_profit*100:.1f}% (current: {current_pnl_pct*100:.2f}%)"

    # Check stop loss
    stop_loss_pct = abs(exit_rules.get("stop_loss_pct", 0.005))
    if current_pnl_pct <= -stop_loss_pct:
        return True, f"Hit stop loss at {stop_loss_pct*100:.1f}% loss"

    # Check risk/reward ratio is acceptable (risk $1 to make $2)
    risk_reward_ratio = exit_rules.get("risk_reward_ratio_min", 1.0)
    if risk_pnl_pct != 0:
        actual_ratio = abs(current_pnl_pct / risk_pnl_pct)
        if actual_ratio < risk_reward_ratio:
            # Risk/reward is unfavorable
            if current_pnl_pct < 0:  # We're in a loss
                return True, f"Unfavorable risk/reward: {actual_ratio:.2f}:1 (need >{risk_reward_ratio}:1)"

    return False, f"Hold: P&L {current_pnl_pct*100:.2f}% (target {min_profit*100:.1f}%)"

# ============================================================================
# DASHBOARD TEMPLATE
# ============================================================================

DASHBOARD_TEMPLATE = """
BOT STATUS: {bot_name} ({role})
───────────────────────────────────────────
Status: {status} | Days active: {days}

CAPITAL
  Deployed: ${deployed:.2f} ({deployed_pct:.1f}%)
  Available: ${available:.2f}
  Max allowed: ${max_allowed:.2f}

PERFORMANCE TODAY
  Trades: {trades_count} ({wins} wins, {losses} losses)
  Win rate: {win_rate:.1f}%
  Avg winner: ${avg_winner:.2f}
  Avg loser: ${avg_loser:.2f}
  Expectancy: ${expectancy:.2f} per trade
  Gross P&L: ${gross_pnl:.2f}
  Fees: ${fees:.2f}
  Net P&L: ${net_pnl:.2f}

COMPLIANCE
  Mandate compliance: {mandate_compliance:.0f}%
  Daily loss limit: ${daily_loss:.2f} / ${daily_limit:.2f}
  Risk level: {risk_level} | Status: {mandate_status}

SIGNALS
  Last signal: {signal_age_min} min ago
  Last trade: {trade_age_min} min ago
  Data freshness: {data_freshness}
"""

if __name__ == "__main__":
    print("Bot Mandates Loaded")
    print(f"Active bots: {len([m for m in ALL_MANDATES.values() if m.get('active', False)])}")
    for bot_key, mandate in ALL_MANDATES.items():
        if mandate.get("active"):
            print(f"  ✓ {bot_key}: {mandate['description']}")


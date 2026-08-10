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
        "commodities": ["GLD", "USO", "SLV"],
        "restriction": "No other symbols allowed"
    },

    # Entry: When it can open a position
    "entry": {
        "rsi_threshold": 30,  # RSI < 30 = oversold
        "trend_required": True,  # Must confirm with SMA5/SMA10
        "min_buying_power": 150,
        "min_position_size": 50,
        "max_open_positions": 2,
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
        "max_open_positions": 2,
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
    "description": "Short-term crypto opportunity capture with tight entry/exit",

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

    # Entry: Tighter than APEX (crypto is volatile)
    "entry": {
        "rsi_threshold": 35,  # Oversold
        "volume_ratio_min": 1.5,  # Higher than average
        "min_buying_power": 150,
        "min_position_size": 50,
        "max_open_positions": 2,
        "max_total_notional_pct": 0.50,
        "max_spread_pct": 0.001,  # Tight spread
        "require_live_data": True,
        "data_staleness_max_sec": 120,
    },

    # Exit: Faster exits, tighter targets
    "exit": {
        "profit_target_pct": [0.01, 0.02, 0.03],  # 1-3% targets
        "profit_scale": [0.50, 0.50],  # Exit 50% at 1%, 50% at 3%
        "stop_loss_pct": 0.005,  # 0.5% (tighter for crypto)
        "aggressive_loss_pct": 0.01,  # 1% loss = full exit
        "max_hold_time_sec": 1800,  # 30 minutes
        "no_movement_timeout_sec": 900,  # 15 min with no change
        "no_fill_timeout_sec": 15,
        "spread_expansion_max_pct": 0.0015,
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
    "description": "Short-term stock/ETF trading growth with capital availability",

    # Universe
    "universe": {
        "approved": ["SPY", "QQQ", "IWM", "VTI", "VOO", "VGT", "XLK", "XLE", "XLF"],
        "excluded": ["TSLA", "GME"],  # Too volatile for small positions
        "restrictions": {
            "min_spread_pct": 0.0005,  # 0.05% max
            "min_volume_shares": 1000000,
        }
    },

    # Entry
    "entry": {
        "rsi_threshold_long": [25, 35],  # Oversold for longs
        "rsi_threshold_short": [65, 75],  # Overbought for shorts
        "volume_ratio_min": 1.5,
        "min_buying_power": 100,
        "min_position_size": 30,
        "max_open_positions": 3,
        "max_total_notional_pct": 0.60,
    },

    # Exit
    "exit": {
        "profit_target_pct": [0.02, 0.05],  # 2-5%
        "stop_loss_pct": 0.015,  # 1.5%
        "max_hold_time_sec": 7200,  # 2 hours
    },

    # Capital
    "capital": {
        "max_per_position": 200,
        "max_open_positions": 3,
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
                   equity: float) -> tuple[bool, str]:
    """Validate entry against bot's mandate"""
    mandate = get_bot_mandate(bot_name)
    if not mandate:
        return False, f"Unknown bot: {bot_name}"

    # Check universe
    if "universe" in mandate:
        approved = (mandate["universe"].get("futures", []) +
                   mandate["universe"].get("crypto", []) +
                   mandate["universe"].get("approved", []))
        if approved and symbol not in approved:
            return False, f"{symbol} not in {bot_name}'s approved universe"

    # Check entry conditions
    entry = mandate.get("entry", {})

    if rsi > entry.get("rsi_threshold", 30):
        return False, f"RSI {rsi:.1f} not oversold (threshold: {entry.get('rsi_threshold')})"

    if buying_power < entry.get("min_buying_power", 150):
        return False, f"Buying power ${buying_power:.0f} < minimum ${entry.get('min_buying_power')}"

    if open_positions >= entry.get("max_open_positions", 2):
        return False, f"Already at max {entry.get('max_open_positions')} positions"

    max_notional = equity * entry.get("max_total_notional_pct", 0.50)
    if total_notional >= max_notional:
        return False, f"Notional ${total_notional:.0f} would exceed {entry.get('max_total_notional_pct')*100:.0f}% of equity"

    return True, "OK"

def check_kill_condition(bot_name: str, condition: str) -> bool:
    """Check if a kill condition has been triggered"""
    mandate = get_bot_mandate(bot_name)
    kill_conditions = mandate.get("kill_conditions", [])
    return condition in kill_conditions

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


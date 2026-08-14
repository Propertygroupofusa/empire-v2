"""
MEASUREMENT SYSTEM - Quantitative Research Pipeline
====================================================
Logs every trade with full context, calculates statistical evidence,
and determines which strategies have real edges.

Trade → Signal Context → Prediction → Execution → P&L → Evidence
"""

import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, Dict, List, Any
from dataclasses import dataclass, asdict
from enum import Enum
import math

ET = ZoneInfo("America/New_York")
log = logging.getLogger("measurement_system")

# ============================================================================
# ENUMS & DATA MODELS
# ============================================================================

class StrategyState(Enum):
    """Lifecycle state of a strategy"""
    HYPOTHESIS = "hypothesis"           # Idea stage
    BACKTEST_PASS = "backtest_pass"     # Survives historical data
    OOS_PASS = "oos_pass"               # Survives out-of-sample
    PAPER_ACTIVE = "paper_active"       # Live paper testing
    LIVE_ACTIVE = "live_active"         # Real capital deployed
    LIVE_MONITORING = "live_monitoring" # Continuous measurement
    EDGE_DEGRADING = "edge_degrading"   # Evidence weakening
    REJECTED = "rejected"               # Not statistically significant
    RETIRED = "retired"                 # Edge lost, capital reallocated

@dataclass
class SignalContext:
    """Market conditions when signal fires"""
    symbol: str
    rsi: Optional[float] = None         # Momentum
    iv_percentile: Optional[float] = None  # Volatility regime
    price: Optional[float] = None       # Price level
    trend: Optional[str] = None         # Uptrend/downtrend/neutral
    regime: Optional[str] = None        # Market regime (bull/bear/range)
    custom_data: Optional[Dict] = None  # Strategy-specific context

    def to_dict(self):
        return asdict(self)

@dataclass
class TradeLog:
    """Complete record of a single trade execution"""
    trade_id: str
    strategy: str                       # e.g., "blitzkrieg", "long_call", "crypto_rsi"
    symbol: str
    entry_time: datetime
    entry_price: float
    entry_quantity: float

    # Signal that triggered entry
    signal_context: SignalContext
    expected_value: Optional[float] = None  # Model's prediction of P&L

    # Exit details
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None   # "profit_target", "stop_loss", "timeout"

    # Execution quality
    entry_slippage: Optional[float] = None   # (actual - predicted) entry price
    exit_slippage: Optional[float] = None    # (actual - predicted) exit price

    # Results
    actual_pnl: Optional[float] = None
    actual_pnl_pct: Optional[float] = None

    # Metadata
    mode: str = "live"                  # "paper" or "live"
    created_at: datetime = None

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now(ET)

    def to_dict(self):
        return {
            'trade_id': self.trade_id,
            'strategy': self.strategy,
            'symbol': self.symbol,
            'entry_time': self.entry_time.isoformat() if self.entry_time else None,
            'entry_price': self.entry_price,
            'entry_quantity': self.entry_quantity,
            'signal_context': self.signal_context.to_dict(),
            'expected_value': self.expected_value,
            'exit_time': self.exit_time.isoformat() if self.exit_time else None,
            'exit_price': self.exit_price,
            'exit_reason': self.exit_reason,
            'entry_slippage': self.entry_slippage,
            'exit_slippage': self.exit_slippage,
            'actual_pnl': self.actual_pnl,
            'actual_pnl_pct': self.actual_pnl_pct,
            'mode': self.mode,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }

# ============================================================================
# STATISTICAL ANALYZER
# ============================================================================

class StatisticalAnalyzer:
    """Calculate evidence metrics for strategy performance"""

    @staticmethod
    def win_rate(trades: List[TradeLog]) -> float:
        """Percentage of profitable trades"""
        if not trades or len(trades) == 0:
            return 0.0

        winners = len([t for t in trades if t.actual_pnl and t.actual_pnl > 0])
        return (winners / len(trades)) * 100

    @staticmethod
    def profit_factor(trades: List[TradeLog]) -> float:
        """Gross profit / Gross loss (>1.5 is good)"""
        if not trades:
            return 0.0

        gross_profit = sum(t.actual_pnl for t in trades if t.actual_pnl and t.actual_pnl > 0)
        gross_loss = abs(sum(t.actual_pnl for t in trades if t.actual_pnl and t.actual_pnl < 0))

        if gross_loss == 0:
            return float('inf') if gross_profit > 0 else 0.0

        return gross_profit / gross_loss

    @staticmethod
    def expectancy(trades: List[TradeLog]) -> float:
        """Average P&L per trade (expected value)"""
        if not trades:
            return 0.0

        returns = [t.actual_pnl for t in trades if t.actual_pnl is not None]
        return sum(returns) / len(returns) if returns else 0.0

    @staticmethod
    def sharpe_ratio(trades: List[TradeLog], risk_free_rate: float = 0.02) -> float:
        """Risk-adjusted return (annualized)"""
        if not trades or len(trades) < 2:
            return 0.0

        returns = [t.actual_pnl_pct / 100 if t.actual_pnl_pct else 0 for t in trades]

        mean_return = sum(returns) / len(returns)
        variance = sum((r - mean_return) ** 2 for r in returns) / len(returns)
        std_dev = math.sqrt(variance)

        if std_dev == 0:
            return 0.0

        # Annualize (assuming ~252 trading days)
        daily_sharpe = (mean_return - (risk_free_rate / 252)) / std_dev
        return daily_sharpe * math.sqrt(252)

    @staticmethod
    def sortino_ratio(trades: List[TradeLog], risk_free_rate: float = 0.02) -> float:
        """Risk-adjusted return, penalizes downside only"""
        if not trades or len(trades) < 2:
            return 0.0

        returns = [t.actual_pnl_pct / 100 if t.actual_pnl_pct else 0 for t in trades]

        mean_return = sum(returns) / len(returns)

        # Downside deviation: only negative returns
        downside_returns = [r - 0 for r in returns if r < 0]
        if not downside_returns:
            downside_dev = 0
        else:
            downside_var = sum(r ** 2 for r in downside_returns) / len(returns)
            downside_dev = math.sqrt(downside_var)

        if downside_dev == 0:
            return 0.0

        # Annualize
        daily_sortino = (mean_return - (risk_free_rate / 252)) / downside_dev
        return daily_sortino * math.sqrt(252)

    @staticmethod
    def max_drawdown(trades: List[TradeLog]) -> float:
        """Largest peak-to-trough decline"""
        if not trades:
            return 0.0

        cumulative_pnl = 0
        peak = 0
        max_dd = 0

        for trade in trades:
            if trade.actual_pnl:
                cumulative_pnl += trade.actual_pnl
                peak = max(peak, cumulative_pnl)
                drawdown = peak - cumulative_pnl
                max_dd = max(max_dd, drawdown)

        return max_dd

    @staticmethod
    def consecutive_losses(trades: List[TradeLog]) -> int:
        """Max consecutive losing trades"""
        if not trades:
            return 0

        max_consecutive = 0
        current_consecutive = 0

        for trade in trades:
            if trade.actual_pnl and trade.actual_pnl < 0:
                current_consecutive += 1
                max_consecutive = max(max_consecutive, current_consecutive)
            else:
                current_consecutive = 0

        return max_consecutive

    @staticmethod
    def p_value(trades: List[TradeLog], null_hypothesis_wr: float = 0.5) -> float:
        """
        Probability that win rate is due to chance (not real edge).
        p < 0.05 = statistically significant edge
        """
        if len(trades) < 10:
            return 1.0  # Not enough data

        actual_wr = StatisticalAnalyzer.win_rate(trades) / 100
        n = len(trades)

        # Binomial test approximation
        # How likely is observed wins vs chance?
        expected_wins = n * null_hypothesis_wr
        actual_wins = len([t for t in trades if t.actual_pnl and t.actual_pnl > 0])

        # Simple z-score approximation
        std_error = math.sqrt(n * null_hypothesis_wr * (1 - null_hypothesis_wr))
        if std_error == 0:
            return 1.0

        z_score = (actual_wins - expected_wins) / std_error

        # Rough p-value from z-score (one-tailed)
        # This is approximate; use scipy.stats for exact calculation
        from math import erfc
        p = 0.5 * erfc(z_score / math.sqrt(2))

        return min(max(p, 0.0), 1.0)

    @staticmethod
    def kelly_fraction(trades: List[TradeLog]) -> float:
        """
        Optimal position sizing: Kelly % = (Win% * AvgWin - Loss% * AvgLoss) / AvgWin
        Result is fraction of capital to risk per trade
        Typical safe usage: Kelly / 4 (quarter-kelly)
        """
        if len(trades) < 10:
            return 0.0

        wr = StatisticalAnalyzer.win_rate(trades) / 100

        winners = [t for t in trades if t.actual_pnl and t.actual_pnl > 0]
        losers = [t for t in trades if t.actual_pnl and t.actual_pnl < 0]

        if not winners or not losers:
            return 0.0

        avg_win = sum(t.actual_pnl for t in winners) / len(winners)
        avg_loss = abs(sum(t.actual_pnl for t in losers) / len(losers))

        if avg_win == 0:
            return 0.0

        kelly = (wr * avg_win - (1 - wr) * avg_loss) / avg_win

        # Clamp to reasonable range
        return max(min(kelly, 0.25), 0.0)  # Max 25% (quarter-kelly is safer)

    @staticmethod
    def calculate_summary(trades: List[TradeLog]) -> Dict[str, Any]:
        """Calculate all metrics at once"""
        return {
            'trade_count': len(trades),
            'win_rate': round(StatisticalAnalyzer.win_rate(trades), 2),
            'profit_factor': round(StatisticalAnalyzer.profit_factor(trades), 2),
            'expectancy': round(StatisticalAnalyzer.expectancy(trades), 2),
            'sharpe_ratio': round(StatisticalAnalyzer.sharpe_ratio(trades), 2),
            'sortino_ratio': round(StatisticalAnalyzer.sortino_ratio(trades), 2),
            'max_drawdown': round(StatisticalAnalyzer.max_drawdown(trades), 2),
            'consecutive_losses': StatisticalAnalyzer.consecutive_losses(trades),
            'p_value': round(StatisticalAnalyzer.p_value(trades), 4),
            'kelly_fraction': round(StatisticalAnalyzer.kelly_fraction(trades), 4),
        }

# ============================================================================
# TRADE LOGGER
# ============================================================================

class TradeLogger:
    """Log trades to file/database"""

    def __init__(self, filepath: str = "/tmp/trade_logs.jsonl"):
        self.filepath = filepath
        self.trades: List[TradeLog] = []

    def record_entry(self, trade_id: str, strategy: str, symbol: str,
                     entry_price: float, entry_quantity: float,
                     signal_context: SignalContext,
                     expected_value: Optional[float] = None,
                     mode: str = "live") -> TradeLog:
        """Record trade entry"""
        trade = TradeLog(
            trade_id=trade_id,
            strategy=strategy,
            symbol=symbol,
            entry_time=datetime.now(ET),
            entry_price=entry_price,
            entry_quantity=entry_quantity,
            signal_context=signal_context,
            expected_value=expected_value,
            mode=mode,
        )

        self.trades.append(trade)
        log.info(f"📍 Entry logged: {strategy} {symbol} @ ${entry_price} qty={entry_quantity}")

        return trade

    def record_exit(self, trade_id: str, exit_price: float, exit_reason: str,
                    entry_slippage: Optional[float] = None,
                    exit_slippage: Optional[float] = None):
        """Record trade exit and calculate P&L"""
        trade = next((t for t in self.trades if t.trade_id == trade_id), None)

        if not trade:
            log.warning(f"⚠️  Trade {trade_id} not found")
            return

        trade.exit_time = datetime.now(ET)
        trade.exit_price = exit_price
        trade.exit_reason = exit_reason
        trade.entry_slippage = entry_slippage
        trade.exit_slippage = exit_slippage

        # Calculate P&L
        pnl = (exit_price - trade.entry_price) * trade.entry_quantity
        pnl_pct = ((exit_price - trade.entry_price) / trade.entry_price * 100) if trade.entry_price > 0 else 0

        trade.actual_pnl = pnl
        trade.actual_pnl_pct = pnl_pct

        # Log to file
        self._append_to_log(trade)

        # Compare to expected value
        ev_error = None
        if trade.expected_value is not None:
            ev_error = pnl - trade.expected_value

        log.info(f"✅ Exit: {trade.strategy} {trade.symbol} | P&L: ${pnl:.2f} ({pnl_pct:.2f}%) | {exit_reason}")
        if ev_error is not None:
            log.info(f"   Expected value error: ${ev_error:.2f}")

    def _append_to_log(self, trade: TradeLog):
        """Append trade to JSONL file"""
        try:
            with open(self.filepath, 'a') as f:
                f.write(json.dumps(trade.to_dict()) + '\n')
        except Exception as e:
            log.error(f"Failed to write trade log: {e}")

    def get_strategy_trades(self, strategy: str) -> List[TradeLog]:
        """Get all trades for a specific strategy"""
        return [t for t in self.trades if t.strategy == strategy and t.actual_pnl is not None]

    def get_strategy_summary(self, strategy: str) -> Dict[str, Any]:
        """Get statistical summary for a strategy"""
        trades = self.get_strategy_trades(strategy)
        summary = StatisticalAnalyzer.calculate_summary(trades)
        summary['strategy'] = strategy
        summary['last_update'] = datetime.now(ET).isoformat()
        return summary

# Global logger instance
trade_logger = TradeLogger()

# ============================================================================
# USAGE EXAMPLE
# ============================================================================

"""
# Record a trade entry
signal = SignalContext(
    symbol="BTC",
    rsi=28,
    iv_percentile=15,
    price=42500,
    trend="uptrend",
    regime="bull"
)

trade = trade_logger.record_entry(
    trade_id="TRADE_001",
    strategy="blitzkrieg",
    symbol="BTC",
    entry_price=42500,
    entry_quantity=0.05,
    signal_context=signal,
    expected_value=125.00,  # Model expects $125 profit
    mode="live"
)

# ... later, when position exits ...

trade_logger.record_exit(
    trade_id="TRADE_001",
    exit_price=42925,
    exit_reason="profit_target_1",
    entry_slippage=-0.50,  # Slightly better than expected
    exit_slippage=0.00
)

# Get stats for strategy
summary = trade_logger.get_strategy_summary("blitzkrieg")
print(summary)
# {
#     'strategy': 'blitzkrieg',
#     'trade_count': 42,
#     'win_rate': 58.33,
#     'sharpe_ratio': 0.85,
#     'p_value': 0.032,  # Statistically significant!
#     'kelly_fraction': 0.08,
#     ...
# }
"""

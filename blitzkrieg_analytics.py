"""
Blitzkrieg Strategy Analytics & Performance Tracking
=====================================================
Tracks paper vs live trading performance with detailed metrics,
comparison matrices, and real-time dashboards.
"""

import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field, asdict
from enum import Enum

ET = ZoneInfo("America/New_York")
log = logging.getLogger("blitzkrieg_analytics")

# ============================================================================
# DATA MODELS
# ============================================================================

class TradingMode(Enum):
    """Paper vs Live trading modes"""
    PAPER = "paper"
    LIVE = "live"

@dataclass
class TradeMetrics:
    """Single trade performance"""
    trade_id: str
    mode: TradingMode
    symbol: str
    entry_time: datetime
    entry_price: float
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    quantity: float = 0.0
    position_value: float = 0.0  # entry_price × quantity
    pnl_dollars: float = 0.0
    pnl_pct: float = 0.0
    holding_minutes: int = 0
    exit_reason: str = ""  # "profit_target_1", "profit_target_2", "profit_target_3", "hard_stop"
    profit_target_hit: Optional[float] = None  # 0.01, 0.02, 0.03

    def to_dict(self):
        return {k: v.isoformat() if isinstance(v, datetime) else (v.value if isinstance(v, Enum) else v)
                for k, v in asdict(self).items()}

@dataclass
class DailyMetrics:
    """Daily trading statistics"""
    date: str
    mode: TradingMode
    trades_total: int = 0
    trades_winning: int = 0
    trades_losing: int = 0
    trades_breakeven: int = 0
    profit_target_1_hits: int = 0  # Exits at 1%
    profit_target_2_hits: int = 0  # Exits at 2%
    profit_target_3_hits: int = 0  # Exits at 3%
    hard_stops: int = 0
    pnl_dollars: float = 0.0
    pnl_pct: float = 0.0
    avg_holding_minutes: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    win_rate: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    total_position_value: float = 0.0

    def to_dict(self):
        return {k: v.value if isinstance(v, Enum) else v for k, v in asdict(self).items()}

@dataclass
class PerformanceComparison:
    """Paper vs Live side-by-side comparison"""
    metric_name: str
    paper_value: Any
    live_value: Any
    paper_unit: str = ""
    live_unit: str = ""
    delta: Optional[float] = None  # Difference
    improvement_pct: Optional[float] = None  # Percentage improvement

    def to_dict(self):
        return asdict(self)

# ============================================================================
# ANALYTICS TRACKER
# ============================================================================

class BlitzkriegAnalytics:
    """Track and analyze blitzkrieg strategy performance (paper vs live)"""

    def __init__(self):
        self.trades_paper: List[TradeMetrics] = []
        self.trades_live: List[TradeMetrics] = []
        self.daily_stats_paper: Dict[str, DailyMetrics] = {}
        self.daily_stats_live: Dict[str, DailyMetrics] = {}

    # ========================================================================
    # RECORD TRADES
    # ========================================================================

    def record_entry(
        self,
        trade_id: str,
        mode: TradingMode,
        symbol: str,
        entry_price: float,
        quantity: float,
    ) -> TradeMetrics:
        """Record trade entry"""
        trade = TradeMetrics(
            trade_id=trade_id,
            mode=mode,
            symbol=symbol,
            entry_time=datetime.now(ET),
            entry_price=entry_price,
            quantity=quantity,
            position_value=entry_price * quantity,
        )

        if mode == TradingMode.PAPER:
            self.trades_paper.append(trade)
        else:
            self.trades_live.append(trade)

        log.info(f"📍 Entry {mode.value}: {symbol} @ ${entry_price} × {quantity} = ${trade.position_value:.2f}")
        return trade

    def record_exit(
        self,
        trade_id: str,
        mode: TradingMode,
        exit_price: float,
        exit_reason: str,
        profit_target_hit: Optional[float] = None,
    ) -> Optional[TradeMetrics]:
        """Record trade exit and calculate P&L"""
        trades = self.trades_paper if mode == TradingMode.PAPER else self.trades_live
        trade = next((t for t in trades if t.trade_id == trade_id), None)

        if not trade:
            log.warning(f"⚠️  Trade {trade_id} not found for exit")
            return None

        trade.exit_time = datetime.now(ET)
        trade.exit_price = exit_price
        trade.exit_reason = exit_reason
        trade.profit_target_hit = profit_target_hit

        # Calculate P&L
        pnl = (exit_price - trade.entry_price) * trade.quantity
        pnl_pct = (exit_price - trade.entry_price) / trade.entry_price * 100 if trade.entry_price > 0 else 0

        trade.pnl_dollars = pnl
        trade.pnl_pct = pnl_pct

        # Calculate holding time
        holding_time = (trade.exit_time - trade.entry_time).total_seconds() / 60
        trade.holding_minutes = int(holding_time)

        result = "✅ WIN" if pnl > 0 else ("⏹️  LOSS" if pnl < 0 else "➖ BREAKEVEN")
        log.info(f"{result} {mode.value}: {trade.symbol} | P&L: ${pnl:.2f} ({pnl_pct:.2f}%) | "
                f"Hold: {trade.holding_minutes}m | Reason: {exit_reason}")

        return trade

    # ========================================================================
    # DAILY STATISTICS
    # ========================================================================

    def calculate_daily_stats(self, mode: TradingMode, date: Optional[str] = None) -> DailyMetrics:
        """Calculate daily statistics for given mode"""
        if date is None:
            date = datetime.now(ET).strftime("%Y-%m-%d")

        trades = self.trades_paper if mode == TradingMode.PAPER else self.trades_live

        # Filter trades for this date
        daily_trades = [
            t for t in trades
            if t.exit_time and t.exit_time.strftime("%Y-%m-%d") == date
        ]

        if not daily_trades:
            return DailyMetrics(date=date, mode=mode)

        metrics = DailyMetrics(date=date, mode=mode)
        metrics.trades_total = len(daily_trades)

        # Count outcomes
        winning = [t for t in daily_trades if t.pnl_dollars > 0]
        losing = [t for t in daily_trades if t.pnl_dollars < 0]
        breakeven = [t for t in daily_trades if t.pnl_dollars == 0]

        metrics.trades_winning = len(winning)
        metrics.trades_losing = len(losing)
        metrics.trades_breakeven = len(breakeven)

        # Count profit targets and stops
        metrics.profit_target_1_hits = len([t for t in daily_trades if t.profit_target_hit == 0.01])
        metrics.profit_target_2_hits = len([t for t in daily_trades if t.profit_target_hit == 0.02])
        metrics.profit_target_3_hits = len([t for t in daily_trades if t.profit_target_hit == 0.03])
        metrics.hard_stops = len([t for t in daily_trades if t.exit_reason == "hard_stop"])

        # Calculate P&L
        metrics.pnl_dollars = sum(t.pnl_dollars for t in daily_trades)
        total_position_value = sum(t.position_value for t in daily_trades)
        metrics.pnl_pct = (metrics.pnl_dollars / total_position_value * 100) if total_position_value > 0 else 0
        metrics.total_position_value = total_position_value

        # Average metrics
        metrics.avg_holding_minutes = sum(t.holding_minutes for t in daily_trades) / len(daily_trades) if daily_trades else 0
        metrics.avg_win = sum(t.pnl_dollars for t in winning) / len(winning) if winning else 0
        metrics.avg_loss = sum(t.pnl_dollars for t in losing) / len(losing) if losing else 0
        metrics.win_rate = len(winning) / len(daily_trades) * 100 if daily_trades else 0
        metrics.largest_win = max((t.pnl_dollars for t in daily_trades), default=0)
        metrics.largest_loss = min((t.pnl_dollars for t in daily_trades), default=0)

        return metrics

    # ========================================================================
    # COMPARISON MATRICES
    # ========================================================================

    def get_performance_matrix(self) -> Dict[str, Dict[str, Any]]:
        """Get paper vs live comparison matrix"""
        today = datetime.now(ET).strftime("%Y-%m-%d")

        paper_stats = self.calculate_daily_stats(TradingMode.PAPER, today)
        live_stats = self.calculate_daily_stats(TradingMode.LIVE, today)

        comparisons = [
            PerformanceComparison(
                metric_name="Trades Today",
                paper_value=paper_stats.trades_total,
                live_value=live_stats.trades_total,
            ),
            PerformanceComparison(
                metric_name="Winning Trades",
                paper_value=paper_stats.trades_winning,
                live_value=live_stats.trades_winning,
            ),
            PerformanceComparison(
                metric_name="Losing Trades",
                paper_value=paper_stats.trades_losing,
                live_value=live_stats.trades_losing,
            ),
            PerformanceComparison(
                metric_name="Win Rate",
                paper_value=round(paper_stats.win_rate, 2),
                live_value=round(live_stats.win_rate, 2),
                paper_unit="%",
                live_unit="%",
            ),
            PerformanceComparison(
                metric_name="Avg Win",
                paper_value=round(paper_stats.avg_win, 2),
                live_value=round(live_stats.avg_win, 2),
                paper_unit="$",
                live_unit="$",
            ),
            PerformanceComparison(
                metric_name="Avg Loss",
                paper_value=round(paper_stats.avg_loss, 2),
                live_value=round(live_stats.avg_loss, 2),
                paper_unit="$",
                live_unit="$",
            ),
            PerformanceComparison(
                metric_name="Avg Holding Time",
                paper_value=round(paper_stats.avg_holding_minutes, 1),
                live_value=round(live_stats.avg_holding_minutes, 1),
                paper_unit="min",
                live_unit="min",
            ),
            PerformanceComparison(
                metric_name="Daily P&L",
                paper_value=round(paper_stats.pnl_dollars, 2),
                live_value=round(live_stats.pnl_dollars, 2),
                paper_unit="$",
                live_unit="$",
            ),
            PerformanceComparison(
                metric_name="Daily Return %",
                paper_value=round(paper_stats.pnl_pct, 2),
                live_value=round(live_stats.pnl_pct, 2),
                paper_unit="%",
                live_unit="%",
            ),
            PerformanceComparison(
                metric_name="1% Target Hits",
                paper_value=paper_stats.profit_target_1_hits,
                live_value=live_stats.profit_target_1_hits,
            ),
            PerformanceComparison(
                metric_name="2% Target Hits",
                paper_value=paper_stats.profit_target_2_hits,
                live_value=live_stats.profit_target_2_hits,
            ),
            PerformanceComparison(
                metric_name="3% Target Hits",
                paper_value=paper_stats.profit_target_3_hits,
                live_value=live_stats.profit_target_3_hits,
            ),
            PerformanceComparison(
                metric_name="Hard Stops Triggered",
                paper_value=paper_stats.hard_stops,
                live_value=live_stats.hard_stops,
            ),
        ]

        return {
            "date": today,
            "paper": paper_stats.to_dict(),
            "live": live_stats.to_dict(),
            "comparisons": [c.to_dict() for c in comparisons],
        }

    def get_summary_report(self) -> Dict[str, Any]:
        """Get comprehensive summary report"""
        return {
            "timestamp": datetime.now(ET).isoformat(),
            "paper_trades": len(self.trades_paper),
            "live_trades": len(self.trades_live),
            "paper_pnl": sum(t.pnl_dollars for t in self.trades_paper),
            "live_pnl": sum(t.pnl_dollars for t in self.trades_live),
            "performance_matrix": self.get_performance_matrix(),
        }

    # ========================================================================
    # EXPORT & PERSISTENCE
    # ========================================================================

    def to_json(self, filepath: str):
        """Export analytics to JSON file"""
        data = {
            "timestamp": datetime.now(ET).isoformat(),
            "paper_trades": [t.to_dict() for t in self.trades_paper],
            "live_trades": [t.to_dict() for t in self.trades_live],
            "summary": self.get_summary_report(),
        }
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)
        log.info(f"📊 Analytics exported to {filepath}")

    def load_from_json(self, filepath: str):
        """Load analytics from JSON file"""
        try:
            with open(filepath, "r") as f:
                data = json.load(f)
            log.info(f"📊 Analytics loaded from {filepath}")
            return data
        except FileNotFoundError:
            log.warning(f"⚠️  Analytics file not found: {filepath}")
            return None

# Global analytics tracker
blitzkrieg_analytics = BlitzkriegAnalytics()

# ============================================================================
# USAGE EXAMPLE
# ============================================================================

"""
# Record paper trade entry
trade_id = "PAPER_001"
blitzkrieg_analytics.record_entry(trade_id, TradingMode.PAPER, "BTC", 42500.0, 0.05)

# ... some time later ...

# Record paper trade exit at 1% profit target
blitzkrieg_analytics.record_exit(trade_id, TradingMode.PAPER, 42925.0, "profit_target_1", 0.01)

# Get performance comparison
matrix = blitzkrieg_analytics.get_performance_matrix()
print(matrix)

# Export to JSON
blitzkrieg_analytics.to_json("/tmp/blitzkrieg_analytics.json")
"""

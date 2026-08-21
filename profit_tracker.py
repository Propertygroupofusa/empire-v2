"""
5-hour Rolling Profit Tracker
================================
Tracks and reports profits within rolling 5-hour windows.
Used by trading bots to show compounding gains in real-time.
"""

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dataclasses import dataclass
from typing import List

log = logging.getLogger("profit_tracker")

ET = ZoneInfo("America/New_York")


@dataclass
class ProfitRecord:
    """A single profit event (trade closed)"""
    timestamp: datetime
    amount: float  # Profit in dollars
    symbol: str
    side: str  # "long" or "short"
    trade_type: str  # "rsi_exit", "profit_target", "stop_loss", etc.


class FiveHourProfitTracker:
    """
    Tracks all profit events within a rolling 5-hour window.
    Automatically purges old records and provides rolling metrics.
    """

    def __init__(self):
        self.records: List[ProfitRecord] = []
        self.window_hours = 5
        self.total_all_time = 0.0

    def add_profit(self, amount: float, symbol: str, side: str, trade_type: str = "profit_lock"):
        """Record a profit event"""
        record = ProfitRecord(
            timestamp=datetime.now(ET),
            amount=amount,
            symbol=symbol,
            side=side,
            trade_type=trade_type
        )
        self.records.append(record)
        self.total_all_time += amount
        self._purge_old_records()

    def _purge_old_records(self):
        """Remove records older than 5 hours"""
        cutoff_time = datetime.now(ET) - timedelta(hours=self.window_hours)
        self.records = [r for r in self.records if r.timestamp > cutoff_time]

    def get_five_hour_total(self) -> float:
        """Get sum of all profits in the last 5 hours"""
        self._purge_old_records()
        return sum(r.amount for r in self.records)

    def get_five_hour_count(self) -> int:
        """Get count of profit events in last 5 hours"""
        self._purge_old_records()
        return len(self.records)

    def get_five_hour_avg(self) -> float:
        """Get average profit per event in last 5 hours"""
        count = self.get_five_hour_count()
        if count == 0:
            return 0.0
        return self.get_five_hour_total() / count

    def get_five_hour_summary(self) -> str:
        """Get formatted summary of 5-hour performance"""
        total = self.get_five_hour_total()
        count = self.get_five_hour_count()
        avg = self.get_five_hour_avg()

        return f"[5H] Total: ${total:.2f} | Trades: {count} | Avg: ${avg:.2f}"

    def get_symbol_breakdown(self) -> dict:
        """Get profit breakdown by symbol"""
        self._purge_old_records()
        breakdown = {}
        for record in self.records:
            if record.symbol not in breakdown:
                breakdown[record.symbol] = {"count": 0, "total": 0.0}
            breakdown[record.symbol]["count"] += 1
            breakdown[record.symbol]["total"] += record.amount
        return breakdown

    def log_summary(self, bot_name: str = "Bot"):
        """Log formatted 5-hour summary"""
        summary = self.get_five_hour_summary()
        breakdown = self.get_symbol_breakdown()

        log.info(f"📊 {bot_name} {summary} | All-time: ${self.total_all_time:.2f}")

        # Log per-symbol breakdown
        if breakdown:
            for symbol, stats in breakdown.items():
                log.info(
                    f"  └─ {symbol}: ${stats['total']:.2f} ({stats['count']} closes)"
                )

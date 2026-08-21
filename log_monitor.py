"""
Real-time log monitoring for bot activity and profit-taking events.
Captures PROFIT LOCK messages, trade alerts, and bot status changes.
"""

import asyncio
import json
from datetime import datetime, timedelta
from collections import deque
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, asdict
import logging

# Configure logging to capture bot messages
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("log_monitor")


@dataclass
class LogEvent:
    """Structured log event for bot activity"""
    timestamp: str
    level: str  # INFO, WARNING, CRITICAL
    source: str  # crypto_bot, alpaca_bot, dashboard
    message: str
    event_type: str  # profit_lock, trade_alert, status, error
    data: Optional[Dict[str, Any]] = None

    def to_dict(self):
        return asdict(self)


class LogMonitor:
    """Tracks bot activity, trade alerts, and profit-taking events"""

    def __init__(self, max_events: int = 200):
        self.events: deque = deque(maxlen=max_events)
        self.last_profit_lock: Optional[LogEvent] = None
        self.last_trade_alert: Optional[LogEvent] = None
        self.bot_status = {
            "crypto_bot": {"status": "unknown", "last_activity": None, "trade_count": 0},
            "alpaca_bot": {"status": "unknown", "last_activity": None, "trade_count": 0},
        }
        self.profit_locks_today = 0
        self.profit_locks_week = 0
        self.trades_today = 0

    def add_event(
        self,
        level: str,
        source: str,
        message: str,
        event_type: str,
        data: Optional[Dict[str, Any]] = None,
    ):
        """Add a new log event to the monitor"""
        event = LogEvent(
            timestamp=datetime.utcnow().isoformat(),
            level=level,
            source=source,
            message=message,
            event_type=event_type,
            data=data,
        )
        self.events.append(event)

        # Track specific event types
        if event_type == "profit_lock":
            self.last_profit_lock = event
            self.profit_locks_today += 1
            self.profit_locks_week += 1
            log.info(f"📊 PROFIT LOCK RECORDED: {source} | {message}")

        elif event_type == "trade_alert":
            self.last_trade_alert = event
            self.trades_today += 1
            bot_key = "crypto_bot" if "crypto" in source.lower() else "alpaca_bot"
            self.bot_status[bot_key]["trade_count"] += 1
            log.info(f"📈 TRADE ALERT: {source} | {message}")

        elif event_type == "status":
            bot_key = source.split("_")[0] + "_bot"
            if bot_key in self.bot_status:
                self.bot_status[bot_key]["status"] = data.get("status", "unknown")
                self.bot_status[bot_key]["last_activity"] = event.timestamp
            log.info(f"🔄 BOT STATUS: {source} | {message}")

    def capture_profit_lock(self, symbol: str, profit: float, entry: float, exit_price: float, reason: str):
        """Capture a profit-lock event from the crypto bot"""
        self.add_event(
            level="INFO",
            source="crypto_bot",
            message=f"💰 PROFIT LOCK {symbol} @ ${exit_price:.2f} (+${profit:.2f})",
            event_type="profit_lock",
            data={
                "symbol": symbol,
                "profit": profit,
                "entry": entry,
                "exit": exit_price,
                "reason": reason,
            },
        )

    def capture_trade_close(self, bot_type: str, symbol: str, pnl: float, pnl_pct: float, reason: str):
        """Capture a trade close event from either bot"""
        source = "crypto_bot" if bot_type == "crypto" else "alpaca_bot"
        self.add_event(
            level="INFO",
            source=source,
            message=f"📤 CLOSE {symbol} | P&L: ${pnl:+.2f} ({pnl_pct:+.1f}%) | {reason}",
            event_type="trade_alert",
            data={
                "symbol": symbol,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "reason": reason,
            },
        )

    def capture_bot_status(self, bot_type: str, status: str, equity: Optional[float] = None, balance: Optional[float] = None):
        """Capture bot status update"""
        source = "crypto_bot" if bot_type == "crypto" else "alpaca_bot"
        data = {"status": status}
        if equity is not None:
            data["equity"] = equity
        if balance is not None:
            data["balance"] = balance

        message = f"{status.upper()}"
        if equity is not None:
            message += f" | Equity: ${equity:.2f}"
        if balance is not None:
            message += f" | Balance: ${balance:.2f}"

        self.add_event(
            level="INFO" if status != "error" else "WARNING",
            source=source,
            message=message,
            event_type="status",
            data=data,
        )

    def get_recent_events(self, limit: int = 50, event_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get recent log events, optionally filtered by type"""
        events = list(self.events)
        if event_type:
            events = [e for e in events if e.event_type == event_type]
        return [e.to_dict() for e in events[-limit:]]

    def get_bot_status(self) -> Dict[str, Any]:
        """Get current bot status and activity metrics"""
        return {
            "crypto_bot": self.bot_status["crypto_bot"],
            "alpaca_bot": self.bot_status["alpaca_bot"],
            "profit_locks_today": self.profit_locks_today,
            "profit_locks_week": self.profit_locks_week,
            "trades_today": self.trades_today,
            "last_profit_lock": self.last_profit_lock.to_dict() if self.last_profit_lock else None,
            "last_trade_alert": self.last_trade_alert.to_dict() if self.last_trade_alert else None,
        }

    def reset_daily_metrics(self):
        """Reset daily counters (call once per day)"""
        self.profit_locks_today = 0
        self.trades_today = 0

    def get_summary(self) -> Dict[str, Any]:
        """Get comprehensive activity summary"""
        return {
            "total_events": len(self.events),
            "bot_status": self.get_bot_status(),
            "recent_events": self.get_recent_events(limit=20),
            "profit_lock_activity": {
                "today": self.profit_locks_today,
                "week": self.profit_locks_week,
                "last_event": self.last_profit_lock.to_dict() if self.last_profit_lock else None,
            },
            "trading_activity": {
                "trades_today": self.trades_today,
                "last_trade": self.last_trade_alert.to_dict() if self.last_trade_alert else None,
            },
        }


# Global monitor instance
monitor = LogMonitor()


# Integration hooks for bots
def log_crypto_profit_lock(symbol: str, profit: float, entry: float, exit_price: float, reason: str):
    """Hook called by crypto_bot when profit lock triggers"""
    monitor.capture_profit_lock(symbol, profit, entry, exit_price, reason)


def log_trade_close(bot_type: str, symbol: str, pnl: float, pnl_pct: float, reason: str):
    """Hook called by either bot when closing a trade"""
    monitor.capture_trade_close(bot_type, symbol, pnl, pnl_pct, reason)


def log_bot_status(bot_type: str, status: str, equity: Optional[float] = None, balance: Optional[float] = None):
    """Hook called by bot on status changes"""
    monitor.capture_bot_status(bot_type, status, equity, balance)

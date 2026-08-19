"""Automated status reporting for trading bots via Hermes Agent and Telegram."""

import logging
import asyncio
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class BotStatus:
    """Snapshot of bot performance metrics"""
    bot_name: str
    timestamp: datetime
    open_positions: Dict[str, Any]
    cash_available: float
    daily_pnl: float
    weekly_pnl: float
    monthly_pnl: float
    win_rate: float
    trade_count: int
    win_count: int
    loss_count: int
    avg_win: float
    avg_loss: float
    errors: List[str]
    last_update: datetime


class StatusReporter:
    """Collects and reports bot status metrics"""

    def __init__(self, telegram_integration=None):
        self.telegram = telegram_integration
        self.bot_statuses: Dict[str, BotStatus] = {}
        self.report_history: List[Dict[str, Any]] = []
        self.auto_report_enabled = False
        self.auto_report_interval = 3600  # 1 hour

    async def record_bot_status(
        self,
        bot_name: str,
        open_positions: Dict[str, Any],
        cash_available: float,
        daily_pnl: float,
        weekly_pnl: float = 0.0,
        monthly_pnl: float = 0.0,
        win_rate: float = 0.0,
        trade_count: int = 0,
        win_count: int = 0,
        loss_count: int = 0,
        avg_win: float = 0.0,
        avg_loss: float = 0.0,
        errors: List[str] = None,
    ) -> BotStatus:
        """Record current bot status"""
        errors = errors or []

        status = BotStatus(
            bot_name=bot_name,
            timestamp=datetime.utcnow(),
            open_positions=open_positions,
            cash_available=cash_available,
            daily_pnl=daily_pnl,
            weekly_pnl=weekly_pnl,
            monthly_pnl=monthly_pnl,
            win_rate=win_rate,
            trade_count=trade_count,
            win_count=win_count,
            loss_count=loss_count,
            avg_win=avg_win,
            avg_loss=avg_loss,
            errors=errors,
            last_update=datetime.utcnow(),
        )

        self.bot_statuses[bot_name] = status
        log.info(f"📊 Recorded status for {bot_name}: P&L ${daily_pnl:+.2f}, Cash ${cash_available:,.2f}")
        return status

    async def get_bot_status(self, bot_name: str) -> Optional[BotStatus]:
        """Get latest recorded status for a bot"""
        return self.bot_statuses.get(bot_name)

    async def get_all_statuses(self) -> Dict[str, BotStatus]:
        """Get all bot statuses"""
        return self.bot_statuses.copy()

    async def generate_summary_report(self) -> Dict[str, Any]:
        """Generate summary report across all bots"""
        if not self.bot_statuses:
            return {"error": "No bot statuses recorded"}

        total_cash = sum(s.cash_available for s in self.bot_statuses.values())
        total_pnl = sum(s.daily_pnl for s in self.bot_statuses.values())
        total_trades = sum(s.trade_count for s in self.bot_statuses.values())
        total_wins = sum(s.win_count for s in self.bot_statuses.values())

        avg_win_rate = (
            (total_wins / total_trades * 100) if total_trades > 0 else 0
        )

        report = {
            "timestamp": datetime.utcnow().isoformat(),
            "bot_count": len(self.bot_statuses),
            "total_cash": total_cash,
            "total_daily_pnl": total_pnl,
            "total_trades": total_trades,
            "total_wins": total_wins,
            "avg_win_rate": avg_win_rate,
            "bot_details": {
                name: {
                    "cash": status.cash_available,
                    "daily_pnl": status.daily_pnl,
                    "positions": len(status.open_positions),
                    "win_rate": status.win_rate,
                }
                for name, status in self.bot_statuses.items()
            },
        }

        self.report_history.append(report)
        return report

    async def send_status_report(self, bot_name: Optional[str] = None) -> bool:
        """Send status report to Telegram"""
        if not self.telegram:
            log.warning("Telegram not configured, skipping report")
            return False

        if bot_name:
            status = await self.get_bot_status(bot_name)
            if not status:
                log.warning(f"No status found for {bot_name}")
                return False

            return await self.telegram.send_formatted_status(
                positions=status.open_positions,
                cash=status.cash_available,
                daily_pnl=status.daily_pnl,
                win_rate=status.win_rate,
                trade_count=status.trade_count,
                errors=status.errors,
            )
        else:
            # Send summary for all bots
            report = await self.generate_summary_report()

            message = f"<b>📊 EMPIRE V2 TRADING SUMMARY</b>\n"
            message += f"<i>{report['timestamp']}</i>\n\n"
            message += f"<b>Bots Active:</b> {report['bot_count']}\n"
            message += f"<b>Total Cash:</b> <code>${report['total_cash']:,.2f}</code>\n"
            message += f"<b>Daily P&L:</b> <b>${report['total_daily_pnl']:+,.2f}</b>\n"
            message += f"<b>Trades Today:</b> {report['total_trades']} "
            message += f"({report['total_wins']} wins, {report['avg_win_rate']:.1f}% win rate)\n\n"

            message += "<b>Bot Breakdown:</b>\n"
            for name, details in report["bot_details"].items():
                pnl_emoji = "🟢" if details["daily_pnl"] > 0 else "🔴" if details["daily_pnl"] < 0 else "⚪"
                message += f"  {pnl_emoji} <b>{name}</b>: "
                message += f"${details['daily_pnl']:+,.2f} "
                message += f"| {details['positions']} pos | {details['win_rate']:.0f}%\n"

            return await self.telegram.send_message(message, message_type="report")

    async def send_hourly_report(self) -> bool:
        """Send hourly automated report"""
        log.info("📨 Sending hourly status report...")
        return await self.send_status_report()

    async def start_auto_reporting(self, interval_seconds: int = 3600) -> None:
        """Start automatic reporting loop"""
        self.auto_report_enabled = True
        self.auto_report_interval = interval_seconds
        log.info(f"🔄 Auto-reporting started (interval: {interval_seconds}s)")

        try:
            while self.auto_report_enabled:
                await asyncio.sleep(self.auto_report_interval)
                await self.send_hourly_report()
        except asyncio.CancelledError:
            log.info("Auto-reporting cancelled")
            self.auto_report_enabled = False

    async def stop_auto_reporting(self) -> None:
        """Stop automatic reporting"""
        self.auto_report_enabled = False
        log.info("Auto-reporting stopped")

    def get_statistics(self) -> Dict[str, Any]:
        """Get reporter statistics"""
        return {
            "auto_report_enabled": self.auto_report_enabled,
            "auto_report_interval": self.auto_report_interval,
            "tracked_bots": len(self.bot_statuses),
            "report_history_size": len(self.report_history),
            "bot_names": list(self.bot_statuses.keys()),
        }


# Global status reporter instance
status_reporter: Optional[StatusReporter] = None


def init_status_reporter(telegram_integration=None) -> StatusReporter:
    """Initialize global status reporter"""
    global status_reporter
    status_reporter = StatusReporter(telegram_integration)
    return status_reporter


def get_status_reporter() -> Optional[StatusReporter]:
    """Get global status reporter"""
    return status_reporter

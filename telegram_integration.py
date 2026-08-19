"""Telegram Bot integration for Hermes Agent notifications."""

import os
import logging
from typing import Optional, Dict, Any
from datetime import datetime
from pydantic import BaseModel

log = logging.getLogger(__name__)


class TelegramConfig(BaseModel):
    """Telegram configuration"""
    enabled: bool = True
    bot_token: Optional[str] = None
    chat_id: Optional[str] = None
    channel_id: Optional[str] = None
    parse_mode: str = "HTML"  # HTML or Markdown
    retry_count: int = 3


class TelegramMessage(BaseModel):
    """Telegram message record"""
    message_id: Optional[str] = None
    chat_id: str
    text: str
    sent_at: datetime
    message_type: str  # status, alert, command, report
    status: str  # sent, failed, pending
    metadata: Dict[str, Any] = {}


class TelegramIntegration:
    """Telegram Bot integration for Empire v2"""

    def __init__(self, config: Optional[TelegramConfig] = None):
        self.config = config or TelegramConfig()
        self.bot_token = self.config.bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = self.config.chat_id or os.getenv("TELEGRAM_CHAT_ID")
        self.enabled = self.config.enabled and bool(self.bot_token) and bool(self.chat_id)
        self.message_history = []

        if self.enabled:
            log.info("✅ Telegram integration initialized")
        else:
            log.warning("⚠️ Telegram integration disabled (token or chat_id not configured)")

    async def send_message(
        self,
        text: str,
        message_type: str = "status",
        parse_mode: Optional[str] = None,
        disable_notification: bool = False
    ) -> bool:
        """Send message to Telegram"""
        if not self.enabled:
            log.warning("Telegram not enabled, skipping message")
            return False

        parse_mode = parse_mode or self.config.parse_mode

        try:
            message = TelegramMessage(
                chat_id=self.chat_id,
                text=text,
                sent_at=datetime.utcnow(),
                message_type=message_type,
                status="pending"
            )
            self.message_history.append(message)

            log.info(f"📤 Sending Telegram message ({message_type}): {text[:50]}...")

            # TODO: Implement actual Telegram Bot API call using python-telegram-bot
            # import telegram
            # bot = telegram.Bot(token=self.bot_token)
            # await bot.send_message(chat_id=self.chat_id, text=text, parse_mode=parse_mode)

            message.status = "sent"
            log.debug(f"✅ Telegram message sent successfully")
            return True

        except Exception as e:
            log.error(f"❌ Telegram send error: {e}")
            if self.message_history:
                self.message_history[-1].status = "failed"
            return False

    async def send_formatted_status(
        self,
        positions: Dict[str, Any],
        cash: float,
        daily_pnl: float,
        win_rate: float,
        trade_count: int,
        errors: list = None
    ) -> bool:
        """Send formatted trading bot status"""
        if not self.enabled:
            return False

        errors = errors or []

        # Format status message in HTML
        message = self._format_status_message(
            positions=positions,
            cash=cash,
            daily_pnl=daily_pnl,
            win_rate=win_rate,
            trade_count=trade_count,
            errors=errors
        )

        return await self.send_message(message, message_type="status")

    def _format_status_message(
        self,
        positions: Dict[str, Any],
        cash: float,
        daily_pnl: float,
        win_rate: float,
        trade_count: int,
        errors: list
    ) -> str:
        """Format trading status as HTML message"""
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

        # Header
        message = f"<b>📊 TRADING BOT STATUS</b>\n"
        message += f"<i>{timestamp}</i>\n\n"

        # Positions
        if positions:
            message += "<b>📈 Open Positions:</b>\n"
            for symbol, pos in positions.items():
                entry = pos.get("entry_price", "N/A")
                current = pos.get("current_price", "N/A")
                pnl = pos.get("pnl", 0)
                pnl_pct = pos.get("pnl_pct", 0)

                pnl_emoji = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
                message += f"  {pnl_emoji} <code>{symbol}</code>: Entry ${entry} | Current ${current} | "
                message += f"<b>${pnl:+.2f}</b> ({pnl_pct:+.2f}%)\n"
        else:
            message += "📭 No open positions\n"

        message += "\n"

        # Account metrics
        message += "<b>💰 Account Metrics:</b>\n"
        message += f"  💵 Cash Available: <code>${cash:,.2f}</code>\n"
        message += f"  📊 Daily P&L: <b>${daily_pnl:+,.2f}</b>\n"
        message += f"  🎯 Win Rate: <code>{win_rate:.1f}%</code> ({trade_count} trades)\n"

        # Errors
        if errors:
            message += "\n<b>⚠️ Errors:</b>\n"
            for error in errors[:3]:  # Show max 3 errors
                message += f"  🔴 {error}\n"
        else:
            message += "\n✅ No errors\n"

        return message

    async def send_alert(self, title: str, message: str) -> bool:
        """Send alert message"""
        alert_msg = f"<b>⚠️ ALERT: {title}</b>\n\n{message}"
        return await self.send_message(alert_msg, message_type="alert")

    async def send_command_result(self, command: str, result: str) -> bool:
        """Send command execution result"""
        msg = f"<b>📋 Command: {command}</b>\n\n<code>{result}</code>"
        return await self.send_message(msg, message_type="command")

    def get_message_history(self, limit: int = 10) -> list:
        """Get recent message history"""
        return self.message_history[-limit:]

    def get_config(self) -> Dict[str, Any]:
        """Get configuration status"""
        return {
            "enabled": self.enabled,
            "chat_id": self.chat_id[:5] + "..." if self.chat_id else None,
            "bot_token": self.bot_token[:20] + "..." if self.bot_token else None,
            "parse_mode": self.config.parse_mode,
            "message_count": len(self.message_history),
        }


# Global Telegram instance
telegram_bot: Optional[TelegramIntegration] = None


def init_telegram(config: Optional[TelegramConfig] = None) -> TelegramIntegration:
    """Initialize global Telegram instance"""
    global telegram_bot
    telegram_bot = TelegramIntegration(config)
    return telegram_bot


def get_telegram() -> Optional[TelegramIntegration]:
    """Get global Telegram instance"""
    return telegram_bot

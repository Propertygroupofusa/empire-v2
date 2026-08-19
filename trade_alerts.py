"""
Real-time Trade Alerts - Slack & Email notifications for live trades
Monitors Coinbase and Alpaca bots for trade execution events
"""
import os
import asyncio
import json
import logging
from datetime import datetime
from typing import Optional
import aiohttp
from sqlalchemy import select
from database import AsyncSessionLocal
from models import CryptoTradeLog, BotPosition

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("trade_alerts")

SLACK_WEBHOOK = os.getenv("SLACK_WEBHOOK_URL", "")
ALERT_EMAIL = os.getenv("ALERT_EMAIL", "delfarrell591@gmail.com")

class TradeAlertSystem:
    def __init__(self):
        self.slack_enabled = bool(SLACK_WEBHOOK)
        self.session = None

    async def send_slack_alert(self, message: str, trade_data: dict):
        """Send trade execution alert to Slack"""
        if not self.slack_enabled:
            return

        try:
            payload = {
                "text": f"🚨 Trade Alert: {message}",
                "blocks": [
                    {
                        "type": "header",
                        "text": {"type": "plain_text", "text": "📊 Live Trade Executed"}
                    },
                    {
                        "type": "section",
                        "fields": [
                            {"type": "mrkdwn", "text": f"*Symbol*\n{trade_data.get('symbol', 'N/A')}"},
                            {"type": "mrkdwn", "text": f"*Side*\n{trade_data.get('side', 'N/A')}"},
                            {"type": "mrkdwn", "text": f"*Price*\n${trade_data.get('price', 'N/A')}"},
                            {"type": "mrkdwn", "text": f"*Quantity*\n{trade_data.get('quantity', 'N/A')}"},
                        ]
                    },
                    {
                        "type": "context",
                        "elements": [
                            {"type": "mrkdwn", "text": f"⏰ {datetime.now().isoformat()} | 🤖 {trade_data.get('bot_name', 'Unknown Bot')}"}
                        ]
                    }
                ]
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(SLACK_WEBHOOK, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        log.info(f"✓ Slack alert sent: {trade_data.get('symbol')}")
                    else:
                        log.warning(f"Slack alert failed: {resp.status}")
        except Exception as e:
            log.error(f"Error sending Slack alert: {e}")

    async def send_trade_summary(self, bot_name: str, position_data: dict):
        """Send daily trade summary"""
        if not self.slack_enabled:
            return

        try:
            unrealized_pnl = position_data.get("unrealized_pnl", 0)
            realized_pnl = position_data.get("realized_pnl", 0)
            total_pnl = unrealized_pnl + realized_pnl

            emoji = "📈" if total_pnl >= 0 else "📉"

            payload = {
                "text": f"{emoji} {bot_name} Daily Summary",
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*{bot_name} - Daily Summary*\n"
                                   f"Total P&L: ${total_pnl:.2f} {emoji}\n"
                                   f"Realized: ${realized_pnl:.2f}\n"
                                   f"Unrealized: ${unrealized_pnl:.2f}\n"
                                   f"Open Positions: {position_data.get('open_count', 0)}"
                        }
                    }
                ]
            }

            async with aiohttp.ClientSession() as session:
                await session.post(SLACK_WEBHOOK, json=payload, timeout=aiohttp.ClientTimeout(total=5))
        except Exception as e:
            log.error(f"Error sending summary: {e}")

    async def monitor_trades(self):
        """Continuously monitor for new trades"""
        log.info("✓ Trade alert monitor started")

        while True:
            try:
                async with AsyncSessionLocal() as session:
                    # Check for new crypto trades
                    result = await session.execute(
                        select(CryptoTradeLog)
                        .where(CryptoTradeLog.alert_sent == False)
                        .order_by(CryptoTradeLog.executed_at.desc())
                        .limit(10)
                    )
                    new_trades = result.scalars().all()

                    for trade in new_trades:
                        trade_data = {
                            "symbol": trade.symbol,
                            "side": trade.entry_type or "N/A",
                            "price": trade.entry_price,
                            "quantity": trade.size,
                            "bot_name": "Coinbase Crypto Bot"
                        }

                        await self.send_slack_alert(
                            f"{trade.symbol} {trade.entry_type or 'ENTRY'}",
                            trade_data
                        )

                        # Mark as alerted
                        trade.alert_sent = True
                        await session.commit()

                await asyncio.sleep(30)  # Check every 30 seconds

            except Exception as e:
                log.error(f"Monitor error: {e}")
                await asyncio.sleep(60)

async def start_trade_alerts():
    """Start the trade alert system"""
    alerter = TradeAlertSystem()
    await alerter.monitor_trades()

if __name__ == "__main__":
    asyncio.run(start_trade_alerts())

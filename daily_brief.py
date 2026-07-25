"""
DAILY VENTURES BRIEF
=====================================
One email each morning summarizing real state across whatever's actually
live - right now that's empire-v2: trading P&L, notary job activity, and
AI support inbox activity. Reuses existing infrastructure, no new
credentials: the same GMAIL_EMAIL/GMAIL_PASSWORD SMTP prop_bot.py and
notary_bot.py already use to send alerts, and the same ANTHROPIC_API_KEY
already used elsewhere in this app.

Deliberately informational only - it reads real numbers and has Claude
write a plain-English summary. It does not move money, restart
anything, or take any action on its own. This is the intentionally
small, real seed of the "DelOS daily executive briefing" idea - built
on infrastructure that actually exists today, not a speculative
autonomous-agent layer.

Own SQLAlchemy engine/session (same reasoning as notary_bot.py), not the
shared database.engine: APScheduler's BackgroundScheduler runs each job
in its own worker thread with a fresh event loop (see
_run_scheduled_brief), and asyncpg connections are bound to the event
loop that created them - sharing the main app's connection pool across
threads/loops causes "attached to a different loop" errors.
"""
import os
import asyncio
import logging
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import aiohttp
import anthropic
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from models import Job, SupportConversation, DailyBrief

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("daily_brief")

ET = ZoneInfo("America/New_York")

ALPACA_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
ALPACA_HEADERS = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}

# Sent before market open (9:30am ET) by default.
BRIEF_HOUR = int(os.getenv("DAILY_BRIEF_HOUR", "7"))
BRIEF_EMAIL = os.getenv("DAILY_BRIEF_EMAIL", os.getenv("TRADE_ALERT_EMAIL", "delfarrell591@gmail.com"))

CLAUDE_KEY = os.getenv("ANTHROPIC_API_KEY", "")
_claude = anthropic.Anthropic(api_key=CLAUDE_KEY)  # sync client is fine - this runs once a day, not per-request

_DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./empire.db")
if _DATABASE_URL.startswith("postgresql://"):
    _DATABASE_URL = _DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
elif _DATABASE_URL.startswith("postgres://"):
    _DATABASE_URL = _DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)

_engine = create_async_engine(_DATABASE_URL, echo=False, pool_pre_ping=True)
_SessionLocal = sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)


async def _fetch_trading_snapshot() -> dict:
    """Real Alpaca account snapshot, plus whatever prop_bot.py and
    crypto_alpaca_bot.py currently hold in memory - same read-only
    approach routers/trading_dashboard.py already uses. Never raises -
    a data-fetch hiccup should degrade the brief, not skip sending it."""
    snapshot = {"available": False}
    if ALPACA_KEY and ALPACA_SECRET:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{ALPACA_BASE_URL}/v2/account", headers=ALPACA_HEADERS) as r:
                    if r.status == 200:
                        account = await r.json()
                        equity = float(account.get("equity", 0))
                        last_equity = float(account.get("last_equity", 0))
                        snapshot = {
                            "available": True,
                            "equity": equity,
                            "cash": float(account.get("cash", 0)),
                            "equity_change": equity - last_equity,
                        }
        except Exception as e:
            log.warning(f"Could not fetch Alpaca account for brief: {e}")

    try:
        import prop_bot
        snapshot["stock_positions"] = len(prop_bot.open_prop_positions)
        snapshot["stock_daily_pnl"] = round(prop_bot.daily_pnl, 2)
    except Exception:
        pass

    try:
        import crypto_alpaca_bot
        snapshot["crypto_positions"] = len(crypto_alpaca_bot.open_crypto_positions)
        snapshot["crypto_daily_pnl"] = round(crypto_alpaca_bot.daily_pnl, 2)
    except Exception:
        pass

    return snapshot


async def _fetch_notary_snapshot(db: AsyncSession) -> dict:
    result = await db.execute(select(Job.status, func.count(Job.id)).group_by(Job.status))
    return {status: count for status, count in result.all()}


async def _fetch_support_snapshot(db: AsyncSession) -> dict:
    result = await db.execute(
        select(SupportConversation.status, func.count(SupportConversation.id)).group_by(SupportConversation.status)
    )
    return {status: count for status, count in result.all()}


def _format_brief_prompt(trading: dict, notary: dict, support: dict) -> str:
    return f"""Write a short daily business briefing for Del Ventures - 3 short
paragraphs max, plain English, no headers or bullet lists. Use ONLY the
real numbers given below - do not invent numbers, and do not give
investment advice or predictions, just report what happened.

TRADING (empire-v2, real Alpaca account):
{trading}

NOTARY MARKETPLACE (jobs grouped by status):
{notary}

AI SUPPORT INBOX (conversations grouped by status):
{support}
"""


def _generate_summary(trading: dict, notary: dict, support: dict) -> str:
    if not CLAUDE_KEY:
        return "(ANTHROPIC_API_KEY not configured - raw numbers only below, no summary generated.)"
    try:
        response = _claude.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=400,
            messages=[{"role": "user", "content": _format_brief_prompt(trading, notary, support)}],
        )
        return response.content[0].text.strip()
    except Exception as e:
        log.error(f"Claude call failed generating daily brief: {e}")
        return "(Summary generation failed - raw numbers only below.)"


def _send_brief_email(subject: str, body: str):
    """Same GMAIL_EMAIL/GMAIL_PASSWORD SMTP pattern as prop_bot.py and
    notary_bot.py - no-ops quietly if creds aren't set."""
    sender_email = os.getenv("GMAIL_EMAIL", "")
    sender_password = os.getenv("GMAIL_PASSWORD", "")
    if not sender_email or not sender_password:
        log.info(f"(daily brief email skipped - GMAIL_EMAIL/GMAIL_PASSWORD not set) {subject}")
        return
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = sender_email
        msg["To"] = BRIEF_EMAIL
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, BRIEF_EMAIL, msg.as_string())
        log.info(f"📧 Daily brief emailed to {BRIEF_EMAIL}")
    except Exception as e:
        log.warning(f"Daily brief email failed: {e}")


async def generate_and_send_brief():
    trading = await _fetch_trading_snapshot()
    async with _SessionLocal() as db:
        notary = await _fetch_notary_snapshot(db)
        support = await _fetch_support_snapshot(db)

    summary = _generate_summary(trading, notary, support)

    # Persisted before sending, not after - so the history record exists
    # even if the email step fails (missing GMAIL_EMAIL/PASSWORD, SMTP
    # hiccup, etc.). The email is a delivery channel; the DB row is the
    # actual record.
    async with _SessionLocal() as db:
        db.add(DailyBrief(summary=summary, trading_snapshot=trading, notary_snapshot=notary, support_snapshot=support))
        await db.commit()

    today = datetime.now(ET).strftime("%A, %B %d")
    body = (
        f"{summary}\n\n"
        f"---\n"
        f"Raw numbers:\n"
        f"Trading: {trading}\n"
        f"Notary jobs: {notary}\n"
        f"Support conversations: {support}\n\n"
        f"Dashboard: https://empire-v2-production.up.railway.app/trading-dashboard"
    )
    _send_brief_email(f"☀️ Del Ventures Brief — {today}", body)
    log.info("Daily brief generated, persisted, and sent")


_scheduler = BackgroundScheduler()


def _run_scheduled_brief():
    """APScheduler's BackgroundScheduler runs jobs in worker threads, not
    the main app's event loop - a fresh event loop per run, same pattern
    daily_publisher.py already uses for its own scheduled jobs."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(generate_and_send_brief())
    except Exception as e:
        log.error(f"Daily brief job failed: {e}")
    finally:
        loop.close()


def start_daily_brief():
    """Start the daily brief scheduler. Disabled entirely via
    DAILY_BRIEF_ENABLED=false; otherwise runs once a day at
    DAILY_BRIEF_HOUR (default 7am ET, before market open)."""
    if os.getenv("DAILY_BRIEF_ENABLED", "true").lower() != "true":
        return False
    _scheduler.add_job(
        _run_scheduled_brief,
        CronTrigger(hour=BRIEF_HOUR, minute=0, timezone=ET),
        id="daily_ventures_brief",
        name="Daily Ventures Brief",
        replace_existing=True,
    )
    if not _scheduler.running:
        _scheduler.start()
    log.info(f"Daily brief scheduled for {BRIEF_HOUR}:00 ET")
    return True


if __name__ == "__main__":
    # Manual test: `python3 daily_brief.py` generates and sends one immediately.
    asyncio.run(generate_and_send_brief())

#!/usr/bin/env python3
"""Scheduler for Alpaca iron condor bot - runs daily checks at fixed times"""

from apscheduler.schedulers.background import BackgroundScheduler
from alpaca_iron_condor_bot import IronCondorBot
import logging
import signal
import sys

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
log = logging.getLogger(__name__)

scheduler = BackgroundScheduler()
bot = None

def run_daily_cycle():
    global bot
    try:
        if not bot:
            bot = IronCondorBot()
        bot.run_daily_cycle()
        bot.generate_daily_report()
        bot.save_trade_log()
    except Exception as e:
        log.error(f"Error in scheduled cycle: {e}", exc_info=True)

def signal_handler(sig, frame):
    log.info("Shutting down scheduler...")
    scheduler.shutdown()
    sys.exit(0)

if __name__ == '__main__':
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    log.info("Starting Alpaca bot scheduler...")
    log.info("Entry check: 10:00 AM ET")
    log.info("Exit check: 2:00 PM ET")

    # 10:00 AM ET - Morning entry check
    scheduler.add_job(run_daily_cycle, 'cron', hour=10, minute=0, timezone='America/New_York', id='morning_check')

    # 2:00 PM ET - Exit check
    scheduler.add_job(run_daily_cycle, 'cron', hour=14, minute=0, timezone='America/New_York', id='afternoon_check')

    scheduler.start()

    try:
        while True:
            import time
            time.sleep(1)
    except KeyboardInterrupt:
        scheduler.shutdown()
        log.info("Scheduler stopped")

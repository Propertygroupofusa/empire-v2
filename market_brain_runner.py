"""Run market_brain's cycle - flag-gated, account-reconciled, no self-update.

The owner chose Option 1 on 2026-09-28: market_brain owns the equity
side and prop_bot stops entering it. b01ca70 put the handover gate in
prop_bot and made market_brain's 60% ceiling measure the real account.
b72b068 made its book rebuild from the broker so a redeploy cannot
orphan a position. This is the last piece: something that actually
calls the cycle.

FOUR THINGS THIS RUNNER DOES THAT market_brain's OWN `schedule` LOOP
DOES NOT:

 1. IT CHECKS THE FLAG EVERY CYCLE. market_brain_owns_equities lives in
    TradingBotState, so the owner can stop the bot from the dashboard
    without a redeploy. A bot you can only stop by deploying is a bot
    you cannot stop.
 2. IT RECONCILES FROM THE BROKER FIRST, so the exit loop is always
    working from the account's real holdings rather than whatever
    survived the last deploy.
 3. IT DOES NOT SELF-UPDATE. market_brain's run_cycle() opens with
    check_for_updates(__file__), which downloads a new copy of the
    module from GitHub and overwrites its own file - remote code
    replacement between trading cycles on a live account. That is now
    off by default (MARKET_BRAIN_SELF_UPDATE).
 4. IT RESPECTS STOP_TRADING, the process-wide kill switch every other
    live subsystem here already honours.

It starts on every boot and does nothing at all until the flag is on.
"""
import asyncio
import logging
import os
import time

log = logging.getLogger(__name__)

POLL_SECONDS_WHEN_OFF = 60.0


def _flag_on():
    """Read the handover flag. False on any error - a runner that cannot
    read its own switch must not trade."""
    try:
        import prop_bot
        return asyncio.run(prop_bot.is_market_brain_equities_active())
    except Exception as e:
        log.warning(f"[BRAIN] cannot read the handover flag ({e}) - not trading this cycle")
        return False


def _reconcile(mb):
    """Rebuild market_brain's book from the broker. Returns True if the
    book is trustworthy, False if the cycle should be skipped - never a
    partial book, which is how a position gets orphaned."""
    try:
        import brain_positions
        positions = mb.api_call("GET", "/v2/positions") or []
        account = mb.api_call("GET", "/v2/account") or {}
        equity = account.get("equity")
        book, changes = brain_positions.reconcile(mb.state.positions, positions, equity)
        if book is None:
            log.warning(f"[BRAIN] reconcile refused ({changes}) - skipping this cycle "
                        f"rather than trading on a partial book")
            return False
        adopted = [s for kind, s in changes if kind == brain_positions.ADOPTED]
        dropped = [s for kind, s in changes if kind == brain_positions.DROPPED]
        if adopted:
            log.warning(f"[BRAIN] adopted from the broker (would otherwise have had no "
                        f"stop): {adopted}")
        if dropped:
            log.info(f"[BRAIN] closed elsewhere, dropped: {dropped}")
        mb.state.positions = book
        return True
    except Exception as e:
        log.warning(f"[BRAIN] reconcile failed ({e}) - skipping this cycle")
        return False


def run():
    """Daemon-thread entry point, same shape as the other bots'."""
    log.info("[BRAIN] runner started - idle until market_brain_owns_equities is on")
    while True:
        try:
            if os.getenv("STOP_TRADING", "false").lower() == "true":
                time.sleep(POLL_SECONDS_WHEN_OFF)
                continue
            if not _flag_on():
                time.sleep(POLL_SECONDS_WHEN_OFF)
                continue

            import market_brain as mb
            if not _reconcile(mb):
                time.sleep(POLL_SECONDS_WHEN_OFF)
                continue

            mb.run_cycle()
            mb.state.save()
            sleep_for = float(mb.CONFIG.get("cycle_minutes", 15)) * 60.0
        except Exception as e:
            log.error(f"[BRAIN] cycle error: {e}")
            sleep_for = POLL_SECONDS_WHEN_OFF
        time.sleep(max(POLL_SECONDS_WHEN_OFF, sleep_for))

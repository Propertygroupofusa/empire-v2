"""The loop that tells the owner a trade closed.

crypto_grid_bot.py has no email code at all, so every one of the fleet's
162 round trips has closed in silence. trade_notify.py builds the
message; this is the thing that notices there is one to build.

ONE MESSAGE PER BATCH, NOT PER TRADE. 2026-09-28 closed 35 round trips.
Thirty-five emails is a muted sender, and a muted sender swallows the
next real alert too.

THE HIGH-WATER MARK IS PERSISTED. Keeping it in memory would re-send the
whole book on every restart, and this app restarts on every deploy. It
lives in the same TradingBotState key-value row the reconcile and
rotation switches use, so it survives a deploy and needs no migration.

IT ONLY EVER MOVES FORWARD, and only after a send SUCCEEDS. A mark
written before delivery turns one failed SMTP call into trades the owner
is never told about - silently, since the row is gone from the next
query either way.
"""
import asyncio
import logging
import os

log = logging.getLogger(__name__)

CHECK_SECONDS = int(os.getenv("TRADE_NOTIFY_CHECK_SECONDS", "600"))
WATERMARK_KEY = "trade_notify_last_id"

# A first run on a book with 162 closes must not email all of them. The
# first pass baselines silently to the newest id and starts from there.
FIRST_RUN_IS_A_BASELINE = True


async def _get_mark(session_factory):
    from models import TradingBotState
    from sqlalchemy import select
    async with session_factory()() as db:
        row = (await db.execute(select(TradingBotState).where(
            TradingBotState.bot_name == WATERMARK_KEY))).scalar_one_or_none()
        return int(row.base_capital) if row and row.base_capital is not None else None


async def _set_mark(session_factory, value: int):
    from models import TradingBotState
    from sqlalchemy import select
    async with session_factory()() as db:
        row = (await db.execute(select(TradingBotState).where(
            TradingBotState.bot_name == WATERMARK_KEY))).scalar_one_or_none()
        if row is None:
            db.add(TradingBotState(bot_name=WATERMARK_KEY,
                                   base_capital=float(value),
                                   starting_capital=0.0))
        else:
            # Never backwards. A stale read must not re-send a batch.
            row.base_capital = float(max(value, int(row.base_capital or 0)))
        await db.commit()


async def check_once(session_factory, *, force=False) -> dict:
    from models import CryptoGridTradeHistory
    from sqlalchemy import select
    import trade_notify

    if not (trade_notify.armed() or force):
        return {"sent": 0, "skipped": f"{trade_notify.MODE_ENV} is "
                                      f"'{trade_notify.mode()}'"}

    mark = await _get_mark(session_factory)
    async with session_factory()() as db:
        q = select(CryptoGridTradeHistory).order_by(CryptoGridTradeHistory.id)
        if mark is not None:
            q = q.where(CryptoGridTradeHistory.id > mark)
        rows = (await db.execute(q)).scalars().all()

    if mark is None and FIRST_RUN_IS_A_BASELINE:
        newest = max((r.id for r in rows), default=0)
        await _set_mark(session_factory, newest)
        return {"sent": 0, "baselined_at": newest,
                "detail": "first run - starting from the newest close, not "
                          "emailing the whole book"}
    if not rows:
        return {"sent": 0, "detail": "no new closes"}

    trades = [{"pnl": r.pnl, "product_id": r.product_id,
               "exit_reason": getattr(r, "exit_reason", None)} for r in rows]
    ok, err = await asyncio.to_thread(trade_notify.send, trades, force=force)
    if not ok:
        # The mark does NOT move. A failed send must not cost the owner
        # the only notice he was going to get about those trades.
        log.warning(f"[trades] notify failed, mark held at {mark}: {err}")
        return {"sent": 0, "held_at": mark, "error": err}

    newest = max(r.id for r in rows)
    await _set_mark(session_factory, newest)
    return {"sent": len(rows), "through_id": newest}


async def run_periodically(session_factory):
    while True:
        try:
            await check_once(session_factory)
        except Exception as e:
            log.warning(f"[trades] notify cycle failed: {type(e).__name__}: {e}")
        await asyncio.sleep(CHECK_SECONDS)

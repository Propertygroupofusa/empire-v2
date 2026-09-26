"""The fleet's memory: one plain-English lesson per coin, from its own closed trades.

This is the durable replacement for bot_learning_engine.py, which had the
right idea and three fatal problems:

  1. NOTHING IMPORTED IT. Zero call sites in the entire codebase. The
     class was written, and never once ran.

  2. IT STORED TO A LOCAL FILE. bot_learnings.json sits on Railway's
     ephemeral disk, wiped on every redeploy. A memory that forgets each
     time you ship is not a memory.

  3. IT WOULD HAVE BLOCKED EVERY COIN. _update_losing_pattern() created a
     "losing pattern" on the FIRST red trade, and check_before_trade()
     returned safe=False on any match - checking losing patterns BEFORE
     winning ones. This fleet closes 75.6% of round trips green, so about
     one in four is red by design. DOGE is +$12.80 over 15 trades with 4
     losses; it would have been switched off while earning. Within days
     every coin would have been blocked, each one profitable.

WHAT CHANGED

A lesson is a running tally, not a single trade. A verdict may only turn
negative once the sample is large enough to mean something (MIN_TRADES),
and it is judged on TOTAL P&L, not on whether the last trade was red -
because a 76%-win system has red trades constantly and they are not a
signal.

ADVISORY BY DEFAULT

check_before_buy() never blocks unless enforcement is explicitly switched
on in the database AND the evidence clears MIN_TRADES_TO_BLOCK. The
default is to inform and let the fee floor do the refusing. That default
is deliberate: on 2026-09-25 auto-rotate retired four EARNING branches on
thin directional evidence and left $276.80 - 64% of the account - sitting
idle. A memory that silently stops trading a profitable coin repeats that
exact failure with better branding.
"""

import logging
from datetime import datetime

from sqlalchemy import select

from database import get_session_factory
from models import GridLesson, TradingBotState

log = logging.getLogger(__name__)

# A verdict may not turn "avoid" on less evidence than this. Ten completed
# round trips is where a 75% win rate stops being indistinguishable from a
# coin flip on small samples.
MIN_TRADES = 10

# Blocking a coin strands its capital, so it needs more evidence than
# merely flagging one does.
MIN_TRADES_TO_BLOCK = 25

# DB key for the enforcement switch. Absent or false => advisory only.
LESSON_ENFORCEMENT_KEY = "grid_lessons_enforce"


def verdict_for(trades: int, total_pnl: float) -> str:
    """earning | watch | avoid, from the coin's own closed record.

    "avoid" requires BOTH a losing total and a real sample. A coin that is
    down on 3 trades is not a lesson, it is noise - and treating it as one
    is how a profitable fleet talks itself out of its own winners.
    """
    if trades <= 0:
        return "watch"
    if total_pnl > 0:
        return "earning"
    if trades >= MIN_TRADES:
        return "avoid"
    return "watch"


def lesson_for(product_id: str, trades: int, wins: int, losses: int,
               total_pnl: float, step_pct=None) -> str:
    """One plain sentence the account owner can read without context."""
    coin = (product_id or "").replace("-USD", "")
    win_rate = (wins / trades * 100.0) if trades else 0.0
    step = f" at a {step_pct * 100:.2f}% step" if step_pct else ""
    if trades < MIN_TRADES:
        if total_pnl > 0:
            return (f"{coin} is up {total_pnl:+.2f} over {trades} closed round "
                    f"trip{'' if trades == 1 else 's'} - real, but only {trades} of the "
                    f"{MIN_TRADES} needed before that counts as a pattern rather than a run.")
        return (f"{coin}: {total_pnl:+.2f} over {trades} closed round "
                f"trip{'' if trades == 1 else 's'}. Too few to conclude anything - needs "
                f"{MIN_TRADES} before this becomes a real lesson, so it is not being "
                f"held against the coin yet.")
    if total_pnl > 0:
        return (f"{coin} earns{step}: {total_pnl:+.2f} over {trades} closed round trips, "
                f"{win_rate:.0f}% green. Keep trading it.")
    return (f"{coin} loses{step}: {total_pnl:+.2f} over {trades} closed round trips, "
            f"{win_rate:.0f}% green. A high win rate still nets a loss when the losers "
            f"run bigger than the winners - check the step against the fee floor before "
            f"trading it again.")


async def is_enforcement_active() -> bool:
    """Whether a lesson is allowed to actually BLOCK a buy.

    Defaults to False. A memory that stops trading a coin on its own is a
    capital-stranding mechanism, and this account has already paid for one
    of those.
    """
    try:
        async with get_session_factory()() as db:
            result = await db.execute(
                select(TradingBotState).where(TradingBotState.bot_name == LESSON_ENFORCEMENT_KEY))
            row = result.scalar_one_or_none()
            return bool(row and row.base_capital and row.base_capital > 0)
    except Exception as e:
        log.warning(f"[LEARN] enforcement switch unreadable ({e}) - staying advisory")
        return False


async def set_enforcement_active(active: bool) -> bool:
    async with get_session_factory()() as db:
        result = await db.execute(
            select(TradingBotState).where(TradingBotState.bot_name == LESSON_ENFORCEMENT_KEY))
        row = result.scalar_one_or_none()
        if row is None:
            db.add(TradingBotState(bot_name=LESSON_ENFORCEMENT_KEY,
                                   base_capital=1.0 if active else 0.0))
        else:
            row.base_capital = 1.0 if active else 0.0
        await db.commit()
    log.warning(f"[LEARN] lesson enforcement set to {active}")
    return active


async def record_closed_trade(product_id: str, pnl: float, step_pct=None) -> dict:
    """Fold one completed round trip into that coin's lesson. Best-effort.

    Called from the same place the trade-history row is written, so the
    memory can never drift from the ledger. Deliberately never raises: the
    real sale already happened, and a memory failure must not unwind it.
    """
    try:
        async with get_session_factory()() as db:
            result = await db.execute(
                select(GridLesson).where(GridLesson.product_id == product_id))
            row = result.scalar_one_or_none()
            if row is None:
                row = GridLesson(product_id=product_id, trades=0, wins=0, losses=0,
                                 total_pnl=0.0, gross_win_usd=0.0, gross_loss_usd=0.0,
                                 first_seen=datetime.utcnow())
                db.add(row)

            row.trades = (row.trades or 0) + 1
            row.total_pnl = (row.total_pnl or 0.0) + pnl
            if pnl > 0:
                row.wins = (row.wins or 0) + 1
                row.gross_win_usd = (row.gross_win_usd or 0.0) + pnl
            else:
                row.losses = (row.losses or 0) + 1
                row.gross_loss_usd = (row.gross_loss_usd or 0.0) + pnl

            row.last_pnl = pnl
            if step_pct is not None:
                row.last_step_pct = step_pct
            row.best_trade_usd = pnl if row.best_trade_usd is None else max(row.best_trade_usd, pnl)
            row.worst_trade_usd = pnl if row.worst_trade_usd is None else min(row.worst_trade_usd, pnl)

            row.verdict = verdict_for(row.trades, row.total_pnl)
            row.lesson = lesson_for(product_id, row.trades, row.wins, row.losses,
                                    row.total_pnl, row.last_step_pct)
            row.updated_at = datetime.utcnow()
            await db.commit()
            out = row.to_dict()
        log.info(f"[LEARN] {out['lesson']}")
        return out
    except Exception as e:
        log.error(f"[LEARN] could not record the lesson for {product_id} "
                  f"(pnl {pnl:+.2f}) - the real trade is unaffected: {e}")
        return {}


async def check_before_buy(product_id: str) -> dict:
    """What the fleet already knows about this coin, before it buys again.

    Returns {allow, verdict, lesson, trades, total_pnl, enforced}. `allow`
    is False ONLY when enforcement is explicitly switched on in the
    database AND the coin has a losing record over at least
    MIN_TRADES_TO_BLOCK closed round trips. Everything else informs and
    lets the trade proceed - the fee floor is what refuses, because it is
    arithmetic rather than inference.
    """
    try:
        async with get_session_factory()() as db:
            result = await db.execute(
                select(GridLesson).where(GridLesson.product_id == product_id))
            row = result.scalar_one_or_none()
        if row is None:
            return {"allow": True, "verdict": "unknown", "trades": 0, "total_pnl": 0.0,
                    "enforced": False,
                    "lesson": f"No closed round trips recorded for {product_id} yet."}

        enforce = await is_enforcement_active()
        blocking = (enforce
                    and (row.total_pnl or 0.0) < 0
                    and (row.trades or 0) >= MIN_TRADES_TO_BLOCK)
        if blocking:
            log.warning(f"[LEARN] BLOCKING a buy on {product_id}: {row.lesson}")
        return {"allow": not blocking, "verdict": row.verdict, "lesson": row.lesson,
                "trades": row.trades, "total_pnl": round(row.total_pnl or 0.0, 2),
                "enforced": enforce}
    except Exception as e:
        # Fail OPEN. An unreadable memory must never silently halt trading.
        log.warning(f"[LEARN] lesson lookup failed for {product_id} ({e}) - allowing the trade")
        return {"allow": True, "verdict": "unknown", "trades": 0, "total_pnl": 0.0,
                "enforced": False, "lesson": f"Lesson lookup failed: {e}"}


async def get_all_lessons(fleet_products=None) -> dict:
    """Everything the fleet has learned, worst first. Read-only.

    fleet_products is the set of coins the fleet ACTUALLY TRADES right
    now. Every lesson is tagged with whether its coin is one of them,
    because on 2026-09-26 this panel showed eleven coins - AAVE, ARB,
    ATOM, BCH, DOGE, ETC, ETH, LINK, LTC, STX, WIF - every one of them
    from the RETIRED cohort, against a live fleet of BTC, NEAR, BONK,
    ONDO, FLOKI and TIA. Zero overlap. It read "DOGE earns +12.80 ... Keep
    trading it" about a coin the fleet does not hold, under a 2.00% step
    and market fallback that no longer exist, while the current cohort's
    own record was {"trades": 0}.

    Passing None means "the fleet is unknown", which tags nothing rather
    than tagging everything as retired - not knowing is not evidence.
    """
    try:
        async with get_session_factory()() as db:
            result = await db.execute(select(GridLesson))
            rows = [r.to_dict() for r in result.scalars().all()]
    except Exception as e:
        return {"lessons": [], "error": str(e), "enforcement_active": False}

    fleet = None
    if fleet_products is not None:
        fleet = {str(p).upper() for p in fleet_products if p}
    for r in rows:
        pid = str(r.get("product_id") or "").upper()
        r["in_fleet"] = None if fleet is None else (pid in fleet)
        if r["in_fleet"] is False:
            coin = pid.replace("-USD", "")
            r["retired_note"] = (
                f"The fleet does not trade {coin} any more. This is history from a "
                f"retired configuration - not advice about what to trade now.")

    rows.sort(key=lambda r: r["total_pnl"])
    in_fleet = [r for r in rows if r.get("in_fleet")]
    retired = [r for r in rows if r.get("in_fleet") is False]
    return {
        "lessons": rows,
        "lesson_count": len(rows),
        "fleet_known": fleet is not None,
        "in_fleet_count": len(in_fleet) if fleet is not None else None,
        "retired_count": len(retired) if fleet is not None else None,
        # The headline the panel had no way to state: every lesson on
        # record can belong to coins the fleet has stopped trading.
        "all_lessons_are_retired": bool(fleet is not None and rows and not in_fleet),
        "enforcement_active": await is_enforcement_active(),
        "min_trades_for_a_verdict": MIN_TRADES,
        "min_trades_to_block": MIN_TRADES_TO_BLOCK,
        "note": ("A verdict only turns negative once a coin has at least "
                 f"{MIN_TRADES} closed round trips, and a lesson can only BLOCK a buy "
                 f"when enforcement is switched on AND the coin is down over at least "
                 f"{MIN_TRADES_TO_BLOCK}. At a 75%+ win rate roughly one trade in four is "
                 "red by design; treating that as a lesson is how a profitable fleet "
                 "talks itself out of its own winners. A lesson whose coin the fleet no "
                 "longer trades is history from a retired configuration, not advice "
                 "about what to trade now."),
    }


async def backfill_from_trade_history() -> dict:
    """Build the memory from every round trip already in the ledger.

    The fleet has 82 closed trades on record. Without this the memory
    starts empty and learns nothing it already paid to find out.
    """
    from models import CryptoGridTradeHistory
    try:
        async with get_session_factory()() as db:
            result = await db.execute(select(CryptoGridTradeHistory))
            trades = list(result.scalars().all())
            existing = await db.execute(select(GridLesson))
            for row in existing.scalars().all():
                await db.delete(row)
            await db.commit()
    except Exception as e:
        return {"status": "error", "detail": str(e)}

    seen = 0
    for t in sorted(trades, key=lambda t: t.closed_at or datetime.min):
        await record_closed_trade(t.product_id, t.pnl or 0.0)
        seen += 1
    out = await get_all_lessons()
    out["status"] = "backfilled"
    out["trades_replayed"] = seen
    return out

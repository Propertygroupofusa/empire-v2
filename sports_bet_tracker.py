"""
Sports Bet Tracker — journal your bets, track ROI and win rate.

This is a TRACKER ONLY — it records bets you manually add.
It never places any real bet or moves any money.  All dollar amounts
are user-entered journal data.

API surface is exposed through routers/sports.py.
"""

import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from models import SportsBet

log = logging.getLogger("sports_bet_tracker")


async def add_bet(
    db: AsyncSession,
    sport: str,
    event: str,
    bet_type: str,
    pick: str,
    odds: str,
    stake_usd: float,
    sportsbook: Optional[str] = None,
    notes: Optional[str] = None,
    event_date: Optional[datetime] = None,
) -> SportsBet:
    """Record a new pending bet in the journal."""
    bet = SportsBet(
        sport=sport,
        event=event,
        bet_type=bet_type,
        pick=pick,
        odds=odds,
        stake_usd=stake_usd,
        result="PENDING",
        sportsbook=sportsbook,
        notes=notes,
        event_date=event_date,
    )
    db.add(bet)
    await db.commit()
    await db.refresh(bet)
    log.info(f"[BET TRACKER] Added bet #{bet.id}: {pick} on {event} @ {odds} (${stake_usd})")
    return bet


async def settle_bet(
    db: AsyncSession,
    bet_id: int,
    result: str,            # WIN / LOSS / PUSH
    payout_usd: Optional[float] = None,
) -> Optional[SportsBet]:
    """Settle a bet — record the result and payout."""
    result = result.upper()
    if result not in ("WIN", "LOSS", "PUSH"):
        raise ValueError("result must be WIN, LOSS, or PUSH")

    stmt = select(SportsBet).where(SportsBet.id == bet_id)
    row = (await db.execute(stmt)).scalar_one_or_none()
    if not row:
        return None

    row.result = result
    row.payout_usd = payout_usd
    row.settled_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(row)

    pnl = row.to_dict().get("pnl", 0) or 0
    log.info(f"[BET TRACKER] Settled bet #{bet_id}: {result} | P&L ${pnl:+.2f}")
    return row


async def get_summary(db: AsyncSession) -> Dict[str, Any]:
    """Overall stats: total staked, total returns, ROI, win rate."""
    bets_stmt = select(SportsBet)
    all_bets: List[SportsBet] = list((await db.execute(bets_stmt)).scalars().all())

    settled = [b for b in all_bets if b.result in ("WIN", "LOSS", "PUSH")]
    wins    = [b for b in settled if b.result == "WIN"]
    losses  = [b for b in settled if b.result == "LOSS"]
    pushes  = [b for b in settled if b.result == "PUSH"]

    total_staked  = sum(b.stake_usd for b in settled)
    total_returns = sum((b.payout_usd or 0) for b in wins)
    net_pnl       = total_returns - sum(b.stake_usd for b in wins) - sum(b.stake_usd for b in losses)
    roi_pct       = (net_pnl / total_staked * 100) if total_staked > 0 else 0.0
    win_rate      = (len(wins) / max(len(settled), 1)) * 100

    # Per-sport breakdown
    by_sport: Dict[str, Dict] = {}
    for b in settled:
        s = b.sport
        if s not in by_sport:
            by_sport[s] = {"bets": 0, "wins": 0, "staked": 0.0, "pnl": 0.0}
        by_sport[s]["bets"] += 1
        by_sport[s]["staked"] += b.stake_usd
        d = b.to_dict()
        by_sport[s]["pnl"] += d.get("pnl") or 0
        if b.result == "WIN":
            by_sport[s]["wins"] += 1

    for s, v in by_sport.items():
        v["win_rate"] = round(v["wins"] / max(v["bets"], 1) * 100, 1)
        v["roi_pct"]  = round(v["pnl"] / max(v["staked"], 0.01) * 100, 1)

    return {
        "total_bets":   len(all_bets),
        "pending_bets": len([b for b in all_bets if b.result == "PENDING"]),
        "settled_bets": len(settled),
        "wins":         len(wins),
        "losses":       len(losses),
        "pushes":       len(pushes),
        "win_rate_pct": round(win_rate, 1),
        "total_staked": round(total_staked, 2),
        "net_pnl":      round(net_pnl, 2),
        "roi_pct":      round(roi_pct, 1),
        "by_sport":     by_sport,
        "recent_bets":  [b.to_dict() for b in sorted(all_bets, key=lambda x: x.created_at or datetime.min, reverse=True)[:20]],
    }

"""
Sports API Router — serves all 4 sports systems under /sports.

Endpoints:
  GET  /sports/data                       — cached live scores/standings/upcoming
  POST /sports/data/refresh               — force-refresh sports data (admin)

  GET  /sports/bets                       — bet tracker summary + recent bets
  POST /sports/bets                       — add a new bet
  POST /sports/bets/{bet_id}/settle       — settle a bet (WIN/LOSS/PUSH)

  GET  /sports/content                    — list generated content
  POST /sports/content/generate           — generate content from current cache
  POST /sports/content/{id}/mark-posted   — mark a content piece as posted

  GET  /sports/trading/signals            — pending/recent trade signals
  POST /sports/trading/scan               — manual scan for new signals (admin)
  POST /sports/trading/confirm/{id}       — CONFIRM a signal and place the order
                                            (requires user to hit this endpoint
                                             — never auto-executed by the bot)

Dashboard page:
  GET  /sports/dashboard                  — returns sports_dashboard.html
"""

import os
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from admin_auth import require_admin_key
from models import SportsBet, SportsContent, SportsTradeSignal

log = logging.getLogger("sports_router")
router = APIRouter()

_dashboard_html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sports_dashboard.html")


# ─── Pydantic models ──────────────────────────────────────────────────────────

class AddBetRequest(BaseModel):
    sport: str
    event: str
    bet_type: str
    pick: str
    odds: str
    stake_usd: float
    sportsbook: Optional[str] = None
    notes: Optional[str] = None
    event_date: Optional[str] = None   # ISO string


class SettleBetRequest(BaseModel):
    result: str           # WIN / LOSS / PUSH
    payout_usd: Optional[float] = None


class MarkPostedRequest(BaseModel):
    platform: Optional[str] = None


# ─── Sports Data ──────────────────────────────────────────────────────────────

@router.get("/data")
async def get_sports_data():
    """Return cached live scores, standings, and upcoming games."""
    try:
        import sports_data_bot
        data = sports_data_bot.get_cached()
        last = sports_data_bot.get_last_refresh()
        return {"status": "ok", "last_refresh": last, "data": data}
    except Exception as e:
        log.error(f"sports data error: {e}")
        return {"status": "unavailable", "error": str(e), "data": {}}


@router.post("/data/refresh")
async def refresh_sports_data(_admin=Depends(require_admin_key)):
    """Force-refresh sports data cache (admin only)."""
    try:
        import sports_data_bot
        import asyncio
        snapshot = await sports_data_bot.refresh_all()
        import threading
        with sports_data_bot._cache_lock:
            sports_data_bot._cache.clear()
            sports_data_bot._cache.update(snapshot)
        sports_data_bot._last_refresh = datetime.now(timezone.utc)
        return {"status": "refreshed", "leagues": list(snapshot.get("leagues", {}).keys())}
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── Bet Tracker ─────────────────────────────────────────────────────────────

@router.get("/bets")
async def get_bets_summary(db: AsyncSession = Depends(get_db)):
    """Full bet tracker summary: ROI, win rate, per-sport breakdown, recent bets."""
    try:
        import sports_bet_tracker
        return await sports_bet_tracker.get_summary(db)
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/bets")
async def add_bet(req: AddBetRequest, db: AsyncSession = Depends(get_db)):
    """Add a new bet to the journal."""
    event_date = None
    if req.event_date:
        try:
            event_date = datetime.fromisoformat(req.event_date.replace("Z", "+00:00"))
        except ValueError:
            pass

    try:
        import sports_bet_tracker
        bet = await sports_bet_tracker.add_bet(
            db, req.sport, req.event, req.bet_type,
            req.pick, req.odds, req.stake_usd,
            req.sportsbook, req.notes, event_date,
        )
        return {"status": "added", "bet": bet.to_dict()}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/bets/{bet_id}/settle")
async def settle_bet(bet_id: int, req: SettleBetRequest, db: AsyncSession = Depends(get_db)):
    """Settle a bet — record WIN / LOSS / PUSH."""
    try:
        import sports_bet_tracker
        bet = await sports_bet_tracker.settle_bet(db, bet_id, req.result, req.payout_usd)
        if not bet:
            raise HTTPException(404, f"Bet #{bet_id} not found")
        return {"status": "settled", "bet": bet.to_dict()}
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── Sports Content ──────────────────────────────────────────────────────────

@router.get("/content")
async def list_content(
    sport: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
):
    """List generated sports content posts."""
    stmt = select(SportsContent)
    if sport:
        stmt = stmt.where(SportsContent.sport == sport)
    if status:
        stmt = stmt.where(SportsContent.status == status)
    stmt = stmt.order_by(desc(SportsContent.created_at)).limit(limit)
    rows = list((await db.execute(stmt)).scalars().all())
    return {"count": len(rows), "content": [r.to_dict() for r in rows]}


@router.post("/content/generate")
async def generate_content(
    platform: str = "YouTube",
    db: AsyncSession = Depends(get_db),
    _admin=Depends(require_admin_key),
):
    """Generate content from the current sports data cache."""
    try:
        import sports_data_bot, sports_content_bot
        cache = sports_data_bot.get_cached()
        if not cache:
            return {"status": "no_data", "message": "Sports cache is empty — wait for first refresh"}
        pieces = await sports_content_bot.generate_content_from_cache(db, cache, platform)
        return {"status": "generated", "count": len(pieces), "pieces": [p.to_dict() for p in pieces]}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/content/{content_id}/mark-posted")
async def mark_content_posted(
    content_id: int,
    req: MarkPostedRequest,
    db: AsyncSession = Depends(get_db),
):
    """Mark a content piece as posted."""
    try:
        import sports_content_bot
        row = await sports_content_bot.mark_posted(db, content_id, req.platform)
        if not row:
            raise HTTPException(404, f"Content #{content_id} not found")
        return {"status": "posted", "content": row.to_dict()}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── Sports Trading ──────────────────────────────────────────────────────────

@router.get("/trading/signals")
async def get_trade_signals(limit: int = 50, db: AsyncSession = Depends(get_db)):
    """List recent sports trade signals (unconfirmed and confirmed)."""
    stmt = select(SportsTradeSignal).order_by(desc(SportsTradeSignal.created_at)).limit(limit)
    rows = list((await db.execute(stmt)).scalars().all())
    return {"count": len(rows), "signals": [r.to_dict() for r in rows]}


@router.get("/trading/universe")
async def get_trading_universe():
    """Return the sports stock universe the bot watches."""
    try:
        import sports_trading_bot
        return {"universe": sports_trading_bot.SPORTS_STOCKS}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/trading/scan")
async def manual_scan(
    db: AsyncSession = Depends(get_db),
    _admin=Depends(require_admin_key),
):
    """Manually trigger a sports stock RSI scan (admin only)."""
    try:
        import sports_trading_bot
        signals = await sports_trading_bot.scan_and_signal(db)
        return {"status": "scanned", "new_signals": len(signals), "signals": signals}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/trading/confirm/{signal_id}")
async def confirm_trade_signal(signal_id: int, db: AsyncSession = Depends(get_db)):
    """
    CONFIRM a trade signal — this is the ONE place a real order can be placed.

    Per the account owner's safety rule: no trade is ever placed without
    explicit human confirmation.  This endpoint is that confirmation.

    The endpoint:
      1. Loads the signal
      2. Verifies it is still unconfirmed
      3. Places a real Alpaca market order for a small fixed qty (1 share)
      4. Marks the signal confirmed
    """
    stmt = select(SportsTradeSignal).where(SportsTradeSignal.id == signal_id)
    signal = (await db.execute(stmt)).scalar_one_or_none()
    if not signal:
        raise HTTPException(404, f"Signal #{signal_id} not found")
    if signal.confirmed:
        return {"status": "already_confirmed", "signal": signal.to_dict()}

    alpaca_key    = os.getenv("ALPACA_API_KEY", "")
    alpaca_secret = os.getenv("ALPACA_SECRET_KEY", "")
    alpaca_base   = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

    if not alpaca_key or not alpaca_secret:
        raise HTTPException(503, "Alpaca credentials not configured — cannot place order")

    # Place the order
    import httpx
    side = "buy" if signal.action == "BUY" else "sell"
    order_body = {
        "symbol": signal.ticker,
        "qty": "1",
        "side": side,
        "type": "market",
        "time_in_force": "day",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{alpaca_base}/v2/orders",
                json=order_body,
                headers={"APCA-API-KEY-ID": alpaca_key, "APCA-API-SECRET-KEY": alpaca_secret},
            )
            r.raise_for_status()
            order = r.json()
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, f"Alpaca order failed: {e.response.text}")
    except Exception as e:
        raise HTTPException(502, f"Alpaca order error: {e}")

    signal.confirmed = True
    signal.placed_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(signal)

    log.info(f"[SPORTS TRADE] Confirmed signal #{signal_id}: {side.upper()} 1 {signal.ticker} | order_id={order.get('id')}")
    return {"status": "confirmed", "order": order, "signal": signal.to_dict()}


# ─── Dashboard HTML ───────────────────────────────────────────────────────────

@router.get("/dashboard", response_class=HTMLResponse)
async def sports_dashboard():
    """Serve the sports dashboard HTML page."""
    paths = [
        _dashboard_html_path,
        "/app/sports_dashboard.html",
        "sports_dashboard.html",
    ]
    for p in paths:
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return HTMLResponse(f.read())
    raise HTTPException(404, "sports_dashboard.html not found")

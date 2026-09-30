"""Paper test of daily_stock_analysis picks. See dsa_paper.py for the rules.

POST /api/trading-dashboard/dsa-paper/webhook  - DSA's custom webhook posts its report here.
                               Bearer token must equal DSA_WEBHOOK_TOKEN;
                               unset -> refused (fails closed).
GET  /api/trading-dashboard/dsa-paper/status   - admin key. The paper account vs live Alpaca
                               and SPY over the same window.

Reads live prices only. There is no order call anywhere in this file.
"""
import hmac
import os
from datetime import datetime

import aiohttp
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select, desc

import dsa_paper
from admin_auth import require_admin_key
from database import get_session_factory
from models import DsaPaperState, DsaPaperLog

router = APIRouter()
DATA_URL = "https://data.alpaca.markets/v2/stocks"


def _alpaca_headers():
    return {"APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", ""),
            "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY", "")}


async def _latest_prices(session, tickers):
    """{ticker: last trade price}. A ticker that cannot be read is simply
    absent - dsa_paper skips it rather than using a guess."""
    out = {}
    for t in sorted(set(tickers)):
        try:
            async with session.get(f"{DATA_URL}/{t}/trades/latest?feed=iex",
                                   headers=_alpaca_headers(), timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    px = float(((await r.json()).get("trade") or {}).get("p") or 0)
                    if px > 0:
                        out[t] = px
        except Exception:
            pass
    return out


async def _alpaca_equity(session):
    base = os.getenv("ALPACA_BASE_URL", "https://api.alpaca.markets")
    try:
        async with session.get(f"{base}/v2/account", headers=_alpaca_headers(),
                               timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200:
                return float((await r.json()).get("equity") or 0) or None
    except Exception:
        pass
    return None


def _check_token(authorization):
    expected = os.getenv("DSA_WEBHOOK_TOKEN", "")
    if not expected:
        raise HTTPException(503, "DSA_WEBHOOK_TOKEN is not set - webhook refused")
    got = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(got, expected):
        raise HTTPException(401, "bad token")


@router.post("/dsa-paper/webhook")
async def dsa_webhook(request: Request, authorization: str = Header(None)):
    _check_token(authorization)
    try:
        body = await request.json()
    except Exception:
        body = {}
    text = (body.get("content") or body.get("text") or "") if isinstance(body, dict) else ""
    signals = dsa_paper.parse_report(text)
    now = datetime.utcnow()
    now_iso = now.isoformat() + "Z"

    async with get_session_factory()() as db:
        row = await db.get(DsaPaperState, 1)
        needed = {t for t, a, _ in signals if a in dsa_paper.BUY_ACTIONS | dsa_paper.SELL_ACTIONS}
        if row is not None:
            needed |= set((row.state or {}).get("positions", {}))
        async with aiohttp.ClientSession() as session:
            prices = await _latest_prices(session, needed | ({"SPY"} if row is None else set()))
            alpaca_eq = await _alpaca_equity(session) if row is None else None
        if row is None:
            row = DsaPaperState(id=1, state=dsa_paper.new_account(now_iso),
                                spy_start_price=prices.get("SPY"), alpaca_start_equity=alpaca_eq)
            db.add(row)
        for t, a, s in signals:
            db.add(DsaPaperLog(at=now, kind="signal", ticker=t, action=a, score=s))
        acct, trades, skipped = dsa_paper.apply_signals(row.state, signals, prices, now_iso)
        for tr in trades:
            db.add(DsaPaperLog(at=now, kind="trade", ticker=tr["ticker"], action=tr["side"],
                               qty=tr["qty"], price=tr["price"], pnl=tr["pnl"], note=tr["reason"]))
        for t, why in skipped:
            db.add(DsaPaperLog(at=now, kind="skip", ticker=t, note=why))
        row.state = acct
        row.updated_at = now
        await db.commit()
    return {"signals": len(signals), "trades": len(trades), "skipped": skipped}


@router.get("/dsa-paper/status", dependencies=[Depends(require_admin_key)])
async def dsa_status():
    async with get_session_factory()() as db:
        row = await db.get(DsaPaperState, 1)
        if row is None:
            return {"started": False, "note": "No report received yet."}
        logs = (await db.execute(select(DsaPaperLog).where(DsaPaperLog.kind != "signal")
                                 .order_by(desc(DsaPaperLog.id)).limit(40))).scalars().all()
    acct = row.state
    async with aiohttp.ClientSession() as session:
        prices = await _latest_prices(session, set(acct["positions"]) | {"SPY"})
        alpaca_eq = await _alpaca_equity(session)
    eq = dsa_paper.equity(acct, prices)
    start = acct["start_capital"]

    def pct(now, then):
        return round((now / then - 1) * 100, 2) if now and then else None

    return {
        "started": True,
        "started_at": acct["started_at"],
        "paper": {"equity": round(eq, 2) if eq is not None else None,
                  "cash": round(acct["cash"], 2),
                  "return_pct": pct(eq, start),
                  "positions": [{"ticker": t, "qty": p["qty"], "entry": round(p["entry"], 4),
                                 "price": prices.get(t),
                                 "pnl": round(p["qty"] * (prices[t] - p["entry"]), 2) if prices.get(t) else None}
                                for t, p in acct["positions"].items()]},
        "spy_return_pct": pct(prices.get("SPY"), row.spy_start_price),
        # Deposits and withdrawals move this too - it is the account, not the bot.
        "alpaca_account_return_pct": pct(alpaca_eq, row.alpaca_start_equity),
        "recent": [{"at": l.at.isoformat() + "Z", "kind": l.kind, "ticker": l.ticker,
                    "action": l.action, "qty": l.qty, "price": l.price, "pnl": l.pnl,
                    "note": l.note} for l in logs],
    }

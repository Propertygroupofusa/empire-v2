"""
Sports Trading Bot — generates BUY/SELL signals for sports-related stocks.

Sports-economy tickers tracked:
  DKNG  — DraftKings (sports betting)
  PENN  — PENN Entertainment (sports betting / Barstool)
  MGM   — MGM Resorts (sports books)
  NKE   — Nike (sports gear)
  ADDYY — Adidas ADR (sports gear)
  LYV   — Live Nation / Ticketmaster (sports events)
  MSGS  — Madison Square Garden Sports
  BALY  — Bally's (sports betting)
  ESPN+ — Disney (DIS) carries ESPN; flagged as a secondary

CRITICAL SAFETY RULE (hardcoded, not env-var):
  This bot NEVER places a real order on its own.
  Every signal is written to sports_trade_signals table with confirmed=False.
  A human must call POST /sports/trading/confirm/{signal_id} to actually trade.
  Even then, the /confirm endpoint checks Alpaca paper/live mode before executing.

Uses the same RSI calculation and Alpaca integration as prop_bot.py.
"""

import os
import logging
import time
import asyncio
import threading
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

log = logging.getLogger("sports_trading_bot")

SCAN_INTERVAL_SECONDS = int(os.getenv("SPORTS_TRADE_SCAN_SECONDS", "900"))  # 15 min
RSI_PERIOD = 14
RSI_OVERSOLD  = 35   # buy signal threshold (a bit looser than prop_bot's 30)
RSI_OVERBOUGHT = 68  # sell signal threshold

# Sports economy universe
SPORTS_STOCKS: Dict[str, Dict] = {
    "DKNG":  {"name": "DraftKings",          "sector": "sports_betting"},
    "PENN":  {"name": "PENN Entertainment",  "sector": "sports_betting"},
    "MGM":   {"name": "MGM Resorts",         "sector": "sports_betting"},
    "NKE":   {"name": "Nike",                "sector": "sports_gear"},
    "LYV":   {"name": "Live Nation",         "sector": "sports_events"},
    "MSGS":  {"name": "MSG Sports",          "sector": "sports_franchise"},
    "BALY":  {"name": "Bally's Corp",        "sector": "sports_betting"},
    "DIS":   {"name": "Disney (ESPN+)",      "sector": "sports_media"},
    "NFLX":  {"name": "Netflix (NFL deals)", "sector": "sports_media"},
}

ALPACA_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE   = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
ALPACA_HEADERS = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}


async def _fetch_bars(ticker: str, limit: int = 50) -> List[float]:
    """Fetch recent 5-minute bar closes from Alpaca for RSI calc."""
    import httpx
    url = f"{ALPACA_BASE}/v2/stocks/{ticker}/bars"
    params = {"timeframe": "5Min", "limit": limit, "feed": "iex"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, headers=ALPACA_HEADERS, params=params)
            r.raise_for_status()
            bars = r.json().get("bars", [])
            return [b["c"] for b in bars]
    except Exception as e:
        log.warning(f"bars fetch failed {ticker}: {e}")
        return []


def _calc_rsi(closes: List[float], period: int = RSI_PERIOD) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 2)


async def _latest_price(ticker: str) -> Optional[float]:
    import httpx
    url = f"{ALPACA_BASE}/v2/stocks/{ticker}/trades/latest"
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(url, headers=ALPACA_HEADERS)
            r.raise_for_status()
            return r.json().get("trade", {}).get("p")
    except Exception as e:
        log.warning(f"price fetch failed {ticker}: {e}")
        return None


async def scan_and_signal(db) -> List[Dict[str, Any]]:
    """Scan all sports stocks, compute RSI, emit BUY/SELL signals (no orders placed)."""
    from models import SportsTradeSignal
    from sqlalchemy import select

    new_signals = []

    for ticker, info in SPORTS_STOCKS.items():
        if not ALPACA_KEY:
            log.debug("No Alpaca key — skipping live scan")
            break

        closes = await _fetch_bars(ticker)
        rsi = _calc_rsi(closes)
        price = await _latest_price(ticker) if closes else None

        if rsi is None:
            continue

        action = None
        reason = None

        if rsi <= RSI_OVERSOLD:
            action = "BUY"
            reason = (
                f"{info['name']} ({ticker}) RSI={rsi} is oversold (≤{RSI_OVERSOLD}). "
                f"Sports-sector momentum dip — potential entry point."
            )
        elif rsi >= RSI_OVERBOUGHT:
            action = "SELL"
            reason = (
                f"{info['name']} ({ticker}) RSI={rsi} is overbought (≥{RSI_OVERBOUGHT}). "
                f"Consider taking profits or trimming position."
            )

        if action:
            signal = SportsTradeSignal(
                ticker=ticker,
                action=action,
                reason=reason,
                price_at_signal=price,
                rsi=rsi,
                confirmed=False,
            )
            db.add(signal)
            new_signals.append(signal.to_dict() if hasattr(signal, "to_dict") else {
                "ticker": ticker, "action": action, "reason": reason,
                "price_at_signal": price, "rsi": rsi,
            })
            log.info(f"[SPORTS TRADE] Signal: {action} {ticker} @ ${price} RSI={rsi}")

    if new_signals:
        await db.commit()
    return new_signals


def _run_loop():
    """Background thread: scan sports stocks every SCAN_INTERVAL_SECONDS."""
    log.info(f"[SPORTS TRADING BOT] Starting — scanning every {SCAN_INTERVAL_SECONDS}s")
    while True:
        try:
            from database import AsyncSessionLocal
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            async def _do():
                async with AsyncSessionLocal() as db:
                    return await scan_and_signal(db)

            signals = loop.run_until_complete(_do())
            loop.close()
            if signals:
                log.info(f"[SPORTS TRADING BOT] {len(signals)} new signal(s) this cycle")
        except Exception as e:
            log.error(f"[SPORTS TRADING BOT] Scan error: {e}")
        time.sleep(SCAN_INTERVAL_SECONDS)


def run():
    """Entry point for main.py threading.Thread(target=sports_trading_bot.run)"""
    _run_loop()

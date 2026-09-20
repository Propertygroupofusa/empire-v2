"""Crypto scalper — the revival of bot_2_crypto_scalper.py, on Coinbase.

WHY THE ORIGINAL WAS RETIRED (main.py, "bot_2_crypto_scalper.py retired")
--------------------------------------------------------------------------
Four separate defects, all on the record:

  1. It traded crypto through ALPACA, which is blocked for this account's
     state. Every order it ever placed failed silently: 0 wins, 0 losses,
     ever. It was not a losing strategy - it was a strategy that never
     executed.
  2. Its state lived in bot2_state.json on Railway's ephemeral filesystem,
     so it reset to defaults on every redeploy. PR #109 fixed exactly this
     for the other two bots; this one never got the fix.
  3. It sized every order off the SAME shared Alpaca balance prop_bot.py
     trades stocks from, with no coordination between them - the two could
     spend the same dollar twice.
  4. The retirement note's own conclusion: "Crypto trading belongs on
     Coinbase - Alpaca is for stocks only."

This revival fixes all four. The STRATEGY is preserved verbatim from the
original (see get_signal below) - what changed is the venue, where state
lives, and whose money it spends.

THE ARITHMETIC YOU SHOULD SEE BEFORE ARMING THIS
------------------------------------------------
At crypto_coinbase_bot.TAKER_FEE_RATE (0.6% per side, 1.2% round trip)
and the original's own exits:

    stop_loss   -0.8%  ->  -2.00% net after fees
    take_profit +2.4%  ->  +1.20% net after fees
    break-even win rate = 62.5%

A scalper taking many small round trips pays that spread every time. This
is why the bot ships OFF, and why "on" has two separate switches.

  SCALPER_ENABLED  - default false. Nothing runs at all.
  SCALPER_LIVE     - default false. The bot computes every signal and
                     records every decision it WOULD have made, and places
                     no orders. This is how you test a backtest claim
                     forward on live data at zero risk.

Turning the first on without the second is the intended way to run this.
"""
import asyncio
import logging
import os
from datetime import datetime, timezone

import aiohttp
from sqlalchemy import select

from database import AsyncSessionLocal
from models import BotPosition

# Venue integration is reused, not reimplemented: crypto_coinbase_bot
# already holds the working Coinbase JWT auth and order placement. Copying
# it would mean two auth paths to keep in sync, and the one that drifted
# would fail silently - which is precisely how the original bot died.
from crypto_coinbase_bot import (
    TAKER_FEE_RATE,
    _to_product_id,
    place_order,
)

log = logging.getLogger("crypto_scalper")

BOT_NAME = "crypto_scalper"

# ── SWITCHES ─────────────────────────────────────────────────
ENABLED = os.getenv("SCALPER_ENABLED", "false").strip().lower() == "true"
LIVE = os.getenv("SCALPER_LIVE", "false").strip().lower() == "true"

# Its own capital envelope, NOT a shared broker balance. Defect 3 was that
# the original sized orders off the same Alpaca account prop_bot.py was
# trading; there is no amount of care in this file that fixes that if the
# number comes from a shared balance, so it comes from here instead and
# defaults to zero.
CAPITAL_USD = float(os.getenv("SCALPER_CAPITAL_USD", "0"))

CONFIG = {
    "daily_target_pct": 2.0,
    "max_daily_loss_pct": 3.0,
    "stop_loss_pct": 0.8,
    "take_profit_pct": 2.4,
    "trailing_stop_pct": 1.2,
    "max_alloc_pct": 25.0,
    "max_positions": 4,
    "max_trades_day": 10,
    "cycle_minutes": 15,
}

# Preserved from the original, minus the pairs Coinbase does not list the
# same way. Kept as the original's tiering so the strategy is unchanged.
CRYPTOS = {
    "BTC/USD":  {"tier": 1, "name": "Bitcoin",   "vol_threshold": 1.5},
    "ETH/USD":  {"tier": 1, "name": "Ethereum",  "vol_threshold": 1.5},
    "SOL/USD":  {"tier": 1, "name": "Solana",    "vol_threshold": 2.0},
    "AVAX/USD": {"tier": 2, "name": "Avalanche", "vol_threshold": 2.0},
    "DOGE/USD": {"tier": 2, "name": "Dogecoin",  "vol_threshold": 2.5},
    "LINK/USD": {"tier": 2, "name": "Chainlink", "vol_threshold": 2.0},
}

COINBASE_CANDLES = "https://api.coinbase.com/api/v3/brokerage/market/products/{pid}/candles"
GRANULARITY = "FIFTEEN_MINUTE"


# ── INDICATORS (verbatim from the original) ──────────────────
def sma(prices, n):
    return sum(prices[-n:]) / n if len(prices) >= n else None


def rsi(prices, n=14):
    if len(prices) < n + 1:
        return 50.0
    gains, losses = [], []
    for i in range(-n, 0):
        d = prices[i] - prices[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag, al = sum(gains) / n, sum(losses) / n
    if al == 0:
        return 100.0
    rs = ag / al
    return 100 - (100 / (1 + rs))


def bollinger(prices, n=20, k=2):
    if len(prices) < n:
        return None, None, None
    window = prices[-n:]
    mid = sum(window) / n
    var = sum((p - mid) ** 2 for p in window) / n
    sd = var ** 0.5
    return mid - k * sd, mid, mid + k * sd


def volume_spike(volumes, n=10):
    if len(volumes) < n + 1:
        return False
    avg = sum(volumes[-n - 1:-1]) / n
    return avg > 0 and volumes[-1] > avg * 1.5


def get_signal(closes, highs, lows, volumes, in_pos, entry_px, peak_px, symbol):
    """The original bot's decision function, unchanged.

    Preserved deliberately. The point of a revival is to find out whether
    THIS strategy works when its orders actually reach an exchange - the
    original never got to answer that. Changing the rules at the same time
    as changing the venue would make the result uninterpretable.
    """
    if len(closes) < 20:
        return "HOLD", "insufficient data"

    px = closes[-1]
    r = rsi(closes)
    bb_lo, bb_mid, bb_hi = bollinger(closes)
    s20 = sma(closes, 20)
    s9 = sma(closes, 9)
    vol_sp = volume_spike(volumes)

    if in_pos and entry_px > 0:
        pnl = ((px - entry_px) / entry_px) * 100
        if pnl <= -CONFIG["stop_loss_pct"]:
            return "SELL", f"stop_loss {pnl:.2f}%"
        if pnl >= CONFIG["take_profit_pct"]:
            return "SELL", f"take_profit {pnl:.2f}%"
        if peak_px > entry_px:
            trail = ((px - peak_px) / peak_px) * 100
            if trail <= -CONFIG["trailing_stop_pct"]:
                return "SELL", f"trailing_stop {trail:.2f}%"
        if r > 75 and bb_hi and px >= bb_hi:
            return "SELL", f"overbought rsi={r:.0f}"
        return "HOLD", f"holding pnl={pnl:+.2f}%"

    buy_score = 0
    reasons = []
    if r < 45:
        buy_score += 2
        reasons.append(f"rsi_oversold={r:.0f}")
    elif r < 55:
        buy_score += 1
        reasons.append(f"rsi_low={r:.0f}")
    # The one deliberate deviation from the original, and it is a bug fix
    # rather than a strategy change.
    #
    # The original awarded +2 whenever px <= bb_lo * 1.005. When volatility
    # collapses, sd -> 0 and the bands collapse onto the mean, so bb_lo
    # == mid == px and that test is trivially true: the bot buys BECAUSE
    # there is no range, which is the opposite of what "price is at the
    # lower band" is meant to detect. Since +2 alone clears the score>=2
    # entry rule, a dead-quiet market produced an unconditional BUY.
    #
    # Measured on a flat series: rsi=100, bands lo=mid=hi, signal = BUY
    # "score=2 | at_bb_lower". This is not only theoretical - the 0.5%
    # tolerance exceeds the whole band whenever sd/price < 0.25%, which is
    # an ordinary quiet stretch on 15-minute candles.
    #
    # So the band must be at least as wide as the tolerance being applied
    # to it before "at the lower band" means anything.
    band_width_pct = ((bb_hi - bb_lo) / bb_mid * 100) if (bb_lo and bb_mid) else 0.0
    if bb_lo and band_width_pct >= 0.5 and px <= bb_lo * 1.005:
        buy_score += 2
        reasons.append("at_bb_lower")
    if s9 and s20 and s9 > s20 and px > s9:
        buy_score += 1
        reasons.append("uptrend")
    if vol_sp:
        buy_score += 1
        reasons.append("vol_spike")

    if buy_score >= 2:
        return "BUY", f"score={buy_score} | {' | '.join(reasons)}"
    return "HOLD", f"score={buy_score} | {' | '.join(reasons)}"


# ── MARKET DATA ──────────────────────────────────────────────
async def fetch_candles(session, symbol, limit=60):
    """Unauthenticated Coinbase candles. Returns (closes, highs, lows, volumes).

    Returns empty lists rather than raising or substituting zeros: a
    fabricated candle would produce a real order. get_signal treats fewer
    than 20 closes as "insufficient data" and holds.
    """
    pid = _to_product_id(symbol)
    now = int(datetime.now(timezone.utc).timestamp())
    start = now - (limit * 15 * 60)
    url = COINBASE_CANDLES.format(pid=pid)
    params = {"start": str(start), "end": str(now), "granularity": GRANULARITY}
    try:
        async with session.get(url, params=params, timeout=15) as r:
            if r.status != 200:
                log.warning(f"[SCALPER] candles {symbol}: HTTP {r.status}")
                return [], [], [], []
            data = await r.json()
    except Exception as e:
        log.warning(f"[SCALPER] candles {symbol} failed: {e}")
        return [], [], [], []

    rows = data.get("candles", [])
    if not rows:
        return [], [], [], []
    # Coinbase returns newest-first; indicators expect oldest-first.
    rows = sorted(rows, key=lambda c: int(c["start"]))
    closes = [float(c["close"]) for c in rows]
    highs = [float(c["high"]) for c in rows]
    lows = [float(c["low"]) for c in rows]
    volumes = [float(c["volume"]) for c in rows]
    return closes, highs, lows, volumes


# ── STATE (database, not a JSON file on an ephemeral disk) ───
async def load_positions() -> dict:
    try:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(BotPosition).where(BotPosition.bot == BOT_NAME)
            )).scalars().all()
            return {
                r.symbol: {"entry": r.entry_price, "qty": r.qty,
                           "peak": r.peak_pct or r.entry_price}
                for r in rows
            }
    except Exception as e:
        log.error(f"[SCALPER] could not load positions: {e}")
        return {}


async def save_position(symbol, entry, qty, peak):
    try:
        async with AsyncSessionLocal() as db:
            db.add(BotPosition(bot=BOT_NAME, symbol=symbol, side="long",
                               entry_price=entry, qty=qty, peak_pct=peak))
            await db.commit()
    except Exception as e:
        log.error(f"[SCALPER] could not persist {symbol}: {e}")


async def update_peak(symbol, peak):
    try:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(BotPosition).where(BotPosition.bot == BOT_NAME,
                                          BotPosition.symbol == symbol)
            )).scalars().all()
            for r in rows:
                r.peak_pct = peak
            await db.commit()
    except Exception as e:
        log.error(f"[SCALPER] could not update peak for {symbol}: {e}")


async def delete_position(symbol):
    try:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(BotPosition).where(BotPosition.bot == BOT_NAME,
                                          BotPosition.symbol == symbol)
            )).scalars().all()
            for r in rows:
                await db.delete(r)
            await db.commit()
    except Exception as e:
        log.error(f"[SCALPER] could not delete {symbol}: {e}")


# ── CYCLE ────────────────────────────────────────────────────
# What the last cycle decided, for GET /api/trading-dashboard/scalper-status.
# In-memory and intentionally so: it is a view of the current cycle, not
# state the bot's correctness depends on. Everything load-bearing is in the
# database.
LAST_CYCLE = {
    "ran_at": None,
    "enabled": ENABLED,
    "live": LIVE,
    "capital_usd": CAPITAL_USD,
    "break_even_win_rate_pct": None,
    "signals": [],
    "positions": [],
    "error": None,
}


def _break_even_win_rate() -> float:
    """Recomputed from the live fee constant, never hardcoded, so it cannot
    silently go stale if TAKER_FEE_RATE changes."""
    rt = TAKER_FEE_RATE * 2 * 100
    loss = CONFIG["stop_loss_pct"] + rt
    win = CONFIG["take_profit_pct"] - rt
    if win <= 0:
        return 100.0
    return loss / (loss + win) * 100


async def run_cycle():
    positions = await load_positions()
    signals = []

    async with aiohttp.ClientSession() as session:
        for symbol in CRYPTOS:
            closes, highs, lows, volumes = await fetch_candles(session, symbol)
            if not closes:
                signals.append({"symbol": symbol, "action": "SKIP",
                                "reason": "no candle data", "price": None})
                continue

            px = closes[-1]
            pos = positions.get(symbol)
            in_pos = pos is not None
            entry = pos["entry"] if in_pos else 0.0
            peak = max(pos["peak"], px) if in_pos else 0.0

            action, reason = get_signal(closes, highs, lows, volumes,
                                        in_pos, entry, peak, symbol)

            if in_pos and peak > pos["peak"]:
                await update_peak(symbol, peak)

            signals.append({
                "symbol": symbol, "action": action, "reason": reason,
                "price": px, "rsi": round(rsi(closes), 1),
                "in_position": in_pos,
                "unrealized_pct": round(((px - entry) / entry) * 100, 2) if in_pos and entry else None,
            })

            # ── the only place money can move ──────────────────
            if not LIVE:
                continue

            if action == "BUY" and len(positions) < CONFIG["max_positions"]:
                alloc = CAPITAL_USD * (CONFIG["max_alloc_pct"] / 100)
                if alloc <= 0:
                    log.warning("[SCALPER] BUY signal but SCALPER_CAPITAL_USD is 0 - skipping")
                    continue
                qty = alloc / px
                ok = await place_order(session, symbol, "buy", qty, px)
                if ok:
                    await save_position(symbol, px, qty, px)
                    positions[symbol] = {"entry": px, "qty": qty, "peak": px}

            elif action == "SELL" and in_pos:
                ok = await place_order(session, symbol, "sell", pos["qty"], px)
                if ok:
                    await delete_position(symbol)
                    positions.pop(symbol, None)

    LAST_CYCLE.update({
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "enabled": ENABLED,
        "live": LIVE,
        "capital_usd": CAPITAL_USD,
        "break_even_win_rate_pct": round(_break_even_win_rate(), 1),
        "signals": signals,
        "positions": [{"symbol": s, **v} for s, v in positions.items()],
        "error": None,
    })
    mode = "LIVE" if LIVE else "PAPER"
    log.info(f"[SCALPER] cycle done ({mode}) - {len(signals)} pairs, "
             f"{len(positions)} open")


async def run_forever():
    if not ENABLED:
        log.info("[SCALPER] disabled (SCALPER_ENABLED is not true) - not starting")
        return
    if not LIVE:
        log.info("[SCALPER] PAPER MODE - signals computed and recorded, no orders placed. "
                 "Set SCALPER_LIVE=true to arm.")
    else:
        log.warning(f"[SCALPER] LIVE - real Coinbase orders, ${CAPITAL_USD:.2f} envelope, "
                    f"break-even win rate {_break_even_win_rate():.1f}%")

    while True:
        try:
            await run_cycle()
        except Exception as e:
            log.error(f"[SCALPER] cycle error: {e}")
            LAST_CYCLE["error"] = str(e)
        await asyncio.sleep(CONFIG["cycle_minutes"] * 60)

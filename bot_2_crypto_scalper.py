#!/usr/bin/env python3
"""
BOT #2 — PURE CRYPTO SCALPER
24/7 BTC + ETH + SOL + AVAX + DOGE

Strategy: Scalp small moves every 15 minutes
Target: 1.0-2.0% per trade, 6-10 trades/day
Stop loss: 0.8% (tighter than Bot #1)
Take profit: 2.4% (3:1 ratio)
Leverage: None — spot only
Runs: 24/7 — crypto never sleeps

COMPOUNDING: Every profit reinvested automatically
No hard stop — keeps compounding indefinitely
$980 → $1k → $5k → $10k → $100k → $500k → $1M+
"""
import os
import json
import time
import math
import logging
import schedule
import urllib.request
import urllib.error
from datetime import datetime, timezone
from statistics import mean, stdev

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── LOGGING ─────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot2_crypto.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("bot2")

# ── CREDENTIALS ─────────────────────────────────────────────
API_KEY = os.getenv("ALPACA_API_KEY", "")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
LIVE_URL = "https://api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"

# ── CONFIG ──────────────────────────────────────────────────
CONFIG = {
    "starting_capital": 980.0,  # Live account capital
    "target_portfolio": 10_000_000.0,  # No hard stop — keep compounding to $10M+
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

# ── CRYPTO PAIRS — 24/7 ─────────────────────────────────────
CRYPTOS = {
    "BTC/USD": {"tier": 1, "name": "Bitcoin", "vol_threshold": 1.5},
    "ETH/USD": {"tier": 1, "name": "Ethereum", "vol_threshold": 1.5},
    "SOL/USD": {"tier": 1, "name": "Solana", "vol_threshold": 2.0},
    "AVAX/USD": {"tier": 2, "name": "Avalanche", "vol_threshold": 2.0},
    "DOGE/USD": {"tier": 2, "name": "Dogecoin", "vol_threshold": 2.5},
    "LINK/USD": {"tier": 2, "name": "Chainlink", "vol_threshold": 2.0},
}

# ── COMPOUNDING MILESTONES ───────────────────────────────────
MILESTONES = [
    {"balance": 500, "alloc": 20, "max_pos": 3, "label": "🌱 Starting"},
    {"balance": 1_000, "alloc": 22, "max_pos": 3, "label": "🌿 $1k hit!"},
    {"balance": 2_500, "alloc": 23, "max_pos": 4, "label": "🌳 Growing"},
    {"balance": 5_000, "alloc": 25, "max_pos": 4, "label": "💪 Serious"},
    {"balance": 10_000, "alloc": 25, "max_pos": 4, "label": "🔥 5 Figures!"},
    {"balance": 25_000, "alloc": 25, "max_pos": 4, "label": "⚡ Quarter!"},
    {"balance": 50_000, "alloc": 25, "max_pos": 4, "label": "🚀 Halfway!"},
    {"balance": 100_000, "alloc": 25, "max_pos": 4, "label": "🏆 $100k!"},
    {"balance": 250_000, "alloc": 25, "max_pos": 4, "label": "💎 Quarter Million!"},
    {"balance": 500_000, "alloc": 25, "max_pos": 4, "label": "🌟 Half Million!"},
    {"balance": 1_000_000, "alloc": 25, "max_pos": 4, "label": "👑 MILLIONAIRE!"},
]


def get_milestone(balance):
    current = MILESTONES[0]
    for m in MILESTONES:
        if balance >= m["balance"]:
            current = m
    return current


def days_to_target(balance, daily_pct=2.0, target=100_000):
    if balance <= 0:
        return 999
    try:
        return int(math.ceil(math.log(target / balance) / math.log(1 + daily_pct / 100)))
    except:
        return 999


# ── API HELPERS ──────────────────────────────────────────────
def api_call(method, endpoint, body=None, live=False):
    url = (LIVE_URL if live else BASE_URL) + endpoint
    headers = {
        "APCA-API-KEY-ID": API_KEY,
        "APCA-API-SECRET-KEY": SECRET_KEY,
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403):
                return None
            time.sleep(2**attempt)
        except Exception as e:
            time.sleep(2**attempt)
    return None


def get_crypto_prices(symbol, limit=50):
    """Get crypto OHLCV bars — 15min timeframe"""
    sym = symbol.replace("/", "%2F")
    url = f"{DATA_URL}/v1beta3/crypto/us/bars?symbols={sym}&timeframe=15Min&limit={limit}"
    headers = {
        "APCA-API-KEY-ID": API_KEY,
        "APCA-API-SECRET-KEY": SECRET_KEY,
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
            bars = data.get("bars", {}).get(symbol, [])
            closes = [b["c"] for b in bars]
            highs = [b["h"] for b in bars]
            lows = [b["l"] for b in bars]
            volumes = [b["v"] for b in bars]
            return closes, highs, lows, volumes
    except Exception as e:
        log.warning(f"  Price error {symbol}: {e}")
        return [], [], [], []


def get_account():
    return api_call("GET", "/v2/account")


def place_buy(symbol, notional, live=False):
    body = {
        "symbol": symbol,
        "notional": str(round(notional, 2)),
        "side": "buy",
        "type": "market",
        "time_in_force": "gtc",
    }
    d = api_call("POST", "/v2/orders", body, live)
    if d and d.get("id"):
        log.info(f"  ✅ BUY {symbol} ${notional:.2f}")
        return True
    return False


def place_sell(symbol, live=False):
    body = {
        "symbol": symbol,
        "qty": "100%",
        "side": "sell",
        "type": "market",
        "time_in_force": "gtc",
    }
    d = api_call("POST", "/v2/orders", body, live)
    if d and d.get("id"):
        log.info(f"  ✅ SELL {symbol}")
        return True
    return False


# ── INDICATORS ───────────────────────────────────────────────
def sma(prices, n):
    if len(prices) < n:
        return None
    return mean(prices[-n:])


def rsi(prices, n=14):
    if len(prices) < n + 1:
        return 50
    gains = [max(prices[i] - prices[i - 1], 0) for i in range(1, len(prices))]
    losses = [max(prices[i - 1] - prices[i], 0) for i in range(1, len(prices))]
    ag = mean(gains[-n:])
    al = mean(losses[-n:])
    if al == 0:
        return 100
    return 100 - (100 / (1 + ag / al))


def bollinger(prices, n=20, k=2):
    if len(prices) < n:
        return None, None, None
    sl = prices[-n:]
    m = mean(sl)
    sd = stdev(sl) if len(sl) > 1 else 0
    return m - k * sd, m, m + k * sd


def volume_spike(volumes, n=10):
    if len(volumes) < n + 1:
        return False
    avg = mean(volumes[-n - 1 : -1])
    cur = volumes[-1]
    return cur > avg * 1.5 if avg > 0 else False


# ── SIGNAL ───────────────────────────────────────────────────
def get_signal(closes, highs, lows, volumes, in_pos, entry_px, peak_px, symbol):
    if len(closes) < 20:
        return "HOLD", "insufficient data"

    px = closes[-1]
    r = rsi(closes)
    bb_lo, bb_mid, bb_hi = bollinger(closes)
    s20 = sma(closes, 20)
    s9 = sma(closes, 9)
    vol_spike = volume_spike(volumes)

    # ── EXIT LOGIC ───────────────────────────────────────────
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

    # ── ENTRY LOGIC ──────────────────────────────────────────
    buy_score = 0
    reasons = []

    if r < 45:
        buy_score += 2
        reasons.append(f"rsi_oversold={r:.0f}")
    elif r < 55:
        buy_score += 1
        reasons.append(f"rsi_low={r:.0f}")

    if bb_lo and px <= bb_lo * 1.005:
        buy_score += 2
        reasons.append("at_bb_lower")

    if s9 and s20 and s9 > s20 and px > s9:
        buy_score += 1
        reasons.append("uptrend")

    if vol_spike:
        buy_score += 1
        reasons.append("vol_spike")

    if buy_score >= 2:
        return "BUY", f"score={buy_score} | {' | '.join(reasons)}"
    return "HOLD", f"score={buy_score}/3 | {' | '.join(reasons)}"


# ── STATE ────────────────────────────────────────────────────
class State:
    def __init__(self):
        self.positions = {}
        self.trades_today = 0
        self.wins = 0
        self.losses = 0
        self.day = 1
        self.start_pf = CONFIG["starting_capital"]
        self.day_start_pf = CONFIG["starting_capital"]
        self.peak_pf = CONFIG["starting_capital"]
        self.is_live = False
        self.load()

    def load(self):
        try:
            d = json.load(open("bot2_state.json"))
            self.day = d.get("day", 1)
            self.wins = d.get("wins", 0)
            self.losses = d.get("losses", 0)
            self.is_live = d.get("is_live", False)
            self.start_pf = d.get("start_pf", CONFIG["starting_capital"])
            self.peak_pf = d.get("peak_pf", CONFIG["starting_capital"])
            log.info(f"📂 Loaded: Day {self.day} W:{self.wins} L:{self.losses}")
        except:
            pass

    def save(self):
        json.dump(
            {
                "day": self.day,
                "wins": self.wins,
                "losses": self.losses,
                "is_live": self.is_live,
                "start_pf": self.start_pf,
                "peak_pf": self.peak_pf,
            },
            open("bot2_state.json", "w"),
            indent=2,
        )

    def new_day(self, pf):
        self.day += 1
        self.trades_today = 0
        self.day_start_pf = pf
        self.save()

    def can_trade(self):
        return self.trades_today < CONFIG["max_trades_day"]

    def daily_loss_hit(self, pf):
        if self.day_start_pf <= 0:
            return False
        return (
            ((self.day_start_pf - pf) / self.day_start_pf) * 100
            >= CONFIG["max_daily_loss_pct"]
        )

    def win_rate(self):
        total = self.wins + self.losses
        return (self.wins / total * 100) if total > 0 else 0


state = State()


# ── MAIN CYCLE ───────────────────────────────────────────────
def run_cycle():
    acct = get_account()
    if not acct:
        log.error("❌ Cannot connect — check .env keys")
        return

    pf = float(acct.get("portfolio_value", CONFIG["starting_capital"]))
    ms = get_milestone(pf)
    days = days_to_target(pf, CONFIG["daily_target_pct"])
    mode = "🔴 LIVE" if state.is_live else "📄 PAPER"

    if pf > state.peak_pf:
        state.peak_pf = pf
        log.info(f"🆕 New all-time high: ${pf:,.2f}!")

    log.info(f"\n{'='*55}")
    log.info(f"⏱ BOT #2 CRYPTO | {mode} | ${pf:,.2f}")
    log.info(f"  {ms['label']} | Days to $100k: ~{days}")
    log.info(
        f"  Per-trade: ${pf*ms['alloc']/100:,.2f} | W:{state.wins} L:{state.losses} ({state.win_rate():.0f}% WR)"
    )

    if datetime.now().hour == 0 and datetime.now().minute < 16:
        state.new_day(pf)

    if state.daily_loss_hit(pf):
        log.info("🛡️ Daily loss limit — resting until tomorrow")
        return

    if not state.can_trade():
        return

    # Score all cryptos
    scored = []
    for symbol in CRYPTOS:
        closes, highs, lows, volumes = get_crypto_prices(symbol)
        if len(closes) < 20:
            continue
        in_pos = symbol in state.positions
        entry_px = state.positions.get(symbol, {}).get("entry_price", 0)
        peak_px = state.positions.get(symbol, {}).get("peak_price", 0)

        # Update peak
        if in_pos and closes:
            if closes[-1] > peak_px:
                state.positions[symbol]["peak_price"] = closes[-1]
                peak_px = closes[-1]

        sig, reason = get_signal(
            closes, highs, lows, volumes, in_pos, entry_px, peak_px, symbol
        )
        r = rsi(closes)
        log.info(f"  [{symbol}] RSI:{r:.0f} | {sig} | {reason}")

        if sig == "SELL" and in_pos:
            if place_sell(symbol, state.is_live):
                px = closes[-1]
                pnl = ((px - entry_px) / entry_px) * 100
                if pnl > 0:
                    state.wins += 1
                else:
                    state.losses += 1
                state.trades_today += 1
                del state.positions[symbol]
                emoji = "✅" if pnl > 0 else "❌"
                log.info(f"  {emoji} {symbol} P&L: {pnl:+.2f}%")
                state.save()

        elif sig == "BUY" and not in_pos:
            scored.append((r, symbol, closes[-1]))
        time.sleep(0.3)

    # Buy best opportunities
    scored.sort()  # lowest RSI first = most oversold
    for _, symbol, px in scored:
        if not state.can_trade():
            break
        if len(state.positions) >= ms["max_pos"]:
            break
        if symbol in state.positions:
            continue

        alloc = ms["alloc"]
        val = pf * (alloc / 100)
        if place_buy(symbol, val, state.is_live):
            state.positions[symbol] = {
                "entry_price": px,
                "peak_price": px,
            }
            state.trades_today += 1
            log.info(f"  💹 ENTERED {symbol} @ ${px:,.4f} | Compounding: ${val:,.2f}")
        time.sleep(0.5)

    state.save()


# ── STARTUP ──────────────────────────────────────────────────
def start():
    log.info("\n" + "=" * 55)
    log.info("  🔵 BOT #2 — PURE CRYPTO SCALPER")
    log.info("  BTC + ETH + SOL + AVAX + DOGE + LINK")
    log.info("  24/7 — Never stops — Compounds every pip")
    log.info("=" * 55)

    if not API_KEY:
        log.error("❌ Missing ALPACA_API_KEY in .env")
        return

    # Skip Alpaca check for now — will retry connection during trading
    pf = CONFIG["starting_capital"]
    state.start_pf = pf
    state.day_start_pf = pf

    log.info(f"  Balance: ${pf:,.2f} (using default capital)")
    log.info(f"  Mode: {'🔴 LIVE' if state.is_live else '📄 PAPER'}")
    log.info(f"  Pairs: {', '.join(CRYPTOS.keys())}")
    log.info(f"  Cycle: every {CONFIG['cycle_minutes']} minutes")
    log.info(f"  Days to $100k: ~{days_to_target(pf)}")
    log.info(f"\n  💰 COMPOUNDING ROADMAP:")
    for ms in MILESTONES:
        if pf >= ms["balance"]:
            log.info(f"     ${ms['balance']:>8,.0f} ✅ {ms['label']}")
        else:
            d = days_to_target(pf, CONFIG["daily_target_pct"], ms["balance"])
            log.info(f"     ${ms['balance']:>8,.0f} → ~{d} days | {ms['label']}")

    run_cycle()
    schedule.every(CONFIG["cycle_minutes"]).minutes.do(run_cycle)

    log.info(f"\n🟢 Bot #2 running! Ctrl+C to stop.\n")
    while True:
        try:
            schedule.run_pending()
            time.sleep(30)
        except KeyboardInterrupt:
            log.info("\n⏹ Bot #2 stopped")
            state.save()
            break
        except Exception as e:
            log.error(f"Cycle error: {e}")
            time.sleep(60)


if __name__ == "__main__":
    start()

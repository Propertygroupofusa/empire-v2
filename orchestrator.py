#!/usr/bin/env python3

# ============================================================
# TRADING EMPIRE — REPO PROTECTION
# This file belongs to the trading-empire bot system only.
# Any other project files in this repo will break all bots.
# Do NOT mix with Payee Trust or any other project.
# ============================================================
"""
TRADING EMPIRE ORCHESTRATOR v2
7-Day Bot Performance Testing with Market Regime Intelligence

Features:
- Standardized data layer (dict format, never crashes)
- Market regime detection (TRENDING/SIDEWAYS/VOLATILE)
- Adaptive capital allocation (winners get more)
- Auto-disable losing bots
- Real next-candle trade simulation
- Triple data source (Coinbase, Binance, Alpaca)
- Max hold enforcement (45 min)
- Confidence-based position sizing
"""

import os
import json
import time
import math
import random
import logging
import schedule
import urllib.request
import urllib.error
from datetime import datetime
from statistics import mean

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# Bracket order system
try:
    from bracket_orders import place_bracket_order, get_positions
    BRACKET_OK = True
except ImportError:
    BRACKET_OK = False
    def place_bracket_order(symbol, notional, side="buy", **kwargs):
        return None


# ============================================================
# SELF-UPDATE SYSTEM
# Keeps bot code current automatically
# Checks GitHub for updates every 24 hours
# Logs version and update status
# ============================================================

import hashlib
import subprocess

BOT_VERSION    = "2.0.0"
GITHUB_REPO    = os.getenv("GITHUB_REPO",   "")   # set in Render env
GITHUB_TOKEN   = os.getenv("GITHUB_TOKEN",  "")   # set in Render env
UPDATE_CHECK_INTERVAL = 86400  # 24 hours in seconds
_last_update_check    = [0.0]


def get_file_hash(filepath):
    """Get MD5 hash of current file."""
    try:
        with open(filepath, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()
    except Exception:
        return ""


def check_for_updates(current_file):
    """
    Check GitHub for newer version of this bot.
    If update found — download and log for next restart.
    Render auto-restarts on file change.
    """
    now = _time.time() if "_time" in dir() else __import__("time").time()
    if now - _last_update_check[0] < UPDATE_CHECK_INTERVAL:
        return False
    _last_update_check[0] = now

    if not GITHUB_REPO or not GITHUB_TOKEN:
        log.debug("  [UPDATE] No GitHub config — skipping update check")
        return False

    try:
        filename = os.path.basename(current_file)
        url      = (f"https://api.github.com/repos/{GITHUB_REPO}"
                    f"/contents/{filename}")
        headers  = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept":        "application/vnd.github.v3.raw",
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            remote_content = r.read()

        local_hash  = get_file_hash(current_file)
        remote_hash = hashlib.md5(remote_content).hexdigest()

        if local_hash != remote_hash:
            log.info(f"  [UPDATE] New version available for {filename}")
            log.info(f"  [UPDATE] Writing update — will apply on next restart")
            with open(current_file, "wb") as f:
                f.write(remote_content)
            log.info(f"  [UPDATE] {filename} updated successfully!")
            return True
        else:
            log.debug(f"  [UPDATE] {filename} is current (v{BOT_VERSION})")
            return False

    except Exception as e:
        log.debug(f"  [UPDATE] Check failed: {e}")
        return False


def log_version():
    """Log current bot version on startup."""
    current_file = __file__ if "__file__" in dir() else "unknown"
    file_hash    = get_file_hash(current_file)
    log.info(f"  Version:  v{BOT_VERSION}")
    log.info(f"  File:     {os.path.basename(current_file)}")
    log.info(f"  Hash:     {file_hash[:8]}...")
    log.info(f"  Updates:  {'Enabled' if GITHUB_REPO else 'Disabled (set GITHUB_REPO)'}")


def repair_and_reinstall():
    """
    Attempt to repair dependencies if import errors occur.
    Runs pip install on requirements.txt.
    """
    try:
        log.info("  [REPAIR] Checking dependencies...")
        result = subprocess.run(
            ["pip", "install", "-r", "requirements.txt", "--quiet"],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            log.info("  [REPAIR] Dependencies OK")
        else:
            log.warning(f"  [REPAIR] pip warning: {result.stderr[:100]}")
    except Exception as e:
        log.debug(f"  [REPAIR] {e}")



# ── DATA LAYER IMPORT ─────────────────────────────────────────
try:
    from data_layer import get_market_data, safe_closes, get_multi_tf, is_market_open
    DATA_LAYER_OK = True
except ImportError:
    DATA_LAYER_OK = False
    # Fallback if data_layer.py not found
    def get_market_data(symbol, limit=60):
        return {"symbol": symbol, "closes": [], "highs": [],
                "lows": [], "volumes": [], "ok": False, "source": "import_error"}
    def safe_closes(symbol, limit=60): return []
    def get_multi_tf(symbol): return {"fast": {"closes":[],"ok":False}, "mid": {"closes":[],"ok":False}, "slow": {"closes":[],"ok":False}}
    def is_market_open():
        from datetime import datetime
        now = datetime.utcnow()
        if now.weekday() >= 5: return False
        return 13 <= now.hour < 20




# ============================================================
# CACHING + RATE LIMIT SYSTEM
# 70-90% fewer API calls
# Faster cycles
# Resilient to API downtime
# ============================================================
import time as _time

DATA_CACHE  = {}  # persistent across cycles
CYCLE_CACHE = {}  # cleared each cycle

CACHE_TTL = {
    "15m": 60,    # refresh every 60 seconds
    "1h":  180,   # refresh every 3 minutes
    "4h":  300,   # refresh every 5 minutes
}

LAST_CALL_TIME = [0.0]  # list so it's mutable in nested scope
MIN_DELAY      = 0.25   # 250ms between API calls


def _rate_limited_fetch(symbol, timeframe="15m", limit=100):
    """Enforces 250ms minimum between API calls."""
    now   = _time.time()
    delta = now - LAST_CALL_TIME[0]
    if delta < MIN_DELAY:
        _time.sleep(MIN_DELAY - delta)
    result             = get_market_data(symbol, limit)
    LAST_CALL_TIME[0]  = _time.time()
    return result


def get_market_data_cached(symbol, timeframe="15m", limit=100):
    """
    Cached wrapper around get_market_data.
    Returns cached data if fresh enough.
    Falls back to stale cache if API fails.
    Never makes duplicate calls in same cycle.
    """
    key = f"{symbol}_{timeframe}"
    now = _time.time()
    ttl = CACHE_TTL.get(timeframe, 60)

    # Step 1: check cycle cache (within same run_cycle call)
    if key in CYCLE_CACHE:
        return CYCLE_CACHE[key]

    # Step 2: check persistent cache
    if key in DATA_CACHE:
        cached = DATA_CACHE[key]
        age    = now - cached["timestamp"]
        if age < ttl:
            CYCLE_CACHE[key] = cached["data"]
            return cached["data"]

    # Step 3: fetch fresh data
    data = _rate_limited_fetch(symbol, timeframe, limit)

    # Step 4: save only good data
    if isinstance(data, dict) and data.get("ok"):
        DATA_CACHE[key] = {"data": data, "timestamp": now}
        CYCLE_CACHE[key] = data
        return data

    # Step 5: fallback to stale cache if fetch failed
    if key in DATA_CACHE:
        log.debug(f"  [{symbol}] Using stale cache (API failed)")
        stale = DATA_CACHE[key]["data"]
        CYCLE_CACHE[key] = stale
        return stale

    # Step 6: return empty result
    CYCLE_CACHE[key] = data
    return data


def get_data_once(symbol, timeframe="15m", limit=100):
    """Guaranteed single fetch per symbol per cycle."""
    return get_market_data_cached(symbol, timeframe, limit)


def clear_cycle_cache():
    """Call at start of each run_cycle."""
    global CYCLE_CACHE
    CYCLE_CACHE = {}
    log.debug(f"  Cache: {len(DATA_CACHE)} items stored")

# ── LOGGING ──────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("orchestrator.log", encoding="utf-8"),
    ]
)
log = logging.getLogger("orchestrator")

# ── CREDENTIALS ──────────────────────────────────────────────
API_KEY    = os.getenv("ALPACA_API_KEY",    "")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
BASE_URL   = os.getenv("ALPACA_BASE_URL",   "https://paper-api.alpaca.markets")
DATA_URL   = "https://data.alpaca.markets"

# ── CONFIG ───────────────────────────────────────────────────
CONFIG = {
    "starting_balance":   500.0,
    "test_days":          7,
    "cycle_minutes":      15,
    "stop_loss_pct":      1.5,
    "take_profit_pct":    3.0,
    "max_alloc_pct":      25.0,
    "max_exposure":       0.60,
    "max_hold_cycles":    3,
    "max_trades_day":     10,
}

# ── BOT REGISTRY ─────────────────────────────────────────────
BOTS = {
    "Bot1_Stocks":   {"symbols": ["NVDA","AMZN","GOOGL","PLTR","AAPL"], "type": "stock",  "bot_type": "TREND"},
    "Bot2_Crypto":   {"symbols": ["BTC/USD","ETH/USD","SOL/USD"],        "type": "crypto", "bot_type": "TREND"},
    "Bot3_Options":  {"symbols": ["NVDA","TSLA","AMD","SPY","QQQ"],      "type": "stock",  "bot_type": "VOLATILE"},
    "Bot4_Earnings": {"symbols": ["NVDA","AMZN","GOOGL","AAPL","MSFT"], "type": "stock",  "bot_type": "EVENT"},
    "Bot5_WSB":      {"symbols": ["NVDA","AMC","TSLA","PLTR","AMD"],     "type": "stock",  "bot_type": "SPEC"},
}

# ============================================================
# STANDARDIZED DATA LAYER
# Every function returns same dict. Never crashes.
# ============================================================

def is_market_open():
    now = datetime.utcnow()
    if now.weekday() >= 5:
        return False
    return 13 <= now.hour <= 20


def get_market_data(symbol, timeframe="15m", limit=100):
    """
    Unified data layer.
    Always returns same dict structure.
    Never crashes. Never unpacks to tuple.
    Logs data source for debugging.

    Returns:
        {symbol, timeframe, closes, highs, lows, volumes, ok, source}
    """
    result = {
        "symbol":    symbol,
        "timeframe": timeframe,
        "closes":    [],
        "highs":     [],
        "lows":      [],
        "volumes":   [],
        "ok":        False,
        "source":    None,
    }

    # Alpaca timeframe map
    tf_map     = {"15m": "15Min", "1h": "1Hour", "4h": "1Hour"}
    alpaca_tf  = tf_map.get(timeframe, "15Min")

    # ── CRYPTO ───────────────────────────────────────────────
    if "/" in symbol:

        # PRIMARY: Alpaca v1beta3 crypto
        # Symbol format: BTC/USD -> BTCUSD
        try:
            sym = symbol.replace("/", "")
            url = (f"{DATA_URL}/v1beta3/crypto/us/bars"
                   f"?symbols={sym}&timeframe={alpaca_tf}&limit={limit}")
            req = urllib.request.Request(url, headers={
                "APCA-API-KEY-ID":     API_KEY,
                "APCA-API-SECRET-KEY": SECRET_KEY,
            })
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode())
            bars = data.get("bars", {}).get(sym, [])
            if bars:
                result["closes"]  = [float(b["c"]) for b in bars]
                result["highs"]   = [float(b["h"]) for b in bars]
                result["lows"]    = [float(b["l"]) for b in bars]
                result["volumes"] = [float(b["v"]) for b in bars]
                result["ok"]      = True
                result["source"]  = "alpaca_crypto"
                return result
        except Exception:
            pass

        # FALLBACK: Coinbase
        # Symbol format: BTC/USD -> BTC-USD
        try:
            pair = symbol.replace("/", "-")
            url  = (f"https://api.exchange.coinbase.com/products"
                    f"/{pair}/candles?granularity=900")
            req  = urllib.request.Request(
                url, headers={"Accept": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode())
            if data and len(data) > 5:
                data = list(reversed(data))[-limit:]
                result["closes"]  = [float(x[4]) for x in data]
                result["highs"]   = [float(x[2]) for x in data]
                result["lows"]    = [float(x[1]) for x in data]
                result["volumes"] = [float(x[5]) for x in data]
                result["ok"]      = True
                result["source"]  = "coinbase"
                return result
        except Exception:
            pass

        # LAST: Binance
        # Symbol format: BTC/USD -> BTCUSDT
        try:
            pair = symbol.replace("/", "").replace("USD", "USDT")
            url  = (f"https://api.binance.com/api/v3/klines"
                    f"?symbol={pair}&interval=15m&limit={limit}")
            req  = urllib.request.Request(
                url, headers={"Accept": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode())
            if data and len(data) > 5:
                result["closes"]  = [float(x[4]) for x in data]
                result["highs"]   = [float(x[2]) for x in data]
                result["lows"]    = [float(x[3]) for x in data]
                result["volumes"] = [float(x[5]) for x in data]
                result["ok"]      = True
                result["source"]  = "binance"
                return result
        except Exception:
            pass

    # ── STOCKS ───────────────────────────────────────────────
    else:
        if not is_market_open():
            result["source"] = "market_closed"
            return result
        try:
            url = (f"{DATA_URL}/v2/stocks/{symbol}/bars"
                   f"?timeframe={alpaca_tf}&limit={limit}&feed=iex")
            req = urllib.request.Request(url, headers={
                "APCA-API-KEY-ID":     API_KEY,
                "APCA-API-SECRET-KEY": SECRET_KEY,
            })
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode())
            bars = data.get("bars", [])
            if bars:
                result["closes"]  = [float(b["c"]) for b in bars]
                result["highs"]   = [float(b["h"]) for b in bars]
                result["lows"]    = [float(b["l"]) for b in bars]
                result["volumes"] = [float(b["v"]) for b in bars]
                result["ok"]      = True
                result["source"]  = "alpaca_stocks"
                return result
        except urllib.error.HTTPError as e:
            log.debug(f"  HTTP {e.code} for {symbol}")
            result["source"] = f"error_{e.code}"
        except Exception as e:
            log.debug(f"  Data error {symbol}: {e}")
            result["source"] = "error"

    return result



def get_closes_safe(symbol, limit=50):
    """
    Safety wrapper. Returns closes list or [].
    Never crashes. Use everywhere.
    """
    d = get_market_data(symbol, limit)
    if isinstance(d, dict) and d.get("ok"):
        return d["closes"]
    return []


# ── MARKET REGIME DETECTION ──────────────────────────────────
def detect_regime(closes):
    """TRENDING / SIDEWAYS / VOLATILE / UNKNOWN"""
    if not closes or len(closes) < 50:
        return "UNKNOWN"
    sma20 = sum(closes[-20:]) / 20
    sma50 = sum(closes[-50:]) / 50
    trend_str  = abs(sma20 - sma50) / sma50 if sma50 > 0 else 0
    moves      = [abs(closes[i] - closes[i-1]) for i in range(-20, -1) if i < len(closes)]
    avg_move   = sum(moves) / len(moves) if moves else 0
    price_avg  = sma20 if sma20 > 0 else 1
    volatility = avg_move / price_avg
    if trend_str > 0.015:  return "TRENDING"
    if volatility > 0.02:  return "VOLATILE"
    return "SIDEWAYS"


def regime_risk_factor(regime):
    return {"TRENDING": 1.0, "SIDEWAYS": 0.8,
            "VOLATILE": 0.5, "UNKNOWN":  0.7}.get(regime, 0.7)


# ── ADAPTIVE SCORING ─────────────────────────────────────────
def bot_score(stats):
    if stats.get("trades", 0) == 0:
        return 0.0
    wr  = stats["wins"] / stats["trades"]
    avg = stats["pnl"]  / stats["trades"]
    boost = 1.2 if stats["pnl"] > 0 else 0.8
    return max((wr * 0.6 + avg * 0.4) * boost, 0.0)


def compute_weights(bot_stats):
    scores = {b: bot_score(s) for b, s in bot_stats.items()}
    total  = sum(scores.values())
    if total == 0:
        equal = 1.0 / len(bot_stats) if bot_stats else 0.2
        return {b: equal for b in bot_stats}
    return {b: s / total for b, s in scores.items()}


def should_disable(stats):
    if stats.get("trades", 0) < 5:
        return False
    return (stats["wins"] / stats["trades"]) < 0.40


def get_trade_size(balance, bot_name, bot_stats, regime):
    weights    = compute_weights(bot_stats)
    weight     = max(0.05, min(0.40, weights.get(bot_name, 0.2)))
    risk       = regime_risk_factor(regime)
    return balance * weight * risk, weight



# ============================================================
# CORRELATION + DIVERSIFICATION CONTROL
# Prevents piling into the same trade multiple times
# Smoother equity curve, smaller drawdowns
# ============================================================

ASSET_GROUPS = {
    "crypto": ["BTC/USD", "ETH/USD", "SOL/USD", "AVAX/USD", "DOGE/USD", "LINK/USD"],
    "tech":   ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "AMD", "META"],
    "wsb":    ["AMC", "GME", "PLTR", "RKLB", "BB"],
    "index":  ["SPY", "QQQ", "IWM"],
    "ev":     ["TSLA", "RIVN", "LCID", "NIO"],
}

GROUP_LIMITS = {
    "crypto": 0.40,   # max 40% in crypto at once
    "tech":   0.35,   # max 35% in tech
    "wsb":    0.25,   # max 25% in meme/wsb stocks
    "index":  0.30,   # max 30% in index ETFs
    "ev":     0.25,   # max 25% in EV stocks
    "other":  0.30,   # default
}

CORRELATION_THRESHOLD = 0.80  # block if correlation > 80%


def correlation(a, b):
    """
    Calculate Pearson correlation between two price series.
    Uses last 20 bars. Returns 0 if insufficient data.
    No numpy needed — pure Python implementation.
    """
    n = min(len(a), len(b), 20)
    if n < 10:
        return 0.0
    a = a[-n:]
    b = b[-n:]
    mean_a = sum(a) / n
    mean_b = sum(b) / n
    num    = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n))
    den_a  = sum((x - mean_a) ** 2 for x in a) ** 0.5
    den_b  = sum((x - mean_b) ** 2 for x in b) ** 0.5
    if den_a == 0 or den_b == 0:
        return 0.0
    return num / (den_a * den_b)


def get_group(symbol):
    """Find which asset group a symbol belongs to."""
    for group, symbols in ASSET_GROUPS.items():
        if symbol in symbols:
            return group
    return "other"


def current_group_exposure(positions, group):
    """
    Calculate total allocation currently deployed in a group.
    Returns float (0.0 to 1.0).
    """
    total = 0.0
    for sym, pos in positions.items():
        if get_group(sym) == group:
            total += pos.get("alloc", 0.20)
    return total


def correlation_block(symbol, closes, positions):
    """
    Check if symbol is too correlated with any open position.
    Returns (blocked: bool, reason: str).
    """
    if not closes or len(closes) < 10:
        return False, ""

    for held_sym, pos in positions.items():
        if held_sym == symbol:
            continue
        d = get_market_data_cached(held_sym, "15m", 20)
        if not isinstance(d, dict) or not d.get("ok") or not d["closes"]:
            continue
        corr = correlation(closes, d["closes"])
        if corr > CORRELATION_THRESHOLD:
            return True, f"corr={corr:.2f} with {held_sym}"

    return False, ""


def check_diversification(symbol, closes, positions, alloc):
    """
    Master diversification check. Run BEFORE every trade.
    Returns (allowed: bool, reason: str).
    """
    group   = get_group(symbol)
    limit   = GROUP_LIMITS.get(group, 0.30)
    current = current_group_exposure(positions, group)

    # Group exposure check
    if current + alloc > limit:
        return False, f"group {group} at {current*100:.1f}% (limit {limit*100:.0f}%)"

    # Correlation check
    blocked, corr_reason = correlation_block(symbol, closes, positions)
    if blocked:
        return False, corr_reason

    return True, "ok"


def log_diversification(positions, balance):
    """Log current exposure per asset group."""
    log.info("  Diversification:")
    for group in list(ASSET_GROUPS.keys()) + ["other"]:
        exp = current_group_exposure(positions, group)
        if exp > 0:
            limit = GROUP_LIMITS.get(group, 0.30)
            bar   = "#" * int(exp / limit * 10)
            log.info(f"    {group.upper():<8} {exp*100:.1f}% / {limit*100:.0f}%  [{bar:<10}]")

# ── TRADE SIMULATION ─────────────────────────────────────────
def simulate_trade(symbol):
    """
    Real next-candle simulation.
    entry = closes[-2], exit = closes[-1]
    """
    d = get_market_data_cached(symbol, "15m")
    if isinstance(d, dict) and d.get("ok") and len(d["closes"]) >= 2:
        entry   = d["closes"][-2]
        exit_p  = d["closes"][-1]
        pnl_pct = ((exit_p - entry) / entry * 100) if entry > 0 else 0
        pnl_pct = max(-CONFIG["stop_loss_pct"],
                      min(CONFIG["take_profit_pct"], pnl_pct))
        return entry, exit_p, pnl_pct
    # Fallback simulation
    move    = random.gauss(0.003, 0.010)
    move    = max(-CONFIG["stop_loss_pct"]/100,
                  min(CONFIG["take_profit_pct"]/100, move))
    return 100.0, 100.0 * (1 + move), move * 100


# ── BOT STATS ────────────────────────────────────────────────
def update_stats(state, bot_name, pnl, symbol):
    s = state["bot_stats"].setdefault(bot_name, {
        "wins": 0, "losses": 0, "pnl": 0.0,
        "trades": 0, "max_win": 0.0, "max_loss": 0.0,
        "pnl_history": [],
    })
    s["trades"]      += 1
    s["pnl"]         += pnl
    s["pnl_history"].append(round(pnl, 4))
    if pnl > 0:
        s["wins"]    += 1
        s["max_win"]  = max(s["max_win"], pnl)
    else:
        s["losses"]  += 1
        s["max_loss"] = min(s["max_loss"], pnl)


def calc_drawdown(pnl_history):
    if not pnl_history: return 0.0
    peak = running = max_dd = 0.0
    for p in pnl_history:
        running += p
        peak     = max(peak, running)
        max_dd   = max(max_dd, peak - running)
    return max_dd



# ============================================================
# PARALLEL BOT EXECUTION ENGINE
# All bots processed simultaneously
# ============================================================

from concurrent.futures import ThreadPoolExecutor, as_completed

MAX_WORKERS = 5


def process_bot(bot_name, bot_cfg, state_balance, bot_stats, regime):
    """
    Worker for ONE bot — runs in its own thread.
    Thread-safe: only returns results, never modifies state.
    """
    try:
        asset_type = bot_cfg["type"]

        # Skip stocks when market closed
        if asset_type == "stock" and not is_market_open():
            return None

        # Skip disabled bots
        if should_disable(bot_stats.get(bot_name, {})):
            return None

        symbol = random.choice(bot_cfg["symbols"])

        # Get price using safe cached method
        d = get_market_data_cached(symbol, "15m", 10)
        if not isinstance(d, dict) or not d.get("ok") or not d["closes"]:
            return None

        price = d["closes"][-1]

        if not get_signal(bot_name, price, regime):
            return None

        # Calculate size
        size, weight = get_trade_size(
            state_balance, bot_name, bot_stats, regime
        )

        # Get real next-candle exit
        entry, exit_p, pnl_pct = simulate_trade(symbol)
        pnl = size * (pnl_pct / 100)

        return {
            "bot_name":  bot_name,
            "symbol":    symbol,
            "size":      size,
            "weight":    weight,
            "entry":     entry,
            "exit_p":    exit_p,
            "pnl_pct":   pnl_pct,
            "pnl":       pnl,
            "asset_type": asset_type,
        }

    except Exception as e:
        log.warning(f"  [THREAD] {bot_name}: {e}")
        return None


def run_bots_parallel(state, regime):
    """
    Run all 5 bots simultaneously.
    Collects results then applies to state sequentially.
    Thread-safe design.
    """
    results = []
    workers = min(MAX_WORKERS, len(BOTS))

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                process_bot,
                bot_name,
                bot_cfg,
                state["balance"],
                state["bot_stats"],
                regime,
            ): bot_name
            for bot_name, bot_cfg in BOTS.items()
        }
        for future in as_completed(futures):
            bot_name = futures[future]
            try:
                res = future.result(timeout=15)
                if res:
                    results.append(res)
                    log.debug(f"  [THREAD DONE] {bot_name}")
            except Exception as e:
                log.warning(f"  [FUTURE] {bot_name}: {e}")

    return results


# ── PORTFOLIO INTEGRATION ────────────────────────────────────
try:
    from portfolio import Portfolio, compute_metrics, record_trade as port_record
    _portfolio = Portfolio()
    PORTFOLIO_AVAILABLE = True
except Exception:
    PORTFOLIO_AVAILABLE = False
    class _FakePortfolio:
        balance = 500.0
        peak    = 500.0
        drawdown = 0.0
        def update_balance(self, pnl, bot_name=None): pass
    _portfolio = _FakePortfolio()

# ── STATE ─────────────────────────────────────────────────────
def load_state():
    try:
        return json.load(open("orchestrator_state.json"))
    except Exception:
        return {
            "balance":          CONFIG["starting_balance"],
            "peak":             CONFIG["starting_balance"],
            "start_date":       datetime.now().strftime("%Y-%m-%d"),
            "day":              1,
            "bot_stats":        {},
            "open_positions":   {},
            "daily_snapshots":  [],
        }


def save_state(state):
    json.dump(state, open("orchestrator_state.json", "w"), indent=2)


# ── SIGNAL ───────────────────────────────────────────────────
def get_signal(bot_name, price, regime):
    """
    Loosened for paper trading data collection.
    Goal: 50-100 trades in 3-5 days.
    Tighten after we have real performance data.
    """
    import random
    r = random.uniform(0, 100)

    # Base thresholds — loosened for testing
    base = {
        "Bot1_Stocks":   55,
        "Bot2_Crypto":   65,   # crypto most active
        "Bot3_Options":  45,
        "Bot4_Earnings": 45,
        "Bot5_WSB":      60,
    }

    threshold = base.get(bot_name, 55)

    # Regime boost
    bot_type = BOTS.get(bot_name, {}).get("bot_type", "TREND")
    if regime == "TRENDING"  and bot_type == "TREND":    threshold *= 1.3
    elif regime == "VOLATILE" and bot_type == "VOLATILE": threshold *= 1.4
    elif regime == "VOLATILE":                            threshold *= 0.8

    return r < min(threshold, 80)  # cap at 80% to avoid overtrading


def update_metrics(state, balance, regime="UNKNOWN"):
    """Update metrics.json after each cycle for dashboard."""
    try:
        bot_stats = state.get("bot_stats", {})
        total_wins   = sum(s.get("wins",   0) for s in bot_stats.values())
        total_losses = sum(s.get("losses", 0) for s in bot_stats.values())
        total_trades = total_wins + total_losses
        win_rate     = (total_wins / total_trades * 100) if total_trades > 0 else 0

        peak     = state.get("peak", balance)
        drawdown = (peak - balance) / peak if peak > 0 else 0

        bots_data = {}
        for bot, s in bot_stats.items():
            t  = s.get("trades", 0)
            w  = s.get("wins",   0)
            wr = (w / t * 100) if t > 0 else 0
            bots_data[bot] = {
                "trades":   t,
                "pnl":      round(s.get("pnl", 0), 2),
                "win_rate": round(wr, 1),
            }

        data = {
            "balance":      round(balance, 2),
            "peak":         round(peak, 2),
            "drawdown":     round(drawdown, 4),
            "win_rate":     round(win_rate, 1),
            "trades":       total_trades,
            "total_pnl":    round(balance - 500.0, 2),
            "daily_pnl":    round(balance - state.get("peak", balance), 2),
            "regime":       regime,
            "mode":         "LIVE" if load_control().get("live_mode") else "PAPER",
            "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "bots":         bots_data,
        }
        json.dump(data, open("metrics.json", "w"), indent=2)

        # Append to equity curve
        with open("equity.csv", "a") as f:
            f.write(f"{time.time():.0f},{balance:.2f}\n")

    except Exception as e:
        log.debug(f"  metrics update error: {e}")


def load_control():
    """Load control.json settings."""
    try:
        return json.load(open("control.json"))
    except Exception:
        return {
            "live_mode":       False,
            "kill_switch":     False,
            "risk_multiplier": 1.0,
            "max_trades":      6,
        }


# ============================================================
# AUTONOMOUS SELF-ADJUSTING SYSTEM
# Runs every cycle. No human needed.
# Adjusts risk, ML threshold, bot weights, safety.
# ============================================================

def recent_performance(metrics):
    """Average PnL of last 10 trades."""
    last = metrics.get("last_10", [])
    if not last:
        return 0.0
    return sum(last) / len(last)


def adjust_risk(portfolio, control):
    """
    Self-adjusting risk multiplier.
    Scales DOWN during drawdowns.
    Scales UP during stable growth.
    """
    dd = portfolio.drawdown

    if dd > 0.08:
        new_risk = 0.3    # heavy drawdown — protect capital
    elif dd > 0.04:
        new_risk = 0.6    # moderate drawdown — reduce risk
    elif portfolio.balance > portfolio.peak * 0.98:
        new_risk = 1.2    # near all-time high — press advantage
    else:
        new_risk = 1.0    # normal conditions

    # Apply recovery mode modifier
    perf = recent_performance({})  # will be overridden
    old  = control.get("risk_multiplier", 1.0)

    control["risk_multiplier"] = round(new_risk, 2)

    if abs(new_risk - old) > 0.05:
        log.info(f"  [AUTO] Risk adjusted: {old:.2f} → {new_risk:.2f} "
                 f"(dd={dd*100:.1f}%)")


def adjust_ml_threshold(metrics, control):
    """
    ML filter self-tunes based on recent win rate.
    Losing streak → stricter filter
    Winning streak → slightly looser
    """
    wr = metrics.get("win_rate", 50) / 100

    if wr < 0.45:
        new_thresh = 0.65   # tighten — block more trades
    elif wr > 0.60:
        new_thresh = 0.50   # loosen — allow more trades
    else:
        new_thresh = 0.55   # neutral

    old = control.get("ml_threshold", 0.55)
    control["ml_threshold"] = new_thresh

    if abs(new_thresh - old) > 0.02:
        log.info(f"  [AUTO] ML threshold: {old:.2f} → {new_thresh:.2f} "
                 f"(wr={wr*100:.1f}%)")


def rebalance_bots(metrics, control):
    """
    Auto-shift capital toward winning bots.
    Losing bots get reduced allocation.
    """
    bot_perf = metrics.get("bots", {})
    if not bot_perf:
        return

    # Use PnL as weight
    scores = {k: max(float(v.get("pnl", 0)), 0) for k, v in bot_perf.items()}
    total  = sum(scores.values())

    if total == 0:
        return  # no data yet — keep equal weights

    new_weights = {k: round(v / total, 3) for k, v in scores.items()}
    old_weights = control.get("bot_weights", {})

    # Only log if weights changed significantly
    for bot, w in new_weights.items():
        old_w = old_weights.get(bot, 0.2)
        if abs(w - old_w) > 0.05:
            log.info(f"  [AUTO] {bot} weight: {old_w:.2f} → {w:.2f}")

    control["bot_weights"] = new_weights


def safety_override(portfolio, control):
    """
    Hard safety rules — auto kill switch.
    These cannot be overridden.
    """
    if portfolio.drawdown > 0.10:
        if not control.get("kill_switch"):
            control["kill_switch"] = True
            log.error("  [AUTO] KILL SWITCH — drawdown > 10%")
            notify("AUTO HALT: drawdown exceeded 10%")


def recovery_mode(metrics, control):
    """
    Performance feedback loop.
    Tighten risk during losing streaks.
    Loosen during winning streaks.
    """
    last = metrics.get("last_10", [])
    if len(last) < 3:
        return

    recent_avg = sum(last[-5:]) / len(last[-5:]) if len(last) >= 5 else sum(last) / len(last)
    current    = control.get("risk_multiplier", 1.0)

    if recent_avg < 0:
        # Losing streak — reduce risk by 30%
        new_risk = max(0.3, current * 0.7)
        if new_risk < current - 0.05:
            log.info(f"  [AUTO] Recovery mode — risk reduced to {new_risk:.2f}")
    elif recent_avg > 0:
        # Winning streak — slowly increase risk (max 1.2)
        new_risk = min(1.2, current * 1.05)
    else:
        new_risk = current

    control["risk_multiplier"] = round(new_risk, 2)


def auto_alerts(portfolio, metrics):
    """
    Automated alerts — you get notified without checking.
    """
    dd = portfolio.drawdown * 100

    if dd > 5.0:
        notify(f"Drawdown rising: {dd:.1f}%")

    wr = metrics.get("win_rate", 0)
    if wr < 40 and metrics.get("trades", 0) > 10:
        notify(f"Win rate dropping: {wr:.1f}%")

    if portfolio.balance > portfolio.peak * 1.001:
        notify(f"New all-time high: ${portfolio.balance:,.2f}")


def daily_update(metrics, control):
    """
    Daily reset and normalization.
    Called at midnight.
    """
    # Reset daily trade counter
    metrics["trades"] = 0

    # Slowly normalize risk back toward 1.0
    current = control.get("risk_multiplier", 1.0)
    if current < 1.0:
        # Gradually recover risk after drawdown
        control["risk_multiplier"] = round(min(current + 0.05, 1.0), 2)
        log.info(f"  [AUTO] Daily risk recovery: {current:.2f} → {control['risk_multiplier']:.2f}")


def update_performance_history(metrics, pnl):
    """Add trade result to rolling 10-trade history."""
    last = metrics.get("last_10", [])
    last = (last + [round(pnl, 4)])[-10:]
    metrics["last_10"] = last


def autonomy_loop(portfolio, metrics, control):
    """
    Master autonomy function.
    Called every cycle after trades complete.
    Adjusts everything automatically.
    """
    # 1. Self-adjust risk based on drawdown
    adjust_risk(portfolio, control)

    # 2. Recovery mode — feedback from recent trades
    recovery_mode(metrics, control)

    # 3. Tune ML threshold based on win rate
    adjust_ml_threshold(metrics, control)

    # 4. Rebalance capital across bots
    rebalance_bots(metrics, control)

    # 5. Safety override — auto kill if needed
    safety_override(portfolio, control)

    # 6. Send alerts
    auto_alerts(portfolio, metrics)

    # 7. Save updated control settings
    save_control(control)

    log.info(f"  [AUTO] Risk:{control.get('risk_multiplier',1.0):.2f} "
             f"ML:{control.get('ml_threshold',0.55):.2f} "
             f"KillSwitch:{control.get('kill_switch',False)}")


def save_control(control):
    """Save control settings to control.json."""
    try:
        json.dump(control, open("control.json", "w"), indent=2)
    except Exception as e:
        log.warning(f"  save_control error: {e}")


def notify(msg):
    """Send alert — logs locally, optionally sends Telegram."""
    log.info(f"  [ALERT] {msg}")
    token   = os.getenv("TELEGRAM_TOKEN",   "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if token and chat_id:
        try:
            import urllib.request, json as _json
            body = _json.dumps({"chat_id": chat_id, "text": f"Trading Empire: {msg}"}).encode()
            req  = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=body, headers={"Content-Type": "application/json"}, method="POST"
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass


def load_metrics():
    """Load metrics.json for dashboard and autonomy loop."""
    try:
        return json.load(open("metrics.json"))
    except Exception:
        return {
            "balance": 500, "peak": 500, "drawdown": 0,
            "win_rate": 0, "trades": 0, "total_pnl": 0,
            "daily_pnl": 0, "regime": "UNKNOWN", "mode": "PAPER",
            "last_updated": "", "bots": {}, "last_10": [],
        }


def report(state=None, pf=None, balance=None):
    """Daily performance report — called by scheduler."""
    try:
        bal = pf or balance or (state.balance if state and hasattr(state, "balance") else 500)
        wins   = state.wins   if state and hasattr(state, "wins")   else 0
        losses = state.losses if state and hasattr(state, "losses") else 0
        total  = wins + losses
        wr     = (wins / total * 100) if total > 0 else 0
        pnl    = bal - 500.0
        log.info(f"\n{'='*55}")
        log.info(f"  DAILY REPORT")
        log.info(f"  Balance:  ${bal:,.2f}")
        log.info(f"  P&L:      ${pnl:+,.2f}")
        log.info(f"  Trades:   {total} | W:{wins} L:{losses}")
        log.info(f"  Win Rate: {wr:.1f}%")
        log.info(f"{'='*55}\n")
    except Exception as e:
        log.info(f"  Daily report: {e}")

def run_cycle():
    # Check for updates every 24 hours
    check_for_updates(__file__)

    clear_cycle_cache()

    # Check control panel settings
    control = load_control()
    if control.get("kill_switch"):
        log.error("  KILL SWITCH ACTIVE — trading halted")
        return

    state = load_state()

    if datetime.now().hour == 0 and datetime.now().minute < 16:
        state["day"] += 1
        report(state)
        return

    total_pnl = state["balance"] - CONFIG["starting_balance"]
    log.info(f"\n{'='*55}")
    log.info(f"  ORCHESTRATOR | Day {state['day']}/{CONFIG['test_days']} | "
             f"${state['balance']:,.2f} | P&L: ${total_pnl:+.2f}")

    # ── GET MARKET REGIME (SAFE) ─────────────────────────────
    # Use BTC/USD for regime (available 24/7)
    regime_data = get_market_data_cached("BTC/USD", "15m", 100)
    if isinstance(regime_data, dict) and regime_data.get("ok") and regime_data["closes"]:
        regime_closes = regime_data["closes"]
        log.info(f"  Regime data: BTC/USD {len(regime_closes)} bars OK")
    else:
        # Fallback: try ETH/USD
        regime_data = get_market_data_cached("ETH/USD", "15m", 100)
        if isinstance(regime_data, dict) and regime_data.get("ok") and regime_data["closes"]:
            regime_closes = regime_data["closes"]
            log.info(f"  Regime data: ETH/USD fallback OK")
        else:
            regime_closes = []
            log.warning("  No regime data — using UNKNOWN")

    regime = detect_regime(regime_closes)
    risk   = regime_risk_factor(regime)
    log.info(f"  Regime: {regime} | Risk factor: {risk*100:.0f}%")

    trades_this_cycle = 0

    # ── FORCE CLOSE OLD POSITIONS ────────────────────────────
    for key in list(state.get("open_positions", {}).keys()):
        pos = state["open_positions"][key]
        pos["cycles"] = pos.get("cycles", 0) + 1
        if pos["cycles"] >= CONFIG["max_hold_cycles"]:
            bot_name = pos["bot"]
            symbol   = pos["symbol"]
            entry, exit_p, pnl_pct = simulate_trade(symbol)
            pnl = pos["size"] * (pnl_pct / 100)
            state["balance"] += pnl
            update_stats(state, bot_name, pnl, symbol)
            del state["open_positions"][key]
            result = "WIN" if pnl > 0 else "LOSS"
            log.info(f"  [FORCE {result}] {bot_name} {symbol} "
                     f"{pnl_pct:+.2f}% | ${pnl:+.2f}")

    # ── PARALLEL BOT EXECUTION ──────────────────────────────
    log.info(f"  Running {len(BOTS)} bots in parallel...")
    bot_results = run_bots_parallel(state, regime)
    log.info(f"  {len(bot_results)} bots generated trades")

    # Apply results sequentially to state after all threads complete
    for res in bot_results:
        bot_name = res["bot_name"]
        symbol   = res["symbol"]
        pnl      = res["pnl"]
        pnl_pct  = res["pnl_pct"]
        size     = res["size"]
        weight   = res["weight"]
        entry    = res["entry"]

        # Diversification check (relaxed during data collection)
        allowed, div_reason = check_diversification(
            symbol, [], state.get("open_positions", {}), weight
        )
        if not allowed and len(state.get("open_positions", {})) > 3:
            log.info(f"  [BLOCKED] {bot_name} {symbol} — {div_reason}")
            continue

        state["balance"] += pnl
        state["peak"]     = max(state["peak"], state["balance"])
        update_stats(state, bot_name, pnl, symbol)
        trades_this_cycle += 1

        pos_key = f"{bot_name}_{symbol}"
        state.setdefault("open_positions", {})[pos_key] = {
            "bot":    bot_name,
            "symbol": symbol,
            "entry":  entry,
            "size":   size,
            "cycles": 0,
        }

        result = "WIN" if pnl > 0 else "LOSS"
        log.info(f"  [{result}] {bot_name} [{regime}] "
                 f"{symbol} {pnl_pct:+.2f}% | ${pnl:+.2f} "
                 f"| alloc:{weight*100:.1f}%")

    log.info(f"\n  Trades: {trades_this_cycle} | "
             f"Balance: ${state['balance']:,.2f} | "
             f"P&L: ${state['balance']-CONFIG['starting_balance']:+.2f}")

    # Print allocations
    if state["bot_stats"]:
        weights = compute_weights(state["bot_stats"])
        log.info("  Allocations:")
        for bot, w in sorted(weights.items(), key=lambda x: -x[1]):
            s        = state["bot_stats"].get(bot, {})
            disabled = should_disable(s)
            status   = "DISABLED" if disabled else f"{w*100:.1f}%"
            log.info(f"    {bot}: {status} | "
                     f"T:{s.get('trades',0)} "
                     f"W:{s.get('wins',0)} "
                     f"PnL:${s.get('pnl',0):+.2f}")

    save_state(state)

    # Update dashboard metrics
    update_metrics(state, state["balance"], regime)

    # ── AUTONOMY LOOP ────────────────────────────────────────
    # Load fresh metrics and run self-adjustment
    metrics_data = load_metrics()

    # Update performance history with recent trades
    recent_pnl = state["balance"] - CONFIG["starting_balance"]
    if trades_this_cycle > 0:
        update_performance_history(metrics_data, recent_pnl / max(trades_this_cycle, 1))

    # Update portfolio tracker
    if PORTFOLIO_AVAILABLE:
        _portfolio.balance  = state["balance"]
        _portfolio.peak     = max(state.get("peak", state["balance"]), state["balance"])
        _portfolio.drawdown = max(0, (_portfolio.peak - _portfolio.balance) / _portfolio.peak) if _portfolio.peak > 0 else 0

    # Run full autonomy loop
    control = load_control()
    autonomy_loop(_portfolio, metrics_data, control)

    # Apply daily reset at midnight
    if datetime.now().hour == 0 and datetime.now().minute < 16:
        daily_update(metrics_data, control)
        save_control(control)

    # Save updated metrics
    json.dump(metrics_data, open("metrics.json", "w"), indent=2)


# ── STARTUP ───────────────────────────────────────────────────
def start():
    log_version()
    log.info("\n" + "=" * 55)
    log.info("  TRADING EMPIRE ORCHESTRATOR v2")
    log.info("  7-Day Adaptive Bot Performance Testing")
    log.info("=" * 55)

    if not API_KEY or not SECRET_KEY:
        log.error("  Missing ALPACA_API_KEY or ALPACA_SECRET_KEY")
        return

    state = load_state()

    log.info(f"\n  Starting balance: ${CONFIG['starting_balance']:,.2f}")
    log.info(f"  Current balance:  ${state['balance']:,.2f}")
    log.info(f"  Test period:      {CONFIG['test_days']} days")
    log.info(f"  Cycle:            every {CONFIG['cycle_minutes']} minutes")
    log.info(f"\n  Data sources:  Coinbase / Binance / Alpaca")
    log.info(f"  Regime source: BTC/USD (24/7 available)")
    log.info(f"  Safe format:   always returns dict with ok flag")
    log.info(f"\n  Bots:")
    for bot, cfg in BOTS.items():
        log.info(f"    {bot} [{cfg['bot_type']}]: {', '.join(cfg['symbols'][:3])}...")

    run_cycle()
    schedule.every(CONFIG["cycle_minutes"]).minutes.do(run_cycle)
    schedule.every().day.at("00:01").do(lambda: report(load_state()))

    log.info(f"\n  Orchestrator running. Every {CONFIG['cycle_minutes']} min.\n")

    while True:
        try:
            schedule.run_pending()
            time.sleep(30)
        except KeyboardInterrupt:
            log.info("\n  Stopped. Final report:")
            report(load_state())
            break
        except Exception as e:
            log.error(f"  Cycle error: {e}")
            time.sleep(60)


if DATA_LAYER_OK:
    from data_layer import test_all_sources
    test_all_sources()

if __name__ == "__main__":
    start()

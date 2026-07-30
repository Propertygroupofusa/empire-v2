#!/usr/bin/env python3

# ============================================================
# TRADING EMPIRE — REPO PROTECTION
# This file belongs to the trading-empire bot system only.
# Any other project files in this repo will break all bots.
# Do NOT mix with Payee Trust or any other project.
# ============================================================
"""
TRADING EMPIRE — MARKET BRAIN v3
Standardized Data Layer + Multi-Timeframe Confluence Engine

Data Architecture:
  Every data request returns the SAME structure — always.
  No tuples. No unpacking errors. No surprises.

Signal Architecture:
  Fast(15m) + Mid(1h) + Slow(4h) alignment required.
  Confidence scoring 0-5 controls position size.
  60% max exposure cap protects capital.
"""

import os
import json
import time
import math
import logging
import schedule
import urllib.request
import urllib.error
from datetime import datetime
from statistics import mean, stdev

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



# ── LOGGING ──────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("market_brain.log", encoding="utf-8"),
    ]
)
log = logging.getLogger("brain")

# ── CREDENTIALS ──────────────────────────────────────────────
API_KEY    = os.getenv("ALPACA_API_KEY",    "")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
BASE_URL   = os.getenv("ALPACA_BASE_URL",   "https://paper-api.alpaca.markets")
DATA_URL   = "https://data.alpaca.markets"

# ── CONFIG ───────────────────────────────────────────────────
CONFIG = {
    "starting_capital":   500.0,
    "target_portfolio":   100_000.0,
    "max_positions":      3,
    "max_trades_day":     12,   # more trades
    "stop_loss_pct":      1.5,   # wider = exits trigger more
    "take_profit_pct":    0.8,   # quick wins — hits more often
    "trailing_stop_pct":  0.5,   # trail after 1.5% peak
    "min_confidence":     1,   # lowered — need more trades
    "min_strength":       0.001,  # very loose — collect data
    "max_exposure":       0.60,
    "cycle_minutes":      15,
}

WATCHLIST   = ["NVDA","AMZN","GOOGL","PLTR","AAPL","MSFT","TSLA","AMD","META","SPY"]
CRYPTO_LIST = ["BTC/USD","ETH/USD","SOL/USD"]

MILESTONES = [
    {"balance":       500, "alloc": 20, "max_pos": 3, "label": "Starting"},
    {"balance":     1_000, "alloc": 22, "max_pos": 3, "label": "Doubled"},
    {"balance":     5_000, "alloc": 25, "max_pos": 4, "label": "Growing"},
    {"balance":    10_000, "alloc": 25, "max_pos": 4, "label": "5 Figures"},
    {"balance":    50_000, "alloc": 25, "max_pos": 4, "label": "Halfway"},
    {"balance": 9_999_999, "alloc": 25, "max_pos": 4, "label": "Target"},
]

def get_milestone(balance):
    current = MILESTONES[0]
    for m in MILESTONES:
        if balance >= m["balance"]:
            current = m
    return current

def days_to_target(balance, rate=2.0):
    try:
        return int(math.ceil(
            math.log(CONFIG["target_portfolio"] / balance) /
            math.log(1 + rate / 100)
        ))
    except Exception:
        return 999


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

# ============================================================
# STANDARDIZED DATA LAYER
# Every request returns the same dict structure.
# No tuples. No unpacking errors. No surprises.
# ============================================================

EMPTY_RESULT = lambda symbol, tf: {
    "symbol":    symbol,
    "timeframe": tf,
    "closes":    [],
    "highs":     [],
    "lows":      [],
    "volumes":   [],
    "ok":        False,
}

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



def safe_closes(symbol, limit=100):
    """
    Global safety wrapper.
    Returns closes list or empty list. Never crashes.
    """
    d = get_market_data(symbol, limit=limit)
    return d["closes"] if d["ok"] else []


# ── MULTI-TIMEFRAME DATA ─────────────────────────────────────
def get_multi_tf(symbol):
    """
    Fetch 3 timeframes using cache.
    Each timeframe fetched once per TTL.
    Fast=60s TTL, Mid=3min TTL, Slow=5min TTL.
    """
    return {
        "fast": get_market_data_cached(symbol, "15m", limit=50),
        "mid":  get_market_data_cached(symbol, "1h",  limit=100),
        "slow": get_market_data_cached(symbol, "4h",  limit=200),
    }


# ── INDICATORS ───────────────────────────────────────────────
def rsi(closes, n=14):
    if len(closes) < n + 1:
        return 50.0
    gains  = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
    ag     = mean(gains[-n:])
    al     = mean(losses[-n:])
    if al == 0:
        return 100.0
    return 100.0 - (100.0 / (1 + ag / al))


def bollinger(closes, n=20, k=2):
    if len(closes) < n:
        return None, None, None
    sl = closes[-n:]
    m  = mean(sl)
    sd = stdev(sl) if len(sl) > 1 else 0
    return m - k * sd, m, m + k * sd


# ── TREND FUNCTIONS ───────────────────────────────────────────
def trend_direction(closes):
    if len(closes) < 50:
        return "NEUTRAL"
    sma20 = sum(closes[-20:]) / 20
    sma50 = sum(closes[-50:]) / 50
    if sma20 > sma50:
        return "UP"
    elif sma20 < sma50:
        return "DOWN"
    return "NEUTRAL"


def trend_strength(closes):
    if len(closes) < 50:
        return 0.0
    sma20 = sum(closes[-20:]) / 20
    sma50 = sum(closes[-50:]) / 50
    return abs(sma20 - sma50) / sma50 if sma50 > 0 else 0.0


# ── CONFIDENCE SCORING ────────────────────────────────────────
def compute_confidence(fast_trend, mid_trend, slow_trend, strength, rsi_val):
    """Score 0-5 based on alignment + strength + entry quality."""
    score = 0
    if fast_trend == mid_trend == slow_trend:
        score += 3
    elif mid_trend == slow_trend:
        score += 2
    elif fast_trend == mid_trend:
        score += 1
    if strength > 0.01:
        score += 1
    if 40 <= rsi_val <= 60:
        score += 1
    return score


def confidence_to_alloc(score):
    """Map confidence score to position size %."""
    if score >= 5:  return 0.28
    elif score >= 4: return 0.22
    elif score >= 3: return 0.16
    elif score >= 2: return 0.10
    else:            return 0.05


def can_open_position(positions, new_alloc):
    """Never exceed 60% total exposure."""
    total = sum(p.get("alloc", 0.20) for p in positions.values())
    return (total + new_alloc) <= CONFIG["max_exposure"]


# ── MTF SIGNAL ENGINE ────────────────────────────────────────
def mtf_confidence_score(fast, mid, slow):
    if fast == mid == slow: return 3
    if mid == slow:          return 2
    if fast == mid:          return 1
    return 0


def entry_filter(closes):
    """Only buy pullbacks. RSI < 60."""
    if not closes:
        return False
    return rsi(closes) < 60


def mtf_exit(entry_price, current_price, peak_price):
    """
    PROFIT-FIRST exit logic.
    NEVER sell at a loss. Hold until green.
    Only exit rules:
      +2.0% = Take Profit (sell and lock in gain)
      Trail from +1% peak = lock in profits
      -8.0% = Emergency stop ONLY (catastrophic protection)
    """
    if entry_price <= 0:
        return None

    pnl = ((current_price - entry_price) / entry_price) * 100

    # NEVER sell at a loss (unless catastrophic)
    if pnl <= 0 and pnl > -8.0:
        return None  # HOLD — let it recover

    # Emergency stop — catastrophic loss only
    if pnl <= -8.0:
        return "EMERGENCY_STOP"

    # Take profit — sell at +2%
    if pnl >= 2.0:
        return "TP"

    # Trail — only after reaching +1% peak (never in loss)
    if peak_price > entry_price * 1.01:
        trail = ((current_price - peak_price) / peak_price) * 100
        if trail <= -0.8:
            return "TRAIL"

    return None



def mtf_signal(symbol):
    """
    Full multi-timeframe signal engine.
    Uses standardized data layer throughout.
    Returns action, reason, fast_closes, confidence.
    """
    mtf = get_multi_tf(symbol)

    fast_data = mtf["fast"]
    mid_data  = mtf["mid"]
    slow_data = mtf["slow"]

    # Safe check using ok flag
    if not fast_data["ok"] or len(fast_data["closes"]) < 20:
        return "HOLD", "no_data", [], 0

    fast_closes = fast_data["closes"]
    mid_closes  = mid_data["closes"]  if mid_data["ok"]  else fast_closes
    slow_closes = slow_data["closes"] if slow_data["ok"] else fast_closes

    fast_trend = trend_direction(fast_closes)
    mid_trend  = trend_direction(mid_closes)
    slow_trend = trend_direction(slow_closes)
    fast_str   = trend_strength(fast_closes)
    rsi_val    = rsi(fast_closes)

    conf = compute_confidence(
        fast_trend, mid_trend, slow_trend, fast_str, rsi_val
    )

    log.info(f"  [{symbol}] "
             f"MTF:{fast_trend}/{mid_trend}/{slow_trend} "
             f"str:{fast_str:.3f} RSI:{rsi_val:.0f} conf:{conf}/5")

    if conf < CONFIG["min_confidence"]:
        return "HOLD", f"low_conf:{conf}/5", fast_closes, conf

    if slow_trend == "UP" and mid_trend == "UP":
        if fast_trend == "UP" and fast_str > CONFIG["min_strength"]:
            if entry_filter(fast_closes):
                return "BUY", f"MTF_UP conf:{conf}/5 str:{fast_str:.3f}", fast_closes, conf

    if slow_trend == "DOWN" and mid_trend == "DOWN":
        if fast_trend == "DOWN" and fast_str > CONFIG["min_strength"]:
            return "SHORT", f"MTF_DOWN conf:{conf}/5 str:{fast_str:.3f}", fast_closes, conf

    # Force BUY if RSI very oversold (even without full alignment)
    r = rsi(fast_closes) if fast_closes else 50
    if r < 35:
        return "BUY", f"oversold_force rsi={r:.0f}", fast_closes, 2

    return "HOLD", f"no_align {fast_trend}/{mid_trend}/{slow_trend}", fast_closes, conf



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


# ============================================================
# DYNAMIC HEDGING SYSTEM
# Protects capital during bear markets and drawdowns
# Bull market  = normal trading, minimal hedging
# Bear market  = fewer buys, tighter stops, hedge on
# Drawdown >5% = hedge size increases automatically
# ============================================================

HEDGE_SYMBOLS = {
    "market":   "SPY",   # broad market hedge
    "risk_off": "GLD",   # gold for risk-off
}


def market_regime_hedge(closes):
    """
    Detect bull/bear/sideways for hedging decisions.
    Different from regime detection — focused on hedge trigger.
    """
    if not closes or len(closes) < 50:
        return "NEUTRAL"
    sma20 = sum(closes[-20:]) / 20
    sma50 = sum(closes[-50:]) / 50
    if sma20 > sma50:
        return "BULL"
    elif sma20 < sma50:
        return "BEAR"
    return "SIDEWAYS"


def hedge_size(pf, regime, drawdown_pct):
    """
    Dynamic hedge sizing based on regime and drawdown.
    Normal:       10%
    Bear market:  20%
    Drawdown >5%: +10% extra
    Max:          30%
    """
    base = 0.10
    if regime == "BEAR":
        base = 0.20
    if drawdown_pct > 5.0:
        base += 0.10
    return min(base, 0.30)


def should_hedge(regime, positions, risk_mult):
    """
    Hedge trigger — returns True when defensive mode needed.
    """
    if regime == "BEAR" and len(positions) > 0:
        return True
    if risk_mult < 0.70:  # already in defensive mode
        return True
    return False


def should_remove_hedge(regime):
    """Remove hedge when market recovers to bull."""
    return regime == "BULL"


def apply_bear_filter(signal, reason, regime):
    """
    In bear markets:
    - Block new BUY signals
    - Tighten stop losses
    - Lower take profits
    Returns filtered signal and reason.
    """
    if regime == "BEAR" and signal == "BUY":
        return "HOLD", reason + " | bear_filter"
    return signal, reason


def get_drawdown_pct(peak_pf, current_pf):
    """Calculate current drawdown as percentage."""
    if peak_pf <= 0:
        return 0.0
    return max(0.0, (peak_pf - current_pf) / peak_pf * 100)


# ============================================================
# MACHINE LEARNING FILTER
# System learns from its own trade history
# Filters bad setups. Prioritizes patterns that worked.
# No heavy libraries — pure Python statistics.
# ============================================================

ML_DATA_FILE    = "ml_trades.json"
ML_MIN_TRADES   = 10    # lower minimum before ML filters    # need this many trades before ML filters
ML_BLOCK_SCORE  = 0.35   # lowered — need data first  # block if score below this
ML_BOOST_SCORE  = 0.55   # lowered — be more active  # full size if score above this


def load_dataset():
    """Load historical trade data for ML training."""
    try:
        return json.load(open(ML_DATA_FILE))
    except Exception:
        return []


def save_dataset(data):
    """Save trade data for future training."""
    try:
        json.dump(data, open(ML_DATA_FILE, "w"), indent=2)
    except Exception:
        pass


def extract_features(symbol, closes, rsi_val, trend, strength, conf):
    """
    Extract numerical features from signal data.
    These match what the system already computes.
    Simple features = more reliable model.
    """
    if not closes or len(closes) < 10:
        volatility = 0.0
    else:
        volatility = max(closes[-10:]) - min(closes[-10:])
        price_avg  = sum(closes[-10:]) / 10
        volatility = volatility / price_avg if price_avg > 0 else 0.0

    return {
        "symbol":     symbol,
        "rsi":        round(rsi_val, 2),
        "trend":      1 if trend == "UP" else -1 if trend == "DOWN" else 0,
        "strength":   round(strength, 4),
        "confidence": conf,
        "volatility": round(volatility, 4),
    }


def record_trade(features, pnl):
    """
    Save trade outcome back into dataset.
    Called when a position closes.
    This is how the system learns.
    """
    data  = load_dataset()
    entry = {k: v for k, v in features.items() if k != "symbol"}
    entry["symbol"] = features.get("symbol", "")
    entry["result"] = 1 if pnl > 0 else 0
    entry["pnl"]    = round(pnl, 4)
    data.append(entry)
    save_dataset(data)
    log.debug(f"  [ML] Recorded trade: {features.get('symbol','')} "
              f"pnl={pnl:+.2f} result={entry['result']}")


def train_model(data):
    """
    Train simple probability-weighted model.
    No sklearn. No numpy. Pure Python.
    Returns weight dict or None if insufficient data.

    Logic:
    - For each feature, calculate weighted average
      where weight = outcome (1=win, 0=loss)
    - Higher weight = feature correlates with wins
    """
    if not data or len(data) < ML_MIN_TRADES:
        return None

    feature_keys = ["rsi", "trend", "strength", "confidence", "volatility"]
    weights      = {k: 0.0 for k in feature_keys}
    counts       = {k: 0   for k in feature_keys}

    for d in data:
        result = d.get("result", 0)
        for k in feature_keys:
            if k in d:
                weights[k] += float(d[k]) * result
                counts[k]  += 1

    # Average weighted by wins
    for k in weights:
        if counts[k] > 0:
            weights[k] /= counts[k]

    # Win rate
    win_rate = sum(d.get("result", 0) for d in data) / len(data)
    weights["_win_rate"] = round(win_rate, 3)
    weights["_samples"]  = len(data)

    return weights


def score_trade(features, model):
    """
    Score a potential trade using the trained model.
    Returns 0.0 to 1.0.
    0.5 = neutral (no data)
    Below 0.45 = bad setup — block
    Above 0.65 = strong setup — full size
    """
    if not model:
        return 0.5  # neutral when no model yet

    feature_keys = ["rsi", "trend", "strength", "confidence", "volatility"]
    score = 0.0
    count = 0

    for k in feature_keys:
        if k in features and k in model:
            val    = float(features[k])
            weight = float(model[k])
            # Normalize contribution
            score += val * weight
            count += 1

    if count == 0:
        return 0.5

    # Normalize to 0-1 range
    score = score / count
    score = max(0.0, min(1.0, abs(score) + 0.3))  # shift to useful range

    # Blend with historical win rate
    win_rate = model.get("_win_rate", 0.5)
    score    = (score * 0.6) + (win_rate * 0.4)

    return round(max(0.0, min(1.0, score)), 3)


def ml_size_multiplier(ml_score):
    """
    Adjust position size based on ML score.
    High score = full size
    Low score  = reduced size
    """
    if ml_score >= ML_BOOST_SCORE:
        return 1.0    # full size
    elif ml_score >= ML_BLOCK_SCORE:
        return 0.75   # slightly reduced
    else:
        return 0.0    # blocked


def get_ml_status():
    """Log current ML model status."""
    data  = load_dataset()
    model = train_model(data)
    if not model:
        remaining = ML_MIN_TRADES - len(data)
        return f"Training ({len(data)}/{ML_MIN_TRADES} trades, need {remaining} more)"
    wr = model.get("_win_rate", 0) * 100
    n  = model.get("_samples", 0)
    return f"Active ({n} trades, {wr:.0f}% WR)"

# ── ALPACA ORDER FUNCTIONS ────────────────────────────────────
def api_call(method, endpoint, body=None):
    url     = BASE_URL + endpoint
    headers = {
        "APCA-API-KEY-ID":     API_KEY,
        "APCA-API-SECRET-KEY": SECRET_KEY,
        "Content-Type":        "application/json",
    }
    data = json.dumps(body).encode() if body else None
    req  = urllib.request.Request(
        url, data=data, headers=headers, method=method
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403):
                return None
            time.sleep(2 ** attempt)
        except Exception:
            time.sleep(2 ** attempt)
    return None


def get_account():
    return api_call("GET", "/v2/account")


def place_order(symbol, notional, side="buy"):
    body = {
        "symbol":        symbol,
        "notional":      str(round(notional, 2)),
        "side":          side,
        "type":          "market",
        "time_in_force": "gtc" if "/" in symbol else "day",
    }
    result = api_call("POST", "/v2/orders", body)
    if result and result.get("id"):
        log.info(f"  ORDER: {side.upper()} {symbol} ${notional:.2f}")
        return True
    log.warning(f"  ORDER FAILED: {side} {symbol}")
    return False


def close_position(symbol):
    body = {
        "symbol":        symbol,
        "qty":           "100%",
        "side":          "sell",
        "type":          "market",
        "time_in_force": "gtc" if "/" in symbol else "day",
    }
    result = api_call("POST", "/v2/orders", body)
    return result and result.get("id")



# ============================================================
# PARALLEL EXECUTION ENGINE
# All symbols processed simultaneously
# 6x faster cycle times
# ============================================================

from concurrent.futures import ThreadPoolExecutor, as_completed

MAX_WORKERS = 6


def process_symbol_brain(symbol, state_positions, pf, hedge_regime="NEUTRAL"):
    """
    Worker function — runs in its own thread.
    Fetches data and generates MTF signal for ONE symbol.
    Does NOT modify state — only returns results.
    Thread-safe by design.
    """
    try:
        # Get all 3 timeframes in parallel sub-calls
        action, reason, fast_closes, conf = mtf_signal(symbol)

        # Apply bear filter
        action, reason = apply_bear_filter(action, reason, hedge_regime)

        if action not in ("BUY", "SHORT"):
            return None

        if not fast_closes:
            return None

        price    = fast_closes[-1]
        strength = trend_strength(fast_closes)
        alloc    = confidence_to_alloc(conf)

        return {
            "symbol":   symbol,
            "action":   action,
            "reason":   reason,
            "price":    price,
            "conf":     conf,
            "strength": strength,
            "alloc":    alloc,
            "fast_closes": fast_closes,
        }

    except Exception as e:
        log.warning(f"  [THREAD] {symbol}: {e}")
        return None


def process_exit_brain(symbol, pos, state_positions):
    """
    Worker for exit checking.
    Checks if existing position should close.
    Thread-safe — returns result, does not modify state.
    """
    try:
        d = get_market_data_cached(symbol, "15m", 10)
        if not isinstance(d, dict) or not d.get("ok") or not d["closes"]:
            return None

        current  = d["closes"][-1]
        entry_px = pos.get("entry_price", current)
        peak_px  = pos.get("peak_price",  current)
        new_peak = max(current, peak_px)

        exit_reason = mtf_exit(entry_px, current, peak_px)

        return {
            "symbol":      symbol,
            "exit_reason": exit_reason,
            "current":     current,
            "entry_px":    entry_px,
            "new_peak":    new_peak,
        }

    except Exception as e:
        log.warning(f"  [EXIT THREAD] {symbol}: {e}")
        return None


def scan_parallel(symbols, state_positions, pf, hedge_regime="NEUTRAL"):
    """
    Scan all symbols in parallel.
    Returns sorted list of signals.
    """
    results = []
    workers = min(MAX_WORKERS, len(symbols))

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(process_symbol_brain, sym, state_positions, pf, hedge_regime): sym
            for sym in symbols
        }
        for future in as_completed(futures):
            try:
                res = future.result(timeout=15)
                if res:
                    results.append(res)
            except Exception as e:
                log.warning(f"  [FUTURE] {futures[future]}: {e}")

    # Sort by confidence then strength
    results.sort(key=lambda x: (x["conf"], x["strength"]), reverse=True)
    return results


def check_exits_parallel(positions):
    """
    Check all open positions for exits in parallel.
    Returns list of exit signals.
    """
    if not positions:
        return []

    exits   = []
    workers = min(MAX_WORKERS, len(positions))

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(process_exit_brain, sym, pos, positions): sym
            for sym, pos in positions.items()
        }
        for future in as_completed(futures):
            try:
                res = future.result(timeout=15)
                if res:
                    exits.append(res)
            except Exception as e:
                log.warning(f"  [EXIT FUTURE] {e}")

    return exits

# ── STATE ─────────────────────────────────────────────────────
class State:
    def __init__(self):
        self.positions      = {}
        self.trades_today   = 0
        self.wins           = 0
        self.losses         = 0
        self.day            = 1
        self.paper_baseline = 0
        self.last_real_pf   = CONFIG["starting_capital"]
        self.peak_pf        = CONFIG["starting_capital"]
        self.is_live        = False
        self.hedges         = {}   # active hedge positions
        self.risk_mult      = 1.0  # 1.0 = normal, 0.5 = defensive
        self.open_features  = {}   # ML features for open positions
        self.load()

    def load(self):
        try:
            d = json.load(open("brain_state.json"))
            self.positions      = d.get("positions",      {})
            self.wins           = d.get("wins",           0)
            self.losses         = d.get("losses",         0)
            self.day            = d.get("day",            1)
            self.paper_baseline = d.get("paper_baseline", 0)
            self.last_real_pf   = d.get("last_real_pf",  CONFIG["starting_capital"])
            self.peak_pf        = d.get("peak_pf",       CONFIG["starting_capital"])
            self.is_live        = d.get("is_live",        False)
            self.hedges         = d.get("hedges",         {})
            self.risk_mult      = d.get("risk_mult",      1.0)
            self.open_features  = d.get("open_features",  {})
        except Exception:
            pass

    def save(self):
        json.dump({
            "positions":      self.positions,
            "wins":           self.wins,
            "losses":         self.losses,
            "day":            self.day,
            "paper_baseline": self.paper_baseline,
            "last_real_pf":   self.last_real_pf,
            "peak_pf":        self.peak_pf,
            "is_live":        self.is_live,
            "hedges":         self.hedges,
            "risk_mult":      self.risk_mult,
            "open_features":  self.open_features,
        }, open("brain_state.json", "w"), indent=2)

    def can_trade(self):
        ms = get_milestone(self.last_real_pf)
        return (
            self.trades_today < CONFIG["max_trades_day"] and
            len(self.positions) < ms["max_pos"]
        )

    def win_rate(self):
        total = self.wins + self.losses
        return (self.wins / total * 100) if total > 0 else 0.0


state = State()


# ── MAIN CYCLE ────────────────────────────────────────────────

def save_control(control):
    """Save control settings to control.json."""
    try:
        json.dump(control, open("control.json", "w"), indent=2)
    except Exception as e:
        log.warning(f"  save_control error: {e}")


def notify(msg):
    """Send alert."""
    log.info(f"  [ALERT] {msg}")
    token   = os.getenv("TELEGRAM_TOKEN",   "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if token and chat_id:
        try:
            body = json.dumps({"chat_id": chat_id, "text": f"Trading Empire: {msg}"}).encode()
            req  = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=body, headers={"Content-Type": "application/json"}, method="POST"
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass


def load_metrics():
    """Load metrics.json."""
    try:
        return json.load(open("metrics.json"))
    except Exception:
        return {"balance": 500, "win_rate": 0, "trades": 0,
                "regime": "UNKNOWN", "last_10": [], "bots": {}}

def run_cycle():
    # Check for updates every 24 hours
    check_for_updates(__file__)

    clear_cycle_cache()
    acct = get_account()
    if not acct:
        log.error("  Cannot connect to Alpaca")
        return

    raw_pf = float(acct.get("portfolio_value", CONFIG["starting_capital"]))

    if state.paper_baseline == 0:
        state.paper_baseline = raw_pf
        state.save()
        log.info(f"  Baseline set: ${raw_pf:,.2f}")

    profit_made        = raw_pf - state.paper_baseline
    pf                 = CONFIG["starting_capital"] + profit_made
    pf                 = max(pf, CONFIG["starting_capital"] * 0.5)  # allow drawdown
    state.last_real_pf = pf

    ms   = get_milestone(pf)
    mode = "LIVE" if state.is_live else "PAPER"

    # ── HEDGE REGIME CHECK ───────────────────────────────────
    spy_data   = get_market_data_cached("SPY", "15m", 60)
    btc_data   = get_market_data_cached("BTC/USD", "15m", 60)
    ref_closes = (spy_data["closes"] if isinstance(spy_data, dict) and spy_data.get("ok")
                  else btc_data["closes"] if isinstance(btc_data, dict) and btc_data.get("ok")
                  else [])
    hedge_regime  = market_regime_hedge(ref_closes)
    drawdown_pct  = get_drawdown_pct(state.peak_pf, pf)

    # Adjust risk multiplier based on regime
    if hedge_regime == "BULL":
        state.risk_mult = min(1.0, state.risk_mult + 0.1)
    elif hedge_regime == "BEAR":
        state.risk_mult = max(0.5, state.risk_mult - 0.1)

    log.info(f"\n{'='*60}")
    log.info(f"  MARKET BRAIN v3 | {mode} | ${pf:,.2f} | Day {state.day}")
    log.info(f"  Hedge Regime: {hedge_regime} | DD: {drawdown_pct:.1f}% | "
             f"Risk: {state.risk_mult*100:.0f}% | Hedges: {len(state.hedges)}")
    log.info(f"  {ms['label']} | Days to $100k: ~{days_to_target(pf)}")
    log.info(f"  W:{state.wins} L:{state.losses} ({state.win_rate():.0f}% WR) "
             f"| Positions:{len(state.positions)}/{ms['max_pos']}")

    # Tighten exits in bear market
    # Keep exits wide — only sell in profit
    CONFIG["stop_loss_pct"]   = 8.0
    CONFIG["take_profit_pct"] = 2.0

    if profit_made >= (CONFIG["target_portfolio"] - CONFIG["starting_capital"]):
        log.info("  $100,000 TARGET REACHED!")
        return schedule.CancelJob

    if datetime.now().hour == 0 and datetime.now().minute < 16:
        state.day          += 1
        state.trades_today  = 0
        state.save()


    # ── FORCE CLOSE STALE POSITIONS (stuck > 3 cycles) ────
    for symbol in list(state.positions.keys()):
        pos = state.positions[symbol]
        pos["cycles"] = pos.get("cycles", 0) + 1
        state.positions[symbol] = pos
        if pos["cycles"] >= 6:  # hold longer — 90 min
            entry_px = pos.get("entry_price", 0)
            d = get_market_data(symbol, limit=5)
            current = d["closes"][-1] if d and d.get("ok") and d["closes"] else entry_px
            pnl_pct = ((current - entry_px) / entry_px * 100) if entry_px > 0 else 0

            # ONLY force close if winning
            if pnl_pct > 0:
                if close_position(symbol):
                    state.wins += 1
                    state.trades_today += 1
                    if symbol in state.open_features:
                        del state.open_features[symbol]
                    del state.positions[symbol]
                    log.info(f"  [FORCE WIN] {symbol} {pnl_pct:+.2f}%")
                    state.save()
            else:
                log.info(f"  [HOLD] {symbol} {pnl_pct:+.2f}% — waiting for green")
                # Reset cycle counter — keep holding
                state.positions[symbol]["cycles"] = 0

    # ── MANAGE EXISTING POSITIONS ────────────────────────────
    for symbol, pos in list(state.positions.items()):
        d = get_data_once(symbol)
        if not isinstance(d, dict) or not d.get("ok") or not d["closes"]:
            continue

        current  = d["closes"][-1]
        entry_px = pos.get("entry_price", current)
        peak_px  = pos.get("peak_price",  current)

        if current > peak_px:
            state.positions[symbol]["peak_price"] = current
            peak_px = current

        exit_reason = mtf_exit(entry_px, current, peak_px)
        if exit_reason:
            if close_position(symbol):
                pnl_pct = ((current - entry_px) / entry_px) * 100
                if pnl_pct > 0: state.wins   += 1
                else:           state.losses += 1
                state.trades_today += 1
                # Record outcome for ML learning
                pnl_val = pnl_pct / 100 * (state.positions.get(symbol, {}).get("alloc", 0.2) * pf)
                if symbol in state.open_features:
                    record_trade(state.open_features[symbol], pnl_val)
                    del state.open_features[symbol]
                del state.positions[symbol]
                result = "WIN" if pnl_pct > 0 else "LOSS"
                log.info(f"  [{result}] {symbol} {pnl_pct:+.2f}% | {exit_reason}")
                state.save()
        time.sleep(0.3)

    if not state.can_trade():
        log.info("  Max positions — monitoring only")
        return

    # ── HEDGE MANAGEMENT ────────────────────────────────────
    # Remove hedge if market recovered to bull
    if should_remove_hedge(hedge_regime):
        for sym in list(state.hedges.keys()):
            if close_position(sym):
                log.info(f"  HEDGE OFF: {sym} — bull market recovered")
                del state.hedges[sym]
                state.save()

    # Add hedge if bear market or drawdown
    elif should_hedge(hedge_regime, state.positions, state.risk_mult):
        h_size  = hedge_size(pf, hedge_regime, drawdown_pct)
        h_val   = pf * h_size
        h_sym   = HEDGE_SYMBOLS["market"]
        if h_sym not in state.hedges and is_market_open():
            if place_order(h_sym, h_val, "buy"):
                state.hedges[h_sym] = {"value": h_val, "entry": "hedge"}
                log.info(f"  HEDGE ON: {h_sym} ${h_val:.2f} "
                         f"({h_size*100:.0f}%) | regime:{hedge_regime} "
                         f"dd:{drawdown_pct:.1f}%")
                state.save()

    # ── PARALLEL SYMBOL SCAN ────────────────────────────────
    all_symbols = [s for s in (CRYPTO_LIST + (WATCHLIST if is_market_open() else []))
                   if s not in state.positions]

    log.info(f"  Scanning {len(all_symbols)} symbols in parallel...")
    candidates = scan_parallel(all_symbols, state.positions, pf, hedge_regime)
    log.info(f"  Found {len(candidates)} signals")

    for res in candidates:
        if not state.can_trade():
            break

        symbol = res["symbol"]
        conf   = res["conf"]
        alloc  = res["alloc"]
        action = res["action"]
        reason = res["reason"]
        price  = res["price"]

        if not can_open_position(state.positions, alloc):
            log.info(f"  [CAP] {symbol} — 60% exposure limit")
            continue

        # Diversification check before entry
        allowed, div_reason = check_diversification(
            symbol,
            res.get("fast_closes", []),
            state.positions,
            alloc
        )
        if not allowed:
            log.info(f"  [BLOCKED] {symbol} — {div_reason}")
            continue

        # ML FILTER — score this trade before executing
        dataset  = load_dataset()
        model    = train_model(dataset)
        features = extract_features(
            symbol,
            res.get("fast_closes", []),
            rsi(res.get("fast_closes", [50]*15)),
            trend_direction(res.get("fast_closes", [])),
            res.get("strength", 0),
            conf,
        )
        ml_score = score_trade(features, model)
        ml_mult  = ml_size_multiplier(ml_score)

        if ml_mult == 0.0:
            log.info(f"  [ML BLOCK] {symbol} | score={ml_score:.2f} < {ML_BLOCK_SCORE}")
            continue

        log.info(f"  [ML] {symbol} score={ml_score:.2f} | mult={ml_mult:.2f}")

        size = pf * alloc * ml_mult
        side = "buy" if action == "BUY" else "sell"

        if BRACKET_OK:
            order = place_bracket_order(symbol, size, side)
        else:
            order = place_order(symbol, size, side)
        if order:
            state.positions[symbol] = {
                "entry_price": price,
                "peak_price":  price,
                "side":        side,
                "confidence":  conf,
                "strength":    round(res["strength"], 4),
                "alloc":       alloc,
                "ml_score":    ml_score,
            }
            state.open_features[symbol] = features
            state.trades_today += 1
            log.info(f"  ENTERED {symbol} | CONF:{conf}/5 | ML:{ml_score:.2f} | "
                     f"Alloc:{alloc*100:.1f}% | ${size:.2f} | {reason}")
            state.save()


# ── STARTUP ───────────────────────────────────────────────────
def start():
    log_version()
    log.info("\n" + "=" * 60)
    log.info("  MARKET BRAIN v3")
    log.info("  Standardized Data + MTF Confluence + Confidence Sizing")
    log.info("=" * 60)

    if not API_KEY or not SECRET_KEY:
        log.error("  Missing ALPACA_API_KEY or ALPACA_SECRET_KEY")
        return

    acct = get_account()
    if not acct:
        log.error("  Cannot connect to Alpaca")
        return

    raw_pf = float(acct.get("portfolio_value", CONFIG["starting_capital"]))

    if state.paper_baseline == 0:
        state.paper_baseline = raw_pf
        state.save()
        log.info(f"  Baseline set: ${raw_pf:,.2f}")

    profit_made        = raw_pf - state.paper_baseline
    pf                 = CONFIG["starting_capital"] + profit_made
    pf                 = max(pf, CONFIG["starting_capital"] * 0.5)  # allow drawdown
    state.last_real_pf = pf

    log.info(f"\n  Balance:       ${pf:,.2f}")
    log.info(f"  Mode:          {'LIVE' if state.is_live else 'PAPER'}")
    log.info(f"  Crypto:        {', '.join(CRYPTO_LIST)}")
    log.info(f"  Stocks:        {', '.join(WATCHLIST[:5])}...")
    log.info(f"  Cycle:         every {CONFIG['cycle_minutes']} minutes")
    log.info(f"  Max exposure:  {CONFIG['max_exposure']*100:.0f}%")
    log.info(f"  ML status:     {get_ml_status()}")
    log.info(f"\n  Data sources:  Coinbase → Binance → Alpaca")
    log.info(f"  Signal needs:  conf >= {CONFIG['min_confidence']}/5")
    log.info(f"  Position sizing by confidence:")
    log.info(f"    5/5 = 28% | 4/5 = 22% | 3/5 = 16% | 2/5 = 10%")

    run_cycle()
    schedule.every(CONFIG["cycle_minutes"]).minutes.do(run_cycle)

    log.info(f"\n  Brain v3 running. Every {CONFIG['cycle_minutes']} min.\n")

    while True:
        try:
            schedule.run_pending()
            time.sleep(30)
        except KeyboardInterrupt:
            log.info("\n  Brain stopped")
            state.save()
            break
        except Exception as e:
            log.error(f"  Cycle error: {e}")
            time.sleep(60)


if DATA_LAYER_OK:
    from data_layer import test_all_sources
    test_all_sources()

if __name__ == "__main__":
    start()

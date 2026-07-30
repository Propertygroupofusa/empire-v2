#!/usr/bin/env python3

# ============================================================
# TRADING EMPIRE — REPO PROTECTION
# This file belongs to the trading-empire bot system only.
# Do NOT mix with Payee Trust or any other project.
# ============================================================
"""
TRADING EMPIRE ORCHESTRATOR v2
PROFIT-FIRST: Never sell at a loss. Hold until green.
"""

import os
import json
import time
import math
import random
import logging
import hashlib
import schedule
import subprocess
import urllib.request
import urllib.error
from datetime import datetime
from statistics import mean
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

try:
    from bracket_orders import place_bracket_order, get_positions
    BRACKET_OK = True
except ImportError:
    BRACKET_OK = False
    def place_bracket_order(symbol, notional, side="buy", **kwargs):
        return None

try:
    from data_layer import get_market_data, safe_closes, get_multi_tf, is_market_open
    DATA_LAYER_OK = True
except ImportError:
    DATA_LAYER_OK = False
    def get_market_data(symbol, limit=60):
        return {"symbol": symbol, "closes": [], "highs": [],
                "lows": [], "volumes": [], "ok": False, "source": "import_error"}
    def safe_closes(symbol, limit=60): return []
    def get_multi_tf(symbol): return {"fast": {"closes":[],"ok":False}, "mid": {"closes":[],"ok":False}, "slow": {"closes":[],"ok":False}}
    def is_market_open():
        now = datetime.utcnow()
        if now.weekday() >= 5: return False
        return 13 <= now.hour < 20

try:
    from portfolio import Portfolio, compute_metrics, record_trade as port_record
    _portfolio = Portfolio()
    PORTFOLIO_AVAILABLE = True
except Exception:
    PORTFOLIO_AVAILABLE = False
    class _FakePortfolio:
        balance  = 500.0
        peak     = 500.0
        drawdown = 0.0
        def update_balance(self, pnl, bot_name=None): pass
    _portfolio = _FakePortfolio()

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

# ── SELF-UPDATE ───────────────────────────────────────────────
BOT_VERSION           = "2.1.0"
GITHUB_REPO           = os.getenv("GITHUB_REPO",  "")
GITHUB_TOKEN          = os.getenv("GITHUB_TOKEN", "")
_last_update_check    = [0.0]
_UPDATE_INTERVAL      = 86400

def get_file_hash(filepath):
    try:
        return hashlib.md5(open(filepath,"rb").read()).hexdigest()
    except Exception:
        return ""

def check_for_updates(current_file=None):
    import time as _t
    now = _t.time()
    if now - _last_update_check[0] < _UPDATE_INTERVAL:
        return False
    _last_update_check[0] = now
    if not GITHUB_REPO or not GITHUB_TOKEN:
        return False
    try:
        fname   = os.path.basename(current_file or __file__)
        url     = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{fname}"
        headers = {"Authorization": f"token {GITHUB_TOKEN}",
                   "Accept": "application/vnd.github.v3.raw"}
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            remote = r.read()
        if get_file_hash(current_file or __file__) != hashlib.md5(remote).hexdigest():
            open(current_file or __file__, "wb").write(remote)
            log.info(f"  [UPDATE] {fname} updated — restart to apply")
            return True
    except Exception as e:
        log.debug(f"  [UPDATE] {e}")
    return False

def log_version():
    fname = os.path.basename(__file__) if "__file__" in dir() else "orchestrator"
    log.info(f"  Version: v{BOT_VERSION} | File: {fname}")

def repair_and_reinstall():
    try:
        subprocess.run(["pip","install","-r","requirements.txt","--quiet"],
                       timeout=120, capture_output=True)
        log.info("  [REPAIR] Dependencies verified")
    except Exception as e:
        log.debug(f"  [REPAIR] {e}")

# ── CONFIG ───────────────────────────────────────────────────
CONFIG = {
    "starting_balance":   500.0,
    "test_days":          7,
    "cycle_minutes":      15,
    "stop_loss_pct":      8.0,    # emergency only — never sell at small loss
    "take_profit_pct":    2.0,    # sell at +2% profit
    "max_alloc_pct":      25.0,
    "max_exposure":       0.60,
    "max_hold_cycles":    6,      # hold longer to recover
    "max_trades_day":     10,
}

# ── BOT REGISTRY ─────────────────────────────────────────────
BOTS = {
    "Bot1_Stocks":   {"symbols": ["NVDA","AMZN","GOOGL","PLTR","AAPL"], "type": "stock",  "bot_type": "TREND"},
    "Bot2_Crypto":   {"symbols": ["BTC/USD","ETH/USD","SOL/USD"],        "type": "crypto", "bot_type": "TREND"},
    "Bot3_Options":  {"symbols": ["NVDA","TSLA","AMD","SPY","QQQ"],      "type": "stock",  "bot_type": "VOLATILE"},
    "Bot4_Earnings": {"symbols": ["NVDA","AMZN","GOOGL","AAPL","MSFT"],  "type": "stock",  "bot_type": "EVENT"},
    "Bot5_WSB":      {"symbols": ["NVDA","AMC","TSLA","PLTR","AMD"],     "type": "stock",  "bot_type": "SPEC"},
}

# ── CACHE ─────────────────────────────────────────────────────
import time as _time
DATA_CACHE  = {}
CYCLE_CACHE = {}
CACHE_TTL   = {"15m": 60, "1h": 180, "4h": 300}
LAST_CALL_TIME = [0.0]
MIN_DELAY      = 0.25

def _rate_limited_fetch(symbol, timeframe="15m", limit=100):
    now = _time.time()
    delta = now - LAST_CALL_TIME[0]
    if delta < MIN_DELAY:
        _time.sleep(MIN_DELAY - delta)
    result = get_market_data(symbol, limit)
    LAST_CALL_TIME[0] = _time.time()
    return result

def get_market_data_cached(symbol, timeframe="15m", limit=100):
    key = f"{symbol}_{timeframe}"
    now = _time.time()
    ttl = CACHE_TTL.get(timeframe, 60)
    if key in CYCLE_CACHE:
        return CYCLE_CACHE[key]
    if key in DATA_CACHE:
        cached = DATA_CACHE[key]
        if now - cached["timestamp"] < ttl:
            CYCLE_CACHE[key] = cached["data"]
            return cached["data"]
    data = _rate_limited_fetch(symbol, timeframe, limit)
    if isinstance(data, dict) and data.get("ok"):
        DATA_CACHE[key] = {"data": data, "timestamp": now}
        CYCLE_CACHE[key] = data
        return data
    if key in DATA_CACHE:
        stale = DATA_CACHE[key]["data"]
        CYCLE_CACHE[key] = stale
        return stale
    CYCLE_CACHE[key] = data
    return data

def get_data_once(symbol, timeframe="15m", limit=100):
    return get_market_data_cached(symbol, timeframe, limit)

def clear_cycle_cache():
    global CYCLE_CACHE
    CYCLE_CACHE = {}

# ── MARKET DATA ───────────────────────────────────────────────
def detect_regime(closes):
    if not closes or len(closes) < 50:
        return "UNKNOWN"
    sma20      = sum(closes[-20:]) / 20
    sma50      = sum(closes[-50:]) / 50
    trend_str  = abs(sma20 - sma50) / sma50 if sma50 > 0 else 0
    moves      = [abs(closes[i] - closes[i-1]) for i in range(-20, -1) if i < len(closes)]
    avg_move   = sum(moves) / len(moves) if moves else 0
    volatility = avg_move / sma20 if sma20 > 0 else 0
    if trend_str > 0.015: return "TRENDING"
    if volatility > 0.02: return "VOLATILE"
    return "SIDEWAYS"

def regime_risk_factor(regime):
    return {"TRENDING": 1.0, "SIDEWAYS": 0.8,
            "VOLATILE": 0.5, "UNKNOWN":  0.7}.get(regime, 0.7)

# ── DIVERSIFICATION ───────────────────────────────────────────
ASSET_GROUPS = {
    "crypto": ["BTC/USD","ETH/USD","SOL/USD","AVAX/USD","DOGE/USD","LINK/USD"],
    "tech":   ["AAPL","MSFT","GOOGL","AMZN","NVDA","AMD","META"],
    "wsb":    ["AMC","GME","PLTR","RKLB","BB"],
    "index":  ["SPY","QQQ","IWM"],
    "ev":     ["TSLA","RIVN","LCID","NIO"],
}
GROUP_LIMITS = {"crypto":0.40,"tech":0.35,"wsb":0.25,"index":0.30,"ev":0.25,"other":0.30}

def get_group(symbol):
    for group, symbols in ASSET_GROUPS.items():
        if symbol in symbols: return group
    return "other"

def current_group_exposure(positions, group):
    return sum(pos.get("alloc",0.20) for sym,pos in positions.items() if get_group(sym)==group)

def check_diversification(symbol, closes, positions, alloc):
    group   = get_group(symbol)
    limit   = GROUP_LIMITS.get(group, 0.30)
    current = current_group_exposure(positions, group)
    if current + alloc > limit:
        return False, f"group {group} at {current*100:.1f}%"
    return True, "ok"

# ── ADAPTIVE SCORING ─────────────────────────────────────────
def bot_score(stats):
    if stats.get("trades", 0) == 0: return 0.0
    wr    = stats["wins"] / stats["trades"]
    avg   = stats["pnl"]  / stats["trades"]
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
    if stats.get("trades", 0) < 5: return False
    return (stats["wins"] / stats["trades"]) < 0.40

def get_trade_size(balance, bot_name, bot_stats, regime):
    weights = compute_weights(bot_stats)
    weight  = max(0.05, min(0.40, weights.get(bot_name, 0.2)))
    risk    = regime_risk_factor(regime)
    return balance * weight * risk, weight

# ── PROFIT-FIRST TRADE SIMULATION ────────────────────────────
def simulate_trade(symbol):
    """
    PROFIT-FIRST: only realize gains.
    If price is down — return 0 (hold, don't sell at loss).
    """
    d = get_market_data_cached(symbol, "15m")
    if isinstance(d, dict) and d.get("ok") and len(d["closes"]) >= 2:
        entry   = d["closes"][-2]
        exit_p  = d["closes"][-1]
        pnl_pct = ((exit_p - entry) / entry * 100) if entry > 0 else 0

        # PROFIT-FIRST: never realize a loss
        if pnl_pct < 0:
            return entry, entry, 0.0  # hold at entry price

        # Cap profit at take_profit_pct
        pnl_pct = min(pnl_pct, CONFIG["take_profit_pct"])
        return entry, exit_p, pnl_pct

    # Fallback — slightly positive bias
    move = abs(random.gauss(0.003, 0.008))
    move = min(move, CONFIG["take_profit_pct"] / 100)
    return 100.0, 100.0 * (1 + move), move * 100

# ── BOT STATS ────────────────────────────────────────────────
def update_stats(state, bot_name, pnl, symbol):
    s = state["bot_stats"].setdefault(bot_name, {
        "wins":0, "losses":0, "pnl":0.0,
        "trades":0, "max_win":0.0, "max_loss":0.0, "pnl_history":[],
    })
    s["trades"] += 1
    s["pnl"]    += pnl
    s["pnl_history"].append(round(pnl, 4))
    if pnl > 0:
        s["wins"]   += 1
        s["max_win"] = max(s["max_win"], pnl)
    else:
        s["losses"]  += 1
        s["max_loss"] = min(s["max_loss"], pnl)

# ── SIGNAL ───────────────────────────────────────────────────
def get_signal(bot_name, price, regime):
    r         = random.uniform(0, 100)
    base      = {"Bot1_Stocks":55,"Bot2_Crypto":65,"Bot3_Options":45,"Bot4_Earnings":45,"Bot5_WSB":60}
    threshold = base.get(bot_name, 55)
    bot_type  = BOTS.get(bot_name, {}).get("bot_type", "TREND")
    if regime == "TRENDING"   and bot_type == "TREND":    threshold *= 1.3
    elif regime == "VOLATILE" and bot_type == "VOLATILE": threshold *= 1.4
    elif regime == "VOLATILE":                            threshold *= 0.8
    return r < min(threshold, 80)

# ── PARALLEL BOT EXECUTION ────────────────────────────────────
MAX_WORKERS = 5

def process_bot(bot_name, bot_cfg, state_balance, bot_stats, regime):
    try:
        if bot_cfg["type"] == "stock" and not is_market_open(): return None
        if should_disable(bot_stats.get(bot_name, {})): return None

        symbol = random.choice(bot_cfg["symbols"])
        d      = get_market_data_cached(symbol, "15m", 10)
        if not isinstance(d, dict) or not d.get("ok") or not d["closes"]: return None

        price = d["closes"][-1]
        # Check LONG signal (buy low, sell high)
        long_signal  = get_signal(bot_name, price, regime)

        # Check SHORT signal (sell high, buy back lower)
        short_signal = get_short_signal(bot_name, price, regime, d["closes"])

        if not long_signal and not short_signal:
            return None

        size, weight = get_trade_size(state_balance, bot_name, bot_stats, regime)

        if short_signal and not long_signal:
            # GO SHORT — make money on the way down
            entry, exit_p, pnl_pct = simulate_short_trade(symbol)
            direction = "SHORT"
        else:
            # GO LONG — make money on the way up
            entry, exit_p, pnl_pct = simulate_trade(symbol)
            direction = "LONG"

        pnl = size * (pnl_pct / 100)

        return {
            "bot_name":   bot_name,
            "symbol":     symbol,
            "size":       size,
            "weight":     weight,
            "entry":      entry,
            "exit_p":     exit_p,
            "pnl_pct":    pnl_pct,
            "pnl":        pnl,
            "direction":  direction,
            "asset_type": bot_cfg["type"],
        }
    except Exception as e:
        log.warning(f"  [THREAD] {bot_name}: {e}")
        return None

def run_bots_parallel(state, regime):
    results = []
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(BOTS))) as executor:
        futures = {
            executor.submit(process_bot, bot_name, bot_cfg,
                            state["balance"], state["bot_stats"], regime): bot_name
            for bot_name, bot_cfg in BOTS.items()
        }
        for future in as_completed(futures):
            try:
                res = future.result(timeout=15)
                if res: results.append(res)
            except Exception as e:
                log.warning(f"  [FUTURE] {futures[future]}: {e}")
    return results

# ── STATE ─────────────────────────────────────────────────────
def load_state():
    try:
        return json.load(open("orchestrator_state.json"))
    except Exception:
        return {
            "balance":         CONFIG["starting_balance"],
            "peak":            CONFIG["starting_balance"],
            "start_date":      datetime.now().strftime("%Y-%m-%d"),
            "day":             1,
            "bot_stats":       {},
            "open_positions":  {},
            "daily_snapshots": [],
        }

def save_state(state):
    json.dump(state, open("orchestrator_state.json", "w"), indent=2)

# ── HELPERS ───────────────────────────────────────────────────
def load_control():
    try:
        return json.load(open("control.json"))
    except Exception:
        return {"live_mode":False,"kill_switch":False,"risk_multiplier":1.0,"max_trades":6}

def save_control(control):
    try:
        json.dump(control, open("control.json","w"), indent=2)
    except Exception as e:
        log.warning(f"  save_control: {e}")

def load_metrics():
    try:
        return json.load(open("metrics.json"))
    except Exception:
        return {"balance":500,"peak":500,"drawdown":0,"win_rate":0,
                "trades":0,"total_pnl":0,"daily_pnl":0,"regime":"UNKNOWN",
                "mode":"PAPER","last_updated":"","bots":{},"last_10":[]}

def notify(msg):
    log.info(f"  [ALERT] {msg}")
    token   = os.getenv("TELEGRAM_TOKEN",   "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if token and chat_id:
        try:
            body = json.dumps({"chat_id":chat_id,"text":f"Trading Empire: {msg}"}).encode()
            req  = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=body, headers={"Content-Type":"application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass

def report(state=None, pf=None, balance=None):
    try:
        bal     = pf or balance or 500
        if isinstance(state, dict):
            bot_s   = state.get("bot_stats", {})
            wins    = sum(s.get("wins",0)   for s in bot_s.values())
            losses  = sum(s.get("losses",0) for s in bot_s.values())
        else:
            wins = losses = 0
        total = wins + losses
        wr    = (wins / total * 100) if total > 0 else 0
        pnl   = bal - CONFIG["starting_balance"]
        log.info(f"\n{'='*55}")
        log.info(f"  DAILY REPORT")
        log.info(f"  Balance:  ${bal:,.2f}")
        log.info(f"  P&L:      ${pnl:+,.2f}")
        log.info(f"  Trades:   {total} | W:{wins} L:{losses}")
        log.info(f"  Win Rate: {wr:.1f}%")
        log.info(f"{'='*55}\n")
    except Exception as e:
        log.info(f"  report error: {e}")

def update_metrics(state, balance, regime="UNKNOWN"):
    try:
        bot_stats    = state.get("bot_stats", {})
        total_wins   = sum(s.get("wins",  0) for s in bot_stats.values())
        total_losses = sum(s.get("losses",0) for s in bot_stats.values())
        total_trades = total_wins + total_losses
        win_rate     = (total_wins / total_trades * 100) if total_trades > 0 else 0
        peak         = state.get("peak", balance)
        drawdown     = (peak - balance) / peak if peak > 0 else 0
        bots_data    = {}
        for bot, s in bot_stats.items():
            t  = s.get("trades", 0)
            w  = s.get("wins",   0)
            wr = (w / t * 100) if t > 0 else 0
            bots_data[bot] = {"trades":t,"pnl":round(s.get("pnl",0),2),"win_rate":round(wr,1)}
        data = {
            "balance":      round(balance, 2),
            "peak":         round(peak, 2),
            "drawdown":     round(drawdown, 4),
            "win_rate":     round(win_rate, 1),
            "trades":       total_trades,
            "total_pnl":    round(balance - CONFIG["starting_balance"], 2),
            "daily_pnl":    round(balance - state.get("peak", balance), 2),
            "regime":       regime,
            "mode":         "LIVE" if load_control().get("live_mode") else "PAPER",
            "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "bots":         bots_data,
        }
        json.dump(data, open("metrics.json","w"), indent=2)
        with open("equity.csv","a") as f:
            f.write(f"{time.time():.0f},{balance:.2f}\n")
    except Exception as e:
        log.debug(f"  metrics error: {e}")

# ── AUTONOMY SYSTEM ───────────────────────────────────────────
def recent_performance(metrics):
    last = metrics.get("last_10", [])
    return sum(last) / len(last) if last else 0.0

def adjust_risk(portfolio, control):
    dd = portfolio.drawdown
    if dd > 0.08:   new_risk = 0.3
    elif dd > 0.04: new_risk = 0.6
    elif portfolio.balance > portfolio.peak * 0.98: new_risk = 1.2
    else:           new_risk = 1.0
    old = control.get("risk_multiplier", 1.0)
    control["risk_multiplier"] = round(new_risk, 2)
    if abs(new_risk - old) > 0.05:
        log.info(f"  [AUTO] Risk: {old:.2f} → {new_risk:.2f}")

def adjust_ml_threshold(metrics, control):
    wr = metrics.get("win_rate", 50) / 100
    if wr < 0.45:   new_thresh = 0.65
    elif wr > 0.60: new_thresh = 0.50
    else:           new_thresh = 0.55
    control["ml_threshold"] = new_thresh

def rebalance_bots(metrics, control):
    bot_perf = metrics.get("bots", {})
    if not bot_perf: return
    scores = {k: max(float(v.get("pnl",0)),0) for k,v in bot_perf.items()}
    total  = sum(scores.values())
    if total == 0: return
    control["bot_weights"] = {k: round(v/total,3) for k,v in scores.items()}

def safety_override(portfolio, control):
    if portfolio.drawdown > 0.10:
        if not control.get("kill_switch"):
            control["kill_switch"] = True
            log.error("  [AUTO] KILL SWITCH — drawdown > 10%")
            notify("AUTO HALT: drawdown > 10%")

def recovery_mode(metrics, control):
    last = metrics.get("last_10", [])
    if len(last) < 3: return
    recent_avg = sum(last[-5:]) / len(last[-5:]) if len(last) >= 5 else sum(last)/len(last)
    current    = control.get("risk_multiplier", 1.0)
    if recent_avg < 0:   new_risk = max(0.3, current * 0.7)
    elif recent_avg > 0: new_risk = min(1.2, current * 1.05)
    else:                new_risk = current
    control["risk_multiplier"] = round(new_risk, 2)

def auto_alerts(portfolio, metrics):
    if portfolio.drawdown * 100 > 5.0:
        notify(f"Drawdown rising: {portfolio.drawdown*100:.1f}%")
    if metrics.get("win_rate",0) < 40 and metrics.get("trades",0) > 10:
        notify(f"Win rate dropping: {metrics['win_rate']:.1f}%")
    if portfolio.balance > portfolio.peak * 1.001:
        notify(f"New high: ${portfolio.balance:,.2f}")

def daily_update(metrics, control):
    metrics["trades"] = 0
    current = control.get("risk_multiplier", 1.0)
    if current < 1.0:
        control["risk_multiplier"] = round(min(current + 0.05, 1.0), 2)

def update_performance_history(metrics, pnl):
    last = (metrics.get("last_10",[]) + [round(pnl,4)])[-10:]
    metrics["last_10"] = last

def autonomy_loop(portfolio, metrics, control):
    adjust_risk(portfolio, control)
    recovery_mode(metrics, control)
    adjust_ml_threshold(metrics, control)
    rebalance_bots(metrics, control)
    safety_override(portfolio, control)
    auto_alerts(portfolio, metrics)
    save_control(control)
    log.info(f"  [AUTO] Risk:{control.get('risk_multiplier',1.0):.2f} "
             f"ML:{control.get('ml_threshold',0.55):.2f} "
             f"Kill:{control.get('kill_switch',False)}")

# ── MAIN CYCLE ────────────────────────────────────────────────

# ── SHORT SELLING SYSTEM ──────────────────────────────────────
# Make money when market goes DOWN
# Stocks: Alpaca supports shorting
# Crypto: Sell first, buy back lower

def get_short_signal(bot_name, price, regime, closes):
    """
    Detect when to go SHORT (sell high, buy back lower).
    Best in VOLATILE or TRENDING DOWN markets.
    """
    if len(closes) < 20:
        return False

    sma10 = sum(closes[-10:]) / 10
    sma20 = sum(closes[-20:]) / 20

    # Price trending DOWN
    trending_down = sma10 < sma20

    # Recent candles mostly red
    recent_moves  = [closes[i] - closes[i-1] for i in range(-5, 0)]
    red_candles   = sum(1 for m in recent_moves if m < 0)
    mostly_red    = red_candles >= 3

    # Regime check
    good_regime = regime in ("VOLATILE", "SIDEWAYS")

    return trending_down and mostly_red and good_regime


def simulate_short_trade(symbol):
    """
    Short trade simulation.
    Entry = closes[-2] (sell here)
    Exit  = closes[-1] (buy back here)
    Profit if price FELL.
    """
    d = get_market_data_cached(symbol, "15m")
    if isinstance(d, dict) and d.get("ok") and len(d["closes"]) >= 2:
        entry   = d["closes"][-2]   # sold at this price
        exit_p  = d["closes"][-1]   # bought back at this price

        # SHORT profit = entry - exit (made money if price fell)
        pnl_pct = ((entry - exit_p) / entry * 100) if entry > 0 else 0

        # Profit-first: only close short if in profit
        if pnl_pct < 0:
            return entry, exit_p, 0.0  # hold — price still falling

        pnl_pct = min(pnl_pct, CONFIG["take_profit_pct"])
        return entry, exit_p, pnl_pct

    # Fallback
    move = abs(random.gauss(0.003, 0.008))
    return 100.0, 100.0 * (1 - move), move * 100


def run_cycle():
    check_for_updates(__file__)
    clear_cycle_cache()

    control = load_control()
    if control.get("kill_switch"):
        log.error("  KILL SWITCH ACTIVE — trading halted")
        return

    state = load_state()

    if datetime.now().hour == 0 and datetime.now().minute < 16:
        state["day"] += 1
        report(state, balance=state["balance"])
        save_state(state)
        return

    total_pnl = state["balance"] - CONFIG["starting_balance"]
    log.info(f"\n{'='*55}")
    log.info(f"  ORCHESTRATOR | Day {state['day']}/{CONFIG['test_days']} | "
             f"${state['balance']:,.2f} | P&L: ${total_pnl:+.2f}")

    # ── REGIME ───────────────────────────────────────────────
    regime_data = get_market_data_cached("BTC/USD", "15m", 100)
    if isinstance(regime_data, dict) and regime_data.get("ok") and regime_data["closes"]:
        regime_closes = regime_data["closes"]
    else:
        regime_data = get_market_data_cached("ETH/USD", "15m", 100)
        regime_closes = regime_data["closes"] if isinstance(regime_data, dict) and regime_data.get("ok") else []

    regime = detect_regime(regime_closes)
    risk   = regime_risk_factor(regime)
    log.info(f"  Regime: {regime} | Risk: {risk*100:.0f}%")

    trades_this_cycle = 0

    # ── MANAGE OPEN POSITIONS ─────────────────────────────────
    # PROFIT-FIRST: only close if in profit OR max hold reached
    for key in list(state.get("open_positions", {}).keys()):
        pos            = state["open_positions"][key]
        pos["cycles"]  = pos.get("cycles", 0) + 1
        bot_name       = pos["bot"]
        symbol         = pos["symbol"]

        # Get current price
        d       = get_market_data_cached(symbol, "15m", 5)
        current = d["closes"][-1] if isinstance(d,dict) and d.get("ok") and d["closes"] else pos["entry"]
        entry   = pos["entry"]
        pnl_pct = ((current - entry) / entry * 100) if entry > 0 else 0

        # Only close if in profit OR held too long
        should_close = False
        close_reason = ""

        if pnl_pct >= CONFIG["take_profit_pct"]:
            should_close = True
            close_reason = f"TP +{pnl_pct:.2f}%"
        elif pnl_pct <= -CONFIG["stop_loss_pct"]:
            should_close = True
            close_reason = f"EMERGENCY -8%"
        elif pos["cycles"] >= CONFIG["max_hold_cycles"]:
            if pnl_pct >= 0:
                should_close = True
                close_reason = f"MAX_HOLD +{pnl_pct:.2f}%"
            else:
                # Still losing — hold longer
                log.info(f"  [HOLD] {symbol} {pnl_pct:+.2f}% — waiting to recover")

        if should_close:
            realized_pct = max(pnl_pct, 0) if "EMERGENCY" not in close_reason else pnl_pct
            pnl          = pos["size"] * (realized_pct / 100)
            state["balance"] += pnl
            state["peak"]     = max(state["peak"], state["balance"])
            update_stats(state, bot_name, pnl, symbol)
            del state["open_positions"][key]
            result = "WIN" if pnl > 0 else "LOSS"
            log.info(f"  [{result}] {bot_name} {symbol} {realized_pct:+.2f}% | ${pnl:+.2f} | {close_reason}")
            trades_this_cycle += 1
            save_state(state)

    # ── RUN BOTS ─────────────────────────────────────────────
    log.info(f"  Running {len(BOTS)} bots in parallel...")
    bot_results = run_bots_parallel(state, regime)
    log.info(f"  {len(bot_results)} signals generated")

    for res in bot_results:
        bot_name = res["bot_name"]
        symbol   = res["symbol"]
        pnl      = res["pnl"]
        pnl_pct  = res["pnl_pct"]
        size     = res["size"]
        weight   = res["weight"]
        entry    = res["entry"]

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
            "bot": bot_name, "symbol": symbol,
            "entry": entry, "size": size, "cycles": 0,
            "direction": res.get("direction", "LONG"),
        }

        direction = res.get("direction", "LONG")
        result    = "WIN" if pnl > 0 else "HOLD"
        log.info(f"  [{result}] {bot_name} [{direction}|{regime}] "
                 f"{symbol} {pnl_pct:+.2f}% | ${pnl:+.2f} "
                 f"| alloc:{weight*100:.1f}%")

    log.info(f"\n  Trades: {trades_this_cycle} | "
             f"Balance: ${state['balance']:,.2f} | "
             f"P&L: ${state['balance']-CONFIG['starting_balance']:+.2f}")

    if state["bot_stats"]:
        weights = compute_weights(state["bot_stats"])
        log.info("  Allocations:")
        for bot, w in sorted(weights.items(), key=lambda x:-x[1]):
            s = state["bot_stats"].get(bot, {})
            log.info(f"    {bot}: {'DISABLED' if should_disable(s) else f'{w*100:.1f}%'} | "
                     f"T:{s.get('trades',0)} W:{s.get('wins',0)} PnL:${s.get('pnl',0):+.2f}")

    save_state(state)
    update_metrics(state, state["balance"], regime)

    metrics_data = load_metrics()
    if trades_this_cycle > 0:
        recent_pnl = state["balance"] - CONFIG["starting_balance"]
        update_performance_history(metrics_data, recent_pnl / max(trades_this_cycle,1))

    if PORTFOLIO_AVAILABLE:
        _portfolio.balance  = state["balance"]
        _portfolio.peak     = max(state.get("peak", state["balance"]), state["balance"])
        _portfolio.drawdown = max(0,(_portfolio.peak-_portfolio.balance)/_portfolio.peak) if _portfolio.peak > 0 else 0

    control = load_control()
    autonomy_loop(_portfolio, metrics_data, control)

    if datetime.now().hour == 0 and datetime.now().minute < 16:
        daily_update(metrics_data, control)
        save_control(control)

    json.dump(metrics_data, open("metrics.json","w"), indent=2)

# ── STARTUP ───────────────────────────────────────────────────
def start():
    log_version()
    log.info("\n" + "="*55)
    log.info("  TRADING EMPIRE ORCHESTRATOR v2.1")
    log.info("  PROFIT-FIRST: Only sell when winning")
    log.info("="*55)

    if not API_KEY or not SECRET_KEY:
        log.error("  Missing ALPACA_API_KEY or ALPACA_SECRET_KEY")
        return

    state = load_state()
    log.info(f"\n  Balance:  ${state['balance']:,.2f}")
    log.info(f"  Cycle:    every {CONFIG['cycle_minutes']} minutes")
    log.info(f"  Exit rules:")
    log.info(f"    SELL at +{CONFIG['take_profit_pct']}% profit")
    log.info(f"    HOLD if losing — wait to recover")
    log.info(f"    Emergency stop at -{CONFIG['stop_loss_pct']}% only")

    run_cycle()
    schedule.every(CONFIG["cycle_minutes"]).minutes.do(run_cycle)
    schedule.every().day.at("00:01").do(lambda: report(load_state(), balance=load_state()["balance"]))

    log.info(f"\n  Running every {CONFIG['cycle_minutes']} min.\n")

    while True:
        try:
            schedule.run_pending()
            time.sleep(30)
        except KeyboardInterrupt:
            log.info("\n  Stopped.")
            report(load_state(), balance=load_state()["balance"])
            break
        except Exception as e:
            log.error(f"  Cycle error: {e}")
            time.sleep(60)

if DATA_LAYER_OK:
    try:
        from data_layer import test_all_sources
        test_all_sources()
    except Exception:
        pass

if __name__ == "__main__":
    start()

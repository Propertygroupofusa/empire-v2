"""
TRADOVATE PROP BOT — APEX TRADER FUNDING (Bot #7)
===================================================
Runs ALONGSIDE prop_bot.py (Alpaca), as an independent bot. Trades real
futures (ES/NQ/YM/RTY) via Tradovate against the APEX_589296 evaluation
account. Signal generation reuses the same RSI/trend approach as
prop_bot.py, driven off each contract's correlated ETF via Alpaca's
market data - Tradovate's own market-data API needs a separate
mdAccessToken + websocket setup that isn't wired up here.

SAFETY:
- Inert by default: if TRADOVATE_USER (or TRADOVATE_USERNAME)/
  TRADOVATE_PASS (or TRADOVATE_PASSWORD)/CID/SECRET aren't set, this
  module logs a warning and does nothing.
- STOP_TRADING=true halts new orders (shared kill switch with
  prop_bot.py - flipping it pauses BOTH bots at once).
- TRADOVATE_MODE defaults to "demo". Set to "live" only after
  confirming behavior in demo.
- IntradayTrail enforces APEX's trailing-drawdown rule and stops
  trading at 75% of the max drawdown - a safety margin before the
  actual $1,000 breach line that would fail the evaluation.
"""

import os
import time
import asyncio
import logging
from datetime import datetime, timezone

import aiohttp

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("tradovate_bot")

# ── CREDENTIALS ───────────────────────────────────────────────────
TV_USER    = os.getenv("TRADOVATE_USER", "") or os.getenv("TRADOVATE_USERNAME", "")
TV_PASS    = os.getenv("TRADOVATE_PASS", "") or os.getenv("TRADOVATE_PASSWORD", "")
TV_APP_ID  = os.getenv("TRADOVATE_APP_ID", "Trading Bot Suite")
TV_APP_VER = os.getenv("TRADOVATE_APP_VERSION", "1.0")
TV_CID     = os.getenv("TRADOVATE_CID", "")
TV_SECRET  = os.getenv("TRADOVATE_SECRET", "")
TV_DEVICE  = os.getenv("TRADOVATE_DEVICE_ID", "apex_589296_bot")
IS_DEMO    = os.getenv("TRADOVATE_MODE", "demo").lower() != "live"
TV_BASE_URL = "https://demo.tradovateapi.com/v1" if IS_DEMO else "https://live.tradovateapi.com/v1"

ALPACA_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_DATA_HEADERS = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}

# ── APEX EVALUATION RULES ─────────────────────────────────────────
APEX = {
    "account_size":  float(os.getenv("APEX_ACCOUNT_SIZE", "25000")),
    "profit_target": float(os.getenv("APEX_PROFIT_TARGET", "1500")),
    "max_drawdown":  float(os.getenv("APEX_MAX_DRAWDOWN", "1000")),
    "max_mini":      4,
    "safety_buffer": 0.75,  # internal stop at 75% of max_drawdown, before the real breach
}

# ── SCALING TIERS — contract size grows with profit ───────────────
SCALING = {
    0:   {"minis": 1, "note": "Starting - 1 mini, building profit"},
    300: {"minis": 2, "note": "Up $300 - scaling to 2 contracts"},
    600: {"minis": 3, "note": "Up $600 - scaling to 3 contracts"},
    900: {"minis": 4, "note": "Up $900 - max 4 contracts, push to target"},
}


def get_scale(profit):
    size, note = 1, "Starting"
    for threshold, cfg in sorted(SCALING.items()):
        if profit >= threshold:
            size = min(cfg["minis"], APEX["max_mini"])
            note = cfg["note"]
    return size, note


# ── CONTRACT SPECS ─────────────────────────────────────────────────
# proxy_symbol is only used to generate the RSI/trend signal via Alpaca's
# data feed - orders themselves are placed on the real futures contract,
# looked up by root symbol through Tradovate's own /contract/suggest
# endpoint (rather than hardcoding month-code/roll-date logic, which is
# easy to get wrong and would place orders on the wrong contract).
CONTRACTS = {
    "ES":  {"name": "E-mini S&P 500",    "point_value": 50.00, "proxy_symbol": "SPY"},
    "NQ":  {"name": "E-mini Nasdaq 100", "point_value": 20.00, "proxy_symbol": "QQQ"},
    "YM":  {"name": "Mini-DOW",          "point_value": 5.00,  "proxy_symbol": "DIA"},
    "RTY": {"name": "Russell 2000",      "point_value": 50.00, "proxy_symbol": "IWM"},
}
ACTIVE_CONTRACTS = ["ES", "NQ", "YM", "RTY"]

CONFIG = {
    "cycle_seconds":  180,  # 3 min
    "max_positions":  2,
    "max_trades_day": 20,
}


class IntradayTrail:
    """
    APEX intraday trailing drawdown.

    floor       = session_high - max_drawdown   (hard breach - APEX fails the account)
    safety_stop = session_high - (max_drawdown * safety_buffer)  (this bot's own
                  internal stop, well before the hard breach, so a bad tick or
                  slippage can't blow through the real limit)

    session_high resets every day at market open.
    """

    def __init__(self):
        self.session_high = APEX["account_size"]
        self.session_date = None

    def reset_if_new_day(self, now: datetime):
        today = now.strftime("%Y-%m-%d")
        if self.session_date != today:
            self.session_date = today
            self.session_high = APEX["account_size"]
            log.info(f"[APEX_589296] New session - drawdown trail reset. High-water mark: ${self.session_high:,.2f}")

    def update(self, current_equity: float) -> bool:
        """Update the high-water mark. Returns True if trading should stop."""
        if current_equity > self.session_high:
            self.session_high = current_equity
            log.info(f"[APEX_589296] New session high: ${self.session_high:,.2f}")

        floor = self.session_high - APEX["max_drawdown"]
        safety_stop = self.session_high - (APEX["max_drawdown"] * APEX["safety_buffer"])

        if current_equity <= safety_stop:
            log.error(
                f"[APEX_589296] SAFETY STOP - equity ${current_equity:,.2f} at/below "
                f"{APEX['safety_buffer']*100:.0f}% of the drawdown limit "
                f"(safety line ${safety_stop:,.2f}, hard breach floor ${floor:,.2f}). "
                f"Halting new trades for the session."
            )
            return True
        return False


class TradovateClient:
    """Minimal Tradovate REST client: auth, account state, orders."""

    def __init__(self):
        self.access_token = None
        self.account_id = None
        self.account_spec = None

    def _headers(self):
        return {"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json"}

    async def authenticate(self, session: aiohttp.ClientSession) -> bool:
        body = {
            "name": TV_USER,
            "password": TV_PASS,
            "appId": TV_APP_ID,
            "appVersion": TV_APP_VER,
            "cid": TV_CID,
            "sec": TV_SECRET,
            "deviceId": TV_DEVICE,
        }
        try:
            async with session.post(f"{TV_BASE_URL}/auth/accesstokenrequest", json=body) as r:
                data = await r.json()
                if r.status != 200 or "accessToken" not in data:
                    log.error(f"Tradovate auth failed: {data}")
                    return False
                self.access_token = data["accessToken"]
                log.info(f"Tradovate authenticated ({'DEMO' if IS_DEMO else 'LIVE'})")
                return await self._load_account(session)
        except Exception as e:
            log.error(f"Tradovate auth error: {e}")
            return False

    async def _load_account(self, session: aiohttp.ClientSession) -> bool:
        try:
            async with session.get(f"{TV_BASE_URL}/account/list", headers=self._headers()) as r:
                accounts = await r.json()
                if not accounts:
                    log.error("No Tradovate accounts found for this login")
                    return False
                acct = accounts[0]
                self.account_id = acct["id"]
                self.account_spec = acct["name"]
                log.info(f"Tradovate account: {self.account_spec} (id={self.account_id})")
                return True
        except Exception as e:
            log.error(f"Failed to load Tradovate account: {e}")
            return False

    async def get_equity(self, session: aiohttp.ClientSession) -> float:
        try:
            body = {"accountId": self.account_id}
            async with session.post(f"{TV_BASE_URL}/cashBalance/getcashbalancesnapshot", headers=self._headers(), json=body) as r:
                data = await r.json()
                return float(data.get("netLiqValue") or data.get("cashBalance") or APEX["account_size"])
        except Exception as e:
            log.error(f"Failed to fetch Tradovate equity: {e}")
            return APEX["account_size"]

    async def get_active_contract(self, session: aiohttp.ClientSession, root: str):
        """Look up the current front-month contract for a root symbol (e.g. 'ES')
        via Tradovate's own suggest endpoint - safer than hardcoding month codes
        and roll dates, which vary and are easy to get wrong."""
        try:
            async with session.get(f"{TV_BASE_URL}/contract/suggest", headers=self._headers(), params={"t": root, "l": 1}) as r:
                results = await r.json()
                if results:
                    return results[0]["name"]
        except Exception as e:
            log.error(f"Contract lookup failed for {root}: {e}")
        return None

    async def place_order(self, session: aiohttp.ClientSession, symbol: str, action: str, qty: int) -> bool:
        body = {
            "accountSpec": self.account_spec,
            "accountId": self.account_id,
            "action": action,  # "Buy" or "Sell"
            "symbol": symbol,
            "orderQty": qty,
            "orderType": "Market",
            "isAutomated": True,
        }
        try:
            async with session.post(f"{TV_BASE_URL}/order/placeorder", headers=self._headers(), json=body) as r:
                result = await r.json()
                if r.status in (200, 201) and result.get("orderId"):
                    log.info(f"TRADOVATE ORDER | {action} {qty} {symbol} | orderId={result['orderId']}")
                    return True
                log.error(f"Tradovate order failed: {result}")
                return False
        except Exception as e:
            log.error(f"Order placement error: {e}")
            return False


trail = IntradayTrail()
client = TradovateClient()
daily_pnl = 0.0
trades_today = 0
open_positions = {}  # root -> {"entry": float, "qty": int, "symbol": str}


async def get_signal(session: aiohttp.ClientSession, proxy_symbol: str):
    """Same RSI/SMA-trend approach as prop_bot.py, reusing Alpaca's data feed."""
    try:
        url = f"https://data.alpaca.markets/v2/stocks/{proxy_symbol}/bars?timeframe=5Min&limit=20"
        async with session.get(url, headers=ALPACA_DATA_HEADERS) as r:
            if r.status != 200:
                return None
            data = await r.json()
            bars = data.get("bars", [])
            if len(bars) < 14:
                return None

            closes = [b["c"] for b in bars]
            price = closes[-1]

            gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
            losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
            avg_gain = sum(gains[-14:]) / 14
            avg_loss = sum(losses[-14:]) / 14
            rs = avg_gain / avg_loss if avg_loss > 0 else 100
            rsi = 100 - (100 / (1 + rs))

            sma5 = sum(closes[-5:]) / 5
            sma10 = sum(closes[-10:]) / 10
            trend = "bullish" if sma5 > sma10 else "bearish"

            return {"price": price, "rsi": round(rsi, 1), "trend": trend}
    except Exception as e:
        log.error(f"Signal error {proxy_symbol}: {e}")
        return None


async def run_cycle():
    global daily_pnl, trades_today

    now = datetime.now(timezone.utc)
    trail.reset_if_new_day(now)

    # Market hours 9:30am-4pm ET = 14:30-21:00 UTC
    market_open = now.replace(hour=14, minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=21, minute=0, second=0, microsecond=0)
    if not (market_open <= now <= market_close):
        log.info("[APEX_589296/Tradovate] Market closed - waiting for 9:30am ET")
        return

    stop_now = os.getenv("STOP_TRADING", "false").lower() == "true"

    async with aiohttp.ClientSession() as session:
        if not client.access_token:
            if not await client.authenticate(session):
                return

        equity = await client.get_equity(session)
        should_halt = trail.update(equity)

        if daily_pnl >= APEX["profit_target"]:
            log.info(f"[APEX_589296] PROFIT TARGET HIT (${daily_pnl:,.2f}) - stopping for the day")
            return

        if should_halt or stop_now:
            log.warning("[APEX_589296/Tradovate] Trading halted (safety stop or STOP_TRADING=true)")
            return

        if trades_today >= CONFIG["max_trades_day"]:
            log.info(f"[APEX_589296/Tradovate] Max trades/day reached ({trades_today})")
            return

        contract_size, scale_note = get_scale(daily_pnl)

        for root in ACTIVE_CONTRACTS:
            if len(open_positions) >= CONFIG["max_positions"]:
                break

            spec = CONTRACTS[root]
            signal = await get_signal(session, spec["proxy_symbol"])
            if not signal:
                continue

            price, rsi, trend = signal["price"], signal["rsi"], signal["trend"]
            log.info(f"[APEX_589296] {root} ({spec['proxy_symbol']} proxy) | ${price:.2f} | RSI:{rsi} | {trend} | scale:{contract_size} ({scale_note})")

            has_position = root in open_positions

            if not has_position and rsi < 38 and trend == "bullish":
                contract_symbol = await client.get_active_contract(session, root)
                if not contract_symbol:
                    continue
                if await client.place_order(session, contract_symbol, "Buy", contract_size):
                    open_positions[root] = {"entry": price, "qty": contract_size, "symbol": contract_symbol}
                    trades_today += 1

            elif has_position and (rsi > 62 or (trend == "bearish" and rsi > 50)):
                pos = open_positions[root]
                if await client.place_order(session, pos["symbol"], "Sell", pos["qty"]):
                    pnl = (price - pos["entry"]) * pos["qty"] * spec["point_value"]
                    daily_pnl += pnl
                    log.info(f"[APEX_589296] CLOSE {root} | Entry: ${pos['entry']:.2f} Exit: ${price:.2f} | P&L: ${pnl:.2f}")
                    open_positions.pop(root, None)
                    trades_today += 1

            await asyncio.sleep(0.5)


def run():
    if not (TV_USER and TV_PASS and TV_CID and TV_SECRET):
        log.warning("Tradovate credentials not configured (TRADOVATE_USER/PASS/CID/SECRET) - tradovate_bot will not start")
        return

    log.info("=" * 60)
    log.info("TRADOVATE PROP BOT - APEX_589296 (Bot #7)")
    log.info(f"Mode: {'DEMO' if IS_DEMO else 'LIVE'} | Profit target: ${APEX['profit_target']} | Max drawdown: ${APEX['max_drawdown']}")
    log.info("=" * 60)

    while True:
        if os.getenv("STOP_TRADING", "false").lower() == "true":
            log.warning("STOP_TRADING=true - tradovate_bot paused")
            time.sleep(60)
            continue
        try:
            asyncio.run(run_cycle())
        except Exception as e:
            log.error(f"Tradovate cycle error: {e}")
        time.sleep(CONFIG["cycle_seconds"])


if __name__ == "__main__":
    run()

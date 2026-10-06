"""Alpaca wheel strategy - REAL MONEY, off by default.

Chosen by the account owner 2026-10-06 ("2 run on real"). The wheel:

  Stage 1  sell a cash-secured put ~10% below the price, 2-4 weeks out.
  Stage 2  if assigned, hold the 100 shares and sell a covered call
           ~10% above what was paid - never below cost basis.
  Stage 3  if the shares are called away, go back to stage 1.

Close any contract early once it has kept 50% of its premium. Check every
15 minutes, market hours only. A daily summary is logged at the close.

THE GATES - every one must pass or no put is sold

  1. Master switch on (DB flag, dashboard, default OFF).
  2. STOP_TRADING not set; Alpaca passive mode off.
  3. Market open, read from Alpaca's own /v2/clock - never inferred.
  4. A FRESH /v2/account read this cycle. Unknown balance = no order.
  5. Alpaca reports options approval (options_trading_level >= 1) and no
     trading block / user suspension.
  6. CASH COLLATERAL: strike x 100 x contracts must be covered by BOTH
     cash (minus collateral this engine already has tied up) AND
     Alpaca's own options_buying_power. This is the owner's rule:
     "never sell a put unless I have enough cash to buy the shares".
  7. RISK CAP: open position notional + wheel collateral + the new
     collateral must stay within prop_bot's MAX_RISK_PERCENT of equity.
     The owner's recommended-default answer: locked put collateral counts
     against the same cap the stock bots use, and prop_bot sees it too
     (see prop_bot._wheel_reserved_collateral) so no stock bot can spend
     cash that is promising to buy 100 shares.
  8. The underlying must be on the owner's approved list. Nothing is
     picked automatically - a cheap price is not a reason to trade it.
  9. The contract must be worth selling: a real two-sided quote, spread
     within MAX_SPREAD_PCT, open interest >= MIN_OPEN_INTEREST, and a
     premium of at least MIN_PREMIUM_YIELD of the collateral.

Covered calls have their own hard rule: strike >= cost basis, always. If
no listed strike satisfies it, no call is sold and the shares are held.

Every order is a DAY limit order at the mid. Nothing here ever buys
shares or opens a long option - the only buys are buy-to-close.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from datetime import date, datetime, timedelta, timezone

import aiohttp
from sqlalchemy import select

from database import AsyncSessionLocal
from models import AlpacaWheelState, TradingBotState

log = logging.getLogger("alpaca_wheel_bot")

WHEEL_MODE_KEY = "alpaca_wheel_live_mode"

PUT_STRIKE_BELOW_PCT = 0.10
CALL_STRIKE_ABOVE_PCT = 0.10
MIN_DTE = 14
MAX_DTE = 28
TAKE_PROFIT_FRACTION = 0.50       # close once 50% of the premium is kept
MAX_SPREAD_PCT = 0.25             # (ask-bid)/mid
MIN_OPEN_INTEREST = 100
MIN_PREMIUM_YIELD = 0.005         # premium >= 0.5% of the collateral
CONTRACTS_PER_CYCLE = 1
# First live deployment is a single-contract validation: one wheel open at a
# time across ALL tickers, until a real fill has proven the whole chain
# (balance -> reservation -> risk check -> order -> fill -> ledger ->
# assignment/exit). Raise only after that, deliberately.
MAX_ACTIVE_WHEELS = 1
CHECK_INTERVAL_SECONDS = 15 * 60
DEFAULT_MAX_RISK_PERCENT = 0.50

STAGE_IDLE, STAGE_PUT, STAGE_SHARES, STAGE_CALL = "IDLE", "PUT", "SHARES", "CALL"
FINAL_DEAD = {"canceled", "expired", "rejected", "done_for_day", "suspended"}

# Collateral this engine currently has tied up in open/pending short puts.
# prop_bot reads it so the stock bots count it against their risk cap.
reserved_collateral_usd = 0.0

# Last status, for the dashboard.
last_status = {"state": "not started", "blockers": [], "checked_at": None,
               "summary": None}
_last_summary_date = None


# ── pure decision functions (tested without network) ─────────────────────

def option_mid(bid, ask):
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return None
    return (bid + ask) / 2.0


def round_option_price(price):
    """Alpaca option tick: $0.01 under $3, $0.05 at or above."""
    if price < 3:
        return round(round(price * 100) / 100, 2)
    return round(round(price * 20) / 20, 2)


def put_collateral(strike, contracts=CONTRACTS_PER_CYCLE):
    return strike * 100.0 * contracts


def collateral_gate(strike, contracts, cash, options_bp, already_reserved):
    """The owner's rule. Returns (ok, reason)."""
    need = put_collateral(strike, contracts)
    if cash is None or options_bp is None:
        return False, "account balance unknown - no order"
    free_cash = cash - already_reserved
    if free_cash < need:
        return False, (f"cash ${free_cash:,.2f} (after ${already_reserved:,.2f} already "
                       f"reserved) < ${need:,.2f} needed to buy {100*contracts} shares at ${strike:g}")
    if options_bp < need:
        return False, f"Alpaca options buying power ${options_bp:,.2f} < ${need:,.2f}"
    return True, "OK"


def risk_cap_gate(equity, open_notional, wheel_reserved, new_collateral, max_pct):
    if equity is None or equity <= 0:
        return False, "equity unknown - no order"
    total = open_notional + wheel_reserved + new_collateral
    limit = equity * max_pct
    if total > limit:
        return False, (f"risk cap: ${open_notional:,.2f} open + ${wheel_reserved:,.2f} wheel "
                       f"+ ${new_collateral:,.2f} new = ${total:,.2f} > {max_pct*100:.0f}% "
                       f"of ${equity:,.2f} equity (${limit:,.2f})")
    return True, "OK"


def _quality_reason(c, collateral_per_contract):
    mid = option_mid(c.get("bid"), c.get("ask"))
    if mid is None:
        return None, "no two-sided quote"
    if (c["ask"] - c["bid"]) / mid > MAX_SPREAD_PCT:
        return None, f"spread {(c['ask']-c['bid'])/mid*100:.0f}% too wide"
    if (c.get("open_interest") or 0) < MIN_OPEN_INTEREST:
        return None, f"open interest {c.get('open_interest') or 0} < {MIN_OPEN_INTEREST}"
    if collateral_per_contract > 0 and mid * 100 / collateral_per_contract < MIN_PREMIUM_YIELD:
        return None, "premium too small for the collateral"
    return mid, None


def _dte(expiration, today):
    return (date.fromisoformat(expiration) - today).days


def pick_put(contracts, price, today):
    """Best short put: 14-28 DTE, strike at or below 90% of price, the one
    nearest that target that passes the quality checks. Returns
    (contract, mid, reason_if_none)."""
    target = price * (1 - PUT_STRIKE_BELOW_PCT)
    eligible = [c for c in contracts
                if c.get("type") == "put"
                and MIN_DTE <= _dte(c["expiration"], today) <= MAX_DTE
                and c["strike"] <= target]
    if not eligible:
        return None, None, f"no put 14-28 days out at or below ${target:.2f}"
    eligible.sort(key=lambda c: (target - c["strike"], _dte(c["expiration"], today)))
    last_reason = None
    for c in eligible:
        mid, reason = _quality_reason(c, c["strike"] * 100)
        if mid is not None:
            return c, mid, None
        last_reason = f"{c['symbol']}: {reason}"
    return None, None, last_reason


def pick_call(contracts, price, cost_basis, today):
    """Covered call: strike >= max(cost basis, price + 10%). The cost-basis
    floor is absolute - never a call below what the shares cost."""
    if cost_basis is None or cost_basis <= 0:
        return None, None, "cost basis unknown - no call"
    floor = max(cost_basis, price * (1 + CALL_STRIKE_ABOVE_PCT))
    eligible = [c for c in contracts
                if c.get("type") == "call"
                and MIN_DTE <= _dte(c["expiration"], today) <= MAX_DTE
                and c["strike"] >= floor and c["strike"] >= cost_basis]
    if not eligible:
        return None, None, f"no call 14-28 days out at or above ${floor:.2f}"
    eligible.sort(key=lambda c: (c["strike"] - floor, _dte(c["expiration"], today)))
    last_reason = None
    for c in eligible:
        mid, reason = _quality_reason(c, cost_basis * 100)
        if mid is not None:
            return c, mid, None
        last_reason = f"{c['symbol']}: {reason}"
    return None, None, last_reason


def take_profit_due(open_credit, contracts, ask):
    """True once buying back costs <= 50% of the credit received."""
    if not open_credit or ask is None or ask <= 0 or contracts <= 0:
        return False
    return ask * 100 * contracts <= open_credit * TAKE_PROFIT_FRACTION


def assigned_cost_basis(strike, open_credit, contracts):
    return strike - open_credit / (100.0 * contracts)


# ── Alpaca IO ────────────────────────────────────────────────────────────

def _headers():
    return {"APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", ""),
            "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY", ""),
            "Content-Type": "application/json"}


def _trading_url():
    return os.getenv("ALPACA_BASE_URL", "https://api.alpaca.markets").rstrip("/")


def _data_url():
    return os.getenv("ALPACA_DATA_URL", "https://data.alpaca.markets").rstrip("/")


async def _get(session, url, params=None):
    async with session.get(url, headers=_headers(), params=params,
                           timeout=aiohttp.ClientTimeout(total=20)) as r:
        if r.status >= 400:
            raise RuntimeError(f"GET {url} -> {r.status}: {(await r.text())[:300]}")
        return await r.json()


async def _post(session, url, payload):
    async with session.post(url, headers=_headers(), json=payload,
                            timeout=aiohttp.ClientTimeout(total=20)) as r:
        text = await r.text()
        if r.status >= 400:
            raise RuntimeError(f"POST {url} -> {r.status}: {text[:300]}")
        return await r.json(content_type=None)


async def fetch_contracts(session, underlying, opt_type, today, strike_lte=None, strike_gte=None):
    params = {"underlying_symbols": underlying, "type": opt_type, "status": "active",
              "expiration_date_gte": (today + timedelta(days=MIN_DTE)).isoformat(),
              "expiration_date_lte": (today + timedelta(days=MAX_DTE)).isoformat(),
              "limit": 500}
    if strike_lte is not None:
        params["strike_price_lte"] = f"{strike_lte:.2f}"
    if strike_gte is not None:
        params["strike_price_gte"] = f"{strike_gte:.2f}"
    data = await _get(session, f"{_trading_url()}/v2/options/contracts", params)
    out = []
    for c in data.get("option_contracts") or []:
        try:
            out.append({"symbol": c["symbol"], "type": c.get("type"),
                        "strike": float(c["strike_price"]),
                        "expiration": c["expiration_date"],
                        "open_interest": int(float(c.get("open_interest") or 0))})
        except (KeyError, TypeError, ValueError):
            continue
    if out:
        quotes = await fetch_option_quotes(session, [c["symbol"] for c in out])
        for c in out:
            q = quotes.get(c["symbol"], {})
            c["bid"], c["ask"] = q.get("bid"), q.get("ask")
    return out


async def fetch_option_quotes(session, symbols):
    quotes = {}
    for i in range(0, len(symbols), 100):
        chunk = symbols[i:i + 100]
        data = await _get(session, f"{_data_url()}/v1beta1/options/snapshots",
                          {"symbols": ",".join(chunk), "feed": "indicative"})
        for sym, snap in (data.get("snapshots") or {}).items():
            q = (snap or {}).get("latestQuote") or {}
            quotes[sym] = {"bid": float(q.get("bp") or 0) or None,
                           "ask": float(q.get("ap") or 0) or None}
    return quotes


async def fetch_stock_price(session, symbol):
    data = await _get(session, f"{_data_url()}/v2/stocks/{symbol}/trades/latest",
                      {"feed": "iex"})
    p = float((data.get("trade") or {}).get("p") or 0)
    return p or None


async def place_option_order(session, symbol, qty, side, intent, limit_price, tag):
    payload = {"symbol": symbol, "qty": str(qty), "side": side, "type": "limit",
               "time_in_force": "day", "limit_price": f"{limit_price:.2f}",
               "position_intent": intent,
               "client_order_id": f"wheel-{tag}-{int(time.time())}"}
    order = await _post(session, f"{_trading_url()}/v2/orders", payload)
    if not order.get("id"):
        raise RuntimeError(f"order response had no id: {order}")
    log.warning(f"[WHEEL] REAL ORDER {side} {qty} {symbol} @ ${limit_price:.2f} ({intent}) -> {order['id']}")
    return order["id"]


# ── DB ───────────────────────────────────────────────────────────────────

async def is_wheel_active() -> bool:
    try:
        async with AsyncSessionLocal() as db:
            row = (await db.execute(select(TradingBotState).where(
                TradingBotState.bot_name == WHEEL_MODE_KEY))).scalar_one_or_none()
            return bool(row and row.base_capital and row.base_capital >= 1.0)
    except Exception as e:
        log.warning(f"[WHEEL] could not read mode flag ({e}) - staying off")
        return False


async def set_wheel_active(enabled: bool):
    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(TradingBotState).where(
            TradingBotState.bot_name == WHEEL_MODE_KEY))).scalar_one_or_none()
        if row is None:
            row = TradingBotState(bot_name=WHEEL_MODE_KEY, base_capital=0.0)
            db.add(row)
        row.base_capital = 1.0 if enabled else 0.0
        await db.commit()
    log.warning(f"[WHEEL] master switch -> {'ON (real orders)' if enabled else 'OFF'}")


async def list_states():
    async with AsyncSessionLocal() as db:
        return list((await db.execute(select(AlpacaWheelState))).scalars().all())


async def set_approved(underlying: str, approved: bool):
    underlying = underlying.strip().upper()
    if not underlying.isalpha() or len(underlying) > 6:
        raise ValueError(f"{underlying!r} is not a stock ticker")
    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(AlpacaWheelState).where(
            AlpacaWheelState.underlying == underlying))).scalar_one_or_none()
        if row is None:
            # A rejection is recorded too, so the candidate scan stops
            # proposing the ticker. approved=False never trades.
            row = AlpacaWheelState(underlying=underlying, approved=approved, stage=STAGE_IDLE,
                                   premium_total=0.0, cycles_completed=0, shares=0.0, contracts=0)
            db.add(row)
        else:
            row.approved = approved
        await db.commit()
        return row.to_dict()


def _reserved_from_states(states):
    return sum(put_collateral(s.option_strike, s.contracts)
               for s in states
               if s.stage == STAGE_PUT and s.option_strike and s.contracts)


def _active_wheel_count(states):
    return sum(1 for s in states if s.stage != STAGE_IDLE or s.open_order_id)


# ── candidate discovery ──────────────────────────────────────────────────
#
# The owner should not have to guess a ticker. This scans the whole listed
# put market for contracts the account could actually secure with cash,
# and grades each underlying on the same rules the live engine trades by.
# Read-only: it never places an order. A pass here is a PROPOSAL - nothing
# trades until the owner presses Approve.

SCAN_MAX_UNDERLYINGS = 60


def collateral_capacity(cash, options_bp, equity, open_notional, reserved, max_pct):
    """The most collateral a new put could use right now, and why."""
    if None in (cash, options_bp, equity) or equity <= 0:
        return {"max_collateral": 0.0, "max_strike": 0.0, "binding": "balance unknown"}
    cash_room = cash - reserved
    risk_room = equity * max_pct - open_notional - reserved
    limits = {"cash": cash_room, "options buying power": options_bp, "risk cap": risk_room}
    binding = min(limits, key=limits.get)
    cap = max(0.0, limits[binding])
    return {"max_collateral": round(cap, 2), "max_strike": round(cap / 100.0, 2),
            "binding": binding, "cash_room": round(cash_room, 2),
            "options_buying_power": round(options_bp, 2), "risk_room": round(risk_room, 2)}


def evaluate_candidate(underlying, price, contracts, today, max_strike):
    """Grade one underlying. Returns the numbers the owner needs to decide,
    with pass/fail and the specific reason."""
    out = {"underlying": underlying, "price": price, "pass": False}
    if not price:
        out["reason"] = "no live price"
        return out
    c, mid, why = pick_put(contracts, price, today)
    if c is None:
        out["reason"] = why
        return out
    dte = _dte(c["expiration"], today)
    collateral = put_collateral(c["strike"])
    premium = round_option_price(mid) * 100
    breakeven = c["strike"] - premium / 100
    out.update({
        "contract": c["symbol"], "strike": c["strike"], "expiration": c["expiration"],
        "dte": dte, "bid": c["bid"], "ask": c["ask"],
        "spread_pct": round((c["ask"] - c["bid"]) / mid * 100, 1),
        "open_interest": c["open_interest"],
        "premium_usd": round(premium, 2), "collateral_usd": round(collateral, 2),
        "return_pct": round(premium / collateral * 100, 2),
        "annualized_pct": round(premium / collateral * 365 / max(dte, 1) * 100, 1),
        "breakeven": round(breakeven, 2),
        "cushion_pct": round((price - breakeven) / price * 100, 1),
    })
    if c["strike"] > max_strike:
        out["reason"] = (f"needs ${collateral:,.2f} collateral - account can secure "
                         f"${max_strike*100:,.2f} right now")
        return out
    out["pass"] = True
    out["reason"] = "qualifies - approve to let the wheel sell this put"
    return out


async def _scan_put_universe(session, today, max_strike):
    """Every active put 14-28 DTE at or below max_strike, any underlying."""
    params = {"type": "put", "status": "active", "limit": 10000,
              "expiration_date_gte": (today + timedelta(days=MIN_DTE)).isoformat(),
              "expiration_date_lte": (today + timedelta(days=MAX_DTE)).isoformat(),
              "strike_price_lte": f"{max(max_strike, 0.5):.2f}"}
    out, pages = [], 0
    while pages < 5:
        data = await _get(session, f"{_trading_url()}/v2/options/contracts", params)
        out.extend(data.get("option_contracts") or [])
        token = data.get("next_page_token")
        pages += 1
        if not token:
            break
        params["page_token"] = token
    return out


async def fetch_stock_prices(session, symbols):
    prices = {}
    for i in range(0, len(symbols), 100):
        data = await _get(session, f"{_data_url()}/v2/stocks/trades/latest",
                          {"symbols": ",".join(symbols[i:i + 100]), "feed": "iex"})
        for sym, t in (data.get("trades") or {}).items():
            p = float((t or {}).get("p") or 0)
            if p:
                prices[sym] = p
    return prices


async def scan_candidates():
    """Fresh account read, capacity, then the best-qualified underlyings."""
    states = await list_states()
    decided = {s.underlying: ("approved" if s.approved else "rejected") for s in states}
    reserved = _reserved_from_states(states)
    result = {"checked_at": datetime.now(timezone.utc).isoformat(), "candidates": [],
              "capacity": None, "blockers": []}
    today = datetime.now(timezone.utc).date()
    async with aiohttp.ClientSession() as session:
        try:
            account = await _get(session, f"{_trading_url()}/v2/account")
            positions = await _get(session, f"{_trading_url()}/v2/positions")
            cash = float(account.get("cash"))
            options_bp = float(account.get("options_buying_power"))
            equity = float(account.get("equity"))
        except Exception as e:
            result["blockers"].append(f"fresh account read failed ({e}) - no candidates")
            return result
        level = int(float(account.get("options_trading_level") or 0))
        if level < 1:
            result["blockers"].append(f"Alpaca options approval level {level} - "
                                      f"cash-secured puts need level 1+")
        open_notional = sum(abs(float(p.get("market_value") or 0)) for p in positions or []
                            if p.get("asset_class") != "us_option")
        cap = collateral_capacity(cash, options_bp, equity, open_notional, reserved, _max_risk_pct())
        cap["open_stock_notional"] = round(open_notional, 2)
        cap["reserved_by_wheel"] = round(reserved, 2)
        cap["options_level"] = level
        result["capacity"] = cap

        # Look a little beyond what fits today, so the owner can see what
        # becomes reachable - those are graded FAIL with the dollar gap.
        look_strike = max(cap["max_strike"], 5.0)
        try:
            raw = await _scan_put_universe(session, today, look_strike)
        except Exception as e:
            result["blockers"].append(f"options chain unreadable ({e})")
            return result
        by_und = {}
        for c in raw:
            try:
                by_und.setdefault(c["underlying_symbol"], []).append({
                    "symbol": c["symbol"], "type": "put", "strike": float(c["strike_price"]),
                    "expiration": c["expiration_date"],
                    "open_interest": int(float(c.get("open_interest") or 0))})
            except (KeyError, TypeError, ValueError):
                continue
        # Most open interest first - liquidity is the first filter that matters.
        ranked = sorted(by_und, key=lambda u: -sum(c["open_interest"] for c in by_und[u]))
        ranked = [u for u in ranked if decided.get(u) != "rejected"][:SCAN_MAX_UNDERLYINGS]
        prices = await fetch_stock_prices(session, ranked) if ranked else {}
        graded = []
        for u in ranked:
            price = prices.get(u)
            target = (price or 0) * (1 - PUT_STRIKE_BELOW_PCT)
            near = sorted((c for c in by_und[u] if c["strike"] <= target),
                          key=lambda c: c["strike"])[-40:]
            if price and near:
                quotes = await fetch_option_quotes(session, [c["symbol"] for c in near])
                for c in near:
                    q = quotes.get(c["symbol"], {})
                    c["bid"], c["ask"] = q.get("bid"), q.get("ask")
            g = evaluate_candidate(u, price, near, today, cap["max_strike"])
            g["status"] = decided.get(u, "new")
            graded.append(g)
    graded.sort(key=lambda g: (not g["pass"], "contract" not in g, -(g.get("annualized_pct") or 0)))
    result["candidates"] = graded
    return result


# ── one cycle ────────────────────────────────────────────────────────────

def _max_risk_pct():
    try:
        import prop_bot
        return float(prop_bot.MAX_RISK_PERCENT)
    except Exception:
        return DEFAULT_MAX_RISK_PERCENT


async def _advance_state(session, st, positions_by_symbol, today, notes):
    """Reconcile one underlying's state with the broker. Returns True if
    the row changed. Never opens a new contract."""
    changed = False
    shares_held = float((positions_by_symbol.get(st.underlying) or {}).get("qty") or 0)

    # Pending close order?
    if st.close_order_id:
        o = await _get(session, f"{_trading_url()}/v2/orders/{st.close_order_id}")
        if o.get("status") == "filled":
            debit = float(o.get("filled_avg_price") or 0) * 100 * st.contracts
            st.premium_total = (st.premium_total or 0) + (st.open_credit or 0) - debit
            notes.append(f"{st.underlying}: closed {st.option_symbol} early, kept "
                         f"${(st.open_credit or 0) - debit:,.2f}")
            was_call = st.stage == STAGE_CALL
            st.option_symbol = st.option_strike = st.option_expiration = None
            st.open_credit = None
            st.close_order_id = None
            st.stage = STAGE_SHARES if was_call else STAGE_IDLE
            if not was_call:
                st.contracts = 0
            return True
        if o.get("status") in FINAL_DEAD:
            st.close_order_id = None
            changed = True
        else:
            return changed  # still working

    # Pending open order?
    if st.open_order_id:
        o = await _get(session, f"{_trading_url()}/v2/orders/{st.open_order_id}")
        status = o.get("status")
        if status == "filled":
            st.open_credit = float(o.get("filled_avg_price") or 0) * 100 * st.contracts
            st.open_order_id = None
            notes.append(f"{st.underlying}: {st.option_symbol} filled, credit ${st.open_credit:,.2f}")
            return True
        if status in FINAL_DEAD:
            notes.append(f"{st.underlying}: open order {status} - nothing sold")
            st.open_order_id = None
            st.option_symbol = st.option_strike = st.option_expiration = None
            if st.stage == STAGE_PUT:
                st.stage, st.contracts = STAGE_IDLE, 0
            else:
                st.stage = STAGE_SHARES
            return True
        return False  # still working

    if st.stage == STAGE_PUT and st.option_symbol:
        if st.option_symbol not in positions_by_symbol:
            if shares_held >= 100 * st.contracts:
                st.cost_basis = assigned_cost_basis(st.option_strike, st.open_credit or 0, st.contracts)
                st.shares = shares_held
                st.premium_total = (st.premium_total or 0) + (st.open_credit or 0)
                notes.append(f"{st.underlying}: ASSIGNED {100*st.contracts} shares at "
                             f"${st.option_strike:g}, cost basis ${st.cost_basis:.2f}")
                st.stage = STAGE_SHARES
            else:
                st.premium_total = (st.premium_total or 0) + (st.open_credit or 0)
                notes.append(f"{st.underlying}: put expired worthless, kept ${st.open_credit or 0:,.2f}")
                st.stage, st.contracts = STAGE_IDLE, 0
            st.option_symbol = st.option_strike = st.option_expiration = None
            st.open_credit = None
            return True

    if st.stage == STAGE_CALL and st.option_symbol:
        if st.option_symbol not in positions_by_symbol:
            st.premium_total = (st.premium_total or 0) + (st.open_credit or 0)
            if shares_held < 100 * st.contracts:
                notes.append(f"{st.underlying}: shares CALLED AWAY at ${st.option_strike:g} - cycle complete")
                st.stage, st.contracts, st.shares, st.cost_basis = STAGE_IDLE, 0, 0.0, None
                st.cycles_completed = (st.cycles_completed or 0) + 1
            else:
                notes.append(f"{st.underlying}: call expired, still holding the shares")
                st.stage = STAGE_SHARES
            st.option_symbol = st.option_strike = st.option_expiration = None
            st.open_credit = None
            return True

    if st.stage == STAGE_SHARES and shares_held < 100 * max(st.contracts, 1):
        notes.append(f"{st.underlying}: shares no longer held ({shares_held:g}) - "
                     f"sold outside the wheel; back to idle")
        st.stage, st.contracts, st.shares, st.cost_basis = STAGE_IDLE, 0, 0.0, None
        return True
    return changed


async def _maybe_take_profit(session, st, notes):
    if st.stage not in (STAGE_PUT, STAGE_CALL) or not st.option_symbol \
            or st.open_order_id or st.close_order_id or not st.open_credit:
        return False
    q = (await fetch_option_quotes(session, [st.option_symbol])).get(st.option_symbol, {})
    ask = q.get("ask")
    if not take_profit_due(st.open_credit, st.contracts, ask):
        return False
    st.close_order_id = await place_option_order(
        session, st.option_symbol, st.contracts, "buy", "buy_to_close",
        round_option_price(ask), f"tp-{st.underlying}")
    notes.append(f"{st.underlying}: 50% profit reached, buying back {st.option_symbol} at ${ask:.2f}")
    return True


async def run_wheel_cycle():
    """One check. Safe to call any time; does nothing unless every gate passes."""
    global reserved_collateral_usd
    blockers, notes = [], []
    status = {"checked_at": datetime.now(timezone.utc).isoformat(), "blockers": blockers,
              "notes": notes, "state": "idle"}

    states = await list_states()
    reserved_collateral_usd = _reserved_from_states(states)
    status["reserved_collateral_usd"] = round(reserved_collateral_usd, 2)

    if not await is_wheel_active():
        blockers.append("master switch is OFF")
        status["state"] = "off"
        last_status.update(status)
        return status
    if os.getenv("STOP_TRADING", "false").lower() == "true":
        blockers.append("STOP_TRADING=true")
        last_status.update(status)
        return status
    try:
        import prop_bot
        if await prop_bot.is_alpaca_passive_mode():
            blockers.append("Alpaca passive mode is on")
            last_status.update(status)
            return status
    except Exception:
        pass

    async with aiohttp.ClientSession() as session:
        try:
            clock = await _get(session, f"{_trading_url()}/v2/clock")
        except Exception as e:
            blockers.append(f"market clock unreadable ({e}) - no orders")
            last_status.update(status)
            return status
        if not clock.get("is_open"):
            blockers.append("market closed")
            status["state"] = "market closed"
            last_status.update(status)
            return status

        try:
            account = await _get(session, f"{_trading_url()}/v2/account")
            positions = await _get(session, f"{_trading_url()}/v2/positions")
        except Exception as e:
            blockers.append(f"fresh account read failed ({e}) - unknown balance, no orders")
            last_status.update(status)
            return status

        positions_by_symbol = {p.get("symbol"): p for p in positions or []}
        today = datetime.now(timezone.utc).date()

        async with AsyncSessionLocal() as db:
            rows = list((await db.execute(select(AlpacaWheelState))).scalars().all())
            for st in rows:
                try:
                    await _advance_state(session, st, positions_by_symbol, today, notes)
                    await _maybe_take_profit(session, st, notes)
                except Exception as e:
                    notes.append(f"{st.underlying}: reconcile error {e}")
            await db.commit()

            reserved_collateral_usd = _reserved_from_states(rows)
            status["reserved_collateral_usd"] = round(reserved_collateral_usd, 2)

            try:
                cash = float(account.get("cash"))
                options_bp = float(account.get("options_buying_power"))
                equity = float(account.get("equity"))
            except (TypeError, ValueError):
                blockers.append("account fields missing - unknown balance, no orders")
                last_status.update(status)
                return status
            level = int(float(account.get("options_trading_level") or 0))
            if level < 1:
                blockers.append(f"Alpaca options approval level {level} - cash-secured puts need level 1+")
            if account.get("trading_blocked") or account.get("account_blocked") \
                    or account.get("trade_suspended_by_user"):
                blockers.append("Alpaca is blocking orders on this account")
            open_notional = sum(abs(float(p.get("market_value") or 0)) for p in positions or []
                                if p.get("asset_class") != "us_option")
            status.update({"cash": cash, "options_buying_power": options_bp,
                           "equity": equity, "options_level": level})

            approved = [s for s in rows if s.approved]
            if not approved:
                blockers.append("no approved tickers - add one from the dashboard")

            hard_block = any(b.startswith("Alpaca") for b in blockers)
            for st in rows:
                if hard_block:
                    break
                if st.open_order_id or st.close_order_id:
                    continue
                # Un-approving a ticker stops NEW puts; shares already held
                # still get covered calls.
                if st.stage == STAGE_IDLE and not st.approved:
                    continue
                if st.stage == STAGE_IDLE and _active_wheel_count(rows) >= MAX_ACTIVE_WHEELS:
                    st.last_note = (f"waiting: {MAX_ACTIVE_WHEELS} wheel already active - "
                                    f"single-contract validation until it completes")
                    continue
                try:
                    price = await fetch_stock_price(session, st.underlying)
                    if not price:
                        st.last_note = "no live price"
                        continue
                    if st.stage == STAGE_IDLE:
                        await _try_sell_put(session, st, price, today, cash, options_bp,
                                            equity, open_notional, notes)
                        reserved_collateral_usd = _reserved_from_states(rows)
                    elif st.stage == STAGE_SHARES:
                        await _try_sell_call(session, st, price, today, notes)
                except Exception as e:
                    st.last_note = f"error: {e}"
                    notes.append(f"{st.underlying}: {e}")
            await db.commit()
            status["tickers"] = [s.to_dict() for s in rows]

    status["state"] = "running"
    status["reserved_collateral_usd"] = round(reserved_collateral_usd, 2)
    last_status.update(status)
    for n in notes:
        log.info(f"[WHEEL] {n}")
    return status


async def _try_sell_put(session, st, price, today, cash, options_bp, equity, open_notional, notes):
    target = price * (1 - PUT_STRIKE_BELOW_PCT)
    # Cheapest possible collateral first: if even the lowest-sensible strike
    # can't be covered, don't bother fetching the chain.
    ok, reason = collateral_gate(target, CONTRACTS_PER_CYCLE, cash, options_bp, reserved_collateral_usd)
    if not ok:
        st.last_note = f"waiting: {reason}"
        return
    contracts = await fetch_contracts(session, st.underlying, "put", today, strike_lte=target)
    c, mid, why = pick_put(contracts, price, today)
    if c is None:
        st.last_note = f"waiting: {why}"
        return
    ok, reason = collateral_gate(c["strike"], CONTRACTS_PER_CYCLE, cash, options_bp, reserved_collateral_usd)
    if not ok:
        st.last_note = f"waiting: {reason}"
        return
    ok, reason = risk_cap_gate(equity, open_notional, reserved_collateral_usd,
                               put_collateral(c["strike"]), _max_risk_pct())
    if not ok:
        st.last_note = f"waiting: {reason}"
        return
    limit = round_option_price(mid)
    st.open_order_id = await place_option_order(
        session, c["symbol"], CONTRACTS_PER_CYCLE, "sell", "sell_to_open", limit, f"put-{st.underlying}")
    st.stage, st.contracts = STAGE_PUT, CONTRACTS_PER_CYCLE
    st.option_symbol, st.option_strike, st.option_expiration = c["symbol"], c["strike"], c["expiration"]
    st.last_note = f"sold put {c['symbol']} @ ${limit:.2f}"
    notes.append(f"{st.underlying}: SELL PUT {c['symbol']} strike ${c['strike']:g} "
                 f"exp {c['expiration']} @ ${limit:.2f} (collateral ${put_collateral(c['strike']):,.2f})")


async def _try_sell_call(session, st, price, today, notes):
    floor = max(st.cost_basis or 0, price * (1 + CALL_STRIKE_ABOVE_PCT))
    contracts = await fetch_contracts(session, st.underlying, "call", today, strike_gte=floor)
    c, mid, why = pick_call(contracts, price, st.cost_basis, today)
    if c is None:
        st.last_note = f"holding shares, no call: {why}"
        return
    limit = round_option_price(mid)
    st.open_order_id = await place_option_order(
        session, c["symbol"], st.contracts, "sell", "sell_to_open", limit, f"call-{st.underlying}")
    st.stage = STAGE_CALL
    st.option_symbol, st.option_strike, st.option_expiration = c["symbol"], c["strike"], c["expiration"]
    st.last_note = f"sold covered call {c['symbol']} @ ${limit:.2f}"
    notes.append(f"{st.underlying}: SELL CALL {c['symbol']} strike ${c['strike']:g} "
                 f"(cost basis ${st.cost_basis:.2f}) @ ${limit:.2f}")


async def daily_summary():
    states = await list_states()
    lines = [f"{s.underlying}: stage {s.stage}, premium to date ${s.premium_total or 0:,.2f}, "
             f"cycles {s.cycles_completed or 0}" + (f", open {s.option_symbol}" if s.option_symbol else "")
             for s in states if s.approved]
    total = sum(s.premium_total or 0 for s in states)
    summary = {"date": date.today().isoformat(), "premium_total": round(total, 2),
               "reserved_collateral_usd": round(reserved_collateral_usd, 2), "lines": lines}
    log.info(f"[WHEEL] DAILY SUMMARY - premium kept across all cycles ${total:,.2f}")
    for line in lines or ["no approved tickers"]:
        log.info(f"[WHEEL]   {line}")
    last_status["summary"] = summary
    return summary


def _is_summary_time(now_et):
    return now_et.weekday() < 5 and (now_et.hour, now_et.minute) >= (16, 0)


def run():
    """Background thread. Checks every 15 minutes; the cycle itself refuses
    to trade outside market hours, so a closed-market check is cheap."""
    global _last_summary_date
    log.info("[WHEEL] Alpaca wheel engine started - real orders, OFF until the dashboard "
             "switch is on and a ticker is approved. Cash-secured puts only; covered calls "
             "never below cost basis.")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
    except Exception:
        et = timezone(timedelta(hours=-4))
    while True:
        try:
            loop.run_until_complete(run_wheel_cycle())
        except Exception as e:
            log.error(f"[WHEEL] cycle error: {e}")
        now_et = datetime.now(et)
        if _is_summary_time(now_et) and _last_summary_date != now_et.date() \
                and loop.run_until_complete(is_wheel_active()):
            try:
                loop.run_until_complete(daily_summary())
                _last_summary_date = now_et.date()
            except Exception as e:
                log.error(f"[WHEEL] summary error: {e}")
        time.sleep(CHECK_INTERVAL_SECONDS)

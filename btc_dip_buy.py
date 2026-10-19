"""GRID_BTC_DIP_BUY: buy $15 of BTC on a dip, paid for by profit. Never sell it.

The owner, 2026-10-19, first: "every time it dropped 10% lower than its
weekly high, buy. And then we'll wait to do the sale." Then, the same day:
"Do $15 per buy. Only make a $15 BTC buy when we have made $15 from all our
sales. I don't think it's going to drop to 75,000. So I say 81 or 82."
$81,000 was taken: that week BTC spent 105 of 168 hours under $82,000 -
a "dip" at that level is most of the time - and 24 under $81,000.

THE RULE
  * BTC buys when its price is at or under GRID_BTC_DIP_BUY_PRICE ($81,000).
  * Each $15 buy needs $15 of profit the grid's and the coin picker's sales
    have banked since the rule was switched on, less what earlier dip buys
    already used. Sales, not the prediction bets placed outside the bot.
  * The price is Coinbase's public ticker - a GET, no key.

WHAT STILL APPLIES
  * The cash floor (GRID_CASH_RESERVE_USD), through the grid's own
    spendable_for_slice, and the $15 viability minimum.
  * The owner's 20% single-coin ceiling (concentration_gate), judged on the
    whole account after the buy - the same book auto_trim uses.
  * Maker-only: a post-only order at the bid. No market fallback.
  * USD only. USDC is never converted for this buy; short of USD, no buy.

WHAT IT IS NOT
  * Not a grid branch and not a slice. The coin joins the account's BTC,
    which NO_SELL_COINS (no_sell.py) already keeps from being sold. 21 stays
    21. It never sells, and nothing here can.
  * Not on unless GRID_BTC_DIP_BUY is exactly "true". It runs under
    STOP_TRADING, as GRID_SELL_ONLY does - that is what the owner asked for.

PRACTICE FIRST
  The owner, the same day: "avoid doing anything outside of the sandbox until your
  bot has proven it can run flawlessly." GRID_BTC_DIP_BUY=paper runs the same rule,
  gates and profit funding on live prices but places no order: a buy is recorded at
  the price seen, against its own record (btc_dip_buy_paper), and the feed says
  PAPER_BUY. "true" is real money; anything else is off.

Every input that cannot be read is a no-buy, never a guessed yes.
"""
import logging
import os
import time
from datetime import datetime, timezone

from env_config import env_float

log = logging.getLogger("btc_dip_buy")

GRID_BTC_DIP_BUY_ENV = "GRID_BTC_DIP_BUY"
PRODUCT_ID = "BTC-USD"
MIN_VIABLE_USD = 15.0          # the owner's viability floor; never lower
BUY_USD = env_float("GRID_BTC_DIP_BUY_USD", 15.0)
BUY_AT_PRICE = env_float("GRID_BTC_DIP_BUY_PRICE", 81000.0)

# base_capital = epoch the rule was first switched on (profit counts from
# there), starting_capital = USD the rule has spent on BTC since.
STATE_KEY = "btc_dip_buy"
PAPER_STATE_KEY = "btc_dip_buy_paper"

# Spent but not yet saved. The database is the record; this covers a fill
# whose write then failed, so a failed write can never let the same profit
# pay for a second buy.
_unsaved_spent = {"live": 0.0, "paper": 0.0}
_said = {}
# The fleet loop passes every ~30s; the market is read at most once a minute.
PASS_SECONDS = 60.0
_next_pass_at = 0.0


def mode() -> str:
    """"live" for exactly "true", "paper" for exactly "paper", otherwise "off"
    (any case, surrounding spaces ignored)."""
    return {"true": "live", "paper": "paper"}.get(
        (os.getenv(GRID_BTC_DIP_BUY_ENV) or "").strip().lower(), "off")


def enabled() -> bool:
    return mode() != "off"


def verdict(price, profit_since, spent, buy_usd=None, buy_at=None):
    """(ok, reason) for one pass. Pure.

    profit_since is the grid's banked profit since the rule switched on,
    spent what earlier dip buys took from it. None for either is unreadable.
    """
    buy_usd = BUY_USD if buy_usd is None else buy_usd
    buy_at = BUY_AT_PRICE if buy_at is None else buy_at
    if buy_usd < MIN_VIABLE_USD:
        return False, (f"GRID_BTC_DIP_BUY_USD ${buy_usd:,.2f} is under the "
                       f"${MIN_VIABLE_USD:.0f} minimum - no buy")
    try:
        price = float(price)
    except (TypeError, ValueError):
        return False, "BTC price unreadable - no buy"
    if not price > 0:
        return False, "BTC price unreadable - no buy"
    if profit_since is None or spent is None:
        return False, "profit since the rule switched on unreadable - no buy"
    if price > buy_at:
        return False, f"waiting - BTC ${price:,.2f} is above the ${buy_at:,.0f} buy price"
    budget = float(profit_since) - float(spent)
    if budget < buy_usd:
        return False, (f"waiting for profit - sales have banked ${budget:,.2f} toward the next "
                       f"${buy_usd:,.0f} BTC buy (${profit_since:,.2f} since the rule "
                       f"switched on, ${spent:,.2f} already used)")
    return True, (f"BTC ${price:,.2f} is at or under ${buy_at:,.0f} and sales have banked "
                  f"${budget:,.2f} to pay for it")


async def price_now(session):
    """BTC's last trade price from Coinbase's public ticker. None when unread."""
    import aiohttp
    try:
        async with session.get(f"https://api.exchange.coinbase.com/products/{PRODUCT_ID}/ticker",
                               headers={"User-Agent": "empire-v2-grid"},
                               timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                return None
            return float((await r.json(content_type=None)).get("price"))
    except Exception as e:
        log.info(f"[BTC-DIP] public ticker unreadable ({type(e).__name__}) - no buy")
        return None


async def read_state(now, key=STATE_KEY):
    """(start epoch, USD spent). Creates the row on first use, so profit
    counts from the moment the switch is first seen on. Raises when the
    table cannot be read - the caller treats that as no-buy."""
    from sqlalchemy import select
    from database import get_session_factory
    from models import TradingBotState
    async with get_session_factory()() as db:
        row = (await db.execute(select(TradingBotState)
                                .where(TradingBotState.bot_name == key))).scalar_one_or_none()
        if row is None or not row.base_capital:
            if row is None:
                row = TradingBotState(bot_name=key)
                db.add(row)
            row.base_capital, row.starting_capital = float(now), 0.0
            await db.commit()
        return float(row.base_capital), float(row.starting_capital or 0.0)


async def profit_since(start):
    """The profit every sale booked after `start`: the grid's, and the coin
    picker's (coin_picker.py) - "all our sales". Raises when either cannot be
    read, so an unreadable book is a no-buy, never a guess."""
    from sqlalchemy import func, select
    from database import get_session_factory
    from models import CryptoGridTradeHistory
    import coin_picker
    since = datetime.fromtimestamp(start, timezone.utc).replace(tzinfo=None)
    async with get_session_factory()() as db:
        total = (await db.execute(
            select(func.coalesce(func.sum(CryptoGridTradeHistory.pnl), 0.0))
            .where(CryptoGridTradeHistory.closed_at > since)
        )).scalar()
    return float(total or 0.0) + await coin_picker.banked_since(since)


async def add_spent(usd, key=STATE_KEY):
    from sqlalchemy import select
    from database import get_session_factory
    from models import TradingBotState
    async with get_session_factory()() as db:
        row = (await db.execute(select(TradingBotState)
                                .where(TradingBotState.bot_name == key))).scalar_one()
        row.starting_capital = float(row.starting_capital or 0.0) + float(usd)
        await db.commit()


def _say(key, text):
    """Log a standing condition once, not every pass."""
    if _said.get(key) != text:
        _said[key] = text
        log.info(f"[BTC-DIP] {text}")


async def maybe_buy(session, now=None):
    """One pass. Returns (bought, reason). Never raises."""
    global _next_pass_at
    which = mode()
    if which == "off":
        return False, f"{GRID_BTC_DIP_BUY_ENV} is not on"
    paper = which == "paper"
    key = PAPER_STATE_KEY if paper else STATE_KEY
    now = time.time() if now is None else now
    if now < _next_pass_at:
        return False, "read this minute already"
    _next_pass_at = now + PASS_SECONDS
    try:
        import concentration_gate
        import crypto_btc_compound_bot as engine
        import crypto_grid_bot as grid

        try:
            start, spent = await read_state(now, key)
            made = await profit_since(start)
        except Exception as e:
            _say("state", f"profit or dip-buy record unreadable ({type(e).__name__}) - no buy")
            return False, "profit or dip-buy record unreadable - no buy"
        spent += _unsaved_spent[which]

        price = await price_now(session)
        ok, why = verdict(price, made, spent)
        if not ok:
            _say("verdict", why)
            return False, why

        usd, usdc, err = await grid.wallet_cash(session)
        if usd is None:
            _say("cash", f"cash unreadable ({err}) - no buy")
            return False, "cash unreadable - no buy"
        reserve, _ = await grid.unfunded_deployment_reserve()
        spend, spend_why = grid.spendable_for_slice(BUY_USD, usd + (usdc or 0.0),
                                                    deployment_reserve=reserve)
        if spend < BUY_USD:
            why = f"cash floor - {spend_why}"
            await _feed("CASH_RESERVE", why)
            return False, why
        if usd < spend:
            why = (f"USD ${usd:,.2f} is short of the ${spend:,.2f} buy - USDC is not "
                   f"converted for the BTC dip buy")
            await _feed("CASH_RESERVE", why)
            return False, why
        conc_ok, conc_why = concentration_gate.concentration_verdict(
            PRODUCT_ID, await grid.account_market_book(), spend)
        if not conc_ok:
            await _feed("CONCENTRATION", conc_why)
            return False, conc_why

        # Practice: a buy at the bid is taken at the price seen now.
        fill = ((spend / price, price) if paper else
                await engine.place_maker_buy(session, spend, PRODUCT_ID,
                                             await grid.maker_wait_seconds()))
        if not fill:
            why = (f"maker buy of ${spend:,.2f} did not fill "
                   f"({engine._last_order_error.get(PRODUCT_ID, 'nobody sold at the bid')}) - "
                   f"tries again next pass")
            _say("fill", why)
            return False, why
        qty, px = fill
        cost = qty * px
        try:
            await add_spent(cost + _unsaved_spent[which], key)
            _unsaved_spent[which] = 0.0
        except Exception as e:
            _unsaved_spent[which] += cost
            log.error(f"[BTC-DIP] bought, but the record did not save ({type(e).__name__}: {e}) - "
                      f"this process still counts it, so the same profit cannot pay twice")
        msg = (f"BTC dip {'practice ' if paper else ''}buy: {qty:.8f} BTC @ ${px:,.2f} "
               f"(${cost:,.2f}), paid for by profit - sales had banked ${made - spent:,.2f} "
               f"toward it. " + ("No order placed - practice only." if paper
                                 else "Held: BTC is on the no-sell list."))
        log.warning(f"[BTC-DIP] {msg}")
        await _feed("PAPER_BUY" if paper else "BUY", msg, throttle=False)
        return True, msg
    except Exception as e:
        log.error(f"[BTC-DIP] pass failed ({type(e).__name__}: {e}) - no buy")
        return False, f"pass failed ({type(e).__name__})"


async def _feed(verdict_name, reason, throttle=True):
    """One activity-feed row, through the grid's own never-raising writer."""
    try:
        import crypto_grid_bot as grid
        if throttle and not grid._feed_should_write(verdict_name, PRODUCT_ID):
            return
        await grid._record_gate_decision("btc_dip_buy", PRODUCT_ID, verdict_name, reason)
    except Exception:
        pass

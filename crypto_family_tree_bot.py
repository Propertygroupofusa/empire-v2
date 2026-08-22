"""
FAMILY TREE COMPOUNDING BOT — Coinbase, multiple single-position branches

Built on top of crypto_btc_compound_bot.py's proven engine (auth, order
placement, ATR-based volatility, adaptive profit target, stop-loss) rather
than duplicating it - this module imports that one as a library (see
`engine` below) and reuses its low-level functions for any coin, not just
BTC.

THE MECHANISM, per the account owner's spec:
  BTC is the root branch ("Level 0"). Every branch runs the exact same
  single-position engine, just on a different coin. When a branch's own
  tracked value crosses a new $1,000 milestone (configurable via
  TREE_UNLOCK_TIER_USD) AND that branch isn't currently floor-breached
  ("unhealthy" - see below), it spins off a new branch: $50 (configurable
  via TREE_SEED_USD) becomes that new branch's starting capital, seeded
  into the next coin from an ordered eligibility list. The parent is NOT
  abandoned - it keeps trading with whatever's left after the seed. A
  branch's OWN later crossings of $2,000, $3,000, etc. spin off further
  children the same way, so the tree keeps growing as long as branches
  keep earning it.

THE FUND-ISOLATION PROBLEM THIS SOLVES: there is only ONE real Coinbase
account/USD wallet - branches don't get real sub-accounts. If every branch
sized its buys off "the real account balance" the way the single-coin
version does, two branches trying to buy at the same real moment would
fight over the same real dollars. Instead, each branch has a persisted
VIRTUAL allocation (CryptoTreeBranch.allocated_usd, see models.py) - its
own slice of the one real pool. A branch only ever spends up to its own
allocated_usd (and never more than the real account balance actually
allows, as a hard backstop). Spawning a child is a pure bookkeeping
transfer between two allocated_usd numbers - no trade needed, since the
real dollars never left the one real wallet to begin with.

THE COIN ORDER is chronological-ish, not literal history: real early
altcoins like Namecoin were often illiquid or are gone entirely, so this
list is the same 28 pairs crypto_coinbase_bot.py already trades (already
vetted as liquid on Coinbase), reordered by approximate real launch year.
Precision isn't the point here - a defensible, fixed order to grow through
is.

THE HONEST LIMITS, same as crypto_btc_compound_bot.py's: the per-branch
equity floor ratchet bounds how far a branch can give back progress it
already banked - it does not make any individual trade risk-free, and a
branch can lose capital on its way toward its first $1,000 the same as any
other branch. "Healthy" (required to spawn a child) means "not currently
below its own locked floor" - the same defensive-mode idea from the spec:
a branch mid-drawdown doesn't get to spend seed money on a new branch.
"""
import asyncio
import logging
import math
import os
import threading
import time
import traceback
from datetime import datetime, timezone

from sqlalchemy import select
from database import AsyncSessionLocal
from models import BotPosition, CryptoTreeBranch, TradingBotState

import crypto_btc_compound_bot as engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("crypto_family_tree_bot")

ROOT_BOT_NAME = "crypto_btc_compound"  # same name the single-bot version used tonight - see ensure_root_exists()
ROOT_PRODUCT_ID = "BTC-USD"

SEED_USD = engine._safe_float_env("TREE_SEED_USD", "50")
# Lowered from the original $1,000 spec at the account owner's request:
# starting from a $50 seed, a $1,000 bar meant realistically no branch
# would spawn for a long time. $300 trades a real cost (each $50 seed is a
# bigger bite out of a smaller milestone, so branches start thinner) for
# branches actually spawning instead of the tree never growing.
UNLOCK_TIER_USD = engine._safe_float_env("TREE_UNLOCK_TIER_USD", "300")
BRANCH_FLOOR_TIER = engine._safe_float_env("TREE_BRANCH_FLOOR_TIER", "50")
COORDINATOR_SCAN_SECONDS = engine._safe_int_env("TREE_COORDINATOR_SCAN_SECONDS", "20")

MIN_TRADE_USD = engine.MIN_TRADE_USD
CYCLE_SECONDS = engine.CYCLE_SECONDS
STOP_LOSS_PCT = engine.STOP_LOSS_PCT
ROUND_TRIP_FEE_RATE = engine.ROUND_TRIP_FEE_RATE

# Ordered eligibility list - BTC is the root, not in this list. Approximate
# real launch year noted per entry; order is fixed and walked through once,
# front to back, as branches earn the right to spawn the next one.
# XRP and SHIB moved to #2/#3 at the account owner's explicit request,
# ahead of where their launch year alone would place them - everything
# else keeps its original relative order.
COIN_FAMILY_TREE = [
    "LTC-USD",    # 2011
    "XRP-USD",    # 2012
    "SHIB-USD",   # 2020 - moved up to #3 by request
    "DOGE-USD",   # 2013
    "ETH-USD",    # 2015
    "LINK-USD",   # 2017
    "ADA-USD",    # 2017
    "STX-USD",    # 2019
    "ATOM-USD",   # 2019
    "RNDR-USD",   # 2020
    "DOT-USD",    # 2020
    "UNI-USD",    # 2020
    "AAVE-USD",   # 2020
    "SOL-USD",    # 2020
    "AVAX-USD",   # 2020
    "NEAR-USD",   # 2020
    "MATIC-USD",  # 2020
    "LDO-USD",    # 2021
    "ICP-USD",    # 2021
    "FLOKI-USD",  # 2021
    "OP-USD",     # 2022
    "APT-USD",    # 2022
    "BONK-USD",   # 2022
    "ARB-USD",    # 2023
    "SUI-USD",    # 2023
    "BLUR-USD",   # 2023
    "JUP-USD",    # 2024
]


async def load_branch(bot_name: str):
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(CryptoTreeBranch).where(CryptoTreeBranch.bot_name == bot_name))
        return result.scalar_one_or_none()


async def load_all_branches():
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(CryptoTreeBranch))
        return result.scalars().all()


async def get_next_eligible_product_id():
    """First coin in COIN_FAMILY_TREE not already claimed by an existing branch."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(CryptoTreeBranch.product_id))
        claimed = set(result.scalars().all())
    for product_id in COIN_FAMILY_TREE:
        if product_id not in claimed:
            return product_id
    return None


async def _load_branch_position(bot_name: str):
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(BotPosition).where(BotPosition.bot == bot_name))
        return result.scalar_one_or_none()


async def _save_branch_position(bot_name, product_id, entry_price, qty, target_price, stop_price):
    async with AsyncSessionLocal() as db:
        db.add(BotPosition(
            bot=bot_name, symbol=product_id, side="long",
            entry_price=entry_price, qty=qty,
            target_price=target_price, stop_price=stop_price,
            opened_at=datetime.utcnow(),
        ))
        await db.commit()


async def _clear_branch_position(bot_name: str):
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(BotPosition).where(BotPosition.bot == bot_name))
        pos = result.scalar_one_or_none()
        if pos:
            await db.delete(pos)
            await db.commit()


async def ensure_root_exists(session):
    """First-ever startup: seed the BTC root branch. Reuses the exact
    bot_name crypto_btc_compound_bot.py used tonight so continuity carries
    over automatically if that single-bot version already had an open
    position or an already-raised equity floor when this replaced it -
    otherwise that position would be left with no thread managing it at
    all, which is the one thing this migration cannot be allowed to do."""
    if await load_branch(ROOT_BOT_NAME) is not None:
        return

    balance, err = await engine.get_usd_balance(session)
    if balance is None:
        log.error(f"[TREE] Could not fetch real balance to seed root branch ({err}) - will retry next scan")
        return

    existing_position = await _load_branch_position(ROOT_BOT_NAME)
    position_value = 0.0
    if existing_position is not None:
        price, _ = await engine.get_price_and_volatility(session, ROOT_PRODUCT_ID)
        position_value = existing_position.qty * price if price else existing_position.entry_price * existing_position.qty

    inherited_floor = 0.0
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == "crypto_btc_compound_equity_floor"))
            row = result.scalar_one_or_none()
            if row and row.base_capital is not None:
                inherited_floor = row.base_capital
    except Exception as e:
        log.warning(f"[TREE] Could not check for an inherited equity floor: {e}")

    starting_equity = balance + position_value
    async with AsyncSessionLocal() as db:
        db.add(CryptoTreeBranch(
            bot_name=ROOT_BOT_NAME, product_id=ROOT_PRODUCT_ID, parent_bot_name=None,
            allocated_usd=starting_equity, next_unlock_tier=UNLOCK_TIER_USD, equity_floor=inherited_floor,
        ))
        await db.commit()

    detail = f"cash ${balance:.2f}" + (f" + open position ~${position_value:.2f}" if position_value else "")
    log.info(f"[TREE] 🌳 Root branch seeded: {ROOT_BOT_NAME} with ${starting_equity:.2f} ({detail}) | inherited floor ${inherited_floor:,.2f}")


# Only this exact bot_name - crypto_coinbase_bot.py's own BOT_NAME constant
# - is ever eligible for adoption. Deliberately not "any position with no
# matching branch": BotPosition is shared with prop_bot.py too
# (bot="prop_apex", trading Alpaca stock/futures-proxy symbols) - treating
# one of ITS positions as a Coinbase product_id would try to place a real
# crypto order for a stock ticker. Only the one legacy bot this system
# actually replaced is safe to fold in.
ORPHAN_SOURCE_BOT_NAME = "crypto_coinbase"


async def adopt_orphaned_positions(session):
    """Finds real open positions still sitting under the old multi-pair
    bot's name (crypto_coinbase) with no branch managing them - true
    orphans, since that bot's thread stopped running the moment
    CRYPTO_STRATEGY_MODE moved away from "multi_pair", but the real
    position never closed. Confirmed live: an LDO-USD position bought
    before tonight's switch was left with no stop-loss, no target, no
    thread checking on it at all. Folds each one into the tree as its own
    branch running the same engine everything else here runs - not a
    special case, just a branch whose first "buy" was actually inherited
    instead of placed fresh."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(BotPosition).where(BotPosition.bot == ORPHAN_SOURCE_BOT_NAME))
        orphans = result.scalars().all()

    for position in orphans:
        product_id = position.symbol.replace("/", "-")
        bot_name = f"crypto_tree_{product_id.lower().replace('-', '_')}"

        already_claimed = await load_branch(bot_name)
        if already_claimed is not None:
            log.warning(f"[TREE] Orphaned {product_id} position exists but {bot_name} is already a branch - "
                        f"leaving it under '{ORPHAN_SOURCE_BOT_NAME}', unmanaged. Needs a manual look.")
            continue

        price, atr_pct = await engine.get_price_and_volatility(session, product_id)
        if price is None:
            log.warning(f"[TREE] Could not fetch price for orphaned {product_id} position - will retry adopting next scan")
            continue

        # This position was bought by the old bot, not this cycle, so
        # there's no "just now" fill to compute a target/stop from. Target
        # and stop are deliberately anchored to DIFFERENT prices here:
        #
        # - target stays anchored to the REAL original entry_price, so it
        #   only sells once it's an actual profit versus what was really
        #   paid - not a lower bar just because adoption happened later.
        #
        # - stop is anchored to the CURRENT price at adoption time, not
        #   the old entry_price. Anchoring the stop to a stale entry would
        #   retroactively punish the position for whatever it did BEFORE
        #   this system ever started watching it - if it had already
        #   drifted down since the original buy, a stop derived from that
        #   old entry could sit at or above the current price and force an
        #   immediate loss on literally the first cycle after adoption,
        #   for a decline this system had no part in and no chance to
        #   react to. Anchoring to "now" instead means: protect it from
        #   HERE forward. One real side effect worth knowing - if it's
        #   already sitting on a gain at adoption time, this stop sits
        #   ABOVE the original entry, so it can't round-trip all the way
        #   back to a loss without hitting the profit target first. If
        #   it's currently underwater, this does not erase that - a
        #   further decline from here is still a real, possible loss.
        target_pct = engine.pick_target_pct(atr_pct)
        target_price = position.entry_price * (1 + target_pct)
        stop_price = price * (1 - STOP_LOSS_PCT)
        position_value = position.qty * price

        async with AsyncSessionLocal() as db:
            db.add(CryptoTreeBranch(
                bot_name=bot_name, product_id=product_id, parent_bot_name=None,
                allocated_usd=position_value, next_unlock_tier=UNLOCK_TIER_USD, equity_floor=0.0,
            ))
            result = await db.execute(select(BotPosition).where(BotPosition.id == position.id))
            row = result.scalar_one_or_none()
            if row:
                row.bot = bot_name
                row.target_price = target_price
                row.stop_price = stop_price
            await db.commit()

        unrealized_pct = (price / position.entry_price - 1) * 100
        log.info(
            f"[TREE] 🌿 ADOPTED orphaned {product_id} position from the old bot: {position.qty:.8f} @ "
            f"entry ${position.entry_price:,.2f} | now ${price:,.2f} ({unrealized_pct:+.2f}%) | "
            f"now managed as {bot_name} - target +{target_pct*100:.2f}% (${target_price:,.2f}) | "
            f"stop -{STOP_LOSS_PCT*100:.2f}% (${stop_price:,.2f})"
        )


async def _maybe_spawn_child(branch):
    """Called right after a branch's allocated_usd is updated (always right
    after a real sell, when the number is freshly accurate). If it just
    crossed a new unlock tier, isn't floor-breached, and a coin remains
    unclaimed, spins off a new branch - a bookkeeping transfer only."""
    if branch.allocated_usd < branch.next_unlock_tier:
        return
    if branch.allocated_usd < branch.equity_floor:
        log.info(f"[TREE] {branch.bot_name} reached ${branch.allocated_usd:.2f} but is below its own floor ${branch.equity_floor:,.2f} - not spawning while unhealthy")
        return
    next_product = await get_next_eligible_product_id()
    if next_product is None:
        log.info(f"[TREE] {branch.bot_name} crossed ${branch.next_unlock_tier:,.0f} but every eligible coin is already claimed - no child to spawn")
        return

    child_name = f"crypto_tree_{next_product.lower().replace('-', '_')}"
    milestone = branch.next_unlock_tier
    async with AsyncSessionLocal() as db:
        # Re-check against a fresh row under this transaction, so the
        # coordinator's scan and this branch's own cycle can't both spawn
        # a child for the same crossing.
        result = await db.execute(select(CryptoTreeBranch).where(CryptoTreeBranch.bot_name == branch.bot_name))
        fresh = result.scalar_one_or_none()
        if not fresh or fresh.allocated_usd < fresh.next_unlock_tier:
            return
        fresh.allocated_usd -= SEED_USD
        fresh.next_unlock_tier += UNLOCK_TIER_USD
        db.add(CryptoTreeBranch(
            bot_name=child_name, product_id=next_product, parent_bot_name=branch.bot_name,
            allocated_usd=SEED_USD, next_unlock_tier=UNLOCK_TIER_USD, equity_floor=0.0,
        ))
        await db.commit()
        remaining = fresh.allocated_usd

    log.info(
        f"[TREE] 🌱 {branch.bot_name} crossed ${milestone:,.0f} - spawned {child_name} ({next_product}) "
        f"with ${SEED_USD:.2f} seed | {branch.bot_name} continues with ${remaining:.2f}"
    )


async def _branch_sell_and_settle(session, bot_name, product_id, position, reason):
    fill = await engine.place_market_sell(session, position.qty, product_id)
    if not fill:
        log.warning(f"[TREE] {bot_name}: {reason} but sell did not fill - will retry next cycle")
        return
    filled_qty, filled_price = fill
    gross_value = filled_price * filled_qty
    fee = gross_value * (ROUND_TRIP_FEE_RATE / 2)
    new_allocated = gross_value - fee
    pnl = new_allocated - (position.entry_price * position.qty)

    await _clear_branch_position(bot_name)
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(CryptoTreeBranch).where(CryptoTreeBranch.bot_name == bot_name))
        row = result.scalar_one_or_none()
        if row:
            row.allocated_usd = new_allocated
            # The floor ratchet only ever raises the floor as gains are
            # banked - but a forced exit (or a stop-loss that dipped below
            # the floor in the gap between cycle checks) can leave the
            # balance below a floor that was set before the loss. Without
            # this, the branch would compare its new, lower balance against
            # that same too-high floor forever, stay "breached" forever, and
            # never trade again since trading is the only thing that could
            # raise the balance back over it - a permanent stall, not a
            # pause. Reset the floor down to match the new balance's own
            # tier so the branch can resume trading immediately; it will
            # only ratchet back up again from here as it earns real gains.
            new_tier_floor = math.floor(new_allocated / BRANCH_FLOOR_TIER) * BRANCH_FLOOR_TIER
            if new_tier_floor < row.equity_floor:
                log.info(f"[TREE] 🪜 {bot_name} floor lowered ${row.equity_floor:,.2f} -> ${new_tier_floor:,.2f} to match post-sale balance ${new_allocated:.2f}")
                row.equity_floor = new_tier_floor
            await db.commit()

    log.info(
        f"[TREE] {bot_name} SOLD {filled_qty:.8f} {product_id} @ ${filled_price:,.2f} ({reason}) | "
        f"entry ${position.entry_price:,.2f} -> exit ${filled_price:,.2f} | "
        f"P&L: {'+' if pnl >= 0 else ''}${pnl:.2f} after est. fees | branch now ${new_allocated:.2f}"
    )
    if row is not None:
        await _maybe_spawn_child(row)


async def run_branch_cycle(bot_name: str) -> bool:
    """One cycle for one branch. Returns False if this branch's row is
    gone (its thread should stop), True otherwise."""
    branch = await load_branch(bot_name)
    if branch is None:
        return False

    if not engine.COINBASE_API_KEY_NAME or not engine.COINBASE_API_PRIVATE_KEY:
        log.error(f"[TREE] {bot_name}: Coinbase credentials not set - cannot trade")
        return True

    async with engine.aiohttp.ClientSession() as session:
        position = await _load_branch_position(bot_name)
        real_balance, real_balance_err = await engine.get_usd_balance(session)
        price, atr_pct = await engine.get_price_and_volatility(session, branch.product_id)

        equity = branch.allocated_usd
        if position is not None and price is not None:
            equity = position.qty * price

        new_floor = branch.equity_floor
        if equity >= BRANCH_FLOOR_TIER:
            candidate = math.floor(equity / BRANCH_FLOOR_TIER) * BRANCH_FLOOR_TIER
            if candidate > new_floor:
                new_floor = candidate
                async with AsyncSessionLocal() as db:
                    result = await db.execute(select(CryptoTreeBranch).where(CryptoTreeBranch.bot_name == bot_name))
                    row = result.scalar_one_or_none()
                    if row:
                        row.equity_floor = new_floor
                        await db.commit()
                log.info(f"[TREE] 🪜 {bot_name} floor raised to ${new_floor:,.2f}")
                branch.equity_floor = new_floor

        breached = equity < branch.equity_floor

        if breached:
            if position is not None:
                if price is None:
                    log.warning(f"[TREE] {bot_name}: floor breach but no price available to force-sell - retry next cycle")
                    return True
                log.warning(f"[TREE] 🛑 {bot_name} EQUITY FLOOR BREACH: ${equity:.2f} < floor ${branch.equity_floor:,.2f} - force-selling, pausing entries")
                await _branch_sell_and_settle(session, bot_name, branch.product_id, position, "EQUITY FLOOR BREACH - forced exit")
            else:
                log.info(f"[TREE] 🛑 {bot_name}: ${equity:.2f} below floor ${branch.equity_floor:,.2f} - entries paused until it recovers")
            return True

        if position is None:
            if real_balance is None:
                log.warning(f"[TREE] {bot_name}: real balance unavailable ({real_balance_err}) - skipping this cycle")
                return True
            spend = min(branch.allocated_usd, real_balance)
            if spend < MIN_TRADE_USD:
                log.info(f"[TREE] {bot_name}: allocated ${branch.allocated_usd:.2f} below minimum trade ${MIN_TRADE_USD:.2f} - waiting")
                return True
            if price is None:
                log.warning(f"[TREE] {bot_name}: could not fetch price/volatility - skipping this cycle")
                return True

            target_pct = engine.pick_target_pct(atr_pct)
            fill = await engine.place_market_buy(session, spend, branch.product_id)
            if not fill:
                log.warning(f"[TREE] {bot_name}: buy did not fill - will retry next cycle")
                return True
            filled_qty, filled_price = fill
            target_price = filled_price * (1 + target_pct)
            stop_price = filled_price * (1 - STOP_LOSS_PCT)
            await _save_branch_position(bot_name, branch.product_id, filled_price, filled_qty, target_price, stop_price)
            log.info(
                f"[TREE] {bot_name} BOUGHT {filled_qty:.8f} {branch.product_id} @ ${filled_price:,.2f} (${spend:.2f} deployed) | "
                f"ATR {atr_pct*100:.2f}% -> target +{target_pct*100:.2f}% (${target_price:,.2f}) | "
                f"stop -{STOP_LOSS_PCT*100:.2f}% (${stop_price:,.2f}) | branch total ${branch.allocated_usd:.2f} | floor ${branch.equity_floor:,.2f}"
            )
            return True

        if price is None:
            log.warning(f"[TREE] {bot_name}: could not fetch current price - holding, will re-check next cycle")
            return True

        unrealized_pct = (price / position.entry_price - 1) * 100
        if price >= position.target_price:
            await _branch_sell_and_settle(session, bot_name, branch.product_id, position, "TARGET HIT")
        elif price <= position.stop_price:
            await _branch_sell_and_settle(session, bot_name, branch.product_id, position, "STOP HIT")
        else:
            log.info(
                f"[TREE] {bot_name} HOLDING {position.qty:.8f} {branch.product_id} | entry ${position.entry_price:,.2f} | "
                f"now ${price:,.2f} ({unrealized_pct:+.2f}%) | target ${position.target_price:,.2f} | "
                f"stop ${position.stop_price:,.2f} | equity ${equity:.2f} | floor ${branch.equity_floor:,.2f}"
            )
        return True


_running_threads = {}
_threads_lock = threading.Lock()


def _branch_thread_main(bot_name: str):
    log.info(f"[TREE] Starting branch thread: {bot_name}")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    while True:
        try:
            keep_going = loop.run_until_complete(run_branch_cycle(bot_name))
            if keep_going is False:
                log.warning(f"[TREE] {bot_name}: branch row no longer exists - stopping this thread")
                break
        except RuntimeError as e:
            if "attached to a different loop" in str(e):
                log.warning(f"[TREE] {bot_name}: event loop mismatch - recreating")
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            else:
                log.error(f"[TREE] {bot_name} cycle error: {e}")
                log.error(f"Traceback: {traceback.format_exc()}")
        except Exception as e:
            log.error(f"[TREE] {bot_name} cycle error: {e}")
            log.error(f"Traceback: {traceback.format_exc()}")
        time.sleep(CYCLE_SECONDS)
    with _threads_lock:
        _running_threads.pop(bot_name, None)


def run():
    """Entry point started as main.py's daemon thread - this IS the
    coordinator. It ensures the BTC root branch exists, then repeatedly
    scans for any branch (root or spawned child) without a running thread
    and starts one. New rows appear whenever an existing branch's own
    cycle spawns a child - this loop is what notices and brings the new
    branch to life, typically within COORDINATOR_SCAN_SECONDS."""
    log.info("=" * 60)
    log.info("FAMILY TREE COMPOUNDING BOT — coordinator")
    log.info(f"Root: {ROOT_BOT_NAME} ({ROOT_PRODUCT_ID}) | seed ${SEED_USD:.2f} per child | unlock every ${UNLOCK_TIER_USD:,.0f} | "
              f"per-branch floor steps ${BRANCH_FLOOR_TIER:,.0f} | {len(COIN_FAMILY_TREE)} coins eligible to grow into")
    log.info("=" * 60)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _scan():
        async with engine.aiohttp.ClientSession() as session:
            await ensure_root_exists(session)
            await adopt_orphaned_positions(session)
        branches = await load_all_branches()
        with _threads_lock:
            for branch in branches:
                if branch.bot_name not in _running_threads:
                    t = threading.Thread(target=_branch_thread_main, args=(branch.bot_name,), daemon=True)
                    _running_threads[branch.bot_name] = t
                    t.start()

    while True:
        try:
            loop.run_until_complete(_scan())
        except RuntimeError as e:
            if "attached to a different loop" in str(e):
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            else:
                log.error(f"[TREE] Coordinator scan error: {e}")
                log.error(f"Traceback: {traceback.format_exc()}")
        except Exception as e:
            log.error(f"[TREE] Coordinator scan error: {e}")
            log.error(f"Traceback: {traceback.format_exc()}")
        time.sleep(COORDINATOR_SCAN_SECONDS)

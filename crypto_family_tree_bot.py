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

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from database import AsyncSessionLocal
from models import BotPosition, CryptoTreeBranch, TradingBotState

import crypto_btc_compound_bot as engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("crypto_family_tree_bot")

ROOT_BOT_NAME = "crypto_btc_compound"  # same name the single-bot version used tonight - see ensure_root_exists()
ROOT_PRODUCT_ID = "BTC-USD"

SEED_USD = engine._safe_float_env("TREE_SEED_USD", "50")
# Lowered twice now, both times at the account owner's request, both times
# for the same reason: too high a bar and branches take too long between
# real wins to ever cross it, so the tree stops growing new coins. Original
# spec was $1,000; that came down to $300; with the min-profit-dollar-floor
# change also slowing how fast a small branch's balance grows (bigger
# average win needed per trade), $300 was projected to take ~40-60 real
# wins for STX/BTC to cross - weeks, not days. $150 (3x the $50 seed
# instead of 6x) roughly halves that, while still leaving the parent a real
# $100 buffer after each spawn, not a razor-thin one.
UNLOCK_TIER_USD = engine._safe_float_env("TREE_UNLOCK_TIER_USD", "150")
# The value being replaced above - used once at coordinator startup (see
# _lower_existing_unlock_tiers) to retroactively apply the new, lower tier
# to branches that already exist and are still waiting to cross their
# ORIGINAL $300 threshold. Only ever a one-time backstop for branches
# created before this change; never touches a branch that already crossed
# its first tier (its next_unlock_tier would no longer be exactly $300).
PRIOR_UNLOCK_TIER_USD = 300.0
BRANCH_FLOOR_TIER = engine._safe_float_env("TREE_BRANCH_FLOOR_TIER", "50")
COORDINATOR_SCAN_SECONDS = engine._safe_int_env("TREE_COORDINATOR_SCAN_SECONDS", "20")

# A floor-breach forced exit resets the floor down to only ~3-4% below the
# fresh post-sale balance (see the tier-reset in _branch_sell_and_settle) -
# real, but thin. Confirmed live tonight on crypto_tree_dot_usd: AAVE ->
# STOP HIT -> instantly rebought XRP -> breached again within its first
# few cycles -> instantly rebought BONK -> breached again - three real
# losses in a row because nothing stopped it from immediately betting
# again on a cushion that thin. FLOOR_BREACH_COOLDOWN_SECONDS blocks a
# branch from re-entering at all for a real cooldown window after a floor
# breach, so an ordinary price wobble right after re-entry can't
# immediately re-trigger the same safety net.
FLOOR_BREACH_COOLDOWN_SECONDS = engine._safe_int_env("TREE_FLOOR_BREACH_COOLDOWN_SECONDS", "1800")  # 30 min default
FLOOR_BREACH_COOLDOWN_KEY_PREFIX = "crypto_family_tree_floor_breach_cooldown_"

# Per the account owner: 10% of every branch's REALIZED PROFIT (not the
# whole balance, and never on a loss) gets permanently pulled out of the
# compounding loop on every profitable exit, root BTC included. "Locked
# away" here means walled off from ever being redeployed by ANY bot - the
# real dollars stay sitting in the one real Coinbase USD balance (visible
# directly in the Coinbase app), not physically transferred to a bank
# account; Coinbase's Advanced Trade API doesn't expose a programmatic ACH
# withdrawal endpoint the way this app could drive automatically (same
# limitation prop_bot.py/trading_dashboard.py already document for
# Alpaca) - an actual bank withdrawal would be a manual step, or a
# separate, bigger integration if ever wanted.
PROFIT_SKIM_PCT = engine._safe_float_env("TREE_PROFIT_SKIM_PCT", "0.10")
LOCKED_PROFIT_STATE_KEY = "crypto_family_tree_locked_usd"

# Real spendable cash (real_balance - locked_usd) below MIN_TRADE_USD just
# sits there forever on its own - no branch can ever spend less than
# MIN_TRADE_USD on a buy, so it isn't profit and run_branch_cycle's
# trading logic can never touch it. Per the account owner's request: if
# that stranded amount hasn't moved for this many hours (nothing sold to
# top it up, no branch could spend it), sweep it into the same
# locked-profit ledger the 10% skim uses - see _check_and_sweep_stranded_dust().
DUST_STUCK_HOURS = engine._safe_float_env("TREE_DUST_STUCK_HOURS", "24")
DUST_CHECK_INTERVAL_SECONDS = engine._safe_int_env("TREE_DUST_CHECK_INTERVAL_SECONDS", "900")
DUST_TRACKER_KEY = "crypto_family_tree_dust_tracker"

MIN_TRADE_USD = engine.MIN_TRADE_USD
CYCLE_SECONDS = engine.CYCLE_SECONDS
STOP_LOSS_PCT = engine.STOP_LOSS_PCT
ROUND_TRIP_FEE_RATE = engine.ROUND_TRIP_FEE_RATE
BREAKEVEN_TRIGGER_PCT = engine.BREAKEVEN_TRIGGER_PCT  # see crypto_btc_compound_bot.py for the reasoning

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


async def get_locked_usd() -> float:
    """The running total of skimmed profit walled off from ever being
    redeployed by any branch - see PROFIT_SKIM_PCT. Reusing TradingBotState
    (the same generic per-key bucket table prop_bot.py's own equity floor
    and bot_N buckets already use) rather than a new table."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == LOCKED_PROFIT_STATE_KEY))
        row = result.scalar_one_or_none()
        return row.base_capital if row else 0.0


async def _add_locked_usd(amount: float):
    if amount <= 0:
        return
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == LOCKED_PROFIT_STATE_KEY))
        row = result.scalar_one_or_none()
        if row:
            row.base_capital += amount
        else:
            db.add(TradingBotState(bot_name=LOCKED_PROFIT_STATE_KEY, base_capital=amount, starting_capital=0.0))
        await db.commit()


_last_dust_check_at = 0.0


async def _check_and_sweep_stranded_dust():
    """If the real spendable balance has been stuck below MIN_TRADE_USD
    and unchanged for DUST_STUCK_HOURS, sweep it into locked_usd instead
    of leaving it dead forever. If it grows before that (a sale added
    proceeds) or crosses MIN_TRADE_USD (a branch can now spend it), the
    clock resets - this only ever catches money that's genuinely never
    going anywhere on its own.

    Runs from the single-threaded coordinator loop (see run()'s _scan()),
    never from a per-branch thread: multiple branches share the same real
    balance, so checking this per-branch would let several branches race
    to sweep the same stranded dollars multiple times over."""
    async with engine.aiohttp.ClientSession() as session:
        real_balance, err = await engine.get_usd_balance(session)
    if real_balance is None:
        log.debug(f"[TREE] dust check: real balance unavailable ({err}) - skipping")
        return

    locked_usd = await get_locked_usd()
    spendable = max(0.0, real_balance - locked_usd)

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == DUST_TRACKER_KEY))
        tracker = result.scalar_one_or_none()

        if spendable <= 0 or spendable >= MIN_TRADE_USD:
            # Nothing stranded, or a branch can spend it now - clear any tracking.
            if tracker is not None and tracker.base_capital != 0.0:
                tracker.base_capital = 0.0
                await db.commit()
            return

        if tracker is None:
            db.add(TradingBotState(bot_name=DUST_TRACKER_KEY, base_capital=spendable, starting_capital=0.0))
            await db.commit()
            log.info(f"[TREE] 💤 Tracking ${spendable:.2f} stranded below the ${MIN_TRADE_USD:.2f} minimum trade "
                     f"- will lock it away if it's still stuck in {DUST_STUCK_HOURS:.0f}h")
            return

        if abs(tracker.base_capital - spendable) > 0.005:
            # Changed since the last check (grew or shrank) - real cash
            # moved, so this isn't dead money yet. Restart the clock.
            tracker.base_capital = spendable
            await db.commit()
            log.info(f"[TREE] 💤 Stranded dust changed (now ${spendable:.2f}) - restarting the {DUST_STUCK_HOURS:.0f}h clock")
            return

        stuck_hours = (datetime.utcnow() - tracker.updated_at).total_seconds() / 3600.0
        if stuck_hours >= DUST_STUCK_HOURS:
            await _add_locked_usd(spendable)
            tracker.base_capital = 0.0
            await db.commit()
            log.info(f"[TREE] 🔒 Swept ${spendable:.2f} of stranded dust (stuck {stuck_hours:.1f}h below the "
                     f"${MIN_TRADE_USD:.2f} minimum trade) into locked profit - permanently out of the compounding loop")


async def _record_floor_breach(bot_name: str):
    """Marks right now as this branch's most recent floor-breach time, so
    _floor_breach_cooldown_active() can block it from re-entering for a
    while. Reuses TradingBotState as a generic per-branch timestamp store
    (same pattern DUST_TRACKER_KEY uses) - updated_at is what actually
    matters here, base_capital is unused.

    Always deletes and re-inserts rather than updating an existing row in
    place: updated_at's onupdate only fires when SQLAlchemy detects an
    actual attribute change, so overwriting an existing row with the same
    base_capital value (0.0 -> 0.0, on a second breach after the first
    cooldown already expired) would silently fail to refresh the
    timestamp. A fresh INSERT always gets a fresh default=datetime.utcnow,
    with no such edge case."""
    key = FLOOR_BREACH_COOLDOWN_KEY_PREFIX + bot_name
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == key))
        row = result.scalar_one_or_none()
        if row:
            await db.delete(row)
            await db.flush()
        db.add(TradingBotState(bot_name=key, base_capital=0.0, starting_capital=0.0))
        await db.commit()


async def _floor_breach_cooldown_active(bot_name: str) -> bool:
    key = FLOOR_BREACH_COOLDOWN_KEY_PREFIX + bot_name
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == key))
        row = result.scalar_one_or_none()
        if row is None:
            return False
        elapsed = (datetime.utcnow() - row.updated_at).total_seconds()
        return elapsed < FLOOR_BREACH_COOLDOWN_SECONDS


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


async def _ensure_product_id_unique_index():
    """One-time safety migration, safe to call on every startup: adds a
    real DB-level UNIQUE index on crypto_tree_branches.product_id.

    Every branch runs as its own thread with its own independent cycle
    timer - nothing coordinates them against each other. Two branches can
    exit at close enough to the same moment that both read the "unclaimed
    coins" set before either has written its own pick back, and without a
    real constraint, both could commit the same coin as their new pick -
    two branches silently trading the identical coin at once, breaking the
    "only unclaimed coins" rule this whole feature was built around. A
    Python-level check alone can't close that window (both checks can pass
    before either write lands); only the database itself, rejecting the
    second write outright, actually guarantees it never happens.

    If duplicate product_id rows already exist (shouldn't happen, but
    would make the index impossible to create), or the index can't be
    created for any other reason, this logs a warning and leaves the
    constraint absent rather than blocking startup - the coin-switch code
    still catches that failure mode at the point of use (see
    _branch_sell_and_settle) so a missing index degrades to "the race is
    possible but rare," not a crash."""
    try:
        async with AsyncSessionLocal() as db:
            dupes = await db.execute(text(
                "SELECT product_id, COUNT(*) FROM crypto_tree_branches "
                "GROUP BY product_id HAVING COUNT(*) > 1"
            ))
            dupe_rows = dupes.fetchall()
            if dupe_rows:
                log.warning(f"[TREE] duplicate product_id rows already exist ({dupe_rows}) - "
                            f"skipping unique index, coin-claim races are NOT prevented at the DB level yet")
                return
            await db.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_crypto_tree_branches_product_id_unique "
                "ON crypto_tree_branches (product_id)"
            ))
            await db.commit()
        log.info("[TREE] product_id uniqueness enforced at the DB level - two branches can never claim the same coin")
    except Exception as e:
        log.warning(f"[TREE] could not add product_id uniqueness constraint: {e} - coin-claim races are not yet prevented at the DB level")


async def _lower_existing_unlock_tiers():
    """One-time backstop, safe to call on every startup: applies the new,
    lower UNLOCK_TIER_USD to branches that were created under the old
    PRIOR_UNLOCK_TIER_USD and are still waiting to cross it for their
    FIRST spawn. Without this, only branches spawned AFTER this code
    deploys would ever see the lower tier - every branch that already
    existed (BTC, and whatever else was running before tonight) would
    keep waiting on the old, higher bar forever, since next_unlock_tier is
    a real value stored per-branch, not re-read from the env var each
    cycle.

    Deliberately only touches rows still sitting at exactly
    PRIOR_UNLOCK_TIER_USD - a branch that already crossed its first tier
    has a next_unlock_tier reflecting real further progress (e.g. $600,
    from $300 + $300), and this must never claw that back down; it only
    ever helps a branch that hasn't spawned yet reach its first spawn
    sooner."""
    if UNLOCK_TIER_USD >= PRIOR_UNLOCK_TIER_USD:
        return
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(CryptoTreeBranch).where(CryptoTreeBranch.next_unlock_tier == PRIOR_UNLOCK_TIER_USD)
            )
            rows = result.scalars().all()
            if not rows:
                return
            for row in rows:
                row.next_unlock_tier = UNLOCK_TIER_USD
            await db.commit()
        log.info(
            f"[TREE] 🪜 lowered {len(rows)} existing branch(es) still waiting on the old "
            f"${PRIOR_UNLOCK_TIER_USD:,.0f} spawn tier down to the new ${UNLOCK_TIER_USD:,.0f}: "
            f"{', '.join(r.bot_name for r in rows)}"
        )
    except Exception as e:
        log.warning(f"[TREE] could not lower existing unlock tiers: {e}")


async def find_most_volatile_unclaimed_coin(session):
    """Among the family-tree coins not already claimed by any existing
    branch, finds the most volatile coin that's ALSO currently bullish
    (price up over the ~25-hour candle window) - called after every branch
    exit (a TARGET HIT, a STOP HIT, or a floor-breach forced exit) so a
    branch moves on to a new coin instead of repeatedly re-buying the one
    it just traded. Requiring bullish
    first means it's chasing a coin that's actually trending in a useful
    direction, not just one that's swinging randomly; volatility as the
    tiebreaker among bullish candidates means more chances for the
    adaptive profit target to fire rather than the price sitting flat.
    Higher volatility is still not free upside - the fixed stop-loss % can
    get hit faster on a bigger swing too.

    If no unclaimed coin is currently bullish, falls back to the highest
    volatility overall rather than doing nothing. Returns (product_id,
    atr_pct), or (None, None) if every coin is already claimed or none
    have usable price data right now.

    Looks up every candidate concurrently rather than one at a time: with
    up to 27 coins and a 15s timeout per request, a sequential loop's
    worst case was minutes (a branch just sitting there, not trading,
    while it worked through the whole list) - running them all at once
    caps the whole search at whatever the single slowest request takes."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(CryptoTreeBranch.product_id))
        claimed = set(result.scalars().all())
    candidates = [p for p in COIN_FAMILY_TREE if p not in claimed]

    results = await asyncio.gather(
        *(engine.get_price_volatility_and_trend(session, product_id) for product_id in candidates),
        return_exceptions=True,
    )

    best_bullish_id, best_bullish_atr = None, -1.0
    best_any_id, best_any_atr = None, -1.0
    for product_id, result in zip(candidates, results):
        if isinstance(result, Exception):
            log.warning(f"[TREE] volatility lookup failed for {product_id}: {result}")
            continue
        price, atr_pct, is_bullish = result
        if price is None or atr_pct is None:
            continue
        if atr_pct > best_any_atr:
            best_any_atr = atr_pct
            best_any_id = product_id
        if is_bullish and atr_pct > best_bullish_atr:
            best_bullish_atr = atr_pct
            best_bullish_id = product_id

    if best_bullish_id:
        return best_bullish_id, best_bullish_atr
    if best_any_id:
        log.info("[TREE] no unclaimed coin is currently bullish - falling back to highest volatility overall")
        return best_any_id, best_any_atr
    return None, None


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


async def _raise_branch_stop_to_breakeven(bot_name: str, entry_price: float):
    """Only ever moves a position's stop UP to its own entry price - never
    down, never past entry. See BREAKEVEN_TRIGGER_PCT."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(BotPosition).where(BotPosition.bot == bot_name))
        pos = result.scalar_one_or_none()
        if pos and pos.stop_price is not None and pos.stop_price < entry_price:
            pos.stop_price = entry_price
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
        target_pct = max(engine.pick_target_pct(atr_pct), engine.min_profit_target_pct(position.qty * position.entry_price, atr_pct))
        target_price = position.entry_price * (1 + target_pct)
        stop_price = price * (1 - STOP_LOSS_PCT)
        position_value = position.qty * price

        try:
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
        except IntegrityError:
            # Vanishingly unlikely (another branch would have to switch
            # INTO this exact orphaned coin in the instant between the
            # already_claimed check above and this commit), but the same
            # DB-level unique index that protects normal coin switches
            # (see _ensure_product_id_unique_index) covers this path too -
            # skip this scan, the position is still sitting under the old
            # bot's name and gets picked up again next coordinator scan.
            log.warning(f"[TREE] Orphaned {product_id} position: another branch claimed this coin first (race) - will retry adopting next scan")
            continue

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
    try:
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
    except IntegrityError:
        # Another branch crossed its own tier at nearly the same moment and
        # claimed this exact coin first - same DB-level unique index that
        # protects the coin-switch path (see _ensure_product_id_unique_index).
        # The parent's own allocated_usd/next_unlock_tier update rolled back
        # together with the failed insert (same transaction), so nothing is
        # stuck half-applied - this crossing just gets retried next cycle,
        # against whatever's unclaimed by then.
        log.info(f"[TREE] {branch.bot_name} crossed ${milestone:,.0f} but {next_product} was claimed by another branch first (race) - will retry next cycle")
        return

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

    # 10% of REALIZED PROFIT ONLY - never touches principal, never fires on
    # a loss - gets pulled out of this branch's own tracked balance and
    # walled off in the shared locked-USD ledger (see PROFIT_SKIM_PCT).
    # Deducted from new_allocated before it's saved as this branch's new
    # balance, so the skimmed amount can never be redeployed by this
    # branch OR any other - see the real_balance/locked_usd clamp in
    # run_branch_cycle's buy path.
    skim = round(pnl * PROFIT_SKIM_PCT, 2) if pnl > 0 else 0.0
    if skim > 0:
        new_allocated -= skim

    await _clear_branch_position(bot_name)

    # Every exit - a profitable TARGET HIT, a STOP HIT, or a floor-breach
    # forced exit - now looks for a new coin to move to rather than
    # automatically re-buying the same one just traded. Looked up before
    # the update transaction below opens, using the DB's still-current
    # claimed set (this branch's own coin included), so it can never
    # "switch" to itself.
    #
    # The root (BTC) is the one exception: it's the permanent foundation
    # the whole tree grows out of, not a branch that wanders - per the
    # account owner, it always stays on BTC-USD regardless of how it exits.
    new_product_id = new_product_atr = None
    if bot_name != ROOT_BOT_NAME:
        new_product_id, new_product_atr = await find_most_volatile_unclaimed_coin(session)

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

    # The coin switch commits separately, in its own transaction, so a
    # conflict here (see below) can never roll back the balance/floor
    # update above - that part is correct and final either way.
    if row is not None and new_product_id:
        old_product_id = row.product_id
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(select(CryptoTreeBranch).where(CryptoTreeBranch.bot_name == bot_name))
                fresh = result.scalar_one_or_none()
                if fresh:
                    fresh.product_id = new_product_id
                    await db.commit()
            row.product_id = new_product_id
            log.info(f"[TREE] 🔀 {bot_name} switching {old_product_id} -> {new_product_id} (ATR {new_product_atr*100:.2f}%) after {reason}")
        except IntegrityError:
            # Another branch's thread claimed this exact coin first, in the
            # gap between find_most_volatile_unclaimed_coin's read and this
            # write - only the DB-level unique index (see
            # _ensure_product_id_unique_index) actually catches this; a
            # Python-only check can't, since both branches' reads can pass
            # before either write lands. Stay on the old coin for this
            # cycle rather than retrying inline - the next cycle re-runs
            # the whole search fresh, against whatever's unclaimed by then.
            log.warning(f"[TREE] {bot_name}: {new_product_id} was claimed by another branch first (race) - staying on {old_product_id} this cycle")
    elif row is not None and bot_name == ROOT_BOT_NAME:
        log.info(f"[TREE] {bot_name}: root stays on {row.product_id} by design (the tree's permanent foundation)")
    elif row is not None:
        log.info(f"[TREE] {bot_name}: no unclaimed coin available to switch to - staying on {row.product_id}")

    log.info(
        f"[TREE] {bot_name} SOLD {filled_qty:.8f} {product_id} @ ${filled_price:,.2f} ({reason}) | "
        f"entry ${position.entry_price:,.2f} -> exit ${filled_price:,.2f} | "
        f"P&L: {'+' if pnl >= 0 else ''}${pnl:.2f} after est. fees | branch now ${new_allocated:.2f}"
    )
    if skim > 0:
        await _add_locked_usd(skim)
        log.info(f"[TREE] 🔒 {bot_name} locked away ${skim:.2f} (10% of this trade's ${pnl:.2f} profit) - permanently out of the compounding loop")
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
                # Per the account owner, after real evidence of this
                # exact loop happening live (crypto_tree_dot_usd: AAVE ->
                # STOP HIT -> instant rebuy into XRP -> breached again ->
                # instant rebuy into BONK -> breached again, three real
                # losses in a row): _branch_sell_and_settle's reset floor
                # sits only ~3-4% below the fresh balance, so instantly
                # rebuying right back into a new coin used to gamble that
                # thin cushion against ordinary price noise almost
                # immediately. Record the breach and stop here instead of
                # recursing into an instant rebuy - _floor_breach_cooldown_active()
                # blocks this branch from entering anything for a real
                # cooldown window, checked below.
                await _record_floor_breach(bot_name)
                return True
            else:
                log.info(f"[TREE] 🛑 {bot_name}: ${equity:.2f} below floor ${branch.equity_floor:,.2f} - entries paused until it recovers")
            return True

        if position is None:
            if await _floor_breach_cooldown_active(bot_name):
                log.info(f"[TREE] {bot_name}: cooling down after a recent floor breach - entries paused a bit longer")
                return True
            if real_balance is None:
                log.warning(f"[TREE] {bot_name}: real balance unavailable ({real_balance_err}) - skipping this cycle")
                return True
            # Locked/skimmed profit (see PROFIT_SKIM_PCT) is walled off from
            # the real balance here so it can never be redeployed by this
            # branch or any other - this is what actually makes "locked
            # away" real, since every branch shares one real Coinbase pool.
            locked_usd = await get_locked_usd()
            spendable_balance = max(0.0, real_balance - locked_usd)
            spend = min(branch.allocated_usd, spendable_balance)
            if spend < MIN_TRADE_USD:
                log.info(f"[TREE] {bot_name}: allocated ${branch.allocated_usd:.2f} below minimum trade ${MIN_TRADE_USD:.2f} - waiting")
                return True
            if price is None:
                log.warning(f"[TREE] {bot_name}: could not fetch price/volatility - skipping this cycle")
                return True

            target_pct = max(engine.pick_target_pct(atr_pct), engine.min_profit_target_pct(spend, atr_pct))
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
                f"ATR {atr_pct*100:.2f}% -> target +{target_pct*100:.2f}% (${target_price:,.2f}, min ${engine.pick_min_profit_usd(atr_pct):.2f} net) | "
                f"stop -{STOP_LOSS_PCT*100:.2f}% (${stop_price:,.2f}) | branch total ${branch.allocated_usd:.2f} | floor ${branch.equity_floor:,.2f}"
            )
            return True

        if price is None:
            log.warning(f"[TREE] {bot_name}: could not fetch current price - holding, will re-check next cycle")
            return True

        unrealized_pct = (price / position.entry_price - 1) * 100
        if price >= position.target_price or price <= position.stop_price:
            exit_reason = "TARGET HIT" if price >= position.target_price else "STOP HIT"
            await _branch_sell_and_settle(session, bot_name, branch.product_id, position, exit_reason)
            # _branch_sell_and_settle already picked the branch's next coin -
            # re-run immediately (same reasoning as the floor-breach path
            # above) so the rebuy happens in this same pass instead of
            # waiting for the next scheduled cycle.
            return await run_branch_cycle(bot_name)
        else:
            if (position.stop_price is not None and position.stop_price < position.entry_price
                    and price >= position.entry_price * (1 + BREAKEVEN_TRIGGER_PCT)):
                await _raise_branch_stop_to_breakeven(bot_name, position.entry_price)
                position.stop_price = position.entry_price
                log.info(
                    f"[TREE] 🔒 {bot_name} stop raised to breakeven ${position.entry_price:,.2f} "
                    f"(up {unrealized_pct:+.2f}%) - can no longer close below (about) even from here"
                )
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
    loop.run_until_complete(_ensure_product_id_unique_index())
    loop.run_until_complete(_lower_existing_unlock_tiers())

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

        global _last_dust_check_at
        now = time.time()
        if now - _last_dust_check_at >= DUST_CHECK_INTERVAL_SECONDS:
            _last_dust_check_at = now
            try:
                await _check_and_sweep_stranded_dust()
            except Exception as e:
                log.warning(f"[TREE] dust sweep check failed: {e}")

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

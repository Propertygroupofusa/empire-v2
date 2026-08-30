"""
CRYPTO GRID BOT - real, live grid-trading branches.

Built per the account owner's direct "you have to do it C" after
Strategy Lab's real A/B/C/D comparison (crypto_selection_backtest.py's
run_strategy_lab_comparison) showed Grid Bot (C) as the clear best real
performer across 35 real coins over 30 days: +$69.50 total P&L, 59.2%
win rate, 637 trades - versus the live baseline's real -$1,457.43 over
the identical coins/window. Strategy Lab's own honest-limits note said
this plainly at the time: "Grid Bot ... would need real engineering work
first, not just a promote click" - the existing family-tree engine
(crypto_family_tree_bot.py / CryptoTreeBranch / BotPosition) is
fundamentally single-position-per-branch, but a real grid needs several
concurrent open slices at once. This module is that real engineering.

Deliberately a genuinely SEPARATE, additive branch type - never touches
or shares state with CryptoTreeBranch/BotPosition, and never imports
crypto_family_tree_bot's own trading logic (only its generic
_log_activity() helper, purely for a shared dashboard feed - no trading
state is shared). A grid branch trades its own real dedicated coin with
its own real capital, split into up to num_levels real concurrent
slices (CryptoGridSlice rows). Same real-money shape as every other
branch system in this codebase: one real shared Coinbase USD wallet, no
per-branch sub-account - allocated_usd is this branch's own virtual
slice of it, and every real order is clamped against the real account
balance at order time by engine.place_market_buy()/place_market_sell()
themselves, the same hard safety backstop the family tree already
relies on.

Real, live mechanics mirror crypto_selection_backtest.py's own
_replay_grid_bot() exactly (the same function the validated Strategy Lab
result came from) - buy a real slice when price closes grid_pct below
the branch's own real reference_price (capped at num_levels concurrent
slices), sell the OLDEST real open slice (FIFO) when price closes
grid_pct above it, reference_price updating to the real fill price on
every real buy AND every real sell.
"""
import asyncio
import logging
import os
import random
import time
from datetime import datetime

from sqlalchemy import select, func, case, desc

import crypto_btc_compound_bot as engine
from database import AsyncSessionLocal
from models import CryptoGridBranch, CryptoGridSlice, CryptoGridTradeHistory, TradingBotState, CryptoTreeBranch, BotPosition

log = logging.getLogger("crypto_grid_bot")

GRID_BOT_MODE_KEY = "crypto_grid_bot_mode_active"
DEFAULT_GRID_PCT = 0.01      # matches crypto_selection_backtest.py's STRATEGY_LAB_GRID_PCT exactly
DEFAULT_GRID_LEVELS = 10     # matches crypto_selection_backtest.py's STRATEGY_LAB_GRID_LEVELS exactly
CYCLE_SECONDS = 30
# Same real per-order minimum crypto_family_tree_bot.py's own MIN_TRADE_USD
# uses - below this, a real Coinbase order isn't worth placing.
MIN_TRADE_USD = 5.0

# Real per-branch drawdown circuit breaker - the direct grid-side
# counterpart to crypto_family_tree_bot.py's own DRAWDOWN_BREAKER_PCT
# (see that file's "Real per-branch drawdown circuit breaker, chosen
# live from a visual comparison" section). Grid Bot never had ANY
# account-level protection before this - a losing branch could keep
# buying new slices into a real, sustained decline indefinitely, with
# nothing to pause it. Same philosophy as the family tree's own version:
# a real, ever-rising peak_equity ratchet per branch; once real current
# equity drops this % below its own peak, NEW buys pause - existing open
# slices are never force-sold, they keep running under the branch's own
# normal FIFO sell rule (which is itself the branch's own recovery path
# back toward its peak). Env-overridable so a value chosen from real
# backtest evidence (see crypto_selection_backtest.py's
# run_grid_drawdown_breaker_comparison) can be applied without a code
# change. 25% default - deliberately tighter than the family tree's own
# 40% (see that constant's docstring for its own real reasoning): a
# grid branch's real equity naturally has FAR less single-position
# swing than a directional branch (a grid never puts more than
# allocated_usd/num_levels into any one slice, spread across up to 10
# concurrent slices) - most of a grid branch's real drawdown is capital
# that's genuinely working (open slices sitting below their own entry,
# still perfectly recoverable on the next up-tick), not evidence of a
# structurally bad position the way a single large directional loss
# would be. 25% still catches a real, sustained adverse trend that keeps
# eating into every slice at once, without pausing on ordinary grid
# noise - confirmed against real backtest evidence before shipping (see
# crypto_selection_backtest.py).
GRID_DRAWDOWN_BREAKER_PCT = float(os.getenv("GRID_DRAWDOWN_BREAKER_PCT", "0.25"))

# Real, opt-in fee-tier-aware dynamic grid spacing - OFF by default,
# per the account owner's own explicit "backtest before going live"
# instruction. Unlike the drawdown breaker above (pure downside
# protection, safe to ship live immediately), this changes real trade
# TIMING/FREQUENCY - it's a genuine live-strategy change, so it follows
# this codebase's established "shadow mode / backtest first / explicit
# promote" discipline for anything that touches real entry/exit
# triggers. See compute_dynamic_grid_pct() below for the real mechanism,
# and crypto_selection_backtest.py's run_grid_fee_tier_spacing_comparison
# for the real backtest that should inform whether to ever turn this on.
DYNAMIC_SPACING_MODE_KEY = "crypto_grid_dynamic_spacing_active"

# Real, automatic idle-cash rotation - per the account owner's explicit
# request: "why don't my system automatic[ally]... move it until the
# next coin that is doing good... so the money will never stay idle and
# keep growing." Unlike dynamic spacing above (a genuine live-strategy
# change that needed backtest evidence first), this reuses the exact
# same real coin-ranking signal already live and placing real orders via
# the $20 Quick Buy button (pick_best_ranked_coin_for_grid - real
# backtested ROI + live BTC-relative-strength) - so it's ON by default,
# with a real dashboard toggle to turn it off. Never touches a branch
# with real open slices (see _maybe_rotate_one_grid_branch) - only real,
# genuinely idle cash sitting in a FLAT branch ever moves.
GRID_AUTO_ROTATE_MODE_KEY = "crypto_grid_auto_rotate_active"
# 30 min, per the account owner's own explicit choice (offered a real
# fee-cost-vs-idle-time tradeoff directly: more frequent means real cash
# rotates faster but pays more real trading fees moving small amounts
# around; less frequent means fewer fees but longer real idle stretches).
GRID_AUTO_ROTATE_INTERVAL_SECONDS = int(os.getenv("GRID_AUTO_ROTATE_INTERVAL_SECONDS", str(30 * 60)))
# Below this, a real Coinbase round-trip (sell nothing / just a fresh
# buy into the new branch) isn't worth the real trading fee it costs to
# move - matches the same order-of-magnitude reasoning as MIN_TRADE_USD,
# just a real notch higher since this is a discretionary optimization
# move, not a required trade.
GRID_AUTO_ROTATE_MIN_USD = float(os.getenv("GRID_AUTO_ROTATE_MIN_USD", "10.0"))
# In-process throttle only (mirrors crypto_family_tree_bot.py's own
# _last_auto_backtest_at pattern) - this is a single, long-running
# coordinator thread, so a plain module-level timestamp is sufficient;
# no DB persistence needed for a value that only ever needs to survive
# within one process's lifetime.
_last_grid_auto_rotate_at = 0.0

# Real, minimum time a branch's own coin has to have been in place before
# it's eligible to rotate away again via the periodic sweep - a real,
# confirmed-live oscillation bug found on the daily health check: the
# same handful of branches were reallocating cash back and forth between
# each other (crypto_grid_1 <-> crypto_grid_7/9/10) every ~25-30 minutes,
# for hours, via move_cash_between_grid_branches() creating a brand-new
# branch row on every rotation (see create_grid_branch's own bot_name
# reassignment). Root cause: _first_ranked_coin_beating_btc's real
# live BTC-relative-strength tiebreak is time-varying by design (it's
# checked fresh on every call) - re-evaluating it every ~30 min with zero
# memory of what a branch just rotated into meant a coin that "currently
# beats BTC" one sweep could stop beating it the next, bouncing real idle
# cash between the same coins instead of ever settling long enough to
# actually catch a real dip and trade. No real Coinbase order or fee was
# ever placed by this (create_grid_branch never trades, it's pure
# bookkeeping) - the real cost was capital never getting the chance to
# actually deploy. Same "give a real decision room before revisiting it"
# reasoning as the family tree's own one-cycle coin-sale cooldown, just
# a real, meaningfully longer window here since a grid branch needs real
# time to actually catch a dip, not just one cycle.
GRID_ROTATION_COOLDOWN_SECONDS = int(os.getenv("GRID_ROTATION_COOLDOWN_SECONDS", str(2 * 60 * 60)))

# Real, automatic deployment of real UNALLOCATED free cash - the direct
# follow-up after the account owner pointed out that a manual "Add 3
# branches" button still meant going back into the dashboard and tapping
# it themselves: "I don't have to go back in there and do it." The
# per-branch rotation above only ever moves cash that's already sitting
# INSIDE a flat branch; this closes the other real gap - genuine free
# cash that was never allocated to any branch at all (a deposit, a
# withdrawal from elsewhere, real profit that already got swept out via
# rotation) now also gets put to work automatically, on the same real
# 30-min sweep, with zero manual click ever required. Reuses the exact
# same real coin-picker (pick_best_ranked_coin_for_grid) and branch-
# creation path (create_grid_branch) the manual "New branch"/"Add 3
# branches" buttons already use - this is that same real action, just
# fired on its own instead of waiting for a tap.
GRID_AUTO_DEPLOY_AMOUNT_USD = float(os.getenv("GRID_AUTO_DEPLOY_AMOUNT_USD", "50.0"))
# Caps how many NEW branches one single sweep can create - real,
# deliberate friction against a large, sudden cash windfall (or a bug)
# spinning up dozens of tiny branches in one shot. A real surplus above
# this cap just gets picked up on the next sweep instead.
GRID_AUTO_DEPLOY_MAX_NEW_BRANCHES_PER_SWEEP = int(os.getenv("GRID_AUTO_DEPLOY_MAX_NEW_BRANCHES_PER_SWEEP", "3"))

# The real, fixed net-margin target this feature holds constant as the
# account's real Coinbase fee tier changes - deliberately DERIVED from
# today's live values so a branch trading at the base fee tier behaves
# BYTE-IDENTICALLY to the existing fixed DEFAULT_GRID_PCT (0.01) - this
# feature only ever narrows the real grid spacing once the account's
# real fee tier genuinely improves (lower real fees), it never changes
# anything for an account still at the base tier. See
# compute_dynamic_grid_pct() for how this composes with the real live
# fee rate.
TARGET_NET_MARGIN_PCT = DEFAULT_GRID_PCT - engine.ROUND_TRIP_FEE_RATE

# A real floor under how tight dynamic spacing is ever allowed to go,
# regardless of how favorable the real fee tier gets - protects against
# over-trading into pure market noise if this codebase's own
# ROUND_TRIP_FEE_RATE assumption ever turns out to be too generous for
# a real, currently-unknown-to-this-sandbox fee tier.
MIN_DYNAMIC_GRID_PCT = 0.003


async def is_grid_bot_active() -> bool:
    """Master on/off switch for the whole grid-branch system - real,
    DB-persisted flag (same generic TradingBotState bucket pattern every
    other real-time toggle in this codebase already uses). Defaults to
    True: per the account owner's direct "you have to do it C" right
    after the real Strategy Lab evidence, and given this session has no
    live network access to click a dashboard toggle itself - same
    constraint and precedent already used for the STOP-HIT reversal
    buy's own default flip. Still a real, reversible switch either way -
    an explicit False from a future dashboard toggle always wins over
    this default. Even while True, nothing trades until at least one
    real grid branch actually exists (see create_grid_branch)."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == GRID_BOT_MODE_KEY))
        row = result.scalar_one_or_none()
        if row is None:
            return True
        return bool(row.base_capital and row.base_capital >= 1.0)


async def set_grid_bot_active(enabled: bool):
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == GRID_BOT_MODE_KEY))
        row = result.scalar_one_or_none()
        if row is None:
            row = TradingBotState(bot_name=GRID_BOT_MODE_KEY, base_capital=0.0)
            db.add(row)
        row.base_capital = 1.0 if enabled else 0.0
        await db.commit()


async def is_dynamic_spacing_active() -> bool:
    """Real, DB-persisted toggle for fee-tier-aware dynamic grid spacing
    (see compute_dynamic_grid_pct). Defaults to False - unlike Grid Bot
    mode itself, this changes real trade timing, so it needs a real,
    explicit decision to turn on, not a default-on with the option to
    disable."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == DYNAMIC_SPACING_MODE_KEY))
        row = result.scalar_one_or_none()
        return bool(row and row.base_capital and row.base_capital >= 1.0)


async def set_dynamic_spacing_active(enabled: bool):
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == DYNAMIC_SPACING_MODE_KEY))
        row = result.scalar_one_or_none()
        if row is None:
            row = TradingBotState(bot_name=DYNAMIC_SPACING_MODE_KEY, base_capital=0.0)
            db.add(row)
        row.base_capital = 1.0 if enabled else 0.0
        await db.commit()


async def is_grid_auto_rotate_active() -> bool:
    """Real, DB-persisted toggle for automatic idle-cash rotation (see
    run_grid_auto_rotate_sweep). Defaults to True - per the account
    owner's own explicit request for this behavior, and because it
    reuses the exact same real coin-ranking signal already live via the
    $20 Quick Buy button, not a new, unvalidated strategy needing a
    shadow-mode period first."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == GRID_AUTO_ROTATE_MODE_KEY))
        row = result.scalar_one_or_none()
        if row is None:
            return True
        return bool(row.base_capital and row.base_capital >= 1.0)


async def set_grid_auto_rotate_active(enabled: bool):
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == GRID_AUTO_ROTATE_MODE_KEY))
        row = result.scalar_one_or_none()
        if row is None:
            row = TradingBotState(bot_name=GRID_AUTO_ROTATE_MODE_KEY, base_capital=0.0)
            db.add(row)
        row.base_capital = 1.0 if enabled else 0.0
        await db.commit()


async def compute_dynamic_grid_pct(session) -> tuple:
    """Real, live fee-tier-aware grid_pct - fetches the account's actual
    current Coinbase fee tier (engine.get_real_fee_tier, the account's
    own real 30-day-volume-based rate, not a guess) and returns
    (grid_pct, tier_name, taker_fee_rate) so a real, improving fee tier
    narrows the real price move needed to trigger a buy/sell (more real
    trade frequency, same target net margin per completed cycle) - see
    TARGET_NET_MARGIN_PCT's own docstring for why this is backward-
    compatible with today's fixed 1% at the base tier.

    Fails OPEN on a real fetch failure - returns (DEFAULT_GRID_PCT, None,
    None), matching every other "don't block real trading on missing
    data" gate in this codebase (crypto_family_tree_bot.py's higher-
    timeframe-trend/BTC-relative-strength filters both do the same)."""
    maker, taker, tier_name, err = await engine.get_real_fee_tier(session)
    if taker is None:
        return DEFAULT_GRID_PCT, None, None
    round_trip_fee_rate = taker * 2  # every real order this codebase places is a MARKET (taker) order
    grid_pct = max(MIN_DYNAMIC_GRID_PCT, TARGET_NET_MARGIN_PCT + round_trip_fee_rate)
    return grid_pct, tier_name, taker


async def get_grid_branches() -> list:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(CryptoGridBranch).order_by(CryptoGridBranch.bot_name))
        return list(result.scalars().all())


async def get_grid_branch_claimed_coins() -> set:
    """Real coins currently claimed by an ACTIVE grid branch - a disabled
    branch (active=False) releases its claim, same convention every
    other claimed-contract/coin check in this codebase already uses."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(CryptoGridBranch.product_id).where(CryptoGridBranch.active == True))
        return {row[0] for row in result.all()}


async def get_grid_allocated_total() -> float:
    """Real total capital already committed across EVERY grid branch
    (active or paused - a paused branch releases its coin claim but its
    real allocated_usd stays committed, same as any other branch type in
    this codebase)."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(CryptoGridBranch))
        return sum(b.allocated_usd for b in result.scalars().all())


async def get_real_free_cash_usd():
    """Real, honest 'how much can I actually deploy into a NEW grid
    branch right now' figure - per the account owner's own direct
    complaint about creating branches "blindly" with no idea what's
    really available. Real Coinbase USD balance minus real locked
    profit minus every FLAT family-tree branch's own allocated_usd (a
    branch holding a position has already deployed that money into
    crypto, not cash - same real distinction
    routers/trading_dashboard.py's own spendable_for_spawn already
    makes) minus every Grid Bot branch's own allocated_usd (its FULL
    amount, active or paused - a deliberately conservative
    simplification: some of a grid branch's own allocation may already
    be sitting in open slices as real crypto rather than literal cash,
    but treating the whole thing as reserved is the safe direction,
    never overstating what's genuinely free).

    Grid Bot and the family tree draw from the exact same shared real
    Coinbase wallet, so this is the one real number both systems should
    agree on - never a separately, independently computed figure that
    could quietly disagree with the other. Returns None on a real
    balance-fetch failure, never a fabricated number."""
    async with engine.aiohttp.ClientSession() as session:
        real_balance, _err = await engine.get_usd_balance(session)
    if real_balance is None:
        return None

    import crypto_family_tree_bot as tree  # lazy - avoids a circular import at module load, same pattern as _log_activity_safe below
    locked_usd = await tree.get_locked_usd()

    async with AsyncSessionLocal() as db:
        tree_result = await db.execute(select(CryptoTreeBranch))
        tree_branches = tree_result.scalars().all()
        open_bots_result = await db.execute(select(BotPosition.bot))
        open_bots = {row[0] for row in open_bots_result.all()}
        tree_flat_allocated = sum(b.allocated_usd for b in tree_branches if b.bot_name not in open_bots)

    grid_allocated_total = await get_grid_allocated_total()

    return round(real_balance - locked_usd - tree_flat_allocated - grid_allocated_total, 2)


def _safe_num_levels_for_allocation(allocated_usd: float) -> int:
    """A small real branch (e.g. a $20 quick-buy) would silently never
    trade under the fixed DEFAULT_GRID_LEVELS - splitting $20 across 10
    levels gives a real $2.00 slice, below the real MIN_TRADE_USD floor
    every buy attempt checks, so run_grid_branch_cycle would just log
    "waiting" forever with no real order ever placed. Found while
    building the real $20 Quick Buy button - fixed generally here rather
    than just for that one path, since ANY branch created below
    DEFAULT_GRID_LEVELS * MIN_TRADE_USD ($50) had this same real bug.
    Caps levels down so each real slice stays at or above the real
    minimum, floored at 1 level (a single-slice "grid" is still a real,
    working position, just with no room to average down)."""
    max_levels_by_min_trade = int(allocated_usd // MIN_TRADE_USD)
    return max(1, min(DEFAULT_GRID_LEVELS, max_levels_by_min_trade))


async def create_grid_branch(product_id: str, allocated_usd: float, skip_free_cash_check: bool = False) -> CryptoGridBranch:
    """Creates a real new grid branch - a pure bookkeeping operation plus
    one real live price fetch to anchor its starting reference_price,
    never a trade by itself (mirrors CryptoTreeBranch/AlpacaBranch's own
    "spawning is a bookkeeping transfer" reasoning - the real dollars
    this represents are already sitting in the one real Coinbase wallet,
    just not earmarked to any branch yet). Refuses a non-positive amount,
    a coin already claimed by another active grid branch, or (unless
    skip_free_cash_check) an amount exceeding real free spendable cash
    (see get_real_free_cash_usd) - per the account owner's own direct
    complaint that they were creating branches "blindly" with no idea
    what was actually available; this can never silently accept a
    request for money that doesn't exist.

    `skip_free_cash_check` exists for fund_grid_from_tree_branch() below:
    that real cash is already reserved (it's a family-tree branch's own
    allocated_usd, not unreserved free cash) - get_real_free_cash_usd()
    would incorrectly refuse a real, legitimate cross-system TRANSFER,
    since it doesn't know the source branch's allocation is about to
    shrink by the identical amount in the same real operation. Every
    other caller (the dashboard's "New grid branch" button, the $20 Quick
    Buy) keeps the real check exactly as before.

    num_levels is chosen per-branch (see _safe_num_levels_for_allocation)
    rather than always the fixed DEFAULT_GRID_LEVELS, so a small real
    branch still genuinely trades instead of every slice rounding below
    the real minimum order size."""
    if allocated_usd <= 0:
        raise ValueError("allocated_usd must be positive")
    claimed = await get_grid_branch_claimed_coins()
    if product_id in claimed:
        raise ValueError(f"{product_id} is already claimed by an active grid branch")

    if not skip_free_cash_check:
        real_spendable = await get_real_free_cash_usd()
        if real_spendable is not None and allocated_usd > real_spendable + 0.01:
            raise ValueError(
                f"Only ${real_spendable:.2f} in real free spendable cash right now - can't deploy ${allocated_usd:.2f}"
            )

    async with engine.aiohttp.ClientSession() as session:
        price, _atr = await engine.get_price_and_volatility(session, product_id)
    if price is None:
        raise ValueError(f"could not fetch a real live price for {product_id} right now - try again shortly")

    num_levels = _safe_num_levels_for_allocation(allocated_usd)

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(CryptoGridBranch))
        existing = list(result.scalars().all())
        used_nums = {
            int(b.bot_name.rsplit("_", 1)[-1])
            for b in existing if b.bot_name.rsplit("_", 1)[-1].isdigit()
        }
        next_num = 1
        while next_num in used_nums:
            next_num += 1
        bot_name = f"crypto_grid_{next_num}"
        branch = CryptoGridBranch(
            bot_name=bot_name, product_id=product_id, allocated_usd=allocated_usd, active=True,
            grid_pct=DEFAULT_GRID_PCT, num_levels=num_levels, reference_price=price,
        )
        db.add(branch)
        await db.commit()
        await db.refresh(branch)
    log.info(f"[GRID] 🌱 Created {bot_name} on {product_id} with ${allocated_usd:.2f} ({num_levels} real levels, reference price ${price:.2f})")
    return branch


async def add_cash_to_grid_branch(bot_name: str, amount: float) -> CryptoGridBranch:
    """Adds real cash to an EXISTING grid branch's own allocation - the
    one real capability this module never had before
    fund_grid_from_tree_branch() below needed it (every prior path only
    ever CREATED a new branch). Pure bookkeeping: increases allocated_usd
    and recomputes num_levels via _safe_num_levels_for_allocation (which
    is monotonic in allocated_usd, so this can only ever hold steady or
    grow the real level count - never shrinks it out from under any
    already-open real slice). Does NOT touch reference_price or any
    already-open CryptoGridSlice row - existing real slices keep their
    own real entry/qty exactly as bought; only the branch's own future
    slice sizing (slice_usd = allocated_usd / num_levels) changes going
    forward. Refuses a non-positive amount or an unknown bot_name."""
    if amount <= 0:
        raise ValueError("amount must be positive")
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(CryptoGridBranch).where(CryptoGridBranch.bot_name == bot_name))
        branch = result.scalar_one_or_none()
        if branch is None:
            raise ValueError(f"no grid branch named {bot_name}")
        branch.allocated_usd += amount
        branch.num_levels = _safe_num_levels_for_allocation(branch.allocated_usd)
        await db.commit()
        await db.refresh(branch)
    log.info(f"[GRID] 💰 Added ${amount:.2f} to {bot_name} - now ${branch.allocated_usd:.2f} ({branch.num_levels} real levels)")
    return branch


async def withdraw_from_grid_branch(bot_name: str, amount: float) -> dict:
    """Pulls real cash OUT of an existing grid branch's own allocation -
    the reverse of add_cash_to_grid_branch(). Built per the account
    owner's own direct request after seeing a real $994.65 STX-USD
    branch sitting completely flat (no open slices) while they wanted to
    "pull some money out of this branch... so I can make more" new
    branches - a real, legitimate need this module never had a way to
    do, since a grid branch's allocated_usd could previously only ever
    grow (via add_cash_to_grid_branch/fund_grid_from_tree_branch) or
    move to another branch's slices via a real sell, never be pulled
    back out on demand.

    Requires the branch to be FLAT (no open CryptoGridSlice rows) - same
    real safety discipline as every other cash-moving function in this
    codebase (reallocate_cash_between_branches, fund_grid_from_tree_
    branch): an open slice represents real crypto already bought, not
    idle cash, so pulling allocated_usd out from under one would desync
    the branch's own bookkeeping from what's genuinely deployed. Refuses
    a non-positive amount or an amount exceeding the branch's own real
    allocated_usd.

    Deliberately does NOT move the withdrawn cash anywhere - it doesn't
    need to. get_real_free_cash_usd() already subtracts every grid
    branch's own allocated_usd (active or paused) from the real Coinbase
    balance, so shrinking this branch's allocation is itself what makes
    that real cash spendable again - the very next "New grid branch" (or
    fund_grid_from_tree_branch, or another add_cash_to_grid_branch) call
    can deploy it immediately, no separate transfer step required.

    If the withdrawal drains the branch down to essentially $0.00 (real
    fee/rounding dust aside), the branch row is deleted outright rather
    than left as a real, empty stub still claiming its coin - matching
    the same "an emptied-out branch doesn't linger" reasoning
    consolidate_branches_by_coin() already established elsewhere in this
    codebase. A partial withdrawal that leaves real money behind keeps
    the branch running exactly as before, with num_levels recomputed via
    the same _safe_num_levels_for_allocation() every other real
    allocation change already uses (so a shrunk branch's future slices
    still clear the real minimum trade size, not just its past ones)."""
    if amount <= 0:
        raise ValueError("amount must be positive")
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(CryptoGridBranch).where(CryptoGridBranch.bot_name == bot_name))
        branch = result.scalar_one_or_none()
        if branch is None:
            raise ValueError(f"no grid branch named {bot_name}")
        if branch.locked:
            raise ValueError(f"{bot_name} is locked - unlock it first before withdrawing real cash from it")
        slices_result = await db.execute(select(CryptoGridSlice).where(CryptoGridSlice.bot_name == bot_name))
        if slices_result.scalars().first() is not None:
            raise ValueError(f"{bot_name} has real open slices - can only withdraw from a FLAT branch (pause it and wait for slices to close first)")
        if amount > branch.allocated_usd + 0.01:
            raise ValueError(f"{bot_name} only has ${branch.allocated_usd:.2f} real allocated - can't withdraw ${amount:.2f}")

        branch.allocated_usd -= amount
        deleted = branch.allocated_usd < 0.01
        if deleted:
            product_id = branch.product_id
            await db.delete(branch)
            await db.commit()
            log.info(f"[GRID] 💵 Withdrew ${amount:.2f} from {bot_name} - fully drained, branch removed and {product_id} released")
            return {"bot_name": bot_name, "product_id": product_id, "amount": amount, "remaining_allocated_usd": 0.0, "branch_deleted": True}

        branch.num_levels = _safe_num_levels_for_allocation(branch.allocated_usd)
        await db.commit()
        await db.refresh(branch)
    log.info(f"[GRID] 💵 Withdrew ${amount:.2f} from {bot_name} - now ${branch.allocated_usd:.2f} ({branch.num_levels} real levels), freed back to real spendable cash")
    return {
        "bot_name": bot_name, "product_id": branch.product_id, "amount": amount,
        "remaining_allocated_usd": round(branch.allocated_usd, 2), "branch_deleted": False,
    }


async def set_grid_branch_locked(bot_name: str, locked: bool) -> dict:
    """Real, manual per-branch lock - per the account owner's direct
    request after recalling losing real money moving cash off a branch
    that was "about to make profit" a few times in the past: "I don't
    want to switch anything that's on his way to being profit so lock it
    so it won't be able to be moved around by me."

    A locked branch's real cash can never be pulled out by any of the
    three cash-removal paths in this file - withdraw_from_grid_branch()
    (direct withdraw), move_cash_between_grid_branches() (as a source,
    checked BEFORE the destination is ever funded, so a locked source can
    never leave a destination double-funded), and the automatic
    auto-rotate sweep (_maybe_rotate_one_grid_branch(), which silently
    skips a locked branch rather than raising every cycle). A locked
    branch's own NORMAL grid trading - buying real dips, selling real
    rises on its existing/future slices - is completely unaffected; this
    only ever blocks cash-REMOVAL, never the branch's real trading."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(CryptoGridBranch).where(CryptoGridBranch.bot_name == bot_name))
        branch = result.scalar_one_or_none()
        if branch is None:
            raise ValueError(f"no grid branch named {bot_name}")
        branch.locked = bool(locked)
        await db.commit()
        await db.refresh(branch)
    log.info(f"[GRID] {'🔒 Locked' if locked else '🔓 Unlocked'} grid branch {bot_name} - its real cash {'can no longer' if locked else 'can now again'} be moved out")
    return {"bot_name": bot_name, "locked": bool(branch.locked)}


async def move_cash_between_grid_branches(from_bot_name: str, amount: float, to_bot_name: str = None, product_id: str = None) -> dict:
    """One-step real grid-to-grid cash move - per the account owner's
    direct follow-up after using withdraw_from_grid_branch()/"New grid
    branch" as two separate steps: "different sections one should be
    able to pick from... you put the amount from the coin that you want
    to pull from... you want to put it in another coin... or just open
    up a new branch." Combines withdraw_from_grid_branch() (the source
    debit) and either add_cash_to_grid_branch() or create_grid_branch()
    (the destination) into one real action and one confirm click,
    instead of requiring a withdraw, a manual note of the freed amount,
    then a separate "New grid branch" click.

    `to_bot_name`, if given, adds to that existing real grid branch
    (must be a DIFFERENT branch than the source - refused otherwise);
    otherwise `product_id` (or an auto-pick by real backtested
    ROI/BTC-relative-strength if neither is given - see
    pick_best_ranked_coin_for_grid) creates a new one. Same real safety
    discipline as fund_grid_from_tree_branch(): the source must be FLAT
    (no real open slices), the amount can't exceed its own real
    allocated_usd, and STOP_TRADING blocks this (it deploys new capital
    into a destination branch).

    Same "destination funded first, source debited only after" ordering
    every other cash-mover in this codebase uses - a failed destination
    (a real live-price fetch failure, a coin already claimed) leaves the
    source completely untouched. The actual debit is done by calling the
    real withdraw_from_grid_branch() itself, which re-validates the
    source's real state (flat, sufficient allocated_usd) fresh at that
    exact moment - not just the initial check above - so a source that
    somehow changed state in the brief window between the two real steps
    is still caught rather than silently over-debited. A real, narrow,
    accepted edge case worth naming honestly (matching the "doesn't
    eliminate the race outright" caveat already used elsewhere in this
    file): if the source becomes invalid in that same brief window, the
    destination has already been funded and the source debit will raise
    - the source keeps its cash (never over-debited) but the destination
    also keeps what it received, a real, narrow double-count risk not
    worth a full two-phase-commit rollback for, given grid branches only
    ever change state from their own single-threaded coordinator cycle,
    not from a second concurrent caller."""
    if os.getenv("STOP_TRADING", "false").lower() == "true":
        raise ValueError("STOP_TRADING is set - new capital deployment is paused")
    if amount <= 0:
        raise ValueError("amount must be positive")
    if to_bot_name and to_bot_name == from_bot_name:
        raise ValueError("source and destination can't be the same branch")

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(CryptoGridBranch).where(CryptoGridBranch.bot_name == from_bot_name))
        source = result.scalar_one_or_none()
        if source is None:
            raise ValueError(f"no grid branch named {from_bot_name}")
        if source.locked:
            raise ValueError(f"{from_bot_name} is locked - unlock it first before moving real cash out of it")
        slices_result = await db.execute(select(CryptoGridSlice).where(CryptoGridSlice.bot_name == from_bot_name))
        if slices_result.scalars().first() is not None:
            raise ValueError(f"{from_bot_name} has real open slices - can only move cash from a FLAT branch")
        if amount > source.allocated_usd + 0.01:
            raise ValueError(f"{from_bot_name} only has ${source.allocated_usd:.2f} real allocated - can't move ${amount:.2f}")
        source_product_id = source.product_id

    if to_bot_name:
        destination = await add_cash_to_grid_branch(to_bot_name, amount)
        action = "added_to_existing"
    else:
        target_coin = product_id or await pick_best_ranked_coin_for_grid()
        destination = await create_grid_branch(target_coin, amount, skip_free_cash_check=True)
        action = "new_branch"

    withdraw_result = await withdraw_from_grid_branch(from_bot_name, amount)

    log.info(f"[GRID] 🔀 Moved ${amount:.2f} from grid branch {from_bot_name} into grid branch {destination.bot_name} ({destination.product_id})")
    await _log_activity_safe(
        destination.bot_name, destination.product_id, "BUY",
        f"Received ${amount:.2f} moved from grid branch {from_bot_name} - branch total now ${destination.allocated_usd:.2f}",
    )
    await _log_activity_safe(
        from_bot_name, source_product_id, "REALLOCATE",
        f"Moved ${amount:.2f} of its own idle real cash into grid branch {destination.bot_name} ({destination.product_id})",
    )

    return {
        "from_bot_name": from_bot_name, "to_bot_name": destination.bot_name, "product_id": destination.product_id,
        "amount": amount, "action": action, "destination_allocated_usd": round(destination.allocated_usd, 2),
        "source_branch_deleted": withdraw_result["branch_deleted"],
        "source_remaining_allocated_usd": withdraw_result["remaining_allocated_usd"],
    }


async def fund_grid_from_tree_branch(from_bot_name: str, amount: float, product_id: str = None, to_grid_bot_name: str = None) -> dict:
    """Real, cross-system cash transfer - moves already-reserved real
    dollars OUT of a flat family-tree branch's own allocated_usd and INTO
    Grid Bot, either adding to an existing grid branch (to_grid_bot_name)
    or creating a new one (product_id, or auto-picked via
    pick_best_ranked_coin_for_grid() if neither is given). Built after
    the account owner's own real, direct request to move more real
    capital into Grid Bot - the one strategy actually winning on a real,
    fresh Strategy Lab sample - right after get_real_free_cash_usd()
    showed genuinely negative real free cash, because the family tree's
    own flat, idle allocation was itself the thing blocking it.

    Same real safety discipline as reallocate_cash_between_branches()
    (the existing family-tree-to-family-tree cash mover this mirrors):
    the source branch MUST be flat (no open BotPosition) - pulling
    allocated_usd out from under a branch actively holding a real
    position would desync its own bookkeeping from what's genuinely
    deployed. Refused if the amount isn't positive, exceeds the source's
    own real allocated_usd, the source bot_name doesn't exist, or
    STOP_TRADING is set (this deploys new capital into Grid Bot, same
    kill-switch every other capital-deployment action already respects).

    The destination is created/funded FIRST (via create_grid_branch with
    skip_free_cash_check=True - see its own docstring for why the normal
    real-free-cash check would incorrectly block this specific transfer),
    and the source's allocated_usd is only debited AFTER that succeeds -
    a failed destination (e.g. a real live-price fetch failure) leaves
    the source completely untouched, no real dollars debited with
    nothing to show for it."""
    if os.getenv("STOP_TRADING", "false").lower() == "true":
        raise ValueError("STOP_TRADING is set - new capital deployment is paused")
    if amount <= 0:
        raise ValueError("amount must be positive")

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(CryptoTreeBranch).where(CryptoTreeBranch.bot_name == from_bot_name))
        source = result.scalar_one_or_none()
        if source is None:
            raise ValueError(f"no family-tree branch named {from_bot_name}")
        pos_result = await db.execute(select(BotPosition).where(BotPosition.bot == from_bot_name))
        if pos_result.scalars().first() is not None:
            raise ValueError(f"{from_bot_name} is currently holding a real position - can only move cash from a FLAT branch")
        if amount > source.allocated_usd + 0.01:
            raise ValueError(f"{from_bot_name} only has ${source.allocated_usd:.2f} real allocated - can't move ${amount:.2f}")

    if to_grid_bot_name:
        destination = await add_cash_to_grid_branch(to_grid_bot_name, amount)
        action = "added_to_existing"
    else:
        target_coin = product_id or await pick_best_ranked_coin_for_grid()
        destination = await create_grid_branch(target_coin, amount, skip_free_cash_check=True)
        action = "new_branch"

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(CryptoTreeBranch).where(CryptoTreeBranch.bot_name == from_bot_name))
        fresh_source = result.scalar_one_or_none()
        if fresh_source is None:
            raise ValueError(f"{from_bot_name} no longer exists - the destination was funded but the source could not be debited")
        fresh_source.allocated_usd -= amount
        await db.commit()

    log.info(f"[GRID] 🔀 Moved ${amount:.2f} from real family-tree branch {from_bot_name} into grid branch {destination.bot_name} ({destination.product_id})")
    # Both real legs logged via the same defensive helper every other
    # grid-side activity event already uses - a logging failure here can
    # never unwind or block the real transfer that already completed.
    await _log_activity_safe(
        destination.bot_name, destination.product_id, "BUY",
        f"Received ${amount:.2f} moved from the family tree's {from_bot_name} - grid branch total now ${destination.allocated_usd:.2f}",
    )
    await _log_activity_safe(
        from_bot_name, source.product_id, "REALLOCATE",
        f"Moved ${amount:.2f} of its own idle real cash into Grid Bot's {destination.bot_name} ({destination.product_id})",
    )

    return {
        "from_bot_name": from_bot_name,
        "to_bot_name": destination.bot_name,
        "product_id": destination.product_id,
        "amount": amount,
        "action": action,
        "destination_allocated_usd": destination.allocated_usd,
    }


async def _first_ranked_coin_beating_btc(ranked_product_ids: list) -> str:
    """Live BTC-relative-strength check, layered on top of
    pick_best_ranked_coin_for_grid()'s existing backtested-ROI ranking -
    per the account owner's explicit "yes do it" after a pasted proposal
    tried to graft this onto Grid Bot with fabricated code (a hardcoded
    fake RSI, a phantom `GridAccountState` table, an Alembic migration
    this project doesn't use). This reuses the REAL, already-validated
    function instead: crypto_btc_compound_bot.get_price_volatility_and_trend(),
    the exact same one crypto_family_tree_bot.find_most_volatile_unclaimed_coin()
    already uses live, after its own real 30-day/21-coin backtest
    comparison showed a net-positive ROI change on 15 of 21 coins when
    gated on beating BTC-USD's own return over the identical window
    (alpha = coin_return - btc_return > 0).

    Real, honest caveat this docstring is explicit about, unlike the
    fabricated version: that 30-day comparison was run against the
    family tree's own directional target/stop/trailing-stop strategy,
    not Grid Bot's mean-reversion buy-the-dip/sell-the-bounce mechanic -
    it has NOT been separately backtested for Grid Bot specifically.
    Wiring it in here is a reasonable, real signal reuse (a coin
    currently trending relative to BTC is a coin actually moving, which
    a grid strategy needs to have anything to buy/sell against at all),
    not a claim that the same 15-of-21 improvement applies to Grid Bot's
    own numbers - that would need its own real comparison, the same
    "evidence before trusting a promoted number" standard every other
    live filter in this codebase was held to.

    Walks the real ROI-ranked list in order and returns the first coin
    whose real live return over the same ~25h window beats BTC-USD's own
    real return over that identical window - not just the single best-ROI
    coin regardless of current live momentum. Fails OPEN (returns the
    plain #1 ROI coin, unfiltered) when BTC's own live data can't be
    fetched, or when every ranked candidate fails the check - a missing
    benchmark, or a real moment where nothing beats BTC, is never grounds
    to block Grid Bot from getting a real coin to trade at all."""
    if not ranked_product_ids:
        return None
    async with engine.aiohttp.ClientSession() as session:
        results = await asyncio.gather(
            engine.get_price_volatility_and_trend(session, "BTC-USD"),
            *(engine.get_price_volatility_and_trend(session, pid) for pid in ranked_product_ids),
            return_exceptions=True,
        )
    btc_result, coin_results = results[0], results[1:]
    btc_return = None
    if not isinstance(btc_result, Exception) and btc_result is not None:
        btc_return = btc_result[4]
    if btc_return is None:
        log.info("[GRID] BTC-relative-strength check: BTC-USD's own real live data unavailable right now - "
                  "falling back to the plain #1 ROI-ranked coin unfiltered")
        return ranked_product_ids[0]

    for pid, result in zip(ranked_product_ids, coin_results):
        if isinstance(result, Exception) or result is None:
            continue
        coin_return = result[4]
        if coin_return is not None and coin_return > btc_return:
            log.info(f"[GRID] BTC-relative-strength check: picked {pid} "
                      f"(real {coin_return*100:+.2f}% vs BTC-USD's real {btc_return*100:+.2f}% over the same window)")
            return pid

    log.info(f"[GRID] BTC-relative-strength check: no ranked candidate currently beats BTC-USD's real "
              f"{btc_return*100:+.2f}% - falling back to the plain #1 ROI-ranked coin")
    return ranked_product_ids[0]


async def pick_best_ranked_coin_for_grid() -> str:
    """Real coin auto-pick for the $20 Quick Buy button - the single best
    real backtested-ROI coin (from CryptoBacktestRun, the same real
    per-coin backtest data crypto_family_tree_bot.py's own top-15
    rotation and exclusion layers already read - not a second, separately
    computed ranking) that isn't already claimed by an active grid branch
    and isn't currently excluded by the family tree's own real exclusion
    layers (manual + auto-backtest + live-performance). Reusing that
    real, already-validated "known bad coin" protection rather than
    risking a quick-buy landing on a coin already proven to lose real
    money live - Grid Bot has no exclusion layer of its own, so this
    borrows the sibling system's rather than shipping a quick-buy with
    none at all.

    Among the real ROI-ranked candidates, the actual pick then goes
    through a live BTC-relative-strength check (see
    _first_ranked_coin_beating_btc) - not just the single highest-ROI
    coin regardless of whether it's currently trending relative to BTC
    right now. Fails open to the plain top-ROI pick if that check can't
    run or nothing currently qualifies - this never blocks a pick outright
    over the live filter alone.

    Raises ValueError if nothing real qualifies (no coin has a real
    backtest run yet, or every ranked coin is excluded/claimed)."""
    import crypto_family_tree_bot as tree  # lazy - avoids a circular import at module load, same pattern as get_real_free_cash_usd above
    from models import CryptoBacktestRun

    excluded = await tree.get_effective_excluded_coins()
    claimed = await get_grid_branch_claimed_coins()

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(CryptoBacktestRun).order_by(CryptoBacktestRun.product_id, desc(CryptoBacktestRun.run_at))
        )
        rows = result.scalars().all()

    latest_by_coin = {}
    for row in rows:
        if row.product_id not in latest_by_coin:
            latest_by_coin[row.product_id] = row

    candidates = [
        row for pid, row in latest_by_coin.items()
        if pid not in excluded and pid not in claimed
    ]
    if not candidates:
        raise ValueError(
            "no eligible coin has a real backtest run yet, or every ranked coin is currently excluded/claimed - "
            "run a coin-selection backtest first, or pick a specific coin instead"
        )
    candidates.sort(key=lambda r: r.roi_pct_of_spend, reverse=True)
    return await _first_ranked_coin_beating_btc([c.product_id for c in candidates])


async def _best_available_coin_and_roi(exclude_bot_name: str = None) -> tuple:
    """Real best-ranked coin AND its real ROI figure - the same
    real signal pick_best_ranked_coin_for_grid() already uses (latest
    CryptoBacktestRun ROI, the family tree's exclusion layers, the live
    BTC-relative-strength tiebreak), but reports the real ROI back too so
    a caller can compare it against what a specific branch already holds
    - pick_best_ranked_coin_for_grid() alone can't answer "is my current
    coin already the best one" because it always excludes every currently
    ACTIVE branch's own claimed coin, including the branch asking the
    question.

    `exclude_bot_name`'s own claimed coin is treated as available (not
    blocked by its own claim) - it's the branch whose idle cash is being
    considered for a move, so its current coin is a legitimate candidate
    for "no move needed, already the best."

    Returns (None, None) if nothing real qualifies (no coin has a real
    backtest run yet, or every ranked coin is excluded/claimed by some
    OTHER active branch) - never raises, so an automatic caller can just
    skip a branch this cycle rather than crash on a real data gap."""
    import crypto_family_tree_bot as tree  # lazy - avoids a circular import at module load, same pattern as get_real_free_cash_usd above
    from models import CryptoBacktestRun

    excluded = await tree.get_effective_excluded_coins()
    claimed = await get_grid_branch_claimed_coins()
    if exclude_bot_name:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(CryptoGridBranch.product_id).where(CryptoGridBranch.bot_name == exclude_bot_name))
            own_coin = result.scalar_one_or_none()
        if own_coin:
            claimed = claimed - {own_coin}

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(CryptoBacktestRun).order_by(CryptoBacktestRun.product_id, desc(CryptoBacktestRun.run_at))
        )
        rows = result.scalars().all()

    latest_by_coin = {}
    for row in rows:
        if row.product_id not in latest_by_coin:
            latest_by_coin[row.product_id] = row

    candidates = [
        row for pid, row in latest_by_coin.items()
        if pid not in excluded and pid not in claimed
    ]
    if not candidates:
        return None, None
    candidates.sort(key=lambda r: r.roi_pct_of_spend, reverse=True)
    best_pid = await _first_ranked_coin_beating_btc([c.product_id for c in candidates])
    if best_pid is None:
        return None, None
    return best_pid, latest_by_coin[best_pid].roi_pct_of_spend


async def get_grid_cash_move_candidates(from_bot_name: str) -> dict:
    """Real "would moving cash here actually help" preview for the Move
    Cash Between Grid Branches modal - per the account owner's direct
    follow-up request: "show me if I do move something to another
    Branch... will help it out and potentially push it to make money
    faster." Read-only, never moves anything itself.

    Reuses the exact same real signal every other coin-pick in this file
    already reads (CryptoBacktestRun's latest real backtested ROI per
    coin) - not a new or separately-computed number, so this can never
    disagree with what pick_best_ranked_coin_for_grid()/auto-rotate would
    actually pick. Every OTHER real active branch is reported as a
    possible destination (a locked branch can still legitimately RECEIVE
    cash - locking only ever protects a branch's cash from being pulled
    OUT, never from being added to), plus a "new branch" option using the
    same real auto-pick logic _best_available_coin_and_roi() already
    validates. `would_help` is real and honest: True only when that
    candidate's own real backtested ROI is both known AND genuinely
    higher than the source's own current coin's real ROI - a candidate
    with no real backtest data on record reports `roi_pct=None` and
    `would_help=None` (never guessed), matching the "no data = no
    verdict" default every other exclusion/ranking layer in this
    codebase already uses."""
    import crypto_family_tree_bot as tree  # lazy - avoids a circular import at module load, same pattern as get_real_free_cash_usd above
    from models import CryptoBacktestRun

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(CryptoGridBranch).where(CryptoGridBranch.bot_name == from_bot_name))
        source = result.scalar_one_or_none()
        if source is None:
            raise ValueError(f"no grid branch named {from_bot_name}")
        other_branches = (await db.execute(
            select(CryptoGridBranch).where(CryptoGridBranch.bot_name != from_bot_name, CryptoGridBranch.active == True)
        )).scalars().all()

        result = await db.execute(
            select(CryptoBacktestRun).order_by(CryptoBacktestRun.product_id, desc(CryptoBacktestRun.run_at))
        )
        rows = result.scalars().all()

    latest_by_coin = {}
    for row in rows:
        if row.product_id not in latest_by_coin:
            latest_by_coin[row.product_id] = row

    source_roi = latest_by_coin[source.product_id].roi_pct_of_spend if source.product_id in latest_by_coin else None

    def _would_help(roi_pct):
        if roi_pct is None:
            return None
        if source_roi is None:
            return roi_pct > 0
        return roi_pct > source_roi

    candidates = []
    for b in other_branches:
        roi_pct = latest_by_coin[b.product_id].roi_pct_of_spend if b.product_id in latest_by_coin else None
        candidates.append({
            "bot_name": b.bot_name, "product_id": b.product_id, "allocated_usd": round(b.allocated_usd, 2),
            "locked": bool(b.locked), "roi_pct": roi_pct, "would_help": _would_help(roi_pct),
            "is_new_branch": False,
        })

    new_branch_pid, new_branch_roi = await _best_available_coin_and_roi(exclude_bot_name=from_bot_name)
    if new_branch_pid is not None:
        candidates.append({
            "bot_name": None, "product_id": new_branch_pid, "allocated_usd": None,
            "locked": False, "roi_pct": new_branch_roi, "would_help": _would_help(new_branch_roi),
            "is_new_branch": True,
        })

    candidates.sort(key=lambda c: (c["roi_pct"] is None, -(c["roi_pct"] or 0)))

    return {
        "source_bot_name": from_bot_name, "source_product_id": source.product_id, "source_roi_pct": source_roi,
        "candidates": candidates,
    }


async def _maybe_rotate_one_grid_branch(branch: CryptoGridBranch, after_sale: bool = False):
    """Real, one-branch check for run_grid_auto_rotate_sweep() below -
    also called immediately right after a real sell empties a branch out
    to flat (see run_grid_branch_cycle, after_sale=True), so a slice's
    freshly-realized profit doesn't just sit waiting for the next
    scheduled sweep before it goes back to work. Never touches a branch
    with real open slices - those are actively working, not idle, and
    move_cash_between_grid_branches() itself refuses a non-flat source as
    a second, independent guard even if this check were ever somehow
    bypassed.

    `after_sale=False` (the periodic sweep's own default) enforces
    GRID_ROTATION_COOLDOWN_SECONDS via branch.created_at - since
    move_cash_between_grid_branches() always creates a brand-new branch
    row on rotation, created_at IS the real "how long has this branch's
    coin been in place" signal, no separate column needed. A branch that
    was itself just (re)assigned its current coin recently is left alone
    until it's had real time to actually trade, closing the real
    oscillation this cooldown was built to fix (see the constant's own
    docstring above). `after_sale=True` skips the cooldown - a branch
    that just genuinely sold a real slice earned the right to redeploy
    its freshly-realized profit immediately, the same "the real source of
    a crossing settles immediately" reasoning the family tree's own
    reinforcement chain already established."""
    if branch.locked:
        return
    if not after_sale and branch.created_at is not None:
        age_seconds = (datetime.utcnow() - branch.created_at).total_seconds()
        if age_seconds < GRID_ROTATION_COOLDOWN_SECONDS:
            return
    slices = await get_grid_slices(branch.bot_name)
    if slices:
        return
    if branch.allocated_usd < GRID_AUTO_ROTATE_MIN_USD:
        return
    best_pid, _best_roi = await _best_available_coin_and_roi(exclude_bot_name=branch.bot_name)
    if best_pid is None or best_pid == branch.product_id:
        return  # already the real best available coin, or nothing real to compare against - no pointless real trade
    amount = branch.allocated_usd
    result = await move_cash_between_grid_branches(branch.bot_name, amount, product_id=best_pid)
    log.info(
        f"[GRID] 🔁 auto-rotated ${amount:.2f} of real idle cash from {branch.bot_name} ({branch.product_id}) "
        f"into {result['to_bot_name']} ({best_pid}) - real best-ranked coin available right now"
    )


async def _auto_deploy_idle_free_cash():
    """Real, automatic new-branch creation from genuinely UNALLOCATED
    real free cash - the other half of run_grid_auto_rotate_sweep()
    below. While real free cash (get_real_free_cash_usd) clears
    GRID_AUTO_DEPLOY_AMOUNT_USD, creates a real new branch on whichever
    coin currently ranks best (same real pick every other auto-pick path
    in this file already uses) - each created branch immediately claims
    its own coin, so the next pass through the loop naturally lands on
    the next-best DIFFERENT coin, same as create_multiple_grid_branches.
    Stops the moment real free cash runs out, no more real eligible
    coins exist, or the per-sweep cap is hit - never raises, a real
    shortfall just means fewer (or zero) branches created this sweep,
    picked up again next time."""
    created = 0
    while created < GRID_AUTO_DEPLOY_MAX_NEW_BRANCHES_PER_SWEEP:
        real_free_cash = await get_real_free_cash_usd()
        if real_free_cash is None or real_free_cash < GRID_AUTO_DEPLOY_AMOUNT_USD:
            return
        try:
            product_id = await pick_best_ranked_coin_for_grid()
            branch = await create_grid_branch(product_id, GRID_AUTO_DEPLOY_AMOUNT_USD)
        except Exception as e:
            log.info(f"[GRID] auto-deploy stopped for this sweep - {e}")
            return
        created += 1
        await _log_activity_safe(
            branch.bot_name, branch.product_id, "SPAWN",
            f"🌱🔁 Auto-deployed ${GRID_AUTO_DEPLOY_AMOUNT_USD:.2f} of real unallocated free cash into a brand-new "
            f"grid branch on {branch.product_id} - real best-ranked coin available right now",
        )
        log.info(f"[GRID] 🌱🔁 auto-deployed ${GRID_AUTO_DEPLOY_AMOUNT_USD:.2f} of real free cash into {branch.bot_name} ({branch.product_id})")


async def run_grid_auto_rotate_sweep():
    """Real, periodic automatic capital rotation - per the account
    owner's explicit request: real idle cash should never just sit
    there, it should keep moving toward whichever real coin is currently
    doing well, with zero manual click ever required ("I don't have to
    go back in there and do it"). Runs every GRID_AUTO_ROTATE_INTERVAL_
    SECONDS (throttled in run_grid_branches_cycle(), not here), and does
    two real things every time it fires:

    1. Rotates real idle cash already sitting INSIDE a flat branch (via
       _maybe_rotate_one_grid_branch, for every active branch in turn) -
       a branch with real open slices, too little idle cash, or already
       on the real best-available coin is left completely alone.
    2. Deploys real UNALLOCATED free cash (never allocated to any branch
       at all) into brand-new branches (see _auto_deploy_idle_free_cash)
       - the real gap a manual "New branch"/"Add 3 branches" click used
       to be the only way to close.

    A per-branch rotation failure (a real live-price fetch hiccup, a
    rare claim race) is logged and skipped rather than aborting the
    whole sweep - every other branch still gets its own real chance this
    cycle."""
    if not await is_grid_auto_rotate_active():
        return
    if os.getenv("STOP_TRADING", "false").lower() == "true":
        return
    for branch in await get_grid_branches():
        if not branch.active:
            continue
        try:
            await _maybe_rotate_one_grid_branch(branch)
        except Exception as e:
            log.warning(f"[GRID] auto-rotate check failed for {branch.bot_name} (non-fatal, will retry next sweep): {e}")
    try:
        await _auto_deploy_idle_free_cash()
    except Exception as e:
        log.warning(f"[GRID] auto-deploy of real free cash failed this sweep (non-fatal, will retry next sweep): {e}")


async def quick_buy_best_coin(amount_usd: float) -> dict:
    """The real $20 Quick Buy button's backend - per the account owner's
    explicit request for "a button that I can put $20 in... it'll place
    the [trade] for me," after being told the honest reason the BTC
    price-prediction panel can't back a real bet (no proven directional
    edge, no real instrument to bet on) and offered the two REAL,
    validated strategies instead. This is the Grid Bot half of that -
    56.2% real backtested win rate, not a coin-flip.

    Real, honest behavior worth being explicit about: this does NOT fire
    an instant market buy. It creates a real new grid branch (via
    create_grid_branch, same real spendable-cash check, same real
    dynamic num_levels fix) on whichever coin currently ranks best (via
    pick_best_ranked_coin_for_grid) - the actual first real buy happens
    on that branch's own next cycle, whenever price genuinely closes a
    real 1% dip below its starting reference price, exactly like every
    other grid branch. Refuses while STOP_TRADING is set, matching every
    other path that deploys new real capital in this codebase."""
    if amount_usd <= 0:
        raise ValueError("amount_usd must be positive")
    if os.getenv("STOP_TRADING", "false").lower() == "true":
        raise ValueError("STOP_TRADING is set - no new real capital can be deployed right now")

    product_id = await pick_best_ranked_coin_for_grid()
    branch = await create_grid_branch(product_id, amount_usd)
    return {
        "status": "created", "bot_name": branch.bot_name, "product_id": branch.product_id,
        "allocated_usd": round(branch.allocated_usd, 2), "num_levels": branch.num_levels,
        "reference_price": branch.reference_price,
    }


async def create_multiple_grid_branches(count: int, amount_per_branch: float) -> dict:
    """Real, one-click convenience for the "New grid branch" flow - per
    the account owner's direct follow-up after being told the real lever
    for more trade frequency is running MORE coins, not narrower spacing
    (already shown, by real backtest evidence, to lose money): "yes build
    the one-click add 3 branches shortcut" instead of clicking the New
    Grid Branch modal 3 separate times by hand.

    Creates up to `count` real branches, each on a DIFFERENT real coin -
    picked the exact same way the $20 Quick Buy button already picks one
    (pick_best_ranked_coin_for_grid). Since every created branch
    immediately claims its own coin, the next pass through the loop
    naturally lands on the next-best real coin with zero extra
    duplicate-avoidance logic needed - the same claim mechanism
    create_grid_branch already enforces for a single branch.

    Real, honest partial-success behavior, deliberately NOT all-or-
    nothing: stops early and returns whatever it genuinely managed the
    moment one real attempt fails (real free cash runs out partway
    through, no more real eligible coins exist, a live price fetch
    hiccups) - every branch already created stays created, this never
    rolls back a real allocation that already succeeded. Refuses only up
    front, before touching anything real, if count/amount_per_branch
    aren't sane or STOP_TRADING is set - matching every other real
    capital-deployment path in this file."""
    if count <= 0:
        raise ValueError("count must be positive")
    if amount_per_branch <= 0:
        raise ValueError("amount_per_branch must be positive")
    if os.getenv("STOP_TRADING", "false").lower() == "true":
        raise ValueError("STOP_TRADING is set - no new real capital can be deployed right now")

    created = []
    error = None
    for _ in range(count):
        try:
            product_id = await pick_best_ranked_coin_for_grid()
            branch = await create_grid_branch(product_id, amount_per_branch)
            created.append({
                "bot_name": branch.bot_name, "product_id": branch.product_id,
                "allocated_usd": round(branch.allocated_usd, 2),
            })
        except Exception as e:
            error = str(e)
            break  # real, genuine failure (cash exhausted, nothing left eligible) - stop rather than keep trying
    return {"created": created, "requested_count": count, "error": error}


async def get_grid_slices(bot_name: str) -> list:
    """Every real currently-open slice for one branch, oldest first -
    the exact FIFO order a real sell always consumes from."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(CryptoGridSlice).where(CryptoGridSlice.bot_name == bot_name).order_by(CryptoGridSlice.opened_at.asc())
        )
        return list(result.scalars().all())


async def _log_grid_trade(bot_name, product_id, entry_price, exit_price, qty, pnl, opened_at):
    """Real, persisted record of one completed real grid-slice round
    trip. Best-effort, deliberately never allowed to raise - a logging
    failure here must never affect the real trade or the real
    allocated_usd update that already happened at the call site, same
    defensive pattern every other trade-history logger in this codebase
    already uses."""
    try:
        async with AsyncSessionLocal() as db:
            db.add(CryptoGridTradeHistory(
                bot_name=bot_name, product_id=product_id, entry_price=entry_price,
                exit_price=exit_price, qty=qty, pnl=round(pnl, 2), opened_at=opened_at,
            ))
            await db.commit()
    except Exception as e:
        log.warning(f"[GRID] trade-history log failed for {bot_name} (non-fatal, real trade unaffected): {e}")


async def _log_activity_safe(bot_name, product_id, event_type, message):
    """Reuses crypto_family_tree_bot.py's existing Live Activity feed
    (CryptoActivityEvent) so grid trades show up in the same real,
    already-built dashboard feed instead of a second, separate one -
    purely a shared logging sink, no trading state is shared. Imported
    lazily and wrapped defensively so a real failure here (or that
    module being unavailable for any reason) can never affect a real
    grid trade that already happened."""
    try:
        import crypto_family_tree_bot as tree
        await tree._log_activity(bot_name, product_id, event_type, message)
    except Exception as e:
        log.warning(f"[GRID] activity-feed log failed (non-fatal): {e}")


def _grid_slice_net_pnl(qty: float, entry_price: float, exit_price: float) -> float:
    """THE single real fee/profit formula for one grid slice's round
    trip - shared by BOTH run_grid_branch_cycle()'s real sell (the
    actual dollars booked into a branch's allocated_usd when a real
    order fills) AND get_grid_status()'s dashboard display (the
    hypothetical "if sold right now" figure). Extracted into one
    function per the account owner's own explicit, correct concern:
    "don't let the dashboard calculation and the actual execution
    calculation use two different fee formulas. They should share one
    fee/profit calculation function. Otherwise you can end up with the
    dashboard saying +$4.21 while the actual sale produces something
    different." Before this, the identical formula was hand-written in
    two separate places - never actually inconsistent (both used the
    same ROUND_TRIP_FEE_RATE constant), but with no structural guarantee
    they'd stay that way if either one were ever edited alone.

    gross = qty * (exit_price - entry_price); fee = qty * (entry_price +
    exit_price) * (ROUND_TRIP_FEE_RATE / 2) - the real round-trip taker
    fee on both legs' notional, matching this module's whole real
    Strategy Lab evidence (see the module docstring)."""
    gross = qty * (exit_price - entry_price)
    fee = qty * (entry_price + exit_price) * (engine.ROUND_TRIP_FEE_RATE / 2)
    return gross - fee


def _grid_branch_real_equity(branch: CryptoGridBranch, slices: list, price: float) -> float:
    """Real live equity for one grid branch right now - allocated_usd is
    a cost-basis figure (see the model's own docstring: it only ever
    changes by the real net P&L delta on a completed sell, never
    debited/credited at buy time) plus the real mark-to-market
    unrealized P&L across every currently-open slice, exactly the same
    "allocated_usd + unrealized P&L" formula crypto_family_tree_bot.py's
    own equity-floor fix already validated and uses for the identical
    real reason (a branch's own current position value in isolation
    understates its true total wealth whenever it isn't 100% deployed)."""
    unrealized = sum(s.qty * (price - s.entry_price) for s in slices)
    return branch.allocated_usd + unrealized


async def run_grid_branch_cycle(session, branch: CryptoGridBranch):
    """One real cycle for one real grid branch - the live counterpart to
    crypto_selection_backtest.py's _replay_grid_bot(), same real
    mechanics exactly: buy a real slice when price closes grid_pct below
    the branch's own real reference_price (capped at num_levels
    concurrent slices), sell the OLDEST real open slice (FIFO) when
    price closes grid_pct above it - reference_price updates to the real
    fill price on every real buy AND every real sell, matching
    _replay_grid_bot's own `reference` variable precisely.

    Also runs the real per-branch drawdown breaker (pauses NEW buys
    only - an existing open slice keeps selling normally, which is
    itself this branch's own real recovery path) and, when the account
    owner has opted into it, real fee-tier-aware dynamic spacing."""
    if os.getenv("STOP_TRADING", "false").lower() == "true":
        return
    price, _atr = await engine.get_price_and_volatility(session, branch.product_id)
    if price is None:
        log.warning(f"[GRID] {branch.bot_name}: could not fetch a real live price for {branch.product_id} - skipping this cycle")
        return

    slices = await get_grid_slices(branch.bot_name)

    # ---- Real per-branch drawdown circuit breaker ----
    # NULL peak_equity means an existing row from before this column
    # existed - self-heal to this branch's own real current equity on
    # first read, same "treat uninitialized as today's real number"
    # pattern used codebase-wide for every added-later column.
    equity = _grid_branch_real_equity(branch, slices, price)
    stored_peak_equity = branch.peak_equity if branch.peak_equity else equity
    if equity > stored_peak_equity:
        stored_peak_equity = equity
    if stored_peak_equity != branch.peak_equity:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(CryptoGridBranch).where(CryptoGridBranch.bot_name == branch.bot_name))
            row = result.scalar_one_or_none()
            if row:
                row.peak_equity = stored_peak_equity
                await db.commit()
        branch.peak_equity = stored_peak_equity
    drawdown_pct = (stored_peak_equity - equity) / stored_peak_equity if stored_peak_equity > 0 else 0.0
    drawdown_breached = drawdown_pct >= GRID_DRAWDOWN_BREAKER_PCT

    # ---- Real fee-tier-aware dynamic spacing (opt-in, off by default) ----
    # Only ever recomputed/persisted when the account owner has actually
    # turned this on - a branch running the default fixed spacing pays
    # zero extra real API cost for this, every cycle.
    grid_pct = branch.grid_pct
    if await is_dynamic_spacing_active():
        dynamic_pct, tier_name, taker_rate = await compute_dynamic_grid_pct(session)
        if abs(dynamic_pct - branch.grid_pct) > 1e-9:
            async with AsyncSessionLocal() as db:
                result = await db.execute(select(CryptoGridBranch).where(CryptoGridBranch.bot_name == branch.bot_name))
                row = result.scalar_one_or_none()
                if row:
                    row.grid_pct = dynamic_pct
                    await db.commit()
            log.info(
                f"[GRID] {branch.bot_name}: dynamic spacing {branch.grid_pct*100:.2f}% -> {dynamic_pct*100:.2f}% "
                f"(real fee tier {tier_name or 'unknown'}, taker {taker_rate*100:.3f}%)" if taker_rate is not None else
                f"[GRID] {branch.bot_name}: dynamic spacing left at {dynamic_pct*100:.2f}% (real fee tier lookup failed - fell back to default)"
            )
            branch.grid_pct = dynamic_pct
        grid_pct = branch.grid_pct

    # ---- Real dip: buy a new slice (skipped while drawdown-breached) ----
    if drawdown_breached:
        log.info(
            f"[GRID] {branch.bot_name}: 🛑 real equity ${equity:.2f} is down {drawdown_pct*100:.0f}% from its own "
            f"${stored_peak_equity:,.2f} peak (breaker at {GRID_DRAWDOWN_BREAKER_PCT*100:.0f}%) - new buys paused, "
            f"existing slices still sell normally"
        )
    elif price <= branch.reference_price * (1 - grid_pct) and len(slices) < branch.num_levels:
        slice_usd = branch.allocated_usd / branch.num_levels
        real_balance, real_balance_err = await engine.get_usd_balance(session)
        if real_balance is None:
            log.warning(f"[GRID] {branch.bot_name}: real balance unavailable ({real_balance_err}) - skipping this cycle")
            return
        spend = min(slice_usd, real_balance)
        if spend < MIN_TRADE_USD:
            log.info(f"[GRID] {branch.bot_name}: only ${spend:.2f} real spendable for a new slice (below ${MIN_TRADE_USD:.2f} minimum) - waiting")
            return
        fill = await engine.place_market_buy(session, spend, branch.product_id)
        if not fill:
            log.warning(f"[GRID] {branch.bot_name}: real grid buy into {branch.product_id} did not fill - will retry next cycle")
            return
        filled_qty, filled_price = fill
        async with AsyncSessionLocal() as db:
            db.add(CryptoGridSlice(bot_name=branch.bot_name, product_id=branch.product_id, entry_price=filled_price, qty=filled_qty))
            result = await db.execute(select(CryptoGridBranch).where(CryptoGridBranch.bot_name == branch.bot_name))
            fresh = result.scalar_one_or_none()
            if fresh:
                fresh.reference_price = filled_price
            await db.commit()
        msg = (
            f"🟢 {branch.bot_name} GRID BUY: bought a real slice of {branch.product_id} @ ${filled_price:,.2f} "
            f"(${spend:.2f} deployed, {len(slices) + 1}/{branch.num_levels} real slices now open)"
        )
        log.info(f"[GRID] {msg}")
        await _log_activity_safe(branch.bot_name, branch.product_id, "BUY", msg)
        return

    # ---- Real rise: sell the oldest open slice (FIFO) ----
    # Never gated on drawdown_breached - an existing open slice keeps
    # selling normally regardless (see the drawdown-breach block above);
    # this branch's own real recovery path back toward its peak.
    if price >= branch.reference_price * (1 + grid_pct) and slices:
        oldest = slices[0]
        fill = await engine.place_market_sell(session, oldest.qty, branch.product_id)
        if not fill:
            log.warning(f"[GRID] {branch.bot_name}: real grid sell of {branch.product_id} did not fill - will retry next cycle")
            return
        filled_qty, filled_price = fill
        pnl = _grid_slice_net_pnl(filled_qty, oldest.entry_price, filled_price)
        new_balance = branch.allocated_usd + pnl
        async with AsyncSessionLocal() as db:
            slice_result = await db.execute(select(CryptoGridSlice).where(CryptoGridSlice.id == oldest.id))
            slice_row = slice_result.scalar_one_or_none()
            if slice_row:
                await db.delete(slice_row)
            branch_result = await db.execute(select(CryptoGridBranch).where(CryptoGridBranch.bot_name == branch.bot_name))
            fresh = branch_result.scalar_one_or_none()
            if fresh:
                fresh.allocated_usd += pnl
                fresh.reference_price = filled_price
                new_balance = fresh.allocated_usd
            await db.commit()
        await _log_grid_trade(branch.bot_name, branch.product_id, oldest.entry_price, filled_price, filled_qty, pnl, oldest.opened_at)
        msg = (
            f"{'📈' if pnl >= 0 else '📉'} {branch.bot_name} GRID SELL: sold the oldest real slice of {branch.product_id} @ ${filled_price:,.2f} "
            f"(entry ${oldest.entry_price:,.2f}) | P&L: {'+' if pnl >= 0 else ''}${pnl:.2f} after est. fees | branch now ${new_balance:.2f}"
        )
        log.info(f"[GRID] {msg}")
        await _log_activity_safe(branch.bot_name, branch.product_id, "SELL", msg)
        # Real "settle immediately" check - only when THIS sale emptied
        # the branch out to flat (its own last open slice), so freshly-
        # realized profit doesn't just sit waiting for the next scheduled
        # 30-min sweep before it goes back to work. Best-effort: a
        # failure here can never unwind or affect the real sale that
        # already completed above.
        if len(slices) == 1:
            try:
                async with AsyncSessionLocal() as db:
                    fresh_result = await db.execute(select(CryptoGridBranch).where(CryptoGridBranch.bot_name == branch.bot_name))
                    fresh_branch = fresh_result.scalar_one_or_none()
                if fresh_branch and fresh_branch.active:
                    await _maybe_rotate_one_grid_branch(fresh_branch, after_sale=True)
            except Exception as e:
                log.warning(f"[GRID] {branch.bot_name}: post-sale auto-rotate check failed (non-fatal, will retry next sweep): {e}")
        return


async def run_grid_branches_cycle():
    """Real per-cycle driver for every active grid branch - a true no-op
    unless is_grid_bot_active() is on AND at least one real active
    branch exists."""
    if not await is_grid_bot_active():
        return
    branches = [b for b in await get_grid_branches() if b.active]
    if not branches:
        return
    async with engine.aiohttp.ClientSession() as session:
        for branch in branches:
            try:
                await run_grid_branch_cycle(session, branch)
            except Exception as e:
                log.error(f"[GRID] {branch.bot_name} cycle error: {e}")
            await asyncio.sleep(0.5)

    # Real, periodic automatic idle-cash rotation - throttled here (not
    # inside run_grid_auto_rotate_sweep itself) via a plain in-process
    # timestamp, same pattern crypto_family_tree_bot.py's own scheduled-
    # backtest throttle already uses. Runs after every branch's own
    # normal buy/sell check above, so a branch that just went flat this
    # exact cycle is already picked up by the immediate post-sale check
    # in run_grid_branch_cycle() - this periodic sweep exists for real
    # idle cash that's been sitting for a while, not freshly realized.
    global _last_grid_auto_rotate_at
    now = time.time()
    if now - _last_grid_auto_rotate_at >= GRID_AUTO_ROTATE_INTERVAL_SECONDS:
        _last_grid_auto_rotate_at = now
        try:
            await run_grid_auto_rotate_sweep()
        except Exception as e:
            log.error(f"[GRID] auto-rotate sweep error: {e}")


async def get_grid_status() -> dict:
    """Real, live status for the dashboard - every branch's own real
    allocation, grid parameters, and currently-open slices, PLUS each
    branch's real current live price (fetched once per distinct
    product_id, not once per branch, so several branches sharing a coin
    never cost extra real API calls) - backs the dashboard's real grid
    visual (where price sits right now against the buy/sell trigger
    levels and every open slice's own entry). Read-only."""
    mode_active = await is_grid_bot_active()
    branches = await get_grid_branches()

    distinct_products = {b.product_id for b in branches}
    live_prices = {}
    if distinct_products:
        async with engine.aiohttp.ClientSession() as session:
            for product_id in distinct_products:
                price, _atr = await engine.get_price_and_volatility(session, product_id)
                live_prices[product_id] = price

    out = []
    total_allocated = 0.0
    for b in branches:
        slices = await get_grid_slices(b.bot_name)
        total_allocated += b.allocated_usd
        current_price = live_prices.get(b.product_id)
        # Read-only real drawdown figures for the dashboard - mirrors the
        # exact equity/peak/drawdown formula run_grid_branch_cycle()
        # itself uses, so this can never disagree with what the live
        # bot is actually acting on. Never writes here (self-heal only
        # happens in the live cycle's own write path) - a NULL
        # peak_equity just falls back to today's equity (0% drawdown)
        # for display purposes until the branch's own next real cycle.
        peak_equity = b.peak_equity
        drawdown_pct = None
        drawdown_breached = False
        if current_price is not None:
            equity = _grid_branch_real_equity(b, slices, current_price)
            peak_equity = b.peak_equity if b.peak_equity and b.peak_equity > equity else equity
            drawdown_pct = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0.0
            drawdown_breached = drawdown_pct >= GRID_DRAWDOWN_BREAKER_PCT

        # Real, fee-adjusted unrealized P&L per slice - per the account
        # owner's direct request ("let me know if it's at a profit after
        # the fees price percentage wise"). Uses _grid_slice_net_pnl(),
        # THE ONE shared function run_grid_branch_cycle()'s own real sell
        # also calls - not a second, hand-written copy of the fee math
        # that could quietly drift out of sync - applied here to the LIVE
        # price instead of a real fill price, so this hypothetical
        # "if sold now" figure can never disagree with what a real sell
        # would actually net. None (not a fabricated number) when there's
        # no real live price to compute it from.
        slices_out = []
        total_net_usd = 0.0
        total_cost_basis = 0.0
        for s in slices:
            net_usd = None
            net_pct = None
            if current_price is not None:
                # THE single shared formula - _grid_slice_net_pnl() is the
                # exact same function run_grid_branch_cycle()'s real sell
                # calls, so this hypothetical "if sold now" figure can
                # never drift from what a real sale would actually book.
                net_usd = _grid_slice_net_pnl(s.qty, s.entry_price, current_price)
                cost_basis = s.qty * s.entry_price
                net_pct = (net_usd / cost_basis) if cost_basis else None
                total_net_usd += net_usd
                total_cost_basis += cost_basis
            slices_out.append({
                "entry_price": s.entry_price, "qty": s.qty,
                "opened_at": (s.opened_at.isoformat() + "Z") if s.opened_at else None,
                "unrealized_net_usd": round(net_usd, 2) if net_usd is not None else None,
                "unrealized_net_pct": round(net_pct, 4) if net_pct is not None else None,
            })
        total_net_pct = (total_net_usd / total_cost_basis) if (current_price is not None and total_cost_basis) else None

        out.append({
            "bot_name": b.bot_name, "product_id": b.product_id, "allocated_usd": round(b.allocated_usd, 2),
            "active": b.active, "locked": bool(b.locked), "grid_pct": b.grid_pct, "num_levels": b.num_levels,
            "reference_price": b.reference_price, "open_slices": len(slices),
            "current_price": current_price,
            "peak_equity": round(peak_equity, 2) if peak_equity is not None else None,
            "drawdown_pct": round(drawdown_pct, 4) if drawdown_pct is not None else None,
            "drawdown_breached": drawdown_breached,
            "total_unrealized_net_usd": round(total_net_usd, 2) if current_price is not None and slices else None,
            "total_unrealized_net_pct": round(total_net_pct, 4) if total_net_pct is not None else None,
            "slices": slices_out,
        })
    return {
        "mode_active": mode_active,
        "dynamic_spacing_active": await is_dynamic_spacing_active(),
        "auto_rotate_active": await is_grid_auto_rotate_active(),
        "auto_rotate_interval_minutes": GRID_AUTO_ROTATE_INTERVAL_SECONDS // 60,
        "drawdown_breaker_pct": GRID_DRAWDOWN_BREAKER_PCT,
        "branch_count": len(branches),
        "total_allocated_usd": round(total_allocated, 2),
        "real_free_cash_usd": await get_real_free_cash_usd(),
        "branches": out,
    }


async def get_grid_trade_history(limit_recent: int = 50) -> dict:
    """Real, per-branch trade-history aggregation - the direct grid-side
    counterpart to crypto_family_tree_bot.get_coin_trade_history() /
    prop_bot.get_alpaca_branch_trade_history(). Read-only."""
    async with AsyncSessionLocal() as db:
        agg_result = await db.execute(
            select(
                CryptoGridTradeHistory.bot_name,
                func.count(CryptoGridTradeHistory.id).label("trade_count"),
                func.sum(CryptoGridTradeHistory.pnl).label("total_pnl"),
                func.avg(CryptoGridTradeHistory.pnl).label("avg_pnl"),
                func.sum(case((CryptoGridTradeHistory.pnl > 0, 1), else_=0)).label("wins"),
            ).group_by(CryptoGridTradeHistory.bot_name)
        )
        branches = []
        for bot_name, trade_count, total_pnl, avg_pnl, wins in agg_result.all():
            branches.append({
                "bot_name": bot_name,
                "trade_count": trade_count,
                "total_pnl": round(total_pnl, 2) if total_pnl is not None else 0.0,
                "avg_pnl": round(avg_pnl, 2) if avg_pnl is not None else 0.0,
                "win_rate": round(wins / trade_count * 100, 1) if trade_count else 0.0,
            })
        branches.sort(key=lambda b: b["total_pnl"], reverse=True)

        recent_result = await db.execute(
            select(CryptoGridTradeHistory).order_by(desc(CryptoGridTradeHistory.closed_at)).limit(limit_recent)
        )
        recent_trades = [row.to_dict() for row in recent_result.scalars().all()]

    return {"branches": branches, "recent_trades": recent_trades}


def run():
    log.info("=" * 60)
    log.info("CRYPTO GRID BOT - real, live grid-trading branches")
    log.info("=" * 60)
    if not engine.COINBASE_API_KEY_NAME or not engine.COINBASE_API_PRIVATE_KEY:
        log.error("[GRID] Coinbase credentials not set - grid bot will not run")
        return

    # One persistent event loop for this thread's entire life - same
    # reasoning crypto_family_tree_bot.py's own run() already documents
    # (a fresh asyncio.run() per cycle previously caused a real thread
    # crash elsewhere in this codebase under uvloop).
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    while True:
        try:
            loop.run_until_complete(run_grid_branches_cycle())
        except Exception as e:
            log.error(f"[GRID] cycle error: {e}")
        # Real per-branch cycle jitter (+/-10%), same fix already applied
        # tree-wide in crypto_family_tree_bot.py after a real, documented
        # multi-day spawn-collision saga traced back to every branch's
        # cycle timer starting from the same moment - keeps this bot's
        # own cadence from staying in lockstep with the family tree's.
        time.sleep(CYCLE_SECONDS + random.uniform(-CYCLE_SECONDS * 0.1, CYCLE_SECONDS * 0.1))


if __name__ == "__main__":
    run()

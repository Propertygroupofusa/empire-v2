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


async def create_grid_branch(product_id: str, allocated_usd: float) -> CryptoGridBranch:
    """Creates a real new grid branch - a pure bookkeeping operation plus
    one real live price fetch to anchor its starting reference_price,
    never a trade by itself (mirrors CryptoTreeBranch/AlpacaBranch's own
    "spawning is a bookkeeping transfer" reasoning - the real dollars
    this represents are already sitting in the one real Coinbase wallet,
    just not earmarked to any branch yet). Refuses a non-positive amount,
    a coin already claimed by another active grid branch, or an amount
    exceeding real free spendable cash (see get_real_free_cash_usd) -
    per the account owner's own direct complaint that they were creating
    branches "blindly" with no idea what was actually available; this
    can never silently accept a request for money that doesn't exist.

    num_levels is chosen per-branch (see _safe_num_levels_for_allocation)
    rather than always the fixed DEFAULT_GRID_LEVELS, so a small real
    branch still genuinely trades instead of every slice rounding below
    the real minimum order size."""
    if allocated_usd <= 0:
        raise ValueError("allocated_usd must be positive")
    claimed = await get_grid_branch_claimed_coins()
    if product_id in claimed:
        raise ValueError(f"{product_id} is already claimed by an active grid branch")

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
    none at all. Raises ValueError if nothing real qualifies (no coin has
    a real backtest run yet, or every ranked coin is excluded/claimed)."""
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
    return candidates[0].product_id


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
        gross = filled_qty * (filled_price - oldest.entry_price)
        fee = filled_qty * (oldest.entry_price + filled_price) * (engine.ROUND_TRIP_FEE_RATE / 2)
        pnl = gross - fee
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
        out.append({
            "bot_name": b.bot_name, "product_id": b.product_id, "allocated_usd": round(b.allocated_usd, 2),
            "active": b.active, "grid_pct": b.grid_pct, "num_levels": b.num_levels,
            "reference_price": b.reference_price, "open_slices": len(slices),
            "current_price": current_price,
            "peak_equity": round(peak_equity, 2) if peak_equity is not None else None,
            "drawdown_pct": round(drawdown_pct, 4) if drawdown_pct is not None else None,
            "drawdown_breached": drawdown_breached,
            "slices": [
                {"entry_price": s.entry_price, "qty": s.qty, "opened_at": (s.opened_at.isoformat() + "Z") if s.opened_at else None}
                for s in slices
            ],
        })
    return {
        "mode_active": mode_active,
        "dynamic_spacing_active": await is_dynamic_spacing_active(),
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

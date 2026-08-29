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
from models import CryptoGridBranch, CryptoGridSlice, CryptoGridTradeHistory, TradingBotState

log = logging.getLogger("crypto_grid_bot")

GRID_BOT_MODE_KEY = "crypto_grid_bot_mode_active"
DEFAULT_GRID_PCT = 0.01      # matches crypto_selection_backtest.py's STRATEGY_LAB_GRID_PCT exactly
DEFAULT_GRID_LEVELS = 10     # matches crypto_selection_backtest.py's STRATEGY_LAB_GRID_LEVELS exactly
CYCLE_SECONDS = 30
# Same real per-order minimum crypto_family_tree_bot.py's own MIN_TRADE_USD
# uses - below this, a real Coinbase order isn't worth placing.
MIN_TRADE_USD = 5.0


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


async def create_grid_branch(product_id: str, allocated_usd: float) -> CryptoGridBranch:
    """Creates a real new grid branch - a pure bookkeeping operation plus
    one real live price fetch to anchor its starting reference_price,
    never a trade by itself (mirrors CryptoTreeBranch/AlpacaBranch's own
    "spawning is a bookkeeping transfer" reasoning - the real dollars
    this represents are already sitting in the one real Coinbase wallet,
    just not earmarked to any branch yet). Refuses a non-positive amount
    or a coin already claimed by another active grid branch."""
    if allocated_usd <= 0:
        raise ValueError("allocated_usd must be positive")
    claimed = await get_grid_branch_claimed_coins()
    if product_id in claimed:
        raise ValueError(f"{product_id} is already claimed by an active grid branch")

    async with engine.aiohttp.ClientSession() as session:
        price, _atr = await engine.get_price_and_volatility(session, product_id)
    if price is None:
        raise ValueError(f"could not fetch a real live price for {product_id} right now - try again shortly")

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
            grid_pct=DEFAULT_GRID_PCT, num_levels=DEFAULT_GRID_LEVELS, reference_price=price,
        )
        db.add(branch)
        await db.commit()
        await db.refresh(branch)
    log.info(f"[GRID] 🌱 Created {bot_name} on {product_id} with ${allocated_usd:.2f} (real reference price ${price:.2f})")
    return branch


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


async def run_grid_branch_cycle(session, branch: CryptoGridBranch):
    """One real cycle for one real grid branch - the live counterpart to
    crypto_selection_backtest.py's _replay_grid_bot(), same real
    mechanics exactly: buy a real slice when price closes grid_pct below
    the branch's own real reference_price (capped at num_levels
    concurrent slices), sell the OLDEST real open slice (FIFO) when
    price closes grid_pct above it - reference_price updates to the real
    fill price on every real buy AND every real sell, matching
    _replay_grid_bot's own `reference` variable precisely."""
    if os.getenv("STOP_TRADING", "false").lower() == "true":
        return
    price, _atr = await engine.get_price_and_volatility(session, branch.product_id)
    if price is None:
        log.warning(f"[GRID] {branch.bot_name}: could not fetch a real live price for {branch.product_id} - skipping this cycle")
        return

    slices = await get_grid_slices(branch.bot_name)

    # ---- Real dip: buy a new slice ----
    if price <= branch.reference_price * (1 - branch.grid_pct) and len(slices) < branch.num_levels:
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
    if price >= branch.reference_price * (1 + branch.grid_pct) and slices:
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
    allocation, grid parameters, and currently-open slices. Read-only."""
    mode_active = await is_grid_bot_active()
    branches = await get_grid_branches()
    out = []
    total_allocated = 0.0
    for b in branches:
        slices = await get_grid_slices(b.bot_name)
        total_allocated += b.allocated_usd
        out.append({
            "bot_name": b.bot_name, "product_id": b.product_id, "allocated_usd": round(b.allocated_usd, 2),
            "active": b.active, "grid_pct": b.grid_pct, "num_levels": b.num_levels,
            "reference_price": b.reference_price, "open_slices": len(slices),
            "slices": [
                {"entry_price": s.entry_price, "qty": s.qty, "opened_at": (s.opened_at.isoformat() + "Z") if s.opened_at else None}
                for s in slices
            ],
        })
    return {
        "mode_active": mode_active,
        "branch_count": len(branches),
        "total_allocated_usd": round(total_allocated, 2),
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

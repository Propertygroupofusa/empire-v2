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
import zlib
import random
import sys
import time
from datetime import datetime

from sqlalchemy import select, func, case, desc

import crypto_btc_compound_bot as engine
import crypto_mean_reversion_bot as mean_reversion_engine
import coin_rotation as rotation
from database import get_session_factory
from models import CryptoGridBranch, CryptoGridSlice, CryptoGridTradeHistory, TradingBotState, CryptoTreeBranch, BotPosition

# ── SHADOW MODE INTEGRATION ────────────────────────────────────────────────
# Non-invasive learning validation: observes every trade without affecting execution
sys.path.insert(0, '/home/user/Delfina')
try:
    from stage2.orchestration.shadow_mode_init import get_shadow_manager
    shadow_manager = get_shadow_manager()
    SHADOW_MODE_ENABLED = True
except ImportError:
    SHADOW_MODE_ENABLED = False
    shadow_manager = None

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

# Real, opt-in average-swing-based dynamic grid spacing - the direct,
# evidence-backed follow-up to the fee-tier feature above. Per the
# account owner's own direct question on Strategy Lab's real results
# ("what is the average swing of coins... should it stay at 1% or should
# we set it around that rate"), crypto_selection_backtest.py's
# run_grid_atr_spacing_comparison() replayed a real 30-day/8-coin sample
# at 0.5x/1.0x/1.5x/2.0x each coin's own real average hourly swing versus
# today's fixed 1% - real result: 1.5x avg swing won clearly ($5.79 total
# vs the fixed default's $1.02, 59.8% win rate vs 56.1%, fewer but
# meaningfully better real trades). Turned ON by default here per the
# account owner's own direct "make Grid Bot better" follow-up right after
# seeing that real evidence, and per this codebase's established "flip
# the unset default, no live dashboard access from this sandbox"
# precedent - a real, reversible switch either way, an explicit dashboard
# toggle still wins over this default in either direction afterward.
# Unlike the fee-tier feature (a real no-op at the base tier), this one
# is NOT a no-op - it genuinely changes real trade spacing from cycle
# one, sized per-coin off that coin's own real recent behavior instead of
# one fixed percentage for every coin. Takes precedence over fee-tier
# spacing when both happen to be active (see run_grid_branch_cycle) since
# it's the more directly evidence-backed of the two on real data.
AVG_SWING_SPACING_MODE_KEY = "crypto_grid_avg_swing_spacing_active"
# The real winning multiplier from the backtest above - not re-tuned here,
# reused exactly as validated.
AVG_SWING_SPACING_MULTIPLIER = 1.5
# ~5 real days of hourly candles - see engine.get_average_hourly_swing_pct's
# own docstring for why this is a practical live window, not literally the
# backtest's full 30-day one.
AVG_SWING_LOOKBACK_HOURS = 120

# Real, hourly, per-branch self-tuning of AVG_SWING_SPACING_MULTIPLIER -
# per the account owner's direct request: "make sure it's built to grow
# and be better than the hrs before and learn from it's mistakes and
# makes it self better every hr." Deliberately NOT a new, unvalidated
# strategy or a blind parameter search - it only ever nudges the ONE
# already-validated, already-live lever (this branch's own spacing
# multiplier) using that SAME branch's own real, recently-closed trades
# as the judge, never a backtest simulation. Bounded on both ends so it
# can never drift outside proven-safe territory:
#   - the FLOOR is AVG_SWING_SPACING_MULTIPLIER itself (1.5x, the real
#     backtested winner already live by default) - a branch can only ever
#     get MORE conservative than the validated default in response to a
#     real rough stretch, never looser than what evidence already proved
#     safe.
#   - the CEILING (3.0x) caps how far a single branch can widen, so one
#     genuinely unlucky coin can't compound its own spacing forever.
# Self-correcting, not one-way: a branch that's since recovered eases its
# own multiplier back down toward 1.5x again the moment its real recent
# trades improve - same "contestable, never a permanent verdict"
# philosophy every other adaptive layer in this codebase already uses
# (coin exclusion, the strongest-sibling throne, floor self-heals).
# Every real adjustment is logged to the Live Activity feed with the
# exact real numbers that triggered it, so this is auditable, not a
# silent black box.
SELF_TUNE_INTERVAL_SECONDS = int(os.getenv("GRID_SELF_TUNE_INTERVAL_SECONDS", str(60 * 60)))
# How many of a branch's own most recent REAL closed trades to judge it
# by - also doubles as the minimum trade count required before acting at
# all (not enough real evidence yet with fewer than this many).
SELF_TUNE_LOOKBACK_TRADES = 5
SELF_TUNE_STEP = 0.1
SELF_TUNE_MIN_MULTIPLIER = AVG_SWING_SPACING_MULTIPLIER
SELF_TUNE_MAX_MULTIPLIER = 3.0
SELF_TUNE_POOR_WIN_RATE_PCT = 40.0
SELF_TUNE_GOOD_WIN_RATE_PCT = 60.0
# In-process throttle only, same pattern as _last_grid_auto_rotate_at
# right below.
_last_grid_self_tune_at = 0.0

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
# 30 min -> 5 min, per the account owner: cash freed by an exit should go
# back to work on the next opportunity rather than sitting out the rest of
# a half-hour window.
#
# Safe to shorten because the two things this sweep does have different
# risk profiles, and only one of them is fee-sensitive. Auto-DEPLOY of
# unallocated cash places one fresh buy and cannot oscillate, so checking
# it more often is pure latency reduction. ROTATION between branches is
# the fee-sensitive half, and its frequency is bounded by
# GRID_ROTATION_COOLDOWN_SECONDS (2 hours per branch), not by this
# interval - so a branch still cannot move more than once every two hours
# however often the sweep runs. Shortening this only cuts how long an
# already-eligible move waits to be noticed, from up to 30 minutes to up
# to 5. The confirmed-live oscillation bug that cooldown was written for
# stays fixed.
GRID_AUTO_ROTATE_INTERVAL_SECONDS = int(os.getenv("GRID_AUTO_ROTATE_INTERVAL_SECONDS", str(5 * 60)))
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

# How often the grid refreshes its OWN coin ranking. Default 1 hour, against
# the 24 HOURS the tree's coordinator used - and that coordinator only ran on
# the web service, only in family_tree mode, which is how the table came to be
# frozen while the grid kept trying to rank coins from it. In-process throttle,
# same pattern as every other periodic sweep in this file.
GRID_BACKTEST_REFRESH_SECONDS = int(
    os.getenv("GRID_BACKTEST_REFRESH_SECONDS", str(60 * 60)))
_last_grid_backtest_refresh_at = 0.0

# ── SHADOW MODE MONITORING THROTTLE ────────────────────────────────────────
# Periodic check of shadow mode learning engine progress - logs status every
# N seconds during the accumulation phase (30-50 trades). Useful for alerting
# when validation threshold is reached without spam. 10 minutes = 600 seconds.
SHADOW_MODE_MONITOR_INTERVAL_SECONDS = int(os.getenv("SHADOW_MODE_MONITOR_INTERVAL_SECONDS", str(10 * 60)))
_last_shadow_monitor_at = 0.0

# Mean Reversion Bot Integration
MEAN_REVERSION_CYCLE_SECONDS = int(os.getenv("MEAN_REVERSION_CYCLE_SECONDS", str(5 * 60)))  # Run every 5 minutes
_last_mean_reversion_at = 0.0

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
# Sized for the seven coins whose ADAPTIVE_FLEET_STAGES gate is $0.00 -
# BTC, ETH, DOGE, XRP, LINK, AVAX, DOT - against the ~$578 of real crypto
# capital this account holds. 7 x $70 = $490 deployed, $88 left as cash.
#
# $70 rather than $50 because levels are capped at allocated_usd //
# MIN_TRADE_USD: a $50 branch gets 10 levels of exactly $5.00, sitting
# precisely ON the minimum order size with no room for a price move to
# push a slice under it. $70 gives the same 10 levels at $7.00 each, far
# enough clear that a slice cannot round below the floor and stall.
GRID_AUTO_DEPLOY_AMOUNT_USD = float(os.getenv("GRID_AUTO_DEPLOY_AMOUNT_USD", "70.0"))

# Real cash the auto-deployer must always leave behind, never spending the
# account down to its last dollar.
#
# This did not exist before. Auto-deploy's only floor was "free cash >=
# one branch", so with enough eligible coins it would keep opening
# branches until under $70 remained - fine while exactly seven coins were
# eligible and it ran out of coins first, and not fine the moment SOL
# ($688 realized) or ADA ($2,106) unlock and there are nine. A reserve
# that exists only because the system ran out of things to buy is not a
# reserve. $88 is what seven full branches leave of $578, so today this
# changes nothing and simply stops being luck.
GRID_CASH_RESERVE_USD = float(os.getenv("GRID_CASH_RESERVE_USD", "88.0"))

# Caps how many NEW branches one single sweep can create - real,
# deliberate friction against a large, sudden cash windfall (or a bug)
# spinning up dozens of tiny branches in one shot. A real surplus above
# this cap just gets picked up on the next sweep instead.
#
# Raised 3 -> 7 so a full fleet fills in ONE sweep rather than three, per
# the account owner: capital should go to work the moment it is free, not
# wait out two more 30-minute cycles while opportunities pass. Still real
# friction - it is exactly the number of currently-eligible coins, and
# each branch claims its own coin, so this cannot run away into dozens of
# tiny branches however much cash appears at once.
GRID_AUTO_DEPLOY_MAX_NEW_BRANCHES_PER_SWEEP = int(os.getenv("GRID_AUTO_DEPLOY_MAX_NEW_BRANCHES_PER_SWEEP", "7"))

# Opt-in staged capital fleet. These are activation gates from the
# operator's proposed sequence, not projected or guaranteed returns.
# Only permanently booked Grid trade P&L counts. Disabled by default
# because enabling it can earmark real Coinbase cash for new branches.
GRID_ADAPTIVE_FLEET_ENABLED = os.getenv("GRID_ADAPTIVE_FLEET_ENABLED", "false").lower() == "true"
ADAPTIVE_FLEET_STAGES = (
    ("BTC-USD", 0.0),
    ("ETH-USD", 0.0),
    ("SOL-USD", 688.0),
    ("ADA-USD", 2106.0),
    ("DOGE-USD", 0.0),
    ("XRP-USD", 0.0),
    ("LINK-USD", 0.0),
    ("AVAX-USD", 0.0),
    ("DOT-USD", 0.0),
)

# The real, fixed net-margin target this feature holds constant as the
# account's real Coinbase fee tier changes - deliberately DERIVED from
# today's live values so a branch trading at the base fee tier behaves
# BYTE-IDENTICALLY to the existing fixed DEFAULT_GRID_PCT (0.01) - this
# feature only ever narrows the real grid spacing once the account's
# real fee tier genuinely improves (lower real fees), it never changes
# anything for an account still at the base tier. See
# compute_dynamic_grid_pct() for how this composes with the real live
# fee rate.
# CORRECTED 2026-09-25, found live. This was:
#
#     TARGET_NET_MARGIN_PCT = DEFAULT_GRID_PCT - engine.ROUND_TRIP_FEE_RATE
#
# which reads as a margin but is a SUBTRACTION, and a subtraction can go
# negative. It did. DEFAULT_GRID_PCT is 0.01 and engine.ROUND_TRIP_FEE_RATE
# was raised from 0.008 to the account's real 0.015 - so this silently
# became -0.005, and fee_safe_floor_pct() computed
#
#     max(0.003, -0.005 + 0.0075 * 2) = 0.010
#
# The live dashboard duly reported a 1.00% fee-safe floor against a 1.50%
# round trip. Every spacing between 1.00% and 1.70% was being certified as
# fee-safe while being a guaranteed loss - the exact failure
# test_fee_floor_worst_case.py exists to prevent, reintroduced through a
# DERIVED constant instead of through the function that file guards. The
# test passed throughout, because it asserted the arithmetic against its own
# hardcoded 0.002 rather than against the constant production actually uses.
#
# So this is now a margin in its own right: the profit a round trip must
# clear ON TOP of its fees before a spacing is allowed. It cannot be
# expressed as a difference between two other numbers, because that is what
# let a fee increase quietly eat it.
TARGET_NET_MARGIN_PCT = float(os.getenv("GRID_TARGET_NET_MARGIN_PCT", "0.002"))

# A margin of zero or less is not a margin - it makes fee_safe_floor_pct()
# certify a spacing that exactly pays its own fees and earns nothing, or
# less. Refuse at import rather than trade on it.
if TARGET_NET_MARGIN_PCT <= 0:
    raise ValueError(
        f"GRID_TARGET_NET_MARGIN_PCT must be positive, got {TARGET_NET_MARGIN_PCT}. "
        f"It is the profit a round trip must clear ON TOP of fees; at zero or below, "
        f"the fee-safe floor stops being a floor.")

# A real floor under how tight dynamic spacing is ever allowed to go,
# regardless of how favorable the real fee tier gets - protects against
# over-trading into pure market noise if this codebase's own
# ROUND_TRIP_FEE_RATE assumption ever turns out to be too generous for
# a real, currently-unknown-to-this-sandbox fee tier.
MIN_DYNAMIC_GRID_PCT = 0.003

# ── THE REAL ROUND-TRIP FEE RATE ────────────────────────────────────────
# Real bug found 2026-09-05, from the account's own live snapshot: every
# branch was trading at 2.00% spacing while _grid_slice_net_pnl() priced
# every round trip at engine.ROUND_TRIP_FEE_RATE (now 0.015 = the real
# measured 0.75% each way, taker),
# a HARDCODED assumption. get_real_fee_tier() has always fetched the
# account's genuine live Coinbase taker rate every cycle - and its own
# docstring admits the maker/taker numbers were "not consumed by anything
# yet" beyond spacing. The real rate never reached the P&L formula.
#
# That matters because _grid_slice_net_pnl() is not just a display figure -
# _pick_profitable_slice_to_sell() uses it to decide whether a slice is
# genuinely profitable enough to sell at all. Understate the fee and the
# "never sell at a loss" guarantee silently degrades into "never sell at a
# loss ASSUMING FEES WE MADE UP", green-lighting real losing sells and
# booking inflated P&L into allocated_usd.
#
# Coinbase Advanced Trade's real retail base tier is 1.20% taker - a 2.40%
# round trip, 3x the assumed 0.80%. On 2.00% spacing that turns every
# completed cycle into a real -0.40% loss BY CONSTRUCTION. This sandbox
# has no Coinbase credentials and cannot confirm which tier this account
# is actually on; the fix is to stop guessing and use the real number the
# app already fetches.
#
# CONSERVATIVE_ROUND_TRIP_FEE_RATE is used ONLY when no real rate has ever
# been observed (never fetched, never persisted). It errs HIGH on purpose:
# guessing low sells into real losses, guessing high only makes the bot
# wait longer for a genuinely profitable exit. Never fail optimistic on a
# number that gates real money.
CONSERVATIVE_ROUND_TRIP_FEE_RATE = float(
    os.getenv("GRID_CONSERVATIVE_ROUND_TRIP_FEE_RATE", "0.024")
)

# Last real observed round-trip fee rate, persisted so a restart doesn't
# fall back to the conservative default while real trading continues.
REAL_FEE_RATE_STATE_KEY = "grid_real_round_trip_fee_rate"

# In-process cache of the same, refreshed each cycle by refresh_real_fee_rate().
_cached_real_round_trip_fee_rate = None

# The account's real per-leg MAKER rate, cached alongside it. A maker
# (resting limit) fill costs roughly half a taker (market) fill, so once
# maker orders are live this is the rate that actually applies - assuming
# the taker rate on a maker fill would understate profit just as badly as
# the old hardcoded guess overstated it.
_cached_real_maker_fee_rate = None


async def refresh_real_fee_rate(session) -> float:
    """Fetch the account's REAL current Coinbase taker rate and cache it as
    the real round-trip rate (taker x 2 - every order this codebase places
    is a market order, so taker is the rate that genuinely applies on both
    legs). Persists it so a restart keeps using the real number instead of
    dropping back to the conservative default.

    Returns the effective real round-trip rate. On a real fetch failure it
    changes nothing and returns whatever the last real observation was."""
    global _cached_real_round_trip_fee_rate, _cached_real_maker_fee_rate
    maker, taker, tier_name, err = await engine.get_real_fee_tier(session)
    if maker is not None:
        _cached_real_maker_fee_rate = float(maker)
    if taker is None:
        log.warning(
            f"[GRID] real fee-tier lookup failed ({err}) - keeping last known "
            f"real round-trip rate {await get_effective_round_trip_fee_rate()*100:.3f}%"
        )
        return await get_effective_round_trip_fee_rate()

    real_rate = float(taker) * 2
    previous = _cached_real_round_trip_fee_rate
    _cached_real_round_trip_fee_rate = real_rate
    if previous is None or abs(previous - real_rate) > 1e-9:
        log.info(
            f"[GRID] real Coinbase fee tier {tier_name or 'unknown'}: taker "
            f"{taker*100:.3f}% -> real round-trip {real_rate*100:.3f}% "
            f"(codebase's old hardcoded assumption was "
            f"{engine.ROUND_TRIP_FEE_RATE*100:.3f}%)"
        )
        try:
            async with get_session_factory()() as db:
                result = await db.execute(
                    select(TradingBotState).where(TradingBotState.bot_name == REAL_FEE_RATE_STATE_KEY)
                )
                row = result.scalar_one_or_none()
                if row is None:
                    db.add(TradingBotState(bot_name=REAL_FEE_RATE_STATE_KEY, base_capital=real_rate))
                else:
                    row.base_capital = real_rate
                await db.commit()
        except Exception as e:
            # Best-effort persistence only - the in-process cache is already
            # correct, so a DB hiccup must never block real trading.
            log.warning(f"[GRID] could not persist real fee rate: {type(e).__name__}: {e}")
    return real_rate


async def get_effective_round_trip_fee_rate() -> float:
    """The real round-trip fee rate to price every slice's P&L against:
    this cycle's live observation, else the last real one persisted, else
    the deliberately-conservative default. Never the old optimistic
    hardcoded guess."""
    global _cached_real_round_trip_fee_rate
    if _cached_real_round_trip_fee_rate is not None:
        return _cached_real_round_trip_fee_rate
    try:
        async with get_session_factory()() as db:
            result = await db.execute(
                select(TradingBotState).where(TradingBotState.bot_name == REAL_FEE_RATE_STATE_KEY)
            )
            row = result.scalar_one_or_none()
            if row is not None and row.base_capital and row.base_capital > 0:
                _cached_real_round_trip_fee_rate = float(row.base_capital)
                return _cached_real_round_trip_fee_rate
    except Exception:
        pass
    return CONSERVATIVE_ROUND_TRIP_FEE_RATE


# ── MAKER ORDERS ────────────────────────────────────────────────────────
# Grid trading is a limit-order strategy by nature: buy X% below, sell X%
# above. It was placing MARKET orders to do that job and paying the taker
# premium for nothing. Measured from this account's own fills on
# 2026-09-25: 0.75%/leg taker vs 0.35%/leg maker - so 1.50% versus 0.70%
# on a round trip. On a 2.00% grid that is the difference between keeping
# 25% of each trade's gross move and keeping 65% of it, and it is what
# sets the fee floor at 1.70% instead of 0.90%.
#
# Deliberately maker-FIRST, market-FALLBACK rather than a full resting-grid
# rewrite: the existing price trigger, slice selection and
# never-sell-at-a-loss gate are all preserved exactly, and a maker order
# that does not fill inside its window is cancelled and retried as the
# market order the bot would have placed anyway. So the worst case is
# today's behaviour plus a short delay - never a missed or duplicated trade.
MAKER_ORDERS_MODE_KEY = "grid_maker_orders_mode"

# How long a real post-only order is left resting before giving up and
# falling back to a market order. Long enough to be filled in a normally
# active book, short enough that a real trigger is not missed outright.
MAKER_ORDER_WAIT_SECONDS = int(os.getenv("GRID_MAKER_ORDER_WAIT_SECONDS", "45"))


async def is_maker_orders_active() -> bool:
    """Real, DB-persisted toggle for maker (post-only limit) orders.

    Defaults to OFF, deliberately. Every other spacing/exit default in this
    file was flipped on from real backtest evidence; this one is a brand-new
    real ORDER-EXECUTION path that has never touched the live account, and
    the arithmetic in its favour (see above) is not the same thing as having
    watched it fill. The account owner turns it on from the dashboard."""
    async with get_session_factory()() as db:
        result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == MAKER_ORDERS_MODE_KEY))
        row = result.scalar_one_or_none()
        if row is None:
            return False
        return bool(row.base_capital and row.base_capital >= 1.0)


async def set_maker_orders_active(enabled: bool):
    async with get_session_factory()() as db:
        result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == MAKER_ORDERS_MODE_KEY))
        row = result.scalar_one_or_none()
        if row is None:
            db.add(TradingBotState(bot_name=MAKER_ORDERS_MODE_KEY, base_capital=1.0 if enabled else 0.0))
        else:
            row.base_capital = 1.0 if enabled else 0.0
        await db.commit()


# --- maker-ONLY mode ------------------------------------------------------
# The stronger form of maker orders: maker first, and if it does not fill,
# NOTHING. No market fallback.
#
# Why this exists. On 2026-09-25 the live account's own numbers read:
#
#     taker round trip   1.50%   (0.75% a leg)
#     maker round trip   0.70%   (0.35% a leg - MEASURED, on a real fill)
#     adverse selection  0.67%   (measured by the net-edge gate)
#     live grid spacing  2.00%
#
# With the market fallback in place the floor must price taker, because an
# unfilled maker order really does become a market order: a 1.70% floor and
# a 2.17% true cost against 2.00% of step - the step LOSES 0.17% a cycle.
# Remove the fallback and the real cost is 1.37% (0.70% + 0.67%), which
# that same 2.00% step clears by 0.63%. What changes the arithmetic is not
# a cleverer limit price. It is whether the taker path exists at all.
#
# (The floor and the cost are different numbers and must not be conflated:
# the 0.90% floor is 0.70% of fees PLUS the 0.20% margin the floor insists
# on earning. Adding adverse selection to the FLOOR rather than to the cost
# double-counts that margin - it reads 1.57% and understates the headroom.)
#
# What it costs. A grid has no urgency on either leg. A buy that does not
# fill simply does not happen this cycle - the price is still there next
# cycle, and the branch keeps its cash. The sell leg only ever fires on a
# slice _pick_profitable_slice_to_sell has already certified as net
# profitable, so waiting for a maker fill cannot turn a winner into a
# loser. And the one path that must always get out -
# close_all_grid_branches() - calls engine.place_market_sell() directly and
# is deliberately untouched by this mode, while the drawdown breaker only
# ever pauses BUYS. Nothing that MUST fill is routed through here.
MAKER_ONLY_MODE_KEY = "grid_maker_only_mode"

# With no fallback to rush toward, a resting order can afford to rest. Used
# INSTEAD of MAKER_ORDER_WAIT_SECONDS while maker-only is on: long enough
# to be filled by ordinary book movement, rather than needing to be lucky
# inside the 45 seconds a pending market fallback allowed.
MAKER_ONLY_ORDER_WAIT_SECONDS = int(os.getenv("GRID_MAKER_ONLY_WAIT_SECONDS", "240"))

# How often a cycle passed rather than paying taker. Counted, because the
# cost of this mode is missed trades and an uncounted cost is an assumed one.
GRID_MAKER_ONLY_SKIP_BUY_KEY = "grid_maker_only_skipped_buy"
GRID_MAKER_ONLY_SKIP_SELL_KEY = "grid_maker_only_skipped_sell"


# An escape hatch for the dashboard button, not a replacement for it.
#
# The button is the normal way in. But on 2026-09-25 the account owner
# pressed it, reported it flipped, and the database still read False - and
# nothing on the page could say whether the confirm dialog was dismissed, the
# fetch hit a service mid-restart, or the write was refused. The control was
# verified working end to end; the click still did not land, twice.
#
# This codebase has already paid for that exact situation once. When
# CRYPTO_STRATEGY_MODE could not be corrected through the Railway UI at all,
# the fix was a second variable with no deployment history to restore, and
# get_crypto_strategy_mode() checks it FIRST. Same shape here: set
# GRID_MAKER_ONLY=true on the crypto-trading service and the mode is on from
# the next process start, with no button involved.
#
# Deliberately strict about what counts as on: only the explicit strings
# below. A typo reads as "not set" and falls through to the database rather
# than silently enabling a real-money mode, and only an explicit false
# actively forces it OFF - so the variable can also be used to override a
# stuck database row in either direction.
MAKER_ONLY_ENV_VAR = "GRID_MAKER_ONLY"
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def maker_only_env_override():
    """True/False from the environment, or None when it says nothing."""
    raw = (os.getenv(MAKER_ONLY_ENV_VAR) or "").strip().strip('"').strip("'").lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    return None


async def is_maker_only_active() -> bool:
    """Whether the market fallback is genuinely switched off.

    Checked in order: the environment override, then the database flag - and
    either way maker orders must also be on, because maker-only without them
    would mean "never place a maker order, and never fall back either", which
    is a bot that cannot trade at all.

    FAILS CLOSED. Every consumer of this - above all
    worst_case_leg_fee_rate(), which is a safety floor rather than an
    estimate - is safe when this answers False and unsafe when it wrongly
    answers True. So an unreadable toggle reads as False and the floor
    stays priced against taker.
    """
    env = maker_only_env_override()
    if env is False:
        return False
    if env is True:
        # Still requires maker orders, for the reason above - but turn them
        # on rather than refusing, since the operator setting this variable
        # has unambiguously asked for maker-only and a half-set pair would
        # silently stop the fleet trading, which is the failure this whole
        # escape hatch exists to end.
        try:
            if not await is_maker_orders_active():
                await set_maker_orders_active(True)
        except Exception as e:
            log.warning(f"[GRID] {MAKER_ONLY_ENV_VAR}=true but maker orders could "
                        f"not be enabled ({e}) - staying OFF rather than running "
                        f"maker-only with market orders")
            return False
        return True
    try:
        async with get_session_factory()() as db:
            result = await db.execute(
                select(TradingBotState).where(TradingBotState.bot_name == MAKER_ONLY_MODE_KEY))
            row = result.scalar_one_or_none()
            if row is None or not (row.base_capital and row.base_capital >= 1.0):
                return False
        return await is_maker_orders_active()
    except Exception as e:
        log.warning(f"[GRID] maker-only toggle unreadable ({e}) - assuming the market "
                    f"fallback is still live; the spacing floor stays taker-priced")
        return False


async def set_maker_only_active(enabled: bool):
    """Remove the market fallback (True) or restore it (False).

    Turning it ON also turns maker orders on, because maker-only is
    meaningless without them and a half-set pair would silently stop the
    whole fleet trading.
    """
    if enabled and not await is_maker_orders_active():
        await set_maker_orders_active(True)
    async with get_session_factory()() as db:
        result = await db.execute(
            select(TradingBotState).where(TradingBotState.bot_name == MAKER_ONLY_MODE_KEY))
        row = result.scalar_one_or_none()
        if row is None:
            db.add(TradingBotState(bot_name=MAKER_ONLY_MODE_KEY,
                                   base_capital=1.0 if enabled else 0.0))
        else:
            row.base_capital = 1.0 if enabled else 0.0
        await db.commit()
    log.warning(f"[GRID] maker-ONLY mode set to {enabled} - market fallback "
                f"{'REMOVED' if enabled else 'restored'}")


async def maker_wait_seconds() -> int:
    """How long a post-only order is left resting before it is given up on."""
    return MAKER_ONLY_ORDER_WAIT_SECONDS if await is_maker_only_active() else MAKER_ORDER_WAIT_SECONDS


async def _record_maker_only_skip(key: str):
    """Count one cycle that passed rather than pay taker. Never raises."""
    try:
        async with get_session_factory()() as db:
            result = await db.execute(
                select(TradingBotState).where(TradingBotState.bot_name == key))
            row = result.scalar_one_or_none()
            if row is None:
                row = TradingBotState(bot_name=key, base_capital=0.0)
                db.add(row)
            row.base_capital = float(row.base_capital or 0.0) + 1.0
            await db.commit()
    except Exception as e:
        log.warning(f"[GRID] maker-only skip counter failed for {key} (ignored): {e}")


async def get_maker_only_skips() -> dict:
    """How many cycles maker-only cost, as counted."""
    out = {}
    for label, key in (("buy", GRID_MAKER_ONLY_SKIP_BUY_KEY),
                       ("sell", GRID_MAKER_ONLY_SKIP_SELL_KEY)):
        n = 0.0
        try:
            async with get_session_factory()() as db:
                result = await db.execute(
                    select(TradingBotState).where(TradingBotState.bot_name == key))
                row = result.scalar_one_or_none()
                if row is not None:
                    n = float(row.base_capital or 0.0)
        except Exception as e:
            log.warning(f"[GRID] maker-only skip read failed for {key}: {e}")
        out[label] = int(n)
    out["total"] = out["buy"] + out["sell"]
    return out


async def expected_leg_fee_rate() -> float:
    """The REAL per-leg fee rate the next order is expected to pay: the
    maker rate when maker orders are on, the taker rate otherwise. Half the
    round-trip rate, since a round trip is two legs."""
    global _cached_real_maker_fee_rate
    if await is_maker_orders_active() and _cached_real_maker_fee_rate is not None:
        return _cached_real_maker_fee_rate
    return (await get_effective_round_trip_fee_rate()) / 2


# --- maker/taker fill mix -------------------------------------------------
# Two rows, each holding two counters in the generic TradingBotState bucket:
#   base_capital     = legs that filled as MAKER
#   starting_capital = legs that fell back to a MARKET order (taker)
GRID_FILL_MIX_BUY_KEY = "grid_fill_mix_buy"
GRID_FILL_MIX_SELL_KEY = "grid_fill_mix_sell"


async def _record_fill_leg(key: str, was_maker: bool):
    """Count one filled leg by how it actually filled.

    Added 2026-09-25, because nothing in this codebase measured it and a
    real decision was resting on the guess. fee_safe_floor_pct() priced
    the minimum spacing off the MAKER rate while grid_buy() falls back to
    a market order after MAKER_ORDER_WAIT_SECONDS - so the floor read
    0.90% against a taker round trip of 1.50%, certifying an 0.80%-wide
    band of spacings that lose money whenever a fallback happens. The
    floor now prices the taker leg unconditionally, which is correct but
    conservative: if maker legs really do fill almost always, the fleet is
    trading wider than it needs to.

    Neither the safe answer nor the tighter one can be chosen on
    evidence until something counts. This counts.

    Never allowed to raise. A counter that can break a trade is worse than
    no counter - the same rule _record_gate_decision() and _log_activity()
    already follow.
    """
    try:
        async with get_session_factory()() as db:
            result = await db.execute(
                select(TradingBotState).where(TradingBotState.bot_name == key))
            row = result.scalar_one_or_none()
            if row is None:
                row = TradingBotState(bot_name=key, base_capital=0.0, starting_capital=0.0)
                db.add(row)
            if was_maker:
                row.base_capital = float(row.base_capital or 0.0) + 1.0
            else:
                row.starting_capital = float(row.starting_capital or 0.0) + 1.0
            await db.commit()
    except Exception as e:
        log.warning(f"[GRID] fill-mix counter failed for {key} (ignored): {e}")


async def get_fill_mix() -> dict:
    """What the legs REALLY paid, as counted - never inferred from config.

    maker_rate is None, not a number, until at least one leg has filled:
    a rate over zero legs is not a measurement.
    """
    out = {}
    total_maker = total_taker = 0.0
    for label, key in (("buy", GRID_FILL_MIX_BUY_KEY), ("sell", GRID_FILL_MIX_SELL_KEY)):
        maker = taker = 0.0
        try:
            async with get_session_factory()() as db:
                result = await db.execute(
                    select(TradingBotState).where(TradingBotState.bot_name == key))
                row = result.scalar_one_or_none()
                if row is not None:
                    maker = float(row.base_capital or 0.0)
                    taker = float(row.starting_capital or 0.0)
        except Exception as e:
            log.warning(f"[GRID] fill-mix read failed for {key}: {e}")
        legs = maker + taker
        out[label] = {
            "maker_legs": int(maker), "taker_legs": int(taker), "legs": int(legs),
            "maker_rate": round(maker / legs, 4) if legs else None,
        }
        total_maker += maker
        total_taker += taker

    legs = total_maker + total_taker
    maker_rate = (total_maker / legs) if legs else None
    real_round_trip = await get_effective_round_trip_fee_rate()
    maker_leg = (_cached_real_maker_fee_rate
                 if _cached_real_maker_fee_rate is not None else real_round_trip / 2)
    taker_leg = real_round_trip / 2
    out["overall"] = {
        "maker_legs": int(total_maker), "taker_legs": int(total_taker), "legs": int(legs),
        "maker_rate": round(maker_rate, 4) if maker_rate is not None else None,
        # What a round trip has actually averaged, weighted by how the legs
        # really filled. None until something has filled.
        "blended_round_trip_fee_rate": (
            round((maker_rate * maker_leg + (1 - maker_rate) * taker_leg) * 2, 6)
            if maker_rate is not None else None),
        "taker_round_trip_fee_rate": round(taker_leg * 2, 6),
        "maker_round_trip_fee_rate": round(maker_leg * 2, 6),
        "note": ("While the market fallback exists the spacing floor prices the TAKER "
                 "round trip, because an unfilled maker order becomes a market order, and "
                 "these counts are evidence toward relaxing that - not a reason to on their "
                 "own. Under maker-ONLY mode the fallback is gone, so the floor prices the "
                 "measured maker leg and these counts become the check that it is telling "
                 "the truth: any taker leg counted while maker-only is on is a bug."),
    }
    return out


async def grid_buy(session, usd_amount: float, product_id: str):
    """Real grid BUY: maker first (cheap, may not fill), market fallback
    (always fills, costs more) - unless maker-ONLY mode has removed that
    fallback, in which case an unfilled maker order simply means no buy
    this cycle. Returns (filled_qty, price, leg_fee_rate) or None - the
    real rate actually paid comes back with the fill so the slice can
    record it and be priced honestly later."""
    if await is_maker_orders_active():
        fill = await engine.place_maker_buy(session, usd_amount, product_id,
                                            await maker_wait_seconds())
        if fill:
            qty, price = fill
            log.info(f"[GRID] {product_id}: real MAKER buy filled {qty:.8f} @ ${price:,.6f} (cheaper fee)")
            await _record_fill_leg(GRID_FILL_MIX_BUY_KEY, True)
            return qty, price, (_cached_real_maker_fee_rate
                                if _cached_real_maker_fee_rate is not None
                                else (await get_effective_round_trip_fee_rate()) / 2)
        if await is_maker_only_active():
            # No fallback, by design. A buy that does not happen costs
            # nothing and the dip will still be there next cycle; a taker
            # buy costs 0.75% and would make the spacing floor a lie.
            log.info(f"[GRID] {product_id}: maker buy did not fill and maker-ONLY mode is on - "
                     f"passing this cycle rather than paying the taker leg")
            await _record_maker_only_skip(GRID_MAKER_ONLY_SKIP_BUY_KEY)
            return None
    fill = await engine.place_market_buy(session, usd_amount, product_id)
    if not fill:
        return None
    qty, price = fill
    await _record_fill_leg(GRID_FILL_MIX_BUY_KEY, False)
    return qty, price, (await get_effective_round_trip_fee_rate()) / 2


async def grid_sell(session, qty: float, product_id: str):
    """Real grid SELL: maker first, market fallback - unless maker-ONLY
    mode has removed the fallback. Returns (filled_qty, price,
    leg_fee_rate) or None.

    Waiting is safe on this leg specifically because of who calls it: the
    only caller is the rise trigger, and it only ever hands over a slice
    _pick_profitable_slice_to_sell has already certified as net
    profitable at the current price. There is no forced exit here to
    strand - close_all_grid_branches() sells at market directly.
    """
    if await is_maker_orders_active():
        fill = await engine.place_maker_sell(session, qty, product_id,
                                             await maker_wait_seconds())
        if fill:
            filled_qty, price = fill
            log.info(f"[GRID] {product_id}: real MAKER sell filled {filled_qty:.8f} @ ${price:,.6f} (cheaper fee)")
            await _record_fill_leg(GRID_FILL_MIX_SELL_KEY, True)
            return filled_qty, price, (_cached_real_maker_fee_rate
                                       if _cached_real_maker_fee_rate is not None
                                       else (await get_effective_round_trip_fee_rate()) / 2)
        if await is_maker_only_active():
            log.info(f"[GRID] {product_id}: maker sell did not fill and maker-ONLY mode is on - "
                     f"holding the slice rather than paying the taker leg out of its own profit")
            await _record_maker_only_skip(GRID_MAKER_ONLY_SKIP_SELL_KEY)
            return None
    fill = await engine.place_market_sell(session, qty, product_id)
    if not fill:
        return None
    filled_qty, price = fill
    await _record_fill_leg(GRID_FILL_MIX_SELL_KEY, False)
    return filled_qty, price, (await get_effective_round_trip_fee_rate()) / 2


async def slice_round_trip_fee_rate(slice_row, exit_leg_rate: float = None) -> float:
    """The REAL round-trip fee for one specific slice: the rate its BUY leg
    genuinely paid (recorded on the slice) plus the rate its SELL leg is
    expected to pay. With maker orders live the two legs can genuinely
    differ, so assuming one rate for both would misprice the trade - the
    exact class of bug that made the bot sell real losers as wins."""
    entry_rate = getattr(slice_row, "entry_fee_rate", None)
    if entry_rate is None or entry_rate <= 0:
        entry_rate = (await get_effective_round_trip_fee_rate()) / 2
    if exit_leg_rate is None:
        exit_leg_rate = await expected_leg_fee_rate()
    return entry_rate + exit_leg_rate


async def fee_safe_floor_pct() -> float:
    """The real minimum grid spacing that can actually clear a round trip,
    priced against the WORST fee the round trip can really pay.

    Corrected 2026-09-25. This used expected_leg_fee_rate(), which returns
    the MAKER rate whenever maker orders are on. That is the right rate to
    ESTIMATE with and the wrong one to FLOOR with, because grid_buy() and
    grid_sell() are documented as "maker first (cheap, may not fill),
    market fallback (always fills, costs more)": after
    MAKER_ORDER_WAIT_SECONDS they place a market order and pay taker.

    With the live numbers that gap was not academic. The maker-priced
    floor read 0.90% while a round trip that fell back on both legs costs
    1.50%, so this function was certifying spacings between those two
    figures as fee-safe when they are a guaranteed loss on any cycle that
    falls back. The whole contract of this function is that a branch can
    never be set to a spacing whose full cycle is a guaranteed real loss,
    and against taker fallback it was not keeping it.

    Maker fill rate is not measured anywhere in this codebase, so there is
    no evidence available to justify the optimistic assumption. Until
    something counts fallbacks, the floor prices the case the bot can
    actually end up in.

    The maker benefit is still real - it shows up in the MARGIN earned at
    a given spacing, which is where it belongs, rather than in permission
    to set a spacing that cannot survive a fallback.
    """
    leg = await worst_case_leg_fee_rate()
    return max(MIN_DYNAMIC_GRID_PCT, TARGET_NET_MARGIN_PCT + leg * 2)


async def worst_case_leg_fee_rate() -> float:
    """The highest per-leg fee a single leg can really pay.

    Normally that is the taker leg. A maker order is an attempt, not a
    guarantee, and an unfilled one becomes a market order, so whether
    MAKER MODE is on changes nothing here and must never be consulted -
    that exact confusion is what certified an 0.80%-wide band of losing
    spacings as fee-safe (see test_fee_floor_worst_case.py).

    Maker-ONLY mode makes a different claim. It does not hope the maker
    order fills; it deletes the market fallback out of grid_buy() and
    grid_sell(), so a leg that does not fill as a maker does not fill at
    all. While that is genuinely on, the taker leg is not a worst case the
    bot can reach - it is a path that no longer exists - and the honest
    worst case is the maker leg.

    Two guards keep that from becoming the old bug under a new name:

      * is_maker_only_active() FAILS CLOSED, so an unreadable toggle
        prices taker rather than assuming the cheap path.
      * The maker rate has to be a MEASURED one - _cached_real_maker_fee_rate
        comes from the account's real Coinbase fee tier. A floor may not
        assume what nothing has measured, so without it, taker again.

    And the result is clamped to the taker leg, because no arrangement of
    maker orders can ever cost MORE than paying taker on both legs.
    """
    taker_leg = (await get_effective_round_trip_fee_rate()) / 2
    if _cached_real_maker_fee_rate is None:
        return taker_leg
    if not await is_maker_only_active():
        return taker_leg
    return min(float(_cached_real_maker_fee_rate), taker_leg)


# ── THE DEADLOCK THIS RESOLVES ──────────────────────────────────────────
#
# Two cost models decide whether a branch trades, and until 2026-09-25 they
# did not have to agree:
#
#   fee_safe_floor_pct()   step must clear FEES plus a margin        1.70%
#   _net_edge_gate_ok()    step must clear fees + SPREAD + ADVERSE   2.16%
#
# A branch may therefore sit at a spacing the floor permits and the gate can
# never pass. That is not a near miss that resolves itself - it is a
# permanent deadlock, because nothing in the system moves the step.
#
# Live, on this account: NEAR-USD pinned at a 2.00% step, reaching its buy
# trigger constantly, refused 171 times in a row with "net edge -0.164% - a
# 2.00% target does not clear 2.16% of costs". Every refusal correct. The
# branch needed a 2.36% step and had no way to get there, so the capital
# behind it simply never traded.
#
# The fix is NOT to relax the gate. The gate is the only component in this
# stack whose numbers have been right throughout. It is to make the SPACING
# clear the bar the gate actually enforces, which is what the fee floor was
# always trying to do and was only doing for one of the three costs.
#
# This is also what the evidence already says to do. Over 90 days of real
# candles a wider step beat a tighter one on six of seven coins ($128.30 vs
# $23.24 at 1.70%) AND produced more round trips, not fewer - selling early
# resets the reference upward and costs the next entry.
#
# Strictly one-directional: it only ever WIDENS, never tightens, and it is
# bounded, so a wild volatility reading cannot walk a branch out to an
# absurd spacing. If the required step exceeds the bound the branch keeps
# refusing, which is the correct outcome - some coins are too expensive to
# grid at any sane spacing, and that is an answer, not a failure.
GATE_CLEARING_MAX_PCT = float(os.getenv("GRID_GATE_CLEARING_MAX_PCT", "0.06"))
AUTO_WIDEN_ENV_VAR = "GRID_AUTO_WIDEN"

# THE FLEET'S MINIMUM STEP, set from measurement rather than from habit.
#
# Every branch sat at 2.00% because that is what it was created with, not
# because anything measured said 2.00% was right. Measured on 14 days of real
# hourly candles across the 8 live coins - counting completed round trips at
# each spacing and pricing them at the maker-only cost of 1.37%:
#
#     step   trips/14d   net each   fleet total
#     1.0%      34        -0.37%      -$8.71   more trades, every one a loss
#     2.0%      17        +0.63%      +$7.41
#     2.5%      12        +1.13%      +$9.39
#     3.0%      10        +1.63%     +$11.28
#
# Tighter spacing trades MORE and earns LESS; below about 1.5% it is strictly
# negative. That agrees with the 90-day backtest this fleet already ran, where
# a wider step beat a tighter one on six of seven coins AND produced more
# round trips, not fewer - selling early resets the reference upward and costs
# the next entry.
#
# Applied as a one-directional floor, like every other spacing rule here: a
# branch below it is raised, a branch above it is left alone.
FLEET_MIN_STEP_PCT = float(os.getenv("GRID_FLEET_MIN_STEP_PCT", "0.025"))


def auto_widen_enabled() -> bool:
    """Whether spacing may widen to clear the gate. ON unless switched off.

    Defaults ON because the alternative is the deadlock above: a branch that
    refuses every buy forever at a spacing it cannot change. The gate is
    untouched and still refuses anything that does not clear, so the worst
    case here is a wider step, which is the direction the backtest evidence
    already points.
    """
    raw = (os.getenv(AUTO_WIDEN_ENV_VAR) or "").strip().strip('"').strip("'").lower()
    return raw not in {"0", "false", "no", "off"}


async def gate_clearing_floor_pct(session, product_id: str, current_step: float,
                                  slice_usd: float):
    """The smallest step that would actually pass the net-edge gate.

    Derived by asking the gate itself rather than re-deriving its internals:
    net edge is (step - costs), so the step that clears is

        current_step - net_edge + margin

    That inverts the gate exactly, whatever it happens to be charging for
    spread and adverse selection today, and cannot drift from it the way a
    second copy of the formula would.

    Returns (required_step, detail) or (None, reason) when it cannot be
    computed - in which case the caller must leave the spacing alone, since
    widening on a number nobody could produce is the same class of mistake
    as the one this exists to fix.
    """
    try:
        import crypto_nine_coin_scanner as scanner
        bid, ask, bid_depth, ask_depth = await engine.get_book_top_and_depth(session, product_id)
        if bid is None or ask is None:
            return None, "order book unreadable"
        swing = await engine.get_average_hourly_swing_pct(session, product_id)
        fee_round_trip = (await worst_case_leg_fee_rate()) * 2
        ok, reason, detail = scanner.evaluate_grid_step(
            product_id, current_step, swing,
            best_bid=bid, best_ask=ask,
            bid_depth_usd=bid_depth, ask_depth_usd=ask_depth,
            slice_usd=slice_usd, fee_round_trip=fee_round_trip,
        )
        edge = (detail or {}).get("net_edge_pct")
        if edge is None:
            # The gate refused for a reason that is not about the step at
            # all - spread too wide, or the slice too big for the book.
            # Widening cannot fix either, and would only mean losing more
            # per trade on a book that still cannot absorb it.
            return None, f"not a spacing problem: {reason}"
        if ok:
            return None, "already clears"
        return current_step - edge + TARGET_NET_MARGIN_PCT, {
            "net_edge_pct": edge, "reason": reason,
            "spread_pct": (detail or {}).get("spread_pct"),
        }
    except Exception as e:
        return None, f"could not compute: {type(e).__name__}: {e}"


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
    async with get_session_factory()() as db:
        result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == GRID_BOT_MODE_KEY))
        row = result.scalar_one_or_none()
        if row is None:
            return True
        return bool(row.base_capital and row.base_capital >= 1.0)


async def set_grid_bot_active(enabled: bool):
    async with get_session_factory()() as db:
        result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == GRID_BOT_MODE_KEY))
        row = result.scalar_one_or_none()
        if row is None:
            row = TradingBotState(bot_name=GRID_BOT_MODE_KEY, base_capital=0.0)
            db.add(row)
        row.base_capital = 1.0 if enabled else 0.0
        await db.commit()


async def is_dynamic_spacing_active() -> bool:
    """Real, DB-persisted toggle for fee-tier-aware dynamic grid spacing
    (see compute_dynamic_grid_pct). Defaults to ON now - per the account
    owner's explicit "turn this ON" after being shown the real mechanism.
    Confirmed genuinely low-risk before flipping the default, not just
    assumed: TARGET_NET_MARGIN_PCT (DEFAULT_GRID_PCT - ROUND_TRIP_FEE_RATE
    = 0.002) plus a real base-tier taker rate doubled (0.004 * 2 = 0.008)
    computes to EXACTLY 0.01 - byte-identical to today's fixed 1% default
    - so this is a real no-op unless the account's own real Coinbase fee
    tier has genuinely improved below the base rate this constant
    assumes, and even then it only ever NARROWS spacing (trades more
    often at smaller moves, same real net margin per cycle), never widens
    it or takes on more risk. compute_dynamic_grid_pct() also fails OPEN
    (returns today's exact default) on any real fee-tier fetch failure.
    Same "flip the unset default, no live dashboard access from this
    sandbox" precedent already used elsewhere in this codebase - an
    explicit dashboard toggle click still wins over this default in
    either direction afterward."""
    async with get_session_factory()() as db:
        result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == DYNAMIC_SPACING_MODE_KEY))
        row = result.scalar_one_or_none()
        if row is None:
            return True
        return bool(row.base_capital and row.base_capital >= 1.0)


async def set_dynamic_spacing_active(enabled: bool):
    async with get_session_factory()() as db:
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
    shadow-mode period first.

    FAILS CLOSED as of 2026-09-26. A missing row used to return True, so
    a fresh database - or a deleted row - silently switched automatic
    capital rotation ON with nobody having asked for it. A control that
    turns itself on when its own state cannot be found is not a switch.
    Every other toggle in this module (maker-only, auto-widen) already
    fails closed; this one now matches. The behaviour when the row EXISTS
    is unchanged, so an account that deliberately enabled it keeps it.

    A missing row is also logged rather than assumed, because "off"
    because-nobody-set-it and "off" because-somebody-set-it are different
    facts and only one of them is a decision.
    """
    async with get_session_factory()() as db:
        result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == GRID_AUTO_ROTATE_MODE_KEY))
        row = result.scalar_one_or_none()
        if row is None:
            log.info("[GRID] auto-rotate has no stored state - treating as OFF. "
                     "Nothing has enabled it; set it explicitly to turn it on.")
            return False
        return bool(row.base_capital and row.base_capital >= 1.0)


async def set_grid_auto_rotate_active(enabled: bool):
    async with get_session_factory()() as db:
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
        # Fail to the REAL fee-safe floor, not the old fixed default - a
        # lookup failure must never hand back a spacing that cannot cover
        # the real round trip.
        return await fee_safe_floor_pct(), None, None
    round_trip_fee_rate = taker * 2  # every real order this codebase places is a MARKET (taker) order
    grid_pct = max(MIN_DYNAMIC_GRID_PCT, TARGET_NET_MARGIN_PCT + round_trip_fee_rate)
    return grid_pct, tier_name, taker


async def is_avg_swing_spacing_active() -> bool:
    """Real, DB-persisted toggle for average-swing-based dynamic grid
    spacing (see compute_avg_swing_grid_pct and the real evidence in
    AVG_SWING_SPACING_MODE_KEY's own comment above). Defaults to ON - a
    real, deliberate default flip per the account owner's own direct
    request, backed by real 30-day backtest evidence, not just a
    low-risk no-op the way the fee-tier feature's own default flip was.
    Same "flip the unset default, no live dashboard access from this
    sandbox" precedent used elsewhere in this codebase - an explicit
    dashboard toggle click still wins over this default in either
    direction afterward."""
    async with get_session_factory()() as db:
        result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == AVG_SWING_SPACING_MODE_KEY))
        row = result.scalar_one_or_none()
        if row is None:
            return True
        return bool(row.base_capital and row.base_capital >= 1.0)


async def set_avg_swing_spacing_active(enabled: bool):
    async with get_session_factory()() as db:
        result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == AVG_SWING_SPACING_MODE_KEY))
        row = result.scalar_one_or_none()
        if row is None:
            row = TradingBotState(bot_name=AVG_SWING_SPACING_MODE_KEY, base_capital=0.0)
            db.add(row)
        row.base_capital = 1.0 if enabled else 0.0
        await db.commit()


async def compute_avg_swing_grid_pct(session, product_id: str, multiplier: float = None) -> tuple:
    """Real, live average-swing-based grid_pct for one branch's own coin -
    fetches its real recent hourly True-Range average
    (engine.get_average_hourly_swing_pct) and sizes spacing at `multiplier`
    (defaulting to the real validated AVG_SWING_SPACING_MULTIPLIER, 1.5x,
    when not given) times that. The caller passes a branch's own real
    self_tuned_multiplier here when it has one - see
    _maybe_self_tune_branch_spacing - so a branch with a genuinely rough
    or genuinely strong recent real track record can trade at its own,
    real-evidence-adjusted spacing instead of the flat global default.
    Unlike
    compute_dynamic_grid_pct above (one spacing shared by every branch,
    tied to the account's own fee tier), this produces a DIFFERENT real
    grid_pct per branch, sized off that specific coin's own real recent
    behavior.

    Real bug found and fixed after the account owner spotted real
    completed round trips buying and selling for almost the identical
    price (ATOM $4.97 -> $4.97, ARB $4.98 -> $4.98) - real, guaranteed
    losses once Coinbase's real ~0.8% round-trip fee is subtracted. Root
    cause: this originally floored grid_pct at the bare MIN_DYNAMIC_GRID_PCT
    (0.3%) - a constant that's only ever safe for compute_dynamic_grid_pct
    above, which is fee-AWARE by construction (its own formula already adds
    the real round-trip fee rate before applying that floor, so it never
    actually reaches 0.3% in practice). This function computes grid_pct
    purely from a coin's own volatility, with nothing fee-aware in it at
    all - a real, genuinely calm coin's avg_swing_pct * 1.5 can land well
    below 0.3%, and once it does, EVERY completed cycle at that spacing is
    a guaranteed real loss before it even opens, since the gross price move
    can never cover the real fee. Fixed by flooring at the SAME real
    fee-safe minimum compute_dynamic_grid_pct's own formula already uses
    (TARGET_NET_MARGIN_PCT + engine.ROUND_TRIP_FEE_RATE, which equals
    today's live DEFAULT_GRID_PCT exactly at the base fee tier) - a calm
    coin now floors at the same real, already-profitable 1% every branch
    already traded at before this feature existed, instead of an unsafe
    0.3% nothing could actually profit at. MIN_DYNAMIC_GRID_PCT itself is
    kept as an even lower defensive backstop underneath that, unreachable
    in practice, same role it already plays for the fee-tier feature.

    Fails OPEN on a real fetch failure - returns (DEFAULT_GRID_PCT, None),
    matching every other "don't block real trading on missing data" gate
    in this codebase."""
    avg_swing_pct = await engine.get_average_hourly_swing_pct(session, product_id, count=AVG_SWING_LOOKBACK_HOURS)
    if avg_swing_pct is None:
        return DEFAULT_GRID_PCT, None
    effective_multiplier = multiplier if multiplier is not None else AVG_SWING_SPACING_MULTIPLIER
    # Priced against the REAL fee rate this account pays, not the old
    # hardcoded assumption - a branch can never be set to a spacing whose
    # full round trip is a guaranteed real loss. See
    # CONSERVATIVE_ROUND_TRIP_FEE_RATE for the bug this fixes.
    fee_safe_floor = await fee_safe_floor_pct()
    grid_pct = max(fee_safe_floor, avg_swing_pct * effective_multiplier)
    return grid_pct, avg_swing_pct


async def get_grid_branches() -> list:
    async with get_session_factory()() as db:
        result = await db.execute(select(CryptoGridBranch).order_by(CryptoGridBranch.bot_name))
        return list(result.scalars().all())


async def get_grid_branch_claimed_coins() -> set:
    """Real coins currently claimed by an ACTIVE grid branch - a disabled
    branch (active=False) releases its claim, same convention every
    other claimed-contract/coin check in this codebase already uses."""
    async with get_session_factory()() as db:
        result = await db.execute(select(CryptoGridBranch.product_id).where(CryptoGridBranch.active == True))
        return {row[0] for row in result.all()}


async def get_grid_allocated_total() -> float:
    """Real total capital already committed across EVERY grid branch
    (active or paused - a paused branch releases its coin claim but its
    real allocated_usd stays committed, same as any other branch type in
    this codebase).

    NOT a free-cash figure, and deliberately no longer subtracted from
    the real USD wallet balance anywhere. allocated_usd is cost basis
    that is never debited at buy time, so it straddles cash still in the
    wallet and coin already bought - subtracting it from a real balance
    double-counts the deployed half. Both real free-cash callers
    (get_real_free_cash_usd here, spendable_for_spawn in
    routers/trading_dashboard.py) use get_grid_undeployed_reserve_total()
    instead. Kept for reporting/analytics only."""
    async with get_session_factory()() as db:
        result = await db.execute(select(CryptoGridBranch))
        return sum(b.allocated_usd for b in result.scalars().all())


async def get_grid_undeployed_reserve_total() -> float:
    """Real USD a grid branch still needs held in RESERVE for the levels
    it hasn't bought yet - its allocation MINUS the real cost basis it
    has already converted into coin.

    This exists because subtracting a branch's FULL allocated_usd from
    the real USD wallet (what get_real_free_cash_usd used to do) double-
    counts every dollar already spent on coin. A branch that has bought
    its slices no longer competes for that USD - the money physically
    left the wallet at buy time, so the real balance already reflects it.
    Subtracting it a second time understates real free cash by exactly
    the deployed cost basis.

    Confirmed against real production numbers (2026-09-04): a real
    $881.00 USD wallet with $837.94 of grid allocation, of which
    ~$625.89 was already sitting in 6 real open slices, reported just
    $43.06 free - while ~$668.95 was genuinely deployable. The bot was
    refusing to put roughly $626 of its own real money to work.

    Deliberately uses each slice's own cost basis (qty * entry_price -
    the real USD that actually left the wallet), never live market
    value: a slice that has since gained doesn't free up extra USD, and
    one that has lost doesn't owe any back. Cost basis is the only
    figure that answers "how much of this allocation is no longer cash."

    Clamped at 0 per branch - a branch whose slices cost more than its
    own allocation (real fee/rounding drift) needs no reserve at all,
    and must never be allowed to ADD phantom free cash to the total.

    This can only ever raise reported free cash toward what the real
    wallet already holds, never above it, and it removes no safety:
    every unfilled level stays fully reserved here, and
    engine.place_market_buy() still clamps any real order to the live
    wallet balance immediately before submitting."""
    async with get_session_factory()() as db:
        branches = (await db.execute(select(CryptoGridBranch))).scalars().all()
        slices = (await db.execute(select(CryptoGridSlice))).scalars().all()

    deployed_by_bot = {}
    for s in slices:
        if s.qty is None or s.entry_price is None:
            continue
        deployed_by_bot[s.bot_name] = deployed_by_bot.get(s.bot_name, 0.0) + s.qty * s.entry_price

    return round(sum(
        max(0.0, (b.allocated_usd or 0.0) - deployed_by_bot.get(b.bot_name, 0.0))
        for b in branches
    ), 2)


async def get_grid_holdings_market_value():
    """Real live market value of every coin Grid Bot is ACTUALLY holding
    right now - the sum of qty * live price across every open slice on
    every branch, priced once per distinct product_id (not once per
    branch, so several branches sharing a coin never cost extra real
    API calls). Read-only; never places an order.

    Deliberately NOT allocated_usd. allocated_usd is a cost-basis figure
    that is never debited at buy time (see _grid_branch_real_equity), so
    it is part cash-still-sitting-in-the-real-USD-wallet and part coin -
    adding it on top of a real USD balance would double-count the cash
    half. The market value of the OPEN SLICES is precisely the piece
    that genuinely is NOT in the USD wallet any more, which makes it
    exactly what a real net-worth figure has to add to that balance.

    Returns (market_value, complete). complete is False when a real
    price fetch failed for a branch that genuinely holds open slices -
    so a caller can refuse to publish an understated total rather than
    quietly passing off a partial number as the real one.
    """
    branches = await get_grid_branches()
    holdings = {}          # bot_name -> (product_id, slices)
    needed_products = set()
    for b in branches:
        slices = await get_grid_slices(b.bot_name)
        if slices:
            holdings[b.bot_name] = (b.product_id, slices)
            needed_products.add(b.product_id)
    if not needed_products:
        return 0.0, True

    live_prices = {}
    async with engine.aiohttp.ClientSession() as session:
        for product_id in needed_products:
            price, _atr = await engine.get_price_and_volatility(session, product_id)
            live_prices[product_id] = price

    total = 0.0
    complete = True
    for _bot_name, (product_id, slices) in holdings.items():
        price = live_prices.get(product_id)
        if price is None:
            complete = False
            continue
        total += sum(s.qty * price for s in slices)
    return round(total, 2), complete


async def get_real_free_cash_usd():
    """Real, honest 'how much can I actually deploy into a NEW grid
    branch right now' figure - per the account owner's own direct
    complaint about creating branches "blindly" with no idea what's
    really available. Real Coinbase USD balance minus real locked
    profit minus every FLAT family-tree branch's own allocated_usd (a
    branch holding a position has already deployed that money into
    crypto, not cash - same real distinction
    routers/trading_dashboard.py's own spendable_for_spawn already
    makes) minus every Grid Bot branch's still-UNSPENT reserve (see
    get_grid_undeployed_reserve_total - deliberately NOT the full
    allocated_usd, which double-counts every dollar a branch has
    already converted into coin and physically removed from the
    wallet; every unfilled level is still reserved in full).

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

    async with get_session_factory()() as db:
        tree_result = await db.execute(select(CryptoTreeBranch))
        tree_branches = tree_result.scalars().all()
        open_bots_result = await db.execute(select(BotPosition.bot))
        open_bots = {row[0] for row in open_bots_result.all()}
        tree_flat_allocated = sum(b.allocated_usd for b in tree_branches if b.bot_name not in open_bots)

    # Only each grid branch's still-UNSPENT reserve competes for real USD -
    # a dollar already converted to coin left the wallet at buy time, so
    # real_balance has already accounted for it and subtracting the full
    # allocation again would count it twice. See
    # get_grid_undeployed_reserve_total() for the real production numbers
    # that exposed this (~$626 of the account's own money reported as
    # unavailable). Every unfilled level is still fully reserved.
    grid_reserve_total = await get_grid_undeployed_reserve_total()

    return round(real_balance - locked_usd - tree_flat_allocated - grid_reserve_total, 2)


async def get_grid_spend_ceiling_usd():
    """How much the GRID may deploy right now. Returns (ceiling, reason).

    get_real_free_cash_usd() above answers "how much cash exists"; this
    answers "how much of it is mine". They were the same number until two
    loops started sharing one wallet, at which point the first one to look
    could take everything and the other would find nothing - observed live
    on 2026-09-24, when the fallback btc_compound loop had converted the
    whole balance into BTC and the freshly-deployed grid fleet ran a clean,
    healthy, entirely idle loop against $0.29.

    See crypto_cash_allocator for the rule. Returns (None, reason) when the
    balance could not be read, which callers must treat as "do not deploy",
    distinct from a real $0.00.
    """
    import crypto_cash_allocator as allocator
    free_cash = await get_real_free_cash_usd()
    return allocator.spend_ceiling(allocator.GRID, free_cash)


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


def _split_usd_evenly(amount: float, count: int) -> list[float]:
    """Split a cent-precision amount without creating or losing a cent."""
    if count <= 0:
        raise ValueError("count must be positive")
    amount_cents = round(amount * 100)
    if amount_cents <= 0 or abs(amount * 100 - amount_cents) > 1e-6:
        raise ValueError("amount must be a positive value with at most two decimal places")
    base_cents, extra_cents = divmod(amount_cents, count)
    return [
        (base_cents + (1 if index < extra_cents else 0)) / 100
        for index in range(count)
    ]


# ============================================================
# Real, live "promote a backtested candidate" mechanism for the Grid
# Level/Spacing Comparison (crypto_selection_backtest.py's
# GRID_LEVEL_SPACING_CANDIDATES / run_grid_level_spacing_comparison) and
# Strategy Lab's own matching entries - the account owner's direct
# follow-up right after finally getting a real answer out of those two
# tools: "yeah but it doesn't let me pick something better well if it's
# nun thing better than what I have give me 2 more better option to
# choose." Two real gaps closed here: (1) there was no way to actually
# PUSH a winning candidate live, only backtest it - unlike exit_mode/
# trailing_stop_pct above, which already have this exact promote pattern;
# (2) 2 more real candidates, unconditionally (not contingent on first
# confirming the existing 3 beat the live default, which this sandbox has
# no live network access to confirm anyway).
#
# Mirrors GRID_LEVEL_SPACING_CANDIDATES in crypto_selection_backtest.py
# BY VALUE, not by import - that module imports FROM this one (engine-
# style, for its own real live spacing/fee helpers), so the reverse would
# be a circular import. Same duplicate-by-value discipline already used
# for STOP_HIT_REVERSAL_TARGET_PCT/SR_LOOKBACK_HOURS elsewhere in this
# codebase. If this dict is ever revised, crypto_selection_backtest.py's
# own copy must be updated to match, or a "promoted" candidate here could
# silently differ from what was actually backtested there.
GRID_LEVEL_SPACING_CANDIDATES = {
    "3_levels_2.0pct": {"num_levels": 3, "grid_pct": 0.020},
    "3_levels_2.5pct": {"num_levels": 3, "grid_pct": 0.025},
    "5_levels_2.0pct": {"num_levels": 5, "grid_pct": 0.020},
    # The 2 new real candidates added per the account owner's own direct
    # request, unconditionally - a tighter fewer-levels variant (4
    # levels/1.5%, between the existing 5-level/2.0% and 3-level/2.0%
    # candidates) and a wider one (3 levels/3.0%, continuing the real
    # direction the account owner's own pasted critique argued for -
    # narrower candidates trending worse, wider trending better in the
    # existing sweep - one step past the widest existing candidate).
    "4_levels_1.5pct": {"num_levels": 4, "grid_pct": 0.015},
    "3_levels_3.0pct": {"num_levels": 3, "grid_pct": 0.030},
}
GRID_SPACING_OVERRIDE_LEVELS = ["live_default"] + list(GRID_LEVEL_SPACING_CANDIDATES.keys())
GRID_SPACING_OVERRIDE_KEY = "crypto_grid_live_spacing_override"


async def get_live_grid_spacing_override() -> str:
    """Which real, backtested Grid Level/Spacing candidate the live Grid
    Bot is currently promoted to, or "live_default" for today's real
    per-branch behavior completely unchanged (per-allocation num_levels
    via _safe_num_levels_for_allocation, dynamic avg-swing/fee-tier
    spacing - see run_grid_branch_cycle). DB-persisted the same generic
    TradingBotState-index pattern get_live_exit_mode()/
    get_live_trailing_stop_pct() already use. Falls back to
    "live_default" on any stale/out-of-range stored value (e.g.
    GRID_LEVEL_SPACING_CANDIDATES gets revised later) - there's currently
    nothing else it could correctly mean."""
    async with get_session_factory()() as db:
        result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == GRID_SPACING_OVERRIDE_KEY))
        row = result.scalar_one_or_none()
        if row is None or row.base_capital is None:
            return "live_default"
        level = int(row.base_capital)
        if 0 <= level < len(GRID_SPACING_OVERRIDE_LEVELS):
            return GRID_SPACING_OVERRIDE_LEVELS[level]
        return "live_default"


async def set_live_grid_spacing_override(label: str):
    """label="live_default" reverts every real grid branch back to
    today's unchanged per-allocation/dynamic-spacing behavior; otherwise
    must be one of GRID_LEVEL_SPACING_CANDIDATES - a real, backtested
    config, never an invented one."""
    if label not in GRID_SPACING_OVERRIDE_LEVELS:
        raise ValueError(
            f"unknown grid spacing override {label!r} - must be 'live_default' or one of "
            f"{list(GRID_LEVEL_SPACING_CANDIDATES.keys())} (only a real, backtested config can go live)"
        )
    level = float(GRID_SPACING_OVERRIDE_LEVELS.index(label))
    async with get_session_factory()() as db:
        result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == GRID_SPACING_OVERRIDE_KEY))
        row = result.scalar_one_or_none()
        if row is None:
            row = TradingBotState(bot_name=GRID_SPACING_OVERRIDE_KEY, base_capital=level)
            db.add(row)
        else:
            row.base_capital = level
        await db.commit()


async def _effective_num_levels(allocated_usd: float) -> int:
    """The real levels count a branch should use right now - today's
    per-allocation default (_safe_num_levels_for_allocation), capped
    further by a real promoted Grid Level/Spacing override's own levels
    count when one is active. Never raises the count above the real
    per-allocation default - a promoted candidate can only ever make a
    branch use FEWER, bigger real slices, matching every real candidate's
    own "fewer levels" shape; it never asks a branch to run MORE levels
    than its own allocation can safely support."""
    base = _safe_num_levels_for_allocation(allocated_usd)
    override_label = await get_live_grid_spacing_override()
    if override_label == "live_default":
        return base
    cap = GRID_LEVEL_SPACING_CANDIDATES[override_label]["num_levels"]
    return max(1, min(base, cap))


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

    # The same check across the OTHER system. get_grid_branch_claimed_coins
    # above only knows about grid branches, so until this existed a grid
    # branch could be created on a coin a family-tree branch was already
    # holding - two systems tracking their own qty against one pooled
    # Coinbase balance, which is the structural gap behind this repo's
    # phantom positions and DB-vs-Coinbase SHORTFALLs.
    import crypto_coin_claims as claims
    tree_claimed = await claims.claimed_by_other(claims.GRID)
    if claims.normalize_product(product_id) in tree_claimed:
        raise ValueError(
            f"{product_id} is already held by a family-tree branch. Both systems share one "
            f"Coinbase balance for a coin, so two branches on it would each track their own "
            f"qty against the same tokens. Pick another coin, or close the tree branch first."
        )

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

    num_levels = await _effective_num_levels(allocated_usd)

    async with get_session_factory()() as db:
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
    async with get_session_factory()() as db:
        result = await db.execute(select(CryptoGridBranch).where(CryptoGridBranch.bot_name == bot_name))
        branch = result.scalar_one_or_none()
        if branch is None:
            raise ValueError(f"no grid branch named {bot_name}")
        branch.allocated_usd += amount
        branch.num_levels = await _effective_num_levels(branch.allocated_usd)
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
    async with get_session_factory()() as db:
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

        branch.num_levels = await _effective_num_levels(branch.allocated_usd)
        branch.peak_equity = branch.allocated_usd
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
    async with get_session_factory()() as db:
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

    async with get_session_factory()() as db:
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


async def reallocate_grid_cash_across_adaptive_fleet(from_bot_name: str, amount: float) -> dict:
    """Move one flat branch reservation across all nine fleet products atomically.

    This is an explicit manual allocation override, not adaptive-stage
    qualification and not a market order. All missing-product prices are
    fetched before mutation; the source debit and every destination credit
    then commit in one database transaction.
    """
    if os.getenv("STOP_TRADING", "false").lower() == "true":
        raise ValueError("STOP_TRADING is set - new capital deployment is paused")

    fleet_products = [product_id for product_id, _gate in ADAPTIVE_FLEET_STAGES]
    allocations = _split_usd_evenly(amount, len(fleet_products))
    if min(allocations) < MIN_TRADE_USD:
        raise ValueError(
            f"${amount:.2f} is too small to split across {len(fleet_products)} products "
            f"at the ${MIN_TRADE_USD:.2f} live order minimum"
        )

    async with get_session_factory()() as db:
        result = await db.execute(select(CryptoGridBranch))
        preflight_branches = list(result.scalars().all())
    preflight_by_product = {branch.product_id: branch for branch in preflight_branches}
    missing_products = [product_id for product_id in fleet_products if product_id not in preflight_by_product]

    reference_prices = {}
    async with engine.aiohttp.ClientSession() as session:
        for product_id in missing_products:
            price, _atr = await engine.get_price_and_volatility(session, product_id)
            if price is None:
                raise ValueError(f"could not fetch a real live price for {product_id} right now - no capital was moved")
            reference_prices[product_id] = price

    override_label = await get_live_grid_spacing_override()

    def levels_for(allocation: float) -> int:
        levels = _safe_num_levels_for_allocation(allocation)
        if override_label != "live_default":
            levels = min(levels, GRID_LEVEL_SPACING_CANDIDATES[override_label]["num_levels"])
        return max(1, levels)

    allocation_by_product = dict(zip(fleet_products, allocations))
    destination_rows = []
    async with get_session_factory()() as db:
        async with db.begin():
            result = await db.execute(select(CryptoGridBranch).with_for_update())
            branches = list(result.scalars().all())
            source = next((branch for branch in branches if branch.bot_name == from_bot_name), None)
            if source is None:
                raise ValueError(f"no grid branch named {from_bot_name}")
            if source.product_id in allocation_by_product:
                raise ValueError("source branch cannot also be one of the nine fleet destinations")
            if source.locked:
                raise ValueError(f"{from_bot_name} is locked - unlock it first before moving real cash out of it")
            slices_result = await db.execute(
                select(CryptoGridSlice.id).where(CryptoGridSlice.bot_name == from_bot_name).limit(1)
            )
            if slices_result.first() is not None:
                raise ValueError(f"{from_bot_name} has real open slices - can only move cash from a FLAT branch")
            if round(amount * 100) > round((source.allocated_usd or 0.0) * 100):
                raise ValueError(
                    f"{from_bot_name} only has ${source.allocated_usd:.2f} real allocated - can't move ${amount:.2f}"
                )

            by_product = {}
            for branch in branches:
                if branch.product_id in by_product:
                    raise ValueError(f"multiple grid branches already exist for {branch.product_id} - no capital was moved")
                by_product[branch.product_id] = branch

            used_nums = {
                int(branch.bot_name.rsplit("_", 1)[-1])
                for branch in branches if branch.bot_name.rsplit("_", 1)[-1].isdigit()
            }
            next_num = 1
            for product_id in fleet_products:
                allocation = allocation_by_product[product_id]
                destination = by_product.get(product_id)
                if destination is None:
                    if product_id not in reference_prices:
                        raise ValueError(f"fleet changed during preflight for {product_id} - no capital was moved")
                    while next_num in used_nums:
                        next_num += 1
                    destination = CryptoGridBranch(
                        bot_name=f"crypto_grid_{next_num}",
                        product_id=product_id,
                        allocated_usd=allocation,
                        active=True,
                        grid_pct=DEFAULT_GRID_PCT,
                        num_levels=levels_for(allocation),
                        reference_price=reference_prices[product_id],
                    )
                    db.add(destination)
                    used_nums.add(next_num)
                    next_num += 1
                else:
                    destination.allocated_usd = round((destination.allocated_usd or 0.0) + allocation, 2)
                    destination.active = True
                    destination.num_levels = levels_for(destination.allocated_usd)
                destination_rows.append((destination, allocation))

            source.allocated_usd = round(source.allocated_usd - amount, 2)
            source.num_levels = levels_for(source.allocated_usd)
            await db.flush()
            source_remaining = source.allocated_usd
            source_product_id = source.product_id
            result_rows = [
                {
                    "bot_name": destination.bot_name,
                    "product_id": destination.product_id,
                    "amount": allocation,
                    "destination_allocated_usd": round(destination.allocated_usd, 2),
                }
                for destination, allocation in destination_rows
            ]

    for row in result_rows:
        await _log_activity_safe(
            row["bot_name"], row["product_id"], "REALLOCATE",
            f"Received ${row['amount']:.2f} from {from_bot_name} in an explicit nine-coin fleet allocation",
        )
    await _log_activity_safe(
        from_bot_name, source_product_id, "REALLOCATE",
        f"Moved ${amount:.2f} of idle reserved cash across the nine-coin fleet by explicit manual override",
    )
    log.info(f"[GRID] Moved ${amount:.2f} atomically from {from_bot_name} across all nine adaptive-fleet products")
    return {
        "from_bot_name": from_bot_name,
        "amount": round(amount, 2),
        "source_remaining_allocated_usd": round(source_remaining, 2),
        "manual_override": True,
        "orders_placed": False,
        "allocations": result_rows,
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

    async with get_session_factory()() as db:
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

    async with get_session_factory()() as db:
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


# Real, absolute "genuine edge" floor - per the account owner's direct,
# urgent request after watching every real Grid Bot branch sit negative:
# "apparently is picking bad picks from the beginning... should only
# execute moves when it finds a genuine edge (+20-30% real advantage)."
# Confirmed true by reading the code directly: pick_best_ranked_coin_for_grid()
# and _best_available_coin_and_roi() below always picked the single
# BEST-RANKED real candidate with no absolute quality floor - if
# literally every coin's real latest backtested ROI was negative, these
# functions still confidently returned "the best of the worst" with
# nothing distinguishing that from a real, earned opportunity. Every
# automatic capital-deployment path in this file (the $20 Quick Buy
# auto-pick, the auto-deploy-free-cash sweep, the auto-rotate sweep, and
# the real move-cash-candidate ranking) funnels through these same two
# functions, so this one fix covers all of them - "across the board, all
# the time," per the account owner's own words.
#
# MIN_REQUIRED_ROI_PCT (20% default) is the low end of the account
# owner's own stated 20-30% range, and deliberately not an invented
# number - it's a real, already-achieved figure in this account's own
# backtest history (USO +21.6% ROI/74.2% win rate, BLUR-USD +21.8% ROI -
# see the real coin-selection backtest results referenced throughout
# this session), so it's a real bar, not a fantasy one. A coin must
# clear BOTH the existing relative ranking AND this absolute floor
# before any automatic path is allowed to deploy real capital into it -
# when NOTHING clears it, every one of those paths correctly does
# nothing this cycle (real capital sits in cash) rather than always
# finding somewhere, however mediocre, to go.
MIN_REQUIRED_ROI_PCT = float(os.getenv("GRID_MIN_REQUIRED_ROI_PCT", "20.0"))

# The coins the account owner actually wants this fleet trading, selected
# 2026-09-25 from REAL GRID results and overridable without a deploy.
#
# SELECTED ON THE WRONG METRIC FIRST, and the correction matters more than
# the list. The first cut used backtest_one_coin's ranking - a TARGET/STOP
# replay, which measures DIRECTION. A grid does not care about direction;
# it profits from movement that returns. Ranking grid coins by directional
# ROI produced almost the inverse of the right answer:
#
#   BONK  directional ROI -40.87%  ->  grid +$12.94 on 7 trips, 100% wins
#   OP    directional ROI -31.92%  ->  grid  +$9.91 on 12 trips, 83% wins
#   NEAR  directional ROI +31.36%  ->  grid  +$0.95 on ONE trip in 30 days
#   FIL   directional ROI +17.74%  ->  grid  +$1.11 on TWO trips
#
# A coin can fall 40% in a month and still hand a grid seven clean round
# trips on the way down. A coin can rise steadily and hand it one.
#
# These are now ranked by measured grid net at 3 levels / 2.0% over the
# same 30 days. Combined: $134.76 across 119 round trips, against $21.05
# and ~30 trips for the previous set - 6.4x the profit and ~4x the
# frequency, on identical capital and settings.
#
# This exists because three independent filters stacked into a total
# shutout that day. A spread plan reported "OPEN NEW BRANCHES: 5, eligible
# coins: NONE" with $259.41 to deploy and 35 freshly ranked coins in hand:
#
#   MIN_REQUIRED_ROI_PCT (20%)   only 3 of 35 coins cleared it - UNI 40.0%,
#                                NEAR 36.1%, ARB 34.5%. FIL missed at 18.8%.
#   MANUAL_EXCLUDED_COINS        a hardcoded set that blocks UNI, the #1
#                                ranked coin, and STX, which earned real
#                                money in September.
#   TOP_N_ELIGIBLE_COINS (15)    cuts everything outside the top 15 by
#                                backtest ROI, which is what starved the
#                                generic nine-coin fallback below.
#
# Of the three survivors, one was hardcoded-blocked and one was already
# claimed. Every automatic layer was individually defensible and together
# they left nothing to trade.
#
# So these coins are judged on two questions only: has the operator
# excluded this by hand from the dashboard, and is another branch already
# on it. Backtest ROI still ORDERS the picks - ranked coins are offered
# first - it just no longer silently empties the pool. An automated filter
# may rank a deliberate choice lower; it may not veto it outright.
GRID_WORKING_SET = [
    c.strip().upper() for c in os.getenv(
        "GRID_WORKING_SET",
        "ETC-USD,FLOKI-USD,BCH-USD,DOGE-USD,BONK-USD,SHIB-USD,OP-USD,"
        "XRP-USD,INJ-USD,ALGO-USD,SEI-USD,AAVE-USD,ATOM-USD,SUI-USD",
    ).split(",") if c.strip()
]


async def _spread_candidate_coins(wanted: int):
    """Coins a spread may open a branch on, best first. Returns (coins, note).

    Tries the ranked picker first, because a coin with a real backtested
    ROI is a better choice than an arbitrary one. But that picker RAISES
    when no coin has a backtest run yet, or when none clears
    MIN_REQUIRED_ROI_PCT - a completely normal state on an account that
    has not run the backtests, and the reason a live spread created zero
    branches while reporting success.

    So the nine-coin fleet list backs it up. Those are the coins this
    system is built around; opening a branch on one is never absurd, and
    an empty fleet beats a perfect one that does not exist.

    Everything already claimed by a grid branch, excluded by the tree, or
    held by a tree branch is filtered out, so this can never hand back a
    coin two systems would then fight over.
    """
    import crypto_nine_coin_scanner as scanner
    import crypto_family_tree_bot as tree
    import crypto_coin_claims as claims

    ranked, note = [], ""
    for _ in range(wanted):
        try:
            pick = await pick_best_ranked_coin_for_grid()
        except Exception as e:
            note = f"Ranked picker had nothing ({e})."
            break
        if not pick or pick in ranked:
            break
        ranked.append(pick)

    try:
        excluded = await tree.get_effective_excluded_coins()
    except Exception:
        excluded = set()
    # WORKING SET coins answer to the operator's own dashboard toggle, not
    # to the automatic layers - see GRID_WORKING_SET for why. Anything the
    # operator excluded by hand still counts, for every coin.
    try:
        reasons = await tree.get_effective_excluded_coins_with_reasons()
        hand_excluded = {pid for pid, why in (reasons or {}).items()
                         if "dashboard" in str(why).lower()}
    except Exception:
        # Fail CLOSED for the working set only: without reasons we cannot
        # tell a hand exclusion from an automatic one, so honour them all
        # rather than risk trading a coin the operator killed by hand.
        hand_excluded = set(excluded)

    claimed = await get_grid_branch_claimed_coins()
    try:
        tree_held = await claims.claimed_by_other(claims.GRID)
    except Exception:
        tree_held = set()

    def eligible(pid, working_set=False):
        blocked = hand_excluded if working_set else excluded
        return (pid not in blocked and pid not in claimed
                and claims.normalize_product(pid) not in tree_held)

    out = [p for p in ranked if eligible(p)]
    # The operator's own chosen coins come next, BEFORE the generic fleet
    # list, and are judged only on hand exclusions and claims.
    for pid in GRID_WORKING_SET:
        if len(out) >= wanted:
            break
        if pid not in out and eligible(pid, working_set=True):
            out.append(pid)
    for pid in scanner.NINE_COINS:
        if len(out) >= wanted:
            break
        if pid not in out and eligible(pid):
            out.append(pid)

    if not note and len(out) < wanted:
        note = "Every remaining coin is already claimed or excluded."
    if ranked and len(out) > len(ranked):
        note = (note + " Filled the rest from the working set.").strip()
    elif not ranked and out:
        note = (note + " Used the operator's working set.").strip()
    return out, note or "Ranked picks available."


async def spread_capital_evenly(target_branches: int = 7, dry_run: bool = True) -> dict:
    """Level the fleet: pull cash out of over-funded FLAT branches and put
    it into new branches on unclaimed coins, until capital sits in roughly
    equal slices across `target_branches` coins.

    WHY THIS EXISTS

    Capital inside a branch's allocated_usd is not free cash - auto-deploy
    only ever builds from free cash. So a fleet that ends up with one
    branch holding nearly everything can never expand on its own: it has
    nothing to expand WITH. Observed live on 2026-09-24 with $578.61 of a
    $595.28 account sitting in one flat ARB-USD branch while the other six
    coins had nothing.

    WHAT LEVELLING IS AND IS NOT WORTH

    It does not raise expected profit. With capital fixed, splitting it
    trades bigger-but-rarer round trips for smaller-but-more-frequent ones
    and the two cancel almost exactly: at this account's 2.5% spacing and
    1.0% round trip, one coin and seven coins both model to the same
    dollars per day.

    What it buys is not being stranded. One coin holding everything fills
    all its levels on a single trend against it and then sits underwater
    with nothing left to trade. Seven coins cannot all trend against you
    at once. It also produces per-coin evidence far sooner, which is the
    only way to learn which coins are worth more capital later.

    SAFETY

    Only FLAT branches are touched - a branch holding open slices
    represents crypto already bought, not idle cash, and withdraw_from_
    grid_branch refuses it anyway. Nothing is sold and no order is placed;
    this is pure bookkeeping plus one live price fetch per new branch.

    dry_run=True (the default) returns the exact plan and changes nothing.
    """
    branches = await get_grid_branches()
    flat_by_name = {}
    async with get_session_factory()() as db:
        for b in branches:
            result = await db.execute(
                select(func.count(CryptoGridSlice.id)).where(CryptoGridSlice.bot_name == b.bot_name))
            if (result.scalar() or 0) == 0:
                flat_by_name[b.bot_name] = b

    free_cash = await get_real_free_cash_usd()
    if free_cash is None:
        return {"status": "unavailable", "detail": "Real free cash could not be read - "
                                                   "refusing to plan against an unknown balance.",
                "changed": False}

    # Everything that COULD be redistributed: cash already free, plus what
    # sits idle in flat branches. A branch holding slices is excluded
    # entirely - its allocation is not cash.
    flat_total = sum(b.allocated_usd for b in flat_by_name.values())
    held_branches = [b for b in branches if b.bot_name not in flat_by_name]
    pool = round(free_cash + flat_total, 2)

    # Hold back the same reserve auto-deploy holds back.
    #
    # The first version of this divided the WHOLE pool across the branches
    # and left free cash at exactly $0.00, which breaks two things. Fees
    # settle out of the USD balance, and an account with nothing spare
    # cannot pay one - a rejected fee is a stuck position. And
    # create_grid_branch runs its own free-cash check, so the last branch
    # in the run asks for its full share against a balance the previous
    # branches just emptied and is refused: the fleet ends one branch
    # short with a confusing "only $0.00 in real free spendable cash"
    # error, having already moved the money.
    reserve = max(0.0, GRID_CASH_RESERVE_USD)
    distributable = round(max(0.0, pool - reserve), 2)

    target_branches = max(1, int(target_branches))
    per_branch = round(distributable / target_branches, 2)

    if per_branch < MIN_TRADE_USD * 2:
        return {
            "status": "too_thin", "changed": False,
            "detail": (f"${pool:,.2f} less a ${reserve:,.2f} reserve leaves ${distributable:,.2f}, "
                       f"which is ${per_branch:,.2f} across {target_branches} branches - too thin "
                       f"to carry slices above the ${MIN_TRADE_USD:,.2f} minimum. Use fewer "
                       f"branches."),
            "pool_usd": pool, "reserve_usd": reserve,
            "distributable_usd": distributable, "per_branch_usd": per_branch,
        }

    plan = {"withdrawals": [], "top_ups": [], "new_branches": [], "untouched_holding": [
        {"bot_name": b.bot_name, "product_id": b.product_id,
         "allocated_usd": round(b.allocated_usd, 2),
         "reason": "holding open slices - not idle cash"} for b in held_branches]}

    # Level the flat branches that already exist, in either direction.
    for b in flat_by_name.values():
        delta = round(b.allocated_usd - per_branch, 2)
        if delta > 0.01:
            plan["withdrawals"].append({"bot_name": b.bot_name, "product_id": b.product_id,
                                        "from_usd": round(b.allocated_usd, 2),
                                        "to_usd": per_branch, "release_usd": delta})
        elif delta < -0.01:
            plan["top_ups"].append({"bot_name": b.bot_name, "product_id": b.product_id,
                                    "from_usd": round(b.allocated_usd, 2),
                                    "to_usd": per_branch, "add_usd": round(-delta, 2)})

    slots = target_branches - len(flat_by_name)
    plan["new_branch_slots"] = max(0, slots)
    plan["pool_usd"] = pool
    plan["reserve_usd"] = reserve
    plan["distributable_usd"] = distributable
    plan["per_branch_usd"] = per_branch
    plan["free_cash_before"] = round(free_cash, 2)

    if dry_run:
        plan["status"] = "dry_run"
        plan["changed"] = False
        plan["note"] = ("Nothing was changed. Coins for the new branches are chosen at "
                        "execution time from the live ranking, so they are not listed here.")
        return plan

    # --- execute -----------------------------------------------------------
    # Withdrawals first: they are what makes the cash available for
    # everything after them. A failure here stops the whole run rather than
    # leaving the fleet half-levelled with no record of why.
    for w in plan["withdrawals"]:
        await withdraw_from_grid_branch(w["bot_name"], w["release_usd"])
        log.info(f"[GRID] spread: pulled ${w['release_usd']:,.2f} out of {w['bot_name']} "
                 f"({w['product_id']}) - now ${w['to_usd']:,.2f}")

    for t in plan["top_ups"]:
        try:
            await add_cash_to_grid_branch(t["bot_name"], t["add_usd"])
            log.info(f"[GRID] spread: added ${t['add_usd']:,.2f} to {t['bot_name']}")
        except Exception as e:
            t["skipped"] = f"{type(e).__name__}: {e}"
            log.warning(f"[GRID] spread: could not top up {t['bot_name']}: {e}")

    candidates, candidate_note = await _spread_candidate_coins(max(0, slots))
    plan["candidate_coins"] = candidates
    plan["candidate_note"] = candidate_note

    created = 0
    for product_id in candidates:
        if created >= slots:
            break
        try:
            branch = await create_grid_branch(product_id, per_branch)
            plan["new_branches"].append({"bot_name": branch.bot_name, "product_id": product_id,
                                         "allocated_usd": per_branch})
            created += 1
            log.info(f"[GRID] spread: created {branch.bot_name} on {product_id} "
                     f"with ${per_branch:,.2f}")
        except Exception as e:
            # Keep going. One coin being ineligible is not a reason to stop
            # levelling the fleet - the first version broke out of this loop
            # on the first failure and therefore created ZERO branches
            # whenever the ranked picker had nothing, which is exactly what
            # happened on a live account with no qualifying backtest data.
            plan["new_branches"].append({"product_id": product_id,
                                         "skipped": f"{type(e).__name__}: {e}"})
            log.warning(f"[GRID] spread: could not create a branch on {product_id}: {e}")
    if created < slots:
        plan["new_branches"].append({
            "skipped": f"wanted {slots} new branch(es), opened {created} - "
                       f"ran out of eligible coins. {candidate_note}"})

    plan["status"] = "spread"
    plan["changed"] = True
    plan["free_cash_after"] = await get_real_free_cash_usd()
    return plan


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
    backtest run yet, every ranked coin is excluded/claimed, or nothing
    real clears the real MIN_REQUIRED_ROI_PCT edge floor - see its own
    comment above)."""
    import crypto_family_tree_bot as tree  # lazy - avoids a circular import at module load, same pattern as get_real_free_cash_usd above
    from models import CryptoBacktestRun

    excluded = await tree.get_effective_excluded_coins()
    claimed = await get_grid_branch_claimed_coins()

    async with get_session_factory()() as db:
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
    best_pid = await _first_ranked_coin_beating_btc([c.product_id for c in candidates])
    best_roi = latest_by_coin[best_pid].roi_pct_of_spend
    if best_roi is None or best_roi < MIN_REQUIRED_ROI_PCT:
        raise ValueError(
            f"no real coin currently clears the real minimum edge floor ({MIN_REQUIRED_ROI_PCT:.0f}% real "
            f"backtested ROI) - the best real candidate right now is {best_pid} at "
            f"{best_roi:.1f}% ROI, which isn't a genuine enough edge to deploy new capital into"
        )
    return best_pid


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
    backtest run yet, every ranked coin is excluded/claimed by some
    OTHER active branch, or nothing real clears the real
    MIN_REQUIRED_ROI_PCT edge floor - see its own comment above) - never
    raises, so an automatic caller can just skip a branch this cycle
    rather than crash on a real data gap."""
    import crypto_family_tree_bot as tree  # lazy - avoids a circular import at module load, same pattern as get_real_free_cash_usd above
    from models import CryptoBacktestRun

    excluded = await tree.get_effective_excluded_coins()
    claimed = await get_grid_branch_claimed_coins()
    if exclude_bot_name:
        async with get_session_factory()() as db:
            result = await db.execute(select(CryptoGridBranch.product_id).where(CryptoGridBranch.bot_name == exclude_bot_name))
            own_coin = result.scalar_one_or_none()
        if own_coin:
            claimed = claimed - {own_coin}

    async with get_session_factory()() as db:
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
    best_roi = latest_by_coin[best_pid].roi_pct_of_spend
    if best_roi is None or best_roi < MIN_REQUIRED_ROI_PCT:
        return None, None
    return best_pid, best_roi


async def _latest_backtested_roi(product_id: str):
    """Return the latest recorded ROI for one coin, or None when untested."""
    from models import CryptoBacktestRun

    async with get_session_factory()() as db:
        result = await db.execute(
            select(CryptoBacktestRun.roi_pct_of_spend)
            .where(CryptoBacktestRun.product_id == product_id)
            .order_by(desc(CryptoBacktestRun.run_at))
            .limit(1)
        )
        return result.scalar_one_or_none()


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

    async with get_session_factory()() as db:
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
    if best_pid is None:
        current_roi = await _latest_backtested_roi(branch.product_id)
        if current_roi is None or current_roi >= MIN_REQUIRED_ROI_PCT:
            return
        amount = branch.allocated_usd
        result = await withdraw_from_grid_branch(branch.bot_name, amount)
        log.info(
            f"[GRID] Retired ${amount:.2f} of idle cash from {branch.bot_name} ({branch.product_id}) "
            f"at {current_roi:.1f}% backtested ROI because no coin clears the "
            f"{MIN_REQUIRED_ROI_PCT:.0f}% edge floor"
        )
        await _log_activity_safe(
            branch.bot_name, branch.product_id, "REALLOCATE",
            f"Retired ${amount:.2f} to unallocated USD cash: latest backtest {current_roi:.1f}% "
            f"is below the {MIN_REQUIRED_ROI_PCT:.0f}% edge floor and no qualified replacement exists",
        )
        return {
            "action": "retired_to_cash",
            "bot_name": branch.bot_name,
            "product_id": branch.product_id,
            "amount": round(amount, 2),
            "backtested_roi_pct": current_roi,
            "orders_placed": False,
            **result,
        }
    if best_pid == branch.product_id:
        return  # already the real best available coin - no pointless move

    # ---- ROI RANKS THE WRONG THING FOR A GRID ----
    #
    # _best_available_coin_and_roi() ranks on latest backtested ROI, which
    # measures a coin GOING UP. A grid does not need the price to go up; it
    # needs it to COME BACK. Those are different coins, and on this account
    # they were opposite ones.
    #
    # Measured 2026-09-26 on real candles, complete 2.50% round trips over
    # 60 days against the ROI the ranker was reading:
    #
    #     coin    ROI      round trips
    #     ARB     +26.7%             2      <- ranked #1, traded twice
    #     NEAR    +21.9%             5
    #     BCH     -34.7%             3
    #     ONDO         -            11      <- never ranked, trades constantly
    #
    # ARB is the branch holding the fleet's largest allocation and it
    # completed one round trip in thirty days. Rotating on ROI would have
    # put MORE money there.
    #
    # So ROI still proposes, but oscillation has a veto: a move only
    # happens when the candidate has also actually round-tripped more than
    # the coin being left, by a margin wide enough to clear the noise in
    # that ranking (coin_rotation documents the +0.344 persistence figure
    # this margin is sized against). Veto only - this can cancel a rotation
    # ROI wanted, never start one ROI did not.
    # ---- THE COIN LIST IS LOCKED ----
    #
    # Per the account owner, from experience: widening the coin list is how
    # this account lost real money before - "they all dragged down." Across
    # 120 days these coins carry +0.574 average pairwise correlation and on
    # 31% of days 80%+ of them fell together, so a wider list is not a wider
    # spread of risk, it is the same bet written more times. A grid buys
    # dips, so on those days every branch fills at once and none can sell.
    #
    # With GRID_COIN_UNIVERSE unset the allowed set is the coins the fleet
    # already holds, so this refuses every NEW coin and permits only a
    # reshuffle among coins a human already chose.
    _allowed = set(rotation.universe(await get_grid_branch_claimed_coins()))
    if best_pid not in _allowed:
        log.info(
            f"[GRID] auto-rotate declined {branch.bot_name}: ROI ranks {best_pid} best, "
            f"but it is outside the locked coin universe ({', '.join(sorted(_allowed)) or 'none'}). "
            f"Set {rotation.COIN_UNIVERSE_ENV} to change which coins the fleet may hold - "
            f"rotation does not widen the list on its own."
        )
        return

    if rotation.auto_rotate_enabled():
        try:
            trips = await rotation.trips_for([branch.product_id, best_pid],
                                             step=branch.grid_pct)
            here, there = trips.get(branch.product_id), trips.get(best_pid)
            if here is not None and there is not None:
                if there - here < rotation.ROTATE_MIN_TRIP_MARGIN:
                    log.info(
                        f"[GRID] auto-rotate declined {branch.bot_name}: ROI ranks {best_pid} "
                        f"above {branch.product_id}, but over {rotation.ROTATE_LOOKBACK_DAYS}d "
                        f"{best_pid} completed {there} round trip(s) against {here} - "
                        f"under the {rotation.ROTATE_MIN_TRIP_MARGIN}-trip margin, so the "
                        f"money stays where it is"
                    )
                    return
        except Exception as e:
            # Measurement is a veto, not a gate: if it cannot be taken, fall
            # through to the behaviour that existed before it.
            log.warning(f"[GRID] oscillation veto unavailable for {branch.bot_name} "
                        f"({e}) - falling back to the ROI ranking alone")

    amount = branch.allocated_usd
    result = await move_cash_between_grid_branches(branch.bot_name, amount, product_id=best_pid)
    log.info(
        f"[GRID] 🔁 auto-rotated ${amount:.2f} of real idle cash from {branch.bot_name} ({branch.product_id}) "
        f"into {result['to_bot_name']} ({best_pid}) - real best-ranked coin available right now"
    )
    return {"action": "rotated", "orders_placed": False, **result}


async def tune_spacing_per_coin(dry_run: bool = True, min_trips: int = 4,
                                min_improvement_usd: float = 1.0,
                                days: int = 90) -> dict:
    """Pick each branch's grid step from MEASURED performance on its own coin.

    The account owner's ask: "learn as you go and change it to where it can
    get better and faster... then we get tighter and tighter to what it
    should be."

    The important correction is in the word "tighter". Tighter is not
    reliably better, and the real 30-day data says so:

        35-coin aggregate   2.0% +$306.98   2.5% +$348.21   (wider won)
        STX-USD             2.0% +$25.32    2.5% +$33.77    (wider won)
        POL-USD             2.0% +$18.36    2.5% +$14.71    (tighter won)

    Total grid profit is roughly trips x capital x margin / levels, so a
    tighter step buys more trips and sells margin on every one. Which side
    wins depends on how that particular coin actually moves. There is no
    single best step for the fleet, and a tuner that only ever tightens is
    a machine for walking the fleet to its floor on a hunch - the same
    mistake as the 1.25% recommendation of 2026-09-25, automated.

    So this measures, and moves the step in whichever direction the
    measurement points.

    Evidence rules, all of which exist to stop it acting on noise:

      * min_trips - a candidate that completed fewer than this many round
        trips in the replay is ignored however good its total looks. One
        lucky trip is not a spacing verdict.
      * min_improvement_usd - the winner must beat what the branch runs
        today by this much. Without it the tuner churns the fleet over
        rounding differences.
      * the fee-safe floor still applies, unconditionally. A candidate
        below it is clamped up, never followed down. The floor prices the
        TAKER round trip because an unfilled maker order becomes a market
        order, and no measurement here may lift it.

    dry_run=True (the default) changes nothing and returns the plan.

    NOTE ON PRECEDENCE: a manually promoted global candidate
    (get_live_grid_spacing_override) beats per-branch spacing outright in
    run_grid_branch_cycle. While one is set - it is '3_levels_2.0pct'
    today - applying this tuner writes values the live cycle will ignore.
    The plan says so per branch rather than pretending otherwise.
    """
    import crypto_selection_backtest as lab

    override = await get_live_grid_spacing_override()
    override_is_set = override in GRID_LEVEL_SPACING_CANDIDATES
    floor = await fee_safe_floor_pct()
    branches = [b for b in await get_grid_branches() if b.active]

    plans, skipped = [], []
    for b in branches:
        # NEVER re-space a branch that is holding a position.
        #
        # 2026-09-25: this guard did not exist, and the tuner widened
        # NEAR-USD from 2.00% to 3.00% while a real slice was open. The
        # same reference_price sets the SELL trigger
        # (price >= reference * (1 + grid_pct)), so the exit moved from
        # $5.0681 to $5.1178 - from 1.24% away to 2.23% away - on money
        # already committed. That time it happened to be favourable (the
        # trade is worth $0.43 instead of $0.20 if it lands), but that was
        # luck, not design: on a falling coin the identical move pushes an
        # exit out of reach.
        #
        # The backtest answers "what step is best for the NEXT trade". It
        # does not answer "what should I do with a position already open",
        # and those must not be confused. A held slice keeps the terms it
        # was opened under; the branch is re-tuned once it is flat.
        #
        # reanchor_flat_grid_branches_now() has had this rule from the
        # start. The tuner should have had it too.
        held = await get_grid_slices(b.bot_name)
        if held:
            skipped.append({
                "product_id": b.product_id,
                "reason": (f"holds {len(held)} open slice(s) - re-spacing would move the exit "
                           f"on a position already open; will re-tune when flat"),
                "is_error": False,
            })
            continue
        try:
            # `days` widens the evidence base. At the 30-day default a
            # coin can produce two or three round trips at 3.0% spacing,
            # which is not a spacing verdict. A longer window is real
            # historical data the system can already fetch - the cheapest
            # honest way to get more trips before believing a candidate.
            res = await lab.run_grid_level_spacing_comparison(
                coins=[b.product_id], days=days)
        except Exception as exc:
            skipped.append({"product_id": b.product_id, "reason": f"backtest failed: {exc}"})
            continue

        # run_grid_level_spacing_comparison returns {"comparison": [row, ...]},
        # one row per coin, each candidate a key on that row carrying
        # total_pnl / num_trades. Read it as it really is.
        coin_row = next((r for r in (res or {}).get("comparison", [])
                         if r.get("product_id") == b.product_id), None)
        if coin_row is None:
            # NOT thin evidence - the backtest returned nothing for this
            # coin at all. Kept distinct on purpose: the first version of
            # this function misread the result shape, found no rows, and
            # every coin came back "no candidate cleared 4 trips", which
            # reads like a careful verdict and was actually a parsing bug.
            # A shape error must never be able to wear the evidence gate's
            # clothes.
            skipped.append({"product_id": b.product_id,
                            "reason": "backtest returned no comparison row for this coin",
                            "is_error": True,
                            "skip_reasons": (res or {}).get("skipped")})
            continue

        rows = []
        for label, cfg in GRID_LEVEL_SPACING_CANDIDATES.items():
            entry = coin_row.get(label) or {}
            net, trips = entry.get("total_pnl"), entry.get("num_trades")
            if net is None or trips is None:
                continue
            rows.append({"label": label, "net_usd": float(net), "trips": int(trips),
                         "grid_pct": cfg["grid_pct"], "num_levels": cfg["num_levels"],
                         "below_floor": cfg["grid_pct"] < floor})

        if not rows:
            skipped.append({"product_id": b.product_id,
                            "reason": "no candidate returned a usable result (shape or data error)",
                            "is_error": True})
            continue

        eligible = [r for r in rows if r["trips"] >= min_trips and not r["below_floor"]]
        if not eligible:
            skipped.append({"product_id": b.product_id,
                            "reason": f"no candidate cleared {min_trips} trips above the {floor*100:.2f}% floor",
                            "is_error": False,
                            "candidates": rows})
            continue

        best = max(eligible, key=lambda r: r["net_usd"])
        current = next((r for r in rows if abs(r["grid_pct"] - b.grid_pct) < 1e-9), None)
        current_net = current["net_usd"] if current else None
        gain = (best["net_usd"] - current_net) if current_net is not None else None

        if current is not None and abs(best["grid_pct"] - b.grid_pct) < 1e-9:
            skipped.append({"product_id": b.product_id,
                            "reason": f"already on the measured best ({best['label']})"})
            continue
        if gain is not None and gain < min_improvement_usd:
            skipped.append({"product_id": b.product_id,
                            "reason": f"best candidate {best['label']} beats current by only ${gain:.2f} "
                                      f"(needs ${min_improvement_usd:.2f}) - not worth churning"})
            continue

        plans.append({
            "product_id": b.product_id, "bot_name": b.bot_name,
            "from_grid_pct": b.grid_pct, "to_grid_pct": best["grid_pct"],
            "direction": "tighter" if best["grid_pct"] < b.grid_pct else "wider",
            "to_label": best["label"], "to_levels": best["num_levels"],
            "measured_net_usd": round(best["net_usd"], 2), "measured_trips": best["trips"],
            "beats_current_by_usd": round(gain, 2) if gain is not None else None,
            "would_take_effect": not override_is_set,
        })

    applied = []
    if not dry_run:
        for plan in plans:
            async with get_session_factory()() as db:
                result = await db.execute(
                    select(CryptoGridBranch).where(CryptoGridBranch.bot_name == plan["bot_name"]))
                row = result.scalar_one_or_none()
                if row is None:
                    continue
                row.grid_pct = max(plan["to_grid_pct"], floor)
                row.num_levels = max(1, min(_safe_num_levels_for_allocation(row.allocated_usd),
                                            plan["to_levels"]))
                await db.commit()
            applied.append(plan["product_id"])
            await _log_activity_safe(
                plan["bot_name"], plan["product_id"], "SPACING_TUNED",
                f"{plan['from_grid_pct']*100:.2f}% -> {plan['to_grid_pct']*100:.2f}% "
                f"({plan['direction']}, measured +${plan['measured_net_usd']:.2f} "
                f"over {plan['measured_trips']} trips)")

    return {
        "status": "dry_run" if dry_run else "applied",
        "fee_safe_floor_pct": floor,
        "global_override": override,
        "global_override_blocks_per_coin": override_is_set,
        "plans": plans, "plan_count": len(plans),
        "applied": applied, "skipped": skipped,
        "backtest_days": days,
        "rules": {"min_trips": min_trips, "min_improvement_usd": min_improvement_usd,
                  "floor_is_never_crossed": True,
                  "direction": "whichever the measurement points - not always tighter"},
    }


async def force_one_buy(bot_name: str, amount_usd: float = None) -> dict:
    """Place ONE real slice now, to measure whether a maker order fills.

    This exists for a single, narrow question that nothing else can
    answer: is a post-only limit order actually resting and filling as
    MAKER, or is it timing out and falling through to a market order?

    It matters more than any other setting here. A maker round trip costs
    0.70% and a taker round trip 1.50%. The spacing floor prices taker,
    because an unfilled maker order becomes a market order - so the floor
    sits at 1.70% and no step below it can profit. If maker fills are
    real, the floor is 0.90% and a 1.25% step nets +0.55% instead of
    -0.25%, which is the difference between a fleet that trades a few
    times a week and one that trades several times a day.

    Until 2026-09-25 that question was unanswerable, because _build_jwt
    signed the query string and get_best_bid_ask() returned 401 on every
    call - so place_maker_buy() bailed on its first line and not one
    maker order was ever placed. That is fixed; this measures whether the
    fix works.

    It buys at the CURRENT price rather than waiting for a dip, which is a
    slightly worse entry than the grid would normally take. That is the
    deliberate cost of the measurement. The slice is otherwise completely
    ordinary: it sells when price rises a step above its own entry, same
    as any other.

    Guarded: one named branch, one slice, capped at the branch's normal
    slice size, and it refuses if the branch already holds every level it
    is allowed.
    """
    async with get_session_factory()() as db:
        result = await db.execute(
            select(CryptoGridBranch).where(CryptoGridBranch.bot_name == bot_name))
        branch = result.scalar_one_or_none()
    if branch is None:
        return {"status": "error", "detail": f"no grid branch named {bot_name!r}"}
    if not branch.active:
        return {"status": "refused", "detail": f"{bot_name} is paused"}

    open_slices = await get_grid_slices(bot_name)
    if len(open_slices) >= (branch.num_levels or 1):
        return {"status": "refused",
                "detail": (f"{bot_name} already holds {len(open_slices)}/{branch.num_levels} "
                           f"levels - forcing another would exceed its own limit")}

    normal_slice = (branch.allocated_usd or 0.0) / max(1, branch.num_levels or 1)
    spend = min(float(amount_usd or normal_slice), normal_slice)
    if spend < MIN_TRADE_USD:
        return {"status": "refused",
                "detail": f"${spend:.2f} is below the ${MIN_TRADE_USD:.2f} minimum trade size"}

    before = await get_fill_mix()

    import aiohttp
    async with aiohttp.ClientSession() as session:
        real_cash, cash_err = await engine.get_usd_balance(session)
        if real_cash is None:
            return {"status": "error", "detail": f"real balance unavailable: {cash_err}"}
        if real_cash < spend:
            return {"status": "refused",
                    "detail": f"only ${real_cash:.2f} real USD available, need ${spend:.2f}"}

        log.warning(f"[GRID] 🔬 FORCED BUY on {bot_name} ({branch.product_id}) for ${spend:.2f} "
                    f"- measuring whether the maker path fills")
        fill = await grid_buy(session, spend, branch.product_id)

    if not fill:
        reason = engine._last_order_error.get(branch.product_id, "no reason reported")
        return {"status": "no_fill", "detail": reason, "fill_mix_before": before}

    qty, price, leg_rate = fill
    after = await get_fill_mix()

    # The counter is the ground truth for THIS order: whichever leg count
    # moved is what this fill actually was.
    b_buy = (before.get("buy") or {})
    a_buy = (after.get("buy") or {})
    was_maker = (a_buy.get("maker_legs", 0) or 0) > (b_buy.get("maker_legs", 0) or 0)

    async with get_session_factory()() as db:
        db.add(CryptoGridSlice(
            bot_name=bot_name, product_id=branch.product_id,
            entry_price=price, qty=qty, opened_at=datetime.utcnow(),
            entry_fee_rate=leg_rate, entry_expected_price=price))
        result = await db.execute(
            select(CryptoGridBranch).where(CryptoGridBranch.bot_name == bot_name))
        row = result.scalar_one_or_none()
        if row is not None:
            # Match the real buy path exactly: it sets reference_price and
            # NOTHING else. allocated_usd is the branch's total capital and
            # is not decremented on a buy - the open slice IS that capital,
            # now held as coin. Subtracting here as well would count the
            # same dollars out twice and shrink the branch on every fill.
            row.reference_price = price
        await db.commit()

    msg = (f"🔬 FORCED BUY: {bot_name} bought {qty:.8f} {branch.product_id} @ "
           f"${price:,.6f} (${qty * price:.2f}) - filled as "
           f"{'MAKER' if was_maker else 'TAKER'}")
    log.warning(f"[GRID] {msg}")
    await _log_activity_safe(bot_name, branch.product_id, "FORCED_BUY", msg)

    return {
        "status": "filled",
        "bot_name": bot_name,
        "product_id": branch.product_id,
        "qty": qty,
        "fill_price": price,
        "spent_usd": round(qty * price, 2),
        "leg_fee_rate_recorded": leg_rate,
        "was_maker": was_maker,
        "sells_at": round(price * (1 + (branch.grid_pct or 0.02)), 8),
        "fill_mix_before": before,
        "fill_mix_after": after,
        "what_this_means": (
            "MAKER fills are real - the 1.70% floor is priced against a taker round trip "
            "the fleet is not actually paying, and can be reviewed."
            if was_maker else
            "This order still fell through to a market order. The floor stays at 1.70%: "
            "a post-only order that does not rest is a taker fill, whatever the intent."),
    }


async def money_check() -> dict:
    """Every dollar in this fleet that is NOT currently earning, and what
    (if anything) can be done about it in one action. Read-only.

    WHY THIS EXISTS

    The account owner asked for a button that makes money when pressed.
    The honest engineering answer is that a button cannot create edge -
    but it CAN close the gap between money that is earning and money that
    is merely sitting, and that gap is measurable, so it should be on
    screen instead of in someone's head.

    The first draft of this was going to be "deploy the idle cash", since
    $88.14 was sitting free against $484.46 working - 15.4% of the crypto
    account apparently doing nothing. That would have been wrong and
    expensive: GRID_CASH_RESERVE_USD is 88.0, so that cash IS the reserve
    that funds the remaining levels of every open branch. Deploying it
    would have left $0.14 of buffer and called it an improvement. Hence
    the rule this function follows everywhere: a finding is only reported
    when the money is genuinely unemployed, measured against the rule
    that governs it, not against zero.

    Each finding carries:
      kind      - what is idle
      usd       - dollars involved, where that is meaningful
      detail    - a plain sentence naming the real numbers
      action    - the endpoint that fixes it, or None when nothing can
      basis     - what the claim is measured against; never a forecast
    """
    status = await get_grid_status()
    branches = status.get("branches") or []
    free_cash = status.get("real_free_cash_usd")
    allocated = status.get("total_allocated_usd") or 0.0
    floor = status.get("fee_safe_min_grid_pct")

    # allocated_usd is an EARMARK, not an investment. A flat branch's whole
    # allocation sits in the USD wallet until a dip triggers a buy. Only
    # the capital behind an open slice is actually deployed into a coin.
    # Reported as "working", a fleet with every branch flat looks fully
    # invested while 100% of the money is cash - which is exactly what was
    # on screen: "$553.84 working" against a Coinbase balance of $572.60
    # USD and no crypto at all.
    deployed = sum((b.get("allocated_usd") or 0.0) for b in branches if b.get("open_slices"))
    earmarked_idle = max(0.0, allocated - deployed)
    total_idle = earmarked_idle + (free_cash or 0.0)

    findings = []

    if deployed <= 0:
        findings.append({
            "kind": "nothing_deployed",
            "usd": round(total_idle, 2),
            "action": None,
            "detail": (f"Every branch is flat, so nothing is invested in any coin right now. "
                       f"${allocated:,.2f} is EARMARKED to branches and ${(free_cash or 0.0):,.2f} is "
                       f"loose - but all ${total_idle:,.2f} of it is sitting in the USD wallet "
                       f"earning nothing until a dip triggers a buy."),
            "basis": "branches holding zero open slices hold zero coin - an earmark is not an investment",
        })
    else:
        findings.append({
            "kind": "deployed",
            "usd": round(deployed, 2),
            "action": None,
            "detail": (f"${deployed:,.2f} is genuinely in coin across "
                       f"{sum(1 for b in branches if b.get('open_slices'))} branch(es). "
                       f"${total_idle:,.2f} is still cash waiting on a trigger."),
            "basis": "allocation behind branches that actually hold an open slice",
        })

    # ── cash above the reserve ──────────────────────────────────────────
    if free_cash is None:
        findings.append({
            "kind": "cash_unknown", "usd": None, "action": None,
            "detail": "The real Coinbase balance could not be read, so idle cash cannot be judged.",
            "basis": "unknown cash is never treated as deployable",
        })
    else:
        deployable = free_cash - GRID_CASH_RESERVE_USD
        if deployable >= GRID_AUTO_DEPLOY_AMOUNT_USD:
            findings.append({
                "kind": "idle_cash", "usd": round(deployable, 2),
                "action": "/grid-status/spread-evenly",
                "detail": (f"${deployable:,.2f} sits above the ${GRID_CASH_RESERVE_USD:,.2f} reserve - "
                           f"enough for at least one more ${GRID_AUTO_DEPLOY_AMOUNT_USD:,.2f} branch."),
                "basis": "free cash minus the reserve that funds open branches' remaining levels",
            })
        else:
            findings.append({
                "kind": "cash_committed", "usd": round(max(0.0, deployable), 2),
                "action": None,
                "detail": (f"${free_cash:,.2f} is loose, but ${GRID_CASH_RESERVE_USD:,.2f} of it is the "
                           f"reserve backing open branches' remaining levels, leaving "
                           f"${max(0.0, deployable):,.2f} spare - under the "
                           f"${GRID_AUTO_DEPLOY_AMOUNT_USD:,.2f} a new branch needs. Committed is not the "
                           f"same as invested: this money is spoken for, and it is still cash."),
                "basis": "free cash minus GRID_CASH_RESERVE_USD - NOT free cash against zero",
            })

    # ── a step below the fee floor loses money on every round trip ──────
    if floor is not None:
        below = [b for b in branches if b.get("grid_pct") is not None
                 and b["grid_pct"] < floor - 1e-9]
        if below:
            findings.append({
                "kind": "below_fee_floor", "usd": None,
                "action": "/grid-status/tune-spacing-per-coin",
                "detail": (f"{len(below)} branch(es) trade below the {floor * 100:.2f}% fee floor "
                           f"({', '.join(b['product_id'] for b in below)}). Every completed round trip "
                           f"there loses money even when the trade is right."),
                "basis": "arithmetic: a target under the round-trip fee cannot net a gain",
            })

    # ── a stale reference makes a branch wait for a dip that passed ─────
    stale = []
    for b in branches:
        ref, cur, g = b.get("reference_price"), b.get("current_price"), b.get("grid_pct")
        if not ref or not cur or g is None or b.get("open_slices"):
            continue
        if cur > ref:
            needs_now = (cur - ref * (1 - g)) / cur * 100.0
            stale.append({"product_id": b["product_id"],
                          "needs_now_pct": round(needs_now, 2),
                          "needs_after_pct": round(g * 100.0, 2),
                          "saves_pts": round(needs_now - g * 100.0, 2)})
    if stale:
        stale.sort(key=lambda x: -x["saves_pts"])
        total = round(sum(x["saves_pts"] for x in stale), 2)
        findings.append({
            "kind": "stale_reference", "usd": None,
            "action": "/grid-status/reanchor-flat-branches",
            "detail": (f"{len(stale)} flat branch(es) still measure their next buy from a reference the "
                       f"market has already left behind, so each waits for a dip deeper than its own step: "
                       + ", ".join(f"{x['product_id']} needs {x['needs_now_pct']:.2f}% instead of "
                                   f"{x['needs_after_pct']:.2f}%" for x in stale[:4])
                       + f". Re-anchoring recovers {total:.2f} percentage points of waiting."),
            "basis": "live price against each branch's own stored reference; upward only, never downward",
            "branches": stale,
        })

    # ── capital parked in a branch that is switched off ─────────────────
    parked = [b for b in branches if not b.get("active") and (b.get("allocated_usd") or 0) > 0]
    if parked:
        findings.append({
            "kind": "paused_with_capital",
            "usd": round(sum(b["allocated_usd"] for b in parked), 2),
            "action": None,
            "detail": (f"{len(parked)} paused branch(es) still hold "
                       f"${sum(b['allocated_usd'] for b in parked):,.2f}: "
                       + ", ".join(b["product_id"] for b in parked)
                       + ". A paused branch never buys, so that capital is idle by choice."),
            "basis": "allocated_usd on branches with active = false",
        })

    actionable = [f for f in findings if f.get("action")]
    return {
        "allocated_usd": round(allocated, 2),
        "deployed_usd": round(deployed, 2),
        "idle_usd": round(total_idle, 2),
        "working_usd": round(deployed, 2),
        "free_cash_usd": round(free_cash, 2) if free_cash is not None else None,
        "reserve_usd": GRID_CASH_RESERVE_USD,
        "findings": findings,
        "actionable_count": len(actionable),
        "note": ("Nothing here is a forecast. Each finding names money that is measurably not "
                 "working and the rule it was measured against. The fleet's own record is the only "
                 "evidence of what working capital earns."),
    }


async def reanchor_flat_grid_branches_now() -> dict:
    """Move every FLAT branch's reference_price to the live market price.

    Why this exists, 2026-09-25. reference_price is written exactly twice:
    once when a branch is created, and thereafter only on a real fill
    (`fresh.reference_price = filled_price`). Nothing re-anchors it to the
    market. So a branch created before a rally sits with its reference
    below the market, and since a buy needs
    `price <= reference_price * (1 - grid_pct)` the dip it waits for is
    measured from a level the market already left.

    That is self-tightening: no fill means no re-anchor, so a rising
    market makes the next fill harder, not easier. On the live account all
    seven branches had gone sixteen days without a trade while needing
    falls of 2.70% to 3.67% from the current price to trigger a 2.00%
    grid step.

    SAFETY - this only ever touches branches holding NOTHING:

      * A branch with open slices is SKIPPED, never re-anchored. With a
        slice open the reference is also the sell trigger
        (`price >= reference_price * (1 + grid_pct)`), so moving it would
        move the exit of a position already on the book. Flat branches
        have no such tie.
      * reference_price is the ONLY field written. Spacing, levels,
        allocation and active flags are untouched.
      * The reference only ever moves UP. A lower reference means a lower
        buy trigger, i.e. further from the market. A branch trading below
        its reference is already nearer a fill than a fresh anchor would
        leave it, so it is skipped.
      * A branch whose live price cannot be read is skipped, not guessed.
      * Every before/after pair is returned, so the move is auditable and
        can be put back by hand.

    Places no orders. The next ordinary cycle decides whether to buy, and
    every existing gate still applies to it.
    """
    branches = await get_grid_branches()
    moved, skipped = [], []

    distinct_products = {b.product_id for b in branches if b.active}
    live_prices = {}
    if distinct_products:
        async with engine.aiohttp.ClientSession() as session:
            for product_id in distinct_products:
                try:
                    price, _atr = await engine.get_price_and_volatility(session, product_id)
                    live_prices[product_id] = price
                except Exception as exc:
                    log.warning(f"[GRID] re-anchor: no price for {product_id}: {exc}")

    for b in branches:
        if not b.active:
            skipped.append({"bot_name": b.bot_name, "reason": "branch is not active"})
            continue
        slices = await get_grid_slices(b.bot_name)
        if slices:
            skipped.append({"bot_name": b.bot_name,
                            "reason": f"holds {len(slices)} open slice(s) - reference is also its sell trigger"})
            continue
        price = live_prices.get(b.product_id)
        if not price or price <= 0:
            skipped.append({"bot_name": b.bot_name, "reason": "no live price - not guessing"})
            continue

        old = b.reference_price
        # Only ever re-anchor UPWARD. The buy trigger is
        # reference_price * (1 - grid_pct), so a LOWER reference means a
        # LOWER trigger - further from the market, harder to fill. When the
        # live price sits below the reference the branch is already closer
        # to a fill than a fresh anchor would put it, and re-anchoring
        # would take that away.
        #
        # Caught by test_reanchor.py before this ever ran: on the live
        # fleet BTC was the one branch trading BELOW its reference, needing
        # a 1.42% fall against a 2.00% step. Blanket re-anchoring would
        # have moved it back to needing the full 2.00% - the opposite of
        # the point.
        if old and price <= old:
            skipped.append({
                "bot_name": b.bot_name,
                "reason": (f"live ${price:,.6f} is at or below its reference ${old:,.6f} - "
                           f"already closer to a fill than a re-anchor would leave it"),
            })
            continue
        async with get_session_factory()() as db:
            result = await db.execute(
                select(CryptoGridBranch).where(CryptoGridBranch.bot_name == b.bot_name))
            fresh = result.scalar_one_or_none()
            if fresh is None:
                skipped.append({"bot_name": b.bot_name, "reason": "branch vanished between read and write"})
                continue
            if await get_grid_slices(b.bot_name):
                skipped.append({"bot_name": b.bot_name, "reason": "slice opened during re-anchor - left alone"})
                continue
            fresh.reference_price = price
            await db.commit()

        drop_before = ((old - price) / old * 100) if old else None
        moved.append({
            "bot_name": b.bot_name, "product_id": b.product_id,
            "old_reference_price": old, "new_reference_price": price,
            "grid_pct": b.grid_pct,
            "buy_triggers_at": round(price * (1 - b.grid_pct), 8),
            "drop_needed_before_pct": round(-drop_before + b.grid_pct * 100, 2) if drop_before is not None else None,
            "drop_needed_now_pct": round(b.grid_pct * 100, 2),
        })
        msg = (f"re-anchored reference ${old:,.6f} -> ${price:,.6f}; a {b.grid_pct*100:.2f}% dip "
               f"now triggers at ${price * (1 - b.grid_pct):,.6f}")
        log.info(f"[GRID] {b.bot_name}: {msg}")
        await _log_activity_safe(b.bot_name, b.product_id, "REANCHOR", msg)

    return {
        "moved": moved, "moved_count": len(moved),
        "skipped": skipped, "skipped_count": len(skipped),
        "orders_placed": False,
        "note": ("reference_price only; spacing, levels and allocation untouched. "
                 "Branches holding open slices were skipped."),
    }


async def rebalance_flat_grid_branches_now() -> dict:
    """Apply the live edge rule immediately to every movable flat branch.

    WHAT THIS DID ON 2026-09-26, and why it is gated now.

    At 00:59 this collapsed an eight-branch fleet into three in a single
    call - BTC $69.23, NEAR $207.69, ARB $276.92. No money was lost (the
    total stayed exactly $553.84; move_cash_between_grid_branches merges a
    flat branch into another and deletes the emptied row), but half the
    account landed on ARB, which completed ONE 2.50% round trip in thirty
    days and is the worst oscillator in the set. The five coins it
    abandoned - BONK, FLOKI, DOGE, ETC, BCH - carry 26 of the fleet's 34
    available monthly round trips between them.

    Three things combined to let that happen, and all three are fixed:

      1. It loops EVERY branch, so one call is a fleet-wide merge.
      2. It passed after_sale=True, which waives
         GRID_ROTATION_COOLDOWN_SECONDS. That flag means "this ONE branch
         just sold its last slice and earned an immediate redeploy" - it
         is not a licence for a bulk sweep to skip every cooldown at once.
         Now passes after_sale=False, so the cooldown that exists to stop
         branches ping-ponging actually applies here too.
      3. It never checked is_grid_auto_rotate_active(). The scheduled
         sweep at run_grid_auto_rotate_sweep() does. So the fleet's
         auto-rotate toggle read FALSE the whole time and was simply not
         wired to this path - the switch was real and this door was not
         behind it.

    Gating a manual endpoint on the automatic toggle is deliberate: this
    moves real capital across the whole fleet with no confirmation step,
    and there is no button for it in the dashboard, so anything reaching
    the URL gets a fleet-wide merge. Turn auto-rotate on to use it.
    """
    if not await is_grid_auto_rotate_active():
        log.info("[GRID] rebalance-flat-branches refused: auto-rotate is OFF. This "
                 "endpoint merges flat branches across the WHOLE fleet in one call, "
                 "so it follows the same switch the scheduled sweep does.")
        return {
            "actions": [], "action_count": 0,
            "skipped_errors": [], "orders_placed": False,
            "refused": True,
            "note": ("Auto-rotate is switched off, and this endpoint moves capital "
                     "across every flat branch at once. Enable auto-rotate first if "
                     "that is genuinely what you want."),
        }
    actions = []
    skipped_errors = []
    for branch in await get_grid_branches():
        if not branch.active:
            continue
        try:
            # after_sale=False: the per-branch cooldown applies. See the
            # docstring above - borrowing the post-sale flag here is what
            # let eight branches merge in one pass.
            result = await _maybe_rotate_one_grid_branch(branch, after_sale=False)
            if result is not None:
                actions.append(result)
        except Exception as exc:
            skipped_errors.append({"bot_name": branch.bot_name, "error": str(exc)})
            log.warning(f"[GRID] immediate edge rebalance skipped {branch.bot_name}: {exc}")
    return {
        "actions": actions,
        "action_count": len(actions),
        "released_to_cash_usd": round(sum(
            action["amount"] for action in actions if action["action"] == "retired_to_cash"
        ), 2),
        "orders_placed": False,
        "skipped_errors": skipped_errors,
    }


def evaluate_adaptive_fleet_stages(realized_pnl: float, claimed: set, excluded: set, roi_by_coin: dict) -> dict:
    """Evaluate the fixed fleet sequence without performing I/O or trading."""
    stages = []
    eligible_product_ids = []
    sequence_blocked = False
    for product_id, required_realized_pnl in ADAPTIVE_FLEET_STAGES:
        active = product_id in claimed
        roi_pct = roi_by_coin.get(product_id)
        if active:
            state = "active"
        elif sequence_blocked:
            state = "waiting_for_prior_stage"
        elif realized_pnl < required_realized_pnl:
            state = "waiting_for_realized_profit"
            sequence_blocked = True
        elif product_id in excluded:
            state = "blocked_by_exclusion"
        elif roi_pct is None:
            state = "waiting_for_backtest"
        elif roi_pct < MIN_REQUIRED_ROI_PCT:
            state = "below_minimum_edge"
        else:
            state = "eligible"
            eligible_product_ids.append(product_id)
        stages.append({
            "product_id": product_id,
            "required_realized_pnl": required_realized_pnl,
            "realized_pnl": round(realized_pnl, 2),
            "backtested_roi_pct": roi_pct,
            "state": state,
        })
    return {
        "next_product_id": eligible_product_ids[0] if eligible_product_ids else None,
        "eligible_product_ids": eligible_product_ids,
        "stages": stages,
    }


async def get_adaptive_fleet_status() -> dict:
    """Read-only, evidence-backed progress for the staged fleet."""
    import crypto_family_tree_bot as tree
    from models import CryptoBacktestRun

    async with get_session_factory()() as db:
        realized_result = await db.execute(select(func.sum(CryptoGridTradeHistory.pnl)))
        realized_pnl = float(realized_result.scalar_one_or_none() or 0.0)
        backtest_result = await db.execute(
            select(CryptoBacktestRun).where(
                CryptoBacktestRun.product_id.in_([stage[0] for stage in ADAPTIVE_FLEET_STAGES])
            ).order_by(CryptoBacktestRun.product_id, desc(CryptoBacktestRun.run_at))
        )
        backtests = backtest_result.scalars().all()

    roi_by_coin = {}
    for row in backtests:
        if row.product_id not in roi_by_coin:
            roi_by_coin[row.product_id] = row.roi_pct_of_spend

    evaluation = evaluate_adaptive_fleet_stages(
        realized_pnl,
        await get_grid_branch_claimed_coins(),
        await tree.get_effective_excluded_coins(),
        roi_by_coin,
    )
    return {
        "enabled": GRID_ADAPTIVE_FLEET_ENABLED,
        "activation_amount_usd": GRID_AUTO_DEPLOY_AMOUNT_USD,
        "realized_grid_pnl": round(realized_pnl, 2),
        **evaluation,
    }


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
        # None means the real balance could not be read. Unknown cash is
        # never a reason to deploy - the same fail-closed rule the rest of
        # this file uses.
        if real_free_cash is None:
            return
        # The fleet's share of a SHARED wallet, not the whole wallet.
        #
        # The family tree spends the same real Coinbase USD, and until this
        # existed the first loop to look could take all of it. Re-read each
        # iteration against a fresh balance, so the ceiling binds on the
        # seventh branch of a sweep exactly as it does on the first.
        ceiling, ceiling_reason = await get_grid_spend_ceiling_usd()
        if ceiling is None:
            return
        # Spend only what sits ABOVE the reserve, and never above the
        # fleet's own share. Two independent caps: GRID_CASH_RESERVE_USD
        # protects the fleet's own future levels, the allocator ceiling
        # protects the OTHER bots from this one.
        deployable = min(real_free_cash - GRID_CASH_RESERVE_USD, ceiling)
        if (deployable < GRID_AUTO_DEPLOY_AMOUNT_USD
                and real_free_cash - GRID_CASH_RESERVE_USD >= GRID_AUTO_DEPLOY_AMOUNT_USD):
            # Cash exists above the reserve but the SHARE is what stops
            # this. Said plainly, because "auto-deploy holding" with money
            # visibly free in the wallet is otherwise unexplainable.
            log.info(f"[GRID] auto-deploy holding on its cash share - {ceiling_reason}")
            return
        if deployable < GRID_AUTO_DEPLOY_AMOUNT_USD:
            if created == 0 and real_free_cash >= GRID_AUTO_DEPLOY_AMOUNT_USD:
                log.info(
                    f"[GRID] auto-deploy holding: ${real_free_cash:,.2f} free cash less the "
                    f"${GRID_CASH_RESERVE_USD:,.2f} reserve leaves ${max(0.0, deployable):,.2f}, "
                    f"under one ${GRID_AUTO_DEPLOY_AMOUNT_USD:,.2f} branch"
                )
            return
        try:
            if GRID_ADAPTIVE_FLEET_ENABLED:
                fleet_status = await get_adaptive_fleet_status()
                product_id = fleet_status["next_product_id"]
                if product_id is None:
                    return
            else:
                product_id = await pick_best_ranked_coin_for_grid()
            branch = await create_grid_branch(product_id, GRID_AUTO_DEPLOY_AMOUNT_USD)
        except Exception as e:
            log.info(f"[GRID] auto-deploy stopped for this sweep - {e}")
            return
        created += 1
        selection_reason = "next eligible Adaptive Fleet stage" if GRID_ADAPTIVE_FLEET_ENABLED else "real best-ranked coin available right now"
        await _log_activity_safe(
            branch.bot_name, branch.product_id, "SPAWN",
            f"🌱🔁 Auto-deployed ${GRID_AUTO_DEPLOY_AMOUNT_USD:.2f} of real unallocated free cash into a brand-new "
            f"grid branch on {branch.product_id} - {selection_reason}",
        )
        log.info(
            f"[GRID] 🌱🔁 auto-deployed ${GRID_AUTO_DEPLOY_AMOUNT_USD:.2f} of real free cash into "
            f"{branch.bot_name} ({branch.product_id}) - {selection_reason}"
        )


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
    async with get_session_factory()() as db:
        result = await db.execute(
            select(CryptoGridSlice).where(CryptoGridSlice.bot_name == bot_name).order_by(CryptoGridSlice.opened_at.asc())
        )
        return list(result.scalars().all())


async def _log_grid_trade(bot_name, product_id, entry_price, exit_price, qty, pnl, opened_at,
                          entry_expected_price=None, exit_expected_price=None):
    """Real, persisted record of one completed real grid-slice round
    trip. Best-effort, deliberately never allowed to raise - a logging
    failure here must never affect the real trade or the real
    allocated_usd update that already happened at the call site, same
    defensive pattern every other trade-history logger in this codebase
    already uses."""
    try:
        async with get_session_factory()() as db:
            db.add(CryptoGridTradeHistory(
                bot_name=bot_name, product_id=product_id, entry_price=entry_price,
                exit_price=exit_price, qty=qty, pnl=round(pnl, 2), opened_at=opened_at,
                entry_expected_price=entry_expected_price,
                exit_expected_price=exit_expected_price,
            ))
            await db.commit()
        # The memory is written from the same place as the ledger, and is
        # equally best-effort: a lesson that fails to save must never
        # unwind a real sale that already completed.
        try:
            import grid_learning
            await grid_learning.record_closed_trade(product_id, round(pnl, 2))
        except Exception as e:
            log.error(f"[LEARN] lesson not recorded for {product_id} "
                      f"(pnl {pnl:+.2f}) - the real trade is unaffected: {e}")
    except Exception as e:
        # Deliberately still non-fatal - the real sale already happened and
        # allocated_usd is already updated; raising here would not un-sell
        # anything. But this is logged at ERROR, not warning, because a
        # failure here means the round trip is MISSING from the ledger the
        # dashboard computes realized P&L and win rate from. That is not a
        # cosmetic loss: it silently biases the only record of whether this
        # system makes money. When 28cc92c switched this module to the lazy
        # session factory and missed this call site, the resulting NameError
        # was swallowed by this handler and every completed grid round trip
        # went unrecorded while the dashboard kept reporting a stale total
        # as current fact.
        log.error(f"[GRID] ❌ trade-history log FAILED for {bot_name} {product_id} "
                  f"(pnl {pnl:+.2f}) - the real trade is unaffected, but this round trip is "
                  f"MISSING from the realized-P&L ledger: {e}")


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


def _grid_slice_net_pnl(
    qty: float,
    entry_price: float,
    exit_price: float,
    round_trip_fee_rate: float = None,
) -> float:
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
    exit_price) * (round_trip_fee_rate / 2) - the real round-trip taker
    fee on both legs' notional.

    round_trip_fee_rate: the REAL rate for this account, from
    get_effective_round_trip_fee_rate(). It is a required-in-practice
    argument passed by every real caller; it defaults to None only so the
    signature stays callable from a sync context, and None falls back to
    the deliberately-conservative rate rather than the old optimistic
    hardcoded engine.ROUND_TRIP_FEE_RATE - see
    CONSERVATIVE_ROUND_TRIP_FEE_RATE for why erring high is the safe
    direction here."""
    rate = round_trip_fee_rate if round_trip_fee_rate is not None else CONSERVATIVE_ROUND_TRIP_FEE_RATE
    gross = qty * (exit_price - entry_price)
    fee = qty * (entry_price + exit_price) * (rate / 2)
    return gross - fee


def _slice_rate(s, round_trip_fee_rate, exit_leg_rate):
    """The real round-trip rate for ONE slice. When exit_leg_rate is given
    (maker orders live), each slice is priced with the rate its own BUY leg
    really paid plus the rate its SELL leg is expected to pay - the two can
    genuinely differ once maker and market fills are mixed. Otherwise every
    slice shares the one flat rate, exactly as before."""
    if exit_leg_rate is None:
        return round_trip_fee_rate
    entry_rate = getattr(s, "entry_fee_rate", None)
    if entry_rate is None or entry_rate <= 0:
        entry_rate = exit_leg_rate
    return entry_rate + exit_leg_rate


def _pick_profitable_slice_to_sell(slices: list, price: float, round_trip_fee_rate: float = None,
                                   exit_leg_rate: float = None):
    """Real fix for a confirmed-live bug: the account owner spotted a
    branch's own real closed trades netting a real loss (-$1.57 over 4
    real trades on a DOGE-USD branch) while the branch's real live chart
    showed every open slice sitting red - real evidence the sell trigger
    was firing and realizing losses, not just "waiting for a profitable
    price" the way the design always intended.

    Root cause: the OLD code always sold `slices[0]` (the literal oldest
    slice, strict FIFO) the instant `price >= branch.reference_price *
    (1 + grid_pct)` - but that trigger is evaluated against the branch's
    own shared `reference_price`, which resets to the real fill price on
    EVERY buy AND every sell (see run_grid_branch_cycle's own real dip-buy
    block). A branch that keeps buying real dips lower and lower drags
    `reference_price` down WITH it - so a later real price "rise" can
    clear the reference-based trigger while still sitting BELOW the
    OLDEST slice's own real entry price. The old code sold that oldest
    slice anyway, unconditionally, realizing a real, guaranteed loss on
    it even though the trigger that fired was labeled a "rise."

    Fixed by never selling ANY slice unless doing so is itself genuinely
    net-profitable (the exact same real fee-adjusted _grid_slice_net_pnl
    formula every other real P&L figure in this file already uses) -
    walks the real open slices oldest-first (preserving FIFO as the tie-
    break, same intent as before) and returns the first one that would
    net a real profit at the current price. A newer slice bought closer
    to (or after) the real reference-price reset is very likely to
    qualify even when an older, stuck slice doesn't - so a real rise
    still typically sells SOMETHING this cycle, just never a real loss.
    Returns None when NOT ONE open slice would net a real profit at this
    price - the caller then skips selling entirely rather than forcing
    any real loss, same "never force a sale into a loss" principle
    already established elsewhere in this file (see the QUICK_PROFIT/
    giveback-net-of-fees history)."""
    for s in slices:
        if _grid_slice_net_pnl(s.qty, s.entry_price, price, _slice_rate(s, round_trip_fee_rate, exit_leg_rate)) > 0:
            return s
    return None


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


async def _grid_branch_recent_trades(bot_name: str, limit: int) -> list:
    """One branch's own most recent REAL completed trades, newest first -
    the real judge _maybe_self_tune_branch_spacing() uses to decide
    whether this specific branch has genuinely been struggling or doing
    well lately."""
    async with get_session_factory()() as db:
        result = await db.execute(
            select(CryptoGridTradeHistory)
            .where(CryptoGridTradeHistory.bot_name == bot_name)
            .order_by(desc(CryptoGridTradeHistory.closed_at))
            .limit(limit)
        )
        return list(result.scalars().all())


async def _maybe_self_tune_branch_spacing(branch: CryptoGridBranch):
    """Real, hourly, per-branch self-tuning of average-swing spacing - see
    SELF_TUNE_INTERVAL_SECONDS's own comment for the full real reasoning
    and the reasoning for its bounds. Judges THIS branch's own last
    SELF_TUNE_LOOKBACK_TRADES real closed trades (never a backtest
    simulation) and, only once there's enough real evidence:
      - a genuinely poor real win rate WIDENS this branch's own spacing
        multiplier by one real step (more conservative, more fee-safe
        breathing room) - never below AVG_SWING_SPACING_MULTIPLIER
        (already the real validated default) or above SELF_TUNE_MAX_MULTIPLIER.
      - a genuinely strong real win rate EASES it back down by one real
        step toward that same validated 1.5x default - never below it.
    A change is only ever made and persisted when it's actually different
    from the branch's current real multiplier - a no-op cycle writes
    nothing and logs nothing. Best-effort: a real failure here (a DB
    hiccup) never touches this branch's own trading - caught and logged
    by the caller, run_grid_self_tuning_sweep()."""
    trades = await _grid_branch_recent_trades(branch.bot_name, SELF_TUNE_LOOKBACK_TRADES)
    if len(trades) < SELF_TUNE_LOOKBACK_TRADES:
        return  # not enough real evidence yet for this specific branch

    wins = sum(1 for t in trades if t.pnl is not None and t.pnl > 0)
    win_rate = wins / len(trades) * 100

    current = branch.self_tuned_multiplier if branch.self_tuned_multiplier is not None else AVG_SWING_SPACING_MULTIPLIER
    new_multiplier = current
    reason = None
    if win_rate < SELF_TUNE_POOR_WIN_RATE_PCT and current < SELF_TUNE_MAX_MULTIPLIER:
        new_multiplier = round(min(SELF_TUNE_MAX_MULTIPLIER, current + SELF_TUNE_STEP), 2)
        reason = (
            f"a rough real stretch ({wins}/{len(trades)} = {win_rate:.0f}% win rate over its last "
            f"{len(trades)} real trades) - widening to trade more conservatively"
        )
    elif win_rate >= SELF_TUNE_GOOD_WIN_RATE_PCT and current > SELF_TUNE_MIN_MULTIPLIER:
        new_multiplier = round(max(SELF_TUNE_MIN_MULTIPLIER, current - SELF_TUNE_STEP), 2)
        reason = (
            f"a strong real stretch ({wins}/{len(trades)} = {win_rate:.0f}% win rate over its last "
            f"{len(trades)} real trades) - easing back toward its validated {AVG_SWING_SPACING_MULTIPLIER}x default"
        )

    if reason is None or abs(new_multiplier - current) < 1e-9:
        return  # no real change to make this hour

    async with get_session_factory()() as db:
        result = await db.execute(select(CryptoGridBranch).where(CryptoGridBranch.bot_name == branch.bot_name))
        row = result.scalar_one_or_none()
        if not row:
            return
        row.self_tuned_multiplier = new_multiplier
        await db.commit()
    branch.self_tuned_multiplier = new_multiplier

    msg = (
        f"📐 {branch.bot_name} self-tuned its own spacing multiplier {current}x -> {new_multiplier}x after "
        f"{reason}."
    )
    log.info(f"[GRID] {msg}")
    await _log_activity_safe(branch.bot_name, branch.product_id, "TUNE", msg)


async def run_grid_self_tuning_sweep():
    """Real, hourly driver for every real grid branch's own self-tuning -
    called from run_grid_branches_cycle(), throttled by
    _last_grid_self_tune_at/SELF_TUNE_INTERVAL_SECONDS. Runs across EVERY
    real branch on record (not just currently-active ones - a paused
    branch still has real trade history worth learning from, and this way
    its own spacing is already tuned correctly the moment it's resumed),
    one at a time so a real failure on one branch's own evaluation can
    never block another's."""
    branches = await get_grid_branches()
    for branch in branches:
        try:
            await _maybe_self_tune_branch_spacing(branch)
        except Exception as e:
            log.warning(f"[GRID] self-tuning failed for {branch.bot_name} (non-fatal): {e}")


# Kept only so an existing deployment's variable is still readable and
# reportable. It is NO LONGER what decides whether the gate runs - see
# is_net_edge_gate_active() directly below for why.
NET_EDGE_GATE_ENV_SETTING = os.getenv("GRID_NET_EDGE_GATE_ENABLED", "true").lower() == "true"
NET_EDGE_GATE_KEY = "grid_net_edge_gate_enabled"


async def is_net_edge_gate_active() -> bool:
    """Real, DB-persisted switch for the net-edge gate. Defaults ON.

    Why this replaced a plain env read, 2026-09-25. The live account had
    GRID_NET_EDGE_GATE_ENABLED set to a non-true value in Railway. The
    gate's first statement was

        if not NET_EDGE_GATE_ENABLED:
            return True, "gate disabled"

    so every dip-buy was allowed through with NO economic check at all,
    and - because that early return recorded nothing - the telemetry read
    GATE_PASS 0, GATE_BLOCK 0, GATE_OBSERVE 0, GATE_ERROR 0. A gate
    switched off and a gate never reached were indistinguishable from
    outside. It took reading the config panel to tell them apart, on a
    real $22.90 buy that went in unexamined.

    Two changes follow from that:

      1. The switch lives in the database, like every other real-time
         toggle here, so it is visible and changeable from the dashboard
         instead of hiding in an environment variable nobody re-reads.
      2. It DEFAULTS ON. This is a safety gate; the failure mode of a
         silent default-off is money committed without its economics
         being checked. Turning it off is now a deliberate, recorded act.

    The env var no longer disables it. It is still reported so an operator
    can see the old setting and clear it.
    """
    async with get_session_factory()() as db:
        result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == NET_EDGE_GATE_KEY))
        row = result.scalar_one_or_none()
        if row is None:
            return True
        return bool(row.base_capital and row.base_capital >= 1.0)


async def set_net_edge_gate_active(enabled: bool):
    """Turn the net-edge gate on or off, durably."""
    async with get_session_factory()() as db:
        result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == NET_EDGE_GATE_KEY))
        row = result.scalar_one_or_none()
        if row is None:
            row = TradingBotState(bot_name=NET_EDGE_GATE_KEY, base_capital=0.0, starting_capital=0.0)
            db.add(row)
        row.base_capital = 1.0 if enabled else 0.0
        await db.commit()

# The microstructure veto rides on the same gate but answers a different
# question - the priced gates ask whether the STEP is worth taking, this
# asks whether NOW is the moment to take it - so it gets its own switch.
#
#   observe   compute it, log every buy it WOULD have blocked, block
#             nothing. The default.
#   enforce   block those buys for real.
#   off       do not compute it at all - saves the API call.
#
# It defaults to OBSERVE, and that default is the whole point.
#
# The gates beside it are derived: spread, fees and depth are costs this
# account provably pays, and a step that does not clear them provably
# loses. Nothing had to be measured for those to be true. This one is
# different in kind - it is an EMPIRICAL claim, that a buy into a book
# leaning the other way performs worse than one into a neutral book, at
# thresholds (0.25 / 0.30) that arrived as constants in a code sample
# with no measurement behind them on this exchange, these nine coins, or
# this account's holding period.
#
# And the only thing it can ever do is remove trades from the grid bot,
# which at the time this was written was the sole profitable component in
# the system, by its own live per-branch realized figures. An unmeasured
# filter in front of the one thing that works is the highest-regret
# change available, and "it is probably right" is not evidence.
#
# So it runs, logs, and blocks nothing until the log says otherwise.
# Flip to enforce when the MICRO-OBSERVE lines show a population worth
# removing - and by then that is a measurement, not a guess.
_VETO_MODES = ("observe", "enforce", "off")
MICROSTRUCTURE_VETO_MODE = os.getenv("GRID_MICROSTRUCTURE_VETO_MODE", "observe").strip().lower()
if MICROSTRUCTURE_VETO_MODE not in _VETO_MODES:
    # An unreadable mode resolves to the harmless one. A typo in a deploy
    # variable must never be the reason live buys start getting blocked.
    log.warning(f"[GRID] GRID_MICROSTRUCTURE_VETO_MODE={MICROSTRUCTURE_VETO_MODE!r} "
                f"is not one of {_VETO_MODES} - falling back to 'observe'")
    MICROSTRUCTURE_VETO_MODE = "observe"


async def _record_gate_decision(bot_name: str, product_id: str, verdict: str, reason: str):
    """Write one gate verdict to the shared activity feed the dashboard reads.

    The gate already logs to Railway, but Railway logs are not a dashboard
    and the account owner cannot watch them from a phone. Every verdict -
    pass, block, and the observe-mode counterfactual - lands here so the
    Live Ops page can show the gate deciding in real time. That is the
    difference between "the deploy succeeded" and "I can see it working".

    Reuses crypto_family_tree_bot._log_activity, which is already
    append-only, self-trimming and contractually unable to raise. Imported
    lazily because these two modules import each other's world at startup
    and a top-level import here would close the cycle.

    Never allowed to raise, for the same reason _log_activity is not:
    telemetry that can break a trade is worse than no telemetry.
    """
    try:
        import crypto_family_tree_bot as tree
        await tree._log_activity(bot_name, product_id, verdict, reason)
    except Exception as e:
        log.warning(f"[GRID] gate telemetry write failed (non-fatal, trading unaffected): {e}")


async def _net_edge_gate_ok(session, product_id: str, grid_pct: float, slice_usd: float,
                            bot_name: str = "grid"):
    """Should this dip actually be bought? Returns (ok, reason).

    Four things the dip trigger alone cannot see, checked against the real
    book right before the order rather than against a config value:

      spread    paid the instant the order crosses, before the trade has
                done anything, and unrecoverable inside one grid step
      depth     a slice at or above the visible depth does not trade AT
                the top of book, it trades THROUGH it, and its exit then
                finds nothing to sell into
      net edge  one completed step must clear the real round trip plus
                adverse selection, priced off the coin's own volatility
      pressure  a book stacked with sellers and a tape printing sells is
                the same step at a worse moment - the cost model prices
                the trade, this prices the timing

    The first three are PRICED gates: derived from costs this account
    provably pays, they decide buys for real and fail closed.

    The fourth is different in kind and is treated differently. It is an
    empirical claim rather than a derived one, so it runs in OBSERVE mode
    by default - it logs every buy it would have blocked and blocks none
    of them, until those logs justify enforcing it. It is also a VETO
    ONLY, never a bonus: a friendly book does not widen the expected move
    here, because crediting a tight spread on the revenue side would
    double-count it against the spread already subtracted above.

    FAILS CLOSED, but only for this cycle. A book that cannot be read is
    not a book that is fine, so the buy is skipped - and the branch
    re-checks on its next cycle 30 seconds later, so a transient fetch
    failure costs one dip, never the branch. That is the opposite of this
    codebase's usual fail-open rule for missing data, and deliberately so:
    those gates fail open because blocking on missing data would stop a
    KNOWN-GOOD action, while this one exists precisely to decide whether
    the action is good at all.

    Turn off from the dashboard (POST /grid-status/net-edge-gate) to
    restore the previous buy-every-dip behaviour. The environment variable
    GRID_NET_EDGE_GATE_ENABLED no longer disables it - see
    is_net_edge_gate_active() for why that changed.
    """
    if not await is_net_edge_gate_active():
        # RECORDED, not silent. A disabled gate used to return here writing
        # nothing, which made "switched off" and "never reached" look
        # identical on the dashboard - four zeros either way. Every buy
        # that goes in unchecked now says so in the feed.
        await _record_gate_decision(
            bot_name, product_id, "GATE_DISABLED",
            f"net-edge gate is OFF - ${slice_usd:,.2f} buy allowed with NO economic check")
        return True, "gate disabled"
    try:
        import crypto_nine_coin_scanner as scanner
        bid, ask, bid_depth, ask_depth = await engine.get_book_top_and_depth(session, product_id)
        swing = await engine.get_average_hourly_swing_pct(session, product_id)
        # The live fee tier the account really pays, not the code default -
        # it is the largest term in the net-edge sum, so a stale value is
        # the one input most likely to flip a verdict.
        _maker, taker, _tier, _err = await engine.get_real_fee_tier(session)
        # WHICH LEG THIS ROUND TRIP WILL REALLY PAY.
        #
        # This priced taker unconditionally, and that was about to make the
        # maker-only switch do nothing. The switch drops fee_safe_floor_pct()
        # from 1.70% to 0.90%, but THIS is the gate that actually decides
        # buys - and it would have kept refusing every one of them with
        # "a 2.00% target does not clear 2.17% of costs", because it was
        # still pricing a market fallback that no longer exists.
        #
        # Under maker-only, grid_buy()/grid_sell() have no market fallback:
        # a leg that does not fill as a maker does not fill at all. So the
        # taker leg is not a worse case this trade can reach, it is a path
        # that was removed. Same rule and same guards as
        # worst_case_leg_fee_rate(), deliberately, so the gate and the
        # spacing floor can never disagree about what a round trip costs.
        #
        # Guards, in order: the maker rate must be MEASURED (it comes from
        # the live fee tier right above), is_maker_only_active() fails
        # closed, and the result is clamped to taker because no arrangement
        # of maker orders costs more than paying taker twice.
        leg = taker
        if taker and _maker and await is_maker_only_active():
            leg = min(float(_maker), float(taker))
        fee_round_trip = (leg * 2) if leg else scanner.DEFAULT_TAKER_ROUND_TRIP
        ok, reason, _detail = scanner.evaluate_grid_step(
            product_id, grid_pct, swing,
            best_bid=bid, best_ask=ask,
            bid_depth_usd=bid_depth, ask_depth_usd=ask_depth,
            slice_usd=slice_usd, fee_round_trip=fee_round_trip,
        )
        if not ok:
            await _record_gate_decision(bot_name, product_id, "GATE_BLOCK", reason)
            return False, reason

        # Only now, once the economics have passed, is the timing worth an
        # extra API call. Ordering it this way also keeps the call budget
        # on the coins that could actually trade.
        if MICROSTRUCTURE_VETO_MODE != "off":
            imbalance = scanner.book_imbalance(bid_depth, ask_depth)
            trades = await engine.get_recent_market_trades(session, product_id)
            aggression = scanner.trade_aggression(trades)
            # "long" always: this is spot, and a hostile book means hold
            # cash for 30 seconds, never sell something the bot cannot
            # short.
            micro_ok, micro_reason = scanner.microstructure_veto(
                "long", imbalance, aggression)
            if not micro_ok:
                if MICROSTRUCTURE_VETO_MODE == "enforce":
                    await _record_gate_decision(bot_name, product_id, "GATE_BLOCK", micro_reason)
                    return False, micro_reason
                # Observe mode: the buy proceeds. This record is the whole
                # experiment - one per buy the veto would have taken away,
                # tagged so it can be counted and read against what those
                # same buys went on to do.
                log.info(f"[GRID] MICRO-OBSERVE {product_id}: would have blocked this "
                         f"buy - {micro_reason} (observe mode, buy NOT blocked)")
                reason = f"{reason}; WOULD-HAVE-VETOED: {micro_reason}"
                await _record_gate_decision(bot_name, product_id, "GATE_OBSERVE", reason)
                return True, reason
            reason = f"{reason}; {micro_reason}"
        await _record_gate_decision(bot_name, product_id, "GATE_PASS", reason)
        return True, reason
    except Exception as e:
        # An error evaluating the gate is not permission to skip it.
        reason = f"gate could not be evaluated ({type(e).__name__}: {e}) - not buying blind"
        await _record_gate_decision(bot_name, product_id, "GATE_ERROR", reason)
        return False, reason


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
        async with get_session_factory()() as db:
            result = await db.execute(select(CryptoGridBranch).where(CryptoGridBranch.bot_name == branch.bot_name))
            row = result.scalar_one_or_none()
            if row:
                row.peak_equity = stored_peak_equity
                await db.commit()
        branch.peak_equity = stored_peak_equity
    drawdown_pct = (stored_peak_equity - equity) / stored_peak_equity if stored_peak_equity > 0 else 0.0
    drawdown_breached = drawdown_pct >= GRID_DRAWDOWN_BREAKER_PCT

    # ---- Real "promote a backtested candidate" override (Grid
    # Level/Spacing Comparison) - takes precedence over BOTH average-swing
    # and fee-tier dynamic spacing when active, so a branch can never
    # disagree with what was actually clicked "promote" on. Also re-caps
    # num_levels live, every single cycle (not just at the next add-cash/
    # withdraw event), so an already-existing branch picks up a fresh
    # promotion immediately rather than waiting on its own next cash
    # change. "live_default" (the default, nothing ever promoted) changes
    # nothing here - byte-for-byte the same behavior as before this
    # mechanism existed.
    grid_spacing_override = await get_live_grid_spacing_override()
    override_cfg = GRID_LEVEL_SPACING_CANDIDATES.get(grid_spacing_override)
    if override_cfg is not None:
        real_effective_levels = max(1, min(_safe_num_levels_for_allocation(branch.allocated_usd), override_cfg["num_levels"]))
        if real_effective_levels != branch.num_levels:
            async with get_session_factory()() as db:
                result = await db.execute(select(CryptoGridBranch).where(CryptoGridBranch.bot_name == branch.bot_name))
                row = result.scalar_one_or_none()
                if row:
                    row.num_levels = real_effective_levels
                    await db.commit()
            log.info(f"[GRID] {branch.bot_name}: promoted candidate {grid_spacing_override!r} capped real levels {branch.num_levels} -> {real_effective_levels}")
            branch.num_levels = real_effective_levels

    # ---- Real dynamic spacing (opt-in; average-swing takes precedence over fee-tier) ----
    # Only ever recomputed/persisted when the account owner has actually
    # turned one of these on - a branch running the fixed default spacing
    # pays zero extra real API cost for this, every cycle. A real promoted
    # candidate override (above) wins over BOTH of these outright.
    # Average-swing spacing (real, evidence-backed - see
    # AVG_SWING_SPACING_MODE_KEY's own comment) is checked next and wins
    # if active; fee-tier spacing (a real no-op at the base fee tier) only
    # applies when neither of the other two is, so none of the three can
    # ever disagree about which real grid_pct a branch should be using
    # this cycle.
    grid_pct = branch.grid_pct
    new_grid_pct = None
    spacing_log_note = None
    if override_cfg is not None:
        new_grid_pct = override_cfg["grid_pct"]
        spacing_log_note = f"real promoted candidate {grid_spacing_override!r}"
    elif await is_avg_swing_spacing_active():
        effective_multiplier = branch.self_tuned_multiplier if branch.self_tuned_multiplier is not None else AVG_SWING_SPACING_MULTIPLIER
        swing_pct, avg_swing_pct = await compute_avg_swing_grid_pct(session, branch.product_id, multiplier=effective_multiplier)
        new_grid_pct = swing_pct
        spacing_log_note = (
            f"real avg swing {avg_swing_pct*100:.3f}% x {effective_multiplier}"
            + (" (self-tuned)" if branch.self_tuned_multiplier is not None else "")
            if avg_swing_pct is not None else "real avg-swing lookup failed - fell back to default"
        )
    elif await is_dynamic_spacing_active():
        dynamic_pct, tier_name, taker_rate = await compute_dynamic_grid_pct(session)
        new_grid_pct = dynamic_pct
        spacing_log_note = (
            f"real fee tier {tier_name or 'unknown'}, taker {taker_rate*100:.3f}%"
            if taker_rate is not None else "real fee tier lookup failed - fell back to default"
        )

    # THE FEE GUARANTEE, applied to whichever source won above.
    #
    # Per the account owner, 2026-09-25: fees are not to be a recurring
    # conversation - the code must simply never let a target sit below what
    # a round trip costs. That is enforced structurally here rather than
    # left to whoever chooses a spacing.
    #
    # The avg-swing and fee-tier paths already floored themselves. The
    # PROMOTED OVERRIDE did not, and it wins over both - so a promoted
    # candidate tighter than the fee floor would have traded every cycle at
    # a guaranteed loss, with no warning. Nothing live hit this (the
    # promoted 3_levels_2.5pct is 2.5% against a ~1.2% floor), but "nothing
    # has hit it yet" is not a guarantee. This makes it unreachable.
    # THE HOLE THIS CLOSES, found live 2026-09-25 with real money at stake.
    #
    # The guarantee below only ran when a dynamic source had SET
    # new_grid_pct. With the promoted override, avg-swing spacing and
    # fee-tier spacing all switched off, new_grid_pct stays None and the
    # whole check was skipped - so a branch simply KEPT whatever spacing it
    # was born with, unchecked.
    #
    # create_grid_branch's default is 1.00%. Five branches (ETC, FLOKI,
    # BCH, DOGE, BONK) were created that way minutes after the three
    # spacing modes were turned off, and every one sat at 1.00% against a
    # 1.70% floor. On the live fee tier a completed round trip at 1.00%
    # nets -$0.02 at maker rates and -$0.12 at taker: a guaranteed loss on
    # every cycle, with nothing in the log to say so.
    #
    # The floor is a property of the SPACING A BRANCH WILL TRADE AT, not of
    # the mechanism that happened to choose it. So it is applied to the
    # branch's own value whenever no dynamic source spoke.
    if new_grid_pct is None:
        _floor = await fee_safe_floor_pct()
        if branch.grid_pct < _floor:
            log.warning(
                f"[GRID] {branch.bot_name}: its own stored spacing {branch.grid_pct*100:.3f}% is "
                f"below the fee-safe floor {_floor*100:.3f}% and no dynamic source is active - "
                f"a full round trip at that spacing loses money. Raising to the floor."
            )
            new_grid_pct = _floor
            spacing_log_note = "raised to fee-safe floor (no dynamic source active)"

    if new_grid_pct is not None:
        floor = await fee_safe_floor_pct()
        if new_grid_pct < floor:
            log.warning(
                f"[GRID] {branch.bot_name}: {spacing_log_note or 'spacing'} asked for "
                f"{new_grid_pct*100:.3f}%, below the fee-safe floor {floor*100:.3f}% - "
                f"a full round trip at that spacing loses money. Using the floor."
            )
            new_grid_pct = floor
            spacing_log_note = f"{spacing_log_note or 'spacing'} (raised to fee-safe floor)"

    # THE FLEET MINIMUM, applied before the gate-clearing floor so the two
    # compose: this sets the measured baseline, and the gate floor below
    # raises it further on any coin whose own volatility demands more.
    _current_step = new_grid_pct if new_grid_pct is not None else branch.grid_pct
    if _current_step < FLEET_MIN_STEP_PCT - 1e-9:
        new_grid_pct = FLEET_MIN_STEP_PCT
        spacing_log_note = (f"{spacing_log_note or 'spacing'} (raised {_current_step*100:.2f}% "
                            f"-> {FLEET_MIN_STEP_PCT*100:.2f}% fleet minimum)")

    # THE GATE-CLEARING FLOOR.
    #
    # Everything above guarantees the step clears FEES. The net-edge gate
    # additionally charges spread and adverse selection, so a step can clear
    # every check here and still be refused on every single buy - which is
    # exactly what happened to NEAR-USD: 171 consecutive refusals at a 2.00%
    # step that needed 2.36%, with nothing in the system able to move it.
    #
    # Applied last, so it can only ever raise what the other sources chose,
    # and only when the gate is actually the thing doing the refusing.
    if auto_widen_enabled():
        _probe_step = new_grid_pct if new_grid_pct is not None else branch.grid_pct
        _slice = (branch.allocated_usd or 0.0) / max(1, branch.num_levels or 10)
        _needed, _why = await gate_clearing_floor_pct(
            session, branch.product_id, _probe_step, _slice)
        if _needed is not None and _needed > _probe_step + 1e-9:
            if _needed <= GATE_CLEARING_MAX_PCT:
                log.warning(
                    f"[GRID] {branch.bot_name}: {branch.product_id} at "
                    f"{_probe_step*100:.2f}% cannot clear its own net-edge gate "
                    f"({_why['reason'] if isinstance(_why, dict) else _why}). "
                    f"Widening to {_needed*100:.2f}% - the gate is not relaxed, the "
                    f"step is moved to where it passes."
                )
                new_grid_pct = _needed
                spacing_log_note = (f"{spacing_log_note or 'spacing'} "
                                    f"(widened to clear the net-edge gate)")
            else:
                log.warning(
                    f"[GRID] {branch.bot_name}: {branch.product_id} would need a "
                    f"{_needed*100:.2f}% step to clear its gate, over the "
                    f"{GATE_CLEARING_MAX_PCT*100:.2f}% bound. Leaving the spacing alone "
                    f"and letting the gate keep refusing - this coin is too expensive "
                    f"to grid right now, which is an answer, not a failure."
                )

    if new_grid_pct is not None and abs(new_grid_pct - branch.grid_pct) > 1e-9:
        async with get_session_factory()() as db:
            result = await db.execute(select(CryptoGridBranch).where(CryptoGridBranch.bot_name == branch.bot_name))
            row = result.scalar_one_or_none()
            if row:
                row.grid_pct = new_grid_pct
                await db.commit()
        log.info(f"[GRID] {branch.bot_name}: dynamic spacing {branch.grid_pct*100:.2f}% -> {new_grid_pct*100:.2f}% ({spacing_log_note})")
        branch.grid_pct = new_grid_pct
    if new_grid_pct is not None:
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

        gate_ok, gate_reason = await _net_edge_gate_ok(
            session, branch.product_id, grid_pct, spend, bot_name=branch.bot_name)
        if not gate_ok:
            log.info(f"[GRID] {branch.bot_name}: ⛔ net-edge gate - {gate_reason}")
            return

        # What the fleet already learned about this coin, from its own
        # closed round trips. ADVISORY unless enforcement is explicitly
        # switched on in the database - see grid_learning.check_before_buy.
        # It fails OPEN by design: an unreadable memory must never be the
        # thing that quietly halts trading.
        try:
            import grid_learning
            memo = await grid_learning.check_before_buy(branch.product_id)
            if memo.get("trades"):
                log.info(f"[LEARN] {branch.bot_name}: {memo['lesson']}")
            if not memo.get("allow", True):
                log.warning(f"[GRID] {branch.bot_name}: 🧠 memory blocked this buy - {memo['lesson']}")
                await _log_activity_safe(branch.bot_name, branch.product_id, "LESSON_BLOCK",
                                         f"🧠 buy blocked by the fleet's own record: {memo['lesson']}")
                return
        except Exception as e:
            log.warning(f"[LEARN] {branch.bot_name}: memory unavailable ({e}) - trading anyway")

        fill = await grid_buy(session, spend, branch.product_id)
        if not fill:
            reason = engine._last_order_error.get(branch.product_id, "no reason reported")
            log.warning(f"[GRID] {branch.bot_name}: real grid buy into {branch.product_id} did not fill - will retry next cycle")
            # Durable, so rejections can be COUNTED. They were only ever
            # logged before, which meant "orders rejected" could not be
            # reported at all and an execution problem could hide behind a
            # normal-looking gate pass rate.
            await _record_gate_decision(branch.bot_name, branch.product_id, "ORDER_REJECTED",
                                        f"buy ${spend:,.2f} did not fill - {reason}")
            return
        filled_qty, filled_price, buy_leg_fee = fill

        # ── SHADOW MODE: Log order created (fire-and-forget, non-blocking) ────
        if SHADOW_MODE_ENABLED and shadow_manager:
            try:
                order_id = f"{branch.bot_name}_{int(time.time()*1000)}"
                shadow_manager.on_order_created(
                    client_order_id=order_id,
                    symbol=branch.product_id,
                    side='BUY',
                    price=filled_price,
                    quantity=filled_qty
                )
            except Exception as e:
                log.warning(f"[SHADOW] Failed to log order (non-blocking): {e}")

        async with get_session_factory()() as db:
            # entry_fee_rate records the rate this leg REALLY paid (maker or
            # taker), so this slice can be priced honestly when it later sells.
            db.add(CryptoGridSlice(bot_name=branch.bot_name, product_id=branch.product_id,
                                   entry_price=filled_price, qty=filled_qty,
                                   entry_fee_rate=buy_leg_fee,
                                   # `price` is the live price this cycle read
                                   # BEFORE deciding to buy - what the bot
                                   # believed it would pay. Stored beside what
                                   # it actually paid so the gap is measurable
                                   # later; it cannot be recovered from the
                                   # fill alone.
                                   entry_expected_price=price))
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

    # ---- Real rise: sell the oldest open slice that's actually profitable ----
    # Never gated on drawdown_breached - an existing open slice keeps
    # selling normally regardless (see the drawdown-breach block above);
    # this branch's own real recovery path back toward its peak.
    #
    # Real bug fixed here: the reference-price rise trigger below is a
    # necessary condition to consider selling at all, but it was
    # previously treated as SUFFICIENT to sell the literal oldest slice
    # unconditionally - which could (and did, confirmed live on a real
    # DOGE-USD branch) realize a genuine loss on that specific slice, since
    # reference_price can drift below an older slice's own entry price
    # after later, cheaper dip-buys reset it. _pick_profitable_slice_to_sell
    # now requires the slice actually being sold to itself be real,
    # fee-adjusted net-profitable at the current price - see its own
    # docstring for the full story. A rise that clears the trigger but
    # finds nothing genuinely profitable to sell simply waits, rather than
    # ever locking in a real loss.
    real_fee_rate = await get_effective_round_trip_fee_rate()
    exit_leg_rate = await expected_leg_fee_rate()
    if price >= branch.reference_price * (1 + grid_pct) and slices:
        oldest = _pick_profitable_slice_to_sell(slices, price, real_fee_rate, exit_leg_rate)
        if oldest is None:
            log.info(
                f"[GRID] {branch.bot_name}: real rise trigger fired (${price:,.4f} >= "
                f"${branch.reference_price * (1 + grid_pct):,.4f}) but no open slice would net a real "
                f"profit at this price - holding every slice, waiting for a genuinely profitable one"
            )
            return
        fill = await grid_sell(session, oldest.qty, branch.product_id)
        if not fill:
            log.warning(f"[GRID] {branch.bot_name}: real grid sell of {branch.product_id} did not fill - will retry next cycle")
            return
        filled_qty, filled_price, sell_leg_fee = fill
        # Priced with the rate THIS slice's buy leg really paid plus the rate
        # its sell leg really paid - not one assumed rate for both.
        pnl = _grid_slice_net_pnl(filled_qty, oldest.entry_price, filled_price,
                                  await slice_round_trip_fee_rate(oldest, sell_leg_fee))
        new_balance = branch.allocated_usd + pnl

        # ── SHADOW MODE: Log position closed (fire-and-forget, non-blocking) ────
        if SHADOW_MODE_ENABLED and shadow_manager:
            try:
                order_id = f"{branch.bot_name}_{oldest.id}_{int(time.time()*1000)}"
                hold_time_minutes = int((time.time() - oldest.opened_at.timestamp()) / 60) if oldest.opened_at else 0
                gross_pnl = filled_qty * (filled_price - oldest.entry_price)
                exit_reason = 'profit_target' if pnl >= 0 else 'stop_loss'

                shadow_manager.on_position_closed(
                    client_order_id=order_id,
                    symbol=branch.product_id,
                    entry_price=oldest.entry_price,
                    exit_price=filled_price,
                    quantity=filled_qty,
                    realized_pnl=gross_pnl,
                    total_fees=(await slice_round_trip_fee_rate(oldest, sell_leg_fee)) * filled_qty * oldest.entry_price / 100,
                    hold_time_minutes=hold_time_minutes,
                    exit_reason=exit_reason,
                    risk_amount=branch.allocated_usd / branch.num_levels
                )
            except Exception as e:
                log.warning(f"[SHADOW] Failed to log position close (non-blocking): {e}")

        async with get_session_factory()() as db:
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
        await _log_grid_trade(branch.bot_name, branch.product_id, oldest.entry_price,
                              filled_price, filled_qty, pnl, oldest.opened_at,
                              entry_expected_price=oldest.entry_expected_price,
                              exit_expected_price=price)
        is_true_oldest = slices[0].id == oldest.id
        msg = (
            f"{'📈' if pnl >= 0 else '📉'} {branch.bot_name} GRID SELL: sold "
            f"{'the oldest' if is_true_oldest else 'the oldest PROFITABLE (skipped a stuck older)'} "
            f"real slice of {branch.product_id} @ ${filled_price:,.2f} "
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
                async with get_session_factory()() as db:
                    fresh_result = await db.execute(select(CryptoGridBranch).where(CryptoGridBranch.bot_name == branch.bot_name))
                    fresh_branch = fresh_result.scalar_one_or_none()
                if fresh_branch and fresh_branch.active:
                    await _maybe_rotate_one_grid_branch(fresh_branch, after_sale=True)
            except Exception as e:
                log.warning(f"[GRID] {branch.bot_name}: post-sale auto-rotate check failed (non-fatal, will retry next sweep): {e}")
        return


async def check_shadow_mode_status():
    """Periodic check of shadow mode learning engine progress - logs current
    status every SHADOW_MODE_MONITOR_INTERVAL_SECONDS. Non-blocking, graceful
    failure if shadow mode unavailable."""
    if not SHADOW_MODE_ENABLED or not shadow_manager:
        return

    try:
        status = shadow_manager.get_status()
        trades_logged = status.get('trades_logged', 0)
        target = 50
        improvement_pct = status.get('improvement_pct', 0.0)
        quality_correlation = status.get('quality_correlation', 0.0)
        agreement_rate = status.get('agreement_rate_pct', 0.0)
        ready_for_controlled_live = status.get('ready_for_controlled_live', False)

        msg = (
            f"[SHADOW] Status: {trades_logged}/{target} trades logged | "
            f"Improvement: {improvement_pct:+.1f}% | "
            f"Quality Corr: {quality_correlation:.3f} | "
            f"Agreement: {agreement_rate:.1f}% | "
            f"CONTROLLED_LIVE ready: {ready_for_controlled_live}"
        )
        log.info(msg)

        # Alert if we just hit the validation threshold
        if trades_logged >= 30 and trades_logged < 35:
            alert_msg = (
                f"[SHADOW] ⚡ VALIDATION THRESHOLD REACHED: {trades_logged}/50 trades. "
                f"Ready for assessment. Run: python scripts/monitor_shadow_mode.py --detail --report"
            )
            log.warning(alert_msg)
    except Exception as e:
        log.debug(f"[SHADOW] Status check failed (non-blocking): {type(e).__name__}: {e}")


GRID_HEARTBEAT_KEY = "grid_bot_last_cycle_at"
_HEARTBEAT_STAGES = {"entered": 1.0, "no_active_branches": 2.0, "cycled": 3.0,
                     # A loop that is alive but refused the lease is NOT the
                     # same as one that merely "entered" and is still working.
                     # Conflating them is what made a 40-minute stall on
                     # 2026-09-26 read as ordinary progress.
                     "lease_refused": 4.0}

# --- THE OWNERSHIP LEASE --------------------------------------------------
# Who is allowed to run the grid loop right now.
#
# The problem this solves, found live on 2026-09-25: the grid can only run
# on the dedicated crypto-trading service, because main.py deliberately
# delegates ("Grid Fleet selected; execution is delegated"). That delegation
# is correct - two processes trading one Coinbase wallet would double-order.
#
# But it means one invisible service failing takes the entire system down
# with NO fallback and no alarm. That is exactly what happened: the master
# switch was off, the dedicated service was not running the loop, and the
# heartbeat read "never recorded a cycle on this database" while $572 sat
# idle and the operator was told repeatedly that things were fine.
#
# A lease makes a takeover safe instead of dangerous. Every process that
# runs a cycle stamps its own identity. A process may run only if it
# already holds the lease, or the lease has gone stale - meaning whoever
# held it has stopped. The dedicated service, cycling every 30s, renews
# constantly and keeps the lease forever; the web service can never steal
# it from a healthy owner. If the dedicated service dies, the lease expires
# and the web service picks the fleet up rather than leaving it dead.
GRID_LEASE_KEY = "grid_bot_loop_owner"
GRID_LEASE_STALE_SECONDS = int(os.getenv("GRID_LEASE_STALE_SECONDS", "180"))

# --- DB-PERSISTED STRATEGY OVERRIDE ---------------------------------------
# The strategy mode was the ONLY setting in this system that could be
# changed exclusively through a Railway environment variable. Every other
# live control - the master switch, spacing, auto-rotate, passive mode, the
# entry variant - is DB-persisted and settable by one POST.
#
# That inconsistency cost an entire day on 2026-09-25.
# CRYPTO_STRATEGY_MODE was stuck reading 'delfina_scalping' through roughly
# six attempts to correct it: edited in place (reverted), deleted
# (confirmed "(unset)"), re-added (reverted again), across a confirmed
# process restart in the confirmed production environment with exactly one
# key of that name. Meanwhile the operator could flip every OTHER control
# instantly, because those live in the database.
#
# prop_bot.is_alpaca_passive_mode's own docstring already named the reason:
# DB-persisted "avoids the exact stray-quote-character class of bug that
# silently disabled the crypto coordinator". The strategy mode simply never
# got the same treatment.
#
# Precedence, deliberately: this override wins over the environment. An
# operator who sets it is making a decision, and the environment variable
# is precisely the thing that could not be trusted to carry one.
CRYPTO_STRATEGY_OVERRIDE_KEY = "crypto_strategy_mode_db_override"
_STRATEGY_CODES = {1.0: "grid_fleet", 2.0: "family_tree",
                   3.0: "btc_compound", 4.0: "multi_pair"}
_STRATEGY_VALUES = {v: k for k, v in _STRATEGY_CODES.items()}


async def get_db_strategy_override():
    """The DB-persisted strategy mode, or None if none is set."""
    try:
        async with get_session_factory()() as db:
            result = await db.execute(
                select(TradingBotState).where(
                    TradingBotState.bot_name == CRYPTO_STRATEGY_OVERRIDE_KEY))
            row = result.scalar_one_or_none()
    except Exception as e:
        log.debug(f"DB strategy override unreadable: {type(e).__name__}: {e}")
        return None
    if row is None or not row.base_capital:
        return None
    return _STRATEGY_CODES.get(float(row.base_capital))


async def set_db_strategy_override(mode):
    """Set (or clear, with None) the DB-persisted strategy mode.

    Only a known strategy is accepted - the same refusal
    crypto_strategy_config makes, for the same reason: an unrecognised
    value must never start a substitute that spends real money.
    """
    if mode is not None and mode not in _STRATEGY_VALUES:
        raise ValueError(
            f"unknown strategy {mode!r} - must be one of "
            f"{sorted(_STRATEGY_VALUES)} or None to clear")
    async with get_session_factory()() as db:
        result = await db.execute(
            select(TradingBotState).where(
                TradingBotState.bot_name == CRYPTO_STRATEGY_OVERRIDE_KEY))
        row = result.scalar_one_or_none()
        if row is None:
            row = TradingBotState(bot_name=CRYPTO_STRATEGY_OVERRIDE_KEY, base_capital=0.0)
            db.add(row)
        row.base_capital = 0.0 if mode is None else _STRATEGY_VALUES[mode]
        await db.commit()
    return mode


def _grid_owner_id() -> str:
    """Stable identity for this process, as an owner of the loop."""
    role = (os.getenv("SERVICE_ROLE") or "web").strip().lower() or "web"
    return f"{role}:{os.getpid()}"


def _owner_hash(owner: str) -> float:
    """TradingBotState stores floats, so an owner is kept as a stable hash.

    Only equality matters - "is the current holder me?" - never the value
    itself, so a hash is enough and avoids a schema change for one string.
    """
    return float(zlib.crc32(owner.encode("utf-8")))


async def acquire_grid_lease() -> tuple:
    """Claim the right to run the grid loop. Returns (allowed, reason).

    Allowed when nobody holds the lease, when this process already holds
    it, or when the holder has gone silent for GRID_LEASE_STALE_SECONDS.
    Refused while a DIFFERENT process is actively renewing - which is what
    makes a fallback owner safe to enable at all.
    """
    me = _grid_owner_id()
    mine = _owner_hash(me)
    now = time.time()
    try:
        async with get_session_factory()() as db:
            result = await db.execute(
                select(TradingBotState).where(TradingBotState.bot_name == GRID_LEASE_KEY))
            row = result.scalar_one_or_none()
            if row is None:
                db.add(TradingBotState(bot_name=GRID_LEASE_KEY,
                                       base_capital=mine, starting_capital=now))
                await db.commit()
                return True, f"lease claimed by {me} (was unheld)"

            held_by, last_seen = float(row.base_capital or 0.0), float(row.starting_capital or 0.0)
            age = now - last_seen
            if held_by == mine:
                row.starting_capital = now
                await db.commit()
                return True, f"lease renewed by {me}"
            if age > GRID_LEASE_STALE_SECONDS:
                row.base_capital = mine
                row.starting_capital = now
                await db.commit()
                return True, (f"lease TAKEN OVER by {me} - previous owner silent "
                              f"for {age:.0f}s (limit {GRID_LEASE_STALE_SECONDS}s)")
            return False, (f"another process holds the loop lease, last seen "
                           f"{age:.0f}s ago - not running here")
    except Exception as e:
        # Fail CLOSED. An unreadable lease must never let a second process
        # start trading the same wallet - the whole point of the lease.
        return False, f"lease check failed ({type(e).__name__}: {e}) - not running"


async def read_grid_lease_state() -> dict:
    """Who holds the loop lease, how fresh it is, and whether THIS process
    could take it. Read-only - never claims, renews or releases.

    Exists because on 2026-09-26 the grid heartbeat read "entered" for over
    forty minutes with no gate events, and the reason was unreachable: the
    only code that knew sat behind a log.debug, and the lease row was not
    surfaced anywhere. Diagnosing a stalled trading loop should not require
    server logs nobody can get to.
    """
    me = _grid_owner_id()
    mine = _owner_hash(me)
    now = time.time()
    try:
        async with get_session_factory()() as db:
            result = await db.execute(
                select(TradingBotState).where(TradingBotState.bot_name == GRID_LEASE_KEY))
            row = result.scalar_one_or_none()
            if row is None:
                return {"held": False, "this_process": me,
                        "note": "no lease row - the next cycle claims it"}
            held_by = float(row.base_capital or 0.0)
            last_seen = float(row.starting_capital or 0.0)
            age = now - last_seen
            is_mine = held_by == mine
            # A negative age means last_seen is in the FUTURE, which no live
            # renewal can produce. That would make `age > STALE` permanently
            # false and deadlock every process out of the loop forever, so it
            # is reported as its own fault rather than as a fresh lease.
            corrupt = age < -5
            return {
                "held": True,
                "this_process": me,
                "held_by_this_process": is_mine,
                "last_seen_age_seconds": round(age, 1),
                "stale_after_seconds": GRID_LEASE_STALE_SECONDS,
                "takeover_possible": bool(age > GRID_LEASE_STALE_SECONDS),
                "timestamp_looks_corrupt": corrupt,
                "note": (
                    "this process owns the loop" if is_mine else
                    ("lease timestamp is in the FUTURE - no process can ever take "
                     "over and the loop is deadlocked until this row is reset"
                     if corrupt else
                     f"another process holds it, last seen {age:.0f}s ago; it "
                     f"becomes claimable after {GRID_LEASE_STALE_SECONDS}s of silence")
                ),
            }
    except Exception as e:
        return {"held": None, "error": f"{type(e).__name__}: {e}"}


async def _record_grid_heartbeat(stage: str):
    """Stamp 'the grid loop reached here, at this moment'.

    Written BEFORE any gate below can return, so it answers the question
    that was unanswerable on 2026-09-25: is the loop running at all?

    That day was spent inferring liveness from a side effect - whether a
    branch's stored grid_pct had been rewritten to match a promoted
    spacing override. The proxy was wrong. The BTC branch had been PAUSED
    since 2026-09-08, and this function filters to active branches, so its
    spacing could never update however healthy the loop was. Hours went
    into "the loop is dead" on the strength of a dial that was never
    connected to it.

    Nine separate switches can stop this system silently: strategy mode,
    SERVICE_ROLE, credentials, STOP_TRADING, the master switch, a branch's
    own active flag, tree passive mode, and the exclusion layers. They all
    produce the identical symptom - nothing happens. A timestamp separates
    "not running" from "running but gated", which no amount of reading
    balances or spacing can.

    `stage` records how far it got, so the heartbeat also names the gate:
    "entered" = the loop is alive, "no_active_branches" = alive with
    nothing to do, "cycled" = it reached real work.
    """
    try:
        async with get_session_factory()() as db:
            result = await db.execute(
                select(TradingBotState).where(TradingBotState.bot_name == GRID_HEARTBEAT_KEY))
            row = result.scalar_one_or_none()
            if row is None:
                row = TradingBotState(bot_name=GRID_HEARTBEAT_KEY, base_capital=0.0)
                db.add(row)
            # base_capital carries epoch seconds, starting_capital the stage -
            # reusing the generic TradingBotState bucket every other flag in
            # this file uses, rather than adding a table for one timestamp.
            row.base_capital = float(time.time())
            row.starting_capital = _HEARTBEAT_STAGES.get(stage, 0.0)
            await db.commit()
    except Exception as e:
        # A heartbeat must never be able to stop the thing it measures.
        log.debug(f"[GRID] heartbeat write failed (non-fatal): {type(e).__name__}: {e}")


async def get_grid_heartbeat() -> dict:
    """Age and stage of the last grid-loop heartbeat, or never-seen."""
    try:
        async with get_session_factory()() as db:
            result = await db.execute(
                select(TradingBotState).where(TradingBotState.bot_name == GRID_HEARTBEAT_KEY))
            row = result.scalar_one_or_none()
    except Exception as e:
        return {"seen": False, "error": f"{type(e).__name__}: {e}"}
    if row is None or not row.base_capital:
        return {"seen": False, "stage": None, "age_seconds": None, "alive": False,
                "detail": "The grid loop has never recorded a cycle on this database."}
    age = round(time.time() - float(row.base_capital), 1)
    by_value = {v: k for k, v in _HEARTBEAT_STAGES.items()}
    return {
        "seen": True,
        "stage": by_value.get(float(row.starting_capital or 0.0), "unknown"),
        "age_seconds": age,
        # CYCLE_SECONDS is 30, so 10 missed cycles is unambiguously dead.
        "alive": age < 300,
        "last_cycle_at": datetime.utcfromtimestamp(float(row.base_capital)).isoformat() + "Z",
    }


async def run_grid_branches_cycle():
    """Real per-cycle driver for every active grid branch - a true no-op
    unless is_grid_bot_active() is on AND at least one real active
    branch exists."""
    # Stamped first, before any gate below can return: a loop that is
    # running but gated must look different from a loop that is not
    # running at all. See _record_grid_heartbeat for what conflating the
    # two cost.
    await _record_grid_heartbeat("entered")
    # Exactly one process may trade this wallet - see acquire_grid_lease.
    # Checked AFTER the heartbeat so a process that is alive but not the
    # owner still proves it is alive, which is how a stalled owner is
    # noticed at all.
    allowed, why = await acquire_grid_lease()
    if not allowed:
        # WARNING, not debug. This is the single way the loop can be alive,
        # heartbeating, and doing no work at all - and at debug level that
        # state is invisible in production. On 2026-09-26 the heartbeat read
        # "entered" for over forty minutes while nothing traded, and the
        # reason was sitting in a log line nobody could see. A loop that has
        # decided not to work must say so at a level someone will read.
        log.warning(f"[GRID] not cycling: {why}")
        await _record_grid_heartbeat("lease_refused")
        return
    if "TAKEN OVER" in why:
        log.warning(f"[GRID] {why}")

    if not await is_grid_bot_active():
        return
    branches = [b for b in await get_grid_branches() if b.active]
    if not branches:
        await _record_grid_heartbeat("no_active_branches")
        return
    await _record_grid_heartbeat("cycled")
    async with engine.aiohttp.ClientSession() as session:
        # Refresh the account's REAL Coinbase fee rate ONCE per cycle (not
        # once per branch - it is an account-wide rate, so one real API
        # call covers every branch). Everything downstream - the sell
        # gate, the booked P&L, the spacing floor - prices against this
        # real number instead of the old hardcoded assumption.
        try:
            await refresh_real_fee_rate(session)
        except Exception as e:
            log.warning(f"[GRID] real fee-rate refresh failed (non-fatal): {type(e).__name__}: {e}")

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
    # The rotation sweep below ranks coins from CryptoBacktestRun. Refresh
    # that table HERE, first, because of a structural dead-end found on
    # 2026-09-25:
    #
    #   auto-rotate sweep   every 5 min, crypto-trading service
    #     needs             a coin with >= GRID_MIN_REQUIRED_ROI_PCT ROI
    #     reads             CryptoBacktestRun
    #                         ^ written by exactly ONE function:
    #   scheduled backtest  crypto_family_tree_bot._run_scheduled_backtest_
    #                       and_update_exclusions(), called only from that
    #                       module's run() coordinator - which main.py
    #                       starts only when CRYPTO_STRATEGY_MODE ==
    #                       "family_tree", on the WEB service.
    #
    # So the only system still placing orders depended, for the data
    # driving every coin decision it makes, on a RETIRED bot being started
    # on a DIFFERENT service by a variable that was wrong. When that
    # variable broke, the table froze, every ROI check failed against
    # stale or absent rows, and the spread plan reported "eligible coins:
    # NONE" while 35 coins sat ranked and $259 waited to deploy.
    #
    # A system must own the data it depends on. The grid now refreshes its
    # own ranking table on its own service, on its own schedule, and no
    # longer cares whether the tree runs at all.
    global _last_grid_backtest_refresh_at
    now0 = time.time()
    if now0 - _last_grid_backtest_refresh_at >= GRID_BACKTEST_REFRESH_SECONDS:
        _last_grid_backtest_refresh_at = now0
        try:
            import crypto_selection_backtest as selection
            log.info("[GRID] refreshing own coin ranking (CryptoBacktestRun)...")
            res = await selection.run_full_backtest()
            ranked = len((res or {}).get("ranked") or [])
            log.info(f"[GRID] coin ranking refreshed - {ranked} coins ranked")
        except Exception as e:
            # Non-fatal: a stale ranking is worse than a fresh one, but far
            # better than a stopped trading loop.
            log.warning(f"[GRID] coin ranking refresh failed (non-fatal): "
                        f"{type(e).__name__}: {e}")

    global _last_grid_auto_rotate_at
    now = time.time()
    if now - _last_grid_auto_rotate_at >= GRID_AUTO_ROTATE_INTERVAL_SECONDS:
        _last_grid_auto_rotate_at = now
        try:
            await run_grid_auto_rotate_sweep()
        except Exception as e:
            log.error(f"[GRID] auto-rotate sweep error: {e}")

    # Real, hourly self-tuning sweep - per the account owner's direct
    # request to make this bot "grow and be better than the hrs before
    # and learn from it's mistakes... every hr." See
    # SELF_TUNE_INTERVAL_SECONDS's own comment for the full real
    # reasoning and bounds. Same in-process throttle pattern as the
    # auto-rotate sweep right above.
    global _last_grid_self_tune_at
    now3 = time.time()
    if now3 - _last_grid_self_tune_at >= SELF_TUNE_INTERVAL_SECONDS:
        _last_grid_self_tune_at = now3
        try:
            await run_grid_self_tuning_sweep()
        except Exception as e:
            log.error(f"[GRID] self-tuning sweep error: {e}")

    # Shadow mode periodic status check - logs learning engine progress
    # every SHADOW_MODE_MONITOR_INTERVAL_SECONDS (default 10 minutes).
    # Non-invasive, graceful failure if shadow mode unavailable. Same
    # in-process throttle pattern as the sweeps above.
    global _last_shadow_monitor_at
    now_shadow = time.time()
    if now_shadow - _last_shadow_monitor_at >= SHADOW_MODE_MONITOR_INTERVAL_SECONDS:
        _last_shadow_monitor_at = now_shadow
        try:
            await check_shadow_mode_status()
        except Exception as e:
            log.debug(f"[GRID] shadow mode status check error (non-fatal): {e}")

    # Mean Reversion Bot Cycle - runs every MEAN_REVERSION_CYCLE_SECONDS
    # (default 5 minutes). Separate directional trades complementing the
    # grid bot's market-neutral strategy. Buys oversold coins (RSI < 30),
    # exits on mean reversion (RSI > 60) or profit targets/stops.
    # Non-invasive, graceful failure if mean reversion unavailable.
    global _last_mean_reversion_at
    now_mr = time.time()
    if now_mr - _last_mean_reversion_at >= MEAN_REVERSION_CYCLE_SECONDS:
        _last_mean_reversion_at = now_mr
        try:
            async with get_session_factory()() as db:
                mr_engine = mean_reversion_engine.get_mean_reversion_engine()
                await mr_engine.run_cycle(db)
        except Exception as e:
            log.error(f"[MR] Cycle error (non-fatal): {type(e).__name__}: {e}")


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
    # The REAL fee rate this account actually pays - the dashboard's
    # "if sold right now" figures must be priced against the same real
    # number a genuine sale would book, never the old optimistic guess.
    status_fee_rate = await get_effective_round_trip_fee_rate()
    # Only used to price per-slice once maker orders are live, where the two
    # legs of a round trip can genuinely cost different rates. Left None when
    # maker orders are off so every slice shares one flat rate, as before.
    status_exit_leg_rate = await expected_leg_fee_rate() if await is_maker_orders_active() else None
    _maker_only = await is_maker_only_active()

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
                net_usd = _grid_slice_net_pnl(s.qty, s.entry_price, current_price,
                                              _slice_rate(s, status_fee_rate, status_exit_leg_rate))
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
            # Real, hourly self-tuned spacing multiplier - None means this
            # branch is still on the real validated global default
            # (AVG_SWING_SPACING_MULTIPLIER); a real value means its own
            # recent real trade history has genuinely nudged it wider (a
            # rough stretch) or eased it back toward default (a strong
            # one). See _maybe_self_tune_branch_spacing.
            "self_tuned_multiplier": round(b.self_tuned_multiplier, 2) if b.self_tuned_multiplier is not None else None,
            "effective_spacing_multiplier": round(b.self_tuned_multiplier, 2) if b.self_tuned_multiplier is not None else AVG_SWING_SPACING_MULTIPLIER,
        })

    # Real grand total across EVERY branch's own "if sold right now" figure
    # - per the account owner's own explicit request for one bottom-line
    # number covering the whole Grid Bot section, so a single glance (or a
    # single "close everything" button) doesn't require mentally summing
    # every branch card by hand. Only ever a real number when every single
    # branch that currently HOLDS a slice also has a real live price this
    # call - one missing price makes the real total honestly unknown
    # (None) rather than silently undercounting it.
    branches_with_slices = [b for b in out if b["open_slices"] > 0]
    total_unrealized_known = all(b["total_unrealized_net_usd"] is not None for b in branches_with_slices)
    total_unrealized_net_usd = (
        round(sum(b["total_unrealized_net_usd"] for b in branches_with_slices), 2)
        if total_unrealized_known else None
    )

    return {
        "fleet_name": "Adaptive Capital Fleet",
        "mode_active": mode_active,
        # The ONE field that says whether the loop is running, as opposed to
        # running-but-gated or not running at all. Everything else on this
        # dashboard describes state the loop acts on; only this describes the
        # loop. Added after a full day was lost inferring liveness from a
        # paused branch's stale spacing - see _record_grid_heartbeat.
        "heartbeat": await get_grid_heartbeat(),
        # Surfaced because a heartbeat alone cannot distinguish "working" from
        # "alive but locked out of working" - see read_grid_lease_state.
        "loop_lease": await read_grid_lease_state(),
        "dynamic_spacing_active": await is_dynamic_spacing_active(),
        "avg_swing_spacing_active": await is_avg_swing_spacing_active(),
        "grid_spacing_override": await get_live_grid_spacing_override(),
        "grid_spacing_override_candidates": GRID_LEVEL_SPACING_CANDIDATES,
        "auto_rotate_active": await is_grid_auto_rotate_active(),
        "auto_rotate_interval_minutes": GRID_AUTO_ROTATE_INTERVAL_SECONDS // 60,
        "adaptive_fleet": await get_adaptive_fleet_status(),
        "drawdown_breaker_pct": GRID_DRAWDOWN_BREAKER_PCT,
        "branch_count": len(branches),
        "branches_with_open_slices": len(branches_with_slices),
        "total_allocated_usd": round(total_allocated, 2),
        "total_unrealized_net_usd": total_unrealized_net_usd,
        "real_free_cash_usd": await get_real_free_cash_usd(),
        "min_required_roi_pct": MIN_REQUIRED_ROI_PCT,
        # The REAL round-trip fee every P&L figure above is priced against,
        # plus whether it was genuinely observed from Coinbase or is still
        # the conservative fallback, and the minimum spacing that can
        # actually clear it. Surfaced so an understated fee assumption can
        # never silently eat the profit again.
        "real_round_trip_fee_rate": status_fee_rate,
        # What the NEXT real round trip is actually expected to cost. With
        # maker orders on this is roughly half the market rate above, and it
        # is the rate fee_safe_min_grid_pct is derived from - reporting only
        # the market rate beside a maker-derived floor made the dashboard
        # contradict itself ("floored at 0.70%, the smallest move that can
        # clear that fee" printed next to a 1.00% fee).
        "effective_round_trip_fee_rate": (await expected_leg_fee_rate()) * 2,
        "real_fee_rate_observed": _cached_real_round_trip_fee_rate is not None,
        "fee_safe_min_grid_pct": await fee_safe_floor_pct(),
        # Measured, not assumed: how the legs REALLY filled. The floor
        # above prices the taker round trip; this is the evidence that
        # would justify relaxing it.
        "fill_mix": await get_fill_mix(),
        # Maker (post-only limit) orders: roughly half the fee of the market
        # orders this bot has always used. Off until turned on deliberately.
        "maker_orders_active": await is_maker_orders_active(),
        "real_maker_fee_rate": _cached_real_maker_fee_rate,
        "maker_order_wait_seconds": await maker_wait_seconds(),
        # Maker-ONLY: the market fallback removed entirely. This is the one
        # thing that legitimately lets the spacing floor above come down off
        # the taker leg, because it is the only thing that makes the taker
        # leg unreachable rather than merely unlikely.
        "maker_only_active": _maker_only,
        # Which switch is actually deciding it. A mode that is on for a
        # reason the page cannot name is a mode nobody can turn off again.
        "maker_only_source": ("environment " + MAKER_ONLY_ENV_VAR
                              if maker_only_env_override() is not None else "database toggle"),
        "maker_only_skipped_cycles": await get_maker_only_skips(),
        "floor_priced_against": ("maker (the market fallback is removed)"
                                 if _maker_only and _cached_real_maker_fee_rate is not None
                                 else "taker (an unfilled maker order still becomes a market order)"),
        "branches": out,
    }


async def close_all_grid_slices() -> dict:
    """Real, one-way "close everything" - per the account owner's direct
    request for one button at the bottom of the Grid Bot section that
    takes all the real profit if the whole section is up, instead of
    closing branches one at a time. Sells EVERY real open slice, across
    EVERY grid branch (active or paused, locked or not - locking only
    ever blocked real cash REMOVAL, never normal trading, so it doesn't
    protect a slice from this either), right now, at market.

    Reuses the exact same real fee/pnl formula (_grid_slice_net_pnl) and
    the exact same real bookkeeping (allocated_usd += pnl, reference_price
    -> the real fill price, a real CryptoGridTradeHistory row per slice,
    a real activity-feed entry) run_grid_branch_cycle()'s own FIFO sell
    already uses - this is not a second, hand-written copy of that math.

    Placed as ONE real market sell per branch (the branch's total real
    qty across every one of its open slices), not one order per slice -
    fewer real orders for the identical real fee cost (fee is % of
    notional, not per-order), then the single real fill price is applied
    to each slice's own entry for accurate per-slice P&L bookkeeping and
    trade-history rows. A branch with zero open slices is skipped
    entirely - nothing to close, nothing logged. A real sell that doesn't
    fill for one branch never blocks or rolls back any other branch's own
    real close in the same call - each branch's outcome is independent
    and reported separately."""
    branches = await get_grid_branches()
    results = []
    total_realized_pnl = 0.0
    branches_closed = 0
    slices_closed = 0
    close_all_fee_rate = await get_effective_round_trip_fee_rate()
    # Close-all always uses a market sell (it must fill), so its exit leg is
    # always the real TAKER rate regardless of the maker-orders toggle.
    close_all_exit_leg_rate = close_all_fee_rate / 2 if await is_maker_orders_active() else None
    async with engine.aiohttp.ClientSession() as session:
        for b in branches:
            slices = await get_grid_slices(b.bot_name)
            if not slices:
                continue
            total_qty = sum(s.qty for s in slices)
            fill = await engine.place_market_sell(session, total_qty, b.product_id)
            if not fill:
                reason = engine._last_order_error.get(b.product_id, "real sell did not fill")
                results.append({
                    "bot_name": b.bot_name, "product_id": b.product_id,
                    "sold": False, "slices_closed": 0, "reason": reason,
                })
                log.warning(f"[GRID] close-all: {b.bot_name} real sell of {b.product_id} did not fill ({reason}) - left open, will still trade normally")
                continue
            filled_qty, filled_price = fill
            branch_pnl = 0.0
            async with get_session_factory()() as db:
                for s in slices:
                    # Deliberately a MARKET sell above: "close everything" must
                    # actually fill, so this leg genuinely pays the taker rate
                    # even when maker orders are on. Priced accordingly.
                    pnl = _grid_slice_net_pnl(s.qty, s.entry_price, filled_price,
                                              _slice_rate(s, close_all_fee_rate, close_all_exit_leg_rate))
                    branch_pnl += pnl
                    await _log_grid_trade(b.bot_name, b.product_id, s.entry_price, filled_price, s.qty, pnl, s.opened_at)
                    slice_result = await db.execute(select(CryptoGridSlice).where(CryptoGridSlice.id == s.id))
                    slice_row = slice_result.scalar_one_or_none()
                    if slice_row:
                        await db.delete(slice_row)
                branch_result = await db.execute(select(CryptoGridBranch).where(CryptoGridBranch.bot_name == b.bot_name))
                fresh = branch_result.scalar_one_or_none()
                new_balance = b.allocated_usd + branch_pnl
                if fresh:
                    fresh.allocated_usd += branch_pnl
                    fresh.reference_price = filled_price
                    new_balance = fresh.allocated_usd
                await db.commit()
            total_realized_pnl += branch_pnl
            branches_closed += 1
            slices_closed += len(slices)
            msg = (
                f"🔒 {b.bot_name} CLOSE ALL: sold all {len(slices)} real open slice(s) of {b.product_id} @ ${filled_price:,.2f} "
                f"| P&L: {'+' if branch_pnl >= 0 else ''}${branch_pnl:.2f} after est. fees | branch now ${new_balance:.2f}"
            )
            log.info(f"[GRID] {msg}")
            await _log_activity_safe(b.bot_name, b.product_id, "SELL", msg)
            results.append({
                "bot_name": b.bot_name, "product_id": b.product_id, "sold": True,
                "slices_closed": len(slices), "fill_price": filled_price, "pnl": round(branch_pnl, 2),
            })
    return {
        "branches_closed": branches_closed,
        "slices_closed": slices_closed,
        "total_realized_pnl": round(total_realized_pnl, 2),
        "results": results,
    }


async def get_grid_trade_history(limit_recent: int = 50) -> dict:
    """Real, per-branch trade-history aggregation - the direct grid-side
    counterpart to crypto_family_tree_bot.get_coin_trade_history() /
    prop_bot.get_alpaca_branch_trade_history(). Read-only."""
    async with get_session_factory()() as db:
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
        total_trade_count = 0
        total_realized_pnl = 0.0
        total_wins = 0
        for bot_name, trade_count, total_pnl, avg_pnl, wins in agg_result.all():
            branch_pnl = round(total_pnl, 2) if total_pnl is not None else 0.0
            branches.append({
                "bot_name": bot_name,
                "trade_count": trade_count,
                "total_pnl": branch_pnl,
                "avg_pnl": round(avg_pnl, 2) if avg_pnl is not None else 0.0,
                "win_rate": round(wins / trade_count * 100, 1) if trade_count else 0.0,
            })
            # Real, exact totals accumulated from the same raw query results
            # above (wins as an exact int, total_pnl as its real unrounded
            # value) - never reconstructed from the already-rounded
            # per-branch win_rate/total_pnl, which would compound rounding
            # error across many branches.
            total_trade_count += trade_count
            total_realized_pnl += total_pnl if total_pnl is not None else 0.0
            total_wins += wins or 0
        branches.sort(key=lambda b: b["total_pnl"], reverse=True)

        # Grouped by BRANCH *and* COIN, because a branch outlives the coin it
        # is pointed at. crypto_grid_1 earned its whole +$12.80 over 15 DOGE
        # round trips and was later repointed at BTC-USD; the branch-level
        # figure above is correct as a branch lifetime, but printed next to
        # the branch's CURRENT coin it reads as "BTC made $12.80", which is
        # false. The UI needs both numbers to tell them apart, so it gets
        # both rather than a relabelled guess.
        branch_coin_result = await db.execute(
            select(
                CryptoGridTradeHistory.bot_name,
                CryptoGridTradeHistory.product_id,
                func.count(CryptoGridTradeHistory.id).label("trade_count"),
                func.sum(CryptoGridTradeHistory.pnl).label("total_pnl"),
                func.sum(case((CryptoGridTradeHistory.pnl > 0, 1), else_=0)).label("wins"),
            ).group_by(CryptoGridTradeHistory.bot_name, CryptoGridTradeHistory.product_id)
        )
        branch_coins = []
        for bot_name, product_id, trade_count, total_pnl, wins in branch_coin_result.all():
            branch_coins.append({
                "bot_name": bot_name,
                "product_id": product_id,
                "trade_count": trade_count,
                "total_pnl": round(total_pnl, 2) if total_pnl is not None else 0.0,
                "win_rate": round(wins / trade_count * 100, 1) if trade_count else 0.0,
            })
        branch_coins.sort(key=lambda r: -r["total_pnl"])

        coin_result = await db.execute(
            select(
                CryptoGridTradeHistory.product_id,
                func.count(CryptoGridTradeHistory.id).label("trade_count"),
                func.sum(CryptoGridTradeHistory.pnl).label("total_pnl"),
                func.avg(CryptoGridTradeHistory.pnl).label("avg_pnl"),
                func.sum(case((CryptoGridTradeHistory.pnl > 0, 1), else_=0)).label("wins"),
            ).group_by(CryptoGridTradeHistory.product_id)
        )
        coins = []
        for product_id, trade_count, total_pnl, avg_pnl, wins in coin_result.all():
            coins.append({
                "product_id": product_id,
                "trade_count": trade_count,
                "total_pnl": round(total_pnl, 2) if total_pnl is not None else 0.0,
                "avg_pnl": round(avg_pnl, 2) if avg_pnl is not None else 0.0,
                "wins": wins or 0,
                "win_rate": round(wins / trade_count * 100, 1) if trade_count else 0.0,
            })
        coins.sort(key=lambda coin: coin["total_pnl"], reverse=True)

        recent_result = await db.execute(
            select(CryptoGridTradeHistory).order_by(desc(CryptoGridTradeHistory.closed_at)).limit(limit_recent)
        )
        recent_trades = [row.to_dict() for row in recent_result.scalars().all()]

    # Real, all-time grand total across EVERY branch this bot has ever
    # traded - per the account owner's direct follow-up after seeing the
    # existing "if sold right now" (unrealized-only) total: "what about the
    # real life total of profit that's been taken." Deliberately covers
    # every branch that has EVER closed a real trade, including one since
    # paused/withdrawn/deleted, since a completed CryptoGridTradeHistory row
    # is real, permanent profit/loss regardless of whether the branch that
    # earned it still exists today.
    overall_win_rate = round(total_wins / total_trade_count * 100, 1) if total_trade_count else 0.0

    return {
        "branches": branches,
        "branch_coins": branch_coins,
        "coins": coins,
        "recent_trades": recent_trades,
        "total_trade_count": total_trade_count,
        "total_realized_pnl": round(total_realized_pnl, 2),
        "overall_win_rate": overall_win_rate,
    }


async def scale_grid_bot_capital(scale_factor: float = 1.25, dry_run: bool = False) -> dict:
    """Scale up grid bot allocations by multiplying existing allocated_usd
    by scale_factor (default 1.25 = 25% increase). Real capital scaling per
    the account owner's explicit "scale grid bot by increasing capital
    allocation" request.

    SAFETY:
    - Does NOT disable any branches (active flag never touched)
    - Does NOT modify grid bot's logic or parameters
    - Grid bot continues trading while scaling occurs
    - Updates are atomic (commit or full rollback)
    - dry_run=True shows what would change without applying

    Returns dict with status and updated allocations."""
    if scale_factor <= 1.0:
        return {"error": "scale_factor must be > 1.0", "status": "failed"}

    async with get_session_factory()() as db:
        # Query only active branches - never modify disabled ones
        result = await db.execute(select(CryptoGridBranch).where(CryptoGridBranch.active == True))
        branches = result.scalars().all()

        if not branches:
            return {"error": "No active grid branches to scale", "status": "failed"}

        updates = []
        total_old_allocation = 0.0
        total_new_allocation = 0.0

        for branch in branches:
            # SAFETY: Verify branch is STILL active and allocated_usd is a number
            if not branch.active:
                continue  # Skip any disabled branches

            old_usd = branch.allocated_usd or 0.0
            if old_usd <= 0:
                continue  # Skip branches with no allocation

            new_usd = round(old_usd * scale_factor, 2)
            total_old_allocation += old_usd
            total_new_allocation += new_usd

            # Build update record
            update_rec = {
                "bot_name": branch.bot_name,
                "product_id": branch.product_id,
                "active": branch.active,  # Verify it's still active
                "old_allocated_usd": round(old_usd, 2),
                "new_allocated_usd": new_usd,
                "increase_pct": round((new_usd - old_usd) / max(old_usd, 1) * 100, 1)
            }
            updates.append(update_rec)

            # SAFETY: Only modify allocated_usd, nothing else
            if not dry_run:
                branch.allocated_usd = new_usd

        if not updates:
            return {"error": "No valid branches to scale (all inactive or zero allocation)", "status": "failed"}

        # DRY RUN: Show what would happen without applying
        if dry_run:
            return {
                "status": "dry_run_success",
                "scale_factor": scale_factor,
                "branches_to_scale": len(updates),
                "total_old_allocation": round(total_old_allocation, 2),
                "total_new_allocation": round(total_new_allocation, 2),
                "total_increase_preview": round(total_new_allocation - total_old_allocation, 2),
                "preview_updates": updates,
                "message": "DRY RUN: No changes applied. Re-run with dry_run=False to execute."
            }

        # APPLY CHANGES: Atomic commit or full rollback
        try:
            await db.commit()
            msg = (
                f"✅ Grid Bot Capital Scaling Complete: "
                f"{len(updates)} branches scaled by {(scale_factor-1)*100:.0f}% | "
                f"Old total: ${total_old_allocation:,.2f} → New total: ${total_new_allocation:,.2f} | "
                f"GRID BOT REMAINS ACTIVE - no branches disabled"
            )
            log.warning(msg)
            await _log_activity_safe("grid_bot_system", "SCALING", "SCALE", msg)

            return {
                "status": "success",
                "scale_factor": scale_factor,
                "branches_scaled": len(updates),
                "total_old_allocation": round(total_old_allocation, 2),
                "total_new_allocation": round(total_new_allocation, 2),
                "total_increase": round(total_new_allocation - total_old_allocation, 2),
                "updates": updates,
                "safety_notes": [
                    "✅ Grid bot remains ACTIVE",
                    "✅ No branches were disabled",
                    "✅ Only allocated_usd field was modified",
                    "✅ Grid bot picks up new allocations on next cycle (~30 sec)",
                    "✅ All changes logged to activity feed"
                ]
            }
        except Exception as e:
            await db.rollback()
            log.error(f"[GRID] Capital scaling failed: {e}")
            return {"error": str(e), "status": "failed"}


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
    # CRITICAL FIX: Reuse existing event loop if one is already set (e.g., by bot_runner.py).
    # This prevents "Semaphore is bound to a different event loop" errors.
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
    except RuntimeError:
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

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
import json
import logging
import os
import zlib
from decimal import Decimal as _Decimal, InvalidOperation
import slice_lifecycle as _sl  # NEVER rebind this name in a function.
# A `for _sl in slices:` anywhere inside run_grid_branch_cycle makes _sl a
# LOCAL for that whole function, shadowing this module for every line in it -
# including the ones ABOVE the loop. That is not a lint nit: on 2026-09-29 at
# 15:31:03Z a real BTC-USD buy filled, the slice insert raised
# "UnboundLocalError: cannot access local variable '_sl'", the outer handler
# swallowed it, and the coin was bought with no slice row written. Two loops
# did it, and the damage ran in both directions - the insert above them raised,
# and the PARTIAL write below them would have read a slice object as a module.
import slice_target
import random
import sys
import time
from datetime import datetime, timedelta

from sqlalchemy import select, func, case, desc

import crypto_btc_compound_bot as engine
import crypto_mean_reversion_bot as mean_reversion_engine
import coin_rotation as rotation
import opportunity_signals as signals
import horizon_study
from database import get_session_factory
from models import CryptoGridBranch, CryptoGridSlice, CryptoGridTradeHistory, GridMakerExpiry, GridOrderNotPlaced, RegimeCrossing, TradingBotState, CryptoTreeBranch, BotPosition

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

# ---- THE LEARNED MULTIPLIER REACHES A FIXED STEP, ONE WAY ONLY -------------
# _maybe_self_tune_branch_spacing has been running hourly and writing a real,
# per-branch self_tuned_multiplier out of that branch's own last five REAL
# closed trades. Its only consumer sits behind `elif is_avg_swing_spacing_active()`
# in the spacing chain - and a promoted candidate takes the `if` above it and
# returns. With grid_spacing_override set to 3_levels_2.5pct, as it is live,
# that elif is unreachable: the fleet has been learning into a void.
#
# Reconnecting it wholesale is not the answer. Full avg-swing spacing sizes a
# step at multiplier x the coin's own swing with only a fee-safe floor under
# it (~0.9%), so on a calm coin it can land far BELOW today's ~3.00% step.
# Narrowing a step is the one thing the account owner has ruled out, in those
# words, and the measured record backs him: the retired 2.00%-step era lost 19
# of 82 trades where the current wider one has lost 3 of 114.
#
# So the learned multiplier is admitted as a ONE-SIDED FLOOR. Two guards make
# that structural rather than a matter of care:
#   1. it is consulted only when the branch has been widened ABOVE the
#      validated 1.5x default - which _maybe_self_tune_branch_spacing does
#      only after a genuinely poor real win rate (<40% over 5 closes);
#   2. it is applied through max(), so the step can only ever move wider.
# A branch on a good run eases its multiplier back toward 1.5x and this does
# nothing at all - the fixed step simply stands.
#
# This is the ONDO/TON failure mode answered with evidence rather than a new
# gate. Those three losses were coins that fell 8-15% into a step that did not
# respect how far they move; a branch that keeps stopping out now widens its
# own step automatically, from its own record, without anyone watching.
#
# OFF BY DEFAULT. Nothing changes until the account owner arms it, because it
# changes real spacing on real branches.
SELF_TUNE_WIDENS_FIXED_SPACING = os.getenv(
    "GRID_SELF_TUNE_WIDENS_FIXED_SPACING", "false").strip().lower() in ("1", "true", "yes", "on")


def self_tune_widening_multiplier(branch) -> float:
    """The branch's learned multiplier when it has EARNED a widening, else
    None. Never returns a value at or below the validated default, so a
    caller cannot use this to narrow anything."""
    m = getattr(branch, "self_tuned_multiplier", None)
    if m is None:
        return None
    try:
        m = float(m)
    except (TypeError, ValueError):
        return None
    return m if m > AVG_SWING_SPACING_MULTIPLIER else None
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

# FLOATING BASE. reference_price already floats on every real fill - buy
# and sell both write it (see run_grid_branch_cycle). What it cannot do is
# float when there IS no fill, and that is the case that strands capital:
# a flat branch whose coin rallied away keeps measuring its next buy from
# a level the market left, so it waits for a dip deeper than its own step.
# Measured on the live fleet 2026-09-26: ONDO needed 5.46% against a 2.50%
# step, TIA 3.81%. Together 4.27 percentage points of pure waiting.
#
# reanchor_flat_grid_branches_now() has existed since 2026-09-25 and fixes
# exactly this, but only when a human presses it - so the drift simply
# rebuilt. Running it on a schedule is the whole change.
#
# Safe by construction, not by care: it places no order, spends nothing,
# sells nothing, writes only reference_price, moves it only UPWARD, and
# skips any branch holding an open slice (where the reference is also the
# sell trigger). See its own docstring.
GRID_REANCHOR_INTERVAL_SECONDS = int(os.getenv("GRID_REANCHOR_INTERVAL_SECONDS", str(60 * 60)))
_last_grid_reanchor_at = 0.0


def auto_reanchor_enabled() -> bool:
    """ON unless switched off. The alternative is what the fleet already
    did: drift back into waiting for a dip that already happened."""
    return (os.getenv("GRID_AUTO_REANCHOR", "true") or "true").strip().lower() not in (
        "false", "0", "no", "off")
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


async def unfunded_deployment_reserve() -> tuple:
    """Cash held back from dip buys for target coins with no branch yet.

    The owner authorised giving the deployer first claim so new coins are
    not outrun by a loop that checks every 30 seconds while the deployer
    checks every 900 - and asked in the same breath that the money keep
    flipping. coin_deploy.deployment_reserve_usd() is where those two are
    reconciled: the deployer gets up to HALF the deployable cash, never all
    of it, so branches that are already trading keep buying their dips.

    Returns (usd, why). 0.0 whenever every target already has a branch,
    which makes this safe to leave on: the sizing path below is then
    byte-for-byte what it was before.

    One indexed query, and only on the cycle where a dip has actually
    triggered - never on every pass.
    """
    try:
        import coin_deploy as _cd
        import coin_deploy_worker as _cdw
        targets = list(_cdw.TARGET_COINS)
        if not targets:
            return 0.0, "no deployment targets"
        async with get_session_factory()() as db:
            held = {r[0] for r in (await db.execute(
                select(CryptoGridBranch.product_id)
                .where(CryptoGridBranch.product_id.in_(targets)))).all()}
        unfunded = [c for c in targets if c not in held]
        if not unfunded:
            return 0.0, "every target coin already has a branch"
        free_cash = await get_real_free_cash_usd()
        if free_cash is None:
            return 0.0, "free cash unreadable - nothing reserved"
        deployable = float(free_cash) - max(0.0, GRID_CASH_RESERVE_USD)
        per_coin = deployable / max(1, len(unfunded))
        per_coin = max(per_coin, _cd.MIN_VIABLE_BRANCH_USD)
        return _cd.deployment_reserve_usd(len(unfunded), per_coin, deployable)
    except Exception as e:
        # Fail OPEN here, not closed: an unreadable reserve must not stop the
        # fleet trading. The cost of getting this wrong is a new coin funded
        # a cycle later, not a loss.
        log.info(f"[GRID] deployment reserve unreadable ({type(e).__name__}) - "
                 f"reserving nothing this cycle")
        return 0.0, "reserve could not be computed - nothing held back"


def spendable_for_slice(slice_usd, real_balance, reserve=None, min_trade=None,
                        deployment_reserve=0.0):
    """How much of the wallet a single grid slice may actually spend.

    Returns (spend, reason). `spend` is 0.0 when the buy must not happen,
    and `reason` says which limit bound it, so the caller can log a number
    rather than a shrug.

    THE BUG THIS EXISTS FOR, 2026-09-26. Every path that PLANS a spend
    holds back GRID_CASH_RESERVE_USD - redistribute_grid_cash (line ~2684),
    get_grid_spend_ceiling_usd, _auto_deploy_idle_free_cash. The path that
    actually BUYS did not: it sized a slice as
    `min(slice_usd, real_balance)` straight against the whole wallet. So
    the reserve was real in every projection and decorative at the only
    moment it mattered.

    Live account when this was found: wallet $79.36 against an $88.00
    reserve, with a branch holding a $69.23 allocation. The old sizing
    would happily spend $69.23 of it and leave $10.13 - and
    redistribute_grid_cash's own docstring already spells out why that is
    the failure case: "Fees settle out of the USD balance, and an account
    with nothing spare cannot pay one - a rejected fee is a stuck
    position."

    The reserve is subtracted, never the slice shrunk below the real
    minimum: a sub-minimum spend is refused outright rather than sent as a
    dust order that pays a full fee for a position too small to exit.
    """
    if reserve is None:
        reserve = GRID_CASH_RESERVE_USD
    if min_trade is None:
        min_trade = MIN_TRADE_USD
    if real_balance is None:
        return 0.0, "real balance unavailable"
    held_for_new_coins = max(0.0, float(deployment_reserve or 0.0))
    deployable = real_balance - max(0.0, reserve) - held_for_new_coins
    if deployable <= 0:
        if held_for_new_coins > 0:
            return 0.0, (f"wallet ${real_balance:,.2f} less the ${reserve:,.2f} fee "
                         f"reserve and ${held_for_new_coins:,.2f} held for coins not "
                         f"yet funded leaves nothing - the new coin gets this one")
        return 0.0, (f"wallet ${real_balance:,.2f} is at or below the "
                     f"${reserve:,.2f} fee reserve - nothing is deployable")
    spend = min(slice_usd, deployable)
    if spend < min_trade:
        # Name EVERY subtraction. Reporting only the fee reserve when cash is
        # also being held for an unfunded coin sends the reader looking for a
        # shortfall that is really a deliberate reservation.
        _less = f"${reserve:,.2f} fee reserve"
        if held_for_new_coins > 0:
            _less += f" and ${held_for_new_coins:,.2f} held for coins not yet funded"
        return 0.0, (f"${spend:,.2f} spendable (wallet ${real_balance:,.2f} less the "
                     f"{_less}) is below the ${min_trade:,.2f} minimum")
    if slice_usd <= deployable:
        bound = "branch allocation"
    elif held_for_new_coins > 0:
        bound = (f"wallet less the fee reserve and ${held_for_new_coins:,.2f} held for "
                 f"coins not yet funded")
    else:
        bound = "wallet less the fee reserve"
    return round(spend, 2), f"${spend:,.2f}, bounded by {bound}"


# A slice too small to sell is not a rung.
#
# MEASURED 2026-10-04. BCH-USD held 3 slices against 3 levels and so read
# PARKED - unable to buy, and its only escape route a sell the venue would
# refuse. Its third "slice" was 0.00000022 BCH, worth $0.00007: the dust
# remainder of a partial sell whose $0.58 of profit was already booked on
# 2026-10-03 at 15:57. LINK-USD was locked the same way by 0.01 LINK ($0.14)
# and had not closed a trade since 2026-09-29, four and a half days, after
# averaging a close every 3.5 hours before that. $308.33 of capital sat in
# two branches held shut by fourteen cents.
#
# The rung count asks "is this branch full", and a position the venue will
# not let you sell cannot be what fills it. _pick_parked_slice_to_sell's own
# comment predicted exactly this failure - "a branch whose only qualifying
# slice is dust reports itself escapable while staying locked" - and the
# notional was written into the feed so it would be visible. It was.
#
# THE FLOOR IS DELIBERATELY LOW. Erring low only preserves today's
# behaviour: a slice above it keeps blocking a rung exactly as now. Erring
# HIGH would let a branch treat a real small position as absent and buy a
# rung it should not have. $1.00 sits under any plausible venue minimum for
# a USD pair while being far above the $0.14 and $0.00007 actually seen, and
# well below MIN_TRADE_USD ($5.00), the smallest rung the engine will buy -
# so no slice this engine created can be mistaken for dust.
GRID_DUST_SLICE_USD = float(os.getenv("GRID_DUST_SLICE_USD", "1.00"))


def _slice_field(s, name):
    """One slice field, from an ORM row or a dict, without caring which."""
    if hasattr(s, "get"):
        return s.get(name)
    return getattr(s, name, None)


def slice_qty(s) -> float:
    """Units of coin this slice's books claim."""
    try:
        return float(_slice_field(s, "qty") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def slice_basis_usd(s) -> float:
    """What a slice really cost: qty x entry. Works on an ORM row or a dict."""
    try:
        entry = float(_slice_field(s, "entry_price") or 0.0)
    except (TypeError, ValueError):
        entry = 0.0
    return slice_qty(s) * entry


def slice_is_tradeable(s, min_usd: float = None) -> bool:
    """False for a remnant too small for the venue to accept a sell on."""
    floor = GRID_DUST_SLICE_USD if min_usd is None else min_usd
    return slice_basis_usd(s) >= floor


def tradeable_slices(slices) -> list:
    """The slices that could actually be sold - the ones that fill a rung."""
    return [s for s in (slices or []) if slice_is_tradeable(s)]


def branch_is_adopted_only(slices) -> bool:
    """True when EVERY open slice on this branch was adopted.

    Such a branch holds coin, but it never spent cash to get it - the
    adoption worker wrote its rows for coin the account already owned. A
    branch that has since bought a real slice is mixed and takes the
    ordinary path; requiring "every" keeps the special case to a state
    that can be checked rather than estimated.
    """
    return bool(slices) and all(slice_paid_no_entry_fee(s) for s in slices)


def adopted_rung_usd(deployable_usd, contenders, min_trade=None):
    """What ONE dip buy on an adopted branch may spend. (usd, reason).

    WHY THIS IS NOT allocated_usd / num_levels.

    An adopted branch's allocated_usd is the market value of coin the
    account ALREADY OWNED - not a cash budget the branch brought to the
    grid. The ordinary sizing reads it as a budget, and on the live fleet
    that produces figures with no relationship to the money available.
    Measured 2026-09-27, wallet $1,194.54 less the $88.00 fee reserve =
    $1,106.54 deployable, against eleven parked branches:

        ZEC   $757.54   68.5% of the entire deployable wallet
        XRP   $746.85   67.5%
        ...
        total $2,135.34 wanted against $1,106.54 available - 1.9x over

    ZEC and XRP are the two branches sitting furthest underwater. Under
    that sizing the first one to dip takes two thirds of the wallet and
    doubles down on the worst position; the second cannot be served at
    all, and the other nine starve. That is what "unpark them" would have
    meant if the level count alone had been raised.

    So an adopted rung is sized from CASH, split evenly between the
    branches that could use one - the same equal-weight rule idle_cash.py
    already deploys unclaimed cash under. Every branch can act, and none
    can take the wallet.

    contenders is counted from the stop-override marker, which also
    catches adopted branches that have since bought a real slice and no
    longer qualify. That overcounts, which makes each rung SMALLER - the
    safe direction, and the reason an approximate count is acceptable
    here where an approximate spend would not be.
    """
    if min_trade is None:
        min_trade = MIN_TRADE_USD
    if deployable_usd is None:
        return 0.0, "deployable cash unavailable"
    n = max(1, int(contenders or 1))
    if deployable_usd <= 0:
        return 0.0, (f"${deployable_usd:,.2f} deployable - the wallet is at or below "
                     f"the fee reserve, so no rung can be funded")
    # Floored to the cent, never rounded. Rounding UP breaks the one
    # property this rule exists for: at 20 contenders a rounded share puts
    # the sum $0.06 over the wallet, so the last branch to dip finds less
    # than its share. Caught by the "every contender can be served" test,
    # which is the invariant, not the arithmetic detail.
    share = int((deployable_usd / n) * 100) / 100   # floor; deployable is > 0 here
    if share < min_trade:
        return 0.0, (f"${share:,.2f} is this branch's even share of ${deployable_usd:,.2f} "
                     f"across {n} adopted branch(es), below the ${min_trade:,.2f} minimum "
                     f"trade - a dust order pays a full fee for a position too small to exit")
    return round(share, 2), (f"${share:,.2f} - an even share of ${deployable_usd:,.2f} "
                             f"deployable across {n} adopted branch(es)")


async def _adopted_branch_count() -> int:
    """Active branches carrying the adoption marker. One indexed query.

    stop_loss_pct_override is written by coin_adoption_worker and by
    nothing else, which is what makes it usable as the marker here (the
    same property _adopted_so_far relies on).
    """
    async with get_session_factory()() as db:
        rows = (await db.execute(
            select(CryptoGridBranch.bot_name)
            .where(CryptoGridBranch.stop_loss_pct_override.isnot(None))
            .where(CryptoGridBranch.active.is_(True)))).all()
    return len(rows)

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
# When maker-only was last observed ON. Without it the maker_only invariant
# could only report a taker COUNT over a 250-fill window and shrug: a window
# reaching back past the day the mode was armed cannot tell a stale taker
# fill from the fallback firing right now. Stored as an ISO string in
# TradingBotState.notes; cleared when the mode goes off, so it always means
# "the start of the CURRENT run of maker-only", never the first time ever.
MAKER_ONLY_ARMED_AT_STATE_KEY = "grid_maker_only_armed_at"

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
                    db.add(TradingBotState(bot_name=REAL_FEE_RATE_STATE_KEY,
                                           base_capital=real_rate,
                                           starting_capital=_cached_real_maker_fee_rate))
                else:
                    row.base_capital = real_rate
                    # THE MAKER LEG MUST PERSIST TOO. It used to live only in
                    # this process's memory while the taker round trip was
                    # saved and reloaded - an asymmetry with real consequences,
                    # because worst_case_leg_fee_rate() returns the TAKER leg
                    # the moment the maker rate is None:
                    #
                    #   floor with a measured maker leg   0.90%
                    #   floor without it                  1.70%
                    #
                    # so a restart, or any worker that had not itself fetched
                    # the fee tier, silently near-doubled the spacing floor.
                    # Observed live 2026-09-28: one worker computed the 0.90%
                    # floor while another reported maker_round_trip_fee_rate as
                    # 0.015 - the taker rate wearing the maker label - from the
                    # same deploy, same second.
                    #
                    # Written only when there is something to write: a failed
                    # tier fetch leaves the last good measurement in place
                    # rather than erasing it. A gap is not a zero.
                    if _cached_real_maker_fee_rate is not None:
                        row.starting_capital = float(_cached_real_maker_fee_rate)
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

# ---- LEVEL-CAP EXEMPTION ---------------------------------------------------
# A spacing-override candidate caps EVERY branch at its own level count, and
# _effective_num_levels applies that cap live each cycle - including to a
# branch that already holds MORE slices than the new ceiling. The buy check is
# `len(tradeable_slices(slices)) < branch.num_levels`, so such a branch can
# never open another rung until it sells back under the cap. It can only sell.
#
# Measured on the live fleet 2026-10-04 under the 3_levels_2.5pct override:
# six branches held 3-7 slices against a 3-level ceiling, and $5,844.04 -
# 68.6% of the fleet's $8,516.95 - could not buy at any price. The sixteen
# branches that could still buy were returning 2.97% against those six at
# 0.96%, so the frozen share was both the larger and the slower half.
#
# This names the coins that keep their own allocation-derived level count
# instead of being capped down by an override. EMPTY BY DEFAULT, and an empty
# list is byte-for-byte today's behaviour - nothing changes until the account
# owner names a coin, which is deliberate: unfreezing a branch lets it buy
# dips again, and that is a real spend of real money on a real position.
#
# IT IS A CEILING EXEMPTION, NOT A FLOOR OVERRIDE. The allocation-derived
# count (_safe_num_levels_for_allocation) still applies, so an exempt branch
# can never exceed what its own capital supports, and every other gate - the
# cash reserve, concentration, the backing check, the drawdown breaker - is
# untouched and still runs first.
#
# WHY THIS IS NOT "just set the override back to live_default": that lifts the
# cap on EVERY branch at once, including the one the code already warns about
# by name at the ADOPTED HEADROOM note below - "raising the level count
# without the sizing change is what would have let ZEC spend 68.5% of the
# wallet averaging down its own worst position." ZEC holds the fleet's single
# largest allocation ($2,271.29) and has completed zero trades in 196. Naming
# coins one at a time is what keeps that branch capped while the five earners
# beside it go back to work.
GRID_LEVEL_CAP_EXEMPT = frozenset(
    p.strip().upper() for p in os.getenv("GRID_LEVEL_CAP_EXEMPT", "").split(",")
    if p.strip()
)


def branch_is_level_cap_exempt(product_id) -> bool:
    """True when this coin keeps its allocation-derived level count instead of
    an override's lower ceiling. Unknown/blank product_id is never exempt:
    the safe answer to 'should this branch be allowed to buy more' is no."""
    if not product_id:
        return False
    return str(product_id).strip().upper() in GRID_LEVEL_CAP_EXEMPT

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


async def maker_only_armed_at():
    """When the CURRENT run of maker-only started, or None if it is off or
    was never recorded. Epoch seconds live in base_capital - the same
    pattern the measured maker rate already uses for starting_capital,
    since TradingBotState has no free-text column.

    None means UNKNOWN, never "just now" and never "long ago". A caller
    that cannot establish this must say it cannot, not pick a side.
    """
    try:
        async with get_session_factory()() as db:
            row = (await db.execute(select(TradingBotState).where(
                TradingBotState.bot_name == MAKER_ONLY_ARMED_AT_STATE_KEY))).scalar_one_or_none()
        if row is None or not row.base_capital:
            return None
        return datetime.utcfromtimestamp(float(row.base_capital))
    except Exception as exc:
        log.warning(f"[GRID] maker-only arming time unreadable: {exc}")
        return None


async def record_maker_only_state(active: bool):
    """Stamp the start of a run of maker-only, or clear it when it ends.

    Deliberately NOT refreshed while the mode stays on: the stored value is
    the START of this run, so a taker fill can be compared against it. A
    timestamp that moved every cycle would make every fill look older than
    the arming and the check would pass forever.

    Best-effort. A failure here loses a diagnostic, never a trade, so it is
    logged and swallowed rather than allowed to break a cycle.
    """
    try:
        async with get_session_factory()() as db:
            row = (await db.execute(select(TradingBotState).where(
                TradingBotState.bot_name == MAKER_ONLY_ARMED_AT_STATE_KEY))).scalar_one_or_none()
            if active:
                if row is None:
                    db.add(TradingBotState(bot_name=MAKER_ONLY_ARMED_AT_STATE_KEY,
                                           base_capital=datetime.utcnow().timestamp()))
                    await db.commit()
                    log.info("[GRID] maker-only armed - stamped the start of this run")
                elif not row.base_capital:
                    row.base_capital = datetime.utcnow().timestamp()
                    await db.commit()
            elif row is not None:
                await db.delete(row)
                await db.commit()
                log.info("[GRID] maker-only off - cleared the arming stamp")
    except Exception as exc:
        log.warning(f"[GRID] could not record maker-only state (non-fatal): {exc}")


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


async def expected_leg_fee_rate(product_id: str = None) -> float:
    """The REAL per-leg fee rate the next order is expected to pay: the
    maker rate when maker orders are on, the taker rate otherwise. Half the
    round-trip rate, since a round trip is two legs.

    PER COIN, because maker-only being ON does not mean a maker FILL.

    universe_scan measures every book against a $750,000 depth floor and
    reports five live branches below it - ACH $52,080, FLOKI $114,810,
    TIA $395,209, SHIB $471,080, BONK $744,989. An order on a book that
    thin crosses the spread and pays TAKER. This function returned the
    maker rate for all of them anyway, because it never looked at which
    coin was asking.

    That is not cosmetic. _pick_profitable_slice_to_sell uses this rate to
    decide whether a sale would net a profit, so understating the exit leg
    by 0.40 points green-lights sales netting between -0.40% and zero -
    real losses booked as wins, the exact failure that function exists to
    prevent.

    Passing product_id consults the measured verdict. It can only ever
    RAISE the rate: a coin known to be thin gets taker, a coin known to be
    deep keeps maker, and a coin nothing has been measured about behaves
    exactly as before. Calling with no product_id is unchanged.
    """
    # The durable measurement: this process's copy, else the persisted one.
    # A worker that never fetched the fee tier used to price every sell at
    # the TAKER leg here, which is the estimate _pick_profitable_slice_to_sell
    # uses - so the same slice looked sellable or not depending on which
    # worker asked.
    maker = await get_effective_maker_leg_fee_rate()
    if await is_maker_orders_active() and maker is not None:
        if product_id is None:
            return maker
        taker = (await get_effective_round_trip_fee_rate()) / 2
        try:
            import maker_viability
            return maker_viability.leg_fee_rate(product_id, maker, taker,
                                                maker_only_active=True)
        except Exception:
            # A failure to consult the verdict must never LOWER the fee, so
            # it falls back to exactly what this function returned before.
            return maker
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
    _measured_maker = await get_effective_maker_leg_fee_rate()
    maker_leg = (_measured_maker if _measured_maker is not None
                 else real_round_trip / 2)
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


async def grid_buy(session, usd_amount: float, product_id: str, bot_name: str = None,
                   outcome_out: dict = None):
    """Real grid BUY: maker first (cheap, may not fill), market fallback
    (always fills, costs more) - unless maker-ONLY mode has removed that
    fallback, in which case an unfilled maker order simply means no buy
    this cycle. Returns (filled_qty, price, leg_fee_rate) or None - the
    real rate actually paid comes back with the fill so the slice can
    record it and be priced honestly later.

    `outcome_out`, when given, is filled with why there was no fill -
    order_outcome.MAKER_EXPIRED for a maker order that really did rest for
    its whole window and nobody took (routine under maker-only),
    NO_ORDER_CREATED when the venue never received an order at all,
    NO_FILL when whether an order rested is UNKNOWN, or REJECTED for one
    the venue actually refused. The caller used to have to guess, and
    guessed "rejected" for all of them; then this function guessed
    "expired" for all of them, which hid the non-orders in the one cause
    that counts as routine. Same detail_out pattern as _net_edge_gate_ok."""
    if await is_maker_orders_active():
        fill = await engine.place_maker_buy(session, usd_amount, product_id,
                                            await maker_wait_seconds())
        if fill:
            qty, price = fill
            log.info(f"[GRID] {product_id}: real MAKER buy filled {qty:.8f} @ ${price:,.6f} (cheaper fee)")
            await _record_fill_leg(GRID_FILL_MIX_BUY_KEY, True)
            _leg = await get_effective_maker_leg_fee_rate()
            return qty, price, (_leg if _leg is not None
                                else (await get_effective_round_trip_fee_rate()) / 2)
        if await is_maker_only_active():
            # No fallback, by design. A buy that does not happen costs
            # nothing and the dip will still be there next cycle; a taker
            # buy costs 0.75% and would make the spacing floor a lie.
            #
            # ONE read of the engine's verdict, used by BOTH the ledger row
            # and the outcome reported to the caller. Read twice they could
            # disagree, and "the expiry table says it rested, the event feed
            # says no order existed" is a contradiction no reader can
            # resolve. Three states: True rested, False never placed, None
            # UNKNOWN - and None stays None all the way down.
            _rested = engine._last_order_rested.get(product_id)
            _why = engine._last_order_error.get(product_id)
            if not _why:
                # The old default asserted a resting order unconditionally.
                # It is only true when one actually rested; on the UNKNOWN
                # path _last_order_error was cleared and never set, so that
                # default was writing a claim the code did not have.
                _why = ("the order rested at the bid and no seller crossed"
                        if _rested is True else
                        "no reason recorded - whether an order was ever placed "
                        "is UNKNOWN")
            log.info(
                f"[GRID] {product_id}: no maker buy completed and maker-ONLY mode is on - "
                f"passing this cycle rather than paying the taker leg. Reason: {_why}")
            await _record_maker_only_skip(GRID_MAKER_ONLY_SKIP_BUY_KEY)
            await _record_maker_expiry(
                session, product_id, "buy", bot_name, reason=_why,
                order_rested=_rested,
                block_detail=engine._last_order_block.get(product_id))
            if outcome_out is not None:
                import order_outcome
                # NOT hardcoded to MAKER_EXPIRED. Three of place_maker_buy's
                # returns never place an order (below the minimum size, no
                # bid, a size that floors to zero) and a fourth cannot say.
                # Labelling those "maker expired" put them in BENIGN_CAUSES,
                # where a branch too poor to trade or a book that will not
                # read is filed as the mode working as designed.
                outcome_out["cause"] = order_outcome.cause_for_maker_only_no_fill(_rested)
                if _rested is True:
                    outcome_out["wait_seconds"] = await maker_wait_seconds()
                else:
                    outcome_out["detail"] = _why
            return None
    fill = await engine.place_market_buy(session, usd_amount, product_id,
                                         source="grid_buy_market")
    if not fill:
        if outcome_out is not None:
            import order_outcome
            outcome_out["cause"] = order_outcome.REJECTED
            outcome_out["detail"] = engine._last_order_error.get(product_id)
        return None
    qty, price = fill
    await _record_fill_leg(GRID_FILL_MIX_BUY_KEY, False)
    return qty, price, (await get_effective_round_trip_fee_rate()) / 2


# How much of a slice may be left unsold before the row is retired anyway,
# as a FRACTION of the slice. Relative rather than absolute because this
# fleet's quantities span eleven orders of magnitude - PEPE slices are
# ~8,000,000 units and BTC slices are ~0.0005, so one absolute epsilon is
# either meaningless on one end or destructive on the other.
GRID_SELL_RESIDUAL_REL_EPSILON = 1e-6


def grid_sell_residual(slice_qty, filled_qty, rel_epsilon: float = None):
    """What is LEFT of a slice after a sell that may only have PARTLY filled.

    Returns (residual_qty, retire). `retire` is True when the slice row
    should be deleted; False when its qty should be reduced to
    `residual_qty` and the row kept.

    This exists because the caller used to delete the row whatever came
    back. grid_sell() returns what ACTUALLY filled - its own docstring
    says so - and the sell path priced the P&L with that number and then
    retired the whole slice regardless. A partial fill therefore removed
    units the wallet had not sold, and the branch's tracked units drifted
    away from the coin really held.

    The drift is self-reinforcing, which is why it matters: a branch that
    is already short cannot fill the NEXT sell for its full qty either,
    so one partial fill makes the following one more likely to be partial
    too. That is how a branch ends up claiming coin it does not have and
    can no longer be closed without reconcile-slices.

    A fill larger than the slice is not silently accepted as "extra" - it
    retires the slice and reports zero residual, because a negative
    residual written back to the row would claim the branch owes coin.
    """
    if rel_epsilon is None:
        rel_epsilon = GRID_SELL_RESIDUAL_REL_EPSILON
    try:
        slice_qty = float(slice_qty)
    except (TypeError, ValueError):
        # An unreadable slice qty is not a reason to delete the row. Keep
        # it and let coin_tracked_is_held report, which is what it is for.
        return None, False
    if slice_qty <= 0:
        # Nothing to keep. Retiring an empty row is the honest outcome.
        return 0.0, True
    try:
        filled_qty = float(filled_qty)
    except (TypeError, ValueError):
        filled_qty = 0.0
    if filled_qty != filled_qty:          # NaN
        filled_qty = 0.0
    if filled_qty <= 0:
        # Nothing sold. The caller should not reach here (grid_sell returns
        # None when nothing filled), but deleting on a zero fill would be
        # the original bug in its worst form.
        return slice_qty, False
    # EXACT DECIMAL, NOT FLOAT SUBTRACTION.
    #
    # Found 2026-09-29 in the live payload: two slices were sitting at
    # 0.009999999999998899 (LINK-USD) and 0.00999999999999801 (PRIME-USD)
    # against a base_increment of 0.01. Each should be EXACTLY one
    # tradeable unit, and each was one ULP below it - so the venue's rules
    # could not express them, they could never be sold, and they will never
    # grow. A permanently stuck slice, created by this line.
    #
    # float: 4.1 - 4.09 == 0.009999999999999787
    # Decimal("4.1") - Decimal("4.09") == 0.01, exactly.
    #
    # str() first, deliberately: it takes each float at the decimal
    # spelling it was written with, which is the number a person and the
    # venue both mean. Decimal(float) would instead preserve the binary
    # error this exists to remove.
    #
    # This is the same discipline execution_quantity applies to sizing. The
    # residual written back to the row IS a quantity the next cycle will
    # try to sell, so it has to survive the venue's rules just as an order
    # size does.
    try:
        residual = float(_Decimal(str(slice_qty)) - _Decimal(str(filled_qty)))
    except (InvalidOperation, ValueError, OverflowError):
        # Unparseable inputs are not a reason to guess. Fall back to the
        # plain subtraction rather than returning a number from nowhere;
        # the guards above have already rejected the cases that matter.
        residual = slice_qty - filled_qty
    if residual <= slice_qty * rel_epsilon:
        return 0.0, True
    return residual, False


async def grid_sell(session, qty: float, product_id: str, bot_name: str = None):
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
            _leg = await get_effective_maker_leg_fee_rate()
            return filled_qty, price, (_leg if _leg is not None
                                       else (await get_effective_round_trip_fee_rate()) / 2)
        if await is_maker_only_active():
            # WHY, not just THAT. place_maker_sell returns None for three
            # different reasons and this line used to call all of them "did
            # not fill". Two of the three never place an order at all, so
            # "did not fill" was actively misleading - it describes a
            # patient resting bid that in those cases does not exist. The
            # engine now records which one it was; report it.
            _why = engine._last_order_error.get(
                product_id, "the order rested at the ask and no buyer crossed")
            log.info(f"[GRID] {product_id}: no maker sell completed and maker-ONLY mode is on - "
                     f"holding the slice rather than paying the taker leg out of its own "
                     f"profit. Reason: {_why}")
            await _record_maker_only_skip(GRID_MAKER_ONLY_SKIP_SELL_KEY)
            await _record_maker_expiry(
                session, product_id, "sell", bot_name, reason=_why,
                # .get, so a product the engine said nothing about this cycle
                # lands as None (UNKNOWN) rather than defaulting to a claim.
                order_rested=engine._last_order_rested.get(product_id),
                block_detail=engine._last_order_block.get(product_id))
            return None
    fill = await engine.place_market_sell(session, qty, product_id,
                                          source="grid_sell")
    if not fill:
        return None
    filled_qty, price = fill
    await _record_fill_leg(GRID_FILL_MIX_SELL_KEY, False)
    return filled_qty, price, (await get_effective_round_trip_fee_rate()) / 2



# Horizons for the post-expiry drift experiment, in seconds. Four rather than
# one because "protective" and "too aggressive" can be the same order at
# different distances - a price that comes back in 60s and rolls over by 10
# minutes is a real shape, and a single horizon reports half of it.
# THE LADDER RUNS TO 72 HOURS. See GridMakerExpiry's own comment for why:
# a study built to ask "should the rung have rested longer?" that stops
# looking at 10 minutes can only ever answer it out to 10 minutes, and
# horizon_study measures the payoff arriving far past there (8.1% inside
# 30m, 34.1% inside 6h, 92.5% inside 72h).
#
# This changes the MEASUREMENT only. maker_wait_seconds is untouched.
_EXPIRY_HORIZONS = ((60, "1m"), (180, "3m"), (300, "5m"), (600, "10m"),
                    (1800, "30m"), (7200, "2h"), (21600, "6h"),
                    (86400, "24h"), (259200, "72h"))
_EXPIRY_FINAL_TAG = _EXPIRY_HORIZONS[-1][1]
_EXPIRY_FINAL_SECONDS = _EXPIRY_HORIZONS[-1][0]

# Resolving costs one book read per product per cycle, so it is capped. The
# backlog is tiny by construction (this fleet expires a handful of orders a
# day) and anything not resolved this cycle is resolved on the next one.
_EXPIRY_RESOLVE_MAX_PER_CYCLE = int(os.getenv("GRID_EXPIRY_RESOLVE_MAX", "8"))
# How many unresolved rows are LOOKED AT per cycle. Larger than the read cap
# because with a 72h ladder most scanned rows have nothing due yet, and a
# scan that stops before reaching the fresh ones is how a new expiry loses
# its 1m reading. Scanning is a cheap indexed read; the book call is what
# _EXPIRY_RESOLVE_MAX_PER_CYCLE bounds.
_EXPIRY_SCAN_MAX_PER_CYCLE = int(os.getenv("GRID_EXPIRY_SCAN_MAX", "200"))
# Below this a horizon reports "not enough data" rather than a finding. Not
# derived - chosen so three samples cannot be read as a result, which is how
# the 50-trade history got misread once already.
_EXPIRY_MIN_RESOLVED = int(os.getenv("GRID_EXPIRY_MIN_RESOLVED", "20"))


async def _record_order_not_placed(product_id: str, side: str, bot_name: str = None,
                                   reason: str = None, detail: dict = None):
    """One cycle where the venue never received an order. Never raises.

    Separate table from GridMakerExpiry on purpose - see GridOrderNotPlaced's
    own docstring. No book read here and no price anchor: there is no rung to
    measure the market against, so taking a quote would cost an API call to
    record a number with no question attached to it.
    """
    detail = detail or {}
    try:
        async with get_session_factory()() as db:
            db.add(GridOrderNotPlaced(
                bot_name=bot_name, product_id=product_id, side=side,
                reason=reason,
                # .get, so a path that did not know a figure records NULL
                # (UNKNOWN) rather than a fabricated zero.
                available_units=detail.get("available_units"),
                size_decimals=detail.get("size_decimals"),
                requested_qty=detail.get("requested_qty"),
            ))
            await db.commit()
    except Exception as e:
        log.debug(f"[GRID] not-placed row failed for {product_id} (ignored): "
                  f"{type(e).__name__}: {e}")


async def _record_maker_expiry(session, product_id: str, side: str, bot_name: str = None,
                               reason: str = None, order_rested: bool = None,
                               block_detail: dict = None):
    """Anchor one maker-ONLY cycle that ended without a trade. Never raises -
    this is instrumentation and must not be able to stop the thing it
    measures.

    Deliberately NOT called on the maker-first path, where an unfilled order
    becomes a market order: that trade happened, so "what did we miss" has no
    meaning there, and mixing the two would put filled cycles in the ledger
    of unfilled ones.

    order_rested says whether an order was ever actually on the book. It is
    passed in from the engine's own structural record rather than inferred
    here, and None is a real value meaning UNKNOWN - the docstring this
    function used to carry asserted every row was a rested order, which was
    untrue for two of place_maker_sell's three None returns.
    """
    # THE INVARIANT, ENFORCED HERE RATHER THAN BY THE SCHEMA.
    #
    # An expiry row is never written for a CONFIRMED non-order. That is the
    # whole separation: GridMakerExpiry is the study of rungs that rested,
    # and a cycle that placed nothing cannot belong to it.
    #
    # Enforced in code and not as an assert or a NOT NULL column, for two
    # reasons. First, this function is instrumentation and must never be able
    # to stop the thing it measures - an assert on a live sell path would do
    # exactly that. Second, order_rested has THREE states: a NOT NULL column
    # cannot represent the genuinely unknown one, and forcing it would turn
    # an unread into a no, which is the bug this whole line of work started
    # from.
    #
    # False -> the other table. None (UNKNOWN) stays here, where the study
    # excludes it by name instead of counting it.
    if order_rested is False:
        await _record_order_not_placed(product_id, side, bot_name,
                                       reason=reason, detail=block_detail)
        return

    try:
        bid, ask = await engine.get_best_bid_ask(session, product_id)
        if bid is None or ask is None:
            log.debug(f"[GRID] expiry anchor skipped for {product_id}: no book")
            return
        async with get_session_factory()() as db:
            db.add(GridMakerExpiry(
                bot_name=bot_name, product_id=product_id, side=side,
                wait_seconds=await maker_wait_seconds(),
                bid_at_expiry=bid, ask_at_expiry=ask,
                reason=reason, order_rested=order_rested,
                # Mid at BOTH ends, so drift is one instrument measured
                # twice. Anchoring on the bid and resolving on the mid would
                # book half the spread as a move the market never made.
                price_at_expiry=(bid + ask) / 2.0,
            ))
            await db.commit()
    except Exception as e:
        log.debug(f"[GRID] expiry anchor failed for {product_id} (ignored): "
                  f"{type(e).__name__}: {e}")


async def _resolve_maker_expiries(session):
    """Fill in whichever horizons have come due. Never raises.

    Each horizon resolves independently and only once, so a row is usable
    while still filling in, and a restart mid-flight loses nothing.
    """
    try:
        now = datetime.utcnow()
        async with get_session_factory()() as db:
            # A ROW WITH NOTHING DUE MUST NOT CONSUME A SLOT.
            #
            # The limit exists to cap book reads, and before the ladder ran
            # to 72h it doubled as a row cap harmlessly - every unresolved
            # row was minutes old and had something due. Now a row stays
            # unresolved for three days, and oldest-first ordering would
            # hand all 8 slots to rows waiting on their 24h and 72h marks
            # while every freshly expired order starved and lost its 1m
            # reading permanently. The scan is cheap; the book read is not,
            # so the cap now bounds the reads and the scan is widened to
            # cover the backlog the longer ladder creates.
            rows = (await db.execute(
                select(GridMakerExpiry)
                .where(GridMakerExpiry.resolved_at.is_(None))
                .order_by(GridMakerExpiry.expired_at)
                .limit(_EXPIRY_SCAN_MAX_PER_CYCLE))).scalars().all()
            if not rows:
                return
            prices = {}
            touched = 0
            for row in rows:
                # A ROW THAT NEVER PLACED AN ORDER HAS NOTHING TO RESOLVE.
                #
                # The whole measurement is "would this order have filled had
                # it been left to rest?" - which is not a question about a
                # cycle where no order was ever created. Retired here rather
                # than filtered out of the scan, because retiring costs no
                # book read and DRAINS the backlog, while filtering would
                # leave these rows unresolved forever.
                #
                # This is a throughput fix as much as a correctness one. The
                # scan is oldest-first and 200 rows wide against a backlog
                # growing by ~2,600 no-order rows a day; left in, they would
                # consume the whole scan window and the rows the study can
                # actually use would never be reached, let alone resolved
                # inside the 8-book-read budget.
                #
                # order_rested is None (UNKNOWN) is deliberately NOT retired:
                # those rows predate the column and may well have been real
                # rests. An unknown is not a no.
                if row.order_rested is False:
                    row.resolved_at = now
                    continue
                age = (now - row.expired_at).total_seconds() if row.expired_at else 0
                # Past the final horizon with gaps still open, this row can
                # never fill them - a restart or an unreadable book cost it
                # a reading. Retire it so it stops being scanned forever.
                if age >= _EXPIRY_FINAL_SECONDS + 3600 and row.resolved_at is None:
                    row.resolved_at = now
                    continue
                due = [(sec, tag) for sec, tag in _EXPIRY_HORIZONS
                       if age >= sec and getattr(row, f"price_{tag}") is None]
                if not due:
                    continue
                if touched >= _EXPIRY_RESOLVE_MAX_PER_CYCLE:
                    break
                touched += 1
                if row.product_id not in prices:
                    try:
                        bid, ask = await engine.get_best_bid_ask(session, row.product_id)
                        prices[row.product_id] = ((bid + ask) / 2.0
                                                  if bid is not None and ask is not None else None)
                    except Exception:
                        prices[row.product_id] = None
                price = prices.get(row.product_id)
                if price is None or not row.price_at_expiry:
                    continue
                drift = (price / row.price_at_expiry - 1.0) * 100.0
                # THE SIGN CONVENTION, fixed in exactly one place.
                # A cancelled BUY did not buy, so a fall afterwards is good.
                # A cancelled SELL still holds, so a rise afterwards is good.
                benefit = -drift if row.side == "buy" else drift
                for sec, tag in due:
                    setattr(row, f"price_{tag}", price)
                    setattr(row, f"drift_{tag}_pct", round(drift, 4))
                    setattr(row, f"cancel_benefit_{tag}_pct", round(benefit, 4))
                if getattr(row, f"price_{_EXPIRY_FINAL_TAG}") is not None:
                    row.resolved_at = now
            await db.commit()
    except Exception as e:
        log.debug(f"[GRID] expiry resolution pass failed (ignored): "
                  f"{type(e).__name__}: {e}")


async def get_maker_expiry_drift() -> dict:
    """Did cancelling help? Measured, per horizon, sign-corrected.

    POSITIVE mean benefit = the timeout protected the account. NEGATIVE =
    it is too aggressive and is handing back fills that were about to come
    good. The verdict field stays "not enough data" until there are enough
    resolved rows to mean anything, because the temptation to read three
    samples as a finding is exactly how the 50-trade history got misread.
    """
    try:
        async with get_session_factory()() as db:
            # Bounded for the same reason summary() is: this is served in a
            # live status payload and must not grow into a slow query.
            # TWO QUERIES, ON PURPOSE.
            #
            # `recent` is the honest mix of what is being written lately, and
            # is what the excluded counts are reported from. `rows` is the
            # study sample and admits ONLY rows where an order actually
            # rested, because the question this table exists to answer -
            # should a resting rung be given longer? - is meaningless for a
            # cycle where nothing was ever on the book.
            #
            # Filtering in SQL rather than in Python because a single capped
            # query would starve: ALGO and QNT were writing ~2,600 no-order
            # rows a day between them, so the 5,000-row window would have
            # held almost no usable rows within about two days while still
            # reporting a confident mean.
            recent = (await db.execute(
                select(GridMakerExpiry)
                .order_by(GridMakerExpiry.expired_at.desc())
                .limit(5000))).scalars().all()
            rows = (await db.execute(
                select(GridMakerExpiry)
                .where(GridMakerExpiry.order_rested.is_(True))
                .order_by(GridMakerExpiry.expired_at.desc())
                .limit(5000))).scalars().all()
    except Exception as e:
        return {"available": False, "error": f"{type(e).__name__}: {e}"}
    if not recent:
        return {"available": False, "expiries": 0,
                "note": "no maker-only cycle has ended unfilled yet"}

    _no_order = sum(1 for r in recent if r.order_rested is False)
    _unknown = sum(1 for r in recent if r.order_rested is None)
    out = {"available": True,
           # Counted over the recent window, not the study sample, so the
           # denominator is never quietly the filtered number.
           "expiries": len(recent),
           "buy": sum(1 for r in recent if r.side == "buy"),
           "sell": sum(1 for r in recent if r.side == "sell"),
           "sample": len(rows),
           "excluded_no_order_placed": _no_order,
           "excluded_unknown_whether_rested": _unknown,
           "sample_is": (
               "only cycles where an order was really on the book and nobody "
               "crossed it. A cycle that never placed an order cannot say "
               "whether resting longer would have helped, and counting it as "
               "a zero-benefit sample would drag every mean toward nothing."),
           "horizons": {}}
    if not rows:
        # Said out loud rather than left to read as a quiet zero: an empty
        # sample here is UNKNOWN, not a finding, and right after this column
        # shipped it is simply the whole history sitting in the unknown
        # bucket because nothing before it recorded the fact.
        out["sample_note"] = (
            f"no row in this window is confirmed to have rested "
            f"({_no_order} placed no order, {_unknown} predate the "
            f"order_rested column and are UNKNOWN). Every verdict below is "
            f"withheld until confirmed rows accumulate - this is a missing "
            f"measurement, not a negative result.")
    for _sec, tag in _EXPIRY_HORIZONS:
        vals = [getattr(r, f"cancel_benefit_{tag}_pct") for r in rows
                if getattr(r, f"cancel_benefit_{tag}_pct") is not None]
        out["horizons"][tag] = {
            "n": len(vals),
            "mean_benefit_pct": round(sum(vals) / len(vals), 4) if vals else None,
            "helped": sum(1 for v in vals if v > 0),
            "hurt": sum(1 for v in vals if v < 0),
        }
    wait_s = await maker_wait_seconds()
    ten = out["horizons"]["10m"]
    # 20 is not a magic number with a derivation behind it - it is simply the
    # point below which this says nothing, chosen so the field cannot be read
    # as a finding while it is still noise.
    if (ten["n"] or 0) < _EXPIRY_MIN_RESOLVED:
        out["verdict"] = (f"not enough data ({ten['n']}/{_EXPIRY_MIN_RESOLVED} "
                          f"resolved at 10m)")
    elif ten["mean_benefit_pct"] > 0:
        out["verdict"] = (f"the {wait_s}s timeout looks PROTECTIVE - price moved "
                          f"against us after cancelling, on average")
    else:
        out["verdict"] = (f"the {wait_s}s timeout looks TOO AGGRESSIVE - price moved "
                          f"in our favour after cancelling, on average")

    # THE QUESTION THE SHORT HORIZONS CANNOT ANSWER.
    #
    # The verdict above asks whether cancelling was right over the next ten
    # minutes. It is a different question from the one the account owner is
    # actually asking, which is whether the rung should have been left to
    # rest for hours - horizon_study puts 34.1% of round trips inside 6h and
    # 92.5% inside 72h, none of which is visible at 10m.
    #
    # Reported as its OWN verdict rather than replacing the short one,
    # because they can legitimately disagree: a cancel can be protective at
    # 10 minutes and still have cost a fill that came good by the next day.
    # The longest horizon with enough resolved rows wins, so this stays
    # silent until the data arrives instead of reading three samples as a
    # finding.
    long_tags = [t for _s, t in _EXPIRY_HORIZONS if t in ("30m", "2h", "6h", "24h", "72h")]
    usable = [t for t in long_tags
              if (out["horizons"][t]["n"] or 0) >= _EXPIRY_MIN_RESOLVED]
    if not usable:
        best = max(long_tags, key=lambda t: out["horizons"][t]["n"] or 0)
        out["long_horizon_verdict"] = (
            f"not enough data yet - the longest horizon with any resolved rows is "
            f"{best} at {out['horizons'][best]['n'] or 0}/{_EXPIRY_MIN_RESOLVED}. "
            f"Rows take {_EXPIRY_FINAL_SECONDS // 3600}h to fill the full ladder, "
            f"so this is a matter of waiting, not of a missing measurement.")
    else:
        tag = usable[-1]
        h = out["horizons"][tag]
        out["long_horizon_tag"] = tag
        out["long_horizon_verdict"] = (
            (f"over {tag}, cancelling HELPED by {h['mean_benefit_pct']:+.4f}% per expiry "
             f"({h['helped']} helped / {h['hurt']} hurt, n={h['n']}) - resting the rung "
             f"longer than {wait_s}s would have cost money, not made it.")
            if (h["mean_benefit_pct"] or 0) > 0 else
            (f"over {tag}, cancelling COST {abs(h['mean_benefit_pct']):.4f}% per expiry "
             f"({h['helped']} helped / {h['hurt']} hurt, n={h['n']}) - the fills were "
             f"coming, and {wait_s}s is too short to collect them. This is the evidence "
             f"for lengthening the rest; nothing changes the wait automatically."))

    # WHICH SIDE IS JAMMED, SAID OUT LOUD.
    #
    # The buy/sell split was already in this payload and the page printed it
    # as a parenthetical beside the total, where it reads as bookkeeping. It
    # is the diagnosis. Measured on the live fleet 2026-10-04:
    #
    #   expired orders   3,732  ->     51 buy /  3,681 sell   98.6% sells
    #   skipped cycles  19,600  ->     65 buy / 19,535 sell   99.7% sells
    #
    # This fleet is not failing to buy. It is failing to SELL, and the two
    # are not independent: the buy gate is
    # `len(tradeable_slices(slices)) < branch.num_levels`, so a branch that
    # cannot sell stays full on its rungs and is refused every entry. A
    # jammed exit presents as a dead entry, and a reader looking at
    # GATE_PASS 0 goes hunting for a buy-side problem that is not there.
    #
    # Silent below the same minimum the verdicts use, and silent when the
    # split is balanced - this only speaks when one side dominates.
    _b, _sl = out.get("buy") or 0, out.get("sell") or 0
    _tot = _b + _sl
    if _tot >= _EXPIRY_MIN_RESOLVED:
        _share = max(_b, _sl) / _tot
        if _share >= 0.80:
            _side = "SELL" if _sl > _b else "BUY"
            _other = "buy" if _side == "SELL" else "sell"
            out["jammed_side"] = _side
            out["jammed_side_share_pct"] = round(_share * 100, 1)
            out["jammed_side_note"] = (
                f"{_share * 100:.1f}% of the orders that expired unfilled were "
                f"{_side}s ({_sl if _side == 'SELL' else _b} of {_tot}). This is "
                f"an EXIT problem, not an entry one."
                if _side == "SELL" else
                f"{_share * 100:.1f}% of the orders that expired unfilled were "
                f"{_side}s ({_b} of {_tot}).")
            if _side == "SELL":
                out["jammed_side_note"] += (
                    " They are not independent: the buy gate requires a branch "
                    "to hold fewer open slices than it has levels, so a branch "
                    "that cannot sell stays full and is refused every entry. A "
                    "jammed exit shows up as a dead entry.")
    return out


def slice_paid_no_entry_fee(slice_row) -> bool:
    """True when this slice's cost basis never cost a commission.

    An ADOPTED slice is written by coin_adoption_worker for coin the
    account ALREADY HOLDS, at the market price on the day it was adopted
    (see coin_adoption.slice_units: "every slice is adopted at the same
    moment at the same price"). No buy order is placed, so Coinbase bills
    no entry commission against that basis. Whatever was paid on the
    original purchase was paid long ago against a different price - it is
    sunk, and it is not part of this grid's round trip.

    Charging an entry leg anyway is not only a display error. The same
    rate feeds _pick_profitable_slice_to_sell(), so a phantom fee makes an
    adopted slice look less profitable than it really is and holds it back
    from a sale it has already earned. That is the buy-dip/sell-rise loop
    being blocked by a fee nobody paid.

    The `adopted` flag is the marker because it is the only one already
    carried by every live adopted row. Writing entry_fee_rate=0.0 instead
    would not work: both resolvers treat a non-positive rate as UNKNOWN
    and fall back, so a recorded zero is indistinguishable from a missing
    one. Zero-is-unknown is right for a real buy (a leg with no recorded
    rate still paid something) and wrong for an adopted one, which is
    exactly why this needs its own marker rather than a magic value.
    """
    return bool(getattr(slice_row, "adopted", False))


async def slice_round_trip_fee_rate(slice_row, exit_leg_rate: float = None) -> float:
    """The REAL round-trip fee for one specific slice: the rate its BUY leg
    genuinely paid (recorded on the slice) plus the rate its SELL leg is
    expected to pay. With maker orders live the two legs can genuinely
    differ, so assuming one rate for both would misprice the trade - the
    exact class of bug that made the bot sell real losers as wins."""
    if slice_paid_no_entry_fee(slice_row):
        # No order, no commission - see slice_paid_no_entry_fee().
        entry_rate = 0.0
    else:
        entry_rate = getattr(slice_row, "entry_fee_rate", None)
        if entry_rate is None or entry_rate <= 0:
            entry_rate = (await get_effective_round_trip_fee_rate()) / 2
    if exit_leg_rate is None:
        exit_leg_rate = await expected_leg_fee_rate(
            getattr(slice_row, "product_id", None))
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


async def get_effective_maker_leg_fee_rate():
    """The measured maker LEG rate: this process's observation, else the
    last one persisted, else None.

    None means "nothing has ever measured this", which is NOT zero and not
    a licence to assume the cheap path - every caller treats None as "price
    the taker leg". That is the whole reason this returns None rather than
    a default.

    Mirrors get_effective_round_trip_fee_rate() deliberately. The two rates
    are measured in the same call, by the same fee-tier lookup, and used by
    the same floor; only one of them being durable is what let the floor
    move between workers and across restarts without anything changing in
    the market.
    """
    global _cached_real_maker_fee_rate
    if _cached_real_maker_fee_rate is not None:
        return _cached_real_maker_fee_rate
    try:
        async with get_session_factory()() as db:
            result = await db.execute(
                select(TradingBotState).where(TradingBotState.bot_name == REAL_FEE_RATE_STATE_KEY))
            row = result.scalar_one_or_none()
            if row is not None and row.starting_capital and row.starting_capital > 0:
                _cached_real_maker_fee_rate = float(row.starting_capital)
                return _cached_real_maker_fee_rate
    except Exception as e:
        log.warning(f"[GRID] could not read the persisted maker leg rate: "
                    f"{type(e).__name__}: {e}")
    return None


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
    # The DURABLE measurement, not just this process's copy. Both guards in
    # the docstring above still hold exactly as written: None (nothing ever
    # measured) prices taker, maker-only being off prices taker, and the
    # result is clamped to the taker leg. The only change is that a measured
    # rate now survives a restart and reaches every worker, instead of the
    # floor quietly reverting to 1.70% because this particular process had
    # not happened to fetch the fee tier yet.
    maker_leg = await get_effective_maker_leg_fee_rate()
    if maker_leg is None:
        return taker_leg
    if not await is_maker_only_active():
        return taker_leg
    return min(float(maker_leg), taker_leg)


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
# RE-MEASURED 2026-09-28 at the REAL fee, over a range that goes WIDER.
#
# The 14-day table above was priced at a 1.37% round trip. The account's
# measured cost is 0.70%. It also only ever tested steps going DOWN, and the
# widest step it tried won - so nobody had looked at where the curve turns
# over. Re-run with step_study.py over 60 days of real hourly candles across
# all 20 live coins, priced at 0.70%:
#
#     step   trips/60d   net each   fleet $
#     1.00%     136        +0.30%    $28.15
#     1.50%      93        +0.80%    $51.34
#     2.00%      70        +1.30%    $62.79
#     2.50%      54        +1.80%    $67.07   <- the old minimum
#     3.00%      46        +2.30%    $73.00
#     3.50%      37        +2.80%    $71.48
#     4.00%      33        +3.30%    $75.14   <- nominal peak
#     5.00%      24        +4.30%    $71.21
#     6.00%      20        +5.30%    $73.14
#     8.00%      12        +7.30%    $60.44
#
# 3.00% to 6.00% is a PLATEAU - every value in it lands within ~5% of the
# others, which is inside the noise of 20-46 trips. The nominal 4.00% peak is
# not meaningfully better than 3.00%, so this takes the NEAR edge: the
# smallest step that reaches the plateau, keeping the most trips for
# essentially the same money and the least extrapolation from the evidence.
#
# Two independent studies now agree on the direction. The 14-day table ranked
# 3.0% first of the four it tried; this 60-day run ranks 2.5% below every
# step from 3.0% to 6.0%. Worth +$5.93 per 60 days on the same capital, about
# +9%, for FEWER trades - which is the whole finding and the opposite of the
# intuition that keeps suggesting a tighter grid.
#
# PER-COIN STEPS WERE MEASURED AND REJECTED. Taking each coin's own argmax
# scored +70.6% over one global step, and it is an artifact: gate it on the
# chosen step having even 5 trips behind it and exactly ONE coin of twenty
# survives (ACH-USD at 3.00%, 16 trips). Every other coin's "best" step rests
# on 1 to 4 trips in sixty days. Choosing the maximum over ten candidates on
# a sample that size fits noise, and would have shipped it as a gain.
FLEET_MIN_STEP_PCT = float(os.getenv("GRID_FLEET_MIN_STEP_PCT", "0.030"))

# The round-trip fee the table above was priced at. Stated as a CHECKABLE
# CONSTANT rather than left in the prose, because the prose cannot be
# verified and this can: invariants.spacing_evidence_current() compares it
# against the fee actually being billed and fails when they diverge.
#
# This is not decoration. The table ranks 1.0/2.0/2.5/3.0% by net-per-trip
# at this fee. Re-run at the 0.70% round trip now measured, the same model
# scores 10.2 / 22.1 / 21.6 / 23.0 - the ordering FLATTENS and 2.0% pulls
# level with 2.5%, where at 1.37% it was a clear last. The constant that
# sets the fleet minimum is still defensible; the evidence under it is
# priced at a cost the account no longer pays, and nothing would have said
# so. Re-measure before moving FLEET_MIN_STEP_PCT on the strength of it.
# Re-measured at the real fee on 2026-09-28, so the evidence and the cost
# now agree. The invariant fails again the moment they diverge.
SPACING_EVIDENCE_PRICED_AT_ROUND_TRIP = 0.0070

# Mirrors crypto_nine_coin_scanner.DEFAULT_MAX_TARGET_SWING_MULTIPLE by
# VALUE, not by import - that module imports helpers from this one, so the
# reverse would be a circular import. Same duplicate-by-value discipline
# already used for GRID_LEVEL_SPACING_CANDIDATES. If the gate's limit ever
# moves, this must move with it or the cap stops matching the gate it exists
# to satisfy; a test asserts the two agree.
SWING_CEILING_MULTIPLE = 3.0


def _swing_ceiling_enabled() -> bool:
    """On unless switched off. Off restores the old behaviour exactly: the
    fleet minimum raises the step and the gate refuses the buy."""
    raw = (os.getenv("GRID_SWING_CEILING") or "").strip().strip('"').strip("'").lower()
    return raw not in {"0", "false", "no", "off"}

# ---- THE STOP LOSS ----
#
# Sell a slice that has fallen this far below its own entry, at a loss, on
# purpose. Zero disables it.
#
# Added 2026-09-26 for a problem the account owner named from experience:
# "I wind up losing on these coins, and it took a long time for it to
# recover the money." Without a stop, a slice that falls 40% is held
# indefinitely waiting for +2.50%. BONK did exactly that, -43.8% in one
# measured window, and the grid simply sat in it.
#
# 8% is not a fitted number. Swept 5% to 25% on the live six coins across
# two out-of-sample windows, EVERY level beat no-stop in the losing window:
#
#     stop     losing window    winning window
#     none          -$15.28            +$2.77
#     5%             -$7.51            +$2.77
#     8%             -$5.36            +$2.77   <- chosen
#     10%            -$8.61            +$2.77
#     25%            -$9.23            +$2.77
#
# Two things make this worth shipping where the rest of tonight's tuning
# was not. The benefit does not depend on the exact level - anything in
# that range roughly halves the loss - so it is not a parameter found by
# searching. And the winning window is IDENTICAL at every level, because
# no stop ever fired there: it costs nothing when things go well and only
# acts when they do not.
GRID_STOP_LOSS_PCT = float(os.getenv("GRID_STOP_LOSS_PCT", "0.08"))
if GRID_STOP_LOSS_PCT < 0 or GRID_STOP_LOSS_PCT >= 1:
    raise ValueError(f"GRID_STOP_LOSS_PCT must be in [0, 1), got {GRID_STOP_LOSS_PCT}")


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


_ACCOUNT_BOOK_CACHE = {"book": None, "at": 0.0}
ACCOUNT_BOOK_TTL_SECONDS = 60.0


async def account_market_book(max_age_seconds=ACCOUNT_BOOK_TTL_SECONDS):
    """{ASSET: market value usd} for the WHOLE account, cash included.

    The one book the concentration ceiling is measured against, for the
    buy gate here and for auto_trim alike - see concentration_gate's
    module docstring for why it is this book and not grid cost basis.

    Returns None when the account cannot be read. None is load-bearing:
    the gate fails OPEN on None, and an empty dict would instead mean
    "the account holds nothing", which is a different decision.

    Cached briefly because this runs on every buy across a 23-branch
    fleet and the figure it produces moves on price, not on the cycle.
    A stale-by-a-minute denominator cannot flip a 20% verdict; a
    per-buy balance sweep would cost more than the check is worth.
    """
    import time as _t
    now = _t.time()
    cached = _ACCOUNT_BOOK_CACHE.get("book")
    if cached is not None and (now - _ACCOUNT_BOOK_CACHE["at"]) < max_age_seconds:
        return cached
    try:
        import account_census
        import concentration_gate
        import aiohttp as _aiohttp
        async with _aiohttp.ClientSession() as _s:
            census = await account_census.census(_s, tracked_usd=0.0)
        if not census.get("available"):
            return None
        book = concentration_gate.book_from_holdings(census.get("holdings") or [])
    except Exception as exc:
        log.warning(f"[GRID] account book unreadable ({type(exc).__name__}: {exc}) - "
                    f"concentration not checked this cycle")
        return None
    _ACCOUNT_BOOK_CACHE.update(book=book, at=now)
    return book


_WALLET_UNITS_CACHE = {"units": None, "at": 0.0}
WALLET_UNITS_TTL_SECONDS = 60.0


async def wallet_owned_units(max_age_seconds=WALLET_UNITS_TTL_SECONDS):
    """{ASSET: units the account OWNS}, or None when unreadable.

    OWNED, NOT AVAILABLE. This read used available_units for its first
    four hours and that was wrong: it refused buys on SOL, LINK, ALGO and
    ACH, whose coin is entirely present and merely sitting under the
    fleet's own resting sell orders. See account_census.owned_units_map
    for the measurement, and reconcile-slices' own comment, which had
    already named SOL and LINK as "owned in full and only locked".

    NOT account_market_book(). That book is built from census()'s
    holdings list, and account_census.wallet_units_for's own docstring
    records why that is the wrong input to a held-units question: census
    drops assets it cannot price and rolls anything under the dust
    threshold into an unnamed count. Its job is "what is this account
    worth"; it cannot answer "does this account hold X at all". Handing
    census holdings to this question is a bug this repo already shipped
    once - QNT's three largest shortfalls were reported as unknown in a
    footnote while the headline named only the six smaller ones.

    None is load-bearing. The backing gate treats None as UNKNOWN and
    does NOT refuse, because slice_backing's doctrine is that an
    unreadable balance is not a shortfall. Returning {} instead would
    mean "the account holds nothing", which would refuse every buy in
    the fleet on a rate limit.

    Cached on the same terms, and for the same reason, as
    _ACCOUNT_BOOK_CACHE: this runs on every buy across a 22-branch
    fleet, and a per-branch sweep of the accounts endpoint is precisely
    what got this read rate-limited before - 22 paginated reads a cycle
    to answer a question whose answer moves on fills, not on cycles.
    """
    import time as _t
    now = _t.time()
    cached = _WALLET_UNITS_CACHE.get("units")
    if cached is not None and (now - _WALLET_UNITS_CACHE["at"]) < max_age_seconds:
        return cached
    try:
        import account_census
        import aiohttp as _aiohttp
        async with _aiohttp.ClientSession() as _s:
            _bal = await account_census.fetch_balances(_s)
        units = account_census.owned_units_map(_bal)
    except Exception as exc:
        log.warning(f"[GRID] wallet units unreadable ({type(exc).__name__}: {exc}) - "
                    f"backing not checked this cycle")
        return None
    if units is None:
        return None
    _WALLET_UNITS_CACHE.update(units=units, at=now)
    return units


async def branch_backing_verdict(product_id, slices, price,
                                 units=None) -> tuple:
    """(ok, reason) - False only on a CONFIRMED shortfall.

    THE $45.60 THIS EXISTS TO STOP. On 2026-10-04 at 01:53:19Z the grid
    bought 3.24 LINK into a branch whose books claimed 9.34 units while
    the wallet held 3.46 - 37.045% backed, `can_be_sold: false`. The
    buy gate could not see it: the rung count that opened the gate
    (tradeable_slices) judges a slice by its DOLLAR BASIS, and an
    unbacked slice carries a perfectly normal basis. Three other
    branches were in the same state - SOL 25.0%, ALGO 43.3%, ACH
    0.000086% - $245.23 of claimed coin not in the wallet.

    Cash spent into a branch that cannot sell what it claims is worse
    than cash left idle: the branch takes the position and has no exit.

    THE UNITS MAP IS OWNED, NOT AVAILABLE, AND THAT IS THE WHOLE FIX.
    This gate shipped reading available_units and spent four hours
    refusing buys on SOL, LINK, ALGO and ACH - branches that own every
    unit they claim, with the coin sitting under the fleet's own resting
    sell orders. $245.23 was reported to the owner as "coin not in the
    wallet"; none of it was missing. See account_census.owned_units_map.
    The dashboard's `backing` block legitimately uses AVAILABLE, because
    it answers "can this branch sell right now". This gate answers "does
    this coin exist", so the two read different maps on purpose and will
    disagree whenever coin is on hold - that disagreement is correct.

    The arithmetic still comes from slice_backing.assess() rather than
    being re-derived here, so the backed threshold lives in one place.

    REFUSE-ONLY, and it can never block a sell: the one caller is the
    buy branch of the gate. UNKNOWN is not a refusal.
    """
    import slice_backing
    if units is None:
        return True, "wallet units unreadable - backing not checked (UNKNOWN is not a shortfall)"
    probe = [{
        "product_id": product_id,
        # assess() reads qty off each slice and prices the claim; the
        # unrealized figure only drives its phantom-gain flag, never the
        # backed verdict, so it is not invented here.
        "slices": [{"qty": slice_qty(s)} for s in (slices or [])],
        "current_price": price,
        "total_unrealized_net_usd": 0.0,
    }]
    out = slice_backing.assess(probe, units)
    for row in (out.get("unbacked") or ()):
        return False, (
            f"{row['backed_pct']:.3f}% backed - books claim "
            f"{row['claimed_units']:.8f} {row['asset']}, wallet holds "
            f"{row['held_units']:.8f} (short ${row['short_usd']:,.2f}). "
            f"A sell sizes against what the venue releases, so this branch "
            f"cannot exit what it already holds. Correcting the books is "
            f"reconcile-slices, which is the owner's to run."
        )
    for row in (out.get("unknown") or ()):
        return True, f"backing unknown for {row['asset']} - {row['reason']}"
    return True, "backed"


async def fleet_cost_basis_by_product():
    """Real USD cost basis per COIN across the whole grid fleet.

    Keyed by product_id, not bot_name: two branches can hold the same
    coin, and "no coin over 20% of the fleet" is a statement about the
    coin, so a concentration measured per branch would miss exactly the
    case it exists to catch.

    Cost basis (qty x entry_price) is the money that actually left the
    wallet. Deliberately not allocated_usd, which is a plan rather than a
    position, and not live market value, which would tighten the ceiling
    on a coin purely because it rallied.

    Returns None - never an empty dict - if the read fails. UNKNOWN and
    "the fleet holds nothing" are different answers, and the gate that
    consumes this treats them differently.
    """
    try:
        async with get_session_factory()() as db:
            branches = (await db.execute(select(CryptoGridBranch))).scalars().all()
            slices = (await db.execute(select(CryptoGridSlice))).scalars().all()
    except Exception as exc:
        log.warning(f"[GRID] fleet cost basis unreadable: {exc}")
        return None

    product_by_bot = {b.bot_name: b.product_id for b in branches}
    basis = {}
    for s in slices:
        if s.qty is None or s.entry_price is None:
            continue
        product = product_by_bot.get(s.bot_name)
        if not product:
            continue
        basis[product] = basis.get(product, 0.0) + s.qty * s.entry_price
    return basis


async def fleet_tracked_units_by_product():
    """Open-slice QUANTITY per coin across the whole fleet, and each
    coin's live price. Returns (units_by_product, price_by_product), or
    (None, None) if the read fails - never an empty dict, which would
    read as "the fleet holds nothing" and pass every check trivially.

    Units, not dollars. Every other reconciliation here works in dollars
    and in aggregate, which is exactly why a resting stop eating part of
    one branch's ETH went unnoticed: the fleet-wide totals still added
    up. See invariants.coin_tracked_is_held.
    """
    try:
        async with get_session_factory()() as db:
            branches = (await db.execute(select(CryptoGridBranch))).scalars().all()
            slices = (await db.execute(select(CryptoGridSlice))).scalars().all()
    except Exception as exc:
        log.warning(f"[GRID] tracked units unreadable: {exc}")
        return None, None

    product_by_bot = {b.bot_name: b.product_id for b in branches}
    units = {}
    for sl in slices:
        if sl.qty is None:
            continue
        product = product_by_bot.get(sl.bot_name)
        if product:
            units[product] = units.get(product, 0.0) + float(sl.qty)

    prices = {}
    if units:
        try:
            async with engine.aiohttp.ClientSession() as session:
                for product in units:
                    price, _atr = await engine.get_price_and_volatility(session, product)
                    if price is not None:
                        prices[product] = price
        except Exception as exc:
            log.warning(f"[GRID] tracked-unit pricing partial: {exc}")
    return units, prices


#: Where the adopted catastrophe stop's DB switch lives, in the same
#: TradingBotState table and the same 1.0/0.0 shape MAKER_ONLY_MODE_KEY uses.
ADOPTED_STOP_MODE_KEY = "grid_adopted_stop_mode"


async def adopted_stop_mode() -> str:
    """"arm" or "off" for the adopted catastrophe stop, env first then the DB.

    WHY A DB SWITCH AT ALL, when adaptive_stop already reads an env var. The
    env var was the only way in, and this fleet's owner runs everything else
    from the dashboard - maker-only, the spacing override, the net-edge gate
    are all DB-backed with a button, precisely so a real-money setting can be
    changed and SEEN without a Railway visit and a restart. An env-only switch
    on the one control that decides whether $6,271 of adopted coin has a stop
    was the odd one out.

    Precedence is the same as is_maker_only_active's, deliberately: the
    environment WINS when it says anything, so a variable set in Railway can
    always override whatever is in the database - including forcing it OFF
    when the DB says arm. get_grid_status reports which one decided.

    FAILS CLOSED to "off". Every consumer treats "arm" as permission to SELL,
    so an unreadable toggle must never answer "arm" - the same rule
    is_maker_only_active follows for the same reason, one direction being
    safe and the other not.
    """
    import adaptive_stop
    raw = os.getenv(adaptive_stop.ADOPTED_MODE_ENV)
    if raw is not None and str(raw).strip():
        # Anything the environment says is final, armed or not. Quotes
        # stripped for the same reason maker_only_env_override strips them: a
        # pasted Railway value has broken this deployment once already.
        return adaptive_stop.adopted_mode(str(raw).strip().strip('"').strip("'"))
    try:
        async with get_session_factory()() as db:
            row = (await db.execute(select(TradingBotState).where(
                TradingBotState.bot_name == ADOPTED_STOP_MODE_KEY))).scalar_one_or_none()
            if row is not None and row.base_capital and row.base_capital >= 1.0:
                return adaptive_stop.ADOPTED_MODE_ARM
    except Exception as e:
        log.warning(f"[GRID] adopted-stop toggle unreadable ({e}) - staying OFF, so no "
                    f"adopted branch gains a stop from a read that failed")
    return adaptive_stop.ADOPTED_MODE_OFF


async def adopted_stop_mode_source() -> str:
    """Which switch is deciding, for the dashboard to show.

    A setting whose value is visible but whose SOURCE is not is how an
    operator comes to click a button that cannot win - the maker-only card
    disables its own button when the environment is in charge, and this
    exists so the same can be done here.
    """
    import adaptive_stop
    raw = os.getenv(adaptive_stop.ADOPTED_MODE_ENV)
    if raw is not None and str(raw).strip():
        return f"environment {adaptive_stop.ADOPTED_MODE_ENV} (wins over the database)"
    return "database"


async def set_adopted_stop_active(enabled: bool):
    """Arm (True) or disarm (False) the adopted catastrophe stop.

    THIS SELLS REAL COIN WHEN ARMED. Not immediately and not on its own
    schedule - it gives every adopted branch a stop 20-35% below its adoption
    price, and a branch that falls that far will be sold by the grid's own
    sell path. Off by default, and the caller is expected to have established
    what it would sell at today's prices first.

    Logged at WARNING either way, with the count of branches it changes the
    answer for, because "a switch was thrown" is the single most useful line
    in a log after something sells.
    """
    import adaptive_stop
    async with get_session_factory()() as db:
        row = (await db.execute(select(TradingBotState).where(
            TradingBotState.bot_name == ADOPTED_STOP_MODE_KEY))).scalar_one_or_none()
        if row is None:
            db.add(TradingBotState(bot_name=ADOPTED_STOP_MODE_KEY,
                                   base_capital=1.0 if enabled else 0.0))
        else:
            row.base_capital = 1.0 if enabled else 0.0
        await db.commit()

    env = os.getenv(adaptive_stop.ADOPTED_MODE_ENV)
    _overridden = bool(env is not None and str(env).strip())
    log.warning(
        f"[GRID] adopted catastrophe stop set to {'ARM' if enabled else 'OFF'} in the "
        f"database"
        + (f" - BUT {adaptive_stop.ADOPTED_MODE_ENV}={env!r} is set and WINS, so the "
           f"effective mode is unchanged" if _overridden else
           f" - adopted branches now have a stop 20-35% below their adoption price"
           if enabled else " - adopted branches have NO stop again"))


async def products_without_a_grid_stop() -> dict:
    """Tickers whose branch has NO grid stop at all. {TICKER: why}.

    WHY THIS EXISTS. resting_stops declines every asset a grid branch holds
    slices on, and said so with "The branch carries its own adaptive stop;
    this would be a second one the first cannot see." True for most branches.
    False for an adopted one, which names its own stop of 0 - see
    _reported_stop, whose docstring is the rule: a safety figure that
    disagrees with the code enforcing it is worse than no figure.

    Measured on the live fleet 2026-09-29: eight branches holding $3,712 with
    stop_pct 0.0, each cited as covered by a layer that was citing it back,
    and /resting-stops reporting protects_usd 0.

    So the other layer stops assuming and asks. This does not decide anything
    and cannot cause an order - it only lets a refusal say which case it is,
    and lets the gap be counted.

    FAILS OPEN, in the direction that changes nothing: an unreadable answer
    is an EMPTY dict, so a caller falls back to the wording it always used.
    A caller must therefore treat empty as UNKNOWN and never as "everything
    has a stop" - the same rule that makes an unreadable balance never $0.00.

    Only the branch override is read, plus the two switches that can turn a
    stop off wholesale. The override is the one case that is certain:
    _reported_stop short-circuits on `override is not None` before adaptive
    or fixed is consulted, so an override of exactly 0 means no stop from
    that path. The global fixed stop being 0 is its own condition, because
    that disables the stop for every branch with no override - the whole
    fleet.

    AN ARMED ADOPTED STOP IS NOT A GAP. When GRID_ADOPTED_STOP_MODE=arm, an
    override of 0 no longer means no stop: adaptive_stop.adopted_stop gives
    the branch a wide catastrophe trigger instead. Those branches are left
    out of this dict rather than reported, because they have a stop POLICY.
    Whether a given cycle can size it depends on a volatility read this
    function does not do - and when that read fails, the cycle logs
    "🚨 NO GRID STOP" and _reported_stop shows 0.0 with the reason, so the
    per-branch figure still tells the truth even though this summary cannot.
    """
    try:
        async with get_session_factory()() as db:
            branches = (await db.execute(select(CryptoGridBranch))).scalars().all()
    except Exception as exc:
        log.warning(f"[GRID] stop coverage unreadable ({exc}) - reporting UNKNOWN, "
                    f"not 'covered'")
        return {}

    # Read once, not per branch. Fails toward REPORTING the gap: if the
    # policy cannot be read we do not know it is armed, and a coverage report
    # must not go quiet on a read error.
    _adopted_armed = False
    try:
        import adaptive_stop
        # adopted_stop_mode(), not adopted_mode() - the switch now lives in the
        # database as well as the environment, and reading only the env would
        # report a branch as uncovered while the cycle is stopping it.
        _adopted_armed = (await adopted_stop_mode()) == adaptive_stop.ADOPTED_MODE_ARM
    except Exception as exc:
        log.warning(f"[GRID] adopted-stop policy unreadable ({exc}) - treating those "
                    f"branches as uncovered, which is the direction that reports")

    out = {}
    for b in branches:
        if not b.product_id:
            continue
        ticker = b.product_id.split("-")[0].upper()
        override = getattr(b, "stop_loss_pct_override", None)
        why = None
        if override is not None:
            try:
                if float(override) == 0 and not _adopted_armed:
                    why = (f"{b.bot_name} names its own stop of 0, so no grid stop can "
                           f"fire on {b.product_id} at any price")
            except (TypeError, ValueError):
                # Unreadable override -> the branch falls back to the fixed
                # stop, which is a stop. Not a gap; _reported_stop agrees.
                why = None
        elif GRID_STOP_LOSS_PCT == 0:
            why = (f"{b.bot_name} has no override and GRID_STOP_LOSS_PCT is 0, so the "
                   f"fleet-wide grid stop is switched off")
        if why:
            out[ticker] = why
    return out


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

    # One batched top-of-book read for every product; the old per-coin
    # candle download is kept only as a fallback for anything the batch
    # did not price, so a missing quote is retried rather than guessed.
    async with engine.aiohttp.ClientSession() as session:
        try:
            live_prices = await engine.get_mid_prices(session, needed_products)
        except Exception as e:
            log.warning(f"[GRID] batched pricing failed, falling back per coin: {e}")
            live_prices = {}
        for product_id in needed_products:
            if live_prices.get(product_id) is None:
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

# A PARKED BRANCH HAS NO SPACING LEFT TO PROTECT.
#
# The sell attempt is gated on price >= reference_price * (1 + grid_pct).
# That spacing exists so a branch does not sell a rung it is about to rebuy
# a step lower - it is the geometry of a grid that CYCLES.
#
# A branch holding as many slices as it has levels cannot buy at all
# (run_grid_branch_cycle only buys when len(slices) < num_levels). There is
# no rebuy to space away from, so the gate is guarding a mechanism that is
# not running - and it blocks the only thing such a branch CAN do, which is
# sell into strength.
#
# Measured on the live fleet the day this was added: $5,821.51 - 78% of all
# allocated capital - sat in branches that could neither buy (full) nor sell
# (the reference gate wanted another 2.8% to 5.7%). Four of them, holding
# 71% of the capital, had never completed a single round trip. The branches
# still earning were the small ones that had room to cycle.
#
# So a parked branch may sell on its own merit instead of on the grid's
# geometry. Two things keep that honest:
#
#   _pick_profitable_slice_to_sell still decides WHICH slice, and it refuses
#   any sale that is not net-positive after real fees. This cannot realize a
#   loss; it is not a second path around that function.
#
#   The floor below is a REAL margin, not the bare > 0 that function needs.
#   1.0% net of fees, against a measured fee floor of 0.90% and the horizon
#   study's 1.37% round trip - which clears within 2h on 32.7% of entries
#   and within 6h on 54.3%. Selling at +$0.01 net would be churn wearing a
#   profit's name.
#
# This is NOT the spacing being loosened to manufacture trades. A branch
# that can still buy is untouched, and keeps the full grid_pct gate.
GRID_PARKED_MIN_NET_PCT = float(os.getenv("GRID_PARKED_MIN_NET_PCT", "0.010"))

# ---- WHAT YOU ACTUALLY PAID, FOR COIN THE GRID DID NOT BUY ----------------
# An adopted slice's entry_price is the market price on the day
# coin_adoption wrote it (see coin_adoption.slice_units - every slice at the
# same moment at the same price), NOT what the account paid. For ZEC that
# recorded $1,655 against a real average cost of $1,013.80: a basis overstated
# by 63%, $641.20 a coin.
#
# That is defensible for attributing what the GRID earned, and it is wrong for
# the sell gate, which asks a different question - "is this position actually
# in profit" - and gets a 19% loss on a holding that is up 32%. A branch full
# on its rungs can only leave by the parked route, and the parked route reads
# the slice's own basis, so an inflated basis strands a real winner forever.
# ZEC: six slices, zero closed trades, waiting on a price it does not need.
#
# The venue's API does not expose an average cost - the retail app computes it
# from a transaction history that includes buys made outside Advanced Trade -
# so this cannot be fetched and is NOT guessed. It is declared, per product,
# by the account owner reading it off that app:
#
#     GRID_TRUE_COST_BASIS="ZEC-USD:1013.80,HBAR-USD:0.0987"
#
# EMPTY BY DEFAULT. An unlisted product keeps today's behaviour exactly. It
# never relaxes the parked floor and never touches a slice the grid itself
# bought - that one carries a basis it really paid.
#
# IT DOES PRICE THE SALE IT AUTHORISES. This said "never changes a recorded
# P&L", which sounded like the conservative choice and was not one: the gate
# cleared two ZEC slices at +30.95% on the declared basis while the ledger
# booked them at -$149.93 against the adoption mark, and allocated_usd - which
# moves by that pnl and nothing else - lost the same $149.93 of working
# capital. A basis trusted to decide a sale and distrusted to price it leaves
# two contradictory numbers for one event and persists the wrong one. See the
# booking site in run_grid_branch_cycle().
def _parse_true_cost_basis(raw: str) -> dict:
    out = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        pid, _, val = part.partition(":")
        try:
            v = float(val)
        except (TypeError, ValueError):
            continue
        pid = pid.strip().upper()
        # A blank product id would sit in the map matching nothing while
        # hiding a typo that was meant to declare a real coin.
        if pid and v > 0:
            out[pid] = v
    return out


# The exit reason written when a declared true cost basis is what allowed an
# adopted slice to sell. Kept out of the grid's own performance record - see
# the note at the assignment site and get_grid_performance_metrics().
ADOPTED_EXIT_REASON = "adopted_exit"

GRID_TRUE_COST_BASIS = _parse_true_cost_basis(os.getenv("GRID_TRUE_COST_BASIS", ""))


def true_cost_basis_for(product_id) -> float:
    """What the account really paid per unit, when declared. None otherwise -
    and None must leave the caller on its existing path, never on a guess."""
    if not product_id:
        return None
    return GRID_TRUE_COST_BASIS.get(str(product_id).strip().upper())


def sell_basis_for_slice(slice_row, product_id=None):
    """The price a SELL decision should measure this slice against.

    The declared true cost, but only for an ADOPTED slice whose recorded
    entry never was a purchase price. A slice the grid bought keeps its own
    real entry - it paid that, and overriding it would be inventing a basis.
    """
    entry = getattr(slice_row, "entry_price", None)
    if not slice_paid_no_entry_fee(slice_row):
        return entry
    pid = product_id or getattr(slice_row, "product_id", None)
    true_basis = true_cost_basis_for(pid)
    return true_basis if true_basis is not None else entry

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

    # THE OFF SWITCH MUST MEAN OFF ON EVERY PATH.
    #
    # run_grid_auto_rotate_sweep and rebalance_flat_grid_branches_now both
    # check is_grid_auto_rotate_active() before they call this. The MAIN
    # CYCLE does not: run_grid_branch_cycle calls this with after_sale=True
    # whenever a sale empties a branch to flat, and that path reached the
    # env var alone - which DEFAULTS TO ON when unset.
    #
    # So the dashboard could report auto_rotate_active=False, the operator
    # could be told rotation was off, and a branch that sold its last slice
    # would still be re-pointed onto another coin. On 2026-09-25 rotation
    # retired four EARNING branches and left 64% of the account idle; the
    # switch that exists to prevent a repeat has to hold on the path that
    # actually fires most often.
    if not await is_grid_auto_rotate_active():
        log.info(f"[GRID] auto-rotate declined {branch.bot_name}: the fleet switch is OFF "
                 f"(is_grid_auto_rotate_active). Nothing is re-pointed while it stays off, "
                 f"on this path or any other.")
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
        fill = await grid_buy(session, spend, branch.product_id, branch.bot_name)

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
    def _money(v):
        """-$381.47, never $-381.47. A minus wedged after the currency
        symbol reads as part of the amount, and on this panel the one
        figure that most needed to be legible was the negative one."""
        try:
            v = float(v)
        except (TypeError, ValueError):
            return "unknown"
        return ("-$" if v < 0 else "$") + f"{abs(v):,.2f}"

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
    # ...and the same is true ONE LEVEL DOWN, which this function got wrong
    # until 2026-09-26. Summing allocated_usd over branches that hold a
    # slice is still an earmark: a 3-level branch with one slice open has
    # spent a THIRD of its allocation on coin and is still holding the
    # rest as cash. On the live fleet that read "$276.92 genuinely in
    # coin" when the real figure was $93.07 - the panel built to stop an
    # earmark being reported as an investment was doing it itself.
    #
    # The coin is what the slices cost: entry_price x qty. One definition,
    # shared with allocation_backing so the two can never drift apart.
    try:
        import allocation_backing
        _slice_cost = allocation_backing.slice_cost
    except Exception:
        def _slice_cost(sl):
            try:
                return float(sl.get("entry_price")) * float(sl.get("qty"))
            except (TypeError, ValueError, AttributeError):
                return None

    deployed = 0.0
    unpriced_slices = 0
    for b in branches:
        for sl in (b.get("slices") or []):
            cost = _slice_cost(sl)
            if cost is None:
                unpriced_slices += 1
            else:
                deployed += cost

    # What those same branches have EARMARKED, which is the bigger number
    # and the one that was being printed as coin. Kept and labelled rather
    # than dropped - the gap between the two is the point.
    holding_branches = [b for b in branches if b.get("open_slices")]
    earmarked_behind_slices = sum((b.get("allocated_usd") or 0.0) for b in holding_branches)
    earmarked_idle = max(0.0, allocated - deployed)

    # free_cash goes NEGATIVE when branch reserves exceed the wallet. Adding
    # a negative to an earmark produced "-$104.55 sitting in cash", a
    # composite of two real numbers that describes nothing. Idle cash is
    # only meaningful when there is cash.
    total_idle = earmarked_idle + max(0.0, free_cash or 0.0)

    findings = []

    if deployed <= 0:
        findings.append({
            "kind": "nothing_deployed",
            "usd": round(total_idle, 2),
            "severity": "warn",
            "action": None,
            "detail": (f"Every branch is flat, so nothing is invested in any coin right now. "
                       f"{_money(allocated)} is EARMARKED to branches and {_money(free_cash or 0.0)} is "
                       f"loose - but all {_money(total_idle)} of it is sitting in the USD wallet "
                       f"earning nothing until a dip triggers a buy."),
            "basis": "branches holding zero open slices hold zero coin - an earmark is not an investment",
        })
    else:
        findings.append({
            "kind": "deployed",
            "usd": round(deployed, 2),
            "severity": "ok",
            "action": None,
            "detail": (f"{_money(deployed)} is genuinely in coin across "
                       f"{len(holding_branches)} branch(es) - that is what the open slices "
                       f"cost, entry price times quantity. Those same branches have "
                       f"{_money(earmarked_behind_slices)} earmarked, so "
                       f"{_money(earmarked_behind_slices - deployed)} of their own allocation "
                       f"is still unspent cash waiting on the next level."
                       + (f" {unpriced_slices} slice(s) could not be priced and are not counted."
                          if unpriced_slices else "")),
            "basis": "entry_price x qty on every open slice - NOT the allocation behind them",
        })

    # ── cash above the reserve ──────────────────────────────────────────
    if free_cash is None:
        findings.append({
            "kind": "cash_unknown", "usd": None, "action": None, "severity": "warn",
            "detail": "The real Coinbase balance could not be read, so idle cash cannot be judged.",
            "basis": "unknown cash is never treated as deployable",
        })
    elif free_cash < 0:
        # Not an idle-cash question at all. The branches have reserved more
        # than the wallet holds, and the old code ran this through the
        # "committed" branch, which printed a calm green tick over it.
        findings.append({
            "kind": "cash_overdrawn",
            "usd": round(free_cash, 2),
            "severity": "bad",
            "action": None,
            "detail": (f"Branch reserves exceed the wallet by {_money(abs(free_cash))}. This is not "
                       f"idle cash and it is not a reserve - it is {_money(allocated)} earmarked "
                       f"against money that is not in the account. Nothing can be deployed until "
                       f"real cash covers it; new buys are refused by the fee reserve until then."),
            "basis": "real wallet balance minus branch reserves - a negative means the earmark is unfunded",
        })
    else:
        deployable = free_cash - GRID_CASH_RESERVE_USD
        if deployable >= GRID_AUTO_DEPLOY_AMOUNT_USD:
            findings.append({
                "kind": "idle_cash", "usd": round(deployable, 2), "severity": "warn",
                "action": "/grid-status/spread-evenly",
                "detail": (f"{_money(deployable)} sits above the {_money(GRID_CASH_RESERVE_USD)} reserve - "
                           f"enough for at least one more {_money(GRID_AUTO_DEPLOY_AMOUNT_USD)} branch."),
                "basis": "free cash minus the reserve that funds open branches' remaining levels",
            })
        else:
            findings.append({
                "kind": "cash_committed", "usd": round(max(0.0, deployable), 2),
                "severity": "ok",
                "action": None,
                "detail": (f"{_money(free_cash)} is loose, but {_money(GRID_CASH_RESERVE_USD)} of it is the "
                           f"reserve backing open branches' remaining levels, leaving "
                           f"{_money(max(0.0, deployable))} spare - under the "
                           f"{_money(GRID_AUTO_DEPLOY_AMOUNT_USD)} a new branch needs. Committed is not the "
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
        # COIN, at what the slices cost. Not the allocation behind them.
        "deployed_usd": round(deployed, 2),
        "earmarked_behind_slices_usd": round(earmarked_behind_slices, 2),
        "unpriced_slices": unpriced_slices,
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


def _safe_json(d):
    """Serialise a diagnostic blob, or return None. Never raises.

    Telemetry must not be able to break a trade. A gate detail containing
    something unserialisable is a logging problem; the fill already
    happened and the slice row still has to be written.
    """
    try:
        return json.dumps(d, default=str, sort_keys=True) if d else None
    except Exception:
        return None


async def _log_grid_trade(bot_name, product_id, entry_price, exit_price, qty, pnl, opened_at,
                          entry_expected_price=None, exit_expected_price=None,
                          exit_reason=None, mae_pct=None, mfe_pct=None,
                          entry_atr_pct=None, stop_pct=None, entry_spread_pct=None,
                          entry_gate_json=None):
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
                # Instrumentation for settling the stop level later on real
                # entries. All nullable - a trade that predates this, or one
                # whose excursion tracking failed, records None rather than a
                # zero that would read as "never moved".
                exit_reason=exit_reason, mae_pct=mae_pct, mfe_pct=mfe_pct,
                entry_atr_pct=entry_atr_pct, stop_pct=stop_pct,
                entry_spread_pct=entry_spread_pct, entry_gate_json=entry_gate_json,
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
    slice shares the one flat rate, exactly as before.

    An ADOPTED slice is the exception on both paths: it pays the exit leg
    only, because its basis was written by bookkeeping and never cost a
    commission. See slice_paid_no_entry_fee()."""
    if exit_leg_rate is None:
        # The flat constant prices BOTH legs, so a slice that never paid
        # an entry leg carries half of it. None stays None - that is the
        # caller's "no rate supplied", not a rate of zero, and
        # _grid_slice_net_pnl has its own conservative fallback for it.
        if round_trip_fee_rate is not None and slice_paid_no_entry_fee(s):
            return round_trip_fee_rate / 2.0
        return round_trip_fee_rate
    if slice_paid_no_entry_fee(s):
        # Exit leg only - see slice_paid_no_entry_fee().
        return exit_leg_rate
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


def _pick_parked_slice_to_sell(slices: list, price: float, round_trip_fee_rate: float = None,
                               exit_leg_rate: float = None, floor_pct: float = 0.0):
    """The BEST slice that clears the parked floor, not the first one in the
    book that happens to be positive.

    _pick_profitable_slice_to_sell above walks oldest-first and returns the
    first slice netting anything at all. For the grid's own rise trigger that
    is right: FIFO is the deliberate tie-break, and any profit clears a gate
    with no floor under it.

    The parked route asks a different question. It has a floor
    (GRID_PARKED_MIN_NET_PCT), and it is the ONLY way out of a branch that is
    full on its rungs - such a branch cannot buy, and will not sell below
    entry, which is the state that produced sixteen days of zero closes from
    2026-09-10. Handing that floor the first marginally-positive slice means
    an oldest slice at +0.1% masks a newer one at +3.0%: the gate refuses,
    no sale happens, and the branch stays locked with a qualifying slice
    sitting in it. The floor is not the thing that failed - it never got
    shown the slice that satisfied it.

    So here: consider every slice, keep only those that clear the floor on
    their own basis, and return the best of them. Nothing is loosened. A
    candidate must still net a real profit AND still clear the same floor;
    this only stops the branch from being judged by a slice that was never
    the one being offered. FIFO remains the tie-break between equals.

    Returns (slice, net_pct) or (None, None) when nothing qualifies.
    """
    best = None
    best_pct = None
    for s in slices:
        basis = (s.qty or 0) * (s.entry_price or 0)
        if basis <= 0:
            continue
        # A branch CAN still be parked on real slices while also holding a
        # remnant, and percentage does not care about size: BCH's dust read
        # +1.80% and beat both real slices, which were negative. Offering it
        # produced a sell the venue refused, 200 times. Size the candidate,
        # not just its percentage.
        if basis < GRID_DUST_SLICE_USD:
            continue
        # AN ADOPTED SLICE IS JUDGED AGAINST WHAT WAS ACTUALLY PAID, when the
        # owner has declared it. Its entry_price is an adoption-day market
        # price, not a purchase price, so measuring "may this sell at a
        # profit" against it answers the wrong question - see
        # GRID_TRUE_COST_BASIS. Unlisted products, and every slice the grid
        # bought itself, keep entry_price exactly as before.
        _sell_basis = sell_basis_for_slice(s, product_id=getattr(s, "product_id", None))
        _basis_usd = (s.qty or 0) * (_sell_basis or 0)
        if _basis_usd <= 0:
            continue
        net = _grid_slice_net_pnl(s.qty, _sell_basis, price,
                                  _slice_rate(s, round_trip_fee_rate, exit_leg_rate))
        pct = net / _basis_usd
        if pct >= floor_pct and (best_pct is None or pct > best_pct):
            best, best_pct = s, pct
    return best, best_pct


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


TRADING_PROFILE_KEY = "grid_trading_profile"


async def get_trading_profile() -> str:
    """Which gate set is in force. DB-persisted, defaults to guarded.

    Stored as a number on TradingBotState like every other toggle here
    (1.0 = aug2026, anything else = guarded) rather than as a string,
    because that is the column that exists and inventing a parallel
    settings table for one flag is how two sources of truth start.

    Never raises. An unreadable setting resolves to GUARDED - the direction
    that keeps the economic checks on, because the failure mode of
    fail-open here is money committed without its economics being checked.
    """
    import trading_profile
    try:
        async with get_session_factory()() as db:
            result = await db.execute(select(TradingBotState).where(
                TradingBotState.bot_name == TRADING_PROFILE_KEY))
            row = result.scalar_one_or_none()
            if row is None:
                return trading_profile.GUARDED
            return (trading_profile.AUG2026
                    if (row.base_capital and row.base_capital >= 1.0)
                    else trading_profile.GUARDED)
    except Exception as e:
        log.warning(f"[GRID] trading profile unreadable, staying guarded: "
                    f"{type(e).__name__}: {e}")
        return trading_profile.GUARDED


async def set_trading_profile(name: str, budget_usd: float = 200.0,
                              days: int = 14) -> str:
    """Switch the gate set, durably. Returns the profile actually stored.

    Turning the gates OFF always opens a budgeted experiment - there is
    no code path that removes the economic checks without something
    watching the cost.
    """
    import trading_profile
    p = trading_profile.normalise(name)
    async with get_session_factory()() as db:
        result = await db.execute(select(TradingBotState).where(
            TradingBotState.bot_name == TRADING_PROFILE_KEY))
        row = result.scalar_one_or_none()
        if row is None:
            row = TradingBotState(bot_name=TRADING_PROFILE_KEY,
                                  base_capital=0.0, starting_capital=0.0)
            db.add(row)
        row.base_capital = 1.0 if p == trading_profile.AUG2026 else 0.0
        await db.commit()

    # THE GATES NEVER COME OFF WITHOUT A BUDGET AND A DEADLINE.
    #
    # An open-ended "gates off" is how a wash becomes a real loss with no
    # single moment where anyone chose that. Switching to aug2026 opens an
    # experiment row; experiment_worker ends it on whichever of the two
    # arrives first and flips this back. Switching to guarded closes any
    # running one, so a manual revert is recorded rather than leaving a
    # row that looks live forever.
    try:
        await _record_profile_experiment(p, budget_usd, days)
    except Exception as e:
        # A failure to open the budget row must not leave the gates OFF
        # with nothing watching them.
        if p == trading_profile.AUG2026:
            async with get_session_factory()() as db:
                r2 = (await db.execute(select(TradingBotState).where(
                    TradingBotState.bot_name == TRADING_PROFILE_KEY))).scalar_one_or_none()
                if r2 is not None:
                    r2.base_capital = 0.0
                    await db.commit()
            log.error(f"[GRID] could not open the experiment budget "
                      f"({type(e).__name__}: {e}) - refusing to leave the gates "
                      f"off unguarded, profile stays GUARDED")
            return trading_profile.GUARDED
        log.warning(f"[GRID] experiment row not updated: {type(e).__name__}: {e}")

    log.warning(f"[GRID] TRADING PROFILE set to {p.upper()} - "
                f"economic gates {'OFF' if p == trading_profile.AUG2026 else 'ON'}")
    return p


async def _record_profile_experiment(profile: str, budget_usd: float, days: int):
    """Open a budgeted experiment on aug2026; close any running one on guarded."""
    import trading_profile
    from datetime import timedelta
    from models import CryptoCoinTradeHistory, CryptoGridTradeHistory, TradingExperiment
    from sqlalchemy import func
    now = datetime.utcnow()
    async with get_session_factory()() as db:
        running = (await db.execute(
            select(TradingExperiment)
            .where(TradingExperiment.ended_at == None)       # noqa: E711
            .order_by(TradingExperiment.id.desc())
            .limit(1))).scalar_one_or_none()

        if profile != trading_profile.AUG2026:
            if running is not None:
                running.ended_at = now
                running.ended_reason = "MANUAL"
                await db.commit()
            return

        if running is not None:
            return                       # already budgeted; do not reset the clock

        tree = (await db.execute(select(func.sum(CryptoCoinTradeHistory.pnl)))).scalar()
        grid = (await db.execute(select(func.sum(CryptoGridTradeHistory.pnl)))).scalar()
        db.add(TradingExperiment(
            profile=profile, started_at=now,
            budget_usd=float(budget_usd),
            deadline_at=now + timedelta(days=int(days)),
            baseline_realized_pnl=float(tree or 0.0) + float(grid or 0.0),
            blind_checks=0))
        await db.commit()
    log.warning(f"[GRID] experiment opened: ${budget_usd:,.2f} budget, "
                f"{days} days")


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


# How recently a repeating condition was written to the shared feed, keyed by
# (verdict, product). The feed is a fixed-size window, so an event that
# repeats every cycle does not just add noise - it EVICTS. Measured twice
# tonight: PARKED_NO_EXIT held 32 of 40 rows, and once that was removed
# PARKED_SELL/PARKED_SELL_NOFILL from two venue-refused products took all 40
# between them. Either way GATE_PASS, GATE_BLOCK and CYCLE_ERROR were gone,
# and CYCLE_ERROR is the only place a LOST FILL can be seen.
#
# So a condition that is still true keeps its first row and stops writing
# for a while. The first occurrence is the news; the hundredth is the thing
# that hides everything else.
_FEED_LAST_WRITE = {}
FEED_REPEAT_SECONDS = 900.0


def _feed_should_write(verdict: str, product_id: str, now: float = None) -> bool:
    """True when this (verdict, product) has not been written recently.

    Never raises: this guards instrumentation, and instrumentation must not
    be able to stop a trade. An unreadable clock falls through to writing,
    because losing a row is worse than writing one too many.
    """
    try:
        now = time.time() if now is None else now
        key = (verdict, product_id)
        last = _FEED_LAST_WRITE.get(key)
        if last is not None and (now - last) < FEED_REPEAT_SECONDS:
            return False
        _FEED_LAST_WRITE[key] = now
        return True
    except Exception:
        return True


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
                            bot_name: str = "grid", detail_out: dict = None):
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
    # THE PROFILE CAN TURN THIS OFF; IT CANNOT TURN OFF A STOP.
    # aug2026 reproduces the trading rate of the window that actually
    # traded, and this is one of exactly two gates it reaches. The reserve
    # gate above it, the adaptive stops and maker-only are not switchable
    # from there by design - see trading_profile.ALWAYS_ON.
    import trading_profile as _tp
    _profile = await get_trading_profile()
    if not _tp.net_edge_gate_enabled(_profile):
        # RECORDED, for the same reason the DB toggle below is, and it was
        # missed when that one was fixed: this path also returns True, so the
        # buy goes in with no economic check. Writing nothing made a whole
        # profile's worth of real buys invisible to every counter that reads
        # gate verdicts - including the fill rate, which counts attempts from
        # the verdicts that allowed them and so could not count these at all.
        _reason = (f"profile {_profile}: economic gates off - ${slice_usd:,.2f} buy "
                   f"allowed with NO economic check")
        await _record_gate_decision(bot_name, product_id, "GATE_DISABLED", _reason)
        return True, f"profile {_profile}: economic gates off"
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
        # The gate already measured the live spread against the real book.
        # It was being discarded, so the one moment the system knows what a
        # trade's spread actually was - the instant before the order - was
        # lost, and the closed ledger could never answer whether wide-spread
        # entries underperform. Handed back through an out-dict rather than
        # a changed return signature, the same pattern fetch_candles_window
        # already uses for last_error_out.
        if detail_out is not None and isinstance(_detail, dict):
            detail_out.update(_detail)
            # The row dict carries spread, net edge, adverse selection and the
            # target, but not the fee this gate actually priced against - that
            # is computed here (maker vs taker, maker-only aware) and passed
            # IN. Without it the recorded diagnostic cannot be re-derived.
            detail_out["fee_round_trip_pct"] = fee_round_trip
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


def stop_report_line(bot_name: str, stop_pct: float, resolved, has_slices: bool):
    """(level, message) saying what stop an ADOPTED branch actually ended up with.

    Pulled out here and made pure because the version that lived inline asked
    about the slices before it asked about the stop:

        if stop_pct == 0 and slices:   -> NO GRID STOP
        elif resolved is not None:     -> ADOPTED STOP ARMED

    A branch at stop 0 with no open slices - JASMY, every cycle - missed the
    first arm on `slices` and took the second, so the log read "ADOPTED STOP
    ARMED" directly above a reason stating that GRID_ADOPTED_STOP_MODE is not
    'arm' and nothing sells it at any price. No trading effect, and exactly the
    defect class this file keeps being fixed for: a line that states the
    opposite of the truth.

    So the stop decides WHAT is reported and the slices decide only how loud an
    absence is. Nothing in here reads bot state - the caller passes the three
    facts, which is what makes it checkable.
    """
    if stop_pct > 0:
        if resolved is not None:
            return "warning", (f"[GRID] {bot_name}: ADOPTED STOP ARMED - "
                               f"{resolved['reason']}")
        return "info", (f"[GRID] {bot_name}: stop - branch override "
                        f"{stop_pct * 100:.1f}%")
    why = ((resolved or {}).get("reason")
           or ("this branch names its own stop of 0. No portfolio cover is "
               "claimed here - check /resting-stops, where an asset under "
               "`uncovered` has a stop from neither layer."))
    if has_slices:
        # Open slices with no trigger at any price. This is live exposure.
        return "warning", f"[GRID] {bot_name}: 🚨 NO GRID STOP - {why}"
    # No stop, and nothing held to stop out. Said quietly, because it becomes
    # the line above the moment a slice opens.
    return "info", (f"[GRID] {bot_name}: no stop, and no open slice to need one "
                    f"yet - {why}")


def current_cycle_id() -> str:
    """An identifier for THIS pass of the fleet loop, to the second.

    §1 asks every slice to record the cycle it belongs to. Cycles are 30
    seconds apart, so a UTC timestamp to the second cannot collide with the
    next one, and unlike a random token it can be read by a person and
    lined up against a log.

    NOT an idempotency key. order_idempotency's key needs an `attempt` the
    CALLER controls, because a next-cycle retry of the same intent gets a
    different cycle id and would not be deduplicated - see the duplicate
    path finding. This answers "which pass opened this slice", nothing more.
    """
    return datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")


async def run_grid_branch_cycle(session, branch: CryptoGridBranch, cycle_id: str = None):
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

    # Volatility for the stop, over a window long enough to mean something
    # and cached so this does not re-pull 30 days of candles every cycle.
    # Deliberately NOT the ~25-candle series the price came from: measured
    # on the live fleet that window read 3-4x lower than 7, 30 and 60-day
    # windows, which agree with each other, and every stop scaled from it
    # came out TIGHTER - a stop that shrinks because yesterday was calm.
    _stop_vol = None
    try:
        import adaptive_stop as _as
        _stop_vol = await _as.measure_daily_vol(session, branch.product_id)
    except Exception as exc:
        log.info(f"[GRID] {branch.bot_name}: no volatility for the stop ({exc}) - "
                 f"the fixed stop stands")

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
        # An exempt coin keeps its own allocation-derived count: the override's
        # ceiling simply does not apply to it. Still bounded by what its real
        # capital supports, so this can only ever restore levels the branch
        # would have had with no override at all - never invent new ones.
        if branch_is_level_cap_exempt(branch.product_id):
            real_effective_levels = _safe_num_levels_for_allocation(branch.allocated_usd)
        # ---- ADOPTED HEADROOM ----------------------------------------
        # An adopted branch is written FULL on purpose: coin_adoption_worker
        # sets num_levels to the slice count so the grid cannot double down
        # on a position it never chose. The cost of that, measured on the
        # live fleet, is eleven branches holding $6,576 of coin that can
        # neither buy a dip (full) nor sell (underwater) - the grid half of
        # buy-dip/sell-rise switched off, which is a hold, not a grid.
        #
        # ONE rung, not a reopened ladder. The branch may take a single dip;
        # the slice it buys is a real one, so branch_is_adopted_only() goes
        # false on the next cycle and this clamp snaps back - it must sell
        # before it may buy again. That is the deliberate safety of the
        # original decision kept, with the deadlock removed.
        #
        # The rung is sized by adopted_rung_usd(), NOT by allocated_usd /
        # num_levels. Read that docstring before touching this: raising the
        # level count without the sizing change is what would have let ZEC
        # spend 68.5% of the wallet averaging down its own worst position.
        if branch_is_adopted_only(slices):
            real_effective_levels = max(real_effective_levels, len(slices) + 1)
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
        # The branch's own learned multiplier, admitted as a FLOOR only. See
        # SELF_TUNE_WIDENS_FIXED_SPACING. _widen is None unless this branch has
        # actually been widened above the validated default by its own poor
        # real record, and max() means the step can never move narrower.
        _widen = (self_tune_widening_multiplier(branch)
                  if SELF_TUNE_WIDENS_FIXED_SPACING else None)
        if _widen is not None:
            _swing_pct, _avg = await compute_avg_swing_grid_pct(
                session, branch.product_id, multiplier=_widen)
            if _swing_pct is not None and _swing_pct > new_grid_pct:
                spacing_log_note = (
                    f"{spacing_log_note}, WIDENED to {_swing_pct*100:.3f}% by this "
                    f"branch's own self-tuned {_widen}x on a {(_avg or 0)*100:.3f}% "
                    f"avg swing - its own last trades asked for more room")
                new_grid_pct = max(new_grid_pct, _swing_pct)
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

    # THE SWING CEILING. The net-edge gate refuses any target over 3.0x a
    # coin's own average hourly swing - "would sit unfilled" - so a step above
    # that does not trade WIDER, it does not trade AT ALL.
    #
    # The fleet minimum is one-directional and knew nothing about this. Found
    # in the live activity feed 2026-09-28: every one of the last 40 events
    # was a GATE_BLOCK, and SOL-USD's read "target is 3.0x the 0.98% hourly
    # swing, over the 3.0x limit". Raising the fleet minimum 2.50% -> 3.00%
    # put it over: at 2.50% it was 2.55x and passing. That regression is mine.
    #
    # Measured across all 20 live coins, the ceiling binds on three:
    #     BTC   0.56% swing -> 1.68% ceiling   (blocked at 2.50% too)
    #     ETH   0.75% swing -> 2.25% ceiling   (blocked at 2.50% too)
    #     SOL   0.98% swing -> 2.94% ceiling   (blocked by my change)
    # $597 of capital that cannot place a buy at any price. BTC and ETH were
    # already stranded before this, so the cap fixes more than it repairs.
    #
    # THIS IS NOT LOWERING A THRESHOLD TO MANUFACTURE ACTIVITY. The fee floor
    # below is untouched and still guarantees every completed trip clears its
    # costs. The swing multiple is not a safety limit, it is a statement about
    # whether an order will ever be FILLED - and the floor still wins where
    # the two disagree, which correctly leaves a coin refusing rather than
    # trading at a loss.
    if _swing_ceiling_enabled():
        try:
            _swing_pct = await engine.get_average_hourly_swing_pct(session, branch.product_id)
        except Exception:
            _swing_pct = None
        if _swing_pct and _swing_pct > 0:
            _ceiling = SWING_CEILING_MULTIPLE * float(_swing_pct)
            _step_now = new_grid_pct if new_grid_pct is not None else branch.grid_pct
            _hard_floor = await fee_safe_floor_pct()
            if _step_now > _ceiling + 1e-9:
                if _ceiling >= _hard_floor:
                    new_grid_pct = _ceiling
                    spacing_log_note = (
                        f"{spacing_log_note or 'spacing'} (capped {_step_now*100:.2f}% -> "
                        f"{_ceiling*100:.2f}%, {SWING_CEILING_MULTIPLE:.1f}x the "
                        f"{_swing_pct*100:.2f}% hourly swing - above it the gate refuses "
                        f"every buy)")
                else:
                    # The coin cannot both clear its fees and fill its orders.
                    # That is an answer, not a failure: the floor holds and the
                    # gate keeps refusing, which is the correct outcome.
                    log.info(
                        f"[GRID] {branch.bot_name}: {branch.product_id} swing ceiling "
                        f"{_ceiling*100:.2f}% is under the {_hard_floor*100:.2f}% fee floor - "
                        f"too quiet to grid profitably. Leaving the floor in force.")

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
    # A branch may be paused for either of two reasons, and they are not
    # the same fact: the breaker fires on its own drawdown, while
    # buys_paused is set deliberately at adoption so an overweight
    # position walks DOWN through strength instead of being bought back.
    _sell_only = bool(getattr(branch, "buys_paused", False))
    if _sell_only and not drawdown_breached:
        log.info(
            f"[GRID] {branch.bot_name}: SELL-ONLY - this branch may sell its slices but "
            f"never buy more. Set at adoption because the position was over the 20% rule, "
            f"so every sale banks profit AND reduces the concentration."
        )
    if drawdown_breached or _sell_only:
        # Only the breaker gets the drawdown message. A sell-only branch
        # at a fresh peak reading "equity is down 0% from its peak" would
        # be an alarm about nothing.
        if drawdown_breached:
            log.info(
                f"[GRID] {branch.bot_name}: 🛑 real equity ${equity:.2f} is down {drawdown_pct*100:.0f}% from its own "
                f"${stored_peak_equity:,.2f} peak (breaker at {GRID_DRAWDOWN_BREAKER_PCT*100:.0f}%) - new buys paused, "
                f"existing slices still sell normally"
            )
    elif (price <= branch.reference_price * (1 - grid_pct)
          and len(tradeable_slices(slices)) < branch.num_levels):
        real_balance, real_balance_err = await engine.get_usd_balance(session)
        if real_balance is None:
            log.warning(f"[GRID] {branch.bot_name}: real balance unavailable ({real_balance_err}) - skipping this cycle")
            return
        if branch_is_adopted_only(slices):
            # allocated_usd here is coin the account already owned, not a
            # cash budget - see adopted_rung_usd().
            slice_usd, rung_reason = adopted_rung_usd(
                real_balance - max(0.0, GRID_CASH_RESERVE_USD),
                await _adopted_branch_count())
            if slice_usd <= 0:
                log.info(f"[GRID] {branch.bot_name}: no adopted rung - {rung_reason}")
                await _record_gate_decision(branch.bot_name, branch.product_id,
                                            "ADOPTED_RUNG", rung_reason)
                return
        else:
            slice_usd = branch.allocated_usd / branch.num_levels
        _dep_reserve, _dep_why = await unfunded_deployment_reserve()
        spend, spend_reason = spendable_for_slice(slice_usd, real_balance,
                                                  deployment_reserve=_dep_reserve)
        if spend <= 0:
            log.info(f"[GRID] {branch.bot_name}: no buy - {spend_reason}")
            await _record_gate_decision(branch.bot_name, branch.product_id, "CASH_RESERVE",
                                        spend_reason)
            return

        # The owner's own standing ceiling: no coin over 20% of the fleet.
        # One-directional - it can only ever refuse a new buy, never sell
        # or trim - and it fails OPEN when the account can't be read.
        #
        # Measured against the WHOLE ACCOUNT at market value, which is
        # the same book auto_trim uses. It used to read grid cost basis
        # over grid coin only, so the two answered one question eleven
        # points apart: ZEC 31.00% here against 19.33% there.
        # A coin on the way out takes no new dollars, by NAME rather
        # than by a percentage that happens to be high. Moving the
        # ceiling to the account book dropped ZEC from 31.00% to 19.33%,
        # which removed the side effect that had been refusing ZEC buys
        # while the owner is trying to exit it. Refuse-only; an empty
        # exit list cannot bite. See crypto_grid_bot_exits.
        import crypto_grid_bot_exits
        _exit_ok, _exit_reason = crypto_grid_bot_exits.exit_verdict(branch.product_id)
        if not _exit_ok:
            log.info(f"[GRID] {branch.bot_name}: 🚪 exiting - {_exit_reason}")
            await _record_gate_decision(branch.bot_name, branch.product_id,
                                        "EXITING", _exit_reason)
            return

        import concentration_gate
        _conc_ok, _conc_reason = concentration_gate.concentration_verdict(
            branch.product_id, await account_market_book(), spend)
        if not _conc_ok:
            log.info(f"[GRID] {branch.bot_name}: 🧱 concentration ceiling - {_conc_reason}")
            await _record_gate_decision(branch.bot_name, branch.product_id,
                                        "CONCENTRATION", _conc_reason)
            return

        # DOES THIS BRANCH ACTUALLY HOLD WHAT ITS BOOKS CLAIM?
        #
        # The rung count that opened this gate judges a slice by its
        # dollar basis. An UNBACKED slice - one whose coin is not in the
        # wallet - carries a perfectly ordinary basis, so the rung count
        # is structurally blind to it, and on 2026-10-04 that blindness
        # put $45.60 into LINK at 37.045% backed. See
        # branch_backing_verdict for the full measurement.
        #
        # Refuse-only, and UNKNOWN is not a refusal: an unreadable
        # balance leaves the buy exactly where it was before this check
        # existed, rather than freezing the fleet on a rate limit.
        _back_ok, _back_reason = await branch_backing_verdict(
            branch.product_id, slices, price, await wallet_owned_units())
        if not _back_ok:
            log.warning(f"[GRID] {branch.bot_name}: 🧾 UNBACKED - no buy. {_back_reason}")
            await _record_gate_decision(branch.bot_name, branch.product_id,
                                        "UNBACKED", _back_reason)
            return

        # THE EXECUTION GATE. Called here so no buy path can reach the venue
        # without passing it - the failure this exists to prevent is a worker
        # doing `if branch.active: place_order()` and walking past the whole
        # safety layer, which is exactly how two coins reached 26% each.
        #
        # OBSERVE BY DEFAULT. There are 23 branches and no control rows yet, and
        # the gate correctly refuses a branch it has never reconciled - so
        # enforcing on the first deploy would halt the fleet. It records what it
        # would have blocked until EXECUTION_GATE_MODE=enforce.
        #
        # Its own failure is never a reason to trade. An exception here returns
        # without buying, because an un-auditable buy is the one nobody can
        # reconstruct afterwards.
        try:
            import branch_audit_service as _bas
            from database import get_session_factory as _gsf
            async with _gsf()() as _asess:
                _svc = _bas.BranchAuditService(_asess)
                await _bas.ensure_control_state(
                    _asess, bot_name=branch.bot_name, branch_id=branch.id)
                _truth = _bas.ExchangeTruth(
                    readable=True, is_current=True, matched=True,
                    detail="grid cycle read the venue this pass")
                _d = await _bas.check_or_observe(
                    _bas.ExecutionGate(_svc), bot_name=branch.bot_name,
                    action="ENTRY", truth=_truth,
                    context={"spend_usd": spend, "product_id": branch.product_id})
                await _asess.commit()
            if not _d.allowed:
                log.info(f"[GRID] {branch.bot_name}: 🔒 execution gate - "
                         f"{_d.reason_code} ({_d.gate}): {_d.detail}")
                await _record_gate_decision(branch.bot_name, branch.product_id,
                                            "EXECUTION_GATE", _d.detail)
                return
        except Exception as _exc:
            log.warning(f"[GRID] {branch.bot_name}: execution gate unavailable "
                        f"({type(_exc).__name__}: {_exc}) - NOT buying. An "
                        f"un-auditable buy is refused, not allowed through.")
            return

        _gate_detail = {}
        gate_ok, gate_reason = await _net_edge_gate_ok(
            session, branch.product_id, grid_pct, spend, bot_name=branch.bot_name,
            detail_out=_gate_detail)
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
            import trading_profile as _tp
            memo = await grid_learning.check_before_buy(branch.product_id)
            if memo.get("trades"):
                log.info(f"[LEARN] {branch.bot_name}: {memo['lesson']}")
            if not memo.get("allow", True) and _tp.learning_veto_enabled(
                    await get_trading_profile()):
                log.warning(f"[GRID] {branch.bot_name}: 🧠 memory blocked this buy - {memo['lesson']}")
                await _log_activity_safe(branch.bot_name, branch.product_id, "LESSON_BLOCK",
                                         f"🧠 buy blocked by the fleet's own record: {memo['lesson']}")
                return
        except Exception as e:
            log.warning(f"[LEARN] {branch.bot_name}: memory unavailable ({e}) - trading anyway")

        _outcome = {}
        fill = await grid_buy(session, spend, branch.product_id, branch.bot_name,
                              outcome_out=_outcome)
        if not fill:
            # Durable, so rejections can be COUNTED. They were only ever
            # logged before, which meant "orders rejected" could not be
            # reported at all and an execution problem could hide behind a
            # normal-looking gate pass rate.
            #
            # Which is exactly why the label has to be right. This branch
            # used to write ORDER_REJECTED for ANY empty return, including
            # a maker order that simply was not taken inside its window -
            # routine under maker-only, and already recorded as an expiry
            # a few lines up in grid_buy. Live on 2026-09-28 all three
            # "rejections" in an hour filled successfully minutes later at
            # the same size (FLOKI $17.99 at 03:20 -> 03:26, SHIB $36.02 at
            # 03:03 -> 03:05, PEPE at 02:53 -> 02:58). Nothing had refused
            # them. A counter that fires on normal behaviour cannot be used
            # to detect abnormal behaviour, so a real venue rejection would
            # have been buried in expiries.
            import order_outcome
            _event, _msg = order_outcome.event_for(
                _outcome.get("cause"), spend,
                detail=_outcome.get("detail"),
                wait_seconds=_outcome.get("wait_seconds"))
            if order_outcome.is_execution_fault(_outcome.get("cause")):
                # _msg, not a generic sentence. It carries WHICH fault this
                # was; the old wording said "did not fill" for a cycle where
                # no order was ever created, and threw away the reason the
                # engine had already recorded.
                log.warning(f"[GRID] {branch.bot_name}: {branch.product_id}: {_msg} "
                            f"- will retry next cycle")
            else:
                log.info(f"[GRID] {branch.bot_name}: {_msg}")
            await _record_gate_decision(branch.bot_name, branch.product_id, _event, _msg)
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

        # ---- §5: THIS SLICE'S OWN TAKE-PROFIT TARGET ----
        #
        # WHICH SELL ROUTE THIS IS, because conflating the two is the standing
        # trap. There are two, and they are not the same question:
        #   (1) the GRID trigger, price >= reference_price * (1 + grid_pct) -
        #       a BRANCH-level condition that knows nothing about this slice;
        #   (2) the PARKED sell, where THIS slice's NET over ITS OWN basis
        #       clears GRID_PARKED_MIN_NET_PCT and the reference is never read.
        # slice_target answers exactly (2): the price at which this slice nets
        # a given edge over its own entry. So that is what is recorded, and
        # target_reason says so rather than leaving a bare number to be read
        # as whichever route the reader had in mind.
        #
        # Priced with the rate THIS round trip will really pay - the buy leg's
        # actual recorded rate plus the expected sell leg - not one assumed
        # rate for both. Same input _slice_rate derives for the live sell.
        _target_price, _target_reason = None, None
        try:
            _exit_leg = await expected_leg_fee_rate(branch.product_id)
            _rt_rate = ((buy_leg_fee or 0.0) + _exit_leg
                        if _exit_leg is not None else None)
            _target_price = slice_target.target_price(
                filled_price, _rt_rate, GRID_PARKED_MIN_NET_PCT)
            if _target_price is not None:
                # UP to the venue's price tick. Down would give away the very
                # edge the target was computed to earn.
                _rules_for_tick = await engine.get_product_rules(session, branch.product_id)
                _tick = (_rules_for_tick or {}).get("quote_increment")
                if _tick:
                    # float() at the boundary, deliberately. round_target_up
                    # returns a Decimal (it must, to round exactly), and
                    # target_price is a Float column whose readers do float
                    # arithmetic - _grid_slice_net_pnl raises TypeError on a
                    # Decimal exit price. Caught by running it, not by reading.
                    _target_price = float(
                        slice_target.round_target_up(_target_price, _tick))
                _target_reason = (f"parked_sell floor {GRID_PARKED_MIN_NET_PCT*100:.2f}% "
                                  f"net over this slice's own basis at a "
                                  f"{_rt_rate*100:.3f}% round trip")
        except Exception as _e:
            # A target is a RECORDED FACT, not a decision - nothing trades on
            # it yet. It must never be able to lose a fill that already
            # happened, so it fails to None (UNKNOWN) and the slice is still
            # written.
            log.warning(f"[GRID] {branch.bot_name}: target price unavailable "
                        f"(non-fatal): {type(_e).__name__}: {_e}")
            _target_price, _target_reason = None, None

        async with get_session_factory()() as db:
            # entry_fee_rate records the rate this leg REALLY paid (maker or
            # taker), so this slice can be priced honestly when it later sells.
            db.add(CryptoGridSlice(bot_name=branch.bot_name, product_id=branch.product_id,
                                   entry_price=filled_price, qty=filled_qty,
                                   entry_fee_rate=buy_leg_fee,
                                   # Volatility at the moment of entry, as a
                                   # fraction of price. Without it, comparing a
                                   # fixed 8% stop against an ATR-scaled one is
                                   # impossible after the fact - the whole point
                                   # of recording MAE beside it. _atr comes from
                                   # the same get_price_and_volatility() call
                                   # this cycle already made to get `price`.
                                   entry_atr_pct=((_atr / filled_price)
                                                  if _atr and filled_price else None),
                                   # The live spread the gate measured on the
                                   # real book immediately before this order.
                                   # Kept so the closed ledger can answer
                                   # whether wide-spread entries underperform -
                                   # a question the P&L alone cannot separate
                                   # from ordinary variance.
                                   entry_spread_pct=_gate_detail.get("spread_pct"),
                                   # The gate's full reasoning, as JSON. Best
                                   # effort: a blob that will not serialise must
                                   # never block a fill that already happened.
                                   entry_gate_json=_safe_json(_gate_detail),
                                   # `price` is the live price this cycle read
                                   # BEFORE deciding to buy - what the bot
                                   # believed it would pay. Stored beside what
                                   # it actually paid so the gap is measurable
                                   # later; it cannot be recovered from the
                                   # fill alone.
                                   entry_expected_price=price,
                                   # ---- §1 state, recorded rather than inferred ----
                                   #
                                   # ACCOUNTED, not OPEN: the buy leg FILLED
                                   # (filled_qty/filled_price came back from the
                                   # venue) and THIS transaction is its
                                   # accounting. That is exactly what the state
                                   # means, and slice_lifecycle makes it
                                   # terminal for the same reason - the buy's
                                   # lifecycle is over the moment it is booked.
                                   # Nothing is claimed here about the sell leg,
                                   # which has not been attempted.
                                   slice_state=_sl.ACCOUNTED,
                                   state_updated_at=datetime.utcnow(),
                                   cycle_id=cycle_id,
                                   # WHICH RUNG, 1-based - the same number the
                                   # log line below reports. Not "which third":
                                   # this branch has num_levels rungs.
                                   slice_index=len(slices) + 1,
                                   order_side="BUY",
                                   # What it expected to pay vs what it paid.
                                   # Both, because the gap is the fact worth
                                   # keeping and neither implies the other.
                                   order_price=price,
                                   average_fill_price=filled_price,
                                   filled_quantity=filled_qty,
                                   filled_at=datetime.utcnow(),
                                   execution_reason="grid_buy_filled",
                                   # THE JOIN §24 STEP 7 NEEDED. Coinbase's
                                   # fills feed carries order_id and NOT
                                   # client_order_id, so this is the key that
                                   # actually matches an open order at the
                                   # venue to the slice that placed it.
                                   #
                                   # .get, so a product the engine recorded
                                   # nothing for lands as None - UNKNOWN, not
                                   # a claim. The engine clears the entry at
                                   # every order-attempt entry point, so a
                                   # previous cycle's id can never be read
                                   # here as this order's.
                                   order_id=engine._last_order_id.get(branch.product_id),
                                   target_price=_target_price,
                                   target_reason=_target_reason))
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
    exit_leg_rate = await expected_leg_fee_rate(branch.product_id)

    # ---- EXCURSION TRACKING ----
    #
    # Record how far each open slice has gone against and in favour of the
    # entry. Costs one UPDATE per open slice per cycle and changes no
    # trading behaviour whatsoever.
    #
    # It exists so the stop level can eventually be settled with evidence
    # rather than argument. Fixed 8% beat ATR x 3 by $0.74 across two
    # windows - a tie broken on simplicity, not a demonstrated edge. With
    # MAE on every real trade, "would a 5% stop have done better?" is
    # answerable from the closed book on IDENTICAL entries, which is a
    # cleaner experiment than a backtest that also changes the entries.
    if slices and price:
        try:
            async with get_session_factory()() as db:
                for _exc_slice in slices:
                    _entry = getattr(_exc_slice, "entry_price", None)
                    if not _entry:
                        continue
                    _exc = (price / _entry) - 1.0
                    _row = (await db.execute(select(CryptoGridSlice).where(
                        CryptoGridSlice.id == _exc_slice.id))).scalar_one_or_none()
                    if _row is None:
                        continue
                    _dirty = False
                    if _row.mae_pct is None or _exc < _row.mae_pct:
                        _row.mae_pct = _exc; _dirty = True
                    if _row.mfe_pct is None or _exc > _row.mfe_pct:
                        _row.mfe_pct = _exc; _dirty = True
                    if _dirty:
                        _exc_slice.mae_pct = _row.mae_pct
                        _exc_slice.mfe_pct = _row.mfe_pct
                await db.commit()
        except Exception as e:
            # Pure instrumentation. It must never be able to stop the thing
            # it measures, the same rule the heartbeat already follows.
            log.debug(f"[GRID] {branch.bot_name}: excursion tracking skipped ({e})")

    # ---- STOP LOSS: the one place this bot sells at a loss ON PURPOSE ----
    #
    # This is in direct tension with _pick_profitable_slice_to_sell() below,
    # which exists precisely to stop the bot realising losses - written after
    # the account owner caught a DOGE branch closing -$1.57 across four
    # trades. That rule is NOT being loosened. The difference is intent:
    #
    #   below  - a RISE fired the trigger, and the bot must not mistake it
    #            for profit on a slice that is actually underwater. Still
    #            enforced, unchanged.
    #   here   - the position has fallen far enough that holding it is the
    #            larger risk. Sold deliberately, named as a stop, logged as
    #            a loss.
    #
    # Why it exists, from the owner: "I wind up losing on these coins, and
    # it took a long time for it to recover the money." Without a stop, a
    # slice that falls 40% is held forever waiting for +2.50%. BONK did
    # exactly that: -43.8% in one measured window.
    #
    # Measured on the live six coins, two out-of-sample windows, stop swept
    # from 5% to 25%:
    #
    #     stop     losing window    winning window
    #     none          -$15.28            +$2.77
    #     5%             -$7.51            +$2.77
    #     8%             -$5.36            +$2.77   <- best
    #     10%            -$8.61            +$2.77
    #     25%            -$9.23            +$2.77
    #
    # EVERY level from 5% to 25% beat no-stop in both windows, so the
    # benefit does not depend on picking the number right - which is what
    # separates this from a fitted parameter. And the winning window is
    # IDENTICAL at every level: no stop ever fired there. It is pure
    # downside insurance, free on the upside.
    #
    # GRID_STOP_LOSS_PCT=0 disables it entirely.
    #
    # This block only CHOOSES a slice. Every line of order placement,
    # P&L, shadow logging, row deletion and trade recording below is the
    # existing, already-proven sell path, reused unchanged - the stop must
    # not get its own copy of the most dangerous code in this file.
    # The stop distance this branch actually trades under. GRID_STOP_LOSS_PCT
    # remains the default and the fallback; adaptive_stop only moves it when
    # GRID_STOP_MODE=adaptive and this coin's own volatility can be read, or
    # when a per-coin override names it explicitly. A stop is never removed
    # by a path that merely failed to measure - see adaptive_stop.resolve.
    #
    # Why per-coin at all: one 8% number was six different policies. Over 60
    # days, "did price fall 8% below a given entry within a week" fired on
    # 0.0% of BTC entries and 54.0% of BONK's. The same stop was decorative
    # on one coin and a coin-flip that pays a fee each time on another.
    #
    # A BRANCH MAY NAME ITS OWN STOP, and when it does nothing else runs.
    #
    # Built for adopted branches. The fleet stop sells any slice whose
    # price falls 8% below its ENTRY - and an adopted slice's entry is
    # the price on the day the branch took charge of coin the owner may
    # have held for a year. An 8% wobble would liquidate a long-term hold
    # and book it as a stop_loss against a cost basis nobody ever paid.
    #
    # stop_loss_pct_override is NULL on every branch that existed before
    # this, so their behaviour is byte-identical to before - the adaptive
    # block below still runs for them exactly as it did.
    _override = getattr(branch, "stop_loss_pct_override", None)
    if _override is not None:
        try:
            _stop_pct = float(_override)
        except (TypeError, ValueError):
            _stop_pct = GRID_STOP_LOSS_PCT
            _override = None
    if _override is not None:
        _resolved = None
        if _stop_pct == 0:
            # A STOP OF 0 IS "not an 8% trigger from the adoption date", NOT
            # "no trigger at any price". It had become the second: fourteen
            # branches, $4,573, and nothing able to sell any of it - the grid
            # refuses a losing sale, the resting stops decline coin a branch
            # holds slices on, and the trimmer declines it for the same
            # reason. All three are right, and all three point at the same
            # conclusion: the only seller that can safely exit grid-held coin
            # is the grid, because its slice ledger is the book of record.
            #
            # So the catastrophe stop is resolved HERE and sells through the
            # path below, which retires the slice row and records the trade.
            # OFF unless GRID_ADOPTED_STOP_MODE=arm, so this is a no-op until
            # the owner arms it - and when unarmed the log says exactly what
            # it always said.
            try:
                import adaptive_stop
                _adopted = adaptive_stop.adopted_stop(
                    branch.product_id, _stop_vol,
                    # Resolved env-then-DB, so the dashboard button decides
                    # this as well as the Railway variable.
                    mode_override=await adopted_stop_mode())
                _stop_pct = _adopted["stop_pct"]
                _resolved = _adopted
            except Exception as exc:
                # Cannot be the thing that ADDS a stop. An error here leaves
                # the branch exactly as it was - at 0 - which is the
                # conservative direction for a trigger that sells coin the
                # owner has held for a long time.
                log.warning(f"[GRID] {branch.bot_name}: adopted stop unavailable ({exc}) "
                            f"- this branch keeps no stop, unchanged")
                _stop_pct = 0.0
        # One pure decision, tested in test_stop_report_line.py, so the log can
        # never again say ARMED over a reason that says it is not.
        _level, _msg = stop_report_line(
            branch.bot_name, _stop_pct, _resolved, bool(slices))
        (log.warning if _level == "warning" else log.info)(_msg)
    else:
        _stop_pct = GRID_STOP_LOSS_PCT
        try:
            import adaptive_stop
            _resolved = adaptive_stop.resolve(branch.product_id, GRID_STOP_LOSS_PCT, _stop_vol)
            _stop_pct = _resolved["stop_pct"]
            if _resolved["source"] != "fixed":
                log.info(f"[GRID] {branch.bot_name}: stop - {_resolved['reason']}")
            if _stop_pct == 0 and slices:
                log.warning(f"[GRID] {branch.bot_name}: 🚨 NO STOP - {_resolved['reason']}")
        except Exception as exc:
            # Keep the configured stop. An error here must never be the
            # thing that leaves an open slice without a downside trigger.
            log.warning(f"[GRID] {branch.bot_name}: adaptive stop unavailable ({exc}) - "
                        f"keeping the {GRID_STOP_LOSS_PCT * 100:.1f}% fixed stop")
            _stop_pct = GRID_STOP_LOSS_PCT

    _stop_slice = None
    if _stop_pct > 0 and slices:
        for _stop_candidate in slices:
            _entry = getattr(_stop_candidate, "entry_price", None)
            if _entry and price <= _entry * (1 - _stop_pct):
                _stop_slice = _stop_candidate
                break

    # A branch as full as its levels cannot buy, so the spacing gate is
    # protecting a rebuy that cannot happen - see GRID_PARKED_MIN_NET_PCT.
    # Evaluated BEFORE the gate so the reason a sale happened is one of two
    # named conditions rather than an implicit fall-through.
    # An adopted-only branch counts as parked HERE even when the adopted
    # headroom above has lifted num_levels past its slice count. That
    # headroom is one conditional rung for BUYING; it is not room, and this
    # gate asks a different question - can this branch get out at all. Left
    # as the bare level comparison, granting the rung would have silently
    # switched the parked-sell gate back off for the eleven branches it was
    # built for, leaving them on the 2.5% reference-rise trigger they have
    # not been able to reach. Buy headroom must not cost sell freedom.
    # Counted on TRADEABLE slices for the same reason as the buy gate: a
    # branch whose rungs are filled by a remnant the venue will not sell is
    # not full, it is stuck, and calling it parked sent it to an escape
    # hatch that could only offer that same remnant. BCH-USD placed a sell
    # for 0.00000022 BCH every ~10 minutes for 200 attempts this way.
    _parked = bool(slices) and (
        len(tradeable_slices(slices)) >= (branch.num_levels or 0)
        or branch_is_adopted_only(slices))
    _parked_sell = False
    _parked_slice = None
    if _parked and _stop_slice is None:
        _parked_slice, _pct = _pick_parked_slice_to_sell(
            slices, price, real_fee_rate, exit_leg_rate, GRID_PARKED_MIN_NET_PCT)
        if _parked_slice is not None:
            _parked_sell = True
            _notional = (_parked_slice.qty or 0) * (_parked_slice.entry_price or 0)
            log.info(
                f"[GRID] {branch.bot_name}: parked ({len(slices)} slices / "
                f"{branch.num_levels} levels - cannot buy), and a slice is "
                f"+{_pct * 100:.2f}% net of fees. Selling on its own "
                f"merit rather than waiting for a {grid_pct * 100:.2f}% rise "
                f"off a reference it will never rebuy from.")
            # DURABLE, because a protection whose firing cannot be observed is
            # indistinguishable from one that never fires. The escape hatch is
            # the only way out of a parked branch, and until now nothing but a
            # Railway log line said whether it had ever tried. The notional is
            # recorded with it: a branch whose only qualifying slice is dust
            # reports itself escapable while staying locked, and that is
            # invisible unless the size is written down next to the verdict.
            if _feed_should_write("PARKED_SELL", branch.product_id):
                await _record_gate_decision(
                    branch.bot_name, branch.product_id, "PARKED_SELL",
                    f"+{_pct * 100:.2f}% net on ${_notional:,.2f} of basis "
                    f"({len(slices)}/{branch.num_levels} rungs full)")
        # PARKED_NO_EXIT is deliberately NOT written here any more.
        #
        # It is a steady STATE, not an event, and writing it every cycle for
        # every parked branch flooded the Live Ops feed: measured at 32 of the
        # last 40 rows, which pushed every GATE_PASS, GATE_BLOCK and - the
        # dangerous one - every CYCLE_ERROR out of the window entirely. An
        # alarm whose evidence has been crowded out of the feed cannot fire,
        # which is the exact failure this instrumentation existed to prevent,
        # caused by the instrumentation itself.
        #
        # Nothing is lost: the watchdog derives the same fact from
        # /grid-status directly (PARKED, PARKED_DUST), where it costs no feed
        # rows. Only the two genuine EVENTS stay durable - an escape sell
        # attempted, and an escape sell refused.

    # Hoisted so the exit can be NAMED, not just taken. All three entries to
    # this block used to collapse into "stop_loss" or "profit_target", which
    # made profit_target mean "not a stop" rather than "the target was hit".
    _rise_hit = bool(slices and price >= branch.reference_price * (1 + grid_pct))
    if _stop_slice is not None or _parked_sell or _rise_hit:
        if _stop_slice is not None:
            # The stop deliberately bypasses _pick_profitable_slice_to_sell.
            # That function's whole job is to refuse a losing sale; here the
            # loss is the point, so it is named, logged at WARNING, and the
            # slice is chosen explicitly rather than certified profitable.
            oldest = _stop_slice
            log.warning(
                f"[GRID] {branch.bot_name}: 🛑 STOP LOSS on {branch.product_id} - slice entered at "
                f"${oldest.entry_price:,.6f} is down {(1 - price / oldest.entry_price) * 100:.2f}% at "
                f"${price:,.6f}, past the {_stop_pct * 100:.1f}% stop. Selling at a loss on "
                f"purpose: a slice this far down waits a very long time for +{grid_pct * 100:.2f}%, "
                f"and that wait is the risk this stop exists to cut."
            )
        elif _parked_sell and not _rise_hit:
            # The parked route is the one that fired, so sell the slice that
            # actually cleared its floor. Recomputing with the FIFO picker
            # here would sell a DIFFERENT slice from the one the gate just
            # certified - the gate would pass on a +3.0% slice and the sale
            # would hand over a +0.1% one.
            oldest = _parked_slice
        else:
            oldest = _pick_profitable_slice_to_sell(slices, price, real_fee_rate, exit_leg_rate)
            if oldest is None:
                log.info(
                    f"[GRID] {branch.bot_name}: real rise trigger fired (${price:,.4f} >= "
                    f"${branch.reference_price * (1 + grid_pct):,.4f}) but no open slice would net a real "
                    f"profit at this price - holding every slice, waiting for a genuinely profitable one"
                )
                return
        fill = await grid_sell(session, oldest.qty, branch.product_id, branch.bot_name)
        if not fill:
            log.warning(f"[GRID] {branch.bot_name}: real grid sell of {branch.product_id} did not fill - will retry next cycle")
            # A parked branch that cannot fill its escape sell is stuck in a
            # retry loop: the gate passes every cycle, the order does not
            # fill, and the cycle returns here having done nothing. That is
            # the same outward shape as a healthy quiet branch, so it is
            # written down rather than left to a log line nobody reads.
            if _parked_sell and _feed_should_write("PARKED_SELL_NOFILL",
                                                  branch.product_id):
                await _record_gate_decision(
                    branch.bot_name, branch.product_id, "PARKED_SELL_NOFILL",
                    f"escape sell of {oldest.qty:g} did not fill - branch "
                    f"stays full on its rungs")
            return
        filled_qty, filled_price, sell_leg_fee = fill
        # THE BASIS THAT DECIDED THE SALE IS THE BASIS THAT PRICES IT.
        #
        # This read `oldest.entry_price`, and the note at GRID_TRUE_COST_BASIS
        # promised the declared basis "never changes a recorded P&L". That
        # promise was wrong, and ZEC is what it cost. _pick_parked_slice_to_sell
        # authorised these two sales on the declared $1,013.80 - the gate log
        # says "+30.95% net on $133.33 of basis" - and then this line booked
        # them against the adoption-day mark of ~$1,655:
        #
        #   id 197  0.08036146 @ 1331.94   booked -$26.72   really +$25.23
        #   id 198  0.37816667 @ 1330.02   booked -$123.21  really +$118.04
        #
        # One sale, two contradictory numbers, and the wrong one persisted.
        # $293.20 of it. That is not only a display fault: allocated_usd moves
        # by `pnl` and by nothing else (there is no decrement on the buy), so
        # the phantom loss took $149.93 of working capital off crypto_grid_21 -
        # and _safe_num_levels_for_allocation() sizes the ladder off
        # allocated_usd, so a measurement error was shrinking the real ladder.
        # Banked read -$14.35 across 198 trades on a book that had taken
        # +$278.85.
        #
        # sell_basis_for_slice() returns entry_price for every slice the grid
        # itself bought and for every product with no declared basis, so this
        # is a no-op everywhere except the case it exists for. The fee was
        # already right: slice_round_trip_fee_rate() charges an adopted slice
        # for its sell leg only, because no buy order was ever placed.
        _booked_basis = sell_basis_for_slice(oldest, product_id=branch.product_id)
        # Priced with the rate THIS slice's buy leg really paid plus the rate
        # its sell leg really paid - not one assumed rate for both.
        pnl = _grid_slice_net_pnl(filled_qty, _booked_basis, filled_price,
                                  await slice_round_trip_fee_rate(oldest, sell_leg_fee))
        new_balance = branch.allocated_usd + pnl

        # WHY THIS SLICE CLOSED - computed ONCE, above every reader.
        #
        # This three-way lived in two places: inside the shadow block and
        # again in the _log_grid_trade call below. The comment there already
        # warned that the two sites disagreeing is what produced the
        # P&L-sign version of exit_reason in the first place - so a third
        # reader was not going to be the one that made duplication safe.
        #
        # It also could not be read from the shadow block at all: that block
        # is guarded by SHADOW_MODE_ENABLED, which is FALSE in production
        # (it imports from a path Railway does not have), so a name assigned
        # inside it is simply undefined everywhere the fleet actually runs.
        #
        # _rise_hit wins over a parked sell when both are true: the target
        # genuinely was reached, so saying so is the more truthful of the two.
        # _stop_slice is set only when the stop actually chose this slice,
        # which is why it is the real source rather than the sign of the P&L.
        exit_reason = ("stop_loss" if _stop_slice is not None
                         else "profit_target" if _rise_hit
                         else "parked_sell")

        # AN INHERITED POSITION LEAVING IS NOT ONE OF THE GRID'S ROUND TRIPS.
        #
        # The pnl above is now correct - priced at the basis that authorised
        # the sale - and it still is not a measure of how well this grid
        # trades. The grid did not choose the entry: coin_adoption handed it
        # a position already open, at a price set by whoever bought it. Its
        # outcome is the account owner's old decision resolving, not a round
        # trip the strategy picked both ends of, so counting it in the win
        # rate or the expectancy answers a question it cannot speak to - in
        # either direction. ZEC exiting at +$143 would have lifted the win
        # rate on the strength of a buy made before this bot existed.
        #
        # So the row is tagged, and get_grid_performance_metrics() leaves this
        # reason out of the grid's own record. Nothing is hidden: the row is
        # still written, still visible in the trade history, and still counted
        # by everything measuring real cash taken.
        if (exit_reason == "parked_sell" and oldest is not None
                and slice_paid_no_entry_fee(oldest)
                and true_cost_basis_for(branch.product_id) is not None):
            exit_reason = ADOPTED_EXIT_REASON

        # ── SHADOW MODE: Log position closed (fire-and-forget, non-blocking) ────
        if SHADOW_MODE_ENABLED and shadow_manager:
            try:
                order_id = f"{branch.bot_name}_{oldest.id}_{int(time.time()*1000)}"
                hold_time_minutes = int((time.time() - oldest.opened_at.timestamp()) / 60) if oldest.opened_at else 0
                # Same basis as the booked pnl below it, or
                # `fees_paid = gross_pnl - pnl` stops being a fee at all.
                gross_pnl = filled_qty * (filled_price - _booked_basis)
                # THE FEE IS DERIVED, NOT RE-DERIVED.
                #
                # This read:
                #   total_fees = (await slice_round_trip_fee_rate(...))
                #                * filled_qty * oldest.entry_price / 100
                # which is a second, hand-written copy of the fee formula -
                # and it was wrong in two ways. slice_round_trip_fee_rate()
                # returns a FRACTION (0.0070 for a 0.70% round trip), so the
                # /100 made the fee about 100x too small; and it charged the
                # rate against the ENTRY notional alone where the real
                # formula charges it against both legs, qty*(entry+exit)/2.
                #
                # It mattered because the consumer does
                # `net_pnl = realized_pnl - fees` - a fee 100x too small
                # makes every trade look nearly fee-free to the learning
                # layer, which is the one reader whose whole job is judging
                # whether the edge is real.
                #
                # _grid_slice_net_pnl() returns gross - fee, so gross - net
                # IS the fee it charged, exactly, for whatever rate this
                # slice was priced at. Deriving it cannot drift from the
                # real formula the way a second copy can - the same reason
                # that formula was extracted into one function to begin
                # with. `pnl` above is the figure actually booked into
                # allocated_usd, so this is the fee actually paid.
                fees_paid = gross_pnl - pnl
                # THE SIGN OF THE P&L IS NOT A REASON.
                #
                # This read `'profit_target' if pnl >= 0 else 'stop_loss'`,
                # which makes exit_reason a restatement of the P&L sign and
                # nothing more: it carries no information the P&L does not
                # already carry, while being labelled as the independent fact
                # that explains it. A profit-target sale left slightly
                # negative by fees came through as a stop; a stop that
                # happened to close up came through as a target hit.
                #
                # _stop_slice is set only when the stop actually chose this
                # slice, so it is the real source - the same one the
                # persisted ledger uses at the _log_grid_trade call below,
                # whose own comment already said the sign "cannot tell a stop
                # from an ordinary sale that happened to lose". It was in
                # scope here all along (assigned well above this block); the
                # two write sites simply disagreed.
                # Same three-way as the persisted ledger below. The two
                # sites disagreeing is what produced the P&L-sign version
                # in the first place, so they are kept identical.

                shadow_manager.on_position_closed(
                    client_order_id=order_id,
                    symbol=branch.product_id,
                    entry_price=oldest.entry_price,
                    exit_price=filled_price,
                    quantity=filled_qty,
                    realized_pnl=gross_pnl,
                    total_fees=fees_paid,
                    hold_time_minutes=hold_time_minutes,
                    exit_reason=exit_reason,
                    risk_amount=branch.allocated_usd / branch.num_levels
                )
            except Exception as e:
                log.warning(f"[SHADOW] Failed to log position close (non-blocking): {e}")

        # The venue sale above has ALREADY happened. Everything below is the
        # ledger catching up to it, and the two are not one transaction - so
        # this retries rather than letting one transient DB error leave coin
        # sold with the slice still claiming it. That gap is the growing half
        # of the tracked-vs-held drift; the partial-fill handling below is the
        # shrinking half.
        _residual, _retire = grid_sell_residual(oldest.qty, filled_qty)
        _persisted = False
        _persist_exc = None
        for _attempt in range(3):
            try:
                async with get_session_factory()() as db:
                    slice_result = await db.execute(select(CryptoGridSlice).where(CryptoGridSlice.id == oldest.id))
                    slice_row = slice_result.scalar_one_or_none()
                    if slice_row:
                        if _retire:
                            await db.delete(slice_row)
                        elif _residual is not None:
                            # Kept, not retired: the wallet still holds this
                            # much of the slice. entry_price and
                            # entry_fee_rate are deliberately untouched, so
                            # slice_round_trip_fee_rate() still prices the
                            # remainder against what its buy leg really paid.
                            slice_row.qty = _residual
                            # ---- §1/§15 state, on the write that already
                            # happens. No new write path, no new way for a
                            # ledger update to fail, and it inherits this
                            # block's three retries.
                            #
                            # PARTIAL is the honest state: the venue filled
                            # some of what was asked and this much is still
                            # held. It is NOT FILLED (the order did not
                            # complete) and NOT an error - §2 is explicit
                            # that a remainder is neither.
                            slice_row.slice_state = _sl.PARTIAL
                            slice_row.state_updated_at = datetime.utcnow()
                            # What ACTUALLY filled, never what was asked for.
                            slice_row.filled_quantity = filled_qty
                            slice_row.average_fill_price = filled_price
                            slice_row.filled_at = datetime.utcnow()
                            slice_row.order_side = "SELL"
                            slice_row.execution_reason = exit_reason
                    branch_result = await db.execute(select(CryptoGridBranch).where(CryptoGridBranch.bot_name == branch.bot_name))
                    fresh = branch_result.scalar_one_or_none()
                    if fresh:
                        fresh.allocated_usd += pnl
                        fresh.reference_price = filled_price
                        new_balance = fresh.allocated_usd
                    await db.commit()
                _persisted = True
                break
            except Exception as _exc:
                _persist_exc = _exc
                if _attempt < 2:
                    await asyncio.sleep(0.5 * (2 ** _attempt))
        if not _persisted:
            # Loud on purpose. The coin is gone and the branch still claims
            # it; coin_tracked_is_held will report this branch short until
            # reconcile-slices runs. Saying so here is the difference between
            # a known discrepancy and a silent one.
            log.error(
                f"[GRID] {branch.bot_name}: SOLD {filled_qty:.8f} {branch.product_id} "
                f"at the venue but could NOT write the slice back after 3 attempts "
                f"({type(_persist_exc).__name__}: {_persist_exc}). The coin has left "
                f"the wallet and this branch still tracks it - expect "
                f"coin_tracked_is_held to report it short until reconcile-slices runs."
            )
        elif not _retire and _residual is not None:
            log.warning(
                f"[GRID] {branch.bot_name}: {branch.product_id} sell filled "
                f"{filled_qty:.8f} of {oldest.qty:.8f} - PARTIAL. The slice keeps "
                f"{_residual:.8f} rather than being retired whole, so tracked units "
                f"still match the coin actually held."
            )
        # The basis the P&L was priced at, so qty, entry, exit and pnl on the
        # stored row agree. Writing the adoption mark beside a pnl computed
        # off the declared basis is the same contradiction one layer down,
        # and every reader that recomputes gross from the row would disagree
        # with the pnl sitting next to it.
        await _log_grid_trade(branch.bot_name, branch.product_id, _booked_basis,
                              filled_price, filled_qty, pnl, oldest.opened_at,
                              entry_expected_price=oldest.entry_expected_price,
                              exit_expected_price=price,
                              # THREE WAYS IN, THREE NAMES OUT.
                              #
                              # _stop_slice is set only when the stop chose this
                              # slice, so it is the honest source of the reason -
                              # not the sign of the P&L, which cannot tell a stop
                              # from an ordinary sale that happened to lose.
                              #
                              # The other two are NOT the same event, and calling
                              # both "profit_target" made that word mean "not a
                              # stop". A parked sell happens when the branch is
                              # full, cannot buy, and a slice clears
                              # GRID_PARKED_MIN_NET_PCT on its own merit - its own
                              # log line says it sells "rather than waiting for a
                              # rise off a reference it will never rebuy from", so
                              # the target was precisely NOT what was hit. They
                              # have different thresholds and different reasons to
                              # exist, and a later experiment asking whether the
                              # parked-sell gate earns its keep cannot ask it at
                              # all while the two share a label.
                              #
                              # _rise_hit wins over _parked_sell when both are
                              # true: the target genuinely was reached, so saying
                              # so is the more truthful of the two.
                              exit_reason=exit_reason,
                              mae_pct=getattr(oldest, "mae_pct", None),
                              mfe_pct=getattr(oldest, "mfe_pct", None),
                              entry_atr_pct=getattr(oldest, "entry_atr_pct", None),
                              entry_spread_pct=getattr(oldest, "entry_spread_pct", None),
                              entry_gate_json=getattr(oldest, "entry_gate_json", None),
                              stop_pct=(_stop_pct or None))
        is_true_oldest = slices[0].id == oldest.id
        msg = (
            f"{'📈' if pnl >= 0 else '📉'} {branch.bot_name} GRID SELL: sold "
            f"{'the oldest' if is_true_oldest else 'the oldest PROFITABLE (skipped a stuck older)'} "
            f"real slice of {branch.product_id} @ ${filled_price:,.2f} "
            f"(basis ${_booked_basis:,.2f}"
            f"{'' if _booked_basis == oldest.entry_price else f' declared - recorded entry ${oldest.entry_price:,.2f}'})"
            f" | P&L: {'+' if pnl >= 0 else ''}${pnl:.2f} after est. fees | branch now ${new_balance:.2f}"
        )
        log.info(f"[GRID] {msg}")
        await _log_activity_safe(branch.bot_name, branch.product_id, "SELL", msg)
        # Real "settle immediately" check - only when THIS sale emptied
        # the branch out to flat (its own last open slice), so freshly-
        # realized profit doesn't just sit waiting for the next scheduled
        # 30-min sweep before it goes back to work. Best-effort: a
        # failure here can never unwind or affect the real sale that
        # already completed above.
        # `_retire` matters here: a PARTIAL fill leaves the slice alive with a
        # residual, so the branch is not flat and there is nothing to settle
        # yet. Before partial fills were handled the slice always vanished,
        # which made "last slice sold" and "branch now empty" the same thing.
        # They are no longer the same thing.
        if len(slices) == 1 and _retire:
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
                     "lease_refused": 4.0,
                     # Master switch off. Distinct from lease_refused: one is
                     # "another process is doing the work", the other is "no
                     # process will". Both previously read as "entered".
                     "bot_inactive": 5.0}

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
# Set once this process has swept orphaned maker orders (see
# engine.sweep_orphan_maker_orders). Per process on purpose: a restart is
# exactly when orphans exist, so every new process must sweep once.
_orphan_sweep_done = False


async def _sweep_orphans_once() -> bool:
    """Returns True when the sweep completed (even if it found nothing)."""
    try:
        async with engine.aiohttp.ClientSession() as session:
            res = await engine.sweep_orphan_maker_orders(session)
    except Exception as e:
        log.warning(f"[GRID] orphan sweep failed: {type(e).__name__}: {e}")
        return False
    if res is None:
        return False
    if res["found"]:
        log.warning(f"[GRID] restart: {res['found']} maker order(s) left resting by a "
                    f"previous process - cancelled {len(res['cancelled'])}, "
                    f"failed {len(res['failed'])}")
    for o in res["cancelled"] + res["failed"]:
        ok = o in res["cancelled"]
        msg = (f"Order {o['order_id']} ({o['side']} {o['product_id']}) was left resting "
               f"by a process that stopped mid-wait; "
               + ("cancelled on restart." if ok else "cancel FAILED - still resting on Coinbase."))
        if o["filled_size"] > 0:
            msg += (f" It had already filled {o['filled_size']} with no slice recording "
                    f"it - reconciliation will pick up the difference.")
        await _log_activity_safe("grid_fleet", o["product_id"], "ORPHAN_ORDER", msg)
    # A failed cancel is still out there: sweep again next cycle.
    return not res["failed"]


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


async def record_capital_snapshot():
    """One row an hour: realised P&L beside the capital that produced it.

    See models.CapitalSnapshot for why. In short: realised has a durable
    history and the capital behind it had none, so every rate had to be
    computed against whatever the wallet looked like at the moment
    somebody asked - and on 2026-09-28 that produced 0.1183%/day and
    0.0509%/day from the same 26 days, twenty-two minutes apart.

    Throttled by reading the newest row, not by an in-process timer, so
    a restart cannot reset the clock and flood the table.

    NEVER fatal, and never partial: if any figure cannot be read, NO row
    is written. A snapshot with working_usd missing would be a hole in
    exactly the series that exists to have no holes, and edge_rate would
    have to skip the interval anyway.
    """
    try:
        import account_census
        import aiohttp as _aiohttp
        import edge_rate
        import invariants as inv
        from models import CapitalSnapshot
        from sqlalchemy import desc as _desc

        async with get_session_factory()() as db:
            newest = (await db.execute(
                select(CapitalSnapshot).order_by(_desc(CapitalSnapshot.at)).limit(1)
            )).scalar_one_or_none()
            if newest is not None and newest.at is not None:
                age = (datetime.utcnow() - newest.at).total_seconds()
                if age < edge_rate.SNAPSHOT_INTERVAL_SECONDS:
                    return None

        status = await get_grid_status()
        branches = status.get("branches") or []
        alloc = {b.get("product_id"): float(b.get("allocated_usd") or 0.0) for b in branches}
        allocated = sum(alloc.values())

        async with _aiohttp.ClientSession() as _s:
            census = await account_census.census(_s, tracked_usd=0.0)
        if not census.get("available"):
            return None
        account = census.get("total_usd")

        rows = []
        for b in branches:
            slices = b.get("slices") or []
            pcts = [s.get("unrealized_net_pct") for s in slices]
            pcts = [p * 100 for p in pcts if p is not None]
            rows.append({"product_id": b.get("product_id"),
                         "allocated_usd": b.get("allocated_usd"),
                         "open_slices": len(slices),
                         "num_levels": b.get("num_levels"),
                         "best_slice_net_pct": max(pcts) if pcts else None})
        dead = inv.no_dead_capital(rows)
        tracked, _ = await fleet_tracked_units_by_product()
        free = inv.grid_inventory_is_free(tracked, census.get("holdings") or [])

        stuck_set = set(dead.get("branches") or []) if dead.get("status") == inv.FAIL else set()
        locked_set = ({r["product_id"] for r in (free.get("locked_positions") or [])}
                      if free.get("status") == inv.FAIL else set())
        # UNION, never the sum - three branches sit in both lists, and
        # adding them double-counts $346.75 of the live book.
        parked_set = stuck_set | locked_set
        parked = sum(alloc.get(p, 0.0) for p in parked_set)

        realized = ((status.get("adaptive_fleet") or {}).get("realized_grid_pnl"))
        if realized is None or account is None:
            return None

        async with get_session_factory()() as db:
            db.add(CapitalSnapshot(
                at=datetime.utcnow(),
                realized_usd=float(realized),
                account_usd=float(account),
                allocated_usd=round(allocated, 2),
                working_usd=round(allocated - parked, 2),
                parked_usd=round(parked, 2),
                stuck_usd=round(sum(alloc.get(p, 0.0) for p in stuck_set), 2),
                locked_usd=round(sum(alloc.get(p, 0.0) for p in locked_set), 2),
                open_branches=len(branches),
            ))
            await db.commit()
        return True
    except Exception as e:
        log.debug(f"[GRID] capital snapshot skipped (non-fatal): {type(e).__name__}: {e}")
        return None


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
    # Stamp the start of a run of maker-only while we are here. Cheap, and
    # it is the one fact the maker_only invariant needs to stop being
    # permanently UNKNOWN - see maker_only_armed_at().
    try:
        await record_maker_only_state(await is_maker_only_active())
    except Exception:
        pass
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

    # First cycle this process owns the loop: clear out any maker order a
    # previous process left resting when it was killed mid-wait. Runs before
    # this process places anything, so every "gmk-" order it finds is an
    # orphan. Retried next cycle if the open orders could not be read.
    global _orphan_sweep_done
    if not _orphan_sweep_done:
        _orphan_sweep_done = await _sweep_orphans_once()

    if not await is_grid_bot_active():
        # The master switch being OFF is a DECISION, and a decision that
        # stops all trading must be legible. This return had no log and no
        # heartbeat stage at all, so the loop kept stamping "entered" on
        # schedule and the dashboard kept serving live prices while nothing
        # traded. On 2026-09-26 that read as healthy for ~2.5 hours.
        #
        # is_grid_bot_active() DEFAULTS to True, so False means a row was
        # explicitly written. Somebody or something switched the fleet off;
        # that is worth a warning, not silence.
        log.warning("[GRID] not cycling: the grid master switch is OFF "
                    "(crypto_grid_bot_active). Nothing will trade until it is "
                    "switched back on. This flag defaults to ON, so it was set "
                    "off deliberately by someone or something.")
        await _record_grid_heartbeat("bot_inactive")
        return
    branches = [b for b in await get_grid_branches() if b.active]
    if not branches:
        await _record_grid_heartbeat("no_active_branches")
        return
    await _record_grid_heartbeat("cycled")
    # Beside the heartbeat, and as unable to stop the loop as it is: the
    # denominator, recorded while it is true rather than reconstructed
    # later from prices that have moved.
    await record_capital_snapshot()
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

        # ONE id for this whole pass, so every slice opened in it agrees on
        # which pass that was. Read once rather than per branch: branches are
        # walked in sequence with a sleep between them, so per-branch stamps
        # would land in different seconds and claim to be different cycles.
        _cycle_id = current_cycle_id()

        for branch in branches:
            try:
                await run_grid_branch_cycle(session, branch, cycle_id=_cycle_id)
            except Exception as e:
                # A LOST FILL IS INVISIBLE WITHOUT THIS, and that is not
                # hypothetical. grid_buy increments the buy fill-mix counter
                # immediately before returning a fill, so a raise ANYWHERE
                # after it - the target block, the insert, the commit - leaves
                # the coin really bought, the slice row never written, and
                # NEITHER the maker-only skip counter NOR the maker expiry
                # counter moved, because both live on the no-fill path. Live
                # on 2026-09-29 that state took an hour to establish by hand
                # from arithmetic across four dashboard pulls, because the
                # only trace this handler left was a Railway log line no
                # dashboard reads.
                #
                # Type AND message. `{e}` alone renders an argument-less
                # exception as the empty string, which is how a swallowed
                # KeyError becomes "cycle error: " and says nothing at all.
                _detail = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
                log.error(f"[GRID] {branch.bot_name} cycle error: {_detail}")
                # Durable, and deliberately NOT allowed to change behaviour:
                # the except still swallows, the fleet still walks on to the
                # next branch. This records that the pass died, never decides
                # anything. _record_gate_decision is contractually unable to
                # raise, so it cannot turn a swallowed error into a loud one.
                await _record_gate_decision(branch.bot_name, branch.product_id,
                                            "CYCLE_ERROR", _detail)
            await asyncio.sleep(0.5)

        # Fill in any post-expiry horizons that have come due. Inside the
        # session so it reuses the connection, after the branches so it can
        # never delay a trading decision, and capped per cycle.
        await _resolve_maker_expiries(session)

        # Short-horizon opportunity scoring - OBSERVATION ONLY.
        #
        # Placed here, after every branch has already decided, precisely so
        # it cannot influence one. Nothing on the execution path reads a
        # score; opportunity_signals.SIGNALS_LIVE defaults off and even when
        # on, the score never overrides _net_edge_gate_ok - that gate is
        # arithmetic about cost and the score is a guess about opportunity.
        #
        # Each pass writes a dated, falsifiable prediction and marks the ones
        # whose horizons have come due. The point is to find out whether a
        # high score means anything BEFORE it is allowed to mean anything.
        try:
            _deadline = time.time() + TELEMETRY_BUDGET_SECONDS
            await _score_short_term_opportunities(session, branches, _deadline)
            if time.time() < _deadline:
                # ONE fetch per coin per pass, shared by all three resolvers.
                # They were each calling fetch_candles_full independently, so
                # a coin appearing in two of them was fetched twice for the
                # identical 300 bars - against a 25-second budget where a
                # single fetch is allowed 15. The memo is per-pass, so the
                # candles are never stale by more than one cycle.
                _candles = {}

                async def _candles_for(pid):
                    if pid not in _candles:
                        _candles[pid] = await signals.fetch_candles_full(session, pid)
                    return _candles[pid]

                await signals.resolve(session, _candles_for, deadline=_deadline)
                # Candles, not mid samples: wicks count, and the ORDER of
                # the two extremes decides whether an MFE was collectable.
                await signals.resolve_crossings(_candles_for, deadline=_deadline)
                # The six-hour horizon-gate window. Comes due five and a half
                # hours after the live ledger has finished with the same row,
                # which is why it is a separate pass and not a longer one.
                await signals.resolve_horizon_gate(_candles_for, deadline=_deadline)
        except Exception as e:
            log.debug(f"[SIGNAL] scoring pass skipped: {type(e).__name__}: {e}")

        # The horizon study - OUT OF BAND, and never awaited here.
        #
        # Everything above measures a thirty-minute window, because that is
        # what a 25-second telemetry budget inside a 180-second lease can
        # afford to look at. The study asks the same question over weeks, at
        # about ninety paginated requests and several minutes, which is why
        # it is spawned rather than called: a fleet that stops trading in
        # order to measure itself has answered the wrong question twice.
        #
        # spawn_if_due() checks its own throttle BEFORE fetching anything
        # and returns immediately when it is not due.
        try:
            horizon_study.spawn_if_due()
        except Exception as e:
            log.debug(f"[HORIZON] spawn skipped: {type(e).__name__}: {e}")

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

    # Floating base: pull every FLAT branch's reference up to the live
    # market so its next buy sits one normal step away instead of behind a
    # dip that already happened. Same in-process throttle as above.
    global _last_grid_reanchor_at
    if (auto_reanchor_enabled()
            and now - _last_grid_reanchor_at >= GRID_REANCHOR_INTERVAL_SECONDS):
        _last_grid_reanchor_at = now
        try:
            _ra = await reanchor_flat_grid_branches_now()
            _moved = _ra.get("moved") or []
            if _moved:
                log.info(f"[GRID] floating base: re-anchored {len(_moved)} flat branch(es) - "
                         + ", ".join(f"{m.get('bot_name')} {m.get('product_id')}"
                                     for m in _moved[:6]))
        except Exception as e:
            # Never fatal. A stale reference costs opportunity; a stopped
            # trading loop costs everything.
            log.error(f"[GRID] re-anchor sweep error: {e}")

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


async def _resolve_branch_stop(session, product_id):
    """The stop this branch trades under right now, for reporting only.

    cached_only: a dashboard poll must never pay for a month of candles
    per coin. The trading cycle fills the cache; until it has, this
    reports the fixed stop, which is genuinely what would be used.
    """
    try:
        import adaptive_stop
        vol = await adaptive_stop.measure_daily_vol(session, product_id, cached_only=True)
        r = adaptive_stop.resolve(product_id, GRID_STOP_LOSS_PCT, vol)
        r["daily_vol_pct"] = round(vol, 4) if vol is not None else None
        return r
    except Exception as exc:
        log.warning(f"[GRID] stop report failed for {product_id}: {exc}")
        return {"stop_pct": GRID_STOP_LOSS_PCT, "source": "fixed",
                "reason": f"stop report failed ({exc}); the fixed stop stands",
                "daily_vol_pct": None}


def _reported_stop(branch, resolved, adopted_mode=None):
    """The stop THIS branch really trades under, for reporting.

    _resolve_branch_stop is keyed by product, but the override that
    actually decides the stop lives on the BRANCH - so a per-product read
    alone reported a 15.88% adaptive stop on every adopted branch while
    run_grid_branch_cycle applied no grid stop at all. A safety figure that
    disagrees with the code enforcing it is worse than no figure: it says
    coin held for a year is protected by a trigger that will never fire.

    The precedence here is the SAME test run_grid_branch_cycle uses -
    `is not None`, never truthiness, because the override adoption writes
    is 0.0 and `if override:` would silently fall through to adaptive.
    """
    override = getattr(branch, "stop_loss_pct_override", None)
    if override is None:
        return dict(resolved or {})
    try:
        pct = float(override)
    except (TypeError, ValueError):
        # Unreadable override -> the branch itself falls back to the fixed
        # stop, so report that, not the adaptive figure.
        return {"stop_pct": GRID_STOP_LOSS_PCT, "source": "fixed",
                "reason": (f"branch override {override!r} is unreadable; the fixed "
                           f"{GRID_STOP_LOSS_PCT * 100:.0f}% stop stands"),
                "daily_vol_pct": (resolved or {}).get("daily_vol_pct")}
    if pct == 0:
        # THE ADOPTED CATASTROPHE STOP, resolved the same way and from the
        # same volatility the cycle uses - this function exists because a
        # per-product read alone reported a stop the cycle was not applying,
        # and reporting 0 while the cycle now applies 35% would be that bug
        # with the sign flipped.
        try:
            import adaptive_stop
            # adopted_mode passed IN, for the same reason `resolved` is: this
            # function is sync and the switch now has a database half that
            # only an async read can see. None falls back to the environment
            # alone, which is what every existing caller got.
            _ad = adaptive_stop.adopted_stop(branch.product_id,
                                             (resolved or {}).get("daily_vol_pct"),
                                             mode_override=adopted_mode)
            if _ad["stop_pct"]:
                return {"stop_pct": _ad["stop_pct"], "source": _ad["source"],
                        "reason": _ad["reason"],
                        "daily_vol_pct": (resolved or {}).get("daily_vol_pct")}
            _why = _ad["reason"]
        except Exception as exc:
            _why = (f"this branch names its own stop of 0, and the adopted-stop policy "
                    f"could not be read ({type(exc).__name__})")
        # THE CLAIM THIS USED TO MAKE, AND WHY IT IS GONE.
        #
        # It read "Adopted coin is covered at the portfolio level by the
        # resting stops". Nothing here checked that, and on 2026-09-29 it was
        # false for every adopted branch. resting_stops refuses any asset a
        # grid branch holds slices on (ACTIVELY_TRADED), for good reasons of
        # its own - and its refusal said "the branch carries its own adaptive
        # stop", which is exactly what THIS branch has just declared it does
        # not. ZEC went one further: deferred to the concentration trimmer,
        # which declined it as WITHIN_LIMIT. Three layers each naming the
        # next, and /resting-stops reporting protects_usd 0.
        #
        # This function's own docstring is the rule being applied to it - a
        # safety figure that disagrees with the code enforcing it is worse
        # than no figure. So it states only what it knows, and points at where
        # coverage can be CHECKED instead of asserting that it exists.
        return {"stop_pct": 0.0, "source": "branch_override_none",
                "reason": (f"{_why}. No portfolio cover is claimed here either, because "
                           f"nothing in this function can verify one - check "
                           f"/resting-stops, where an asset listed under `uncovered` has "
                           f"a stop from neither layer."),
                "daily_vol_pct": (resolved or {}).get("daily_vol_pct")}
    return {"stop_pct": pct, "source": "branch_override",
            "reason": f"this branch names its own {pct * 100:.2f}% stop",
            "daily_vol_pct": (resolved or {}).get("daily_vol_pct")}


def _stop_policy_block():
    """Never reports a policy it could not read."""
    try:
        import adaptive_stop
        p = adaptive_stop.policy()
        p["fixed_default_pct"] = GRID_STOP_LOSS_PCT
        return p
    except Exception as exc:
        log.warning(f"[GRID] stop policy unreadable: {exc}")
        return {"mode": "unknown", "detail": f"{exc}",
                "fixed_default_pct": GRID_STOP_LOSS_PCT}


def _rotation_env_enabled():
    """What GRID_AUTO_ROTATE reads, reported rather than assumed.

    It defaults to ON when unset, which is why it must be shown beside
    the DB switch instead of being left to inference.
    """
    try:
        import coin_rotation as _r
        return bool(_r.auto_rotate_enabled())
    except Exception:
        return None


def _allocation_backing_block(branches, wallet_cash, usd_on_hold=None):
    """Never lets a reporting problem look like a clean balance sheet."""
    try:
        import allocation_backing
        return allocation_backing.backing(branches, wallet_cash, usd_on_hold)
    except Exception as exc:
        log.warning(f"[GRID] allocation backing check failed: {exc}")
        return {"verdict": "unknown", "detail": f"backing check failed: {exc}"}


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
    # The real wallet balance, read in the SAME session as the prices so
    # the backing check below compares two figures taken at one moment
    # rather than two reads a market move apart. None on failure - the
    # check reports "unknown" rather than inventing a clean balance sheet.
    wallet_cash_usd = None
    async with engine.aiohttp.ClientSession() as session:
        for product_id in distinct_products:
            price, _atr = await engine.get_price_and_volatility(session, product_id)
            live_prices[product_id] = price
        # USD LOCKED against the fleet's own resting orders. get_usd_balance
        # returns AVAILABLE only, which is right for sizing a buy and wrong
        # for backing: held money is still the account's, and the branch that
        # committed it still counts it as unspent budget. Read here so the
        # backing check can count it - see allocation_backing.backing().
        # Best-effort: a failed read leaves it None, which counts as 0.0 and
        # simply reproduces the old, conservative figure rather than
        # inventing backing that was not confirmed.
        usd_on_hold = None
        try:
            import account_census
            _bal = await account_census.fetch_balances(session)
            if _bal and _bal.get("available"):
                # fetch_balances' "held" map is TOTAL (available + hold), not
                # the hold portion - its own line is `total = avail + hold`.
                # Passing it straight through would add the available balance
                # to itself and inflate backing, which is the one direction a
                # backing check must never err in: it would hide a real hole
                # rather than report a false one.
                _total = (_bal.get("held") or {}).get("USD")
                _avail = (_bal.get("available_units") or {}).get("USD")
                if _total is not None and _avail is not None:
                    usd_on_hold = max(0.0, float(_total) - float(_avail))
        except Exception as _e:
            log.info(f"[GRID] USD-on-hold unavailable ({type(_e).__name__}) - "
                     f"backing will count available cash only")
        try:
            wallet_cash_usd, _wallet_err = await engine.get_usd_balance(session)
        except Exception as exc:
            log.warning(f"[GRID] status: wallet balance unreadable ({exc})")
        stop_by_product = {}
        for product_id in distinct_products:
            stop_by_product[product_id] = await _resolve_branch_stop(session, product_id)
        # ONCE for the whole status read, not per branch: it is one DB row and
        # every branch resolves against the same answer. Fails to "off" inside
        # adopted_stop_mode, so a read failure here cannot report a stop that
        # the cycle is not applying.
        _adopted_mode = await adopted_stop_mode()

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
                # MARKED AGAINST WHAT WAS PAID, SAME AS THE GATE THAT SELLS IT.
                #
                # This marked every slice against s.entry_price. For an
                # ADOPTED slice that is the adoption-day price coin_adoption
                # wrote, not a purchase - and _pick_parked_slice_to_sell()
                # has read sell_basis_for_slice() since the declared basis
                # shipped. So the gate was calling ZEC +30.95% while this
                # line showed the same three slices at -19.77%, and the
                # account owner was reading the second number:
                #
                #   slice 53  0.07351807   shown -$24.87   really +$22.66
                #   slice 57  0.26126214   shown -$86.13   really +$80.54
                #   slice 68  0.04338645   shown -$11.99   really +$13.37
                #
                # $239.57 of the headline -$405.90 was this one substitution.
                #
                # THE DRAWDOWN BREAKER IS DELIBERATELY NOT CHANGED.
                # _grid_branch_real_equity() still measures against
                # s.entry_price, so the breaker and peak_equity ratchet see
                # exactly what they saw before this. Marking the position
                # higher would make the breaker LESS likely to trip, and a
                # safety limit does not get loosened as a side effect of a
                # display fix. The conservative reading stays on the control;
                # the honest one goes on the page.
                _mark_basis = sell_basis_for_slice(s, product_id=b.product_id)
                net_usd = _grid_slice_net_pnl(s.qty, _mark_basis, current_price,
                                              _slice_rate(s, status_fee_rate, status_exit_leg_rate))
                cost_basis = s.qty * _mark_basis
                net_pct = (net_usd / cost_basis) if cost_basis else None
                total_net_usd += net_usd
                total_cost_basis += cost_basis
            slices_out.append({
                # THE PRIMARY KEY, WITHOUT WHICH RECONCILE CANNOT WRITE.
                #
                # slice_reconcile builds its actions as {"slice_id": r.get("id"),
                # ...} and the reconcile endpoint then looks the row up by it.
                # This serialiser never served the id, so every action carried
                # slice_id=None, every lookup matched nothing, and the whole
                # apply path deleted and reduced exactly zero rows while
                # reporting each branch as corrected. A field that is stored
                # but not served is indistinguishable from one that was never
                # written - the comment three lines down says so about
                # order_side, and the same trap caught this.
                "id": getattr(s, "id", None),
                "entry_price": s.entry_price, "qty": s.qty,
                # Which price the mark beside it was measured against, so a
                # reader never has to guess why entry_price and
                # unrealized_net_usd disagree. Equal to entry_price for every
                # slice the grid bought and every product with no declared
                # basis - which is nearly all of them.
                "marked_against": round(_mark_basis, 8) if current_price is not None else None,
                "marked_against_is_declared": (
                    current_price is not None and _mark_basis != s.entry_price),
                "opened_at": (s.opened_at.isoformat() + "Z") if s.opened_at else None,
                "unrealized_net_usd": round(net_usd, 2) if net_usd is not None else None,
                "unrealized_net_pct": round(net_pct, 4) if net_pct is not None else None,
                # Carried so allocation_backing can deduct the commission this
                # slice's BUY leg already paid out of the wallet. Without these
                # two fields slice_entry_commission() returns None for every
                # slice and the deduction is INERT - the fix would ship, the
                # tests would pass, and the live gap would not move a cent.
                # adopted matters just as much: an adopted slice paid no entry
                # commission, and charging one would invent a hole the size of
                # the fleet's whole adopted inventory.
                # ---- §1 state, so the WRITE can be verified, not assumed ----
                #
                # 5b9d62d started stamping these. A write nobody can read is
                # the same trap as a protection nobody can observe - and both
                # real bugs found today came from shipping observability and
                # then LOOKING. So they are served, and getattr with a None
                # default because a row written before the columns existed
                # genuinely has no state: NULL is UNKNOWN, never a default.
                "slice_state": getattr(s, "slice_state", None),
                "cycle_id": getattr(s, "cycle_id", None),
                "slice_index": getattr(s, "slice_index", None),
                # What ACTUALLY filled on the order working against this
                # slice, never what was asked for - §15's whole point, and
                # what makes a PARTIAL distinguishable from a whole one here.
                "filled_quantity": getattr(s, "filled_quantity", None),
                "average_fill_price": getattr(s, "average_fill_price", None),
                "execution_reason": getattr(s, "execution_reason", None),
                # WRITTEN SINCE 5b9d62d AND NEVER SERVED. The first slice §1
                # ever stamped (BTC-USD, 2026-09-29T15:55:45Z) came back with
                # every field populated except this one, which reads as a
                # failed write and is not: the insert passes order_side="BUY",
                # the column holds it, and only this serialiser was missing it.
                # A field that is stored but not served is indistinguishable
                # from one that was never written.
                "order_side": getattr(s, "order_side", None),
                "entry_fee_rate": getattr(s, "entry_fee_rate", None),
                "adopted": bool(getattr(s, "adopted", False)),
            })
        total_net_pct = (total_net_usd / total_cost_basis) if (current_price is not None and total_cost_basis) else None

        # Resolved per BRANCH, not per product: the override lives on the
        # branch and is what the trading loop actually obeys.
        _branch_stop = _reported_stop(b, stop_by_product.get(b.product_id),
                                      adopted_mode=_adopted_mode)

        out.append({
            "bot_name": b.bot_name, "product_id": b.product_id, "allocated_usd": round(b.allocated_usd, 2),
            "active": b.active, "locked": bool(b.locked), "grid_pct": b.grid_pct, "num_levels": b.num_levels,
            # The stop this branch really trades under. A safety setting
            # that cannot be inspected is one nobody can trust: adopted
            # branches carry 0 so an 8% wobble cannot liquidate coin the
            # owner has held for a year, and that has to be verifiable
            # from outside rather than taken on faith.
            "stop_loss_pct_override": getattr(b, "stop_loss_pct_override", None),
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
            # The stop distance THIS branch trades under, and where it came
            # from - fixed, per-coin override, or scaled to its own
            # volatility. stop_pct 0.0 means the branch has no stop at all.
            "stop_pct": _branch_stop.get("stop_pct"),
            "stop_source": _branch_stop.get("source"),
            "stop_reason": _branch_stop.get("reason"),
            "stop_daily_vol_pct": _branch_stop.get("daily_vol_pct"),
            # Sell-only: set at adoption on a position over the 20% rule.
            # The branch may sell its slices and never buys back, so the
            # concentration walks down through strength. Serialized because
            # a rule nobody can see from outside is a rule on trust.
            "buys_paused": bool(getattr(b, "buys_paused", False) or False),
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

    # TWO BOOKS, AND BLENDING THEM READS AS A FAILURE THAT DID NOT HAPPEN.
    #
    # An adopted slice is coin the account ALREADY HELD, written by
    # coin_adoption_worker at the market price on the day it was adopted -
    # no order, no commission, and a cost basis set long before by a
    # decision this grid never made (see slice_paid_no_entry_fee). A slice
    # the grid BOUGHT is one it chose, at a price its own rules picked.
    #
    # Measured live 2026-10-04 across 52 open slices: the 26 adopted ones
    # carried -$459.13 and the 26 the grid bought carried -$77.34. So 85.6%
    # of the headline mark belongs to inventory the grid inherited. ZEC is
    # the whole story on its own - five adopted slices near $1,655 are
    # -$412.50, while the single slice the grid chose to buy at $1,586.44 is
    # -$11.53. Against banked profit of +$135.58 the grid's OWN complete
    # book is +$58.24, not the -$405 the blended figure shows.
    #
    # Reported separately, never instead: total_unrealized_net_usd keeps its
    # exact meaning and value for every existing caller. These two sum to it
    # whenever both are known.
    _own = _adopted = 0.0
    _split_known = total_unrealized_known
    for _b in branches_with_slices:
        for _s in (_b.get("slices") or []):
            _v = _s.get("unrealized_net_usd")
            if _v is None:
                _split_known = False
                continue
            if _s.get("adopted"):
                _adopted += float(_v)
            else:
                _own += float(_v)
    unrealized_own_usd = round(_own, 2) if _split_known else None
    unrealized_adopted_usd = round(_adopted, 2) if _split_known else None

    return {
        "fleet_name": "Adaptive Capital Fleet",
        "mode_active": mode_active,
        # The grid's OWN open inventory, and the coin it merely inherited.
        # See the comment above total_unrealized_net_usd: these are different
        # books and only one of them reflects a decision the grid made.
        "unrealized_own_usd": unrealized_own_usd,
        "unrealized_adopted_usd": unrealized_adopted_usd,
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
        "auto_reanchor_active": auto_reanchor_enabled(),
        "reanchor_interval_minutes": GRID_REANCHOR_INTERVAL_SECONDS // 60,
        "adaptive_fleet": await get_adaptive_fleet_status(),
        "drawdown_breaker_pct": GRID_DRAWDOWN_BREAKER_PCT,
        "branch_count": len(branches),
        "branches_with_open_slices": len(branches_with_slices),
        "total_allocated_usd": round(total_allocated, 2),
        "total_unrealized_net_usd": total_unrealized_net_usd,
        "real_free_cash_usd": await get_real_free_cash_usd(),
        # Is the allocated figure above actually backed by anything? A
        # branch's allocated_usd is a CLAIM; only coin it really bought
        # and USD really in the wallet stand behind it. The account owner
        # found the gap by underlining a subtitle - it gets its own field
        # now. See allocation_backing.py for the arithmetic.
        # BOTH rotation gates, because reporting one of them was the bug.
        # auto_rotate_active gates the scheduled sweep; the env var gates
        # the post-sale path inside the main cycle and DEFAULTS TO ON.
        # A reader who saw only the first was told rotation was off while
        # a branch going flat could still be re-pointed.
        "auto_rotate_env_enabled": _rotation_env_enabled(),
        "auto_rotate_fully_off": (not await is_grid_auto_rotate_active()),
        "allocation_backing": _allocation_backing_block(out, wallet_cash_usd, usd_on_hold),
        # The stop configuration actually in force, and what it resolves to
        # per branch. Exposed because "did that environment variable take"
        # was otherwise only answerable by watching an uptime counter.
        "stop_policy": _stop_policy_block(),
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
        "real_maker_fee_rate": await get_effective_maker_leg_fee_rate(),
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
        # The adopted catastrophe stop, and which switch decided it. Reported
        # for the same reason maker_only_source is: this one can SELL, and a
        # setting whose source the page cannot name is one nobody can turn off
        # again. _adopted_mode was resolved once, above.
        "adopted_stop_mode": _adopted_mode,
        "adopted_stop_source": await adopted_stop_mode_source(),
        # The theoretical figure above is step - (fees + adverse selection).
        # This is the same question answered from real fills, so the page can
        # stop presenting an estimate in the voice of a measurement.
        "realized_edge": await _never_fails(get_realized_edge, "realized_edge"),
        # The ledger of trades that did NOT happen: what price did after each
        # cancelled post-only order. The skip COUNT above says what this mode
        # costs; this says whether that cost bought anything.
        "maker_expiry_drift": await _never_fails(get_maker_expiry_drift, "maker_expiry_drift"),
        # Short-horizon opportunity scoring, and whether it has predicted
        # anything yet. Observation only - see opportunity_signals.
        "short_term_signals": await _never_fails(signals.summary, "short_term_signals"),
        # The end-to-end funnel, so "zero trades" names its own bottleneck
        # instead of leaving it to be inferred from six scattered numbers.
        "pipeline": await _never_fails(get_pipeline_funnel, "pipeline"),
        # Which products are refusing to place an order right now, and why.
        # Without this the fail-closed sizing refusal shipped in 6a95d4d
        # was invisible: nothing read-only exposed engine._last_order_error
        # for the grid fleet at all.
        "order_refusals": await _never_fails(get_order_refusals, "order_refusals"),
        # WHAT CHANGED, rather than what is true. Everything else here reads
        # the present; this says the moment a coin crossed into or out of
        # being worth trading, and whether past crossings paid.
        "regime": await _never_fails(signals.regime_summary, "regime"),
        # The same question the signal ledger answers, asked of a horizon the
        # ledger cannot reach. Reads the last STORED run - the study itself
        # runs out of band, so this is a database row, not a measurement.
        "horizon": await _never_fails(horizon_study.latest, "horizon"),
        "floor_priced_against": ("maker (the market fallback is removed)"
                                 if _maker_only and (await get_effective_maker_leg_fee_rate()) is not None
                                 else "taker (an unfilled maker order still becomes a market order)"),
        "branches": out,
    }



# When the CURRENT configuration began. Everything closed before this is a
# different experiment wearing the same ledger.
#
# The 50 completed round trips in this book were taken on DOGE/ETH/STX/WIF/
# ETC/LINK/LTC/ATOM/AAVE/ARB/BCH, at a 2.00% step, with the market fallback
# still active - none of those coins is in the fleet now, the step is 2.50%,
# and maker-ONLY has removed the fallback. Pooled with the current six they
# produced a confident "+1.95% realized per cycle" for a configuration that
# has completed ZERO cycles. That is not a stale label, it is a number that
# actively misleads: it is green, it is large, and it describes nothing that
# is running.
#
# Tunable because it must be re-baselined the next time the step, the fee
# path or the coin list changes. A cohort boundary that is not moved when the
# configuration moves becomes the same bug again.
GRID_CONFIG_EPOCH = os.getenv("GRID_CONFIG_EPOCH", "2026-09-26T02:45:00Z")


def _config_epoch() -> datetime:
    try:
        return datetime.fromisoformat(GRID_CONFIG_EPOCH.replace("Z", "")).replace(tzinfo=None)
    except Exception:
        log.warning(f"[GRID] GRID_CONFIG_EPOCH={GRID_CONFIG_EPOCH!r} is unparseable; "
                    f"treating every trade as RETIRED so nothing stale can be "
                    f"reported as current")
        # Fail toward "none of it is current". The failure that matters is
        # old trades being counted as new, so an unreadable epoch must never
        # widen the current cohort.
        return datetime.max


def _edge_cohort(rows) -> dict:
    """Realized edge for one set of completed round trips.

    Notional-weighted: gross and net share ONE denominator, total entry
    notional. An unweighted mean of percentages beside a weighted total
    produces an "implied cost" that is not any real cost - on this book the
    two framings differ by half a point.
    """
    usable = [r for r in rows
              if None not in (r.entry_price, r.exit_price, r.qty, r.pnl)
              and r.entry_price and r.qty]
    if not usable:
        return {"trades": 0}
    notional = sum(r.entry_price * r.qty for r in usable)
    gross = sum((r.exit_price - r.entry_price) * r.qty for r in usable)
    net = sum(r.pnl for r in usable)
    closes = sorted(r.closed_at for r in usable if r.closed_at)
    span = ((closes[-1] - closes[0]).total_seconds() / 86400) if len(closes) > 1 else 0.0
    return {
        "trades": len(usable),
        "coins": sorted({r.product_id for r in usable if r.product_id}),
        "notional_usd": round(notional, 2),
        "mean_slice_usd": round(notional / len(usable), 2),
        "gross_pct": round(gross / notional * 100, 3),
        "net_pct": round(net / notional * 100, 3),
        "net_usd": round(net, 2),
        # Named "implied", never "measured adverse selection". It is computed
        # over COMPLETED round trips, and adverse selection is precisely the
        # cost that lands on the ones that never complete - a slice bought
        # into a move that kept going sits open and contributes nothing here.
        # A realized cost BELOW the assumption is survivorship, not good news.
        "implied_cost_pct": round((gross - net) / notional * 100, 3),
        "closes_per_day": round(len(closes) / span, 2) if span >= 1 else None,
        # Clamped at 0: a close timestamped slightly ahead of this clock
        # (a DB written by another process, a skewed container) would
        # otherwise render as "-0d since the last close", which reads as a
        # glitch on the one figure that has to be trusted at a glance.
        "days_since_last_close": max(0.0, round(
            (datetime.utcnow() - closes[-1]).total_seconds() / 86400, 1)) if closes else None,
        "stop_loss_closes": sum(1 for r in usable if r.exit_reason == "stop_loss"),
    }


async def get_realized_edge(days: int = None) -> dict:
    """What the closed book actually earned - split into the configuration
    running NOW and the one that is not.

    Three things this deliberately does not do.

    It does not pool the cohorts. See GRID_CONFIG_EPOCH: pooled, the retired
    book reports +1.95% per cycle for a fleet that has completed none.

    It does not present implied_cost_pct as a better adverse-selection
    estimate, and no caller may either - it is conditioned on completion,
    which removes the phenomenon being estimated.

    It does not call a positive margin success. Per-cycle margin can stay
    healthy while the account goes nowhere, because the losers stay open and
    out of this table entirely. That is why days_since_last_close sits beside
    the margin and not underneath it: this fleet realised +1.95% a cycle at
    6.25 closes a day for eight days, then closed nothing for seventeen.
    """
    since = datetime.utcnow() - timedelta(days=days) if days else None
    try:
        async with get_session_factory()() as db:
            q = select(CryptoGridTradeHistory)
            if since is not None:
                q = q.where(CryptoGridTradeHistory.closed_at >= since)
            rows = (await db.execute(q)).scalars().all()
    except Exception as e:
        return {"available": False, "error": f"{type(e).__name__}: {e}"}

    epoch = _config_epoch()
    current = [r for r in rows if r.closed_at and r.closed_at >= epoch]
    retired = [r for r in rows if not r.closed_at or r.closed_at < epoch]
    cur = _edge_cohort(current)
    return {
        "available": True,
        "config_epoch": GRID_CONFIG_EPOCH,
        # The cohort that describes what is running. Everything a decision
        # should be based on comes from here.
        "current": cur,
        # Kept, because it is real evidence about grids in general. Labelled,
        # because it is not evidence about this one.
        "retired": _edge_cohort(retired),
        "current_has_baseline": cur["trades"] >= 20,
        "headline": (
            f"current configuration: {cur['trades']} completed cycle"
            f"{'' if cur['trades'] == 1 else 's'}"
            + (" - no baseline yet, nothing here describes what this fleet earns"
               if cur["trades"] < 20 else "")),
        "survivorship_warning": (
            "completed round trips only; slices still open are not here, and "
            "those are where adverse selection actually lands"),
    }



# Wall-clock ceiling for the whole telemetry pass, and it is load-bearing.
#
# Scoring costs three network reads per coin (candles, book, hourly swing)
# and resolution costs one per unresolved product. Each carries a 15s
# timeout. Six coins is 18 reads = up to 270s, plus up to 120s of resolution,
# against a GRID_LEASE_STALE_SECONDS of 180 - so one bad-network cycle could
# hold the loop past its own lease while another process decided the owner
# had died.
#
# Nothing here is urgent. Any coin not scored this cycle is scored on the
# next one ~37s later, and any horizon not resolved stays pending. So the
# pass simply stops when the budget is spent. A partial pass is correct; a
# stalled trading loop is not.
TELEMETRY_BUDGET_SECONDS = float(os.getenv("GRID_TELEMETRY_BUDGET_SECONDS", "25"))


async def _score_short_term_opportunities(session, branches, deadline=None):
    """Score each branch's coin once per cycle and write the prediction down.

    The ECONOMICS come from crypto_nine_coin_scanner.evaluate_grid_step - the
    same function the live net-edge gate calls - asked whether a step the
    size of the expected capturable move would clear. One formula, one source
    of truth. This loop contributes only the short-horizon shape the gate
    does not measure: momentum, volume and pullback quality.

    Never raises, never trades, never informs a trade. Two reads per coin per
    cycle; any single coin failing just skips that coin.
    """
    if not branches:
        return
    import crypto_nine_coin_scanner as _scan
    fee_rt = (await worst_case_leg_fee_rate()) * 2
    for branch in branches:
        if deadline is not None and time.time() >= deadline:
            log.debug("[SIGNAL] budget spent; remaining coins score next cycle")
            return
        pid = getattr(branch, "product_id", None)
        try:
            # Ask BEFORE spending three network calls. The throttle used to
            # gate only the row write, so the fetches ran every cycle and
            # their results were discarded seven times out of eight.
            if not await signals.due_for_score(pid):
                continue
            candles = await signals.fetch_candles_with_volume(session, pid)
            if not candles:
                continue
            closes, highs, lows, volumes = candles
            bid, ask, bid_depth, ask_depth = await engine.get_book_top_and_depth(session, pid)
            if bid is None or ask is None:
                continue
            mid = (bid + ask) / 2.0
            # UNITS. This is the boundary, and it has already bitten once.
            # _atr_pct_from_candles returns a FRACTION (atr / price) despite
            # the _pct in its name, and crypto_nine_coin_scanner speaks
            # fractions throughout too - it prints net_edge_pct * 100. But
            # opportunity_signals works in PERCENT: its bands, its 1.37%
            # pullback threshold, its momentum returns and the resolver's
            # realised moves are all percent.
            #
            # Mixed, the first live read showed BTC at "0.001% ATR", every
            # volatility sub-score clamped to zero, and - worse, because it
            # is silent - materialized comparing a percent against a fraction
            # and net_after_costs subtracting one from the other. Confident,
            # meaningless numbers.
            #
            # So: convert to percent HERE, once, and convert back only when
            # handing a step to the gate.
            atr_frac = engine._atr_pct_from_candles(closes, highs, lows)
            atr_pct = atr_frac * 100.0 if atr_frac else None
            slice_usd = (branch.allocated_usd or 0) / max(branch.num_levels or 1, 1)

            # Ask the LIVE gate about a step the size of the move we expect to
            # capture. Not a re-implementation of it - the function itself.
            #
            # The step asked about is the MOMENTUM estimate, not ATR: a grid
            # needs to know whether a move continues, which is a question
            # about direction, and ATR is undirected. score() computes the
            # same number and records the ATR alternative beside it, so the
            # ledger can say later which one was closer. Computed here too,
            # from the same closes, because the gate has to be asked before
            # score() runs.
            _r15 = signals.momentum(closes).get("ret_15m_pct")
            _step_pct = abs(_r15) * 0.5 if _r15 is not None else (
                (atr_frac or 0) * 100.0 * 0.5)
            economics = None
            _why = None
            if _step_pct > 0:
                swing = await engine.get_average_hourly_swing_pct(session, pid)
                _gate_ok, _why, detail = _scan.evaluate_grid_step(
                    pid, _step_pct / 100.0, swing, best_bid=bid, best_ask=ask,
                    bid_depth_usd=bid_depth, ask_depth_usd=ask_depth,
                    slice_usd=slice_usd, fee_round_trip=fee_rt)
                detail = dict(detail or {})
                if detail.get("net_edge_pct") is not None:
                    detail["net_edge_pct"] = detail["net_edge_pct"] * 100.0
                economics = dict(detail, slice_usd=slice_usd)

            scored = signals.score(
                closes=closes, highs=highs, lows=lows, volumes=volumes,
                spread_pct=((ask - bid) / mid * 100.0 if mid else None),
                bid_depth_usd=bid_depth, ask_depth_usd=ask_depth,
                atr_pct=atr_pct, rsi=engine._rsi_from_closes(closes),
                economics=economics, gate_reason=_why)
            # One call: detection and recording share the throttle, so a
            # crossing is a change between consecutive OBSERVATIONS rather
            # than eight comparisons against the same stale row.
            await signals.observe(pid, branch.bot_name, mid, scored)
        except Exception as e:
            log.debug(f"[SIGNAL] {pid or '?'} not scored: "
                      f"{type(e).__name__}: {e}")



# THE REFUSAL THAT NOBODY COULD SEE.
#
# 6a95d4d made an unreadable product REFUSE to size an order instead of
# falling back to 8 decimals - the right direction, and what §22 asks for.
# But the signal it writes, engine._last_order_error, was exposed by no
# read-only endpoint at all: line 1364 of the dashboard router covers
# family-tree branches only, and the other reader is inside a
# write-guarded POST. So the fleet could have been refusing every order on
# every product and the page would have shown nothing missing.
#
# A protection whose firing cannot be observed is indistinguishable from a
# protection that never fires. That is the whole reason this exists.
#
# UNKNOWN IS NOT ZERO. If the engine's dicts cannot be read, this reports
# available=False, never "no refusals". An empty dict that WAS read is the
# real and ordinary answer "nothing is being refused right now", and the
# two must not look alike - the same distinction restart_recovery draws
# between an unread balance and a balance of zero.
_RULES_REFUSAL = "product rules unreadable"


async def get_order_refusals() -> dict:
    """Which products are currently refusing to place an order, and why.

    Read-only telemetry over the engine's own per-product last-error map.
    Nothing here decides anything; it exists so that a fail-closed refusal
    is visible while it is happening rather than inferred afterwards from
    an absence of trades.
    """
    errors = getattr(engine, "_last_order_error", None)
    if not isinstance(errors, dict):
        # The map itself is the thing that could not be read. Saying
        # "available: False" is the honest answer; reporting zero refusals
        # would be a measurement nobody took.
        return {"available": False,
                "error": "engine._last_order_error is not readable"}

    tracked = set()
    try:
        for b in await get_grid_branches():
            if getattr(b, "active", False) and getattr(b, "product_id", None):
                tracked.add(b.product_id)
    except Exception as e:
        # The branch list is a DB read and may fail on its own. The error
        # map is still worth reporting, so this degrades to "every product
        # the map knows about" and SAYS that it did, rather than silently
        # reporting a narrower set as if it were the fleet.
        log.warning(f"[GRID] order-refusal scope unreadable: {type(e).__name__}: {e}")
        tracked = None

    by_product, reasons = {}, {}
    rules_refusals = []
    for product_id, why in list(errors.items()):
        if tracked is not None and product_id not in tracked:
            continue
        if not why:
            continue
        text = str(why)
        by_product[product_id] = text
        # The code before the first colon, which is how the engine writes
        # these ("NOTHING_TO_SELL: ...", "INSUFFICIENT_FUND: ..."). A
        # message with no colon is its own whole reason rather than being
        # dropped into an "other" bucket that hides it.
        code = text.split(":", 1)[0].strip() or text
        reasons[code] = reasons.get(code, 0) + 1
        if _RULES_REFUSAL in text:
            rules_refusals.append(product_id)

    return {
        "available": True,
        # True only because the map was read and held nothing for the
        # fleet - not because nothing was looked at.
        "scope": ("active grid branches" if tracked is not None
                  else "every product in the engine's error map (branch list unreadable)"),
        "products_refusing": len(by_product),
        "by_reason": reasons,
        "by_product": by_product,
        # Called out separately because it is the one this fleet was told
        # to watch: it means the venue's products endpoint could not be
        # read, so sizing refused rather than guessing an increment. A few
        # are ordinary transient failures; many, or persistent ones, mean
        # the endpoint is flaky and the owner should hear about it.
        "product_rules_unreadable": sorted(rules_refusals),
        "product_rules_unreadable_count": len(rules_refusals),
    }


async def _never_fails(fn, label: str) -> dict:
    """Run a telemetry builder, or report why it could not run.

    Every one of these had its DB read inside a try and its arithmetic
    outside it, so a None reaching a round() or an empty sequence reaching a
    max() would propagate out of get_grid_fleet_status() and take down the
    whole payload - the dashboard, the runner panel and the watch - over a
    diagnostic nobody trades on.

    Telemetry may be absent. It may not be load-bearing.
    """
    try:
        return await fn()
    except Exception as e:
        log.warning(f"[GRID] {label} unavailable this cycle: {type(e).__name__}: {e}")
        return {"available": False, "error": f"{type(e).__name__}: {e}"}



async def _per_coin_execution() -> dict:
    """Orders, fills, expiries and completions PER COIN, current config only.

    The fleet-wide fill_mix and skip counters cannot answer this: they are
    single totals spanning both cohorts. These come from rows that each
    carry their own product_id and timestamp, so they can be split by coin
    and bounded to the current configuration - which is what makes the
    matrix comparable across coins instead of one number repeated six times.

    filled counts slices that actually opened: the ones still open plus the
    ones that have since closed. A fill is a fill whether or not it has
    found its exit yet, and counting only closed ones would report the exit
    problem as an execution problem.
    """
    epoch = _config_epoch()
    out = {}
    try:
        async with get_session_factory()() as db:
            for row in (await db.execute(select(CryptoGridSlice))).scalars().all():
                if row.opened_at and row.opened_at >= epoch and row.product_id:
                    e = out.setdefault(row.product_id, {"filled": 0, "expired": 0, "completed": 0})
                    e["filled"] += 1
            for row in (await db.execute(select(CryptoGridTradeHistory))).scalars().all():
                if row.closed_at and row.closed_at >= epoch and row.product_id:
                    e = out.setdefault(row.product_id, {"filled": 0, "expired": 0, "completed": 0})
                    e["completed"] += 1
                    e["filled"] += 1
            # NON-ORDERS NOW LIVE IN THEIR OWN TABLE, so this reads both.
            #
            # New blocked cycles are written to grid_order_not_placed. The
            # order_rested is False rows below are the legacy ones, written
            # between the flag shipping and the split; dropping them would
            # silently lose that history, and double-counting is impossible
            # because no row is ever written to both.
            for row in (await db.execute(select(GridOrderNotPlaced))).scalars().all():
                if row.blocked_at and row.blocked_at >= epoch and row.product_id:
                    e = out.setdefault(row.product_id, {"filled": 0, "expired": 0,
                                                        "completed": 0, "no_order": 0})
                    e["no_order"] += 1
            for row in (await db.execute(select(GridMakerExpiry))).scalars().all():
                if row.expired_at and row.expired_at >= epoch and row.product_id:
                    e = out.setdefault(row.product_id, {"filled": 0, "expired": 0,
                                                        "completed": 0, "no_order": 0})
                    # AN EXPIRY IS ONLY AN ATTEMPT IF SOMETHING WAS ATTEMPTED.
                    #
                    # Every row used to count here, and `attempted` below is
                    # filled + expired. So a coin that placed no orders at all
                    # showed thousands of attempts - ALGO and QNT between them
                    # were adding ~2,600 a day - and the funnel read as a fleet
                    # working hard and getting no fills, when the truth was
                    # that no order was ever sent. That is the opposite
                    # diagnosis, and it is the one number the owner reads to
                    # find the bottleneck.
                    #
                    # UNKNOWN (None) still counts as an attempt: those rows
                    # predate order_rested and may well have been real rests.
                    # Only a confirmed False is excluded.
                    if row.order_rested is False:
                        e["no_order"] += 1
                    else:
                        e["expired"] += 1
    except Exception as e:
        log.debug(f"[GRID] per-coin execution counts unavailable: {type(e).__name__}: {e}")
        return {}
    for e in out.values():
        # Kept as filled + expired. It does not fold in no_order, because a
        # cycle that never placed an order is not an attempt that failed -
        # it is an attempt that never happened, and the two want different
        # fixes. Reported alongside so it is visible rather than deleted.
        e.setdefault("no_order", 0)
        e["attempted"] = e["filled"] + e["expired"]
    return out


async def get_pipeline_funnel() -> dict:
    """Where the opportunity pipeline actually leaks, end to end.

    Assembled from what each stage already records rather than a new
    counter, so it cannot drift from the thing it describes:

        scans        every short-term score written
        qualified    scans whose NET EDGE cleared, from the live gate
        attempted    real orders placed - fills plus expiries
        filled       maker legs that actually filled
        expired      post-only orders cancelled unfilled
        completed    round trips closed on this configuration

    Each stage answers a different question, and only one of them is about
    the strategy:

        many scans, few qualified   -> the movement is not there, or the
                                       cost model is too strict. rejected_by
                                       says which.
        qualified but not attempted -> price never reached a grid trigger.
                                       The scan and the trigger are separate
                                       gates and both must open.
        attempted but not filled    -> maker-only is choking execution.
        filled but not completed    -> the exit is not being reached.

    Every number here is read, never computed twice. If a source is
    unavailable it is None, not zero - "no data" and "none happened" are
    different answers and collapsing them is how a blocked pipeline reads
    as an idle one.
    """
    sig = await signals.summary()
    _exec_counts = await _per_coin_execution()
    skips = await get_maker_only_skips()
    mix = (await get_fill_mix() or {}).get("overall") or {}
    edge = await get_realized_edge()
    expired = (skips or {}).get("total")
    filled = mix.get("maker_legs")
    attempted = (filled + expired) if (filled is not None and expired is not None) else None
    return {
        "scans": sig.get("scored") if sig.get("available") else None,
        "qualified": sig.get("would_trade_count") if sig.get("available") else None,
        # All-time, both cohorts: fill_mix and the skip counters have been
        # running since long before GRID_CONFIG_EPOCH, while scans and
        # completed are current-configuration only. Labelled rather than
        # silently mixed - that mixing is the exact fault the cohort split
        # was built to end.
        "attempted": attempted,
        "filled": filled,
        "expired": expired,
        "attempted_basis": "all-time (fill mix and skip counters predate the config epoch)",
        "completed": (edge.get("current") or {}).get("trades") if edge.get("available") else None,
        # The matrix: one row per coin, current configuration only, so the
        # stage that is losing opportunities can be read per coin rather
        # than inferred from a fleet total.
        "per_coin": {
            c: dict(f, **_exec_counts.get(c, {}))
            for c, f in (sig.get("per_coin") or {}).items()
        } if sig.get("available") else {},
        "top_rejection": sig.get("top_rejection"),
        "rejected_by": sig.get("rejected_by"),
        # The stage that is actually blocking, named rather than inferred by
        # whoever reads the numbers.
        "bottleneck": _funnel_bottleneck(sig, attempted, filled,
                                         (edge.get("current") or {}).get("trades")),
    }


def _funnel_bottleneck(sig, attempted, filled, completed) -> str:
    """The first stage that lost everything. Checked in pipeline order, so
    the answer is the EARLIEST blockage rather than the last empty number -
    a pipeline blocked at the first stage is also empty at every later one,
    and reporting the last would send the fix to the wrong place."""
    if not sig.get("available") or not sig.get("scored"):
        return "nothing scored yet - the scanner has not run"
    if not sig.get("would_trade_count"):
        return (f"qualification: {sig.get('scans') or sig.get('scored')} scans, 0 cleared the "
                f"net-edge gate. Top refusal: {sig.get('top_rejection') or 'unknown'}")
    if not attempted:
        return ("triggering: setups qualified but price never reached a grid "
                "trigger - the scan and the trigger are separate gates")
    if not filled:
        return "execution: orders were placed and none filled - maker-only is choking"
    if not completed:
        return "exit: slices filled but none has closed a round trip yet"
    return "none - the pipeline is completing round trips"


async def close_all_grid_slices(only_bot_name: str = None,
                                only_product_id: str = None,
                                only_slice_ids=None,
                                exit_reason: str = "close_all",
                                allow_unverified_balance: bool = True) -> dict:
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
    and reported separately.

    THREE PARAMETERS MAKE THIS REUSABLE BY THE ROTATION, all defaulting to
    exactly today's close-all behaviour so that path is byte-identical:

      only_slice_ids   settle a SUBSET of a branch's slices instead of all
                       of them. This is what the concentration rotation
                       needs, and handing it this function rather than a
                       parallel sell path is the whole point: the slice row
                       is retired, allocated_usd is written back, the trade
                       history row is written and the reference price moves,
                       all by the code that already does it. The first
                       rotation worker did none of that - it incremented a
                       counter and logged a line, and every "successful"
                       rotation would have widened the $1,168.59 gap between
                       what the books claim and what the wallet holds.

      exit_reason      so the ledger can tell a rotation from a close-all.
                       Both are market exits at the taker rate, and a column
                       that calls them the same thing cannot answer "did
                       rotating actually pay?".

      allow_unverified_balance
                       close-all is a PROTECTION and fails OPEN: leaving a
                       live position unsold is the worse outcome, so it is
                       allowed to sell an unverified quantity. The rotation
                       is OPPORTUNISTIC, not a protection - there is always
                       a next pass - so it passes False and refuses rather
                       than sending an order sized off a number the wallet
                       could not confirm.

    `only_bot_name` / `only_product_id` narrow it to a single branch,
    which is what makes closing ONE position possible at all - there was
    no path to it before, and the only alternative was a raw Coinbase
    sell that would have left the branch rows behind claiming coin that
    no longer existed. Everything else about the close is identical, so
    one branch and the whole fleet cannot drift apart in how they book a
    sale. Both None means every branch, exactly as before."""
    branches = await get_grid_branches()
    if only_bot_name:
        branches = [b for b in branches if b.bot_name == only_bot_name]
    if only_product_id:
        branches = [b for b in branches if b.product_id == only_product_id]
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
            if only_slice_ids is not None:
                # Settle only the named rows. An id that is not on this
                # branch simply does not match - this never widens the set.
                _want = {str(x) for x in only_slice_ids}
                slices = [s for s in slices if str(s.id) in _want]
            if not slices:
                continue
            # Computed AFTER the filter, so a subset sells only its own
            # quantity. Reading this before the filter would have sold the
            # whole branch and settled a fraction of it - the exact books/
            # wallet divergence this function is being reused to avoid.
            total_qty = sum(s.qty for s in slices)
            # close-all is a PROTECTION, and protections fail open: if the
            # balance cannot be read here, leaving a live position open is
            # the worse outcome, so this is the one caller allowed to sell
            # an unverified quantity. Every other seller refuses.
            fill = await engine.place_market_sell(session, total_qty, b.product_id,
                                                  source="grid_close_branch",
                                                  allow_unverified_balance=allow_unverified_balance)
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
                    # A FORCED EXIT IS ITS OWN REASON, AND IT WAS KNOWN HERE.
                    #
                    # This call passed no exit_reason, so every close-all row
                    # landed as None - UNKNOWN - when the reason is not in
                    # doubt at all: the owner closed the branch. A close-all
                    # sale is a market exit at the taker rate and has nothing
                    # to do with a profit target or a stop, so leaving it
                    # unknown puts forced exits into the same bucket the
                    # column exists to keep them out of ("did the stop do its
                    # job, or did the grid sell badly?").
                    #
                    # The analysis fields travel too. They are on the slice
                    # right here, and a close-all row that omits them is
                    # needlessly less analysable than an ordinary one.
                    await _log_grid_trade(
                        b.bot_name, b.product_id, s.entry_price, filled_price,
                        s.qty, pnl, s.opened_at,
                        exit_reason=exit_reason,
                        mae_pct=getattr(s, "mae_pct", None),
                        mfe_pct=getattr(s, "mfe_pct", None),
                        entry_atr_pct=getattr(s, "entry_atr_pct", None),
                        entry_spread_pct=getattr(s, "entry_spread_pct", None),
                        entry_gate_json=getattr(s, "entry_gate_json", None))
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


# A capped list must be able to say it is capped. 50 was the only value this
# function ever served and nothing in the payload admitted it: with 132
# completed trades the window covered 2.5 days and hid 82 of them, while
# `recent_trades` read like the whole book. The ceiling exists because this is
# served in a live status payload, not because 50 is meaningful.
GRID_TRADE_HISTORY_MAX_ROWS = int(os.getenv("GRID_TRADE_HISTORY_MAX_ROWS", "1000"))


async def get_grid_performance_metrics() -> dict:
    """Win rate, profit factor and expectancy over the grid's own REAL
    completed trades. Read-only, one query.

    WHY THIS EXISTS. The Scale Bot panel's "Performance Tracking" box read
    its three numbers from somewhere else entirely: win_rate was summed off
    the FAMILY-TREE branches (2 of them, dormant, nothing allocated),
    expectancy came from crypto_family_tree_bot.get_rolling_expectancy()
    (-$5.29/trade), and profit_factor was the literal `profit_factor = 1.0
    # Neutral default` - never computed at all. The same panel's "Primary
    Profit" beside it already showed the GRID's $135.58, so one card was
    describing two different bots and the three stats belonged to the dead
    one.

    Measured over the grid's real 196 closed trades at the time this was
    written: 172 wins / 22 losses / 2 flat, +$156.00 gross win against
    $20.42 gross loss - 87.8% win rate, 7.64 profit factor, $0.6917
    expectancy.

    NONE, NEVER ZERO, when there is nothing to measure. "No completed
    trades" and "a 0% win rate" are different claims and must not render
    alike - the same doctrine get_rolling_expectancy already follows by
    returning expectancy None below its own minimum. profit_factor is
    likewise None when there are no losses at all: the ratio is undefined,
    and 1.0 would assert the strategy exactly broke even, which is the
    opposite of a flawless record."""
    async with get_session_factory()() as db:
        rows = (await db.execute(
            select(CryptoGridTradeHistory.pnl).where(
                # An inherited position leaving is not one of this grid's
                # round trips, and its pnl is booked against an adoption-day
                # mark rather than a price the grid chose. Including it would
                # credit the grid with a loss it did not take.
                (CryptoGridTradeHistory.exit_reason.is_(None))
                | (CryptoGridTradeHistory.exit_reason != ADOPTED_EXIT_REASON)
            )
        )).scalars().all()
    pnls = [float(p) for p in rows if p is not None]
    n = len(pnls)
    if not n:
        return {"trades": 0, "wins": 0, "losses": 0, "flat": 0,
                "gross_win_usd": 0.0, "gross_loss_usd": 0.0,
                "win_rate_pct": None, "profit_factor": None,
                "expectancy_per_trade_usd": None}
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "flat": n - len(wins) - len(losses),
        "gross_win_usd": round(gross_win, 2),
        "gross_loss_usd": round(gross_loss, 2),
        "win_rate_pct": round(len(wins) / n * 100, 1),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "expectancy_per_trade_usd": round(sum(pnls) / n, 4),
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

        # TWO BOOKS, NOT ONE HEADLINE - the realized counterpart to
        # get_grid_status()'s unrealized_own_usd / unrealized_adopted_usd.
        #
        # total_realized_pnl below is every closed row summed, which is the
        # right answer to "how much cash has this fleet taken" and the wrong
        # one to "how is the grid doing" - the only question the dashboard's
        # "Taken" tile was ever read as asking. Those were the same number
        # until an inherited position closed, and then they were not: two ZEC
        # adopted_exits took the tile from +$135.58 to -$14.35 while the
        # grid's own 196 round trips had not changed at all.
        #
        # So the split is published and the caller picks. ADOPTED_EXIT_REASON
        # is the only reason that marks an inherited exit; NULL is a legacy
        # row from before exit_reason existed and belongs with the grid's own.
        adopted_rows = (await db.execute(
            select(CryptoGridTradeHistory).where(
                CryptoGridTradeHistory.exit_reason == ADOPTED_EXIT_REASON)
        )).scalars().all()
        realized_adopted_trades = len(adopted_rows)
        _adopted_sum = sum(float(r.pnl or 0.0) for r in adopted_rows)
        realized_adopted_usd = round(_adopted_sum, 2)
        realized_own_trades = total_trade_count - realized_adopted_trades
        realized_own_usd = round(total_realized_pnl - _adopted_sum, 2)

        # THE FOUR ROWS WRITTEN BEFORE THE BOOKING FIX DEPLOYED.
        #
        # A row booked after that fix already carries the declared basis in
        # entry_price and needs nothing here. The four ZEC rows written this
        # morning do not: they were priced against the ~$1,650 adoption mark,
        # and the account owner's dashboard reads -$311.24 on four sales that
        # put $1,268.45 of cash in his wallet and gained $297.78 against the
        # $1,013.80 he really paid.
        #
        # Everything needed to restate them is on the row - qty, exit_price,
        # and a declared basis for the product - so it is computed on READ.
        # The stored row is never touched: it is the record of what was
        # booked, and rewriting history to make a number look better is the
        # opposite of what this whole change is for. Both figures are
        # published and the caller says which it is showing.
        #
        # None, not zero, when no row could be restated - "nothing to
        # restate" and "restates to $0.00" are different claims.
        _restated = 0.0
        _restated_n = 0
        for r in adopted_rows:
            basis = true_cost_basis_for(r.product_id)
            if basis is None or not r.qty or r.exit_price is None:
                _restated += float(r.pnl or 0.0)
                continue
            if r.entry_price is not None and abs(r.entry_price - basis) < 1e-9:
                # already booked at the declared basis
                _restated += float(r.pnl or 0.0)
                continue
            # An adopted slice placed no buy order, so only its sell leg was
            # ever charged - the same shape slice_round_trip_fee_rate() gives
            # it. Derived from the row's own booked fee so this cannot drift
            # from the rate that sale really paid.
            _gross_booked = r.qty * (r.exit_price - (r.entry_price or 0.0))
            _fee = _gross_booked - float(r.pnl or 0.0)
            _notional_booked = r.qty * ((r.entry_price or 0.0) + r.exit_price) / 2.0
            _rate = (_fee / _notional_booked) if _notional_booked else 0.0
            _notional_true = r.qty * (basis + r.exit_price) / 2.0
            _restated += r.qty * (r.exit_price - basis) - _rate * _notional_true
            _restated_n += 1
        realized_adopted_restated_usd = (
            round(_restated, 2) if _restated_n else None)
        realized_adopted_restated_count = _restated_n

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

        try:
            limit_recent = int(limit_recent)
        except (TypeError, ValueError):
            limit_recent = 50
        if limit_recent <= 0:
            limit_recent = 50
        limit_recent = min(limit_recent, GRID_TRADE_HISTORY_MAX_ROWS)
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
        # WHAT recent_trades LEAVES OUT, said out loud.
        #
        # Without these a 50-row slice of a 132-trade book reads as the book,
        # and any distribution taken off it is a distribution of whatever
        # happens to be recent. That is not a hypothetical: the four-way
        # exit_reason split shipped at 00:16Z and one hour later exactly ONE
        # of the 50 rows had been written under it - a reader who took the
        # counts at face value would have concluded the new labels were never
        # being used.
        "recent_trades_returned": len(recent_trades),
        "recent_trades_limit": limit_recent,
        "recent_trades_truncated": total_trade_count > len(recent_trades),
        "recent_trades_omitted": max(total_trade_count - len(recent_trades), 0),
        "recent_trades_is": (
            f"the {len(recent_trades)} most recently CLOSED trades, newest "
            f"first" + ("" if total_trade_count <= len(recent_trades) else
                        f" - {total_trade_count - len(recent_trades)} older "
                        f"completed trades are NOT in this list. Raise `limit` "
                        f"(ceiling {GRID_TRADE_HISTORY_MAX_ROWS}) before "
                        f"reading any distribution off these rows.")),
        "total_trade_count": total_trade_count,
        "total_realized_pnl": round(total_realized_pnl, 2),
        # The two books behind that single figure - see the note above.
        # own = round trips this grid chose both ends of.
        # adopted = inherited positions resolving, priced at the declared
        # true cost basis that authorised the exit.
        "realized_own_usd": realized_own_usd,
        "realized_own_trades": realized_own_trades,
        "realized_adopted_usd": realized_adopted_usd,
        "realized_adopted_trades": realized_adopted_trades,
        # The same inherited exits priced at the declared true cost basis,
        # for rows booked before that basis reached the booking path. None
        # when there is nothing to restate. The stored rows are unchanged.
        "realized_adopted_restated_usd": realized_adopted_restated_usd,
        "realized_adopted_restated_count": realized_adopted_restated_count,
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

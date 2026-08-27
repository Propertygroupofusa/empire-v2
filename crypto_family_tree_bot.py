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
import random
import threading
import time
import traceback
from datetime import datetime, timezone

from sqlalchemy import select, text, desc
from sqlalchemy.exc import IntegrityError
from database import AsyncSessionLocal
from models import BotPosition, CryptoTreeBranch, TradingBotState, CryptoBacktestRun, CryptoCoinTradeHistory, CryptoActivityEvent

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
#
# Lowered again to $100 (2x the $50 seed) per the account owner's
# explicit request ("I need more than just 5 up here... add to the
# tree") - only 5 branches existed and none had crossed $150 yet, so
# nothing was spawning. $100 leaves only a $50 buffer after each spawn
# (exactly the seed amount) - thinner than the $150 tier's $100 buffer,
# but still real: a branch can't go net-negative from spawning, just
# closer to its own floor. Real branches sitting at the old $150 tier
# get migrated down automatically on next startup (see
# _lower_existing_unlock_tiers) rather than waiting for their next sale.
UNLOCK_TIER_USD = engine._safe_float_env("TREE_UNLOCK_TIER_USD", "100")
# The value being replaced above - used once at coordinator startup (see
# _lower_existing_unlock_tiers) to retroactively apply the new, lower tier
# to branches that already exist and are still waiting to cross their
# PRIOR threshold. Only ever a one-time backstop for branches created
# before this change; never touches a branch that already crossed its
# first tier (its next_unlock_tier would no longer be exactly this value).
PRIOR_UNLOCK_TIER_USD = 150.0

# Per the account owner's explicit request ("let Bitcoin have its next
# child at $50"): root's OWN next-spawn requirement is now half the
# regular UNLOCK_TIER_USD - after root spawns a child, the amount it
# needs to grow by before spawning its NEXT one is $50, not $100. Only
# ever applies to root's own increment (see _maybe_spawn_child) - a
# spawned child's own first tier still uses the regular UNLOCK_TIER_USD,
# same as always.
ROOT_UNLOCK_TIER_USD = engine._safe_float_env("TREE_ROOT_UNLOCK_TIER_USD", "50")

BRANCH_FLOOR_TIER = engine._safe_float_env("TREE_BRANCH_FLOOR_TIER", "50")
COORDINATOR_SCAN_SECONDS = engine._safe_int_env("TREE_COORDINATOR_SCAN_SECONDS", "20")


def _floor_tier_for_balance(balance: float) -> float:
    """The real floor-tier formula every self-heal path below uses,
    clamped so it can never go negative. Real fee/rounding drift can
    leave a branch's allocated_usd sitting a few cents below zero (a
    flat branch that's sold down to nothing, its qty-weighted math
    settling a hair negative) - without the clamp, math.floor(balance /
    BRANCH_FLOOR_TIER) * BRANCH_FLOOR_TIER produces a real negative
    floor (e.g. -$50.00 off a -$0.00-ish balance), which is meaningless
    and confusing on the dashboard - a floor exists to protect real
    money, and there's no real money below $0 left to protect. Found
    live: crypto_tree_xrp_usd_4 showing Balance $-0.00 / Floor -$50.00."""
    return max(0.0, math.floor(balance / BRANCH_FLOOR_TIER) * BRANCH_FLOOR_TIER)

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
#
# Lowered from the original 30 min to 5 min per the account owner's
# explicit choice, after being told what this protects against (an
# immediate re-breach spiral). Still real protection, just thinner - a
# branch gets back to trading much faster, at the cost of somewhat less
# cushion before it's allowed to risk hitting the same wall again.
FLOOR_BREACH_COOLDOWN_SECONDS = engine._safe_int_env("TREE_FLOOR_BREACH_COOLDOWN_SECONDS", "300")  # 5 min default
FLOOR_BREACH_COOLDOWN_KEY_PREFIX = "crypto_family_tree_floor_breach_cooldown_"

# ROLLING EXPECTANCY KILL SWITCH - per the account owner's explicit
# request, evaluating a pasted proposal: track real expectancy (average
# real P&L per trade) across the tree's most recent real completed
# trades, and automatically pause new entries tree-wide if it's gone
# negative - closing losing positions (STOP/TARGET/breakeven/giveback)
# always keeps running regardless, same "existing protection never
# pauses, only new money does" principle every other kill switch in this
# file already follows (STOP_TRADING, the floor-breach cooldown). Scoped
# tree-wide (not per-coin/per-branch) since any single coin's real trade
# count is usually too thin for a meaningful rolling window on its own.
# Real, contestable, self-healing - recomputed fresh on every check, so
# it lifts automatically the moment enough real winning trades roll into
# the window and enough losers roll back out, never a one-way flag.
ROLLING_EXPECTANCY_WINDOW = engine._safe_int_env("TREE_ROLLING_EXPECTANCY_WINDOW", "20")
ROLLING_EXPECTANCY_MIN_TRADES = engine._safe_int_env("TREE_ROLLING_EXPECTANCY_MIN_TRADES", "15")

# Per the account owner's explicit request: a coin a branch just sold
# becomes "unclaimed" the instant that branch's row commits its new
# product_id - with nothing else blocking it, a different branch (or
# even the same one, on a near-simultaneous cycle) could buy straight
# back into a coin that was sold moments ago. This gives every coin a
# real one-cycle cooldown after being sold before ANY branch can claim
# it again - after that single cycle, if it's still bullish (the normal
# find_most_volatile_unclaimed_coin() filter, unchanged), it's fair game
# again like any other coin. Tracked in-memory (all branches run as
# threads in the same process) rather than in the DB - this is a real
# but short-lived timing guard, not something that needs to survive a
# restart.
_coin_last_sold_at: dict = {}


def _coin_sale_cooldown_active(product_id: str) -> bool:
    last_sold = _coin_last_sold_at.get(product_id)
    if last_sold is None:
        return False
    return (time.time() - last_sold) < CYCLE_SECONDS

# Per the account owner: once a position has ever shown a real profit, it
# should never be allowed to give more than this many real dollars back
# from its best point before locking in whatever's left and moving on to
# a new coin - independent of the fixed target/stop prices set at buy
# time. Tracked every cycle a position is held: whenever unrealized
# profit reaches a new high, that high is saved as the peak (reusing
# BotPosition.peak_pct - unused by this bot otherwise - as a dollar
# figure, not a percent, purely for this); once (peak - current) reaches
# MAX_PROFIT_GIVEBACK_USD, that's a real exit ("PEAK PROFIT GIVEBACK"),
# same real sell/skim/coin-switch path as TARGET HIT or STOP HIT. Only
# ever engages after real profit has actually been reached - a position
# still underwater is governed by the ordinary stop, not this.
MAX_PROFIT_GIVEBACK_USD = engine._safe_float_env("TREE_MAX_PROFIT_GIVEBACK_USD", "3.75")

# Per the account owner's explicit request ("buy and sell with a profit
# within a short period of time... if it's a profit... allow it to be
# taken and do it again, if not then keep it until it grows"): a real,
# fast profit-take exit, checked on the SAME per-branch cycle every other
# exit already runs on (~27-33s with jitter - well inside the "every 5
# minutes" the account owner asked for, no separate timer needed). Reuses
# the exact same real fee-adjusted net-P&L formula the giveback-net-of-
# fees fix already validated (price * qty * (1 - ROUND_TRIP_FEE_RATE/2) -
# entry_price * qty) - if that number is ever genuinely positive, right
# now, the position closes and the branch is immediately free to look for
# its next opportunity (the existing post-sale coin-switch/rebuy path
# already handles "do it again," no new logic needed there). Deliberately
# does NOT force-close while net-negative - a position that hasn't
# cleared real fees yet is left completely alone, still governed only by
# its own TARGET/STOP/breakeven/giveback protection, exactly matching the
# account owner's explicit "never force it into the negative" requirement
# (the OPPOSITE of a blind timeout that closes whatever's still
# undecided). QUICK_PROFIT_MIN_NET_USD (0.0 default - literally "any real
# profit, however small") is a real dollar floor above the already-
# fee-adjusted number, env-overridable if a minimum worth bothering with
# turns out to matter more than taking every real cent on offer.
QUICK_PROFIT_MIN_NET_USD = engine._safe_float_env("TREE_QUICK_PROFIT_MIN_NET_USD", "0.0")

# Per the account owner (a king/throne model, corrected after an earlier
# "permanent once earned" version): BTC (the root) is King - always stays
# on BTC-USD, never contested, never changes - see the ROOT_BOT_NAME check
# in _branch_sell_and_settle. Among any group of siblings (same
# parent_bot_name) under it, whichever one currently carries the highest
# real balance holds the throne for that group and stays locked onto its
# current coin - but the throne is CONTESTABLE: the moment another
# sibling in the same group grows bigger, it dethrones the current
# holder (who resumes normal coin-switching) and takes the lock for
# itself. Checked periodically by the coordinator (see
# _check_and_lock_strongest_siblings) rather than per-branch-cycle, since
# it needs to see every sibling at once to compare them. A parent with
# only one child has nothing to contest yet, so no lock is granted until
# there are real siblings to compare against.
COIN_LOCK_KEY_PREFIX = "crypto_family_tree_coin_locked_"

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

# Per the account owner's explicit request: every real spawn reinforces
# whichever EXISTING branch is currently weakest (lowest % toward its own
# next spawn tier) with a real $50 buy, instead of starting a brand-new
# branch - unconditionally, every single spawn, not gated by any threshold.
# Revised twice: first "every other spawn" (alternating), then "reinforce
# until everyone clears 50%, then back to normal," and finally - after the
# account owner asked what happens to a branch stuck partway (60-90%) that
# never dips below 50% and so never gets help - "always help whichever one
# is weakest, period." A brand-new branch now only ever gets created once
# there's no OTHER existing branch left to reinforce (see
# _pick_weakest_branch_for_reinforcement's None case in _maybe_spawn_child).

# Real spendable cash (real_balance - locked_usd) below MIN_TRADE_USD just
# sits there forever on its own - no branch can ever spend less than
# MIN_TRADE_USD on a buy, so it isn't profit and run_branch_cycle's
# trading logic can never touch it. Per the account owner's request: if
# that stranded amount hasn't moved for this many hours (nothing sold to
# top it up, no branch could spend it), sweep it into the same
# locked-profit ledger the 10% skim uses - see _check_and_sweep_stranded_dust().
#
# Lowered from 24h to 15 min, then again to 6 min (0.1h) per the account
# owner's explicit follow-up requests. 6 min isn't just DUST_STUCK_HOURS
# alone - DUST_CHECK_INTERVAL_SECONDS below had to come down too, from
# 15 min to 5 min, or the stuck-threshold change would have been
# meaningless (the check itself would still only run every 15 min
# regardless of how low the threshold went). With a 5-min check
# interval, dust stuck for 6 min gets caught on the very next check
# after crossing that mark - two checks apart, not one, so it's still
# never a literal zero-wait sweep (which would risk catching cash that's
# only momentarily below the trade minimum mid-cycle, e.g. the few
# seconds between a sell settling and its own rebuy - not genuinely
# stranded).
DUST_STUCK_HOURS = engine._safe_float_env("TREE_DUST_STUCK_HOURS", "0.1")
DUST_CHECK_INTERVAL_SECONDS = engine._safe_int_env("TREE_DUST_CHECK_INTERVAL_SECONDS", "300")
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
    "POL-USD",    # 2020 - was MATIC-USD; Coinbase permanently disabled MATIC
                  # trading Oct 14, 2025 and completed the 1:1 migration to
                  # POL by Oct 17 (Polygon's real token upgrade, not a bug on
                  # our end) - see _migrate_matic_to_pol() below for the
                  # branch already stuck on the dead MATIC-USD product_id
    "LDO-USD",    # 2021
    "ICP-USD",    # 2021
    "FLOKI-USD",  # 2021
    "OP-USD",     # 2022
    "APT-USD",    # 2022
    "BONK-USD",   # 2022
    "ARB-USD",    # 2023
    "SUI-USD",    # 2023
    "BLUR-USD",   # 2023
    # JUP-USD removed: real, confirmed-live "INVALID_ARGUMENT: Invalid
    # product_id" on every single buy attempt, repeatedly, over multiple
    # sessions - unlike MATIC-USD (a real Coinbase migration to POL-USD,
    # with a genuine successor id), there is no public migration notice
    # for JUP-USD and no successor to rename it to - it appears to simply
    # not be a listed product on this account/tier. The existing
    # permanent-rejection auto-switch (_is_permanent_order_rejection)
    # already moves any branch caught holding it onto a different real
    # coin on its very next cycle, but leaving it in this list meant
    # OTHER branches could keep landing on it too (confirmed live:
    # crypto_tree_bch_usd switched off RNDR-USD's real permission
    # rejection straight into JUP-USD, which would have just failed
    # again next cycle) - removing it here stops it from ever being
    # offered as a candidate again, for any branch.
    # Added per the account owner's explicit request after the tree hit
    # its real ceiling - every coin above was already claimed by an
    # existing branch or excluded, so a real "Start new $50 branch"
    # click had nowhere left to go ("No eligible coin left unclaimed to
    # start a new branch on"). These are all real, liquid pairs already
    # tradeable on Coinbase Advanced Trade - same onboarding as every
    # coin above, nothing else about the spawn/exclusion logic changes.
    "BCH-USD",    # 2017
    "ETC-USD",    # 2016
    "XLM-USD",    # 2014
    "ALGO-USD",   # 2019
    "FIL-USD",    # 2020
    "INJ-USD",    # 2021
    "SEI-USD",    # 2023
    "TIA-USD",    # 2023
    "PEPE-USD",   # 2023
    "WIF-USD",    # 2023
]

# Real backtested evidence (crypto_selection_backtest.py, replaying this
# bot's own actual rules against 30 real days of Coinbase history) showed
# these four as the worst performers of the whole family - STX-USD dead
# last at -44.1% ROI with a 21.6% win rate, followed by BLUR/UNI/DOT all
# similarly deep negative. Per the account owner's original explicit
# choice these started out excluded - but per a later explicit choice,
# NOT a permanent blacklist: a coin in this set becomes tradable again
# once it clears the same bar an auto-excluded coin needs to self-heal
# (see _manually_excluded_still_excluded() below) - same contestable
# philosophy as everything else in this system, just starting from
# "excluded" instead of "included" by default. This set itself never
# shrinks or grows on its own though - only another explicit decision
# adds or removes a coin from the starting list.
#
# POL-USD added per the account owner's explicit real-money decision
# after the reinforcement-loophole fix (see "Real feedback loop found"
# above) had already stopped the bleeding from getting WORSE but left
# existing exposure untouched. The account owner then watched the real
# per-coin trade history keep showing POL-USD as, by far, the tree's
# worst performer: 79 real trades, a 14% win rate, -$337.96 total P&L -
# more negative than every other coin's total loss combined, and the
# direct real consequence of POL having accumulated far more branches
# than any other coin during the earlier top-15-rotation spawn storm
# (more branches sharing one coin means more independent buy/sell
# cycles counted against that coin, not better odds). Adding it here
# stops any NEW branch from spawning into it, being reinforced with it,
# or switching into it after an exit - it does NOT force-sell any
# branch currently holding it (see the "never force-sold" note below);
# those keep running under their own real stop/target/breakeven
# protection and simply won't be offered POL-USD again once they exit.
# Same contestable/self-healing rule as every other coin here - it
# becomes tradable again the instant a real backtest run shows it
# genuinely positive.
MANUAL_EXCLUDED_COINS = {"STX-USD", "BLUR-USD", "UNI-USD", "DOT-USD", "PEPE-USD", "WIF-USD", "POL-USD"}

# Per the account owner's explicit choice: the coordinator (see run()'s
# _scan()) now re-runs the real backtest on its own every
# AUTO_BACKTEST_INTERVAL_SECONDS and grows/shrinks this automatic layer
# on top of MANUAL_EXCLUDED_COINS with NO human check before it takes
# effect - a coin is auto-excluded once its last AUTO_EXCLUDE_RUN_WINDOW
# real backtest runs have ALL been negative-ROI, and un-excluded again
# the moment its most recent run turns positive (contestable/self-
# healing, same philosophy as the strongest-sibling throne and the
# floor self-heal - never a one-way, permanent verdict). Requiring
# several consecutive bad runs (not just one) is deliberate: DOT-USD and
# UNI-USD swung from -38.8%/-40.0% to -14.2%/-12.4% in the same
# afternoon in real testing, so a single run is too noisy to act on
# alone.
AUTO_BACKTEST_INTERVAL_SECONDS = engine._safe_int_env("TREE_AUTO_BACKTEST_INTERVAL_SECONDS", str(24 * 60 * 60))
AUTO_EXCLUDE_RUN_WINDOW = engine._safe_int_env("TREE_AUTO_EXCLUDE_RUN_WINDOW", "3")

# Per the account owner's explicit request ("doing 15 for now out of 28
# and rotate them as it goes... if it's more profitable then it jumps up
# there and be able to get in this place"): rather than every
# non-excluded coin in COIN_FAMILY_TREE being a candidate, the tree now
# concentrates on a smaller, actively-rotating set of its real
# best-performing coins - the top TOP_N_ELIGIBLE_COINS by latest real
# backtest ROI (see _compute_top_ranked_coins). A coin ranks in the
# instant its latest real backtest run puts it in the top N, and ranks
# back out the instant a fresher run drops it below the cut - same
# contestable, never-one-way philosophy as the exclusion layers below,
# just working from a rank cut instead of a good/bad verdict.
TOP_N_ELIGIBLE_COINS = engine._safe_int_env("TREE_TOP_N_ELIGIBLE_COINS", "15")

# Real gap found live: POL-USD's backtest ranked it #1 (44 simulated
# trades, 50% win rate, +6.3% ROI) while its REAL live trade history
# showed 79 real trades, a 14% win rate, and -$337.96 - the tree's worst
# performer by far. The automatic backtest-based exclusion layer above
# was structurally blind to this: it only ever reads CryptoBacktestRun,
# which simulates ONE clean position trading a coin alone with a fresh
# $150 each time - it can never see what actually happens when several
# real branches (up to 15, for POL) fight over the same coin's real
# pooled Coinbase balance and real order book at once. This layer closes
# that gap by watching REAL CryptoCoinTradeHistory directly, independent
# of what any backtest says about the same coin. A rolling window (not
# all-time) so a coin that's actually turned around recently heals
# quickly, same contestable/self-healing philosophy as every other layer
# here - the window itself, re-read fresh on every call, IS the healing
# mechanism, no separate un-exclude logic needed.
LIVE_PERFORMANCE_TRADE_WINDOW = engine._safe_int_env("TREE_LIVE_PERF_TRADE_WINDOW", "30")
LIVE_PERFORMANCE_MIN_TRADES = engine._safe_int_env("TREE_LIVE_PERF_MIN_TRADES", "15")
LIVE_PERFORMANCE_MIN_WIN_RATE = engine._safe_float_env("TREE_LIVE_PERF_MIN_WIN_RATE", "0.25")
LIVE_PERFORMANCE_MIN_PNL_USD = engine._safe_float_env("TREE_LIVE_PERF_MIN_PNL_USD", "-50.0")

_last_auto_backtest_at = 0.0


async def _compute_live_performance_excluded_coins() -> set:
    """Real, live counterpart to _compute_auto_excluded_coins() above, but
    reading actual completed trades (CryptoCoinTradeHistory) instead of
    backtest simulations. A coin is excluded once its most recent
    LIVE_PERFORMANCE_TRADE_WINDOW real trades (or fewer, if it hasn't
    traded that many times yet) number at least LIVE_PERFORMANCE_MIN_TRADES
    and EITHER the real win rate over that window is below
    LIVE_PERFORMANCE_MIN_WIN_RATE OR the real total P&L over that window
    is below LIVE_PERFORMANCE_MIN_PNL_USD - catches both "a long string of
    small losses" (POL's real shape: 14% win rate) and "a few large
    losses dragging an otherwise-okay win rate down" in one rule. A coin
    with fewer than LIVE_PERFORMANCE_MIN_TRADES real trades on record is
    never excluded here - not enough real evidence yet, same "needs real
    evidence" default every other layer in this file uses."""
    excluded = set()
    async with AsyncSessionLocal() as db:
        for product_id in COIN_FAMILY_TREE:
            result = await db.execute(
                select(CryptoCoinTradeHistory.pnl)
                .where(CryptoCoinTradeHistory.product_id == product_id)
                .order_by(desc(CryptoCoinTradeHistory.closed_at))
                .limit(LIVE_PERFORMANCE_TRADE_WINDOW)
            )
            recent_pnls = result.scalars().all()
            if len(recent_pnls) < LIVE_PERFORMANCE_MIN_TRADES:
                continue
            wins = sum(1 for pnl in recent_pnls if pnl > 0)
            win_rate = wins / len(recent_pnls)
            total_pnl = sum(recent_pnls)
            if win_rate < LIVE_PERFORMANCE_MIN_WIN_RATE or total_pnl < LIVE_PERFORMANCE_MIN_PNL_USD:
                excluded.add(product_id)
    return excluded


async def _compute_top_ranked_coins():
    """Real, live ranking by latest CryptoBacktestRun.roi_pct_of_spend per
    coin - the exact same real backtest data the exclusion layers below
    and crypto_selection_backtest.html's own table already read, not a
    new or separately-computed number. Returns the set of the top
    TOP_N_ELIGIBLE_COINS coins by ROI, or None if fewer than
    TOP_N_ELIGIBLE_COINS coins have ANY real backtest run yet - a
    deliberate cold-start guard: with too little real evidence to
    meaningfully fill a top-N cut, get_effective_excluded_coins() skips
    this filter entirely rather than accidentally excluding every coin in
    the tree because most of them still show as "unranked". A coin with
    no real run yet is simply never in the ranked set until it has one -
    same "needs real evidence" default MANUAL_EXCLUDED_COINS already
    uses. One query regardless of how many coins are in COIN_FAMILY_TREE
    (ordered by run_at descending, first row per product_id kept) rather
    than one query per coin, since this is on the hot path for every
    coin-selection call."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(CryptoBacktestRun.product_id, CryptoBacktestRun.roi_pct_of_spend)
            .order_by(CryptoBacktestRun.product_id, desc(CryptoBacktestRun.run_at))
        )
        rows = result.all()
    latest_roi = {}
    for product_id, roi in rows:
        if product_id not in latest_roi:
            latest_roi[product_id] = roi
    if len(latest_roi) < TOP_N_ELIGIBLE_COINS:
        return None
    ranked = sorted(latest_roi.items(), key=lambda kv: kv[1], reverse=True)
    return {product_id for product_id, _roi in ranked[:TOP_N_ELIGIBLE_COINS]}


async def _compute_auto_excluded_coins() -> set:
    """Reads CryptoBacktestRun history (most recent AUTO_EXCLUDE_RUN_WINDOW
    rows per coin) and returns the set of coins whose last
    AUTO_EXCLUDE_RUN_WINDOW real runs were ALL negative ROI. A coin with
    fewer than AUTO_EXCLUDE_RUN_WINDOW runs on record yet is never
    auto-excluded - there isn't enough evidence."""
    auto_excluded = set()
    async with AsyncSessionLocal() as db:
        for product_id in COIN_FAMILY_TREE:
            result = await db.execute(
                select(CryptoBacktestRun.roi_pct_of_spend)
                .where(CryptoBacktestRun.product_id == product_id)
                .order_by(desc(CryptoBacktestRun.run_at))
                .limit(AUTO_EXCLUDE_RUN_WINDOW)
            )
            recent = result.scalars().all()
            if len(recent) >= AUTO_EXCLUDE_RUN_WINDOW and all(roi < 0 for roi in recent):
                auto_excluded.add(product_id)
    return auto_excluded


async def _manually_excluded_still_excluded(live_bad: set = None) -> set:
    """Per the account owner's later explicit choice: MANUAL_EXCLUDED_COINS
    is no longer a one-way permanent blacklist. Per a further explicit
    follow-up ("if it become profitable faster than that allow it to
    break free"), a manually-excluded coin now heals on the SAME bar an
    auto-excluded coin does - the instant its single most recent real
    backtest run turns positive-ROI, not a run of several in a row. This
    was deliberately the slower bar originally (matching
    AUTO_EXCLUDE_RUN_WINDOW), but the account owner asked for parity with
    the faster auto-heal rule instead.

    A coin with no real runs on record at all STAYS excluded (not enough
    evidence yet to lift the original decision) - this is still the
    opposite default from _compute_auto_excluded_coins, where a coin with
    no history is never excluded in the first place. Nothing here is a
    one-way verdict either direction: a coin that heals out of this set
    can still get caught by _compute_auto_excluded_coins later if its
    performance turns negative again, exactly like any other coin.

    Real gap found live: this heal check used to read ONLY the backtest
    side - POL-USD's most recent backtest run (44 sim trades, 50% win
    rate, +6.3% ROI) let it heal out of this set entirely, while its real
    live trade history stayed catastrophic (79 real trades, 14% win rate,
    -$392+) - the exact backtest-good/live-bad divergence
    get_effective_excluded_coins()'s own docstring already documents as
    the reason the automatic exclusion layer requires BOTH signals to
    agree. That protection never applied to this manual-list heal check,
    which read only the backtest - confirmed live: POL-USD, still
    catastrophically losing in real trading, got picked as
    crypto_btc_compound's real reinforcement target once it healed this
    way. Fixed: a manually-excluded coin now also stays excluded while
    _compute_live_performance_excluded_coins currently flags it as bad,
    regardless of what its backtest says - real live losses can't be
    out-voted by a simulated result. A coin with too little real live
    trade history to have an opinion either way is unaffected (same
    "needs real evidence" default every other layer already uses) - only
    a CONFIRMED-bad live track record blocks the heal.

    live_bad: the caller's own already-computed
    _compute_live_performance_excluded_coins() result, so
    get_effective_excluded_coins() doesn't pay for the same real DB scan
    twice in one call. Computed internally when omitted (e.g. a
    standalone caller or a test)."""
    if live_bad is None:
        live_bad = await _compute_live_performance_excluded_coins()
    still_excluded = set()
    async with AsyncSessionLocal() as db:
        for product_id in MANUAL_EXCLUDED_COINS:
            if product_id in live_bad:
                still_excluded.add(product_id)
                continue
            result = await db.execute(
                select(CryptoBacktestRun.roi_pct_of_spend)
                .where(CryptoBacktestRun.product_id == product_id)
                .order_by(desc(CryptoBacktestRun.run_at))
                .limit(1)
            )
            most_recent = result.scalar_one_or_none()
            healed = most_recent is not None and most_recent > 0
            if not healed:
                still_excluded.add(product_id)
    return still_excluded


async def get_effective_excluded_coins_with_reasons() -> dict:
    """Same real exclusion computation as get_effective_excluded_coins()
    below, but returns WHY each excluded coin is excluded instead of just
    a flat set - built after the account owner pointed out that the live
    coin-watchlist/backtest page was showing real, hot, bullish coins
    tagged with a bare "Excluded" badge and no explanation at all, making
    it impossible to tell whether the exclusion was actually deserved
    (real bad live trades) or just an artifact of the top-N rotation cutting
    off a coin that looks great RIGHT NOW. Returns {product_id: reason}
    for every currently-excluded coin. get_effective_excluded_coins() is a
    thin wrapper around this returning just the key set, so the two can
    never disagree with each other."""
    live_bad = await _compute_live_performance_excluded_coins()
    manual = await _manually_excluded_still_excluded(live_bad=live_bad)
    backtest_bad = await _compute_auto_excluded_coins()
    signal_agreed_bad = backtest_bad & live_bad
    top_ranked = await _compute_top_ranked_coins()

    reasons = {}
    for product_id in manual:
        reasons[product_id] = "Manually excluded (real live losses)"
    for product_id in signal_agreed_bad:
        if product_id not in reasons:
            reasons[product_id] = "Bad recent backtest AND bad real live trades"
    if top_ranked is not None:
        for product_id in COIN_FAMILY_TREE:
            if product_id not in top_ranked and product_id not in reasons:
                reasons[product_id] = f"Outside the current top {TOP_N_ELIGIBLE_COINS} by backtested ROI"
    return reasons


async def get_effective_excluded_coins() -> set:
    """The real, live set of coins no branch will ever be offered right
    now - whichever of MANUAL_EXCLUDED_COINS hasn't yet healed (see
    _manually_excluded_still_excluded), unioned with every coin currently
    OUTSIDE the real top-TOP_N_ELIGIBLE_COINS ranking by backtest ROI
    (see _compute_top_ranked_coins, skipped entirely during the
    cold-start case where there isn't yet enough real ranking data), PLUS
    whichever coins the two automated "is this coin bad" signals below
    both agree on.

    Per the account owner's explicit choice, made after watching POL-USD
    rank #1 on backtest (44 sim trades, 50% win rate, +6.3% ROI) while
    its real live trade history showed the exact opposite (79 real
    trades, 14% win rate, -$337.96): the automatic backtest rule
    (_compute_auto_excluded_coins) and the automatic live-performance
    rule (_compute_live_performance_excluded_coins) now only exclude a
    coin when BOTH signals agree it's bad - a coin flagged by just one
    of the two (like POL would have been, backtest-good/live-bad) no
    longer gets cut off by this combined layer on its own. Real evidence
    from either tool alone still counts for something, it just isn't
    thrown away the instant the other tool disagrees.

    This is a deliberate, accepted tradeoff: a coin with genuinely bad
    live results but literally zero backtest history yet (never run)
    won't be caught by this intersection either, since "no data" isn't
    the same as "backtest agrees it's bad." MANUAL_EXCLUDED_COINS (POL-USD
    included, added from real live evidence that already accounted for
    both) remains the way to act immediately and directly on a real
    problem coin without waiting on signal agreement - manual exclusion
    is a deliberate human decision, not an automated-signal question, and
    is completely unaffected by this change."""
    reasons = await get_effective_excluded_coins_with_reasons()
    return set(reasons.keys())


async def get_latest_backtest_result(product_id: str):
    """Real historical context for the dashboard's 💡 Sell advice button,
    per the account owner's explicit follow-up request to use "whatever
    system... is getting its information" on the coin-selection backtest
    page to help inform sell advice too - reads the exact same
    CryptoBacktestRun rows that page's table and the automatic
    coin-exclusion rule already read (_compute_auto_excluded_coins above),
    not a new or separately-computed number. Returns the most recent real
    run for this coin, or None if it's never been backtested."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(CryptoBacktestRun)
            .where(CryptoBacktestRun.product_id == product_id)
            .order_by(desc(CryptoBacktestRun.run_at))
            .limit(1)
        )
        row = result.scalar_one_or_none()
    if row is None:
        return None
    return {
        "num_trades": row.num_trades,
        "win_rate": row.win_rate,
        "roi_pct_of_spend": row.roi_pct_of_spend,
        # + "Z" - same real display bug as PricePredictionLog/
        # PricePredictionCalibration in models.py: a naive UTC value with
        # no timezone designator gets silently misread as the viewer's
        # local time by the browser's Date parser.
        "run_at": row.run_at.isoformat() + "Z" if row.run_at else None,
    }


async def _run_scheduled_backtest_and_update_exclusions():
    """Called from the coordinator's own scan loop, throttled to once per
    AUTO_BACKTEST_INTERVAL_SECONDS (see run()'s _scan()). Runs the exact
    same real backtest the manual dashboard button triggers, persists
    every coin's result, then logs the resulting auto-excluded set so
    it's visible in the real deploy logs without needing the dashboard."""
    try:
        # Deferred import - crypto_selection_backtest.py itself imports
        # COIN_FAMILY_TREE etc. from this module, so importing it at the
        # top of this file would be a circular import. By the time this
        # function actually runs, both modules are already fully loaded.
        import crypto_selection_backtest
    except Exception as e:
        log.warning(f"[TREE] scheduled backtest skipped - crypto_selection_backtest not available ({e})")
        return
    log.info("[TREE] 🔄 running the scheduled real coin-selection backtest...")
    output = await crypto_selection_backtest.run_full_backtest()
    async with AsyncSessionLocal() as db:
        for r in output["ranked"]:
            db.add(CryptoBacktestRun(
                product_id=r["product_id"], num_trades=r["num_trades"],
                win_rate=r["win_rate"], roi_pct_of_spend=r["roi_pct_of_spend"],
            ))
        await db.commit()
    auto_excluded = await _compute_auto_excluded_coins()
    log.info(
        f"[TREE] 🔄 scheduled backtest done - {output['coins_with_results']} coins scored. "
        f"Auto-excluded (last {AUTO_EXCLUDE_RUN_WINDOW} runs all negative): "
        f"{sorted(auto_excluded) if auto_excluded else 'none'}"
    )


async def load_branch(bot_name: str):
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(CryptoTreeBranch).where(CryptoTreeBranch.bot_name == bot_name))
        return result.scalar_one_or_none()


CRYPTO_PASSIVE_MODE_KEY = "crypto_family_tree_passive_mode"


async def is_crypto_passive_mode() -> bool:
    """True once the account owner has retired the entire family tree in
    favor of one real buy-and-hold BTC position (mirrors
    prop_bot.is_alpaca_passive_mode() on the Alpaca side - see that
    function's docstring for why this is a DB-persisted flag rather than
    a Railway env var). Checked at the top of every real branch cycle
    (run_branch_cycle) so every branch thread - root included - stops
    doing anything at all once set: no entries, no exits, no spawns, no
    reinforcement. A real, deliberate one-way retirement, not a pause."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == CRYPTO_PASSIVE_MODE_KEY))
        row = result.scalar_one_or_none()
        return bool(row and row.base_capital and row.base_capital >= 1.0)


async def set_crypto_passive_mode(enabled: bool):
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == CRYPTO_PASSIVE_MODE_KEY))
        row = result.scalar_one_or_none()
        if row is None:
            row = TradingBotState(bot_name=CRYPTO_PASSIVE_MODE_KEY, base_capital=0.0)
            db.add(row)
        row.base_capital = 1.0 if enabled else 0.0
        await db.commit()


async def get_locked_usd() -> float:
    """The running total of skimmed profit walled off from ever being
    redeployed by any branch - see PROFIT_SKIM_PCT. Reusing TradingBotState
    (the same generic per-key bucket table prop_bot.py's own equity floor
    and bot_N buckets already use) rather than a new table."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == LOCKED_PROFIT_STATE_KEY))
        row = result.scalar_one_or_none()
        return row.base_capital if row else 0.0


ACTIVITY_FEED_MAX_ROWS = engine._safe_int_env("TREE_ACTIVITY_FEED_MAX_ROWS", "500")


async def _log_activity(bot_name: str, product_id: str, event_type: str, message: str):
    """Best-effort real activity log for the dashboard's Live Activity
    feed - per the account owner's explicit request to actually SEE the
    bot buying/selling/spawning in real time, not just static balances.
    Deliberately never allowed to raise: a logging failure must never
    block or roll back a real trade the caller is in the middle of
    recording, so any DB error here is caught and only warned about.
    `message` should be the exact same human-readable text already going
    to the real Railway log at the call site, so the dashboard feed can
    never say something different from what the logs already say.

    Opportunistically trims the table back down to ACTIVITY_FEED_MAX_ROWS
    (deleting the oldest rows past that cap) on roughly 1-in-20 calls -
    cheap, and keeps this real but low-value table from growing forever
    on a bot that generates one of these events every 30s per branch."""
    try:
        async with AsyncSessionLocal() as db:
            db.add(CryptoActivityEvent(bot_name=bot_name, product_id=product_id, event_type=event_type, message=message))
            await db.commit()
            if random.random() < 0.05:
                result = await db.execute(select(CryptoActivityEvent.id).order_by(desc(CryptoActivityEvent.id)).offset(ACTIVITY_FEED_MAX_ROWS).limit(1))
                cutoff_id = result.scalar_one_or_none()
                if cutoff_id is not None:
                    await db.execute(text("DELETE FROM crypto_activity_events WHERE id < :cutoff"), {"cutoff": cutoff_id})
                    await db.commit()
    except Exception as e:
        log.warning(f"[TREE] activity feed log failed (non-fatal, real trade unaffected): {e}")


async def get_activity_feed(limit: int = 50):
    """Real, live feed of the bot's own recent activity - reads back
    exactly what _log_activity() above wrote, most recent first. Purely
    read-only, used by the dashboard's Live Activity panel."""
    limit = max(1, min(limit, ACTIVITY_FEED_MAX_ROWS))
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(CryptoActivityEvent).order_by(desc(CryptoActivityEvent.created_at)).limit(limit)
        )
        rows = result.scalars().all()
    return [row.to_dict() for row in rows]


async def _add_locked_usd(amount: float):
    """Every branch runs as its own thread, and several can skim profit
    into this same shared row within the same instant (confirmed live:
    two real skims landing close enough together produced two rows with
    the same bot_name, and every later scalar_one_or_none() read of this
    key started raising MultipleResultsFound - see
    _dedupe_locked_profit_state() below for the one-time cleanup this
    caused). Now that a real DB-level unique index exists on
    trading_bot_state.bot_name (see _ensure_trading_bot_state_unique_index),
    a genuine race here raises IntegrityError on the second INSERT instead
    of silently creating a duplicate row - caught below and retried as a
    real update against whichever row actually won, so no dollars are
    lost either way."""
    if amount <= 0:
        return
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == LOCKED_PROFIT_STATE_KEY))
        row = result.scalar_one_or_none()
        if row:
            row.base_capital += amount
            await db.commit()
            return
        db.add(TradingBotState(bot_name=LOCKED_PROFIT_STATE_KEY, base_capital=amount, starting_capital=0.0))
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            async with AsyncSessionLocal() as db2:
                result2 = await db2.execute(select(TradingBotState).where(TradingBotState.bot_name == LOCKED_PROFIT_STATE_KEY))
                row2 = result2.scalar_one_or_none()
                if row2:
                    row2.base_capital += amount
                    await db2.commit()
                else:
                    log.warning(f"[TREE] _add_locked_usd race: lost ${amount:.2f} - row vanished between retry attempts")


async def _subtract_locked_usd(amount: float) -> float:
    """Reverse of _add_locked_usd - releases real money back OUT of the
    locked-profit ledger. Per the account owner's explicit choice, this
    is a real, deliberate reversal of the "permanently out of the
    compounding loop" design everywhere else in this system (the 10%
    skim, the dust sweep) - it only ever happens via an explicit manual
    dashboard action (see unlock_locked_profit in
    routers/trading_dashboard.py), never automatically. Clamps to
    whatever's actually there and returns the real amount released, so
    the caller can never release more than genuinely exists."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == LOCKED_PROFIT_STATE_KEY))
        row = result.scalar_one_or_none()
        current = row.base_capital if row else 0.0
        released = min(max(amount, 0.0), current)
        if row:
            row.base_capital = current - released
        await db.commit()
        return released


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
                     f"- will lock it away if it's still stuck in {DUST_STUCK_HOURS:.2f}h")
            return

        if abs(tracker.base_capital - spendable) > 0.005:
            # Changed since the last check (grew or shrank) - real cash
            # moved, so this isn't dead money yet. Restart the clock.
            tracker.base_capital = spendable
            await db.commit()
            log.info(f"[TREE] 💤 Stranded dust changed (now ${spendable:.2f}) - restarting the {DUST_STUCK_HOURS:.2f}h clock")
            return

        stuck_hours = (datetime.utcnow() - tracker.updated_at).total_seconds() / 3600.0
        if stuck_hours >= DUST_STUCK_HOURS:
            await _add_locked_usd(spendable)
            tracker.base_capital = 0.0
            await db.commit()
            log.info(f"[TREE] 🔒 Swept ${spendable:.2f} of stranded dust (stuck {stuck_hours:.1f}h below the "
                     f"${MIN_TRADE_USD:.2f} minimum trade) into locked profit - permanently out of the compounding loop")


async def _is_coin_locked(bot_name: str) -> bool:
    """True while this branch currently holds the throne for its sibling
    group - see COIN_LOCK_KEY_PREFIX. Presence of the row IS the lock;
    contestable - _check_and_lock_strongest_siblings() removes it the
    moment a sibling overtakes it."""
    key = COIN_LOCK_KEY_PREFIX + bot_name
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == key))
        return result.scalar_one_or_none() is not None


async def _lock_branch_coin(bot_name: str):
    key = COIN_LOCK_KEY_PREFIX + bot_name
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == key))
        if result.scalar_one_or_none() is None:
            db.add(TradingBotState(bot_name=key, base_capital=0.0, starting_capital=0.0))
            await db.commit()


async def _unlock_branch_coin(bot_name: str):
    key = COIN_LOCK_KEY_PREFIX + bot_name
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == key))
        row = result.scalar_one_or_none()
        if row:
            await db.delete(row)
            await db.commit()


async def _check_and_lock_strongest_siblings():
    """Once per coordinator scan: groups every non-root branch by its
    parent_bot_name, and for any group with 2+ siblings, makes sure the
    current highest-balance one - and only it - holds the throne (see
    COIN_LOCK_KEY_PREFIX for the full reasoning). Contestable: if a
    different sibling now outranks whoever currently holds the lock, the
    old holder is dethroned (unlocked, resumes normal coin-switching) and
    the new leader is crowned. A group with only one child is skipped -
    nothing to contest yet."""
    branches = await load_all_branches()
    by_parent = {}
    for b in branches:
        if b.bot_name == ROOT_BOT_NAME:
            continue  # root is permanently locked by design already - see _branch_sell_and_settle
        by_parent.setdefault(b.parent_bot_name, []).append(b)

    for parent, siblings in by_parent.items():
        if len(siblings) < 2:
            continue
        strongest = max(siblings, key=lambda b: b.allocated_usd)
        currently_locked = [s for s in siblings if await _is_coin_locked(s.bot_name)]

        if len(currently_locked) == 1 and currently_locked[0].bot_name == strongest.bot_name:
            continue  # already correctly held - nothing to do

        for holder in currently_locked:
            if holder.bot_name != strongest.bot_name:
                await _unlock_branch_coin(holder.bot_name)
                log.info(
                    f"[TREE] 👑💥 {holder.bot_name} dethroned by {strongest.bot_name} "
                    f"(${strongest.allocated_usd:,.2f} vs ${holder.allocated_usd:,.2f}) - resumes normal coin-switching"
                )

        await _lock_branch_coin(strongest.bot_name)
        log.info(
            f"[TREE] 🔒🌟 {strongest.bot_name} ({strongest.product_id}) holds the throne among "
            f"{len(siblings)} siblings under {parent} at ${strongest.allocated_usd:,.2f} - locked "
            f"on {strongest.product_id} until dethroned"
        )


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


async def get_rolling_expectancy() -> dict:
    """Real, live expectancy across the tree's most recent real completed
    trades - reads the exact same real CryptoCoinTradeHistory ledger the
    Coin Trade History dashboard panel already reads (here scoped
    tree-wide, not per-coin), most recent ROLLING_EXPECTANCY_WINDOW real
    trades by real closed_at. Returns real, honest numbers - "negative"
    only ever True once there are at least ROLLING_EXPECTANCY_MIN_TRADES
    real trades to judge it on; fewer than that is never enough evidence
    to act on, the same "no data = not excluded" default every other
    contestable layer in this file already uses."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(CryptoCoinTradeHistory.pnl)
            .order_by(desc(CryptoCoinTradeHistory.closed_at))
            .limit(ROLLING_EXPECTANCY_WINDOW)
        )
        pnls = [row[0] for row in result.all()]
    if len(pnls) < ROLLING_EXPECTANCY_MIN_TRADES:
        return {"expectancy": None, "num_trades": len(pnls), "min_trades_required": ROLLING_EXPECTANCY_MIN_TRADES, "negative": False}
    expectancy = sum(pnls) / len(pnls)
    return {
        "expectancy": round(expectancy, 4),
        "num_trades": len(pnls),
        "min_trades_required": ROLLING_EXPECTANCY_MIN_TRADES,
        "negative": expectancy < 0,
    }


async def load_all_branches():
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(CryptoTreeBranch))
        return result.scalars().all()


async def _unique_child_bot_name(product_id: str, randomize: bool = False) -> str:
    """A branch's bot_name has always been derived directly from its
    coin (crypto_tree_{coin}) - that was fine when product_id itself was
    unique, but per the account owner's explicit choice, multiple
    branches can now share the same coin, and bot_name still needs to be
    unique (it's the real per-branch thread/identity key everywhere else
    in this system). Appends _2, _3, ... until landing on a name nothing
    already uses, so a second (or third) branch on an already-claimed
    coin still gets a real, distinct identity instead of colliding with
    the existing one.

    Real production gap found live: this sequential scheme has no upper
    bound on how many DIFFERENT concurrent callers can independently
    compute the exact same "next free" name at once - unlike
    get_next_eligible_product_id()'s coin choice, there's no coin-level
    diversification possible here when several callers are all targeting
    the SAME explicit coin (e.g. several real "Trade this" taps on the
    identical coin, or the auto-spawn coordinator racing a manual click
    for that exact coin) - real, observed live even after more
    attempts+jitter fixed the coin-selection side of this. `randomize`
    is the fallback for exactly that case: instead of continuing the
    sequential search (which every racer computes identically), pick a
    short random numeric suffix - the random space is large enough that
    two concurrent callers landing on the identical suffix is
    vanishingly unlikely regardless of how many are racing, so this
    can't run out of room the way sequential numbering effectively can
    under heavy real contention."""
    base = f"crypto_tree_{product_id.lower().replace('-', '_')}"
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(CryptoTreeBranch.bot_name))
        existing = set(result.scalars().all())
    if randomize:
        candidate = f"{base}_{random.randint(1000, 999999)}"
        while candidate in existing:
            candidate = f"{base}_{random.randint(1000, 999999)}"
        return candidate
    if base not in existing:
        return base
    n = 2
    while f"{base}_{n}" in existing:
        n += 1
    return f"{base}_{n}"


async def spawn_child_branch_with_retry(product_id: str, parent_bot_name, attempts: int = 12) -> str:
    """Inserts a brand-new $50-seed branch on product_id, used by both manual
    dashboard spawn buttons. _unique_child_bot_name() computes a name from a
    plain SELECT, then this does a separate INSERT - a real gap a concurrent
    spawn (the coordinator's own per-cycle catch-up spawn check in
    _maybe_spawn_child, or a second near-simultaneous manual click) can land
    in between, claiming the identical name first and failing this one's
    commit with a bot_name IntegrityError. Real, observed live: a manual
    "Start new $50 branch" on XRP-USD failed with "crypto_tree_xrp_usd_2 was
    just created by another branch - try again", dumping the race onto the
    account owner to retry by hand. Rather than surface that, recompute a
    fresh name and retry the insert here, server-side.

    Real follow-up found live: a plain immediate retry (5 attempts, no
    delay) still exhausted every attempt and surfaced "collided with
    another branch on every retry (5x)". Root cause: every branch's
    30-second cycle timer starts from roughly the same moment (server
    boot), so when several branches cross their unlock tier in the same
    cycle window, get_next_eligible_product_id()'s deterministic
    "least-crowded coin" pick has them ALL converge on the identical
    target coin and race in near-perfect lockstep - back-to-back
    zero-delay retries can keep landing on the exact same instant as a
    sibling's own retry, colliding again and again instead of
    desynchronizing. Fixed with two changes: more attempts (12, not 5),
    and a small random jitter sleep before each retry after the first -
    real racers rarely pick the same random delay twice in a row, so this
    breaks the lockstep almost immediately even under multiple branches
    racing at once. Only raises once every attempt genuinely collides (a
    real, sustained pileup, not a transient race).

    Real follow-up found live: even 12 attempts+jitter can still exhaust
    when several REAL concurrent callers are all targeting the exact same
    explicit coin (e.g. multiple "Trade this" taps on the identical coin,
    or the auto-spawn coordinator racing a manual click for that same
    coin) - there's no coin-level diversification possible here the way
    get_next_eligible_product_id()'s randomized tiebreak helps the
    auto-pick path, since the coin itself is fixed by the caller. The
    first few attempts still try the nice sequential name
    (crypto_tree_{coin}_2, _3, ...) for the common low-contention case,
    but every attempt after that switches to _unique_child_bot_name's
    randomized suffix - a large enough random space that real exhaustion
    becomes vanishingly unlikely regardless of how many callers are
    racing for the same coin."""
    last_name = None
    last_error = None
    for attempt in range(attempts):
        if attempt > 0:
            await asyncio.sleep(random.uniform(0.05, 0.4))
        child_name = await _unique_child_bot_name(product_id, randomize=attempt >= 4)
        last_name = child_name
        async with AsyncSessionLocal() as db:
            db.add(CryptoTreeBranch(
                bot_name=child_name, product_id=product_id, parent_bot_name=parent_bot_name,
                allocated_usd=SEED_USD, next_unlock_tier=UNLOCK_TIER_USD, equity_floor=0.0,
            ))
            try:
                await db.commit()
                await _log_activity(
                    child_name, product_id, "SPAWN",
                    f"🌱 Manually started {child_name} ({product_id}) with ${SEED_USD:.2f} seed",
                )
                return child_name
            except IntegrityError as e:
                await db.rollback()
                # Real gap found live: a randomized 6-digit suffix
                # colliding on literally every one of 12 attempts is
                # statistically near-impossible if this is really a
                # bot_name uniqueness race - that pointed at this except
                # clause silently assuming every IntegrityError here means
                # "bot_name taken" without ever checking, when it could be
                # a completely different constraint or data problem.
                # Capture the real driver error text (same pattern as
                # _describe_order_rejection for Coinbase rejections) so a
                # repeat of this is diagnosable from the error message
                # itself instead of guessing again.
                last_error = str(getattr(e, "orig", e))[:300]
                # Real production discovery: this error text once revealed
                # "duplicate key value violates unique constraint
                # ix_crypto_tree_branches_product_id_unique...
                # Key (product_id)=(POL-USD) already exists" - a real,
                # separate DB-level index (see _drop_product_id_unique_index)
                # that should have been dropped hours earlier but wasn't.
                # That's a conflict on which COIN this branch holds, not
                # its bot_name - retrying with a different random name can
                # NEVER satisfy a product_id conflict, so burning the
                # remaining attempts (each with a real jitter delay) here
                # would just waste time before failing anyway. Fail fast
                # with an honest, specific message instead.
                if "product_id" in last_error.lower():
                    raise RuntimeError(
                        f"{product_id} still has a real database-level uniqueness index blocking a "
                        f"second branch on it (real error: {last_error}) - this is a schema issue, not "
                        f"a naming race, and retrying with a different name can't fix it"
                    )
                continue
    detail = f"{last_name} collided with another branch on every retry ({attempts}x)"
    if last_error:
        detail += f" - real DB error: {last_error}"
    raise RuntimeError(detail + " - try again")


async def get_next_eligible_product_id():
    """Picks a coin for a brand-new $50 branch to start on - not currently
    excluded (see get_effective_excluded_coins), not still cooling down
    from having been sold within the last cycle (see
    _coin_sale_cooldown_active).

    Per the account owner's explicit choice, no longer REQUIRES the coin
    to be unclaimed - multiple branches can now hold the same coin at
    once. Still prefers spreading out where possible though: among all
    eligible coins, picks whichever currently has the FEWEST branches
    already on it - a coin with zero branches always wins first, same
    practical result as the old "must be unclaimed" rule in the common
    case, but this degrades to piling onto the least-crowded coin instead
    of returning None once every coin has at least one branch, rather
    than hard-blocking new branches from spawning at all.

    Real production evidence: exhausting even 12 retries+jitter in
    spawn_child_branch_with_retry() on a real, repeated basis (not a
    one-off) meant the collision rate itself was too high for retry
    timing alone to fix - the OLD tie-break (fixed COIN_FAMILY_TREE list
    order) made every concurrent spawner - manual clicks, "Trade this" on
    the backtest page, and the coordinator's own per-cycle catch-up spawn
    across every branch - deterministically agree on the IDENTICAL single
    target coin whenever several coins were tied at the same lowest
    count, funneling all of that real concurrent demand onto one coin
    instead of spreading it out. Ties are now broken with a real random
    pick among every coin AT the minimum count, so simultaneous spawners
    naturally spread their targets across however many coins are tied
    (often several, when most coins sit at 0-1 branches) instead of all
    piling onto the same one - directly cutting the real collision rate,
    not just retrying around it."""
    excluded = await get_effective_excluded_coins()
    eligible = [
        p for p in COIN_FAMILY_TREE
        if p not in excluded and not _coin_sale_cooldown_active(p)
    ]
    if not eligible:
        return None
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(CryptoTreeBranch.product_id))
        held = list(result.scalars().all())
    counts = {p: held.count(p) for p in eligible}
    min_count = min(counts.values())
    tied = [p for p in eligible if counts[p] == min_count]
    return random.choice(tied)


async def _drop_product_id_unique_index():
    """One-time reversal, safe to call on every startup: per the account
    owner's explicit choice, branches are no longer required to each
    trade a different coin - multiple branches can now hold the same
    coin at once. The real DB-level UNIQUE index this same startup
    sequence used to create (ix_crypto_tree_branches_product_id_unique)
    would reject that outright at the database layer regardless of what
    the Python-level "claimed" checks say (already removed from
    get_next_eligible_product_id(), find_most_volatile_unclaimed_coin(),
    and the manual spawn-branch endpoint), so it has to come off too, on
    any deployment that already created it in an earlier session.

    Real production gap found live: `DROP INDEX IF EXISTS` never raises
    even when nothing was actually removed, so the old version of this
    function logged "removed" unconditionally - that log line was never
    real proof the index was gone. A real live error, hours later, proved
    it wasn't: "duplicate key value violates unique constraint
    ix_crypto_tree_branches_product_id_unique... Key (product_id)=(POL-USD)
    already exists" - every single spawn-collision error the retry logic
    was fighting all night was actually THIS, not a bot_name race at all
    (see spawn_child_branch_with_retry's real-time detection of this same
    error for the other half of this fix).

    Now verifies the real outcome against Postgres's own system catalog
    (pg_indexes) after the drop attempt, tries the ALTER TABLE...DROP
    CONSTRAINT form too if the plain DROP INDEX didn't actually clear it
    (Postgres can back a UNIQUE with a constraint rather than a bare
    index depending on how it was originally created, and DROP INDEX
    can't remove a constraint's backing index directly), and logs LOUDLY
    at ERROR level if it's still there after both attempts - instead of
    a misleading INFO "removed" line that was never actually verified.
    The verification query is Postgres-specific; harmless no-op (skipped,
    not fatal) on local SQLite dev where this whole problem doesn't
    exist."""
    index_name = "ix_crypto_tree_branches_product_id_unique"

    async def _check_still_exists():
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    text("SELECT indexname FROM pg_indexes WHERE tablename = 'crypto_tree_branches' AND indexname = :name"),
                    {"name": index_name},
                )
                return result.scalar_one_or_none() is not None
        except Exception:
            return None  # not Postgres (e.g. local SQLite dev) - can't verify this way, not fatal

    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text(f"DROP INDEX IF EXISTS {index_name}"))
            await db.commit()
    except Exception as e:
        log.error(f"[TREE] DROP INDEX for {index_name} raised a real error: {e}")

    still_exists = await _check_still_exists()

    if still_exists is True:
        log.warning(f"[TREE] {index_name} still present after DROP INDEX - trying ALTER TABLE...DROP CONSTRAINT instead")
        try:
            async with AsyncSessionLocal() as db:
                await db.execute(text(f"ALTER TABLE crypto_tree_branches DROP CONSTRAINT IF EXISTS {index_name}"))
                await db.commit()
        except Exception as e:
            log.error(f"[TREE] ALTER TABLE DROP CONSTRAINT for {index_name} also raised a real error: {e}")
        still_exists = await _check_still_exists()

    if still_exists is False:
        log.info(f"[TREE] ✅ confirmed {index_name} is gone (verified against pg_indexes) - branches can share the same coin")
    elif still_exists is True:
        log.error(
            f"[TREE] 🔴 {index_name} STILL EXISTS after both DROP INDEX and DROP CONSTRAINT attempts - "
            f"every real spawn onto an already-held coin will keep failing with a genuine duplicate-key "
            f"error until this is resolved directly against the database"
        )
    else:
        log.info(f"[TREE] {index_name}: drop attempted, but couldn't verify the real outcome (not running on Postgres)")


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


async def _force_root_spawn_ready():
    """Real, direct one-time startup fix - per the account owner's
    explicit request ("push that now") after the automatic per-cycle
    floor self-heal (see _maybe_spawn_child's own self-heal branch,
    fixed earlier the same night) did not visibly take effect across
    several real redeploys: crypto_btc_compound's floor kept showing
    $150.00 on the live dashboard for hours after that fix should have
    healed it to $100.00 on its very first cycle. Rather than keep
    guessing at why the reactive, per-cycle self-heal hasn't shown up
    live, this forces the exact same real correction directly at
    startup, unconditionally, then immediately attempts the spawn itself
    right here - no dependency on root's own branch thread reaching its
    next cycle tick first. Safe to call on every startup: a no-op once
    root's floor already matches its real tier and it has nothing new to
    spawn."""
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(CryptoTreeBranch).where(CryptoTreeBranch.bot_name == ROOT_BOT_NAME))
            root = result.scalar_one_or_none()
        if root is None:
            log.info("[TREE] _force_root_spawn_ready: no root row yet (fresh install) - skipping")
            return
        log.info(
            f"[TREE] _force_root_spawn_ready: root balance ${root.allocated_usd:,.2f} | "
            f"next_unlock_tier ${root.next_unlock_tier:,.2f} | floor ${root.equity_floor:,.2f}"
        )
        if root.allocated_usd < root.next_unlock_tier:
            log.info("[TREE] _force_root_spawn_ready: root hasn't crossed its own tier yet - nothing to force")
            return
        new_tier_floor = _floor_tier_for_balance(root.allocated_usd)
        if root.equity_floor > new_tier_floor:
            async with AsyncSessionLocal() as db:
                result = await db.execute(select(CryptoTreeBranch).where(CryptoTreeBranch.bot_name == ROOT_BOT_NAME))
                fresh = result.scalar_one_or_none()
                if fresh and fresh.equity_floor > new_tier_floor:
                    old_floor = fresh.equity_floor
                    fresh.equity_floor = new_tier_floor
                    await db.commit()
                    root.equity_floor = new_tier_floor
                    log.info(
                        f"[TREE] 🪜 FORCE-healed root's floor at startup ${old_floor:,.2f} -> ${new_tier_floor:,.2f} "
                        f"(real balance ${root.allocated_usd:.2f} had crossed its ${root.next_unlock_tier:,.2f} tier but stayed floor-blocked)"
                    )
        await _maybe_spawn_child(root)
        log.info("[TREE] _force_root_spawn_ready: done")
    except Exception as e:
        # Never let this one-time fix block the rest of startup (every
        # branch thread launching, the coordinator scan loop, etc.) even
        # if something real and unexpected goes wrong here - same
        # defensive pattern as every other one-time migration in this
        # file. Logged loudly so a real failure is diagnosable from
        # Railway logs instead of silently vanishing.
        log.error(f"[TREE] _force_root_spawn_ready failed: {e}")
        log.error(f"Traceback: {traceback.format_exc()}")


async def _dedupe_locked_profit_state():
    """One-time cleanup, safe to call on every startup: a real production
    crash confirmed this actually happened - multiple branches (each its
    own thread) can call _add_locked_usd() close enough together that
    both read "no row yet" before either commit lands, and both insert a
    real trading_bot_state row for LOCKED_PROFIT_STATE_KEY. From that
    point every read of this key (get_locked_usd, _add_locked_usd,
    _subtract_locked_usd - all scalar_one_or_none()) started raising
    sqlalchemy.exc.MultipleResultsFound, which is not caught anywhere
    upstream, so it took down whatever branch cycle touched it.

    Every dollar in every duplicate row is real skimmed profit, so the
    fix is a real sum, not "pick one and discard the rest" - discarding
    would make real money vanish from the locked-profit ledger. Keeps the
    oldest row (lowest id) as the survivor with the summed total,
    deletes the rest."""
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(TradingBotState)
                .where(TradingBotState.bot_name == LOCKED_PROFIT_STATE_KEY)
                .order_by(TradingBotState.id)
            )
            rows = result.scalars().all()
            if len(rows) <= 1:
                return
            total = sum(r.base_capital for r in rows)
            survivor, dupes = rows[0], rows[1:]
            individual = ", ".join(f"${r.base_capital:.2f}" for r in rows)
            survivor.base_capital = total
            for dupe in dupes:
                await db.delete(dupe)
            await db.commit()
        log.warning(
            f"[TREE] 🔧 real duplicate locked-profit rows found and merged ({individual} -> "
            f"${total:.2f} total, no dollars lost) - this is what was crashing get_locked_usd()"
        )
    except Exception as e:
        log.warning(f"[TREE] could not dedupe locked-profit state: {e}")


async def _dedupe_family_tree_positions():
    """One-time cleanup, safe to call on every startup: a real production
    crash confirmed this happened live - crypto_tree_xrp_usd's thread
    raised sqlalchemy.exc.MultipleResultsFound on EVERY cycle
    (_load_branch_position's scalar_one_or_none() can only handle 0 or 1
    row) because two BotPosition rows existed under its bot_name.

    Deliberately scoped to only the bot_names that currently exist as real
    CryptoTreeBranch rows (crypto_tree_* and the BTC root) - never touches
    prop_apex's or crypto_coinbase's rows in this same shared table, which
    legitimately hold many concurrent positions (one row per open symbol)
    by design, unlike a family-tree branch, which is a single-position
    engine where its bot_name IS meant to be the position's unique key.

    Every duplicate row represents qty this system has no way to tell
    apart from a real fill, so the fix sums qty (at a qty-weighted-average
    entry_price) rather than discarding one row's worth of real quantity -
    same reasoning _dedupe_locked_profit_state() uses for real dollars.
    Keeps the most recently opened row's target/stop, since that reflects
    whichever buy actually happened last."""
    try:
        async with AsyncSessionLocal() as db:
            bot_names = (await db.execute(select(CryptoTreeBranch.bot_name))).scalars().all()

        for bot_name in bot_names:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(BotPosition).where(BotPosition.bot == bot_name).order_by(BotPosition.id)
                )
                rows = result.scalars().all()
                if len(rows) <= 1:
                    continue

                total_qty = sum(r.qty for r in rows)
                merged_entry = (
                    sum(r.entry_price * r.qty for r in rows) / total_qty
                    if total_qty > 0 else rows[-1].entry_price
                )
                newest = rows[-1]
                survivor = rows[0]
                survivor.qty = total_qty
                survivor.entry_price = merged_entry
                survivor.target_price = newest.target_price
                survivor.stop_price = newest.stop_price
                survivor.opened_at = newest.opened_at
                survivor.peak_pct = newest.peak_pct
                for dupe in rows[1:]:
                    await db.delete(dupe)
                await db.commit()
            log.warning(
                f"[TREE] 🔧 real duplicate BotPosition rows found and merged for {bot_name} "
                f"({len(rows)} rows -> qty {total_qty:.8f} @ avg entry ${merged_entry:,.4f}, no quantity lost) - "
                f"this is what was crashing _load_branch_position()"
            )
    except Exception as e:
        log.warning(f"[TREE] could not dedupe family-tree positions: {e}")


async def _ensure_trading_bot_state_unique_index():
    """One-time safety migration, safe to call on every startup: the
    TradingBotState model has always declared bot_name unique=True, but
    Base.metadata.create_all() only applies that to a table at CREATE
    time - it never retroactively adds a missing constraint to a table
    that already existed, which is exactly why the real duplicate rows
    _dedupe_locked_profit_state() just cleaned up were able to exist in
    the first place. Adds the real DB-level index the model always
    claimed to have, so the same race can never recreate a duplicate for
    ANY bot_name in this shared table again - not just the locked-profit
    key, every caller across the app that keys off TradingBotState.bot_name.

    Run this AFTER the dedupe above - a unique index cannot be created
    while real duplicates still exist. If some other, unrelated bot_name
    also has duplicates this migration doesn't know how to safely merge,
    this logs a warning and leaves the constraint absent rather than
    guessing at a merge for data it doesn't understand - same defensive
    pattern as _ensure_product_id_unique_index above."""
    try:
        async with AsyncSessionLocal() as db:
            dupes = await db.execute(text(
                "SELECT bot_name, COUNT(*) FROM trading_bot_state "
                "GROUP BY bot_name HAVING COUNT(*) > 1"
            ))
            dupe_rows = dupes.fetchall()
            if dupe_rows:
                log.warning(f"[TREE] duplicate trading_bot_state rows still exist for other keys ({dupe_rows}) - "
                            f"skipping unique index, this exact race is still possible for those keys")
                return
            await db.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_trading_bot_state_bot_name_unique "
                "ON trading_bot_state (bot_name)"
            ))
            await db.commit()
        log.info("[TREE] trading_bot_state.bot_name uniqueness enforced at the DB level - this exact crash can't recur")
    except Exception as e:
        log.warning(f"[TREE] could not add trading_bot_state uniqueness constraint: {e}")


async def _migrate_matic_to_pol():
    """One-time backstop, safe to call on every startup: Coinbase
    permanently disabled MATIC-USD trading on Oct 14, 2025 and migrated
    everything to POL-USD (Polygon's real token upgrade - confirmed via
    Coinbase's own migration help page, not a guess) - a branch already
    holding "MATIC-USD" as its product_id would be stuck flat forever,
    retrying a buy against a product_id Coinbase no longer recognizes
    ("INVALID_ARGUMENT: Invalid product_id" on every attempt, confirmed
    live). Renaming COIN_FAMILY_TREE's entry alone doesn't fix an
    already-stuck branch - a branch buys against its own stored
    product_id directly, never re-reading COIN_FAMILY_TREE at buy time -
    so this migrates any row still on the dead pair straight to POL-USD,
    the same coin under its real current identifier."""
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(CryptoTreeBranch).where(CryptoTreeBranch.product_id == "MATIC-USD")
            )
            rows = result.scalars().all()
            if not rows:
                return
            for row in rows:
                row.product_id = "POL-USD"
            await db.commit()
        log.info(
            f"[TREE] 🔄 migrated {len(rows)} branch(es) off the dead MATIC-USD product_id to POL-USD "
            f"(Coinbase's real token migration): {', '.join(r.bot_name for r in rows)}"
        )
    except IntegrityError:
        log.warning("[TREE] could not migrate MATIC-USD -> POL-USD: another branch already holds POL-USD")
    except Exception as e:
        log.warning(f"[TREE] could not migrate MATIC-USD -> POL-USD: {e}")


async def find_most_volatile_unclaimed_coin(session):
    """Among the eligible family-tree coins (not excluded, not still
    cooling down from a recent sale), finds the most volatile coin that's
    ALSO currently bullish
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

    A real, previously-known gap, now fixed: "bullish over ~25 hours" is a
    coarse, medium-term signal with no protection against a coin that's
    ALREADY extended right now - the exact shape of loss the real
    coin-trade-history evidence showed (PEPE, DOGE, one of two XRP trades,
    all quick losers). Any candidate whose current RSI is at or above
    engine.ENTRY_MAX_RSI (65, matching prop_bot.py's own crypto overbought
    threshold) is skipped entirely here, in both the bullish path and the
    any-volatility fallback below - never buy into a coin that's already
    overbought, whichever path picks it. A candidate with no RSI yet
    (too little price history) is still eligible - this only excludes a
    confirmed-overbought reading, it doesn't require one.

    BTC-relative-strength filter, added after a real 30-day/21-coin
    backtest comparison (crypto_selection_backtest.py's
    run_btc_relative_strength_comparison(), run live on the backtest page)
    showed a net-positive ROI change on the majority of coins (15 of 21)
    when entries were gated on beating BTC-USD's own return over the same
    window - "up in isolation" is a much weaker signal than "beating the
    market's own benchmark asset" in a market where everything grinds up
    or down together, which is exactly what that 30-day sample showed
    (almost every coin was net negative in isolation; the coins that also
    beat BTC over the same stretch did meaningfully better). Requires each
    candidate's real simple return over the same ~25-hour window to exceed
    BTC-USD's real return over the identical window (alpha =
    coin_return - btc_return > 0) - the exact same comparison
    calculate_relative_strength() validated offline, now live instead of
    shadow-only. Fails OPEN (does not block) when BTC's own real return
    can't be fetched, matching the backtest gate's own documented
    behavior - a missing benchmark isn't grounds to block every candidate.

    Higher-timeframe (hourly SMA20/SMA50) trend filter, added the same way
    after a real 30-day/18-coin comparison (crypto_selection_backtest.py's
    run_higher_tf_trend_comparison()) showed a net-positive ROI change on
    15 of 18 coins, several substantially (ADA +24.6pp, DOT +23.2pp,
    SHIB +18.8pp) against only 3 made worse. Requires the candidate's own
    real hourly SMA20 to be above its SMA50 (see engine.get_higher_tf_trend) -
    a separate real hourly-candle fetch from the ~25h/5-min data every other
    check here uses, since that's the exact real window the backtest
    validated. Fails OPEN on missing/insufficient real hourly history -
    only a CONFIRMED downtrend blocks a candidate.

    If no non-overbought, BTC-beating coin is currently bullish, falls back to the
    highest volatility among the remaining non-overbought candidates
    rather than doing nothing. Returns (product_id, atr_pct), or
    (None, None) if every coin is excluded, overbought, or none have
    usable price data right now.

    Per the account owner's explicit choice, no longer excludes a coin
    just because another branch already holds it - multiple branches can
    trade the same coin at once, so this always picks the objectively
    best real-time candidate (by trend/volatility/RSI) rather than
    steering away from a coin that's already proving itself elsewhere in
    the tree.

    A coin sold within the last CYCLE_SECONDS (see
    _coin_sale_cooldown_active) is still skipped though - a real
    one-cycle cooldown so a branch (this one or another) can't
    immediately buy straight back into a coin that was JUST sold. After
    that single cycle, if it's still bullish, it's a normal candidate
    again like any other coin.

    Looks up every candidate concurrently rather than one at a time: with
    up to 37 coins and a 15s timeout per request, a sequential loop's
    worst case was minutes (a branch just sitting there, not trading,
    while it worked through the whole list) - running them all at once
    caps the whole search at whatever the single slowest request takes."""
    excluded = await get_effective_excluded_coins()
    candidates = [
        p for p in COIN_FAMILY_TREE
        if p not in excluded and not _coin_sale_cooldown_active(p)
    ]

    # BTC-USD's own return over the identical ~25h window is fetched once,
    # concurrently with every candidate, and compared against each
    # candidate's own coin_return below - not root's own coin (root never
    # calls this function), just the real benchmark every candidate gets
    # measured against. The real hourly SMA20/50 trend check runs
    # concurrently alongside all of it, in the same round trip.
    vol_results, trend_results = await asyncio.gather(
        asyncio.gather(
            engine.get_price_volatility_and_trend(session, "BTC-USD"),
            *(engine.get_price_volatility_and_trend(session, product_id) for product_id in candidates),
            return_exceptions=True,
        ),
        asyncio.gather(
            *(engine.get_higher_tf_trend(session, product_id) for product_id in candidates),
            return_exceptions=True,
        ),
    )
    btc_result, results = vol_results[0], vol_results[1:]
    btc_return = None
    if isinstance(btc_result, Exception):
        log.warning(f"[TREE] BTC-relative-strength: BTC-USD lookup failed ({btc_result}) - filter fails open this cycle")
    else:
        _, _, _, _, btc_return = btc_result

    best_bullish_id, best_bullish_atr = None, -1.0
    best_any_id, best_any_atr = None, -1.0
    for product_id, result, trend_ok in zip(candidates, results, trend_results):
        if isinstance(result, Exception):
            log.warning(f"[TREE] volatility lookup failed for {product_id}: {result}")
            continue
        price, atr_pct, is_bullish, rsi, coin_return = result
        if price is None or atr_pct is None:
            continue
        if rsi is not None and rsi >= engine.ENTRY_MAX_RSI:
            log.info(f"[TREE] {product_id}: skipping - RSI {rsi:.1f} is already overbought (>= {engine.ENTRY_MAX_RSI:.0f})")
            continue
        if btc_return is not None and coin_return is not None and (coin_return - btc_return) <= 0:
            log.info(
                f"[TREE] {product_id}: skipping - not beating BTC-USD over the same ~25h window "
                f"(coin {coin_return:+.2%} vs BTC {btc_return:+.2%})"
            )
            continue
        # Real, live SMA20/SMA50 (hourly) trend confirmation - promoted from
        # shadow-mode backtest after a real 30-day/18-coin comparison showed
        # a net-positive ROI change on 15 of 18 coins (see
        # engine.get_higher_tf_trend's docstring for the real numbers).
        # Fails OPEN (None) on missing/insufficient real hourly data - only
        # a CONFIRMED downtrend (False) blocks a candidate.
        if isinstance(trend_ok, Exception):
            trend_ok = None
        if trend_ok is False:
            log.info(f"[TREE] {product_id}: skipping - hourly SMA20/SMA50 trend is DOWN")
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
        log.info("[TREE] no eligible coin is currently bullish - falling back to highest volatility overall")
        return best_any_id, best_any_atr
    return None, None


async def get_live_coin_snapshot():
    """Real-time (NOT backtested) view of every family-tree coin's current
    status - built per the account owner's explicit request after looking
    at the 30-day backtest table and asking "I need to know what's bullish
    right now." That table replays 30 days of history; this instead reuses
    the exact same real, live checks find_most_volatile_unclaimed_coin()
    runs at the moment a branch actually needs to pick a coin - same
    ~25-hour bullish/ATR/RSI/BTC-relative-strength lookup, same exclusion
    and cooldown checks, same engine.ENTRY_MAX_RSI threshold - just
    reporting every coin's status instead of only returning the single
    best pick. Read-only, never places an order.

    Returns {"btc_return_25h": ..., "coins": [...]}, one row per coin in
    COIN_FAMILY_TREE, sorted eligible-right-now first, then by ATR%
    descending (the same volatility tiebreak the live picker uses). Each
    row's "exclusion_reason" names the REAL, specific cause when excluded
    (manual list, bad backtest+bad live combined, or outside the current
    top-N rotation) rather than a bare true/false - per the account
    owner's real, direct complaint that hot/bullish coins were showing a
    plain "Excluded" tag with no way to tell whether that was deserved."""
    exclusion_reasons = await get_effective_excluded_coins_with_reasons()
    excluded = set(exclusion_reasons.keys())
    async with engine.aiohttp.ClientSession() as session:
        vol_results, trend_results = await asyncio.gather(
            asyncio.gather(
                engine.get_price_volatility_and_trend(session, "BTC-USD"),
                *(engine.get_price_volatility_and_trend(session, product_id) for product_id in COIN_FAMILY_TREE),
                return_exceptions=True,
            ),
            asyncio.gather(
                *(engine.get_higher_tf_trend(session, product_id) for product_id in COIN_FAMILY_TREE),
                return_exceptions=True,
            ),
        )
    btc_result, coin_results = vol_results[0], vol_results[1:]
    btc_return = None
    if isinstance(btc_result, Exception):
        log.warning(f"[TREE] live snapshot: BTC-USD lookup failed ({btc_result}) - BTC-relative filter shown as N/A")
    else:
        _, _, _, _, btc_return = btc_result

    snapshot = []
    for product_id, result, trend_ok in zip(COIN_FAMILY_TREE, coin_results, trend_results):
        if isinstance(trend_ok, Exception):
            trend_ok = None
        row = {
            "product_id": product_id,
            "excluded": product_id in excluded,
            "exclusion_reason": exclusion_reasons.get(product_id),
            "cooldown": _coin_sale_cooldown_active(product_id),
        }
        if isinstance(result, Exception) or result is None:
            row.update({
                "price": None, "atr_pct": None, "is_bullish": None, "rsi": None,
                "coin_return": None, "btc_return": btc_return, "alpha": None,
                "overbought": None, "beats_btc": None, "higher_tf_uptrend": trend_ok, "eligible_now": False,
                "error": str(result) if isinstance(result, Exception) else "no real price data available",
            })
            snapshot.append(row)
            continue

        price, atr_pct, is_bullish, rsi, coin_return = result
        if price is None or atr_pct is None:
            row.update({
                "price": None, "atr_pct": None, "is_bullish": None, "rsi": None,
                "coin_return": None, "btc_return": btc_return, "alpha": None,
                "overbought": None, "beats_btc": None, "higher_tf_uptrend": trend_ok, "eligible_now": False,
                "error": "no real price data available",
            })
            snapshot.append(row)
            continue

        overbought = rsi is not None and rsi >= engine.ENTRY_MAX_RSI
        alpha = (coin_return - btc_return) if (coin_return is not None and btc_return is not None) else None
        beats_btc = alpha is None or alpha > 0  # fails open, matching the live picker's own documented behavior
        eligible_now = (
            not row["excluded"] and not row["cooldown"] and not overbought and beats_btc and trend_ok is not False
        )
        row.update({
            "price": price, "atr_pct": atr_pct, "is_bullish": is_bullish, "rsi": rsi,
            "coin_return": coin_return, "btc_return": btc_return, "alpha": alpha,
            "overbought": overbought, "beats_btc": beats_btc, "higher_tf_uptrend": trend_ok,
            "eligible_now": eligible_now,
        })
        snapshot.append(row)

    snapshot.sort(key=lambda r: (not r["eligible_now"], not r["is_bullish"], -(r["atr_pct"] or -1)))
    return {"btc_return_25h": btc_return, "coins": snapshot}


async def get_reconciliation_report():
    """Real DB-vs-Coinbase reconciliation, built directly off the phantom-
    position self-heal fix above: the dashboard showing "22 branches
    holding positions" is misleading on its own - a DB BotPosition row
    proves the bot THINKS it holds an asset, not that Coinbase actually
    has it right now. This makes that gap directly visible instead of only
    discovering it reactively when a stuck sell finally surfaces it.

    Deliberately grouped by asset currency, NOT by individual branch:
    since branches can share a coin, Coinbase's real balance for an asset
    is POOLED across every branch holding it - comparing one branch's own
    qty against the full pooled balance would flag a false mismatch on
    every shared coin (most of the tree, by design, since the shared-coin
    change shipped). The correct comparison is real_balance vs the SUM of
    every branch's tracked qty for that same asset.

    Read-only - fetches real balances, never places an order or touches
    the DB. One row per asset currently held by 1+ branches:
    {currency, branch_count, tracked_qty, real_balance, status} where
    status is "ok" (real balance covers the tracked sum, within a small
    tolerance for fee/rounding dust), "SHORTFALL" (real balance is
    meaningfully below what the tree thinks it holds - the dangerous
    direction: a phantom position, or one about to become one), or
    "unchecked" (the real balance lookup itself failed).

    Real bug fixed here: bot_positions is a table SHARED across every bot
    in this codebase (prop_apex's Alpaca futures, crypto_coinbase, and
    every family-tree branch), distinguished only by the bot column - an
    earlier version of this query had no filter at all, so it pulled in
    prop_apex's real Alpaca futures positions (MES, MNQ, ...) too. Those
    have no "-" in their symbol, so pos.symbol.split("-")[0] returned the
    literal contract code as a "currency" and looked it up against real
    Coinbase balances - which of course never have an MES/MNQ account,
    producing a false SHORTFALL on a real Alpaca position that was never
    a Coinbase asset to begin with. Scoped to only real family-tree branch
    bot_names below, so this panel only ever reports on what it's actually
    meant to: Coinbase coins the tree itself tracks."""
    async with AsyncSessionLocal() as db:
        branch_result = await db.execute(select(CryptoTreeBranch.bot_name))
        tree_bot_names = {row[0] for row in branch_result.all()}
        result = await db.execute(select(BotPosition).where(BotPosition.bot.in_(tree_bot_names)))
        positions = result.scalars().all()

    tracked_by_currency = {}
    branch_count_by_currency = {}
    for pos in positions:
        currency = pos.symbol.split("-")[0]
        tracked_by_currency[currency] = tracked_by_currency.get(currency, 0.0) + pos.qty
        branch_count_by_currency[currency] = branch_count_by_currency.get(currency, 0) + 1

    async with engine.aiohttp.ClientSession() as session:
        all_balances, fetch_err = await engine.get_all_asset_balances(session)

    report = []
    for currency, tracked_qty in tracked_by_currency.items():
        if all_balances is None:
            report.append({
                "currency": currency, "branch_count": branch_count_by_currency[currency],
                "tracked_qty": round(tracked_qty, 8), "real_balance": None,
                "status": "unchecked", "detail": fetch_err,
            })
            continue
        real_balance = all_balances.get(currency, 0.0)
        # Small proportional tolerance for real fee/dust rounding, not
        # a strict equality check - a few thousandths of a unit off is
        # normal and not a real discrepancy.
        shortfall = tracked_qty - real_balance
        tolerance = max(tracked_qty * 0.01, 1e-6)
        status = "SHORTFALL" if shortfall > tolerance else "ok"
        report.append({
            "currency": currency, "branch_count": branch_count_by_currency[currency],
            "tracked_qty": round(tracked_qty, 8), "real_balance": round(real_balance, 8),
            "status": status, "detail": None,
        })

    report.sort(key=lambda r: (r["status"] != "SHORTFALL", r["currency"]))
    return {"assets": report, "shortfall_count": sum(1 for r in report if r["status"] == "SHORTFALL")}


async def _load_branch_position(bot_name: str):
    """A real production crash (sqlalchemy.exc.MultipleResultsFound) hit
    crypto_tree_xrp_usd's thread on every single cycle once two BotPosition
    rows existed under its bot_name - scalar_one_or_none() can only handle
    0 or 1 row. Unlike prop_bot.py/crypto_coinbase_bot.py, which
    legitimately hold several concurrent positions under one shared bot
    value (one row per open symbol), each family-tree branch is a
    single-position engine - more than one row under the same bot_name is
    always a bug here. _dedupe_family_tree_positions() cleans up any
    duplicate that already exists, and _save_branch_position now clears any
    existing row before inserting a new one so this can't recur - taking
    the most recent row here (order by id desc) is kept anyway as defense
    in depth, so a stray duplicate degrades gracefully instead of crashing
    this branch's thread every cycle."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(BotPosition).where(BotPosition.bot == bot_name).order_by(BotPosition.id.desc())
        )
        return result.scalars().first()


async def _save_branch_position(bot_name, product_id, entry_price, qty, target_price, stop_price):
    # Defensive: clear any existing row(s) for this bot_name first, so this
    # can never leave two rows behind under the same bot_name - see
    # _dedupe_family_tree_positions() for the real duplicate-row crash this
    # guards against recurring.
    await _clear_branch_position(bot_name)
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
        result = await db.execute(
            select(BotPosition).where(BotPosition.bot == bot_name).order_by(BotPosition.id.desc())
        )
        pos = result.scalars().first()
        if pos and pos.stop_price is not None and pos.stop_price < entry_price:
            pos.stop_price = entry_price
            await db.commit()


async def _update_branch_position_peak(bot_name: str, new_peak_usd: float):
    """Persists a new high-water mark for a position's unrealized dollar
    profit - see MAX_PROFIT_GIVEBACK_USD. Reuses BotPosition.peak_pct as a
    dollar figure for this bot only; only ever called with a value higher
    than what's already stored."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(BotPosition).where(BotPosition.bot == bot_name).order_by(BotPosition.id.desc())
        )
        pos = result.scalars().first()
        if pos:
            pos.peak_pct = new_peak_usd
            await db.commit()


def compute_sell_advice(entry_price: float, qty: float, target_price: float, stop_price: float,
                         current_price: float, stored_peak_usd) -> dict:
    """Real-time "is now a good time to sell this" advisory, backing the
    dashboard's 💡 Sell advice button (per the account owner's explicit
    request, after being talked out of a thin, fee-losing take-profit on
    BTC and asking for that same reasoning available on demand instead of
    typed out by hand each time). Deliberately reuses the EXACT same three
    real exit checks run_branch_cycle() evaluates every cycle - TARGET,
    STOP, and PEAK PROFIT GIVEBACK (see MAX_PROFIT_GIVEBACK_USD above) -
    not a separate heuristic, so this can never tell the account owner
    something different from what the bot itself is actually about to do.

    Verdict is one of:
      "sell"  - an automatic exit condition is already true; the bot is
                about to do this on its own next cycle regardless.
      "watch" - getting close to the giveback cap, not there yet.
      "hold"  - none of the above; real net profit after fees still needs
                the target to get meaningfully positive."""
    unrealized_usd = qty * (current_price - entry_price)
    exit_fee_usd = current_price * qty * (ROUND_TRIP_FEE_RATE / 2)
    net_after_fees = unrealized_usd - exit_fee_usd
    stored_peak = stored_peak_usd or 0.0
    peak_giveback = (stored_peak - unrealized_usd) if stored_peak > 0 else 0.0
    giveback_exceeded = stored_peak > 0 and peak_giveback >= MAX_PROFIT_GIVEBACK_USD
    pct_to_target = (target_price / current_price - 1) * 100 if current_price else None

    if current_price >= target_price:
        return {"verdict": "sell", "reason": (
            f"Target hit (${target_price:,.2f}) - real net profit after fees is about "
            f"${net_after_fees:,.2f}. The bot will sell this automatically on its next "
            f"cycle if you don't beat it to it."
        )}
    if current_price <= stop_price:
        return {"verdict": "sell", "reason": (
            f"At or below stop (${stop_price:,.2f}) - the bot is about to force-exit "
            f"this on its next cycle to cap the loss. No reason to wait."
        )}
    if giveback_exceeded:
        return {"verdict": "sell", "reason": (
            f"Given back ${peak_giveback:,.2f} of its ${stored_peak:,.2f} peak profit, "
            f"past the ${MAX_PROFIT_GIVEBACK_USD:,.2f} giveback cap - the bot is about "
            f"to force-sell this automatically to lock in what's left."
        )}
    if stored_peak > 0 and peak_giveback >= MAX_PROFIT_GIVEBACK_USD * 0.6:
        return {"verdict": "watch", "reason": (
            f"Pulled back ${peak_giveback:,.2f} from its ${stored_peak:,.2f} peak - "
            f"within ${(MAX_PROFIT_GIVEBACK_USD - peak_giveback):,.2f} of the automatic "
            f"giveback-cap exit. Worth watching closely, but not a clear sell yet."
        )}
    if net_after_fees <= 0:
        return {"verdict": "hold", "reason": (
            f"Selling right now would net about ${net_after_fees:,.2f} after real "
            f"round-trip fees - not a real profit yet. Target is "
            f"{pct_to_target:.2f}% away at ${target_price:,.2f}."
        )}
    return {"verdict": "hold", "reason": (
        f"Real net profit right now is only about ${net_after_fees:,.2f} after fees - "
        f"target (${target_price:,.2f}) is {pct_to_target:.2f}% further out for a real "
        f"win, and the stop is protecting the downside in the meantime."
    )}


async def _clear_branch_position(bot_name: str):
    """Deletes every BotPosition row under this bot_name, not just one -
    defense against a stray duplicate (see _load_branch_position) lingering
    behind after a sell instead of being fully cleared."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(BotPosition).where(BotPosition.bot == bot_name))
        for pos in result.scalars().all():
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


async def _pick_weakest_branch_for_reinforcement(exclude_bot_name: str, also_exclude_bot_names: frozenset = frozenset()):
    """Picks the branch with the lowest real progress toward its own next
    spawn tier (allocated_usd / next_unlock_tier) - the exact same real
    percentage shown on the dashboard's "Next spawn" bars. Excludes the
    branch that's doing the spawning itself - it just crossed its OWN tier
    and is about to have that tier raised, so it would often (wrongly)
    look "weakest" right at this moment and end up reinforcing itself,
    which is pointless (functionally the same as not spawning at all).

    Also excludes any branch whose CURRENT coin is presently excluded
    (manual or automatic layer - see get_effective_excluded_coins). Real
    gap found live: POL-USD topped the 30-day backtest ranking right as
    the top-15 rotation shipped, a spawn storm landed 15 of 23 branches
    on it, and its real live performance turned out to be terrible (39
    trades, 12.8% win rate, -$310.66). Since a losing coin keeps its
    branches weak, and this function used to have zero awareness of coin
    exclusion, "always reinforce the weakest" was a closed loop feeding
    fresh money back into whichever coin was already losing the most -
    reinforcement bypassed exclusion entirely, so even flagging a coin
    excluded couldn't stop it. Now a branch stuck on an excluded coin is
    skipped for reinforcement (it still trades normally under its own
    target/stop, it just isn't handed more capital) in favor of the next
    real weakest branch on a coin that's actually still eligible.

    also_exclude_bot_names: an additional set of bot_names to skip, on top
    of exclude_bot_name - used when a just-picked weakest branch's real
    reinforcement buy permanently fails (see _maybe_spawn_child) and a
    different candidate needs to be picked in the same call, instead of
    retrying the identical doomed order against the same branch forever
    next cycle (real gap found live: a flat, permanently-rejected branch's
    balance never changes, so it stays "weakest" and gets picked again
    every single cycle).

    Returns the CryptoTreeBranch row, or None if there's no other real
    eligible branch to reinforce (e.g. the very first spawn in a fresh
    tree, or every other branch's coin is currently excluded)."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(CryptoTreeBranch))
        branches = result.scalars().all()
    excluded_coins = await get_effective_excluded_coins()
    candidates = [
        b for b in branches
        if b.bot_name != exclude_bot_name and b.bot_name not in also_exclude_bot_names
        and b.next_unlock_tier and b.next_unlock_tier > 0
        and b.product_id not in excluded_coins
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda b: b.allocated_usd / b.next_unlock_tier)


async def _deploy_seed_into_weakest_branch(session, target_bot_name: str, usd_amount: float) -> bool:
    """Places a real market buy for usd_amount into target_bot_name's
    current coin and blends it into its existing position (or opens a
    fresh one if flat) - the same real quantity-weighted blended-entry and
    target/stop recompute logic the dashboard's "Add cash to {coin}"
    button already uses (add_cash_to_branch in routers/trading_dashboard.py),
    reimplemented here so the automatic every-other-spawn reinforcement
    path can call it without a FastAPI/HTTPException dependency. Returns
    True on a real successful deploy; False (order didn't fill, or price
    data unavailable) leaves it to the caller to not lose the seed money."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(CryptoTreeBranch).where(CryptoTreeBranch.bot_name == target_bot_name))
        target_branch = result.scalar_one_or_none()
    if target_branch is None:
        return False

    price, atr_pct = await engine.get_price_and_volatility(session, target_branch.product_id)
    if price is None or atr_pct is None:
        log.warning(f"[TREE] reinforcement: could not fetch a live price for {target_branch.product_id} - skipping this turn")
        return False

    fill = await engine.place_market_buy(session, usd_amount, target_branch.product_id)
    if not fill:
        stuck_reason = engine._last_order_error.get(target_branch.product_id, "unknown reason")
        log.warning(f"[TREE] reinforcement: real buy into {target_bot_name} ({target_branch.product_id}) did not fill: {stuck_reason}")
        return False
    filled_qty, filled_price = fill

    existing_position = await _load_branch_position(target_bot_name)
    if existing_position is not None:
        new_qty = existing_position.qty + filled_qty
        blended_entry = (
            existing_position.qty * existing_position.entry_price + filled_qty * filled_price
        ) / new_qty
    else:
        new_qty = filled_qty
        blended_entry = filled_price

    position_dollar_size = new_qty * blended_entry
    target_pct = max(engine.pick_target_pct(atr_pct), engine.min_profit_target_pct(position_dollar_size, atr_pct))
    target_price = blended_entry * (1 + target_pct)
    stop_price = blended_entry * (1 - STOP_LOSS_PCT)
    await _save_branch_position(target_bot_name, target_branch.product_id, blended_entry, new_qty, target_price, stop_price)

    new_balance = None
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(CryptoTreeBranch).where(CryptoTreeBranch.bot_name == target_bot_name))
        fresh = result.scalar_one_or_none()
        if fresh:
            fresh.allocated_usd += usd_amount
            new_balance = fresh.allocated_usd
            await db.commit()

    log.info(
        f"[TREE] 💪 reinforcement: bought {filled_qty:.8f} {target_branch.product_id} @ ${filled_price:,.2f} into "
        f"{target_bot_name} (was weakest at spawn time) - blended entry now ${blended_entry:,.2f}"
        + (f", branch total now ${new_balance:.2f}" if new_balance is not None else "")
    )
    return True


async def consolidate_branches_by_coin(dry_run: bool = True) -> dict:
    """Per the account owner's explicit request: merge every branch
    currently sharing the same coin into ONE branch - real bookkeeping
    consolidation only, no Coinbase order placed, no money created or
    destroyed. Real motivation: the shared-coin-branches feature let up to
    15 real branches pile onto POL-USD alone during the top-15-rotation
    spawn storm, and each branch independently tracks its own qty against
    a POOLED real Coinbase balance for that coin - the exact structural
    gap behind both the phantom-position self-heal and the DB-vs-Coinbase
    reconciliation SHORTFALLs found earlier this session. Merging every
    branch on a coin into one eliminates that gap for good on every coin
    it's applied to (one real tracked position, one thread managing it,
    nothing left to drift apart) and turns many thin, individually
    floor-fragile branches into one bigger, sturdier one - real branches
    helping each other by combining forces instead of quietly competing
    for the same pooled balance.

    For each product_id with 2+ real branches (BTC-USD, and any group
    containing the root branch, are always skipped - root never shares
    its coin with another branch by design):
    - allocated_usd: SUMMED across every branch in the group - no money
      created or destroyed, purely consolidated bookkeeping.
    - next_unlock_tier: the MAX of the group's tiers, deliberately
      conservative - the merge itself is a bookkeeping operation, not new
      capital, so it shouldn't artificially trigger an immediate spawn/
      reinforcement none of the branches separately earned yet.
    - equity_floor: recomputed fresh from the NEW combined balance via
      the same real tier formula every other floor self-heal in this file
      already uses - never just the max of the old floors, which could
      exceed the combined balance's own real bracket and immediately
      block trading.
    - Position: every branch's real tracked qty (if holding) is SUMMED
      into one real quantity-weighted average entry price - the same
      blended-entry math the "Add cash" button already uses - then
      target/stop are recomputed fresh off that blended entry via a live
      price/ATR fetch, same formula used everywhere else in this file.
    - Survivor: the branch with the LARGEST allocated_usd in the group
      keeps its identity (matches the existing "strongest sibling" throne
      philosophy already used elsewhere in this codebase); every other
      branch in the group is deleted after being folded in.

    dry_run=True (the default) computes and returns the full real plan -
    what would merge into what, and the resulting numbers - WITHOUT
    touching the database or placing any order, so the plan can be seen
    before it executes. dry_run=False executes it for real."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(CryptoTreeBranch))
        all_branches = result.scalars().all()

    groups = {}
    for b in all_branches:
        groups.setdefault(b.product_id, []).append(b)

    merge_groups = {
        pid: bs for pid, bs in groups.items()
        if len(bs) > 1 and pid != "BTC-USD" and not any(b.bot_name == ROOT_BOT_NAME for b in bs)
    }

    plan = []
    for product_id, branches in merge_groups.items():
        survivor = max(branches, key=lambda b: b.allocated_usd)
        losers = [b for b in branches if b.bot_name != survivor.bot_name]

        combined_allocated = sum(b.allocated_usd for b in branches)
        new_next_tier = max(b.next_unlock_tier for b in branches if b.next_unlock_tier)
        new_floor = _floor_tier_for_balance(combined_allocated)

        positions = []
        for b in branches:
            pos = await _load_branch_position(b.bot_name)
            if pos is not None:
                positions.append(pos)

        combined_qty = sum(p.qty for p in positions)
        blended_entry = (sum(p.qty * p.entry_price for p in positions) / combined_qty) if combined_qty > 0 else None

        plan.append({
            "product_id": product_id,
            "survivor_bot_name": survivor.bot_name,
            "merged_bot_names": [b.bot_name for b in losers],
            "combined_allocated_usd": round(combined_allocated, 2),
            "new_next_unlock_tier": new_next_tier,
            "new_equity_floor": new_floor,
            "combined_qty": combined_qty,
            "blended_entry_price": blended_entry,
        })

    if dry_run:
        return {"dry_run": True, "groups_to_merge": len(plan), "plan": plan}

    executed = []
    async with engine.aiohttp.ClientSession() as session:
        for item in plan:
            product_id = item["product_id"]
            survivor_bot_name = item["survivor_bot_name"]

            if item["combined_qty"] > 0:
                price, atr_pct = await engine.get_price_and_volatility(session, product_id)
                if price is not None and atr_pct is not None:
                    position_dollar_size = item["combined_qty"] * item["blended_entry_price"]
                    target_pct = max(engine.pick_target_pct(atr_pct), engine.min_profit_target_pct(position_dollar_size, atr_pct))
                    target_price = item["blended_entry_price"] * (1 + target_pct)
                    stop_price = item["blended_entry_price"] * (1 - STOP_LOSS_PCT)
                    await _save_branch_position(
                        survivor_bot_name, product_id, item["blended_entry_price"],
                        item["combined_qty"], target_price, stop_price,
                    )
                else:
                    log.warning(
                        f"[TREE] consolidate: could not fetch a live price for {product_id} - merged the "
                        f"bookkeeping for {survivor_bot_name} but left its position row untouched this pass"
                    )

            async with AsyncSessionLocal() as db:
                result = await db.execute(select(CryptoTreeBranch).where(CryptoTreeBranch.bot_name == survivor_bot_name))
                survivor_row = result.scalar_one_or_none()
                if survivor_row:
                    survivor_row.allocated_usd = item["combined_allocated_usd"]
                    survivor_row.next_unlock_tier = item["new_next_unlock_tier"]
                    survivor_row.equity_floor = item["new_equity_floor"]
                    await db.commit()

                for loser_bot_name in item["merged_bot_names"]:
                    result2 = await db.execute(select(CryptoTreeBranch).where(CryptoTreeBranch.bot_name == loser_bot_name))
                    loser_row = result2.scalar_one_or_none()
                    if loser_row:
                        await db.delete(loser_row)
                await db.commit()

            for loser_bot_name in item["merged_bot_names"]:
                await _clear_branch_position(loser_bot_name)

            executed.append(item)
            consolidate_msg = (
                f"🔗 Merged {len(item['merged_bot_names']) + 1} branches on {product_id} into "
                f"{survivor_bot_name} - combined balance ${item['combined_allocated_usd']:,.2f} "
                f"(no coins sold, real balances summed: {', '.join(item['merged_bot_names'])} folded in)"
            )
            log.info(f"[TREE] {consolidate_msg}")
            await _log_activity(survivor_bot_name, product_id, "CONSOLIDATE", consolidate_msg)

    return {"dry_run": False, "groups_merged": len(executed), "plan": executed}


async def liquidate_family_tree_and_buy_btc() -> dict:
    """Per the account owner's explicit, real decision (mirrors
    prop_bot.py's liquidate-and-buy-SPY on the Alpaca side): retire the
    ENTIRE crypto family tree and consolidate everything into one real
    buy-and-hold BTC position on the permanent root branch. A ONE-WAY
    real action:

    1. Sells every real position held by every NON-root branch at market
       (place_market_sell(), which clamps to the real Coinbase balance
       itself - no separate balance check needed here), records each real
       fill as a CryptoCoinTradeHistory row (exit_reason "RETIRED_TO_BTC")
       so nothing vanishes from the real per-coin trade history, and
       deletes every non-root branch row once flat (its own allocated_usd
       was only ever a bookkeeping split of the same shared real Coinbase
       cash - deleting the row simply stops earmarking it, the real
       dollars become free cash automatically).
    2. Sets is_crypto_passive_mode() to True BEFORE buying, so nothing can
       race in and spawn/reinforce/enter in the gap between everything
       being sold and the BTC buy landing - every branch thread's
       run_branch_cycle() checks this first thing and does nothing at all
       once it's set.
    3. Buys real BTC-USD with the real free cash (real Coinbase balance
       minus whatever is genuinely locked profit - locked profit is never
       auto-spent, matching every other real spend path in this file),
       minus a small safety buffer (same reasoning as every other
       real-balance clamp in this codebase), and blends it into root's
       EXISTING BTC position with the same real quantity-weighted average
       entry price and recomputed target/stop the "Add cash" button
       already uses - root keeps whatever real position it already held,
       this only adds to it.
    4. Bumps root's own allocated_usd by the real amount just spent - its
       pre-existing balance (never touched by this liquidation) plus every
       other branch's freed real capital now consolidated into it.

    Returns a real summary: what was closed and its P&L, how much was
    spent on BTC, and the resulting real fill."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(CryptoTreeBranch))
        all_branches = result.scalars().all()

    root = next((b for b in all_branches if b.bot_name == ROOT_BOT_NAME), None)
    others = [b for b in all_branches if b.bot_name != ROOT_BOT_NAME]

    closed = []
    async with engine.aiohttp.ClientSession() as session:
        for b in others:
            pos = await _load_branch_position(b.bot_name)
            if pos is not None:
                fill = await engine.place_market_sell(session, pos.qty, b.product_id)
                if fill:
                    filled_qty, filled_price = fill
                    pnl = round((filled_price - pos.entry_price) * filled_qty, 2)
                    async with AsyncSessionLocal() as db:
                        db.add(CryptoCoinTradeHistory(
                            product_id=b.product_id, bot_name=b.bot_name,
                            entry_price=pos.entry_price, exit_price=filled_price, qty=filled_qty,
                            pnl=pnl, exit_reason="RETIRED_TO_BTC",
                        ))
                        await db.commit()
                    closed.append({"bot_name": b.bot_name, "product_id": b.product_id, "qty": filled_qty, "exit_price": filled_price, "realized_pnl": pnl})
                    log.info(f"[TREE] 🔒 retiring {b.bot_name}: sold {filled_qty:.8f} {b.product_id} @ ${filled_price:,.2f} | P&L ${pnl:.2f}")
                else:
                    reason = engine._last_order_error.get(b.product_id, "unknown reason")
                    closed.append({"bot_name": b.bot_name, "product_id": b.product_id, "qty": 0.0, "exit_price": None, "realized_pnl": 0.0, "note": f"sell did not fill: {reason}"})
                    log.warning(f"[TREE] 🔒 retiring {b.bot_name}: real sell of {b.product_id} did not fill ({reason})")
                await _clear_branch_position(b.bot_name)

        async with AsyncSessionLocal() as db:
            for b in others:
                result = await db.execute(select(CryptoTreeBranch).where(CryptoTreeBranch.bot_name == b.bot_name))
                row = result.scalar_one_or_none()
                if row:
                    await db.delete(row)
            await db.commit()

        await set_crypto_passive_mode(True)

        real_balance, err = await engine.get_usd_balance(session)
        if real_balance is None:
            log.warning(f"[TREE] 🔒 could not fetch real USD balance to buy BTC: {err} - passive mode still enabled")
            return {"status": "closed_only", "closed_positions": closed, "passive_mode": True, "error": f"Could not fetch real USD balance to buy BTC: {err}"}

        locked = await get_locked_usd()
        spendable = max(0.0, real_balance - locked)

        if spendable < 1.0:
            log.warning(f"[TREE] 🔒 only ${spendable:.2f} real spendable cash after closing - not enough to buy BTC, passive mode still enabled")
            return {
                "status": "closed_only", "closed_positions": closed, "passive_mode": True,
                "cash_after_closing": round(real_balance, 2),
                "note": "Real spendable cash after closing was too small to buy BTC. The tree is still retired - nothing will trade on its own.",
            }

        spend = round(spendable * 0.995, 2)
        price, atr_pct = await engine.get_price_and_volatility(session, ROOT_PRODUCT_ID)
        if price is None or atr_pct is None:
            log.warning("[TREE] 🔒 could not fetch a live BTC-USD price - passive mode still enabled, no buy attempted")
            return {
                "status": "closed_only", "closed_positions": closed, "passive_mode": True,
                "cash_after_closing": round(real_balance, 2),
                "note": "Could not fetch a live BTC-USD price to size the buy. The tree is still retired - retry the buy manually or via this endpoint again.",
            }

        fill = await engine.place_market_buy(session, spend, ROOT_PRODUCT_ID)
        if not fill:
            reason = engine._last_order_error.get(ROOT_PRODUCT_ID, "unknown reason")
            log.error(f"[TREE] 🔒 real BTC buy failed: {reason} - passive mode still enabled")
            return {
                "status": "closed_no_buy", "closed_positions": closed, "passive_mode": True,
                "cash_after_closing": round(real_balance, 2),
                "error": f"Closed every other real position, but the real BTC buy failed: {reason}. The tree is still retired - retry the buy manually or via this endpoint again.",
            }
        filled_qty, filled_price = fill

        existing_position = await _load_branch_position(ROOT_BOT_NAME)
        if existing_position is not None:
            new_qty = existing_position.qty + filled_qty
            blended_entry = (existing_position.qty * existing_position.entry_price + filled_qty * filled_price) / new_qty
        else:
            new_qty = filled_qty
            blended_entry = filled_price

        position_dollar_size = new_qty * blended_entry
        target_pct = max(engine.pick_target_pct(atr_pct), engine.min_profit_target_pct(position_dollar_size, atr_pct))
        target_price = blended_entry * (1 + target_pct)
        stop_price = blended_entry * (1 - STOP_LOSS_PCT)
        await _save_branch_position(ROOT_BOT_NAME, ROOT_PRODUCT_ID, blended_entry, new_qty, target_price, stop_price)

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(CryptoTreeBranch).where(CryptoTreeBranch.bot_name == ROOT_BOT_NAME))
            root_row = result.scalar_one_or_none()
            new_root_balance = None
            if root_row:
                root_row.allocated_usd = round((root_row.allocated_usd or 0.0) + spend, 2)
                new_root_balance = root_row.allocated_usd
                await db.commit()

    root_balance_note = f"${new_root_balance:,.2f}" if new_root_balance is not None else "unknown (root row missing)"
    log.info(
        f"[TREE] 🔒📈 Retired the family tree - closed {len(others)} other branch(es), bought {filled_qty:.8f} "
        f"BTC-USD @ ${filled_price:,.2f} (${spend:,.2f}) - root now {root_balance_note}"
    )
    return {
        "status": "liquidated_and_bought_btc",
        "closed_positions": closed,
        "cash_after_closing": round(real_balance, 2),
        "spend_on_btc": spend,
        "btc_filled_qty": filled_qty,
        "btc_filled_price": round(filled_price, 2),
        "root_new_balance": round(new_root_balance, 2) if new_root_balance is not None else None,
        "passive_mode": True,
    }


async def _maybe_spawn_child(branch, allow_reinforce: bool = True):
    """Called right after a branch's allocated_usd is updated (always right
    after a real sell, when the number is freshly accurate). If it just
    crossed a new unlock tier, isn't floor-breached, and a coin remains
    unclaimed, spins off a new branch - a bookkeeping transfer only.

    allow_reinforce=False is used for exactly one case: settling a
    reinforcement RECIPIENT immediately (see the reinforcement block below).
    It hard-disables this call from reinforcing anyone else - it can only
    spawn a brand-new branch or do nothing - which makes a bounce back to
    an existing branch structurally impossible, not just unlikely. This is
    deliberately narrower than the full recursive recheck that was tried
    and reverted earlier (see the account owner's real ping-pong bug)."""
    if branch.allocated_usd < branch.next_unlock_tier:
        return
    if branch.allocated_usd < branch.equity_floor:
        # Real gap found live: crypto_btc_compound sat at $121.93 vs a
        # $150.00 floor while HOLDING a healthy, breakeven-protected
        # position (+0.09%) - the ~$28 gap couldn't be explained by this
        # position's own price movement, so it traces back to a real,
        # legitimate spawn deduction (giving a child its $50 seed) that
        # happened while the balance was above $150, leaving the parent
        # permanently unable to spawn again until its current position
        # happened to sell (only _branch_sell_and_settle's post-sale
        # reset, or the separate flat-branch self-heal in
        # run_branch_cycle, ever lower a floor - neither fires for a
        # branch that's still holding). A deliberate spawn deduction
        # isn't the "real trading loss" the floor ratchet exists to
        # guard against, so self-heal it the same way those other two
        # paths already do: lower the floor to match this branch's own
        # real current tier, then spawn immediately rather than waiting
        # on a sale that might not happen for a long time. Doesn't touch
        # the held position's own risk management at all - the floor
        # breach only ever force-sells a healthy held position when its
        # OWN stop has also failed, which this doesn't change either way.
        new_tier_floor = _floor_tier_for_balance(branch.allocated_usd)
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(CryptoTreeBranch).where(CryptoTreeBranch.bot_name == branch.bot_name))
            row = result.scalar_one_or_none()
            if row and row.allocated_usd < row.equity_floor:
                old_floor = row.equity_floor
                row.equity_floor = new_tier_floor
                await db.commit()
                branch.equity_floor = new_tier_floor
                log.info(
                    f"[TREE] 🪜 {branch.bot_name} floor self-healed ${old_floor:,.2f} -> ${new_tier_floor:,.2f} "
                    f"(real balance ${branch.allocated_usd:.2f} was below it, likely from a real spawn deduction) - can spawn again now"
                )

    # Per the account owner's explicit request: every single real spawn
    # reinforces whichever EXISTING branch is currently weakest (lowest %
    # toward its own next spawn tier) with a real buy, instead of starting
    # a brand-new branch - unconditionally, no threshold. This closes a
    # real gap the account owner spotted in the earlier 50%-threshold
    # version: a branch stuck partway (say 60-90%) that never dipped below
    # 50% never got help and could sit stagnant indefinitely. Now a new
    # branch only ever gets created once there's no OTHER branch left to
    # reinforce at all (see the None case below - the very first spawn in
    # a fresh tree, or a tree of exactly one branch).
    weakest = await _pick_weakest_branch_for_reinforcement(exclude_bot_name=branch.bot_name) if allow_reinforce else None
    if weakest is not None:
        own_increment = ROOT_UNLOCK_TIER_USD if branch.bot_name == ROOT_BOT_NAME else UNLOCK_TIER_USD
        milestone = branch.next_unlock_tier
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(CryptoTreeBranch).where(CryptoTreeBranch.bot_name == branch.bot_name))
            fresh = result.scalar_one_or_none()
            if not fresh or fresh.allocated_usd < fresh.next_unlock_tier:
                return
            fresh.allocated_usd -= SEED_USD
            fresh.next_unlock_tier += own_increment
            await db.commit()
            remaining = fresh.allocated_usd

        weakest_pct_before = (weakest.allocated_usd / weakest.next_unlock_tier) * 100
        async with engine.aiohttp.ClientSession() as session:
            deployed = await _deploy_seed_into_weakest_branch(session, weakest.bot_name, SEED_USD)

        if not deployed:
            # Real gap found live: crypto_btc_compound reinforced the same
            # flat POL branch every single cycle, hit a real permanent
            # rejection (UNSUPPORTED_ORDER_CONFIGURATION) every time, and
            # refunded - but since that branch's own balance never changes
            # on a failed attempt, it stayed the objectively "weakest"
            # candidate and got picked again next cycle, forever. Try ONE
            # different real candidate (excluding the one that just
            # permanently failed) in this same call, instead of waiting a
            # full cycle just to make the identical doomed attempt again.
            first_stuck_reason = engine._last_order_error.get(weakest.product_id, "unknown reason")
            if engine._is_permanent_order_rejection(first_stuck_reason):
                fallback = await _pick_weakest_branch_for_reinforcement(
                    exclude_bot_name=branch.bot_name, also_exclude_bot_names=frozenset({weakest.bot_name})
                )
                if fallback is not None:
                    weakest = fallback
                    weakest_pct_before = (weakest.allocated_usd / weakest.next_unlock_tier) * 100
                    async with engine.aiohttp.ClientSession() as session:
                        deployed = await _deploy_seed_into_weakest_branch(session, weakest.bot_name, SEED_USD)

        if deployed:
            reinforce_msg = (
                f"🌱💪 {branch.bot_name} crossed ${milestone:,.0f} - its ${SEED_USD:.2f} seed "
                f"went into {weakest.bot_name} (currently weakest at {weakest_pct_before:.0f}% toward its own next tier) "
                f"| {branch.bot_name} continues with ${remaining:.2f}"
            )
            log.info(f"[TREE] {reinforce_msg}")
            await _log_activity(branch.bot_name, weakest.product_id, "REINFORCE", reinforce_msg)
            # Settle the RECIPIENT immediately too, per the account owner's
            # real complaint that a branch sitting at 100% "Next spawn" was
            # visibly waiting up to a full cycle (~30s, felt like "a
            # minute") before anything happened. A full recursive recheck
            # was tried here before and reverted - it let the recipient
            # reinforce back whichever branch just gave it money, which
            # could bounce back and forth firing real Coinbase orders each
            # hop (confirmed live in testing). This is deliberately
            # narrower: allow_reinforce=False means the recipient, even if
            # this reinforcement pushed it over its OWN tier, can only ever
            # spawn a brand-new branch or do nothing - it can never send
            # money to another existing branch. That makes a bounce back to
            # branch.bot_name (or anyone else) structurally impossible, not
            # just unlikely, while still collapsing the wait to this same
            # call instead of the recipient's own next scheduled cycle.
            async with AsyncSessionLocal() as db:
                result = await db.execute(select(CryptoTreeBranch).where(CryptoTreeBranch.bot_name == weakest.bot_name))
                fresh_recipient = result.scalar_one_or_none()
            if fresh_recipient is not None:
                await _maybe_spawn_child(fresh_recipient, allow_reinforce=False)
        else:
            # Real buy failed (order rejection, price fetch failure) -
            # the seed was already deducted from the parent above, so
            # give it back rather than silently losing real allocated
            # dollars into nothing.
            async with AsyncSessionLocal() as db:
                result = await db.execute(select(CryptoTreeBranch).where(CryptoTreeBranch.bot_name == branch.bot_name))
                fresh2 = result.scalar_one_or_none()
                if fresh2:
                    fresh2.allocated_usd += SEED_USD
                    fresh2.next_unlock_tier -= own_increment
                    await db.commit()
            stuck_reason = engine._last_order_error.get(weakest.product_id, "unknown reason")
            fail_msg = (
                f"⚠️ {branch.bot_name} crossed ${milestone:,.0f} but the reinforcement buy into "
                f"{weakest.bot_name} ({weakest.product_id}) failed ({stuck_reason}) - refunded the "
                f"${SEED_USD:.2f} seed, will retry next cycle"
            )
            log.warning(f"[TREE] {fail_msg}")
            # Real gap found live: a repeatedly-failing reinforcement deploy
            # refunds back to the exact same balance/tier every cycle and
            # retries - which, with NO visible trace on the dashboard
            # before this (only a successful REINFORCE ever logged to the
            # activity feed), looked from the outside like the branch was
            # simply stuck frozen at 100% "Next spawn" forever, with no way
            # to tell a retry loop from an actual bug. Logging this failure
            # too makes a real, repeated failure visible and diagnosable
            # (the real rejection reason included) instead of silent.
            await _log_activity(branch.bot_name, weakest.product_id, "REINFORCE_FAILED", fail_msg)
        return
    # No other branch exists yet to reinforce (e.g. the very first
    # spawn in a fresh tree, or a tree of exactly one branch) - fall
    # through to the normal new-branch path below instead of silently
    # skipping this spawn opportunity.

    next_product = await get_next_eligible_product_id()
    if next_product is None:
        log.info(f"[TREE] {branch.bot_name} crossed ${branch.next_unlock_tier:,.0f} but every coin is excluded or cooling down - no child to spawn yet")
        return

    milestone = branch.next_unlock_tier
    remaining = None
    child_name = None
    # product_id no longer needs to be unique (branches can share a coin
    # now), but bot_name still does - a child's name is derived directly
    # from its coin (crypto_tree_{product}), so two DIFFERENT parents
    # crossing their own tier at nearly the same moment and both picking
    # the same next_product would collide trying to create the identical
    # child bot_name (the same real race the manual spawn buttons can hit -
    # see spawn_child_branch_with_retry). Retry with a freshly computed
    # name before giving up - each attempt's parent deduction and child
    # insert stay atomic (same transaction), so a failed attempt rolls
    # back cleanly with nothing stuck half-applied. Every branch's cycle
    # timer starts from roughly the same moment (server boot), so several
    # branches crossing their tier in the same window can race in
    # near-lockstep on the identical target coin - a real jitter sleep
    # between retries (see spawn_child_branch_with_retry for the full
    # story) breaks that lockstep instead of retrying at the exact same
    # instant a sibling's own retry does.
    last_error = None
    for attempt in range(12):
        if attempt > 0:
            await asyncio.sleep(random.uniform(0.05, 0.4))
        child_name = await _unique_child_bot_name(next_product, randomize=attempt >= 4)
        try:
            async with AsyncSessionLocal() as db:
                # Re-check against a fresh row under this transaction, so the
                # coordinator's scan and this branch's own cycle can't both
                # spawn a child for the same crossing.
                result = await db.execute(select(CryptoTreeBranch).where(CryptoTreeBranch.bot_name == branch.bot_name))
                fresh = result.scalar_one_or_none()
                if not fresh or fresh.allocated_usd < fresh.next_unlock_tier:
                    return
                fresh.allocated_usd -= SEED_USD
                # Root's own next requirement is deliberately lower than a
                # regular branch's (see ROOT_UNLOCK_TIER_USD) - the child
                # being spawned still gets the normal UNLOCK_TIER_USD for
                # its own first tier below.
                own_increment = ROOT_UNLOCK_TIER_USD if branch.bot_name == ROOT_BOT_NAME else UNLOCK_TIER_USD
                fresh.next_unlock_tier += own_increment
                db.add(CryptoTreeBranch(
                    bot_name=child_name, product_id=next_product, parent_bot_name=branch.bot_name,
                    allocated_usd=SEED_USD, next_unlock_tier=UNLOCK_TIER_USD, equity_floor=0.0,
                ))
                await db.commit()
                remaining = fresh.allocated_usd
            break
        except IntegrityError as e:
            # See the matching comment in spawn_child_branch_with_retry -
            # capture the real driver error text rather than assuming
            # every IntegrityError here means "bot_name taken".
            last_error = str(getattr(e, "orig", e))[:300]
            # A real product_id conflict (the old, supposedly-dropped
            # uniqueness index on which coin a branch holds - see
            # _drop_product_id_unique_index) can never be fixed by trying
            # a different bot_name, so stop burning the remaining
            # attempts (each with a real jitter delay) here every single
            # cycle - just log it plainly and let the next cycle re-check
            # from scratch instead.
            if "product_id" in last_error.lower():
                break
            continue
    if remaining is None:
        suffix = f" - real DB error: {last_error}" if last_error else ""
        log.info(f"[TREE] {branch.bot_name} crossed ${milestone:,.0f} but every candidate name for a child on {next_product} collided with another branch (race){suffix} - will retry next cycle")
        return

    spawn_msg = (
        f"🌱 {branch.bot_name} crossed ${milestone:,.0f} - spawned {child_name} ({next_product}) "
        f"with ${SEED_USD:.2f} seed | {branch.bot_name} continues with ${remaining:.2f}"
    )
    log.info(f"[TREE] {spawn_msg}")
    await _log_activity(child_name, next_product, "SPAWN", spawn_msg)


async def _branch_sell_and_settle(session, bot_name, product_id, position, reason) -> bool:
    """Returns True if the branch's real state actually changed (a genuine
    fill, or the phantom-position self-heal clearing a stale tracked
    position) - safe for a caller to immediately recurse into an updated
    world. Returns False when the real sell attempt simply failed to fill
    and nothing changed - the caller must NOT recurse in that case (see
    run_branch_cycle's own comment at its call site: recursing on an
    unchanged state re-tries the identical doomed sell against the
    identical price forever, a real RecursionError risk)."""
    fill = await engine.place_market_sell(session, position.qty, product_id)
    if not fill:
        stuck_reason = engine._last_order_error.get(product_id, "")
        if stuck_reason.startswith("NOTHING_TO_SELL"):
            # Real, confirmed phantom position: this branch's tracked qty
            # no longer corresponds to any real Coinbase balance for this
            # coin. Since branches can share a coin, the real balance is
            # POOLED across every branch holding it while each branch's
            # own qty is tracked separately with no live reconciliation -
            # confirmed live via repeated real log lines ("clamping sell
            # qty X -> real held balance 0.00000000" / "TARGET HIT but
            # sell did not fill") recurring across multiple coins (POL-USD,
            # BCH-USD, DOGE-USD) every cycle, forever, because the same
            # doomed sell was being retried against a balance that was
            # never coming back. Self-heals instead: clears the stale
            # tracked position and (for a non-root branch) picks a new
            # coin the same way a real exit does, so the branch becomes
            # usable again instead of stuck in a permanent retry loop.
            # Deliberately does NOT touch allocated_usd or write a
            # CryptoCoinTradeHistory row - no real trade happened here, so
            # there is no real fill price/qty to record, and inventing one
            # would fabricate P&L that never actually occurred.
            phantom_msg = (
                f"⚠️ {bot_name}: {reason} but real held balance for {product_id} is effectively 0 "
                f"(tracked position no longer matches reality - likely real cross-branch balance drift "
                f"on a shared coin) - clearing the stale position instead of retrying forever (no real trade occurred)"
            )
            log.warning(f"[TREE] {phantom_msg}")
            await _log_activity(bot_name, product_id, "SELL", phantom_msg)
            await _clear_branch_position(bot_name)
            _coin_last_sold_at[product_id] = time.time()
            if bot_name != ROOT_BOT_NAME:
                new_product_id, new_product_atr = await find_most_volatile_unclaimed_coin(session)
                if new_product_id:
                    async with AsyncSessionLocal() as db:
                        result = await db.execute(select(CryptoTreeBranch).where(CryptoTreeBranch.bot_name == bot_name))
                        row = result.scalar_one_or_none()
                        if row:
                            old_product_id = row.product_id
                            row.product_id = new_product_id
                            await db.commit()
                            log.info(
                                f"[TREE] 🔀 {bot_name} switching {old_product_id} -> {new_product_id} "
                                f"(ATR {new_product_atr*100:.2f}%) after phantom-position self-heal"
                            )
            return True  # real state change (position cleared) - safe to recurse
        log.warning(f"[TREE] {bot_name}: {reason} but sell did not fill - will retry next cycle")
        return False  # nothing changed - the caller must not recurse on this
    filled_qty, filled_price = fill
    # Starts this coin's one-cycle cooldown (see _coin_sale_cooldown_active)
    # right away, before find_most_volatile_unclaimed_coin() runs a few
    # lines down - so this branch itself can't immediately buy straight
    # back into the coin it just sold, same as any other branch.
    _coin_last_sold_at[product_id] = time.time()
    gross_value = filled_price * filled_qty
    fee = gross_value * (ROUND_TRIP_FEE_RATE / 2)
    new_allocated = gross_value - fee
    pnl = new_allocated - (position.entry_price * position.qty)

    # Per the account owner's explicit request: a real, permanent record
    # of every round-trip trade on this coin - scoped by product_id (not
    # branch), so a coin's history keeps accumulating across different
    # branches and across being bought and sold multiple times over. This
    # is what /family-tree-status/coin-history reads to show each coin's
    # trade count, total P&L, and average P&L on the dashboard.
    async with AsyncSessionLocal() as db:
        db.add(CryptoCoinTradeHistory(
            product_id=product_id, bot_name=bot_name,
            entry_price=position.entry_price, exit_price=filled_price,
            qty=filled_qty, pnl=round(pnl, 2), exit_reason=reason,
            opened_at=position.opened_at,
        ))
        await db.commit()

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
    # automatically re-buying the same one just traded. It can't
    # immediately switch back to the coin it just sold either - the
    # one-cycle cooldown set at the top of this function (_coin_last_sold_at)
    # covers that, same protection for every branch, not a "claimed" check.
    #
    # The root (BTC) is the permanent foundation the whole tree grows out
    # of, not a branch that wanders - per the account owner, it always
    # stays on BTC-USD regardless of how it exits. A non-root branch that
    # currently holds the throne among its siblings (see
    # COIN_LOCK_KEY_PREFIX / _check_and_lock_strongest_siblings) behaves
    # the same way for as long as it holds it: stays on its current coin -
    # but unlike root, that's contestable, not forever.
    # A throne lock means "don't make this branch hop coins" - it was
    # never meant to override an explicit EXCLUDED_COINS decision. Real
    # bug caught live: crypto_tree_ldo_usd held the throne while sitting
    # on BLUR-USD (now excluded) - a manual sell just sold and instantly
    # rebought the identical excluded coin, because the lock check short-
    # circuited before ever reaching find_most_volatile_unclaimed_coin()
    # (where EXCLUDED_COINS is actually filtered). The lock still holds
    # for everything else; it just can't keep a branch parked on a coin
    # that's been explicitly excluded.
    new_product_id = new_product_atr = None
    branch_is_locked = (
        bot_name != ROOT_BOT_NAME
        and product_id not in await get_effective_excluded_coins()
        and await _is_coin_locked(bot_name)
    )
    if bot_name != ROOT_BOT_NAME and not branch_is_locked:
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
            new_tier_floor = _floor_tier_for_balance(new_allocated)
            if new_tier_floor < row.equity_floor:
                log.info(f"[TREE] 🪜 {bot_name} floor lowered ${row.equity_floor:,.2f} -> ${new_tier_floor:,.2f} to match post-sale balance ${new_allocated:.2f}")
                row.equity_floor = new_tier_floor
            await db.commit()

    # The coin switch commits separately, in its own transaction, so a
    # conflict here (see below) can never roll back the balance/floor
    # update above - that part is correct and final either way.
    if row is not None and new_product_id:
        old_product_id = row.product_id
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(CryptoTreeBranch).where(CryptoTreeBranch.bot_name == bot_name))
            fresh = result.scalar_one_or_none()
            if fresh:
                fresh.product_id = new_product_id
                await db.commit()
        row.product_id = new_product_id
        log.info(f"[TREE] 🔀 {bot_name} switching {old_product_id} -> {new_product_id} (ATR {new_product_atr*100:.2f}%) after {reason}")
    elif row is not None and bot_name == ROOT_BOT_NAME:
        log.info(f"[TREE] {bot_name}: root stays on {row.product_id} by design (the tree's permanent foundation)")
    elif row is not None and branch_is_locked:
        log.info(f"[TREE] {bot_name}: stays on {row.product_id} - currently holds the throne as the strongest of its siblings")
    elif row is not None:
        log.info(f"[TREE] {bot_name}: no eligible coin available to switch to - staying on {row.product_id}")

    sell_msg = (
        f"{'📈' if pnl >= 0 else '📉'} {bot_name} SOLD {filled_qty:.8f} {product_id} @ ${filled_price:,.2f} ({reason}) | "
        f"entry ${position.entry_price:,.2f} -> exit ${filled_price:,.2f} | "
        f"P&L: {'+' if pnl >= 0 else ''}${pnl:.2f} after est. fees | branch now ${new_allocated:.2f}"
    )
    log.info(f"[TREE] {sell_msg}")
    await _log_activity(bot_name, product_id, "SELL", sell_msg)
    if skim > 0:
        await _add_locked_usd(skim)
        lock_msg = f"🔒 {bot_name} locked away ${skim:.2f} (10% of this trade's ${pnl:.2f} profit) - permanently out of the compounding loop"
        log.info(f"[TREE] {lock_msg}")
        await _log_activity(bot_name, product_id, "LOCK", lock_msg)
    if row is not None:
        await _maybe_spawn_child(row)
    return True  # a genuine fill - real state changed, safe to recurse


async def run_branch_cycle(bot_name: str) -> bool:
    """One cycle for one branch. Returns False if this branch's row is
    gone (its thread should stop), True otherwise."""
    if await is_crypto_passive_mode():
        # Real, deliberate retirement (see is_crypto_passive_mode) - every
        # branch, root included, does nothing at all: no entries, no
        # exits, no spawns, no reinforcement. Returns True (not False) so
        # the thread keeps existing rather than exiting - passive mode is
        # reversible in principle even though nothing in this codebase
        # currently flips it back on, and a thread that already exited
        # wouldn't restart on its own if it ever were.
        return True

    branch = await load_branch(bot_name)
    if branch is None:
        return False

    # Catch-up spawn check, every cycle - not just right after a sell.
    # _maybe_spawn_child() is also called directly inside
    # _branch_sell_and_settle() at the moment a sale crosses the tier, but
    # that's the ONLY other place it ran: a branch that crossed its tier
    # and then couldn't spawn right then (e.g. every eligible coin was
    # claimed at that exact moment) had no other chance until its NEXT
    # sell - which could be a long wait while it's holding a healthy
    # position (the dashboard's "Next spawn" bar would sit at 100% the
    # whole time, misleadingly implying it was about to happen). Also
    # covers a branch adopted from the old bot already above its tier at
    # adoption time (see orphan adoption above), which never got a spawn
    # check at all before this. Cheap when not yet eligible - the first
    # line inside is a synchronous comparison against the already-loaded
    # branch, no DB query happens unless it's actually crossed.
    await _maybe_spawn_child(branch)
    # _maybe_spawn_child() deducts the $50 seed from a FRESH row it loads
    # internally, not from this already-in-memory `branch` object - reload
    # so every use of branch.allocated_usd below (most importantly the
    # buy-sizing "spend = min(branch.allocated_usd, ...)" further down)
    # reflects the real post-spawn balance. Without this, a branch that
    # spawns this cycle would double-count the seed: transferred for real
    # to the child, then spent again here off the stale pre-spawn figure.
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

        # Real bug found live (root's own screenshot: "equity $250.18 |
        # floor $700.00" while its real allocated_usd was $869.10) - this
        # used to be `equity = position.qty * price`, the CURRENT
        # POSITION's raw market value alone, discarding any real idle
        # cash sitting in `branch.allocated_usd` beyond what's currently
        # deployed (common: after reinforcement adds capital without a
        # matching full-size buy, after a partial-fill, or simply a
        # branch that never invests 100% of its balance at once). A
        # branch can be genuinely wealthy and healthy while holding a
        # comparatively small position, and this wrongly reported it as
        # floor-breached purely because the position itself was small -
        # nothing to do with real financial health. The correct real
        # equity while holding is the branch's own tracked total PLUS the
        # position's real unrealized P&L (mark-to-market), not the
        # position's notional value in isolation.
        equity = branch.allocated_usd
        if position is not None and price is not None:
            unrealized_pnl = position.qty * (price - position.entry_price)
            equity = branch.allocated_usd + unrealized_pnl

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

        if breached and position is not None:
            if price is None:
                log.warning(f"[TREE] {bot_name}: floor breach but no price available to force-sell - retry next cycle")
                return True
            # The branch-level floor is a compounding checkpoint unrelated
            # to THIS position's own entry price - it has no idea whether
            # the position is still healthy. Forcing an exit here
            # unconditionally could cut short a position that's still above
            # its own stop (possibly already breakeven-ratcheted, or
            # heading to target), realizing a loss the position's own
            # protections were specifically built to prevent. Only force
            # the floor-breach exit when the position's OWN stop has
            # ALSO already failed (price <= stop_price) - at that point
            # it's exiting anyway, so nothing is lost by labeling it a
            # floor breach and applying the rebuy cooldown. Otherwise,
            # leave the position alone and let it run under its own
            # target/stop/breakeven/giveback protection below - the floor
            # stays breached (still blocks new entries elsewhere) but this
            # held position isn't punished for an unrelated milestone.
            if price <= position.stop_price:
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
            log.info(
                f"[TREE] {bot_name}: floor breached (${equity:.2f} < ${branch.equity_floor:,.2f}) but this position "
                f"is still above its own stop ${position.stop_price:,.2f} - letting it run under its own "
                f"protection instead of forcing an early exit"
            )
            # Fall through (not returning here) into the normal
            # target/stop/breakeven/giveback logic further below, exactly
            # as if the branch hadn't breached at all - new entries
            # elsewhere are still blocked by the breach, but this position
            # gets to keep running on its own merits.

        if breached and position is None:
            # A flat branch's own allocated_usd should never end up
            # below its ratcheted floor on its own - the real path that
            # produces this: _maybe_spawn_child() pulls the $50 seed for
            # a new child right after the floor was ratcheted up to
            # match the pre-spawn balance, leaving the parent flat and
            # permanently stuck - it can never trade its way back over
            # the floor, because trading is the only thing that could
            # raise its balance, and that's exactly what "breached"
            # blocks. Without this, it would compare its real balance
            # against that now-too-high floor forever. Self-heal by
            # lowering the floor to match this branch's own real,
            # current tier - the same reset _branch_sell_and_settle
            # already applies right after a sale - instead of leaving
            # real money frozen waiting for a balance it can't earn.
            new_tier_floor = _floor_tier_for_balance(equity)
            if new_tier_floor < branch.equity_floor:
                async with AsyncSessionLocal() as db:
                    result = await db.execute(select(CryptoTreeBranch).where(CryptoTreeBranch.bot_name == bot_name))
                    row = result.scalar_one_or_none()
                    if row:
                        row.equity_floor = new_tier_floor
                        await db.commit()
                log.info(
                    f"[TREE] 🪜 {bot_name} floor lowered ${branch.equity_floor:,.2f} -> ${new_tier_floor:,.2f} "
                    f"to match its own real balance ${equity:.2f} - entries resume next cycle"
                )
            else:
                log.info(f"[TREE] 🛑 {bot_name}: ${equity:.2f} below floor ${branch.equity_floor:,.2f} - entries paused until it recovers")
            return True

        if position is None:
            # Real, immediate kill switch for NEW entries only - matches the
            # existing STOP_TRADING convention prop_bot.py/crypto_coinbase_bot.py
            # already use, which this bot never had. Deliberately checked here,
            # not at the top of run_branch_cycle: every exit path above this
            # (STOP HIT, TARGET, breakeven, floor-breach) must keep running
            # unconditionally so an already-open real position never loses its
            # protection - only NEW buys pause.
            if os.getenv("STOP_TRADING", "false").lower() == "true":
                log.warning(f"[TREE] {bot_name}: STOP_TRADING=true - new entries paused (existing positions still protected)")
                return True
            if await _floor_breach_cooldown_active(bot_name):
                log.info(f"[TREE] {bot_name}: cooling down after a recent floor breach - entries paused a bit longer")
                return True
            expectancy_state = await get_rolling_expectancy()
            if expectancy_state["negative"]:
                log.warning(
                    f"[TREE] {bot_name}: rolling expectancy over the tree's last {expectancy_state['num_trades']} "
                    f"real trades is negative (${expectancy_state['expectancy']:.2f}/trade avg) - new entries "
                    f"paused tree-wide until it recovers (existing positions still protected)"
                )
                return True
            if real_balance is None:
                log.warning(f"[TREE] {bot_name}: real balance unavailable ({real_balance_err}) - skipping this cycle")
                return True

            # Real bug found live: a flat non-root branch's own stored
            # product_id can end up on a coin that's since become excluded
            # (its own coin-switch search at exit time found no eligible
            # replacement that exact moment, so it stayed put) - and
            # nothing before this point ever re-checked that before the
            # ordinary flat-branch buy below just blindly re-entered it.
            # Confirmed live: crypto_tree_eth_usd_4 kept re-buying
            # POL-USD - manually excluded for its real -$338+ loss and
            # 14% win rate - every time it went flat. Exclusion is
            # already enforced at every NEW-coin-pick decision point
            # (spawn, reinforcement, coin-switch-after-exit); this closes
            # the one remaining gap where a branch's OWN currently-stored
            # coin was never re-checked. Root is exempt - it never
            # switches off BTC-USD by design.
            if bot_name != ROOT_BOT_NAME:
                excluded_now = await get_effective_excluded_coins()
                if branch.product_id in excluded_now:
                    new_product_id, new_atr = await find_most_volatile_unclaimed_coin(session)
                    if new_product_id:
                        async with AsyncSessionLocal() as db:
                            result = await db.execute(select(CryptoTreeBranch).where(CryptoTreeBranch.bot_name == bot_name))
                            fresh = result.scalar_one_or_none()
                            if fresh:
                                fresh.product_id = new_product_id
                                await db.commit()
                        log.warning(
                            f"[TREE] {bot_name}: was about to re-enter {branch.product_id}, which is now excluded - "
                            f"switching to {new_product_id} (ATR {new_atr*100:.2f}%) instead"
                        )
                        branch.product_id = new_product_id
                        price, atr_pct = await engine.get_price_and_volatility(session, branch.product_id)
                    else:
                        log.info(f"[TREE] {bot_name}: currently assigned to excluded coin {branch.product_id} with no eligible replacement available yet - waiting")
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
                # A real, confirmed-live rejection (PERMISSION_DENIED on
                # RNDR-USD, "Invalid product_id" on MATIC-USD/JUP-USD)
                # means this exact coin can NEVER fill for this account,
                # no matter how many more cycles retry the identical
                # order - previously this branch would sit flat forever,
                # retrying the same doomed buy every 30s and showing the
                # same red rejection on the dashboard permanently. Switch
                # to a different coin right away instead, same real
                # search every other coin-switch already uses.
                stuck_reason = engine._last_order_error.get(branch.product_id, "")
                if engine._is_permanent_order_rejection(stuck_reason):
                    new_product_id, new_atr = await find_most_volatile_unclaimed_coin(session)
                    if new_product_id:
                        async with AsyncSessionLocal() as db:
                            result = await db.execute(select(CryptoTreeBranch).where(CryptoTreeBranch.bot_name == bot_name))
                            fresh = result.scalar_one_or_none()
                            if fresh:
                                fresh.product_id = new_product_id
                                await db.commit()
                        log.warning(
                            f"[TREE] {bot_name}: {branch.product_id} can never fill ({stuck_reason}) - "
                            f"switching to {new_product_id} (ATR {new_atr*100:.2f}%) instead of retrying forever"
                        )
                    else:
                        log.warning(f"[TREE] {bot_name}: {branch.product_id} can never fill ({stuck_reason}) but no other coin is currently available to switch to")
                else:
                    log.warning(f"[TREE] {bot_name}: buy did not fill - will retry next cycle")
                return True
            filled_qty, filled_price = fill
            target_price = filled_price * (1 + target_pct)
            stop_price = filled_price * (1 - STOP_LOSS_PCT)
            await _save_branch_position(bot_name, branch.product_id, filled_price, filled_qty, target_price, stop_price)
            buy_msg = (
                f"🟢 {bot_name} BOUGHT {filled_qty:.8f} {branch.product_id} @ ${filled_price:,.2f} (${spend:.2f} deployed) | "
                f"ATR {atr_pct*100:.2f}% -> target +{target_pct*100:.2f}% (${target_price:,.2f}, min ${engine.pick_min_profit_usd(atr_pct):.2f} net) | "
                f"stop -{STOP_LOSS_PCT*100:.2f}% (${stop_price:,.2f}) | branch total ${branch.allocated_usd:.2f} | floor ${branch.equity_floor:,.2f}"
            )
            log.info(f"[TREE] {buy_msg}")
            await _log_activity(bot_name, branch.product_id, "BUY", buy_msg)
            return True

        if price is None:
            log.warning(f"[TREE] {bot_name}: could not fetch current price - holding, will re-check next cycle")
            return True

        unrealized_pct = (price / position.entry_price - 1) * 100

        # Peak-profit giveback tracking (see MAX_PROFIT_GIVEBACK_USD): once
        # this position has ever shown a real profit, it can never give
        # back more than that many dollars from its best point without
        # being force-sold - independent of the fixed target/stop prices.
        unrealized_usd = position.qty * (price - position.entry_price)
        stored_peak = position.peak_pct or 0.0
        if unrealized_usd > stored_peak:
            await _update_branch_position_peak(bot_name, unrealized_usd)
            position.peak_pct = unrealized_usd
            stored_peak = unrealized_usd
        peak_giveback = stored_peak - unrealized_usd

        # Real bug found live: this exit is labeled "locking in gains,"
        # but the dollar-giveback check above is purely GROSS (raw price
        # move x qty) - it never checked whether what's actually left,
        # AFTER the real round-trip Coinbase fee, is still genuinely a
        # profit. Confirmed live: a position whose peak was small enough
        # that giving back $3.75 of it left less than the real fee cost
        # force-sold anyway, and the real settled P&L came back at -$6.65
        # - a real loss, from an exit that called itself profit-locking.
        # Fixed the same way the real TARGET exit's own min-profit floor
        # already works: mirror _branch_sell_and_settle's own real fee
        # formula here (half the round-trip rate, matching how the actual
        # settlement computes real P&L) and only let the giveback path
        # fire when the real, fee-adjusted proceeds would still be a
        # genuine profit. If not, this exit is skipped entirely and the
        # position keeps running under its own real TARGET/STOP/breakeven
        # protection instead - never force-sold into a real loss dressed
        # up as a win.
        projected_net_pnl = (price * position.qty * (1 - ROUND_TRIP_FEE_RATE / 2)) - (position.entry_price * position.qty)
        giveback_would_realize_loss = stored_peak > 0 and peak_giveback >= MAX_PROFIT_GIVEBACK_USD and projected_net_pnl <= 0
        giveback_exceeded = stored_peak > 0 and peak_giveback >= MAX_PROFIT_GIVEBACK_USD and projected_net_pnl > 0
        # Real, fast profit-take: any genuine real profit (after real
        # round-trip fees) gets taken now rather than waiting for the
        # formal, bigger TARGET price - see QUICK_PROFIT_MIN_NET_USD
        # above. Never fires on a net-negative or break-even position -
        # that case is left completely alone under existing protection.
        quick_profit_available = projected_net_pnl > QUICK_PROFIT_MIN_NET_USD
        if giveback_would_realize_loss:
            log.info(
                f"[TREE] {bot_name}: peak-profit giveback cap reached (${peak_giveback:.2f} given back from "
                f"${stored_peak:.2f} peak) but the real fee-adjusted proceeds right now would be a loss "
                f"(${projected_net_pnl:.2f} net) - holding under its own target/stop protection instead of "
                f"force-selling into a loss labeled as a win"
            )

        if price >= position.target_price or price <= position.stop_price or giveback_exceeded or quick_profit_available:
            if price >= position.target_price:
                exit_reason = "TARGET HIT"
            elif price <= position.stop_price:
                exit_reason = "STOP HIT"
            elif giveback_exceeded:
                exit_reason = "PEAK PROFIT GIVEBACK - locking in gains"
                log.warning(
                    f"[TREE] 💰 {bot_name} gave back ${peak_giveback:.2f} from its ${stored_peak:.2f} peak "
                    f"profit - force-selling to lock in what's left"
                )
            else:
                exit_reason = "QUICK PROFIT - real net gain taken fast"
                log.info(
                    f"[TREE] ⚡ {bot_name} showing a real ${projected_net_pnl:.2f} net profit (after fees) - "
                    f"taking it now instead of waiting for the bigger ${position.target_price:,.2f} target"
                )
            sold = await _branch_sell_and_settle(session, bot_name, branch.product_id, position, exit_reason)
            if sold:
                # _branch_sell_and_settle already picked the branch's next
                # coin - re-run immediately (same reasoning as the
                # floor-breach path above) so the rebuy happens in this
                # same pass instead of waiting for the next scheduled cycle.
                return await run_branch_cycle(bot_name)
            # Real, previously-latent bug: recursing here unconditionally
            # (the old behavior) re-evaluates the identical exit condition
            # against the identical price and retries the identical doomed
            # sell forever whenever a real sell attempt simply fails to
            # fill - a genuine RecursionError risk, confirmed while testing
            # the QUICK PROFIT exit above (it fires far more often than
            # TARGET/STOP/GIVEBACK ever did, and was the first thing to
            # actually exercise this gap at scale). Nothing about this
            # position's state changed, so don't recurse - _branch_sell_
            # and_settle already logged "will retry next cycle"; let that
            # actually happen instead of retrying inside this same call.
            return True
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
                f"stop ${position.stop_price:,.2f} | peak profit ${stored_peak:.2f} | equity ${equity:.2f} | floor ${branch.equity_floor:,.2f}"
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
        # Per the account owner's own real observation (after a night of
        # spawn-collision fixes): every branch's cycle timer effectively
        # starts from the same moment (the coordinator's startup scan
        # starts every existing branch's thread back-to-back, and a
        # newly-spawned branch's thread starts immediately too), and a
        # bare fixed-interval sleep never lets that initial clustering
        # drift apart on its own - branches that started in lockstep stay
        # in lockstep, cycle after cycle, which is exactly what made
        # several branches keep re-hitting the same spawn target at the
        # same instant tonight. A small random jitter on the sleep itself
        # (kept modest, +/-10%, so real trading responsiveness is
        # unaffected) means every branch's cycle boundary keeps wandering
        # relative to every other branch's - within a few real cycles, a
        # group that started together is spread across the whole window
        # instead of clustered at one instant. Deliberately NOT delaying
        # the FIRST cycle above (a freshly spawned branch still checks for
        # its buy immediately, no initial wait) - only the recurring sleep
        # between cycles gets the jitter.
        time.sleep(CYCLE_SECONDS + random.uniform(-CYCLE_SECONDS * 0.1, CYCLE_SECONDS * 0.1))
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
    loop.run_until_complete(_drop_product_id_unique_index())
    loop.run_until_complete(_dedupe_locked_profit_state())
    loop.run_until_complete(_ensure_trading_bot_state_unique_index())
    loop.run_until_complete(_dedupe_family_tree_positions())
    loop.run_until_complete(_migrate_matic_to_pol())
    loop.run_until_complete(_lower_existing_unlock_tiers())
    loop.run_until_complete(_force_root_spawn_ready())

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

        global _last_auto_backtest_at
        if now - _last_auto_backtest_at >= AUTO_BACKTEST_INTERVAL_SECONDS:
            _last_auto_backtest_at = now
            try:
                await _run_scheduled_backtest_and_update_exclusions()
            except Exception as e:
                log.warning(f"[TREE] scheduled backtest failed: {e}")

        # Cheap (one DB read, no external calls) and idempotent once a
        # branch is locked - safe to check every scan rather than on a
        # separate throttle timer.
        try:
            await _check_and_lock_strongest_siblings()
        except Exception as e:
            log.warning(f"[TREE] strongest-sibling lock check failed: {e}")

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

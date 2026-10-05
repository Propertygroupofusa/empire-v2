"""Where a harvested dollar goes, instead of sitting as cash.

THE OWNER'S INSTRUCTION, 2026-10-05: "every branch once it fills, I want
the money to go into the weakest branch and help it build up." And, in the
same breath: "Do not make any more branches, whatever we have. Keep it the
way it is."

profit_harvest takes realised profit out of a FLAT branch and leaves it as
unallocated cash. That was deliberate - "taking profit off the table is the
point" - and with the wallet under GRID_CASH_RESERVE_USD it means the money
stops there and nothing can spend it. This module names the branch that
dollar should go to instead.

WHY NOT "THE WEAKEST BRANCH", LITERALLY.

The family tree already built that rule, at this owner's request, and
revised it three times (see crypto_family_tree_bot, "always help whichever
one is weakest, period"). It worked there because a TREE branch builds up
toward a spawn tier - more dollars IS progress.

A GRID branch does not work that way. It earns from a RUNG AT A PRICE THAT
CAN SELL, not from holding money. Money handed to a branch whose rungs are
all full, or whose coin is nowhere near its buy line, becomes idle claim -
which is the condition the fleet is already in: $7,638.93 allocated against
$75.80 actually free.

So the money goes to the branch where it can soonest BECOME a rung:

  1. it is not the branch the money came from
  2. it has an empty rung (open_slices < num_levels)
  3. the deposit does not push it past the concentration ceiling
  4. nearest its buy line first - a branch already past its trigger is
     first in line, because it buys the moment cash exists
  5. ties broken toward the SMALLER branch, which is the owner's
     "help the weakest" intent, applied where it changes nothing else

RULE 1 IS NOT COSMETIC. crypto_family_tree_bot's own comment records what
happens without it: "without that exclusion, 'always reinforce the weakest'
was a closed loop feeding itself."

THIS MODULE PLACES NO ORDER, SELLS NO COIN, AND CREATES NO BRANCH. It
returns a name. The caller moves the claim, and only ever the exact amount
it just withdrew.

OFF BY DEFAULT. GRID_HARVEST_REDIRECT must be set to "true" for any of this
to run. Unset, the harvest behaves exactly as it does today and this file
is dead weight - which is the right default for a change that moves money
without anyone pressing a button.
"""
import logging
import os

log = logging.getLogger(__name__)

ENV_FLAG = "GRID_HARVEST_REDIRECT"

# The same ceiling the buy gate enforces. Measured on ALLOCATED CLAIM here,
# not on the coin book the real concentration_gate uses, because this moves
# claim and not coin. The two books differ, so this is deliberately the
# more conservative of the pair for this purpose: it can refuse a transfer
# the coin book would allow, and it can never permit one the coin book
# would refuse, because no coin is bought here at all. The real gate still
# runs, unchanged, when the branch actually spends.
MAX_SHARE_OF_FLEET = 0.20

# HOW MANY CLOSES BEFORE A BRANCH'S NET% PER CLOSE IS EVIDENCE.
#
# Measured on this fleet's own ledger, 2026-10-05: a branch's NET % PER
# CLOSE persists between windows (Spearman +0.643, t=+3.03 on 13 df) while
# its NUMBER OF CLOSES does not (Spearman +0.007 - noise). Walk-forward on
# the live ledger, three out-of-sample steps, ranking on the prior window's
# net% per close: the top half went on to earn +2.33%, +2.17%, +2.71% per
# close against the bottom half's +0.86%, +0.92%, +0.71% - a mean spread of
# +1.57 percentage points per close, positive in 3 of 3.
#
# THAT RESULT IS WHY AN EARLIER REALLOCATION TEST FAILED. It ranked
# branches by TOTAL DOLLARS EARNED, which is net% x closes - and closes are
# the half that does not persist. Ranking on dollars is therefore ranking
# half on noise, and it lost out of sample. Only the net% half is carried
# here.
#
# Two closes can produce any percentage at all, so a branch under this
# floor is UNKNOWN, never zero and never bottom-tier. Unknown keeps the
# ordering this module already had.
MIN_CLOSES_FOR_PRODUCTIVITY = 3

# THE ABSOLUTE BAR, BESIDE THE RELATIVE ONE.
#
# The median split above is RELATIVE, and that is its one dangerous
# property: in a dead market the "top half" is only the least bad half, and
# a ranking that trusts it would keep feeding rungs that do not clear their
# own costs. So a branch must beat the median AND beat what a round trip
# actually costs.
#
# 1.2003% is this account's measured all-in bar from /cost-truth: 0.70%
# maker fees plus 0.5003% adverse selection measured over 3,262 falling-
# market samples. It is priced off the worst measured regime on purpose.
# Overridable because a fee tier change moves it, and a bar that cannot
# follow the fees it is made of is a stale number pretending to be a rule.
REGIME_COST_BAR_PCT = float(os.getenv("GRID_REGIME_COST_BAR_PCT", "1.2003"))


def enabled() -> bool:
    return (os.getenv(ENV_FLAG, "false") or "").strip().lower() == "true"


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def regime_verdict(branches, cost_bar_pct=None):
    """Whether ANY branch is currently clearing the cost of trading.

    This is the market condition the ranking sits on top of. The median
    always produces a top half, even when every branch in it is losing
    money - so without this the module would confidently route cash into
    the best of a bad set and call that an improvement.

    Returns a dict. `tradeable` False means the honest answer is to leave
    the money as cash, which is exactly what this module does when it
    cannot find a target, so a dead regime needs no special path.
    """
    bar = REGIME_COST_BAR_PCT if cost_bar_pct is None else cost_bar_pct
    measured = [(b.get("bot_name"), _net_pct_per_close(b))
                for b in (branches or [])]
    measured = [(n, v) for n, v in measured if v is not None]
    if not measured:
        # No branch has enough closes. UNKNOWN, and unknown must not be
        # read as a dead market - a quiet fleet and a losing one look the
        # same here and are not the same thing.
        return {"tradeable": True, "readable": False, "cost_bar_pct": bar,
                "clearing_count": None, "measured_branches": 0,
                "detail": (f"no branch has {MIN_CLOSES_FOR_PRODUCTIVITY}+ closes "
                           f"to measure, so the regime is UNKNOWN - the ranking "
                           f"falls back to distance-to-buy-line and nothing is "
                           f"refused on an unread number")}
    clearing = [n for n, v in measured if v > bar]
    best = max(v for _, v in measured)
    return {
        "tradeable": bool(clearing),
        "readable": True,
        "cost_bar_pct": round(bar, 4),
        "clearing_count": len(clearing),
        "measured_branches": len(measured),
        "best_net_pct_per_close": round(best, 4),
        "clearing": clearing,
        "detail": (f"{len(clearing)} of {len(measured)} measured branch(es) net "
                   f"more than the {bar:.4f}% all-in cost of a round trip"
                   if clearing else
                   f"NO branch is clearing the {bar:.4f}% cost bar - the best "
                   f"nets {best:.4f}% per close. Sending cash into a rung here "
                   f"buys a trade that loses money on average, so it stays cash."),
    }


def productivity_tier(net_pct_per_close, median_net_pct, cost_bar_pct=None):
    """0 above the fleet median, 2 below it, 1 when it cannot be judged.

    THE SPLIT IS A MEDIAN AND NOT A SCORE ON PURPOSE. The walk-forward that
    justifies this ranking compared the TOP HALF against the BOTTOM HALF; it
    never measured what a 0.2-point difference inside a half is worth. A
    continuous score would be claiming precision the test did not produce,
    so the only thing read here is which side of the median a branch is on.

    Tier 1 is for a branch with no trustworthy figure, and it sits BETWEEN
    the two measured tiers rather than at the bottom. A branch that has not
    traded enough is unproven, not bad, and sending it nothing would starve
    exactly the new branch that needs a rung to prove itself.
    """
    if net_pct_per_close is None or median_net_pct is None:
        return 1
    if cost_bar_pct is None:
        cost_bar_pct = REGIME_COST_BAR_PCT
    # BOTH bars, not either. Above the median alone is "better than its
    # peers", which in a bad week is still a losing rung.
    if net_pct_per_close > median_net_pct and net_pct_per_close > cost_bar_pct:
        return 0
    return 2


def _net_pct_per_close(branch):
    """A branch's measured net % per close, or None when it is not evidence.

    None and 0.0 are different claims and are kept different: a branch with
    two closes has no figure, while a branch with ten closes that netted
    nothing has a real one of zero.
    """
    n = _num(branch.get("closes"))
    if n is None or n < MIN_CLOSES_FOR_PRODUCTIVITY:
        return None
    pct = _num(branch.get("net_pct_per_close"))
    if pct is not None:
        return pct
    pnl = _num(branch.get("realized_usd"))
    notional = _num(branch.get("closed_notional_usd"))
    if pnl is None or notional is None or notional <= 0:
        return None
    return pnl / notional * 100.0


def fleet_median_net_pct(branches):
    """The median of every branch that HAS a figure. None when none do."""
    vals = sorted(v for v in (_net_pct_per_close(b) for b in (branches or []))
                  if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    if len(vals) % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0


def candidates(branches, *, exclude_bot_name=None, amount_usd=0.0,
               fleet_allocated_usd=None, max_share=MAX_SHARE_OF_FLEET):
    """Every branch that could take this money, best first, with reasons.

    Returns (eligible, rejected). Both are lists of dicts; nothing is
    written and no branch is modified.
    """
    amount = _num(amount_usd) or 0.0
    rows = list(branches or [])
    fleet = _num(fleet_allocated_usd)
    if fleet is None:
        fleet = sum((_num(b.get("allocated_usd")) or 0.0) for b in rows)

    # Computed across EVERY branch handed in, before any is filtered out.
    # A median taken after the eligibility gates would move with which
    # branches happen to be full today, so the same branch could change
    # tier without its own record changing at all.
    median_net = fleet_median_net_pct(rows)
    regime = regime_verdict(rows)

    eligible, rejected = [], []
    for b in rows:
        pid = b.get("product_id")
        name = b.get("bot_name")
        alloc = _num(b.get("allocated_usd"))
        levels = _num(b.get("num_levels"))
        px = _num(b.get("current_price"))
        ref = _num(b.get("reference_price"))
        gp = _num(b.get("grid_pct"))
        open_slices = len(b.get("slices") or [])

        def reject(why):
            rejected.append({"bot_name": name, "product_id": pid, "why": why})

        if exclude_bot_name and name == exclude_bot_name:
            reject("this is the branch the money came from - sending it "
                   "back would be a closed loop")
            continue
        if alloc is None or levels is None or levels <= 0:
            reject("allocation or level count unreadable - a gap is not a zero")
            continue
        if open_slices >= levels:
            reject(f"no empty rung ({open_slices} slice(s) against "
                   f"{levels:.0f} level(s)) - money here cannot become a rung")
            continue
        # The concentration ceiling, measured on claim. A fleet total of
        # zero cannot be divided, and is not an excuse to skip the check.
        if fleet > 0:
            share_after = (alloc + amount) / (fleet + 0.0)
            if share_after > max_share:
                reject(f"would take it to {share_after * 100:.1f}% of the "
                       f"fleet, past the {max_share * 100:.0f}% ceiling")
                continue
        else:
            reject("fleet total is zero or unreadable, so no share can be checked")
            continue
        if px is None or ref is None or gp is None or px <= 0:
            reject("price, reference or step unreadable - cannot say how far "
                   "it is from buying")
            continue

        # How far price must FALL before this branch buys. Negative means
        # it is ALREADY past its buy line and is waiting on cash alone.
        fall_pct = (px - ref * (1.0 - gp)) / px * 100.0
        _prod = _net_pct_per_close(b)
        _tier = productivity_tier(_prod, median_net, regime.get("cost_bar_pct"))
        eligible.append({
            "bot_name": name, "product_id": pid,
            "allocated_usd": round(alloc, 2),
            "open_slices": open_slices, "num_levels": int(levels),
            "empty_rungs": int(levels) - open_slices,
            "fall_to_buy_pct": round(fall_pct, 4),
            "already_past_buy_line": fall_pct <= 0,
            "share_after_pct": round((alloc + amount) / fleet * 100.0, 2),
            "net_pct_per_close": (None if _prod is None else round(_prod, 4)),
            "closes_measured": _num(b.get("closes")),
            "productivity_tier": _tier,
            "productivity_is": ("above the fleet median" if _tier == 0 else
                                "below the fleet median" if _tier == 2 else
                                f"unproven - under {MIN_CLOSES_FOR_PRODUCTIVITY} "
                                f"closes, which is not evidence either way"),
        })

    # PRODUCTIVITY TIER FIRST, then the ordering this module always had.
    #
    # Within a tier nothing changes: nearest its buy line first, so the
    # money becomes a rung soonest, then the smaller branch, which is the
    # owner's "help the weakest" applied only where it breaks a tie. The
    # tier is what is new, and it only ever reorders ACROSS the median -
    # a difference the walk-forward actually measured.
    #
    # Deploy-speed is deliberately kept INSIDE the tier rather than traded
    # off against productivity. A branch 6% from its buy line will not buy
    # for days whatever its record, and a continuous score mixing the two
    # would invent an exchange rate between them that nothing here measured.
    eligible.sort(key=lambda r: (r["productivity_tier"],
                                 r["fall_to_buy_pct"],
                                 r["allocated_usd"]))
    return eligible, rejected, regime


def pick_target(branches, *, exclude_bot_name=None, amount_usd=0.0,
                fleet_allocated_usd=None, max_share=MAX_SHARE_OF_FLEET):
    """The one branch to send it to, or (None, why not).

    Returning None is a normal outcome, not a failure: the caller leaves
    the money as cash, which is exactly what it does today.
    """
    eligible, rejected, regime = candidates(
        branches, exclude_bot_name=exclude_bot_name, amount_usd=amount_usd,
        fleet_allocated_usd=fleet_allocated_usd, max_share=max_share)
    # THE REGIME IS CHECKED BEFORE THE RANKING IS READ. An eligible list is
    # not evidence that trading pays - every gate it passed was about
    # whether a branch CAN take money, never about whether it should.
    if not regime.get("tradeable", True):
        return None, {
            "reason": regime["detail"],
            "regime": regime,
            "would_have_picked": eligible[0]["bot_name"] if eligible else None,
            "rejected": rejected,
        }
    if not eligible:
        return None, {
            "reason": "no branch could take it - it stays as cash",
            "regime": regime,
            "rejected": rejected,
        }
    best = eligible[0]
    where = ("already past its buy line, so it buys as soon as cash exists"
             if best["already_past_buy_line"] else
             f"nearest its buy line, {best['fall_to_buy_pct']:.2f}% away")
    _tier_said = best.get("productivity_is") or "unranked"
    return best["bot_name"], {
        "reason": f"{_tier_said}; {where}",
        "target": best,
        "regime": regime,
        "runners_up": eligible[1:4],
        "rejected": rejected,
    }

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


def enabled() -> bool:
    return (os.getenv(ENV_FLAG, "false") or "").strip().lower() == "true"


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


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
        eligible.append({
            "bot_name": name, "product_id": pid,
            "allocated_usd": round(alloc, 2),
            "open_slices": open_slices, "num_levels": int(levels),
            "empty_rungs": int(levels) - open_slices,
            "fall_to_buy_pct": round(fall_pct, 4),
            "already_past_buy_line": fall_pct <= 0,
            "share_after_pct": round((alloc + amount) / fleet * 100.0, 2),
        })

    # Nearest its buy line first; then the smaller branch, which is the
    # owner's "help the weakest" applied only where it breaks a tie.
    eligible.sort(key=lambda r: (r["fall_to_buy_pct"], r["allocated_usd"]))
    return eligible, rejected


def pick_target(branches, *, exclude_bot_name=None, amount_usd=0.0,
                fleet_allocated_usd=None, max_share=MAX_SHARE_OF_FLEET):
    """The one branch to send it to, or (None, why not).

    Returning None is a normal outcome, not a failure: the caller leaves
    the money as cash, which is exactly what it does today.
    """
    eligible, rejected = candidates(
        branches, exclude_bot_name=exclude_bot_name, amount_usd=amount_usd,
        fleet_allocated_usd=fleet_allocated_usd, max_share=max_share)
    if not eligible:
        return None, {
            "reason": "no branch could take it - it stays as cash",
            "rejected": rejected,
        }
    best = eligible[0]
    where = ("already past its buy line, so it buys as soon as cash exists"
             if best["already_past_buy_line"] else
             f"nearest its buy line, {best['fall_to_buy_pct']:.2f}% away")
    return best["bot_name"], {
        "reason": where,
        "target": best,
        "runners_up": eligible[1:4],
        "rejected": rejected,
    }

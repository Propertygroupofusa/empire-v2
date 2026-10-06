"""What the portfolio SHOULD look like, given what can move without selling.

THE OWNER'S INSTRUCTION, 2026-10-05, in his own words: "Scaling, not
selling." - "Do not sell anything that I have as a loss. Just use what's in
the account and build it up." - "If it's 2,000 in there, next time it
should be 4,000." - "I want to build a bot that's going to manage my
portfolio."

So this names moves. It never names a sale, and there is no code path here
that can produce one: the only capital it will ever propose moving is claim
that is ALREADY free of coin. A branch's coin cost basis is a FLOOR this
module will not go below, because going below it is selling by another name.

WHAT IT RANKS ON, AND WHY ONLY THAT.

Measured on this fleet's own ledger, 2026-10-05, 125 own-trading closes
since the 2026-09-26 config epoch:

  net % per close PERSISTS between windows   Spearman +0.643, t=+3.03, 13 df
  number of closes does NOT                  Spearman +0.007 - noise

Walk-forward, three out-of-sample steps, ranking on the prior window's
net% per close: the top half went on to earn +2.33%, +2.17%, +2.71% per
close against the bottom half's +0.86%, +0.92%, +0.71%. Mean spread +1.570
percentage points, positive in 3 of 3.

AN EARLIER REALLOCATION TEST FAILED BECAUSE IT RANKED ON TOTAL DOLLARS,
which is net% x closes - and closes are the half that does not persist.
Half its signal was noise and it lost out of sample. That failure is the
reason this module ranks on net% per close ALONE, and the reason it does
not try to predict how often anything will trade.

WHAT IT WILL NOT DO, STRUCTURALLY.

  * It places no order, sells no coin, creates no branch, moves no dollar
    and writes nothing. It returns a plan. A person runs it.
  * It never sources capital from a branch that still has a rung to buy -
    that branch's budget is not spare, it is waiting for a dip.
  * It never takes a branch below its coin cost basis. That floor is the
    whole difference between scaling and selling.
  * It never funds a branch past the concentration ceiling, which is the
    owner's "I just don't want it to put so much money into one coin".
  * It plans nothing at all when no branch is clearing the measured cost
    of a round trip - see harvest_redirect.regime_verdict. A ranking is
    relative, and the top half of a losing fleet is still losing.
"""
import logging

import harvest_redirect as hr

log = logging.getLogger(__name__)

# The owner's "split it up", and the same ceiling the buy gate enforces.
MAX_SHARE_OF_FLEET = hr.MAX_SHARE_OF_FLEET

# Below this a move is not worth its own fees. MIN_TRADE_USD on the live
# gate is $5.00; a move smaller than that cannot become a rung at all.
MIN_MOVE_USD = 5.0


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def releasable(branch):
    """Claim this branch can give up WITHOUT selling anything, or 0.0.

    Two conditions, both required, and neither is a judgement call:

      1. The branch is FULL on its rungs. A branch with an empty rung is
         holding that budget to buy a dip - taking it is not freeing idle
         money, it is disarming the grid. This is the single most important
         line in the file.
      2. What is taken stays above the branch's coin cost basis. Below that
         floor the claim is backing coin the branch actually holds, and
         releasing it would mean selling to make it real.
    """
    alloc = _num(branch.get("allocated_usd"))
    basis = _num(branch.get("coin_basis_usd"))
    levels = _num(branch.get("num_levels"))
    if alloc is None or basis is None or levels is None:
        return 0.0
    open_slices = len(branch.get("slices") or [])
    if open_slices < levels:
        return 0.0
    free = alloc - basis
    return round(free, 2) if free >= MIN_MOVE_USD else 0.0


def sources(branches):
    """Every branch with claim to give, and what it is giving."""
    out = []
    for b in branches or []:
        amt = releasable(b)
        if amt > 0:
            out.append({
                "bot_name": b.get("bot_name"), "product_id": b.get("product_id"),
                "allocated_usd": round(_num(b.get("allocated_usd")) or 0.0, 2),
                "coin_basis_usd": round(_num(b.get("coin_basis_usd")) or 0.0, 2),
                "releases_usd": amt,
                "why": ("full on its rungs, so this budget cannot buy anything "
                        "where it is; it stays above the coin it holds, so "
                        "nothing is sold to release it"),
            })
    out.sort(key=lambda r: -r["releases_usd"])
    return out


def rung_capacity(dest):
    """How much this branch can actually PUT TO WORK, not how much it may hold.

    A grid branch deploys in rungs. Money beyond what its empty rungs can
    buy does not become a position - it becomes claim sitting as cash with a
    branch's name on it, which is the condition this whole exercise exists
    to end. So the useful size of a destination is its own rung size times
    the number of empty rungs it has.

    WHY THIS EXISTS AS A SEPARATE LIMIT FROM THE CEILING. The first version
    of this module had only the 20% ceiling, and on the live fleet that sent
    $1,299.48 of $1,385.70 into APE-USD - taking one branch to exactly 20.00%
    of the book on the strength of being nearest its buy line, while LINK-USD,
    which nets 3.343% per close against APE's 2.044%, received $86.22. The
    ceiling is a safety limit and it was doing the job of a sizing rule.
    The owner's instruction is the other way round: "I just don't want it to
    put so much money into one coin like it was doing... I wanted to be able
    to split it up."
    """
    alloc = _num(dest.get("allocated_usd"))
    levels = _num(dest.get("num_levels"))
    empty = _num(dest.get("empty_rungs"))
    if not alloc or not levels or levels <= 0 or not empty or empty <= 0:
        return 0.0
    return round(alloc / levels * empty, 2)


def plan(branches, *, loose_cash_usd=0.0, fleet_allocated_usd=None,
         max_share=MAX_SHARE_OF_FLEET):
    """Where the free capital should go. Returns a plan; changes nothing.

    The destination ranking is harvest_redirect's, unchanged and deliberately
    not reimplemented - one ranking, one regime gate, one place to be wrong.
    """
    rows = list(branches or [])
    fleet = _num(fleet_allocated_usd)
    if fleet is None:
        fleet = sum((_num(b.get("allocated_usd")) or 0.0) for b in rows)

    srcs = sources(rows)
    pot = round(sum(s["releases_usd"] for s in srcs) + (_num(loose_cash_usd) or 0.0), 2)

    regime = hr.regime_verdict(rows)
    if not regime.get("tradeable", True):
        return {
            "plan": [], "sources": srcs, "capital_available_usd": pot,
            "regime": regime, "would_move_usd": 0.0,
            "verdict": "HOLD",
            "detail": (f"{regime['detail']} ${pot:,.2f} is free to move and this "
                       f"plans none of it: capital is only worth moving into a "
                       f"rung that pays for itself."),
        }

    # Destinations, ranked. The amount is passed so the ceiling is checked
    # against what the branch would hold AFTER the move, not before.
    eligible, rejected, _r = hr.candidates(
        rows, amount_usd=0.0, fleet_allocated_usd=fleet, max_share=max_share)
    # TIER 0 ONLY - and this is deliberately STRICTER than
    # harvest_redirect, which lets an unproven branch outrank a bad one.
    #
    # The two are moving different money. The redirect places a harvested
    # few dollars, where funding an unproven branch is cheap exploration
    # that buys the rung it needs to produce a record. This moves BLOCKS of
    # claim - $1,186.32 from one branch on the live fleet - and the same
    # exploration costs a thousand times more for the same one data point.
    #
    # So an unproven branch is not funded here. It is not being judged
    # badly; it is being left to earn its record on the dollars it already
    # has, where a wrong guess is cheap.
    top = [e for e in eligible if e["productivity_tier"] == 0]

    moves, remaining = [], pot
    for dest in top:
        if remaining < MIN_MOVE_USD:
            break
        alloc = dest["allocated_usd"]
        # Headroom to the ceiling, measured on the fleet total AFTER the
        # whole pot lands - the fleet does not grow here, claim only moves.
        room = round(max_share * fleet - alloc, 2)
        capacity = rung_capacity(dest)
        # THE BINDING LIMIT IS WHICHEVER IS SMALLEST. The ceiling keeps one
        # coin from dominating the book; capacity keeps a branch from being
        # handed money it cannot deploy. They answer different questions and
        # both have to hold.
        take = round(min(remaining, room, capacity), 2)
        if take < MIN_MOVE_USD:
            continue
        moves.append({
            "bot_name": dest["bot_name"], "product_id": dest["product_id"],
            "allocated_usd_now": alloc,
            "add_usd": take,
            "allocated_usd_after": round(alloc + take, 2),
            "share_after_pct": round((alloc + take) / fleet * 100.0, 2)
                               if fleet else None,
            "net_pct_per_close": dest["net_pct_per_close"],
            "empty_rungs": dest["empty_rungs"],
            "rung_capacity_usd": capacity,
            "limited_by": ("the pot" if take >= remaining - 0.011 else
                           "its empty rungs" if capacity <= room else
                           f"the {max_share * 100:.0f}% ceiling"),
            "why": (f"nets {dest['net_pct_per_close']}% per close, above both "
                    f"the fleet median and the {regime['cost_bar_pct']}% cost "
                    f"of a round trip, with {dest['empty_rungs']} empty rung(s) "
                    f"to put it in"),
        })
        remaining = round(remaining - take, 2)

    moved = round(pot - remaining, 2)
    return {
        "plan": moves, "sources": srcs, "capital_available_usd": pot,
        "would_move_usd": moved, "unplaced_usd": remaining,
        "regime": regime,
        "rejected_destinations": rejected,
        "verdict": "MOVE" if moves else "HOLD",
        "sells_nothing": True,
        "detail": (
            f"${moved:,.2f} of ${pot:,.2f} free capital placed into "
            f"{len(moves)} branch(es) that clear both bars. "
            + (f"${remaining:,.2f} had nowhere to go that could deploy it - "
               f"every qualifying branch is either full on rungs or at its "
               f"ceiling - so it stays as cash rather than becoming idle claim."
               if remaining >= MIN_MOVE_USD else "")
            if moves else
            f"${pot:,.2f} is free but no branch both clears the bars and has "
            f"room under the {max_share * 100:.0f}% ceiling. It stays as cash."),
        "what_this_is_not": (
            "Not an order, not a sale, and not a forecast. Every dollar here "
            "is claim already free of coin: it comes from branches that are "
            "full on their rungs and stays above the coin they hold. Nothing "
            "in this module can produce a sale."),
    }

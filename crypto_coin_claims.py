"""Which system owns which coin, across the grid fleet AND the family tree.

THE PROBLEM

Both systems hold positions in one Coinbase account, where the balance for
a given coin is POOLED. Two branches on the same coin each track their own
qty against that single shared balance, and the arithmetic stops meaning
anything: each believes it owns tokens the other also believes it owns.

This codebase already carries the scars. The consolidate-branches feature
exists because 15 tree branches piled onto POL-USD in one spawn storm -
"each independently tracking its own qty against one POOLED real Coinbase
balance", which its own docstring names as the structural gap behind both
the phantom-position self-heal and the DB-vs-Coinbase SHORTFALLs.

Each system already guards itself and only itself:

    crypto_grid_bot.get_grid_branch_claimed_coins()   grid branches only
    crypto_family_tree_bot.find_most_volatile_...     tree coins only

crypto_family_tree_bot does not reference CryptoGridBranch anywhere, so
until now the tree could pick a coin the fleet was actively gridding and
neither would notice. That was harmless only while one of the two was not
running. Both went live together on 2026-09-24.

WHY A SEPARATE MODULE

It reads models directly and imports neither bot, so both can import it
without a cycle - crypto_grid_bot already has to import the tree lazily
inside functions to avoid one.

Product ids are normalised because the two systems do not agree on a
separator: tree positions were found stored as "BTC/USD" while product ids
passed to Coinbase are "BTC-USD". Comparing those raw makes every claim
check silently miss - the same bug that returned a 404 from the
reconciliation panel's own Reconcile link.
"""

import logging

from sqlalchemy import select

from database import get_session_factory
from models import BotPosition, CryptoGridBranch, CryptoTreeBranch

log = logging.getLogger("crypto_coin_claims")

GRID = "grid"
TREE = "tree"


def normalize_product(symbol):
    """"BTC/USD", "btc-usd", "BTC" -> "BTC-USD".

    One spelling, so a claim check cannot miss on punctuation. A bare base
    asset is assumed to be the USD pair, which is the only quote either
    system trades.
    """
    if not symbol:
        return ""
    s = str(symbol).strip().upper().replace("/", "-")
    if "-" not in s:
        return f"{s}-USD"
    return s


async def grid_claimed_products(include_inactive: bool = False):
    """Coins the grid fleet owns.

    Inactive branches are excluded by default: a disabled branch is skipped
    by the cycle driver and will not buy, so holding a coin off the tree
    for it would waste an opportunity. A branch that still holds open
    slices is a different matter and IS included regardless of its active
    flag - those tokens are really in the wallet, and a second system
    trading the same coin would corrupt both sides' accounting whether or
    not the branch is currently allowed to buy more.
    """
    async with get_session_factory()() as db:
        result = await db.execute(select(CryptoGridBranch))
        branches = result.scalars().all()
        holding = set()
        if not include_inactive:
            from models import CryptoGridSlice
            slice_result = await db.execute(select(CryptoGridSlice.bot_name).distinct())
            holding = {row[0] for row in slice_result.all()}
    return {
        normalize_product(b.product_id) for b in branches
        if include_inactive or b.active or b.bot_name in holding
    }


async def tree_claimed_products():
    """Coins the family tree owns.

    A tree branch claims its configured product_id, and separately claims
    whatever its BotPosition actually holds - those can differ while a
    branch is mid-rotation, and both matter: the configured coin is what it
    is about to buy, the held coin is what is really in the wallet.
    """
    async with get_session_factory()() as db:
        branch_result = await db.execute(
            select(CryptoTreeBranch.bot_name, CryptoTreeBranch.product_id))
        rows = branch_result.all()
        tree_bots = {r[0] for r in rows}
        claimed = {normalize_product(r[1]) for r in rows if r[1]}

        pos_result = await db.execute(select(BotPosition.bot, BotPosition.symbol))
        for bot, symbol in pos_result.all():
            if bot in tree_bots and symbol:
                claimed.add(normalize_product(symbol))
    return {c for c in claimed if c}


async def claimed_by_other(system: str):
    """Coins `system` must NOT take, because the other system owns them.

    Returns an empty set on any failure. This gate fails OPEN deliberately,
    and the reasoning is narrow: it is a de-confliction courtesy between
    two systems that each already guard themselves, not a safety check
    standing between the bot and a loss. Failing closed would let one
    unreadable table halt all coin selection on both sides, which is a
    larger outage than the overlap it prevents.
    """
    try:
        if system == GRID:
            return await tree_claimed_products()
        if system == TREE:
            return await grid_claimed_products()
        log.warning("unknown system %r - no coins withheld", system)
        return set()
    except Exception as e:
        log.warning("cross-system coin claims unreadable (%s: %s) - withholding no coins "
                    "this cycle rather than blocking selection entirely", type(e).__name__, e)
        return set()


async def claim_map():
    """product_id -> owning system, for the dashboard. Overlaps are listed
    as "grid+tree", which is the state this module exists to prevent and
    therefore the one worth seeing immediately if it ever occurs."""
    grid = await grid_claimed_products()
    tree = await tree_claimed_products()
    out = {}
    for p in grid | tree:
        if p in grid and p in tree:
            out[p] = "grid+tree"
        else:
            out[p] = GRID if p in grid else TREE
    return out

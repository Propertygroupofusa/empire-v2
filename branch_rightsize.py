"""Right-size a branch whose allocation is larger than the coin it holds.

THE PROBLEM, MEASURED 2026-10-04. XRP-USD carries allocated_usd of
$2,228.05 against $653.60 of actual coin, on a 3-level grid holding 7
slices. The other $1,574.45 is budget for rungs that cannot exist: the
branch is full, so it cannot buy, and allocated_usd only ever moves on a
completed sell. That $1,574.45 is CASH sitting in the wallet, claimed by a
branch that is structurally unable to spend it. Across the six parked
branches the same arithmetic strands $1,830.57.

Nothing in the codebase could free it. Every path that lowers
allocated_usd goes through withdraw_from_grid_branch, which refuses any
branch holding an open slice - correctly, because that cash is already
bought coin. But here it is NOT bought coin. The refusal is right about
the general case and wrong about this one, and the difference is
measurable per branch rather than assumed.

WHAT THIS DOES. Lowers allocated_usd toward the coin the branch actually
holds, and stops there. The freed amount stops being claimed by that
branch and becomes deployable cash.

WHAT THIS IS NOT. It is not a withdrawal and not a sale. No order is
placed, no coin changes hands, no slice is touched, no branch row is
deleted. The account holds exactly the same coins and exactly the same
dollars one second after this runs as one second before. The only thing
that changes is which branch is claiming budget it cannot use.

THE FLOOR IS COIN BASIS, AND IT IS RE-CHECKED AT WRITE TIME. Reducing a
branch below the cost basis of the coin it holds would make allocated_usd
understate a real position, which breaks _grid_branch_real_equity, the
drawdown breaker that reads it, and the P&L on the branch's next sell.
So the floor is max(coin basis, $15) - the $15 also being the row-deletion
floor the harvest and rotation already keep. The floor is computed again
from fresh database rows inside the writing transaction: a slice selling
between preview and execute raises the basis, and a plan computed seconds
earlier must not be allowed to cut past the new floor.

ONLY PARKED BRANCHES. A branch with a free rung will spend its budget on
the next dip, so its budget is not stranded and this leaves it alone. A
FLAT branch goes through withdraw_from_grid_branch, which already handles
it and is tested. This touches only the case neither covers: full on its
rungs, holding coin, carrying budget it can never reach.

WHAT THE OWNER WILL SEE. TOTAL ALLOCATED (GRID) GOES DOWN by the freed
amount. The money is not gone - it moves from "claimed by XRP" to
"available to deploy". Every caller must say so.
"""
import logging

log = logging.getLogger(__name__)

# Never leave a branch under this even if its coin basis is lower: a row
# drained below a cent is DELETED, and a deleted branch takes its coin out
# of the fleet. Same floor the harvest and the idle rotation keep.
MIN_BRANCH_USD = 15.0

# Below this, freeing is not worth a write or a feed row.
MIN_FREE_USD = 10.0


def coin_basis(slices) -> float:
    """What the branch really paid for the coin it is holding right now.

    Cost basis, not market value, because allocated_usd is itself a
    cost-basis figure (see CryptoGridBranch's own docstring: it moves only
    by realised net P&L on a sell, never at buy time). Flooring a
    cost-basis column at a market-value number would be comparing two
    different things, and on a branch that is down - which every parked
    branch here is - market value is the SMALLER of the two, so it would
    cut deeper than the real position. Basis is the conservative floor.
    """
    total = 0.0
    for s in (slices or []):
        qty = float(getattr(s, "qty", None) if not isinstance(s, dict)
                    else s.get("qty") or 0.0) or 0.0
        entry = float(getattr(s, "entry_price", None) if not isinstance(s, dict)
                      else s.get("entry_price") or 0.0) or 0.0
        total += qty * entry
    return round(total, 2)


def _slices_of(branch):
    return (branch.get("slices") or []) if isinstance(branch, dict) else []


def floor_for(branch) -> float:
    """The lowest allocated_usd this branch may be reduced to."""
    return round(max(coin_basis(_slices_of(branch)), MIN_BRANCH_USD), 2)


def freeable(branch):
    """Dollars this branch is claiming but cannot spend, and why not if zero.

    Returns (amount, why_not).
    """
    slices = _slices_of(branch)
    levels = int(branch.get("num_levels") or 1)
    alloc = round(float(branch.get("allocated_usd") or 0.0), 2)

    if not slices:
        return 0.0, ("branch is flat; withdraw_from_grid_branch already "
                     "handles a flat branch and is the tested path")
    if len(slices) < levels:
        return 0.0, (f"has a free rung ({len(slices)}/{levels}); its budget is "
                     f"not stranded - it will buy the next dip")

    fl = floor_for(branch)
    amount = round(alloc - fl, 2)
    if amount < MIN_FREE_USD:
        return 0.0, (f"only ${amount:,.2f} above its ${fl:,.2f} floor "
                     f"(coin basis ${coin_basis(slices):,.2f}), under the "
                     f"${MIN_FREE_USD:,.2f} minimum")
    return amount, None


async def plan(grid):
    """What right-sizing would free, per branch. Reads only; writes nothing."""
    status = await grid.get_grid_status()
    branches = status.get("branches") or []
    rows, total = [], 0.0
    for b in branches:
        bot = b.get("bot_name")
        if not bot:
            continue
        slices = _slices_of(b)
        amount, why_not = freeable(b)
        rows.append({
            "bot_name": bot,
            "product_id": b.get("product_id"),
            "allocated_usd": round(float(b.get("allocated_usd") or 0.0), 2),
            "coin_basis_usd": coin_basis(slices),
            "floor_usd": floor_for(b),
            "slices": len(slices),
            "num_levels": int(b.get("num_levels") or 1),
            "parked": bool(slices) and len(slices) >= int(b.get("num_levels") or 1),
            "freeable_usd": round(amount, 2),
            "why_not": why_not,
        })
        total += amount
    rows.sort(key=lambda r: -r["freeable_usd"])
    return {
        "branches": rows,
        "total_freeable_usd": round(total, 2),
        "ready": round(total, 2) >= MIN_FREE_USD,
        "read_only": True,
        "detail": (
            f"${round(total, 2):,.2f} is claimed by parked branches that cannot "
            f"spend it. Freeing it places no order and sells no coin - it only "
            f"stops a branch claiming budget it cannot reach. TOTAL ALLOCATED "
            f"(GRID) WILL GO DOWN by this amount; the money is not gone, it "
            f"becomes cash available to deploy."),
    }


async def apply_one(grid, bot_name, amount_usd=None, dry_run=True):
    """Lower ONE branch's allocated_usd toward its coin basis.

    amount_usd=None frees everything above the floor. A smaller amount is
    honoured; a larger one is CLAMPED to the floor rather than refused,
    and the result says so, because the floor is the invariant and the
    requested number is only a preference.

    Everything is re-derived from fresh rows inside the transaction. The
    caller's plan may be seconds old, and a slice that sold in between
    raises the basis and therefore the floor.
    """
    from sqlalchemy import select

    from models import CryptoGridBranch, CryptoGridSlice

    async with grid.get_session_factory()() as db:
        branch = (await db.execute(select(CryptoGridBranch).where(
            CryptoGridBranch.bot_name == bot_name))).scalars().first()
        if branch is None:
            return {"ok": False, "reason": f"no branch named {bot_name!r}"}

        slices = (await db.execute(select(CryptoGridSlice).where(
            CryptoGridSlice.bot_name == bot_name))).scalars().all()
        if not slices:
            return {"ok": False, "reason": (
                f"{bot_name} is flat; use withdraw_from_grid_branch, which "
                f"already handles a flat branch and is the tested path")}

        levels = int(branch.num_levels or 1)
        if len(slices) < levels:
            return {"ok": False, "reason": (
                f"{bot_name} has a free rung ({len(slices)}/{levels}) - its "
                f"budget is not stranded and will be spent on the next dip")}

        basis = coin_basis(slices)
        alloc = round(float(branch.allocated_usd or 0.0), 2)
        floor = round(max(basis, MIN_BRANCH_USD), 2)
        headroom = round(alloc - floor, 2)

        if headroom < MIN_FREE_USD:
            return {"ok": False, "reason": (
                f"{bot_name} is only ${headroom:,.2f} above its ${floor:,.2f} "
                f"floor (coin basis ${basis:,.2f}), under the "
                f"${MIN_FREE_USD:,.2f} minimum")}

        want = headroom if amount_usd is None else round(float(amount_usd), 2)
        if want <= 0:
            return {"ok": False, "reason": f"nothing to free (asked for {want})"}
        take = min(want, headroom)
        clamped = take < want
        new_alloc = round(alloc - take, 2)

        # The invariant, asserted rather than trusted. If this is ever false
        # the write does not happen: understating allocated_usd below the
        # basis of real held coin corrupts equity, the drawdown breaker and
        # the next sell's P&L.
        if new_alloc < floor - 0.005:
            return {"ok": False, "reason": (
                f"refused: would leave ${new_alloc:,.2f}, under the "
                f"${floor:,.2f} floor")}

        result = {
            "ok": True, "bot_name": bot_name,
            "product_id": branch.product_id,
            "slices": len(slices), "num_levels": levels,
            "coin_basis_usd": basis,
            "allocated_before": alloc, "allocated_after": new_alloc,
            "floor_usd": floor, "freed_usd": round(take, 2),
            "clamped_to_floor": clamped,
            "dry_run": bool(dry_run),
            "detail": (
                f"{'WOULD free' if dry_run else 'Freed'} ${take:,.2f} from "
                f"{branch.product_id}: allocated ${alloc:,.2f} -> "
                f"${new_alloc:,.2f}, still at or above its ${floor:,.2f} coin "
                f"basis. No order was placed and no coin was sold. TOTAL "
                f"ALLOCATED (GRID) goes down by ${take:,.2f}; that money is "
                f"now cash available to deploy, not money lost."),
        }
        if dry_run:
            return result

        branch.allocated_usd = new_alloc
        await db.commit()

    log.warning(f"[rightsize] {result['product_id']}: freed ${take:,.2f}, "
                f"allocated {alloc:,.2f} -> {new_alloc:,.2f} "
                f"(coin basis {basis:,.2f}) - no order, no sale")
    try:
        await grid._log_activity_safe(
            None, result["product_id"], "RIGHTSIZE",
            f"Freed ${take:,.2f} of budget this branch could not spend "
            f"({len(slices)}/{levels} rungs full). Allocated ${alloc:,.2f} -> "
            f"${new_alloc:,.2f}, still covering its ${basis:,.2f} of coin. "
            f"No order placed, no coin sold.")
    except Exception:
        pass
    return result

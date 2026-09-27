"""The loop that actually adopts. The only file here that writes slices.

It spends nothing and sells nothing - adoption is bookkeeping - but it
hands real coin to a trading loop, and from that moment the coin is
traded rather than held. That is a one-way change in how the owner's
money behaves, so the permission lives in a setting and is checked twice,
exactly as the trimmer and the resting stops are.

  1. `is_armed()` before anything is fetched, so an observing deployment
     does no work and cannot fail into adopting.
  2. immediately before the write, against the same constant.

WHAT IT WRITES, AND THE THREE THINGS THAT MUST HOLD

  allocated_usd == the market value of the slices written. Adoption adds
  the same amount to both sides of the backing ledger or it does not
  happen. A branch claiming more than its coin is the exact hole
  allocation_backing exists to detect.

  num_levels == the number of slices. The branch then starts FULL, and
  run_grid_branch_cycle only buys when len(slices) < num_levels - so it
  physically cannot buy until it has sold, and the cash for every rebuy
  comes from a sale it already made. Set num_levels higher and the branch
  immediately tries to buy rungs with cash nobody earmarked to it.

  stop_loss_pct_override == 0. The fleet stop sells any slice 8% below
  its ENTRY, and an adopted entry is the price on the day it was adopted,
  not a price anyone paid. An 8% wobble would liquidate a hold of any age
  and book it as a stop_loss against a cost basis that never existed.
  Adopted coin is covered at the PORTFOLIO level by the resting stops
  instead, which is where a hold belongs.

EVERY DECISION IS WRITTEN DOWN, including the refusals. "Why was XLM
skipped" is the question asked after a bad week, and it only has an
answer if the skips were recorded.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime

import coin_adoption

log = logging.getLogger("coin_adoption")

HEARTBEAT = {
    "started_at": None,
    "last_pass_at": None,
    "passes": 0,
    "adopted": 0,
    "last_result": None,
    "last_error": None,
}

MODE_ENV = "COIN_ADOPTION_MODE"
CHECK_SECONDS = int(os.getenv("COIN_ADOPTION_CHECK_SECONDS", "3600"))

# The stop an adopted branch trades under. Zero on purpose - see above.
ADOPTED_STOP_PCT = float(os.getenv("COIN_ADOPTION_STOP_PCT", "0"))


def current_mode() -> str:
    """`observe` unless the setting says exactly `arm`.

    Stripped and lowercased, so "ARM " arms it. That is deliberate and is
    stated here because the docstring once claimed the opposite and the
    account owner was told the wrong thing twice.
    """
    return (os.getenv(MODE_ENV, "observe") or "observe").strip().lower()


def is_armed() -> bool:
    return current_mode() == "arm"


async def _already_adopted(session_factory):
    """Coins this loop has taken charge of before, so it never does it twice."""
    from models import CryptoGridSlice
    from sqlalchemy import select
    async with session_factory()() as db:
        rows = (await db.execute(
            select(CryptoGridSlice.product_id)
            .where(CryptoGridSlice.adopted == True))).scalars().all()  # noqa: E712
    return {str(p).split("-")[0].upper() for p in rows if p}


async def check_once(session_factory, *, force_preview=False):
    """One pass. Adopts only when armed; otherwise sizes and stops."""
    import aiohttp
    import account_census
    import crypto_grid_bot as grid
    from models import CryptoGridBranch, CryptoGridSlice
    from sqlalchemy import select

    armed = is_armed()
    if not armed and not force_preview:
        return {"armed": False, "adopted": 0,
                "detail": f"{MODE_ENV} is '{current_mode()}' - observing, nothing fetched"}

    # The claim list is FATAL when unreadable. Adopting a coin a branch
    # already holds puts two systems on one balance, which is the
    # structural gap behind this repo's phantom positions.
    claimed = []
    st = await grid.get_grid_status()
    claimed = [b.get("product_id") for b in (st.get("branches") or []) if b.get("product_id")]
    try:
        import crypto_coin_claims as claims
        claimed += list(await claims.claimed_by_other(claims.GRID))
    except Exception as exc:
        return {"armed": armed, "adopted": 0,
                "detail": (f"family-tree claims unreadable ({type(exc).__name__}) - refusing "
                           f"to adopt against an unknown claim list")}

    async with aiohttp.ClientSession() as session:
        census = await account_census.census(session, tracked_usd=0.0)
    if not census.get("available"):
        return {"armed": armed, "adopted": 0,
                "detail": f"census unavailable ({str(census.get('error'))[:60]}) - nothing sized"}

    done = await _already_adopted(session_factory)
    plan = coin_adoption.plan(census.get("holdings") or [],
                              account_total_usd=census.get("total_usd"),
                              claimed_products=claimed, already_adopted=done)

    if not plan["ok"]:
        return {"armed": armed, "adopted": 0, "plan": plan,
                "detail": f"nothing adoptable: {plan['detail'][:140]}"}
    if not armed:
        return {"armed": False, "adopted": 0, "plan": plan,
                "detail": f"PREVIEW only - would adopt ${plan['total_usd']:,.2f}"}

    written = []
    for a in plan["adopt"]:
        # Checked again here, against the same constant, immediately
        # before the write. Between the first check and this one the
        # account was read and the plan sized; neither can arm anything.
        if not is_armed():
            log.warning("[adopt] disarmed mid-pass - stopping before the write")
            break
        try:
            async with session_factory()() as db:
                existing = (await db.execute(
                    select(CryptoGridBranch)
                    .where(CryptoGridBranch.product_id == a["product_id"]))).scalars().first()
                if existing is not None:
                    log.warning(f"[adopt] {a['product_id']} gained a branch since the plan "
                                f"was sized - skipping rather than doubling up")
                    continue

                nums = {int(b.bot_name.rsplit("_", 1)[-1])
                        for b in (await db.execute(select(CryptoGridBranch))).scalars().all()
                        if b.bot_name and b.bot_name.rsplit("_", 1)[-1].isdigit()}
                n = 1
                while n in nums:
                    n += 1

                branch = CryptoGridBranch(
                    bot_name=f"crypto_grid_{n}",
                    product_id=a["product_id"],
                    allocated_usd=a["allocated_usd"],
                    active=True,
                    grid_pct=getattr(grid, "DEFAULT_GRID_PCT", 0.025),
                    # FULL from the first cycle: it cannot buy until it sells.
                    num_levels=a["num_levels"],
                    reference_price=a["price"],
                    peak_equity=a["allocated_usd"],
                    # An adopted entry is not a price anyone paid.
                    stop_loss_pct_override=ADOPTED_STOP_PCT,
                    # Over the 20% rule -> may sell, never buys back, so
                    # the position walks DOWN through strength instead of
                    # being repurchased on the next dip.
                    buys_paused=bool(a.get("sell_only")),
                )
                db.add(branch)
                for sl in a["slices"]:
                    db.add(CryptoGridSlice(
                        bot_name=branch.bot_name,
                        product_id=a["product_id"],
                        entry_price=sl["entry_price"],
                        qty=sl["qty"],
                        opened_at=datetime.utcnow(),
                        adopted=True,
                    ))
                await db.commit()
            written.append({"asset": a["asset"], "usd": a["allocated_usd"],
                            "bot_name": branch.bot_name, "slices": len(a["slices"]),
                            "sell_only": bool(a.get("sell_only"))})
            HEARTBEAT["adopted"] += 1
            log.warning(f"[adopt] 🌱 {a['asset']} ${a['allocated_usd']:,.2f} adopted into "
                        f"{branch.bot_name} as {len(a['slices'])} slice(s) at "
                        f"${a['price']:,.8f}"
                        + (" SELL-ONLY (over the 20% rule - it will walk the position down "
                           "through strength and never buy back)" if a.get("sell_only") else "")
                        + ". Nothing bought, nothing sold.")
        except Exception as exc:
            log.warning(f"[adopt] {a['asset']} failed: {type(exc).__name__}: {exc}")

    return {"armed": True, "adopted": len(written), "written": written, "plan": plan,
            "detail": (f"{len(written)} coin(s) adopted, "
                       f"${sum(w['usd'] for w in written):,.2f} put to work"
                       if written else "armed, but nothing was written")}


async def run_periodically(session_factory):
    """Never dies. A loop that raises adopts nothing and says nothing."""
    HEARTBEAT["started_at"] = datetime.utcnow().isoformat() + "Z"
    log.info(f"[adopt] loop up, mode={current_mode()}, every {CHECK_SECONDS}s")
    while True:
        try:
            r = await check_once(session_factory)
            HEARTBEAT["last_result"] = r.get("detail")
            HEARTBEAT["last_error"] = None
            if r.get("adopted"):
                log.warning(f"[adopt] {r['detail']}")
        except Exception as e:
            HEARTBEAT["last_error"] = f"{type(e).__name__}: {e}"
            HEARTBEAT["last_result"] = None
            log.warning(f"[adopt] pass failed: {type(e).__name__}: {e}")
        HEARTBEAT["last_pass_at"] = datetime.utcnow().isoformat() + "Z"
        HEARTBEAT["passes"] += 1
        await asyncio.sleep(max(CHECK_SECONDS, 60))

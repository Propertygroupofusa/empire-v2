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
    "topped_up": 0,
    "last_result": None,
    "last_topup_result": None,
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


async def _adopted_so_far(session_factory):
    """What this loop has ALREADY adopted, so the cap binds across passes.

    THE HOLE THIS CLOSES. `plan()` starts `spent` at 0.0 every call, and
    `already_adopted` only stops a coin being taken twice - it says nothing
    about the budget. So an hourly loop handed a fresh $1,000 and a fresh
    3-coin allowance on every pass adopted a NEW $1,000 every hour against
    a cap the owner chose precisely because it was bounded. Measured live:
    15 coins and $3,082.16 under branches, against a stated 3 and $1,000.
    Nothing was bought and nothing was lost - adoption is bookkeeping - but
    a limit that does not bind is not a limit, and the owner picked $1,000
    over $2,100 and $8,600 on purpose.

    The marker is `stop_loss_pct_override IS NOT NULL`, which ONLY this
    loop writes (checked: nothing else in the repo sets it). It survives
    the branch selling and rebuying every slice, which the adopted=True
    slice flag does not - those rows disappear on the first sale, and a
    budget measured from them would quietly refill itself.

    The figure is current allocated_usd, which GROWS with realized profit
    (run_grid_branch_cycle adds pnl to it). So this can over-count what was
    originally adopted, never under-count - the cap tightens as adopted
    branches earn, and the error can only ever refuse an adoption, not
    permit one. That is the safe direction, and it is the reason this
    returns a measured number rather than an estimate.
    """
    from models import CryptoGridBranch
    from sqlalchemy import select
    async with session_factory()() as db:
        rows = (await db.execute(
            select(CryptoGridBranch.product_id, CryptoGridBranch.allocated_usd)
            .where(CryptoGridBranch.stop_loss_pct_override.isnot(None)))).all()
    usd = round(sum(float(r[1] or 0.0) for r in rows), 2)
    return {"coins": len(rows), "usd": usd,
            "products": [str(r[0]) for r in rows if r[0]]}


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

    # THE CAP IS CUMULATIVE, NOT PER-PASS. Without this the hourly loop got
    # a fresh $1,000 and a fresh 3 coins every hour - see _adopted_so_far.
    prior = await _adopted_so_far(session_factory)
    room_usd = round(max(0.0, coin_adoption.MAX_TOTAL_ADOPT_USD - prior["usd"]), 2)
    room_coins = max(0, coin_adoption.MAX_COINS - prior["coins"])
    if room_usd < coin_adoption.MIN_ADOPT_USD or room_coins <= 0:
        return {"armed": armed, "adopted": 0, "prior": prior,
                "detail": (f"cap reached: {prior['coins']} coin(s) and ${prior['usd']:,.2f} "
                           f"are already adopted, against a cap of "
                           f"{coin_adoption.MAX_COINS} and "
                           f"${coin_adoption.MAX_TOTAL_ADOPT_USD:,.0f}. Nothing more is "
                           f"adopted until the cap is raised deliberately.")}

    plan = coin_adoption.plan(census.get("holdings") or [],
                              account_total_usd=census.get("total_usd"),
                              claimed_products=claimed, already_adopted=done,
                              max_total_usd=room_usd, max_coins=room_coins)
    plan["prior_adopted"] = prior
    plan["room"] = {"usd": room_usd, "coins": room_coins}

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


async def topup_once(session_factory, *, force_preview=False):
    """One top-up pass: put idle coin under branches that already exist.

    Separate from check_once on purpose. Adoption opens a branch for a coin
    that has none and refuses anything already claimed; this deepens a
    branch that exists. Same arm switch, same double check, same refusal to
    buy or sell anything - but a different question, and merging them would
    make the ALREADY_CLAIMED refusal ambiguous.

    It writes to branches that are actively trading, so the branch row is
    re-read inside the transaction and every figure is recomputed from it.
    A plan sized a second earlier can already be stale: a sell between the
    read and the write changes allocated_usd and the open slice count, and
    writing the planned numbers over that would silently undo the sale.
    """
    import aiohttp
    import account_census
    import coin_topup
    import crypto_grid_bot as grid
    from models import CryptoGridBranch, CryptoGridSlice
    from sqlalchemy import select

    armed = is_armed()
    if not armed and not force_preview:
        return {"armed": False, "topped_up": 0,
                "detail": f"{MODE_ENV} is '{current_mode()}' - observing, nothing fetched"}

    st = await grid.get_grid_status()
    branches = st.get("branches") or []
    if not branches:
        return {"armed": armed, "topped_up": 0, "detail": "no branches to top up"}

    async with aiohttp.ClientSession() as session:
        census = await account_census.census(session, tracked_usd=0.0)
    if not census.get("available"):
        return {"armed": armed, "topped_up": 0,
                "detail": f"census unavailable ({str(census.get('error'))[:60]}) - nothing sized"}

    plan = coin_topup.plan(census.get("holdings") or [], branches,
                           account_total_usd=census.get("total_usd"))
    if not plan["ok"]:
        return {"armed": armed, "topped_up": 0, "plan": plan,
                "detail": f"nothing to top up: {plan['detail'][:140]}"}
    if not armed:
        return {"armed": False, "topped_up": 0, "plan": plan,
                "detail": f"PREVIEW only - would add ${plan['total_usd']:,.2f}"}

    written = []
    for t in plan["topups"]:
        # Checked again immediately before the write, against the same
        # constant. Between the first check and this one the account was
        # read and the plan sized; neither can arm anything.
        if not is_armed():
            log.warning("[topup] disarmed mid-pass - stopping before the write")
            break
        try:
            async with session_factory()() as db:
                row = (await db.execute(
                    select(CryptoGridBranch)
                    .where(CryptoGridBranch.bot_name == t["bot_name"]))).scalars().first()
                if row is None:
                    log.warning(f"[topup] {t['bot_name']} vanished since the plan - skipping")
                    continue
                # Re-checked against the LIVE row, not the planned one. The
                # override is what makes this branch safe to deepen, and a
                # branch that lost it between the read and now must not be
                # written to.
                if row.stop_loss_pct_override is None:
                    log.warning(f"[topup] {t['bot_name']} is no longer an adopted branch "
                                f"- skipping rather than moving its stop")
                    continue
                open_now = (await db.execute(
                    select(CryptoGridSlice)
                    .where(CryptoGridSlice.bot_name == row.bot_name))).scalars().all()

                added_usd = round(sum(s["qty"] * s["entry_price"] for s in t["slices"]), 2)
                for sl in t["slices"]:
                    db.add(CryptoGridSlice(
                        bot_name=row.bot_name,
                        product_id=row.product_id,
                        entry_price=sl["entry_price"],
                        qty=sl["qty"],
                        opened_at=datetime.utcnow(),
                        adopted=True,
                    ))
                # THE THREE FIGURES MOVE TOGETHER OR NOT AT ALL.
                #
                # allocated_usd by exactly the coin put behind it, so the
                # backing ledger gains the same amount on both sides.
                row.allocated_usd = round((row.allocated_usd or 0.0) + added_usd, 2)
                # num_levels from the LIVE open count plus the new slices,
                # never the planned count: the branch must end this write
                # exactly full, and a sale since the plan was sized would
                # otherwise leave it short and free to buy.
                #
                # THIS WRITE DOES NOT SURVIVE, and that is the point of the
                # comment. run_grid_branch_cycle re-applies the global
                # spacing override every pass and forces num_levels back to
                # its own figure (3, under 3_levels_2.5pct). So the branch
                # ends up with MORE open slices than levels - measured live:
                # ZEC 6/3, XRP 9/3, SHIB 6/3 - which made it unable to buy
                # AND, until GRID_PARKED_MIN_NET_PCT, unable to sell.
                #
                # It is still written, because it is the correct value at
                # the moment of the write and a branch must never be left
                # short of full even for one cycle. The parked-sell path is
                # what makes the override's reclaim survivable rather than
                # something this has to fight.
                row.num_levels = len(open_now) + len(t["slices"])
                # Equity is allocated + unrealized, and a slice entered at
                # the current price adds no unrealized - so equity rises by
                # exactly added_usd. The cycle ratchet would lift the peak
                # anyway at the next pass; doing it here keeps the stored
                # row consistent with itself in the meantime.
                row.peak_equity = round((row.peak_equity or row.allocated_usd) + added_usd, 2)
                if t.get("sell_only"):
                    row.buys_paused = True
                await db.commit()
            written.append({"asset": t["asset"], "bot_name": t["bot_name"],
                            "added_usd": added_usd, "slices": len(t["slices"]),
                            "sell_only": bool(t.get("sell_only"))})
            HEARTBEAT["topped_up"] += 1
            log.warning(f"[topup] 🌿 {t['asset']} +${added_usd:,.2f} of coin already owned "
                        f"put under {t['bot_name']} as {len(t['slices'])} slice(s) at "
                        f"${t['price']:,.8f}. Nothing bought, nothing sold."
                        + (" SELL-ONLY." if t.get("sell_only") else ""))
        except Exception as exc:
            log.warning(f"[topup] {t['asset']} failed: {type(exc).__name__}: {exc}")

    return {"armed": True, "topped_up": len(written), "written": written, "plan": plan,
            "detail": (f"{len(written)} branch(es) deepened, "
                       f"${sum(w['added_usd'] for w in written):,.2f} of idle coin put to work"
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

            # Adoption first, then the top-up: a coin adopted this pass
            # gets its branch before the top-up reads the fleet, so its
            # remaining idle coin is offered on the NEXT pass rather than
            # being deepened in the same breath as being opened.
            try:
                tr = await topup_once(session_factory)
                HEARTBEAT["last_topup_result"] = tr.get("detail")
                if tr.get("topped_up"):
                    log.warning(f"[topup] {tr['detail']}")
            except Exception as e:
                HEARTBEAT["last_topup_result"] = f"{type(e).__name__}: {e}"
                log.warning(f"[topup] pass failed: {type(e).__name__}: {e}")
        except Exception as e:
            HEARTBEAT["last_error"] = f"{type(e).__name__}: {e}"
            HEARTBEAT["last_result"] = None
            log.warning(f"[adopt] pass failed: {type(e).__name__}: {e}")
        HEARTBEAT["last_pass_at"] = datetime.utcnow().isoformat() + "Z"
        HEARTBEAT["passes"] += 1
        await asyncio.sleep(max(CHECK_SECONDS, 60))

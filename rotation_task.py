"""Rotate idle allocation into the six coins with the deepest dip. Once, at boot.

WHAT THIS IS. continuous_rotation.py measured it: running the grids
continuously and rotating only IDLE allocation into the six coins with the
largest dip_depth realised $649.35 over six months against $364.86 for
never moving it - +78%, zero negative realised months, 3.33%/month on
$3,253.84. This applies that policy once, in-process, at startup.

SHIPPED INERT. Nothing happens unless ROTATION_TASK_TICKET is set in the
environment, which only the account owner can do. Deploying this file
changes no behaviour by itself.

THE LIVE CONSTRAINT THE BACKTEST DID NOT HAVE, AND IT IS SEVERE.

crypto_grid_bot.withdraw_from_grid_branch refuses outright on a branch
holding open slices: "can only withdraw from a FLAT branch". The model
moved idle out of any coin, including coins with rungs open. Live, that
path does not exist. So this task can only source from branches that are
genuinely FLAT - zero open slices - which today is five of them holding
$451.36, not the $3,253.84 the model rotated.

That is a real reduction of the measured result and it is stated here
rather than discovered later: at the measured rate the reachable version
is worth roughly a seventh of the modelled one. It is not a smaller
version of the same thing by choice; it is what the existing guards allow.

RELEASING IDLE FROM A NON-FLAT BRANCH would unlock the rest. It is a
different write - lowering allocated_usd toward (never below) the cost
basis of the open rungs, which is the same one-directional downward move
reconcile.py already makes for a different reason. It is a NEW
money-moving primitive, so it sits behind its own separate variable,
ROTATION_TASK_RELEASE_DEPLOYED_IDLE, default off, and the owner arms it as
a distinct decision. Nothing is released from a branch whose deployed cost
basis cannot be read.

WHAT IT WRITES: allocated_usd on grid branches, down on sources and up on
targets. Nothing else. It places NO order, sells NO coin, touches no
spacing, reference price, stop, level count, breaker or threshold. The
grid decides when to buy, exactly as before; this only decides where the
budget sits.

EVERY WAY IT FAILS CLOSED:
  no ticket / ticket done / attempts spent / STOP_TRADING  -> nothing runs
  candles unreadable for a coin      -> that coin is not ranked. UNKNOWN,
                                        never treated as a zero dip.
  fewer than MIN_TARGETS rankable    -> nothing moves
  a source is not flat               -> skipped, never forced
  a target would breach 20%          -> replaced by the next in the ranking
  transfers do not balance to the cent-> nothing is written at all
  a transfer under MIN_TRANSFER_USD  -> dropped, so no unusable dust moves

CONSERVATION IS CHECKED BEFORE THE WRITE, NOT AFTER. The sum withdrawn
must equal the sum added. A rotation that mints or deletes capital would
look exactly like profit or loss that never happened, and continuous
rotation's own tests caught a float-equality bug of precisely that family.

Usage: set ROTATION_TASK_TICKET, deploy. GET /rotation-task reads back
what it did.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

TICKET_ENV = "ROTATION_TASK_TICKET"
RELEASE_ENV = "ROTATION_TASK_RELEASE_DEPLOYED_IDLE"
MARKER_PREFIX = "rotation_task:"

MAX_ATTEMPTS = 5
DONE = -1.0

# The measured width. continuous_rotation.py: top 3 realised $408.12 with
# two dead months where the book parked; top 6 realised $649.35 with none;
# top 10 realised $522.75 with one negative month. Six is not a round
# number, it is where the measurement put the balance.
TOP_N = 6
MIN_TARGETS = 3

# The owner's standing limit. Not a tuning knob.
MAX_COIN_SHARE_PCT = 20.0

# Below this a transfer is noise that still changes slice sizing.
MIN_TRANSFER_USD = 10.0
# A slice under this cannot be placed (MIN_TRADE_USD in crypto_grid_bot).
MIN_SLICE_USD = 5.0

# DRAINING A FLAT BRANCH TO ZERO DELETES IT.
#
# withdraw_from_grid_branch ends with `deleted = branch.allocated_usd <
# 0.01` and removes the row, releasing the coin. A dry run against the live
# book had this task take 100% of JASMY and TIA - both flat - which would
# have silently destroyed two branches as a side effect of a rotation. They
# hold no coin so nothing would have been lost but the rows; it would still
# have been a deletion nobody asked for. Every source keeps at least the
# $15 viability floor (MIN_TRADE_USD x MIN_LEVELS) so the branch survives.
KEEP_BRANCH_ALIVE_USD = 15.0
# Money is compared in cents. Float equality on dollars stalled the
# continuous engine for 498 candles before a test caught it.
CENT = 0.01

DIP_LOOKBACK_DAYS = 30

_LAST = None


def last_report():
    return _LAST


def ticket():
    raw = (os.getenv(TICKET_ENV) or "").strip().strip('"').strip("'")
    return raw or None


def release_armed():
    raw = (os.getenv(RELEASE_ENV) or "").strip().strip('"').strip("'").lower()
    return raw in {"yes", "true", "1", "on", "arm", "armed"}


def dip_depth(closes):
    """How far below its own high in the window the coin closed, 0..1.

    None when it cannot be computed. None is UNKNOWN and the caller must
    leave the coin unranked - a coin whose candles failed is not a coin
    with no dip.
    """
    if not closes:
        return None
    try:
        hi = max(float(c) for c in closes if c is not None)
    except (TypeError, ValueError):
        return None
    if not hi or hi <= 0:
        return None
    last = closes[-1]
    try:
        last = float(last)
    except (TypeError, ValueError):
        return None
    return (hi - last) / hi


async def rank_by_dip(product_ids, days=DIP_LOOKBACK_DAYS, fetch=None):
    """[(product_id, dip)] deepest first, plus the ones that could not be read."""
    import aiohttp

    import crypto_selection_backtest as bt
    fetch = fetch or bt.fetch_historical_candles
    ranked, unreadable = [], []
    async with aiohttp.ClientSession() as s:
        for pid in product_ids:
            try:
                res = await fetch(s, pid, days=days)
            except Exception as e:
                unreadable.append({"product_id": pid, "why": f"{type(e).__name__}: {e}"})
                continue
            closes = (res or [None])[0]
            d = dip_depth(closes)
            if d is None:
                unreadable.append({"product_id": pid, "why": "no usable candles"})
                continue
            ranked.append((pid, d))
    ranked.sort(key=lambda r: -r[1])
    return ranked, unreadable


def _slices(b):
    return b.get("slices") or []


def _deployed(b):
    tot = 0.0
    for s in _slices(b):
        q, e = s.get("qty"), s.get("entry_price")
        if q is None or e is None:
            return None            # UNKNOWN: an unpriced rung makes the
        tot += float(q) * float(e)  # branch's deployed cost unknowable
    return tot


def plan(branches, ranked, release_deployed_idle=False, top_n=TOP_N):
    """What would move. Changes nothing.

    Sources: branches NOT in the target set that hold releasable idle.
    A FLAT branch (no open slices) can release all of its allocation,
    because withdraw_from_grid_branch permits exactly that. A branch with
    open rungs can release allocation down to - never below - the cost
    basis of those rungs, and only when the separate release switch is on.
    """
    by = {str(b.get("product_id")): b for b in (branches or [])
          if b.get("product_id")}
    book_total = sum(float(b.get("allocated_usd") or 0.0) for b in by.values())

    # BUILD THE TARGET SET AGAINST THE CEILING AS IT WALKS, not afterwards.
    #
    # The first version took the top six and then filtered - so when ZEC
    # was refused for concentration, the plan ran with FIVE targets instead
    # of backfilling with the seventh-ranked coin, which is not what this
    # docstring promised and not what the measured policy does. A refused
    # pick must be REPLACED, or a block quietly narrows the spread that the
    # measurement said six coins' worth of spread was carrying.
    #
    # The share each target will receive is not known until the set is
    # fixed, and the ceiling test needs it. So the walk uses the pool split
    # the MOST-FAVOURABLE way - over a full top_n - which is the smallest
    # per-target share, and therefore the least likely to breach. Anything
    # that still breaches at that share is re-checked for real below, once
    # the true share is known.
    targets, blocked, refused_early = [], [], []
    for pid, dip in ranked:
        if len(targets) >= top_n:
            break
        if pid not in by:
            blocked.append({"product_id": pid, "why": "no branch for this coin"})
            continue
        targets.append({"product_id": pid, "dip_depth": round(dip, 6),
                        "bot_name": by[pid].get("bot_name")})
    tset = {t["product_id"] for t in targets}

    sources, skipped = [], []
    for pid, b in by.items():
        if pid in tset:
            continue
        alloc = float(b.get("allocated_usd") or 0.0)
        n_open = len(_slices(b))
        if n_open == 0:
            free = max(0.0, alloc - KEEP_BRANCH_ALIVE_USD)
            why = (f"flat - no open slices, keeping ${KEEP_BRANCH_ALIVE_USD:,.2f} "
                   f"so the branch is not deleted")
        else:
            if not release_deployed_idle:
                skipped.append({"product_id": pid, "idle_usd": None,
                                "why": ("holds open slices; withdraw refuses a "
                                        "non-flat branch and the release switch "
                                        "is off")})
                continue
            dep = _deployed(b)
            if dep is None:
                skipped.append({"product_id": pid, "why": (
                    "an open rung carries no price, so this branch's deployed "
                    "cost is UNKNOWN and nothing is released from it")})
                continue
            free = max(0.0, alloc - dep)
            why = f"released down to its ${dep:,.2f} of deployed cost"
        if free < MIN_TRANSFER_USD:
            skipped.append({"product_id": pid, "idle_usd": round(free, 2),
                            "why": f"under the ${MIN_TRANSFER_USD:,.2f} minimum"})
            continue
        sources.append({"product_id": pid, "bot_name": b.get("bot_name"),
                        "release_usd": round(free, 2), "flat": n_open == 0,
                        "why": why})

    pool = round(sum(s["release_usd"] for s in sources), 2)
    if not targets or len(targets) < MIN_TARGETS or pool < MIN_TRANSFER_USD:
        return {"ok": False, "status": "NOTHING_TO_DO",
                "targets": targets, "sources": sources, "pool_usd": pool,
                "blocked": blocked or None, "skipped": skipped or None,
                "detail": (
                    f"only {len(targets)} coin(s) could be ranked, under the "
                    f"{MIN_TARGETS} this needs" if len(targets) < MIN_TARGETS else
                    f"${pool:,.2f} of releasable idle is under the "
                    f"${MIN_TRANSFER_USD:,.2f} minimum - nothing moves")}

    # Walk the ranking and give each target an equal share the ceiling
    # allows; a refused target is replaced by the next one down, so a
    # block narrows the ranking rather than silently shrinking the spread.
    # Walk the WHOLE ranking, taking the first top_n the ceiling admits, so
    # a refusal is backfilled from further down instead of shrinking the set.
    def _admits(pid, share_usd):
        b = by.get(pid)
        if b is None:
            return None, "no branch for this coin"
        # A PARKED BRANCH CANNOT SPEND WHAT IT IS GIVEN.
        #
        # open slices >= num_levels means the buy gate's
        # `len(slices) < num_levels` is already false, so the branch cannot
        # open another rung at any price. The same dry run had this task
        # send $459 each to HBAR (5 slices / 3 levels) and NEAR (3/3) -
        # $918, a third of the whole rotation, into two branches that
        # physically could not use it. That is the identical mistake as
        # topping up a branch that is already sitting on idle, and it gets
        # refused here rather than explained afterwards.
        n_open = len(b.get("slices") or [])
        nl_now = int(b.get("num_levels") or 0) or 1
        if n_open >= nl_now:
            return None, (f"parked at {n_open} open slice(s) against {nl_now} "
                          f"level(s) - it cannot open a rung, so new budget "
                          f"would sit unused")
        have = float(b.get("allocated_usd") or 0.0)
        after = ((have + share_usd) / book_total * 100.0) if book_total else 0.0
        if after > MAX_COIN_SHARE_PCT:
            return None, (f"${have + share_usd:,.2f} would be {after:.1f}% of the "
                          f"${book_total:,.2f} book, over the "
                          f"{MAX_COIN_SHARE_PCT:.0f}% ceiling")
        nl = int(b.get("num_levels") or 0) or 1
        if (have + share_usd) / nl < MIN_SLICE_USD:
            return None, "the resulting slice would be unplaceable"
        return have, None

    adds, refused = [], []
    share = pool / max(1, top_n)
    for pid, dip in ranked:
        if len(adds) >= top_n:
            break
        if pid in {s["product_id"] for s in sources}:
            continue                       # never fund a coin out of itself
        have, why = _admits(pid, share)
        if why is not None:
            refused.append({"product_id": pid, "dip_depth": round(dip, 6),
                            "why": why})
            continue
        adds.append({"product_id": pid, "dip_depth": round(dip, 6),
                     "bot_name": by[pid].get("bot_name"),
                     "add_usd": round(share, 2),
                     "allocated_before": round(have, 2)})

    if len(adds) < MIN_TARGETS:
        return {"ok": False, "status": "REFUSED_CEILING",
                "targets": targets, "sources": sources, "pool_usd": pool,
                "refused": refused or None, "blocked": blocked or None,
                "skipped": skipped or None,
                "detail": (f"only {len(adds)} target(s) survived the "
                           f"{MAX_COIN_SHARE_PCT:.0f}% ceiling, under the "
                           f"{MIN_TARGETS} this needs - nothing moves")}

    # Re-share across the survivors so the pool is fully placed, then make
    # the cents balance exactly on the last one.
    # Fewer survivors means a BIGGER share each, which can breach a ceiling
    # that the smaller trial share cleared. Re-check at the real share and
    # drop anyone who no longer fits, repeating until it is stable.
    while adds:
        share = pool / len(adds)
        over = [a for a in adds if _admits(a["product_id"], share)[1] is not None]
        if not over:
            break
        for a in over:
            refused.append({**a, "why": _admits(a["product_id"], share)[1]})
            adds.remove(a)
    if len(adds) < MIN_TARGETS:
        return {"ok": False, "status": "REFUSED_CEILING",
                "sources": sources, "pool_usd": pool, "refused": refused or None,
                "blocked": blocked or None, "skipped": skipped or None,
                "detail": (f"only {len(adds)} target(s) fit under the "
                           f"{MAX_COIN_SHARE_PCT:.0f}% ceiling at the real share "
                           f"- nothing moves")}
    share = pool / len(adds)
    for a in adds:
        a["add_usd"] = round(share, 2)
    drift = round(pool - sum(a["add_usd"] for a in adds), 2)
    if adds:
        adds[-1]["add_usd"] = round(adds[-1]["add_usd"] + drift, 2)

    return {"ok": True, "status": "READY",
            "targets": adds, "sources": sources, "pool_usd": pool,
            "targets_considered": len(ranked), "refused": refused or None, "blocked": blocked or None,
            "skipped": skipped or None,
            "balances": abs(sum(a["add_usd"] for a in adds) - pool) <= CENT,
            "places_no_order": True, "sells_no_coin": True,
            "detail": (f"${pool:,.2f} would move from {len(sources)} branch(es) "
                       f"into {len(adds)}: "
                       + ", ".join(f"{a['product_id']} +${a['add_usd']:,.2f}"
                                   for a in adds))}


async def apply(grid, p):
    """Execute a READY plan. Refuses an unbalanced one outright."""
    if not p or not p.get("ok"):
        return {"status": "NOT_APPLICABLE", "rows_written": 0,
                "detail": "the plan was not READY, so nothing was written"}
    took = round(sum(s["release_usd"] for s in p["sources"]), 2)
    gave = round(sum(a["add_usd"] for a in p["targets"]), 2)
    if abs(took - gave) > CENT:
        log.warning(f"[rotation] REFUSED: ${took:,.2f} out against ${gave:,.2f} in")
        return {"status": "REFUSED_UNBALANCED", "rows_written": 0,
                "withdrawn_usd": took, "added_usd": gave,
                "detail": (f"the plan would take ${took:,.2f} and place "
                           f"${gave:,.2f}. A rotation that does not balance "
                           f"mints or deletes capital, so nothing was written.")}

    withdrew, added, failed = [], [], []
    for s in p["sources"]:
        try:
            await grid.withdraw_from_grid_branch(s["bot_name"], s["release_usd"])
            withdrew.append(s)
            log.warning(f"[rotation] released ${s['release_usd']:,.2f} from "
                        f"{s['product_id']} ({s['why']})")
        except Exception as e:
            failed.append({**s, "error": f"{type(e).__name__}: {e}"})
            log.warning(f"[rotation] could not release from {s['product_id']}: "
                        f"{type(e).__name__}: {e}")

    # PLACE ONLY WHAT WAS ACTUALLY TAKEN. If a withdrawal was refused, the
    # money behind it does not exist to be placed, and adding it anyway
    # would invent budget out of a failed call.
    real_pool = round(sum(s["release_usd"] for s in withdrew), 2)
    if real_pool < MIN_TRANSFER_USD:
        return {"status": "NOTHING_RELEASED", "rows_written": 0,
                "failed_sources": failed or None,
                "detail": (f"only ${real_pool:,.2f} could actually be released, "
                           f"under the ${MIN_TRANSFER_USD:,.2f} minimum. Nothing "
                           f"was placed, so nothing is stranded.")}
    share = real_pool / len(p["targets"])
    for i, a in enumerate(p["targets"]):
        amt = round(share, 2)
        if i == len(p["targets"]) - 1:
            amt = round(real_pool - sum(x["added_usd"] for x in added), 2)
        if amt < CENT:
            continue
        try:
            await grid.add_cash_to_grid_branch(a["bot_name"], amt)
            added.append({**a, "added_usd": amt})
            log.warning(f"[rotation] placed ${amt:,.2f} into {a['product_id']}")
        except Exception as e:
            failed.append({**a, "error": f"{type(e).__name__}: {e}"})
            log.warning(f"[rotation] could not place into {a['product_id']}: "
                        f"{type(e).__name__}: {e}")

    out_usd = round(sum(s["release_usd"] for s in withdrew), 2)
    in_usd = round(sum(a["added_usd"] for a in added), 2)
    stranded = round(out_usd - in_usd, 2)
    for a in added:
        try:
            await grid._log_activity_safe(
                None, a["product_id"], "ROTATION",
                f"Received ${a['added_usd']:,.2f} of idle allocation, rotated in "
                f"on a dip_depth of {a['dip_depth']:.4f}. No order was placed and "
                f"no coin was sold.")
        except Exception:
            pass
    log.warning(f"[rotation] COMMITTED out ${out_usd:,.2f} / in ${in_usd:,.2f}, "
                f"stranded ${stranded:,.2f}")
    return {"status": "APPLIED" if added else "NOTHING_PLACED",
            "withdrawn": withdrew, "added": added,
            "failed": failed or None,
            "withdrawn_usd": out_usd, "added_usd": in_usd,
            "stranded_usd": stranded,
            "rows_written": len(withdrew) + len(added),
            "detail": (
                f"${in_usd:,.2f} moved into {len(added)} branch(es) from "
                f"{len(withdrew)}."
                + (f" ${stranded:,.2f} was released but could not be placed and "
                   f"is sitting as free cash." if abs(stranded) > CENT else "")
                if added else
                "NOTHING WAS PLACED. The books are unchanged except for any "
                "release that succeeded - see stranded_usd.")}


async def _claim_attempt(session_factory, key):
    from sqlalchemy import select

    from models import TradingBotState
    async with session_factory()() as db:
        row = (await db.execute(select(TradingBotState).where(
            TradingBotState.bot_name == key))).scalars().first()
        if row is None:
            db.add(TradingBotState(bot_name=key, base_capital=1.0))
            await db.commit()
            return True, 1.0
        used = float(row.base_capital or 0.0)
        if used == DONE or used >= MAX_ATTEMPTS:
            return False, used
        row.base_capital = used + 1.0
        await db.commit()
        return True, used + 1.0


async def _mark_done(session_factory, key):
    from sqlalchemy import select

    from models import TradingBotState
    async with session_factory()() as db:
        row = (await db.execute(select(TradingBotState).where(
            TradingBotState.bot_name == key))).scalars().first()
        if row is not None:
            row.base_capital = DONE
            await db.commit()


async def run_at_boot(grid, tkt=None, ranker=None):
    global _LAST
    tkt = tkt if tkt is not None else ticket()
    if not tkt:
        out = {"ran": False, "reason": "NO_TICKET",
               "detail": f"{TICKET_ENV} is not set - this file is inert."}
        _LAST = out
        return out
    if (os.getenv("STOP_TRADING", "false") or "").strip().lower() == "true":
        out = {"ran": False, "reason": "STOP_TRADING", "ticket": tkt,
               "detail": "STOP_TRADING is set - allocation writes are paused."}
        _LAST = out
        return out
    if grid is None:
        out = {"ran": False, "reason": "NO_GRID_MODULE", "ticket": tkt}
        _LAST = out
        return out

    key = MARKER_PREFIX + tkt
    try:
        may, used = await _claim_attempt(grid.get_session_factory, key)
    except Exception as e:
        out = {"ran": False, "reason": "MARKER_UNREADABLE", "ticket": tkt,
               "detail": (f"the one-shot marker could not be used "
                          f"({type(e).__name__}: {e}), so nothing was run.")}
        _LAST = out
        return out
    if not may:
        out = {"ran": False, "ticket": tkt,
               "reason": "ALREADY_DONE" if used == DONE else "ATTEMPTS_EXHAUSTED",
               "detail": ("this ticket already completed."
                          if used == DONE else
                          f"this ticket spent its {MAX_ATTEMPTS} attempts.")}
        _LAST = out
        return out

    released = release_armed()
    log.warning(f"[rotation] RUNNING ticket={tkt!r} attempt {used:.0f}, "
                f"deployed-idle release {'ARMED' if released else 'off'}")
    out = {"ran": True, "ticket": tkt, "attempt": used,
           "release_deployed_idle": released}
    try:
        status = await grid.get_grid_status()
        branches = status.get("branches") or []
        pids = [str(b.get("product_id")) for b in branches if b.get("product_id")]
        ranked, unreadable = await ((ranker or rank_by_dip)(pids))
        out["unreadable_coins"] = unreadable or None
        out["ranking"] = [{"product_id": p, "dip_depth": round(d, 6)}
                          for p, d in ranked[:10]]
        p = plan(branches, ranked, release_deployed_idle=released)
        out["plan"] = p
        out["applied"] = await apply(grid, p) if p.get("ok") else {
            "status": "NOT_APPLICABLE", "rows_written": 0,
            "detail": p.get("detail")}
    except Exception as e:
        out["applied"] = {"status": "FAILED", "rows_written": 0,
                          "error": f"{type(e).__name__}: {e}"}
        log.warning(f"[rotation] FAILED {type(e).__name__}: {e}")

    ap = out.get("applied") or {}
    out["rows_written_total"] = ap.get("rows_written") or 0
    settled = ap.get("status") in {"APPLIED", "NOTHING_TO_DO", "NOT_APPLICABLE"}
    if settled:
        try:
            await _mark_done(grid.get_session_factory, key)
            out["marked_done"] = True
        except Exception as e:
            out["marked_done"] = False
            out["mark_error"] = f"{type(e).__name__}: {e}"
    else:
        out["marked_done"] = False
    out["detail"] = ap.get("detail") or "nothing was written"
    log.warning(f"[rotation] {'SETTLED' if out.get('marked_done') else 'NOT SETTLED'} "
                f"{out['detail']}")
    _LAST = out
    return out


# ---------------------------------------------------------------------------
# API-TRIGGERED ROTATION
#
# run_at_boot() above arms from ROTATION_TASK_TICKET, an environment
# variable. On 2026-10-03 that proved unusable: four separate mechanisms -
# the Railway Deploy button, Restart, `railway variables --set` and
# `railway redeploy` - all failed to restart the service, while every
# GitHub push restarted it first time. A rotation that can only be armed by
# a deploy is a rotation that cannot be armed at all on that account.
#
# These helpers let the dashboard arm it directly instead. Same planner,
# same conservation check, same one-shot guarantee - a different trigger.
# The environment path is left exactly as it is; nothing here changes it.
# ---------------------------------------------------------------------------

API_MARKER_PREFIX = "rotation_api:"
INFLIGHT = 1.0


async def claim_once(session_factory, key):
    """Atomically reserve a one-shot ticket. Returns (ok, state).

    THE RESERVATION IS THE INSERT, not a read followed by a write.
    _claim_attempt above reads the row, decides, then writes - three
    statements with a gap between them, so two simultaneous requests can
    both read "unused" and both proceed. That is survivable at boot, where
    exactly one process runs the task once, and not survivable on an HTTP
    endpoint anyone can call twice.

    trading_bot_state.bot_name carries a UNIQUE constraint, so the database
    itself arbitrates: of two concurrent INSERTs for the same key exactly
    one commits and the other raises. The winner holds the ticket. No
    application-level check can be raced because no application-level check
    is what decides.

    A ticket left INFLIGHT by a crash stays unusable, and that is
    deliberate - the safe direction is refusing a possible double-spend
    rather than risking one. Use a new ticket name. A run that wrote
    NOTHING releases its own ticket (see finish_claim) so an honest
    no-op can be retried under the same name.
    """
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy import insert, select

    from models import TradingBotState
    async with session_factory()() as db:
        try:
            await db.execute(insert(TradingBotState).values(
                bot_name=key, base_capital=INFLIGHT))
            await db.commit()
            return True, "CLAIMED"
        except IntegrityError:
            await db.rollback()
        row = (await db.execute(select(TradingBotState).where(
            TradingBotState.bot_name == key))).scalars().first()
        if row is None:
            # Deleted between the failed insert and this read. Refuse rather
            # than loop: UNKNOWN is a third verdict and a retry is free.
            return False, "RACED"
        state = float(row.base_capital or 0.0)
        if state == DONE:
            return False, "ALREADY_DONE"
        return False, "IN_FLIGHT"


async def finish_claim(session_factory, key, wrote_rows: int):
    """Settle a claimed ticket: spent if it moved anything, freed if not.

    A ticket that moved money is spent forever - that is the whole point of
    the one-shot. A ticket whose run wrote zero rows moved nothing, so
    holding it would burn a name for a no-op; it is deleted and can be used
    again. The distinction is the row count the writer actually reported,
    never an assumption about why it was zero.
    """
    from sqlalchemy import delete, select

    from models import TradingBotState
    async with session_factory()() as db:
        if wrote_rows > 0:
            row = (await db.execute(select(TradingBotState).where(
                TradingBotState.bot_name == key))).scalars().first()
            if row is not None:
                row.base_capital = DONE
                await db.commit()
            return "DONE"
        await db.execute(delete(TradingBotState).where(
            TradingBotState.bot_name == key))
        await db.commit()
        return "RELEASED"


async def build_plan(grid, ranker=None, release_deployed_idle=None):
    """The proposed rotation, computed from live state. Writes nothing.

    Shared by the preview and the execute endpoints so the thing previewed
    and the thing executed are produced by one function. Execute recomputes
    rather than trusting a plan posted back to it - prices and slices move,
    and a plan is only honest about the moment it was built.
    """
    status = await grid.get_grid_status()
    branches = status.get("branches") or []
    pids = [str(b.get("product_id")) for b in branches if b.get("product_id")]
    ranked, unreadable = await ((ranker or rank_by_dip)(pids))
    released = release_armed() if release_deployed_idle is None else bool(release_deployed_idle)
    p = plan(branches, ranked, release_deployed_idle=released)
    p["unreadable_coins"] = unreadable or None
    p["release_deployed_idle"] = released
    p["ranking"] = [{"product_id": q, "dip_depth": round(d, 6)} for q, d in ranked[:10]]
    return p

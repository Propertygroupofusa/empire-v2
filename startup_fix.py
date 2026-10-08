"""Two bookkeeping writes, applied once from inside the process at boot.

WHY THIS EXISTS, AND WHY IT IS NOT A FEATURE.

Two changes have been measured, planned and agreed for days and have never
reached the database:

  1. XRP-USD 3 -> 10 levels and LINK-USD 3 -> 6 levels. Both branches are
     PARKED (open slices >= num_levels), so they cannot buy another rung at
     any price. Planned against live data 2026-10-03: XRP $1,495.21 unspent
     against a $225.62 slice, 3 rungs it could then open; LINK $46.82
     against $22.98, 2 rungs. Five rungs in total.
  2. The slice reconcile. Other subsystems (the concentration trimmer, a
     resting stop) sold coin without going through the grid, so branches
     still claim units the wallet no longer holds. A sale of those slices
     would be an order for coin that does not exist.

Both are reachable only through a write-guarded POST. The dashboard's
buttons for them record NOTHING server-side when tapped - not a write
attempt, not even the GET beacon placed as the first statement of each
handler - across a cleared cache, an incognito window and three deploys.
The failure is upstream of anything this process can see, and the owner has
been asked to tap those buttons more times than is reasonable. So this
stops needing a browser: the same two writes, run in-process at startup,
where no page, no fetch and no token is involved.

IT IS SHIPPED INERT. Nothing happens unless STARTUP_FIX_TICKET is set in
the environment, which only the account owner can do. Deploying this file
changes no behaviour by itself. That is deliberate: code that writes to the
book on boot must be armed by the person who owns the book, not by a merge.

WHAT IT WRITES: num_levels on two named branches, and slice qty / slice
rows for branches claiming coin the wallet does not hold. Nothing else.

WHAT IT NEVER DOES: place an order, move cash, convert USD, touch spacing,
reference prices, stops, allocations, breakers, maker-only settings, or any
threshold. It cannot raise a level count below a branch's open slices. It
writes off only coin that is NOT OWNED - held_including_zero is total owned
units, so staked SOL and locked LINK survive, which reconciling against
AVAILABLE units would not.

FAIL CLOSED, EVERY BRANCH OF IT:
  * no ticket                      -> does nothing
  * ticket already completed       -> does nothing
  * MAX_ATTEMPTS boots used        -> does nothing, ever again on that ticket
  * STOP_TRADING set               -> does nothing
  * wallet unreadable              -> no reconcile (a gap is not a zero)
  * unfiltered wallet map absent   -> no reconcile (the dust-filtered map
                                      cannot answer "is this coin owned")
  * write-off over MAX_WRITEOFF_USD-> no reconcile (that size means the
                                      reading is wrong, which is a
                                      measurement to check, not an
                                      instruction to obey)

WHAT THE FIRST ARMED RUN ACTUALLY DID, 2026-10-03. The reconcile landed:
twelve branches written at 04:49:38Z, $921.25 of phantom cost basis
cleared, and ACH's and QNT's refusal loops - 24 refused sell cycles each
per twenty minutes, every cycle asking to sell coin the wallet did not
hold - stopped within seconds (ACH's last refusal 04:49:28Z, QNT's
04:49:17Z, none since).

The levels did not. XRP and LINK still read 3/3, and the run marked itself
done anyway. Two faults, both fixed here:

  * The levels step runs first, milliseconds after boot, and read a grid
    status that did not yet contain the fleet. An empty or incomplete
    branch list is UNKNOWN. status_with() now waits for the branches the
    request names before anything is planned.
  * "Wrote nothing" was treated as "finished". It is not. A step is
    SETTLED only when it wrote something, or when re-running could not do
    better (levels already at target, nothing left to reconcile). An
    unsettled step leaves the ticket unspent for the next boot.

And the reason the first run's own report could not be read afterwards is
that it lived in memory and the process restarted. The reconcile could be
confirmed only because it writes to the activity log; the levels step
wrote nothing there, so why it wrote nothing was unrecoverable. Both
outcomes now write a durable activity line, success and refusal alike.

ATTEMPTS ARE COUNTED BEFORE THE WORK, NOT AFTER. A hard crash mid-run still
spends an attempt, so a crash loop cannot re-run this forever. Both
operations are idempotent - a level already set reports NO_CHANGE, and a
reconciled branch has nothing left to write off - so a retry after a
transient failure is safe.

THE REPORT IS KEPT IN MEMORY and served by GET /startup-fix, because the
owner has no terminal and the whole reason this file exists is that
browser-side instruments came back empty. A run that writes nothing says so
in those words; this must never print a success for a write that did not
happen.

DUPLICATION IS DELIBERATE. The apply logic mirrors
routers/trading_dashboard.py's set-levels and reconcile-slices endpoints
rather than refactoring them, so that the live endpoints are not touched by
this change. The two guards that matter - never a level below open slices,
never a write-off against an unreadable or dust-filtered wallet - are
pinned by tests on BOTH paths. If one side changes, change the other.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

TICKET_ENV = "STARTUP_FIX_TICKET"

# THE RIGHT-SIZE IS ARMED SEPARATELY, ON PURPOSE.
#
# 2026-10-08 taught this the hard way. The ticket was armed to run the
# reconcile, and a level change that had been sitting in LEVELS from an
# older plan rode along with it and lowered LINK-USD from 10 rungs to 6 -
# a change the account owner had not asked for and, with the set-levels
# button unreachable, could not undo from the dashboard.
#
# So a SECOND money operation does not inherit the first one's consent.
# The right-size lowers budget claim across several branches; it gets its
# own switch, and the ticket alone will not run it. Unset, the step
# reports NOT_ARMED and settles, so it changes nothing whatever about a
# ticket armed only for the reconcile.
RIGHTSIZE_ENV = "STARTUP_FIX_RIGHTSIZE"

# THE FLAT-BRANCH WITHDRAW, ARMED ON ITS OWN. A third money step on one
# ticket is exactly how the stale LINK level change rode along with the
# reconcile on 2026-10-08, so it gets its own switch like the right-size
# did and inherits nobody's consent.
FLAT_WITHDRAW_ENV = "STARTUP_FIX_FLAT_WITHDRAW"
MARKER_PREFIX = "startup_fix:"

# How many boots a single ticket may spend trying. Counted before the work.
MAX_ATTEMPTS = 5

# A COLD STATUS IS NOT AN ANSWER.
#
# The first armed run wrote the reconcile (12 branches, $921.25 of phantom
# cost basis, 2026-10-03T04:49:38Z) and wrote NO levels, then marked itself
# done - so the level change was lost and the ticket was spent. The levels
# step runs first, milliseconds after boot, and asked get_grid_status()
# before the fleet was there to be read; the reconcile asked again seconds
# later and got twenty-three branches. An empty or incomplete branch list is
# UNKNOWN, and a step that acted on one must not be allowed to call itself
# finished. So: wait for the branches the request actually names, and if
# they never appear, leave the ticket unspent for the next boot.
READY_TRIES = 20
READY_SLEEP_SECONDS = 15.0
# Sentinel stored in the marker row once a run completed without crashing.
DONE = -1.0

# The level changes, named here so they are in the diff and reviewable,
# never read from the environment. Measured parked on 2026-10-03.
#
# EMPTIED 2026-10-08, at the account owner's instruction. Both entries had
# already been overtaken by the fleet: XRP-USD was at 10 levels, so writing
# 10 was a no-op, and LINK-USD had been raised to 10, so writing 6 would
# have LOWERED it - removing four empty rungs the owner never asked to
# give up, and leaving it parked at 6 slices against 6 levels. Worse, the
# set-levels button is unreachable in the same way the reconcile button is
# (see this module's docstring), so that reduction could not have been
# undone from the dashboard afterwards.
#
# The ticket now does ONE thing: the slice reconcile. A level change that
# is wanted later belongs in a diff of its own, measured against the fleet
# as it is on that day, not carried along inside an unrelated fix.
LEVELS = {}

# A plan larger than this means the wallet reading collapsed, not that the
# fleet over-claims by that much. The audited plan was $1,157.41.
MAX_WRITEOFF_USD = 2000.0

# The same kind of bound for the right-size. Freeing budget places no order
# and sells nothing, but a plan far larger than the fleet means a reading
# went wrong, and a reading that went wrong is a measurement to check, not
# an instruction to obey. The plan measured 2026-10-08 16:16Z was $961.97.
MAX_RIGHTSIZE_USD = 3000.0

# Ceiling for the flat-branch withdraw. ZEC-USD needs $1,752.76; anything
# far past that is a fleet this code has not seen and should stop, not
# improvise.
MAX_FLAT_WITHDRAW_USD = 4000.0

# The last run's report, for GET /startup-fix.
_LAST = None


def last_report():
    """What the boot run did, or None if it has not run in this process."""
    return _LAST


def ticket():
    raw = (os.getenv(TICKET_ENV) or "").strip().strip('"').strip("'")
    return raw or None


def rightsize_armed():
    """True only for an explicit affirmative. Anything else is off.

    Quotes are stripped for the same reason ticket() strips them: a value
    pasted with them is the bug class that once stopped a whole module
    from importing and took the fleet off the air.
    """
    raw = (os.getenv(RIGHTSIZE_ENV) or "").strip().strip('"').strip("'").lower()
    return raw in ("1", "true", "yes", "on")

def flat_withdraw_target():
    """The ONE branch to free, by name, or None.

    THIS CARRIES A BRANCH NAME, NOT A YES. The first version of this step
    took a boolean and swept every flat branch above the floor. A dry
    replay against the live 2026-10-08 20:42Z fleet showed what that
    means: it would have taken ZEC-USD's $1,752.76 - correct, the branch
    is dead - AND stripped ONDO-USD from $86.02 to $15.00, a working
    branch with 9 closes that happened to be flat because it had sold two
    hours earlier.

    Nothing in the payload tells those two apart. out_of_reach reports
    ONDO, TIA and ZEC all "confirmed_zero", because a flat branch holds no
    coin BY DEFINITION - that signal says "flat", not "dead". The real
    difference is history, and a rule that decides on its own which
    branches are dead is exactly the kind of sweep that orphaned FLOKI and
    moved LINK's rungs unasked.

    So the owner names the branch. Blast radius: one row he chose.
    """
    raw = (os.getenv(FLAT_WITHDRAW_ENV) or "").strip().strip('"').strip("'")
    # A bare yes is NOT a target. It used to arm a fleet-wide sweep, so it
    # must refuse loudly rather than quietly mean something new.
    if raw.lower() in ("", "1", "true", "yes", "on", "0", "false", "no", "off"):
        return None
    return raw


async def _claim_attempt(session_factory, key):
    """Spend one attempt on this ticket. Returns (may_run, attempts_used).

    The increment is committed BEFORE any work, so a crash still costs an
    attempt and cannot produce an unbounded retry loop.
    """
    from sqlalchemy import select

    from models import TradingBotState
    async with session_factory()() as db:
        row = (await db.execute(select(TradingBotState).where(
            TradingBotState.bot_name == key))).scalars().first()
        if row is None:
            db.add(TradingBotState(bot_name=key, base_capital=1.0))
            await db.commit()
            return True, 1
        used = float(row.base_capital or 0.0)
        if used == DONE:
            return False, DONE
        if used >= MAX_ATTEMPTS:
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


async def status_with(grid, product_ids, tries=None, sleep_seconds=None):
    """A grid status that actually contains the named branches.

    Returns (status, ready). `ready` False means the branches never turned
    up inside the window - UNKNOWN, and the caller must write nothing and
    must not mark the ticket done.

    The two limits are read from the module here rather than bound as
    argument defaults, so changing them changes this function's behaviour.
    A default captured at definition time silently ignores the change.
    """
    import asyncio
    tries = READY_TRIES if tries is None else tries
    sleep_seconds = READY_SLEEP_SECONDS if sleep_seconds is None else sleep_seconds
    want = {str(p) for p in (product_ids or ())}
    status, seen = {}, set()
    for n in range(1, int(tries) + 1):
        status = await grid.get_grid_status() or {}
        branches = status.get("branches") or []
        seen = {str(b.get("product_id")) for b in branches if isinstance(b, dict)}
        if want and want <= seen:
            if n > 1:
                log.warning(f"[startup-fix] the fleet was readable on attempt {n} "
                            f"({len(branches)} branch(es))")
            return status, True
        log.warning(f"[startup-fix] waiting for the fleet: attempt {n} of {tries} "
                    f"served {len(branches)} branch(es), missing "
                    f"{sorted(want - seen)}")
        if n < int(tries):
            await asyncio.sleep(float(sleep_seconds))
    log.warning(f"[startup-fix] the fleet never served {sorted(want - seen)} - "
                f"UNKNOWN, so nothing is written and the ticket stays unspent")
    return status, False


async def apply_levels(grid, wanted=None):
    """Write num_levels on the named branches. Places no order.

    Mirrors the set-levels endpoint, including its re-check: a count below
    a branch's own open slices is the one thing this must never write.
    """
    import branch_levels
    from sqlalchemy import select

    from models import CryptoGridBranch

    want = dict(wanted if wanted is not None else LEVELS)

    # NOTHING REQUESTED IS SETTLED, AND IT MUST NOT WAIT TO FIND THAT OUT.
    #
    # status_with() tests `want and want <= seen`, so an empty request can
    # never satisfy it: it would burn READY_TRIES x READY_SLEEP_SECONDS -
    # five minutes of boot - and then return UNKNOWN, which run_at_boot
    # reads as unsettled and which would leave the ticket unspent and
    # retrying on all five attempts. An empty request is not an unreadable
    # fleet; it is a request with nothing in it.
    if not want:
        log.warning("[startup-fix] levels: nothing requested - skipped, settled")
        return {"requested": {}, "applied": [], "settled": True,
                "not_applied": None, "refused": None, "missing": None,
                "plans": [], "rows_written": 0, "places_no_order": True,
                "status": "NOTHING_REQUESTED",
                "detail": ("no level change is requested by this ticket, so none was "
                           "planned and none was written. Settled: re-running could "
                           "not do better.")}

    status, ready = await status_with(grid, want.keys())
    if not ready:
        return {"requested": want, "rows_written": 0, "settled": False,
                "status": "UNKNOWN_FLEET_NOT_READABLE",
                "detail": ("the fleet never served the branches this asks about, so "
                           "no level was planned and none was written. UNKNOWN, not "
                           "a refusal - the ticket stays unspent for the next boot.")}
    report = branch_levels.plan_many(status.get("branches") or [], want)
    ready = [r for r in report["plans"] if r.get("ok")]
    log.warning(f"[startup-fix] levels planned ready={report.get('ready')} "
                f"refused={report.get('refused')} missing={report.get('missing')}")
    for r in report["plans"]:
        if not r.get("ok"):
            log.warning(f"[startup-fix] levels NOT APPLICABLE {r.get('product_id')}: "
                        f"{r.get('status')} - {r.get('detail')}")

    applied, not_applied = [], []
    if ready:
        async with grid.get_session_factory()() as db:
            for r in ready:
                row = (await db.execute(select(CryptoGridBranch).where(
                    CryptoGridBranch.bot_name == r["bot_name"]))).scalars().first()
                if row is None:
                    not_applied.append({"product_id": r["product_id"],
                                        "reason": f"no branch row named {r['bot_name']!r}"})
                    continue
                if r["levels_after"] < (r["open_slices"] or 0):
                    not_applied.append({
                        "product_id": r["product_id"],
                        "reason": (f"{r['levels_after']} levels is below its "
                                   f"{r['open_slices']} open slice(s)")})
                    continue
                row.num_levels = r["levels_after"]
                applied.append({"product_id": r["product_id"],
                                "bot_name": r["bot_name"],
                                "levels_before": r["levels_before"],
                                "levels_after": r["levels_after"],
                                "rungs_it_could_actually_open":
                                    r.get("rungs_it_could_actually_open")})
            await db.commit()

    # DURABLE, because an in-memory report dies with the process and the
    # first armed run's did exactly that - the only reason the reconcile
    # could be confirmed at all is that it writes to the activity log. The
    # levels step wrote nothing there, so WHY it wrote nothing could not be
    # recovered. Both outcomes are logged now, success and refusal alike.
    for a in applied:
        log.warning(f"[startup-fix] levels WROTE {a['product_id']} "
                    f"{a['levels_before']} -> {a['levels_after']}")
        await _activity(grid, a["product_id"], "LEVELS",
                        f"Raised the rung limit from {a['levels_before']} to "
                        f"{a['levels_after']}. It can open "
                        f"{a.get('rungs_it_could_actually_open')} more rung(s) on its "
                        f"own dips. No order was placed and no money moved.")
    for r in report["plans"]:
        if not r.get("ok"):
            await _activity(grid, r.get("product_id"), "LEVELS",
                            f"Rung limit NOT changed ({r.get('status')}): "
                            f"{r.get('detail')}")
    for r in not_applied:
        await _activity(grid, r.get("product_id"), "LEVELS",
                        f"Rung limit NOT written: {r.get('reason')}")

    # SETTLED means re-running could not do better. Zero writes with a
    # refusal standing is NOT settled: the whole point of the ticket is that
    # the change lands, and the run that quietly marked itself done on zero
    # level writes is why this distinction exists.
    all_no_change = bool(report["plans"]) and all(
        r.get("status") == "NO_CHANGE" for r in report["plans"])
    out = {"requested": want, "applied": applied,
           "settled": bool(applied) or all_no_change,
           "not_applied": not_applied or None,
           "refused": report.get("refused") or None,
           "missing": report.get("missing") or None,
           "plans": report.get("plans"),
           "rows_written": len(applied),
           "places_no_order": True}
    out["detail"] = (
        f"{len(applied)} branch(es) had num_levels written."
        if applied else
        "NOTHING WAS WRITTEN. No level plan was applicable - see plans for why.")
    log.warning(f"[startup-fix] levels {out['detail']}")
    return out


async def apply_rightsize(grid, max_free_usd=MAX_RIGHTSIZE_USD):
    """Free budget that PARKED branches cannot spend. In-process, no browser.

    WHY THIS EXISTS. The reconcile correctly deleted $839.48 of phantom coin
    basis on 2026-10-08 at 15:58:40Z, and in doing so converted it into
    phantom CASH claim: a branch's unspendable reserve is
    allocated_usd - coin_basis, so removing basis RAISES it. ZEC alone went
    from claiming $1,167.06 of cash it could not spend to claiming the full
    $1,789.11. Within the hour the fleet's reserves exceeded the wallet and
    free cash read negative. Writing off the coin without releasing the
    budget behind it is half an operation.

    The other half is the right-size, and it was reachable ONLY through the
    write-guarded POST - one caller, no in-process path, no script. The same
    dashboard that could not deliver the reconcile four times is the only
    way to reach it, which is not a way to reach it.

    IT PLACES NO ORDER AND SELLS NO COIN. allocated_usd comes down toward
    the branch's own coin cost basis; the account holds exactly the same
    coins and the same dollars afterwards.

    EVERY INVARIANT STAYS WHERE IT WAS TESTED. This delegates each branch to
    branch_rightsize.apply_one rather than writing rows itself, so the three
    guards that matter are re-derived inside apply_one's own transaction,
    against fresh rows, exactly as the endpoint gets them:
      the branch must still be PARKED - a slice that sold since the plan
        gives it a free rung, and a branch with a free rung will spend its
        budget, so the write refuses;
      allocated_usd never ends below the coin's cost basis;
      allocated_usd never ends below $15.00, the row-deletion floor.
    apply_one also carries peak_equity down with the allocation, because the
    drawdown breaker measures peak against allocated - without it a
    right-size would freeze the branch it just freed. That is its logic and
    it is not re-implemented here.

    A FLAT BRANCH IS NOT IN SCOPE and plan() already excludes it, pointing
    at withdraw_from_grid_branch instead. ZEC-USD is flat with $1,789.11
    allocated and this step will not touch it - that is a separate write
    with separate reasoning, and bundling two money operations into one
    ticket is how the LINK level change rode along unnoticed earlier today.
    """
    if not rightsize_armed():
        log.warning(f"[startup-fix] rightsize: {RIGHTSIZE_ENV} is not set - skipped")
        return {"status": "NOT_ARMED", "rows_written": 0, "settled": True,
                "freed_usd": 0.0, "planned_usd": 0.0,
                "detail": (f"{RIGHTSIZE_ENV} is not set, so no budget was freed. "
                           f"This step does not inherit the ticket's consent - it "
                           f"lowers budget claim across several branches and is "
                           f"armed on its own.")}

    import branch_rightsize

    plan = await branch_rightsize.plan(grid)
    rows = [b for b in (plan.get("branches") or [])
            if (b.get("freeable_usd") or 0) > 0 and b.get("bot_name")]
    if not rows:
        log.warning("[startup-fix] rightsize: nothing is parked above its floor")
        return {"status": "NOTHING_TO_DO", "rows_written": 0, "settled": True,
                "freed_usd": 0.0, "planned_usd": 0.0,
                "detail": ("no parked branch claims budget above its own coin "
                           "basis - nothing to free.")}

    planned = round(sum(b["freeable_usd"] for b in rows), 2)
    if planned > max_free_usd:
        log.warning(f"[startup-fix] rightsize REFUSED: ${planned:,.2f} planned is "
                    f"over the ${max_free_usd:,.2f} ceiling")
        return {"status": "REFUSED_OVER_CEILING", "rows_written": 0,
                "settled": False, "freed_usd": 0.0, "planned_usd": planned,
                "branch_count": len(rows),
                "detail": (f"the plan would free ${planned:,.2f}, over the "
                           f"${max_free_usd:,.2f} ceiling. A plan that size means a "
                           f"reading is wrong, which is a measurement to check, not "
                           f"an instruction to obey. Nothing was written.")}

    # Biggest first, so a refusal part-way through has already banked the
    # one that mattered most.
    rows.sort(key=lambda b: -b["freeable_usd"])
    applied, not_applied = [], []
    for b in rows:
        try:
            r = await branch_rightsize.apply_one(
                grid, b["bot_name"], amount_usd=None, dry_run=False,
                # The branch's own live mark, carried through the plan so
                # the write keeps the drawdown breaker reading the same
                # percentage it read before. None is honoured as None.
                price=b.get("current_price"))
        except Exception as e:
            not_applied.append({"product_id": b.get("product_id"),
                                "bot_name": b["bot_name"],
                                "reason": f"{type(e).__name__}: {e}"})
            log.warning(f"[startup-fix] rightsize {b['bot_name']} raised "
                        f"{type(e).__name__}: {e}")
            continue
        if r and r.get("ok"):
            applied.append(r)
        else:
            not_applied.append({"product_id": b.get("product_id"),
                                "bot_name": b["bot_name"],
                                "reason": (r or {}).get("reason") or "refused"})

    freed = round(sum(a.get("freed_usd") or 0.0 for a in applied), 2)
    log.warning(f"[startup-fix] rightsize COMMITTED {len(applied)} branch(es), "
                f"${freed:,.2f} freed ({len(not_applied)} not written)")
    for a in not_applied:
        await _activity(grid, a.get("product_id"), "RIGHTSIZE",
                        f"Budget NOT freed: {a.get('reason')}")

    # SETTLED means re-running could not do better. apply_one logs its own
    # activity line per branch, so a write is auditable without this report.
    return {"status": "APPLIED" if applied else "NOTHING_WRITTEN",
            "settled": bool(applied),
            "branch_count": len(rows),
            "applied": applied, "not_applied": not_applied or None,
            "planned_usd": planned, "freed_usd": freed,
            "rows_written": len(applied),
            "places_no_order": True, "sells_no_coin": True,
            "flat_branches_are_not_in_scope": True,
            "detail": (
                f"{len(applied)} branch(es) right-sized, ${freed:,.2f} of unspendable "
                f"budget released against ${planned:,.2f} planned. No order was placed "
                f"and no coin was sold."
                if applied else
                "NOTHING WAS WRITTEN. No parked branch could be right-sized - see "
                "not_applied for each reason.")}


async def _activity(grid, product_id, kind, message):
    """One durable line. Never raises into the caller."""
    try:
        await grid._log_activity_safe(None, product_id, kind, message)
    except Exception as e:
        log.warning(f"[startup-fix] could not log activity for {product_id}: "
                    f"{type(e).__name__}: {e}")


async def _default_census():
    import aiohttp

    import account_census
    async with aiohttp.ClientSession() as s:
        return await account_census.census(s, tracked_usd=0.0)


async def apply_reconcile(grid, census_fn=None, max_writeoff_usd=MAX_WRITEOFF_USD):
    """Bring tracked units down to what the wallet actually owns.

    Mirrors the reconcile-slices endpoint with one guard the endpoint does
    not have, because this runs unattended: there is NO dust-filtered
    fallback. If the unfiltered owned-units map is missing from the census
    reading, nothing is written. holdings cannot answer "does the account
    own X at all" - it drops anything under $0.50 - and an unattended write
    must not guess.
    """
    import slice_reconcile
    from sqlalchemy import select

    from models import CryptoGridSlice

    status = await grid.get_grid_status()
    census = await ((census_fn or _default_census)())
    if not isinstance(census, dict) or not census.get("available"):
        log.warning("[startup-fix] reconcile SKIPPED: the wallet holdings could not "
                    "be read. A gap is not a zero, and a zero here would delete "
                    "every slice on the fleet.")
        return {"status": "SKIPPED_WALLET_UNREADABLE", "rows_written": 0,
                "settled": False,
                "detail": ("the wallet holdings could not be read, so nothing was "
                           "written off. A gap is not a zero.")}

    unfiltered = census.get("held_including_zero")
    if not isinstance(unfiltered, dict) or not unfiltered:
        log.warning("[startup-fix] reconcile SKIPPED: the census reading carried no "
                    "unfiltered owned-units map, and the dust-filtered one cannot "
                    "answer whether a coin is owned at all.")
        return {"status": "SKIPPED_NO_UNFILTERED_WALLET_MAP", "rows_written": 0,
                "settled": False,
                "detail": ("this census reading carried no unfiltered owned-units "
                           "map. The dust-filtered map hides anything under $0.50, "
                           "so it cannot answer whether a coin is owned at all, and "
                           "an unattended write does not guess.")}
    wallet = {str(k).upper(): v for k, v in unfiltered.items()}

    branches, skipped = [], []
    for b in (status.get("branches") or []):
        pid = b.get("product_id")
        slices = b.get("slices") or []
        if not slices:
            continue
        held = wallet.get(str(pid).split("-")[0].upper())
        if held is None:
            skipped.append({"product_id": pid, "reason": "NOT_IN_WALLET_READING"})
            continue
        actions, rep = slice_reconcile.plan(slices, held, price=b.get("current_price"))
        if rep.get("status") != "READY":
            continue
        branches.append({"product_id": pid, "bot_name": b.get("bot_name"),
                         "actions": actions, **rep})

    planned = round(sum(x.get("cost_basis_removed_usd") or 0.0 for x in branches), 2)
    log.warning(f"[startup-fix] reconcile planned {len(branches)} branch(es), "
                f"${planned:,.2f} of tracked cost basis, "
                f"{len(skipped)} skipped as unreadable")
    if not branches:
        return {"status": "NOTHING_TO_DO", "rows_written": 0, "settled": True,
                "skipped": skipped or None,
                "detail": ("no branch claims more coin than the wallet owns - "
                           "nothing to reconcile")}
    if planned > max_writeoff_usd:
        log.warning(f"[startup-fix] reconcile REFUSED: ${planned:,.2f} planned is over "
                    f"the ${max_writeoff_usd:,.2f} ceiling")
        return {"status": "REFUSED_OVER_CEILING", "rows_written": 0,
                "settled": False,
                "cost_basis_planned_usd": planned,
                "branch_count": len(branches),
                "detail": (f"the plan would clear ${planned:,.2f} of tracked cost "
                           f"basis, over the ${max_writeoff_usd:,.2f} ceiling. A plan "
                           f"that size means the wallet reading is wrong, which is a "
                           f"measurement to check, not an instruction to obey. "
                           f"Nothing was written.")}

    applied, not_applied = [], []
    async with grid.get_session_factory()() as db:
        for br in branches:
            changed, unfound = 0, 0
            for a in br["actions"]:
                sid = a.get("slice_id")
                if sid is None:
                    unfound += 1
                    continue
                row = (await db.execute(select(CryptoGridSlice).where(
                    CryptoGridSlice.id == sid))).scalars().first()
                if row is None:
                    unfound += 1
                    continue
                if a["action"] == "REMOVE":
                    await db.delete(row)
                else:
                    row.qty = a["qty_after"]
                changed += 1
            if changed:
                applied.append({"product_id": br["product_id"],
                                "units_removed": br["units_removed"],
                                "cost_basis_removed_usd": br["cost_basis_removed_usd"],
                                "slice_rows_changed": changed,
                                "slice_rows_not_found": unfound or None})
            else:
                not_applied.append({
                    "product_id": br["product_id"],
                    "slice_rows_not_found": unfound,
                    "reason": ("not one of this branch's planned slice rows could be "
                               "found to write, so nothing was changed for it")})
        await db.commit()

    cleared = round(sum(a["cost_basis_removed_usd"] or 0.0 for a in applied), 2)
    rows = sum(a["slice_rows_changed"] for a in applied)
    for a in applied:
        await _activity(
            grid, a["product_id"], "RECONCILE",
            f"Wrote off {a['units_removed']:.8f} units another subsystem had "
            f"already sold - ${a['cost_basis_removed_usd']:,.2f} of tracked cost "
            f"basis. Not a loss: the proceeds were already in the wallet.")
    log.warning(f"[startup-fix] reconcile COMMITTED {len(applied)} branch(es), "
                f"{rows} slice row(s), ${cleared:,.2f} actually cleared "
                f"({len(not_applied)} could not be written)")
    return {"status": "APPLIED" if applied else "NOTHING_WRITTEN",
            "settled": bool(applied),
            "branch_count": len(branches),
            "applied": applied, "not_applied": not_applied or None,
            "skipped": skipped or None,
            "cost_basis_planned_usd": planned,
            # What was WRITTEN, never what was planned.
            "cost_basis_removed_usd": cleared,
            "rows_written": rows,
            "writes_off_only_unowned_coin": True,
            "locked_or_staked_coin_is_not_written_off": True,
            "detail": (
                f"{len(applied)} branch(es) corrected, {rows} slice row(s) written, "
                f"${cleared:,.2f} of tracked cost basis cleared." if applied else
                f"NOTHING WAS CHANGED. {len(branches)} branch(es) had a plan, but no "
                f"slice row could be found to write. The books are unchanged.")}


async def apply_flat_withdraw(grid, max_free_usd=MAX_FLAT_WITHDRAW_USD):
    """Free a FLAT branch's stranded claim. In-process, no browser.

    WHAT IS STRANDED, measured 2026-10-08 20:42Z. crypto_grid_21 (ZEC-USD)
    holds $1,767.76 of allocation against ZERO coin, ZERO open slices and
    zero completed closes in 39 days. Its trading balance is a confirmed
    zero and it refused 199 sell attempts in three hours. It is 26% of the
    grid's entire budget and it is attached to nothing. That is not a
    ranking judgement about a coin - the branch holds nothing at all.

    WHY IT WAS NOT ALREADY FREED. The right-size refuses a flat branch by
    design; branch_rightsize.plan names withdraw_from_grid_branch as "the
    tested path" instead. And withdraw_from_grid_branch had exactly one
    caller, behind the write-guarded POST - the dashboard Withdraw button,
    on a page whose write buttons do not reach the server from the owner's
    tab. Tested, correct, and unreachable. The right-size had the same
    shape until it was given an in-process path; this is that same fix.

    IT PLACES NO ORDER AND SELLS NO COIN. There is no coin to sell. Only
    allocated_usd comes down, and get_real_free_cash_usd() already
    subtracts every branch's allocation from the real wallet balance, so
    shrinking the claim IS what makes the cash spendable again.

    *** IT NEVER DELETES A BRANCH. *** withdraw_from_grid_branch deletes a
    branch it drains below a cent, and its own log says why that is
    dangerous: "Its coin is now owned by no branch: no grid rule will sell
    it, no breaker watches it, and any resting order against it stands
    unmanaged." FLOKI-USD proved it - deleted 2026-10-04, ~$43 of coin
    orphaned, sold by something on 10-07 in 8 fills with no buy side, and
    nothing reported it for three days. The owner's instruction is flat:
    "Don't delete any losing branches. Not none of them."

    So this withdraws DOWN TO the keep-alive floor and stops. allow_delete
    is never passed, and no amount is ever computed that would trip the
    floor. ZEC ends at $15.00, alive and watched, with $1,752.76 freed.

    THE FLOOR IS READ FROM THE ENGINE, never carried here.
    GRID_KEEP_BRANCH_ALIVE_USD is MIN_TRADE_USD * 3 and has moved before; a
    step holding its own copy of a money constant can disagree with the
    module it is calling, which is the whole failure this codebase keeps
    re-learning. Unreadable is NO_FLOOR and writes nothing.
    """
    target = flat_withdraw_target()
    if not target:
        log.warning(f"[startup-fix] flat-withdraw: {FLAT_WITHDRAW_ENV} names no branch - skipped")
        return {"status": "NOT_ARMED", "rows_written": 0, "settled": True,
                "freed_usd": 0.0, "planned_usd": 0.0, "not_applied": [], "target": None,
                "detail": (f"{FLAT_WITHDRAW_ENV} does not name a branch, so no claim was "
                           f"freed. It takes a bot_name (crypto_grid_21), not a yes: this "
                           f"step moves budget off a branch, it is armed on its own, and "
                           f"it acts on exactly the one row it is given.")}

    floor = getattr(grid, "GRID_KEEP_BRANCH_ALIVE_USD", None)
    if not isinstance(floor, (int, float)) or floor <= 0:
        log.warning("[startup-fix] flat-withdraw: keep-alive floor unreadable - refusing")
        return {"status": "NO_FLOOR", "rows_written": 0, "settled": False,
                "freed_usd": 0.0, "planned_usd": 0.0, "not_applied": [],
                "detail": ("GRID_KEEP_BRANCH_ALIVE_USD could not be read from the grid "
                           "module. Withdrawing against an assumed floor is how a branch "
                           "gets deleted by accident, so nothing was written.")}
    floor = float(floor)

    status = await grid.get_grid_status()
    plan = []
    for b in (status.get("branches") or []):
        name = b.get("bot_name")
        alloc = b.get("allocated_usd")
        if not name or not isinstance(alloc, (int, float)):
            continue
        if name != target:
            continue
        # FLAT only. An open slice is real coin bought against this
        # allocation; pulling the budget out from under it desyncs the
        # branch's books from what is genuinely deployed.
        if (b.get("open_slices") or 0) > 0:
            continue
        if b.get("locked"):
            continue
        amount = round(float(alloc) - floor, 2)
        if amount <= 0:
            continue          # at or under the floor - only a delete empties it
        plan.append({"bot_name": name, "product_id": b.get("product_id"),
                     "allocated_usd": float(alloc), "withdraw_usd": amount,
                     "leaves_usd": round(float(alloc) - amount, 2)})

    if not plan:
        log.warning(f"[startup-fix] flat-withdraw: {target} is not eligible")
        return {"status": "NOTHING_TO_DO", "rows_written": 0, "settled": True,
                "freed_usd": 0.0, "planned_usd": 0.0, "not_applied": [], "target": target,
                "detail": (f"{target} is not flat, unlocked and above the keep-alive "
                           f"floor - or no branch by that name exists - so nothing was "
                           f"freed. An open slice is real coin bought against that "
                           f"allocation and this step will not pull budget out from "
                           f"under one.")}

    planned = round(sum(p["withdraw_usd"] for p in plan), 2)
    if planned > max_free_usd:
        log.warning(f"[startup-fix] flat-withdraw: REFUSED - ${planned:,.2f} "
                    f"over the ${max_free_usd:,.2f} ceiling")
        return {"status": "REFUSED_OVER_CEILING", "rows_written": 0, "settled": False,
                "freed_usd": 0.0, "planned_usd": planned, "not_applied": [],
                "plan": plan,
                "detail": (f"the plan would free ${planned:,.2f}, over this step's "
                           f"${max_free_usd:,.2f} ceiling. Nothing was written. A fleet "
                           f"this far outside what the step was built for should stop, "
                           f"not improvise.")}

    plan.sort(key=lambda p: -p["withdraw_usd"])     # biggest first
    applied, not_applied, freed = [], [], 0.0
    for p in plan:
        try:
            # NO allow_delete. Not as a default, not as a flag, not ever.
            # The amount above already stops at the floor.
            res = await grid.withdraw_from_grid_branch(p["bot_name"], p["withdraw_usd"])
        except Exception as e:
            not_applied.append({**p, "error": f"{type(e).__name__}: {e}"})
            log.warning(f"[startup-fix] flat-withdraw {p['bot_name']} FAILED "
                        f"{type(e).__name__}: {e}")
            continue
        if not res or (isinstance(res, dict) and res.get("ok") is False):
            not_applied.append({**p, "error": (res or {}).get("reason") if isinstance(res, dict) else "refused"})
            continue
        applied.append(p)
        freed += p["withdraw_usd"]
        log.warning(f"[startup-fix] flat-withdraw: freed ${p['withdraw_usd']:,.2f} from "
                    f"{p['bot_name']} ({p['product_id']}) - allocation "
                    f"${p['allocated_usd']:,.2f} -> ${p['leaves_usd']:,.2f}, branch KEPT")
        try:
            await grid._log_activity_safe(
                "grid_fleet", p["product_id"], "FLAT_WITHDRAW",
                f"Freed ${p['withdraw_usd']:,.2f} of claim from a branch holding no coin "
                f"and no open slice. Allocation ${p['allocated_usd']:,.2f} -> "
                f"${p['leaves_usd']:,.2f}; the branch is KEPT alive at the keep-alive "
                f"floor, not deleted. No order placed, no coin sold - there was none.")
        except Exception:
            pass

    freed = round(freed, 2)
    return {
        "status": "APPLIED" if applied else "NOTHING_WRITTEN",
        "rows_written": len(applied),
        # Nothing written with a candidate standing means a retry could do
        # better, so the ticket must stay unspent.
        "settled": bool(applied),
        "freed_usd": freed, "planned_usd": planned,
        "applied": applied, "not_applied": not_applied,
        "floor_used": floor, "target": target,
        "detail": (f"freed ${freed:,.2f} of claim from {len(applied)} flat branch(es); "
                   f"every one was left standing at ${floor:,.2f}, none deleted."
                   if applied else
                   "every flat branch refused or raised, so nothing was freed."),
    }


async def run_at_boot(grid, tkt=None, census_fn=None):
    """The whole one-shot. Returns a report; never raises into the lifespan."""
    global _LAST
    tkt = tkt if tkt is not None else ticket()
    if not tkt:
        out = {"ran": False, "reason": "NO_TICKET",
               "detail": (f"{TICKET_ENV} is not set, so nothing was run. This file is "
                          f"inert until the account owner arms it.")}
        _LAST = out
        return out
    if (os.getenv("STOP_TRADING", "false") or "").strip().lower() == "true":
        out = {"ran": False, "reason": "STOP_TRADING", "ticket": tkt,
               "detail": "STOP_TRADING is set - configuration writes are paused."}
        _LAST = out
        log.warning("[startup-fix] SKIPPED: STOP_TRADING is set")
        return out
    if grid is None:
        out = {"ran": False, "reason": "NO_GRID_MODULE", "ticket": tkt,
               "detail": "the grid module is not available in this process."}
        _LAST = out
        return out

    key = MARKER_PREFIX + tkt
    try:
        may, used = await _claim_attempt(grid.get_session_factory, key)
    except Exception as e:
        out = {"ran": False, "reason": "MARKER_UNREADABLE", "ticket": tkt,
               "detail": (f"the one-shot marker could not be read or written "
                          f"({type(e).__name__}: {e}), so nothing was run. Without a "
                          f"marker this could not promise to run only once.")}
        _LAST = out
        log.warning(f"[startup-fix] SKIPPED: marker unusable - {type(e).__name__}: {e}")
        return out
    if not may:
        out = {"ran": False,
               "reason": "ALREADY_DONE" if used == DONE else "ATTEMPTS_EXHAUSTED",
               "ticket": tkt, "attempts_used": None if used == DONE else used,
               "detail": ("this ticket already completed, so nothing was run again."
                          if used == DONE else
                          f"this ticket has already spent its {MAX_ATTEMPTS} attempts "
                          f"without completing. Set a new {TICKET_ENV} to try again, "
                          f"after reading why the earlier attempts failed.")}
        _LAST = out
        log.warning(f"[startup-fix] SKIPPED: {out['reason']} ticket={tkt!r}")
        return out

    log.warning(f"[startup-fix] RUNNING ticket={tkt!r} attempt {used:.0f} of "
                f"{MAX_ATTEMPTS}")
    out = {"ran": True, "ticket": tkt, "attempt": used}
    try:
        out["levels"] = await apply_levels(grid)
    except Exception as e:
        out["levels"] = {"status": "FAILED", "rows_written": 0,
                         "error": f"{type(e).__name__}: {e}",
                         "detail": "the level write raised, so nothing is claimed for it."}
        log.warning(f"[startup-fix] levels FAILED {type(e).__name__}: {e}")
    try:
        out["reconcile"] = await apply_reconcile(grid, census_fn=census_fn)
    except Exception as e:
        out["reconcile"] = {"status": "FAILED", "rows_written": 0,
                            "error": f"{type(e).__name__}: {e}",
                            "detail": "the reconcile raised, so nothing is claimed for it."}
        log.warning(f"[startup-fix] reconcile FAILED {type(e).__name__}: {e}")
    # RIGHT-SIZE RUNS LAST, AND THAT ORDER IS THE POINT. The reconcile
    # changes coin basis, and a branch's floor IS its coin basis - so a
    # right-size planned before it would measure against a basis that is
    # about to change. apply_one re-derives the floor inside its own
    # transaction either way, but planning after the reconcile is the
    # honest order.
    try:
        out["rightsize"] = await apply_rightsize(grid)
    except Exception as e:
        out["rightsize"] = {"status": "FAILED", "rows_written": 0,
                            "error": f"{type(e).__name__}: {e}",
                            "detail": "the right-size raised, so nothing is claimed for it."}
        log.warning(f"[startup-fix] rightsize FAILED {type(e).__name__}: {e}")

    # FOURTH AND LAST, and after the right-size on purpose: a right-size can
    # hand a parked branch a free rung, which changes what "flat" means for
    # this step, so planning it earlier would measure a fleet about to move.
    try:
        out["flat_withdraw"] = await apply_flat_withdraw(grid)
    except Exception as e:
        out["flat_withdraw"] = {"status": "FAILED", "rows_written": 0,
                                "settled": False, "freed_usd": 0.0,
                                "error": f"{type(e).__name__}: {e}",
                                "detail": "the flat-withdraw raised, so nothing is claimed for it."}
        log.warning(f"[startup-fix] flat-withdraw FAILED {type(e).__name__}: {e}")

    failed = [k for k in ("levels", "reconcile", "rightsize", "flat_withdraw")
              if (out[k] or {}).get("status") == "FAILED"]
    unsettled = [k for k in ("levels", "reconcile", "rightsize", "flat_withdraw")
                 if not (out[k] or {}).get("settled")]
    out["failed_steps"] = failed or None
    out["unsettled_steps"] = unsettled or None
    lv = (out["levels"] or {}).get("rows_written") or 0
    rc = (out["reconcile"] or {}).get("rows_written") or 0
    rs = (out["rightsize"] or {}).get("rows_written") or 0
    fw = (out["flat_withdraw"] or {}).get("rows_written") or 0
    out["rows_written_total"] = lv + rc + rs + fw
    if failed or unsettled:
        why = []
        if failed:
            why.append(f"{', '.join(failed)} raised and wrote nothing")
        if unsettled:
            why.append(f"{', '.join(unsettled)} did not settle "
                       f"({'; '.join((out[k] or {}).get('status') or '?' for k in unsettled)})")
        out["detail"] = (
            f"{'; '.join(why)}. {out['rows_written_total']} row(s) were written in "
            f"total. The ticket is NOT marked done, so the next boot will try again; "
            f"{MAX_ATTEMPTS - int(used)} attempt(s) remain on it.")
        out["marked_done"] = False
    else:
        try:
            await _mark_done(grid.get_session_factory, key)
            out["marked_done"] = True
        except Exception as e:
            out["marked_done"] = False
            out["mark_error"] = f"{type(e).__name__}: {e}"
            log.warning(f"[startup-fix] could not mark done: {type(e).__name__}: {e}")
        out["detail"] = (
            f"{out['rows_written_total']} row(s) written in total "
            f"({lv} level row(s), {rc} slice row(s)). No order was placed."
            if out["rows_written_total"] else
            "NOTHING WAS WRITTEN. Both steps ran and both found nothing applicable - "
            "see levels and reconcile for exactly why.")
    log.warning(f"[startup-fix] "
                f"{'SETTLED' if out.get('marked_done') else 'NOT SETTLED'} "
                f"{out['detail']}")
    _LAST = out
    return out

"""Take the profit off redeployed money, instead of letting it ride.

THE OWNER'S INSTRUCTION, 2026-10-03: "Any money that you redeploy, make
sure you hurry up and take the profit off of them from now on."

Skim is off fleet-wide, so every winning sell compounds straight back into
its own branch's allocated_usd. That is the right default for a branch
building itself up, and it is the wrong one for money that was deliberately
moved somewhere to see whether it works: the gains never leave, so a good
week is indistinguishable from a flat one once the price turns.

This harvests. When a branch is FLAT it withdraws the profit it earned
since its baseline, leaving the capital to keep working.

WHY "SINCE ITS BASELINE" AND NOT "ABOVE SOME NUMBER". CryptoGridBranch has
exactly one money column, allocated_usd, and no record of what the branch
was originally funded with - five weeks of compounding and rotations are
already baked into it, and no amount of arithmetic recovers the split now.
So this never tries. It records, per branch, the cumulative realised P&L at
the moment it starts watching, and only ever harvests what is earned AFTER
that. Which is exactly what "from now on" means, and it makes the harvest
impossible to confuse with capital: every dollar it takes is a dollar that
appears in the trade ledger as a closed, realised, fee-adjusted win.

WHY FLAT ONLY. Not a policy - withdraw_from_grid_branch refuses any branch
holding an open slice, because that cash is already bought coin. So the
harvest happens at the moments a branch empties out, which is the only time
the money is cash at all.

WHERE IT GOES. Out of allocated_usd, which leaves it as unallocated cash in
the wallet. Taking profit off the table is the point.

AMENDED 2026-10-05, at the owner's instruction: "every branch once it fills,
I want the money to go into the weakest branch and help it build up." With
GRID_HARVEST_REDIRECT=true the harvested claim is handed to the branch that
can soonest turn it into a rung (see harvest_redirect) instead of stopping
as cash. This paragraph used to end "putting it straight back on would be
the thing being replaced", and that is no longer the standing instruction -
it is recorded here rather than deleted, because the reasoning behind it
still holds whenever the flag is off, which is the default.

The withdrawal and the deposit are the same amount and the deposit is the
only thing that can follow a successful withdrawal: if it fails the money
goes back and the baseline does not advance. No dollar is ever left
nowhere.

NOTHING HERE PLACES AN ORDER OR SELLS ANY COIN.
"""
import logging
import os

log = logging.getLogger(__name__)

BASELINE_PREFIX = "harvest_base:"

# Never leave a branch under this. withdraw_from_grid_branch deletes a row
# it drains below a cent, and a deleted branch takes its coin out of the
# fleet entirely - the same floor the idle rotation keeps.
KEEP_BRANCH_ALIVE_USD = 15.0

# The smallest profit worth taking off the table.
#
# MEASURED, not chosen. This was $10.00 for its first few hours, which was
# wrong by more than an order of magnitude: across the fleet's busiest week
# (2026-09-27 to 10-03, 113 closes, 109 of them winners, $116 of realised
# profit) NOT ONE closed trade cleared $10.00, and only 2 of 55
# branch-days did. A harvest with that floor would have sat at $0.00
# through the best run the system has ever had, which is the opposite of
# "hurry up and take the profit off".
#
# The real scale of a win on this fleet:
#   median winning trade      $0.68
#   median branch-day         $1.01
#   $0.50 is cleared by 61.5% of individual winners and 72.7% of
#   branch-days, so a typical single round trip is enough to bank.
#
# Not lower than this: winners run down to $0.01, and taking those would
# churn allocated_usd for pennies and bury the activity feed.
#
# THIS IS NOT A RISK LIMIT. It decides when realised profit moves from
# allocated_usd into cash. It places no order, sells no coin, and cannot
# reach capital. The floor that protects the branch is KEEP_BRANCH_ALIVE_USD
# above, which is unchanged at $15.00.
MIN_HARVEST_USD = 0.50


async def realised_by_branch(session_factory):
    """{bot_name: cumulative realised P&L} straight from the trade ledger.

    The ledger is the only honest source: allocated_usd mixes compounding,
    funding and rotations together and cannot say which dollars were won.
    """
    from sqlalchemy import select

    from models import CryptoGridTradeHistory
    out = {}
    async with session_factory()() as db:
        rows = (await db.execute(select(CryptoGridTradeHistory))).scalars().all()
        for r in rows:
            if not r.bot_name:
                continue
            out[r.bot_name] = out.get(r.bot_name, 0.0) + float(r.pnl or 0.0)
    return out


async def _baseline(session_factory, bot_name, realised_now, create=True):
    """This branch's realised-P&L mark. Creates it at the current total.

    A branch seen for the first time is marked where it stands and harvests
    nothing on that pass. Marking it at zero would treat five weeks of
    history as freshly earned profit and withdraw it on sight.
    """
    from sqlalchemy import select

    from models import TradingBotState
    key = BASELINE_PREFIX + bot_name
    async with session_factory()() as db:
        row = (await db.execute(select(TradingBotState).where(
            TradingBotState.bot_name == key))).scalars().first()
        if row is None:
            if not create:
                return None
            db.add(TradingBotState(bot_name=key, base_capital=realised_now))
            await db.commit()
            return realised_now
        return float(row.base_capital or 0.0)


async def _advance_baseline(session_factory, bot_name, to_value):
    from sqlalchemy import select

    from models import TradingBotState
    key = BASELINE_PREFIX + bot_name
    async with session_factory()() as db:
        row = (await db.execute(select(TradingBotState).where(
            TradingBotState.bot_name == key))).scalars().first()
        if row is not None:
            row.base_capital = to_value
            await db.commit()


def harvestable(branch, realised_now, baseline):
    """Dollars this branch could give back right now, and why not if zero.

    Three independent caps, all of which must hold:
      the branch is FLAT          - withdraw refuses anything else
      profit earned since baseline - never capital, only logged wins
      what is left stays >= $15    - a drained branch row is deleted

    A baseline of None means the harvest has never looked at this branch, so
    there is no mark to measure profit from. That is not zero profit - it is
    an unknown, and the only safe reading of an unknown here is to take
    nothing. The read-only preview passes None deliberately, so that a
    fleet of None baselines is legible as "the loop has not run yet"
    rather than silently reading as "nothing has been earned".
    """
    if baseline is None:
        return 0.0, ("no baseline yet - the harvest has not seen this branch, "
                     "so there is nothing to measure profit against")
    slices = branch.get("slices") or []
    if slices:
        return 0.0, f"holds {len(slices)} open slice(s); withdraw needs a flat branch"
    earned = round(realised_now - baseline, 2)
    if earned < MIN_HARVEST_USD:
        return 0.0, (f"${earned:,.2f} earned since baseline, under the "
                     f"${MIN_HARVEST_USD:,.2f} minimum")
    alloc = float(branch.get("allocated_usd") or 0.0)
    room = round(alloc - KEEP_BRANCH_ALIVE_USD, 2)
    if room < MIN_HARVEST_USD:
        return 0.0, (f"only ${room:,.2f} above the ${KEEP_BRANCH_ALIVE_USD:,.2f} "
                     f"keep-alive floor")
    return min(earned, room), None


async def plan(grid, create=True):
    """What a harvest would take, per branch.

    NOT read-only by default, and the previous docstring claiming otherwise
    was wrong: create=True records a baseline for any branch that does not
    have one, which is a write. That is correct for the hourly loop - a
    branch has to be marked before it can ever be harvested, and marking it
    is what makes the first sight take nothing.

    create=False is the honest read. It writes nothing, and reports
    baseline=None for any branch the loop has not marked yet, which is the
    only way from outside to tell "watched, nothing earned" apart from
    "never looked at". A live money-moving loop that cannot be observed
    read-only is indistinguishable from one that is not running.
    """
    status = await grid.get_grid_status()
    branches = status.get("branches") or []
    realised = await realised_by_branch(grid.get_session_factory)
    rows, total, unwatched = [], 0.0, 0
    for b in branches:
        bot = b.get("bot_name")
        if not bot:
            continue
        now = round(realised.get(bot, 0.0), 2)
        base = await _baseline(grid.get_session_factory, bot, now, create=create)
        take, why_not = harvestable(b, now, base)
        if base is None:
            unwatched += 1
        row = {"bot_name": bot, "product_id": b.get("product_id"),
               "allocated_usd": round(float(b.get("allocated_usd") or 0.0), 2),
               "realised_total": now,
               "baseline": None if base is None else round(base, 2),
               "earned_since_baseline": (None if base is None
                                         else round(now - base, 2)),
               "harvest_usd": round(take, 2), "why_not": why_not}
        rows.append(row)
        total += take
    rows.sort(key=lambda r: -r["harvest_usd"])
    # WHETHER THE MONEY-MOVING BEHAVIOURS ARE ARMED, on the read-only path.
    #
    # Both flags were shipped 2026-10-05 and switched on the same evening,
    # and NOTHING exposed their state: run() reports redirect_active, but
    # returns before that on dry_run, which is the only path the preview
    # endpoint uses. So the owner had turned on two behaviours that move
    # money and had no way to see that they were on.
    #
    # This module's own docstring already named that fault: "A live
    # money-moving loop that cannot be observed read-only is
    # indistinguishable from one that is not running." Reported here, where
    # the preview already looks.
    import coin_quality
    import harvest_redirect
    _rd, _cq = harvest_redirect.enabled(), coin_quality.enabled()
    return {"branches": rows, "total_harvest_usd": round(total, 2),
            "ready": round(total, 2) >= MIN_HARVEST_USD,
            "unwatched_branches": unwatched,
            "loop_has_run": bool(rows) and unwatched < len(rows),
            "redirect_active": _rd,
            "coin_quality_active": _cq,
            "max_coin_share_pct": round(coin_quality.MAX_COIN_SHARE * 100.0, 2),
            "armed_is": (
                f"{harvest_redirect.ENV_FLAG}="
                f"{'ON' if _rd else 'OFF'}: harvested profit "
                + ("goes to the branch that can soonest turn it into a rung, "
                   "never back to its own source."
                   if _rd else "stays as unallocated cash.")
                + f" {coin_quality.ENV_FLAG}={'ON' if _cq else 'OFF'}: the "
                + ("selector's four gates rank which branch that is, with a "
                   f"{coin_quality.MAX_COIN_SHARE * 100:.0f}% per-coin cap "
                   "applied BEFORE quality."
                   if _cq else "ranking falls back to distance-to-buy-line.")
                + " Reading this placed no order and moved no dollar.")}


async def run(grid, dry_run=True):
    """Harvest. dry_run=True (the default) computes and writes nothing."""
    if (os.getenv("STOP_TRADING", "false") or "").strip().lower() == "true":
        return {"ran": False, "reason": "STOP_TRADING"}

    p = await plan(grid)
    if dry_run:
        return dict(p, ran=False, dry_run=True,
                    detail=("PREVIEW ONLY - nothing was withdrawn. "
                            f"${p['total_harvest_usd']:,.2f} of realised profit "
                            f"is sitting in flat branches."))

    # WHERE THE MONEY GOES AFTER IT COMES OUT.
    #
    # Default: nowhere - it becomes unallocated cash, exactly as before.
    # With GRID_HARVEST_REDIRECT=true it is handed to the branch that can
    # soonest turn it into a rung. See harvest_redirect for why that is not
    # the same as "the weakest branch", and for the closed-loop bug the
    # family tree hit when it sent money back to its own source.
    import harvest_redirect
    _redirect = harvest_redirect.enabled()
    _fleet = None
    if _redirect:
        try:
            _status = await grid.get_grid_status()
            _all_branches = _status.get("branches") or []
            _fleet = _status.get("total_allocated_usd")
        except Exception as e:
            # Fail CLOSED on the redirect only: an unreadable fleet means
            # no target can be chosen, so the money stays cash. It must
            # never mean the harvest stops - the profit still comes off.
            log.warning(f"[harvest] fleet unreadable, redirect skipped this "
                        f"run: {type(e).__name__}: {e}")
            _redirect, _all_branches = False, []

    taken, failed, moved = [], [], []
    for r in p["branches"]:
        if r["harvest_usd"] < MIN_HARVEST_USD:
            continue
        try:
            await grid.withdraw_from_grid_branch(r["bot_name"], r["harvest_usd"])
        except Exception as e:
            failed.append({**r, "error": f"{type(e).__name__}: {e}"})
            log.warning(f"[harvest] {r['product_id']} refused: {e}")
            continue

        # THE MONEY IS OUT OF THE SOURCE AND NOT YET ANYWHERE. Every path
        # below either lands it in a target or puts it back. There is no
        # branch of this code where a withdrawn dollar is left nowhere.
        if _redirect:
            _target, _why = harvest_redirect.pick_target(
                _all_branches, exclude_bot_name=r["bot_name"],
                amount_usd=r["harvest_usd"], fleet_allocated_usd=_fleet)
            if _target:
                try:
                    await grid.add_cash_to_grid_branch(
                        _target, r["harvest_usd"], caller="profit_harvest.redirect")
                except Exception as e:
                    # PUT IT BACK. A failed deposit must not delete capital,
                    # and the baseline must NOT advance - the profit is still
                    # in the source branch, so the next run retries it.
                    try:
                        await grid.add_cash_to_grid_branch(
                            r["bot_name"], r["harvest_usd"],
                            caller="profit_harvest.redirect_rollback")
                        _rolled = "returned to its own branch"
                    except Exception as e2:
                        _rolled = (f"COULD NOT BE RETURNED: {type(e2).__name__}: {e2}")
                        log.error(f"[harvest] ${r['harvest_usd']:,.2f} left "
                                  f"{r['product_id']} and landed nowhere - {_rolled}")
                    failed.append({**r, "error": (
                        f"deposit into {_target} failed ({type(e).__name__}: {e}) "
                        f"- {_rolled}")})
                    continue
                moved.append({**r, "to_bot_name": _target,
                              "why": _why.get("reason")})
                log.warning(f"[harvest] moved ${r['harvest_usd']:,.2f} from "
                            f"{r['product_id']} into {_target} "
                            f"({_why.get('reason')})")
            else:
                log.info(f"[harvest] ${r['harvest_usd']:,.2f} from "
                         f"{r['product_id']} stays as cash: "
                         f"{_why.get('reason')}")

        # Advance the mark ONLY after the withdrawal succeeded. Advancing
        # first would forget profit that is still sitting in the branch.
        await _advance_baseline(grid.get_session_factory, r["bot_name"],
                                r["realised_total"])
        taken.append(r)
        log.warning(f"[harvest] took ${r['harvest_usd']:,.2f} of profit out of "
                    f"{r['product_id']}, leaving "
                    f"${r['allocated_usd'] - r['harvest_usd']:,.2f} working")
        try:
            await grid._log_activity_safe(
                None, r["product_id"], "HARVEST",
                f"Took ${r['harvest_usd']:,.2f} of realised profit off the table. "
                f"No order was placed and no coin was sold.")
        except Exception:
            pass
    total = round(sum(r["harvest_usd"] for r in taken), 2)
    _moved_usd = round(sum(r["harvest_usd"] for r in moved), 2)
    return {"ran": bool(taken), "harvested_usd": total,
            "branches": taken, "failed": failed or None,
            "rows_written": len(taken),
            "redirect_active": _redirect,
            "redirected_usd": _moved_usd,
            "redirected": moved or None,
            "redirect_is": (
                "Harvested profit was moved into the branch that can soonest "
                "turn it into a rung, never back into its own source. Claim "
                "only - no order was placed and no coin was bought or sold."
                if _redirect else
                f"OFF ({harvest_redirect.ENV_FLAG} is not true) - harvested "
                f"profit stayed as unallocated cash, as it always has."),
            "detail": (f"${total:,.2f} of profit taken out of {len(taken)} "
                       f"branch(es)." if taken else
                       "Nothing was harvested - no flat branch had "
                       f"${MIN_HARVEST_USD:,.2f} of profit since its baseline.")}

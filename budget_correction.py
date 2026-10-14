"""Take back the budget the 2026-10-13/14 rotation bug created, once.

WHAT HAPPENED. Between 22:12Z on 10-13 and the 14:30Z deploy on 10-14 the
idle rotation asked every 15 minutes to move APE-USD's whole $96.81. Every
pass credited ONDO-USD or TIA-USD first and then had its debit from APE
refused by the $15 keep-alive floor, so the destination kept the money and
APE kept it too: 32 passes x $96.81 = $3,097.92 of budget (allocated_usd)
created from nothing. No order was placed and no coin moved - the numbers
on two branches grew. 72c4aec stopped the cause.

WHAT THIS DOES. At startup, before the claim-reconciliation job can run,
it lowers those two branches by exactly the duplicated amount, through the
same withdraw_from_grid_branch the dashboard's Withdraw button calls (so the
levels and the drawdown peak are reset the way a withdraw always resets
them). The owner asked for exactly this and could not get the dashboard
withdraw to go through from his phone: "I'm telling you on the back end to
go ahead and do it."

WHY IT IS NARROW. It is not a general fixer. For each of the two branches it
acts only when ALL of these hold, and otherwise leaves the branch alone:
  * the branch exists, is flat (no open slice) and is not locked
  * its allocation is the inflated figure the bug left, to the cent
and it takes exactly (inflated - original). If the owner already withdrew,
or a trade changed the figure, nothing is touched. It runs once (a marker
row), and not at all after EXPIRES - so it can never fire on some unrelated
state a month from now.
"""
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

MARKER_KEY = "budget_correction_2026_10_14_rotation_duplicate"
EXPIRES = datetime(2026, 10, 21, tzinfo=timezone.utc)
TOLERANCE_USD = 0.02

# allocated_usd read live at 14:56Z on 10-14, and the figure before the bug
# (read live at 20:54Z on 10-13). 1666.25 - 117.29 = 1649.43 - 100.47 = 1548.96
# = 16 x 96.81, which is what makes these two a duplicated-credit signature.
TARGETS = (
    {"product_id": "ONDO-USD", "inflated_usd": 1666.25, "original_usd": 117.29},
    {"product_id": "TIA-USD", "inflated_usd": 1649.43, "original_usd": 100.47},
)

_LAST = None


def _keep(report):
    global _LAST
    report["checked_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _LAST = report
    return report


def decide(branch, target):
    """(act, amount, reason) for one branch. Pure: the whole rule."""
    if branch is None:
        return False, 0.0, "no such branch"
    alloc = float(getattr(branch, "allocated_usd", 0.0) or 0.0)
    if getattr(branch, "locked", False):
        return False, 0.0, "locked - left alone"
    if abs(alloc - target["inflated_usd"]) > TOLERANCE_USD:
        return False, 0.0, (f"allocation is ${alloc:,.2f}, not the ${target['inflated_usd']:,.2f} "
                            f"the bug left - left alone")
    amount = round(target["inflated_usd"] - target["original_usd"], 2)
    if amount <= 0:
        return False, 0.0, "nothing to take back"
    return True, amount, "exactly the duplicated amount"


async def _marker_exists():
    from sqlalchemy import select
    from models import TradingBotState
    from database import get_session_factory
    async with get_session_factory()() as db:
        row = (await db.execute(select(TradingBotState).where(
            TradingBotState.bot_name == MARKER_KEY))).scalar_one_or_none()
        return row is not None


async def _write_marker():
    from models import TradingBotState
    from database import get_session_factory
    async with get_session_factory()() as db:
        db.add(TradingBotState(bot_name=MARKER_KEY, base_capital=1.0))
        await db.commit()


async def run_once(grid, now=None):
    """Never raises. Returns what it did and why."""
    now = now or datetime.now(timezone.utc)
    if now >= EXPIRES:
        return _keep({"ran": False, "reason": "EXPIRED",
                      "detail": "the correction window has closed - nothing touched"})
    try:
        if await _marker_exists():
            return _keep({"ran": False, "reason": "ALREADY_DONE", "detail": "done on an earlier boot"})
        branches = await grid.get_grid_branches()
    except Exception as e:
        log.warning(f"[budget-correction] could not read - nothing touched: {type(e).__name__}: {e}")
        return _keep({"ran": False, "reason": "READ_FAILED",
                      "detail": f"{type(e).__name__}: {e} - nothing touched, will retry next boot"})

    by_product = {b.product_id: b for b in branches}
    done, skipped = [], []
    for t in TARGETS:
        b = by_product.get(t["product_id"])
        act, amount, why = decide(b, t)
        if act:
            try:
                slices = await grid.get_grid_slices(b.bot_name)
            except Exception as e:
                skipped.append({"product_id": t["product_id"], "why": f"slices unreadable ({type(e).__name__})"})
                continue
            if slices:
                skipped.append({"product_id": t["product_id"], "why": "holds an open slice - left alone"})
                continue
            try:
                r = await grid.withdraw_from_grid_branch(
                    b.bot_name, amount, caller="budget_correction (owner-authorised 2026-10-14)")
                done.append({"product_id": t["product_id"], "usd": amount,
                             "remaining_usd": r.get("remaining_allocated_usd")})
            except Exception as e:
                skipped.append({"product_id": t["product_id"], "why": f"{type(e).__name__}: {e}"})
        else:
            skipped.append({"product_id": t["product_id"], "why": why})

    # Done for good once every target is either corrected or in a state this
    # was never meant to touch. A target skipped for an open slice, an
    # unreadable slice list or a failed withdraw stays open for the next boot.
    retry = [s for s in skipped if "open slice" in s["why"] or "unreadable" in s["why"]
             or "Error" in s["why"] or "refus" in s["why"].lower()]
    if not retry:
        try:
            await _write_marker()
        except Exception as e:
            log.warning(f"[budget-correction] marker not written ({type(e).__name__}) - "
                        f"the preconditions keep a second run from doing anything")
    total = round(sum(x["usd"] for x in done), 2)
    log.warning(f"[budget-correction] took back ${total:,.2f} of duplicated budget "
                f"({len(done)} branch(es)); left alone: {skipped or 'none'}")
    try:
        record = getattr(grid, "_record_gate_decision", None)
        if record is not None:
            for x in done:
                await record("budget_correction", x["product_id"], "BUDGET_CORRECTED",
                             f"-${x['usd']:,.2f} duplicated by the 10-13/14 rotation bug; "
                             f"now ${x['remaining_usd']:,.2f} - no order placed")
    except Exception:
        pass
    return _keep({"ran": bool(done), "reason": "CORRECTED" if done else "NOTHING_TO_CORRECT",
                  "taken_back_usd": total, "done": done, "left_alone": skipped})


def status():
    return _LAST

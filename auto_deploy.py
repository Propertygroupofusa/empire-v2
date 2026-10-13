"""Put idle cash above the owner's floor to work, automatically.

THE OWNER, 2026-10-13: "every time there's $1,000 in there, that will be the
action" - and, after the Deploy button went live: "why do I have to push it
every time? I'm not gonna always be looking at that... build the
auto-deploy."

IT IS THE DEPLOY CASH BUTTON, PRESSED ON A TIMER. Nothing here decides
anything the button does not already decide:

  inputs  idle_cash.deploy_inputs - the pot is the SMALLER of unclaimed cash
          and real free cash, and the reserve is the larger of the working
          float and the owner's floor (GRID_CASH_RESERVE_USD)
  plan    idle_cash.plan - equal amounts, only to branches that can buy and
          have completed a round trip; parked, net-negative and unbacked
          branches refused; nothing pushed past the 20% share rule
  write   crypto_grid_bot.add_cash_to_grid_branch - allocated_usd goes up

IT PLACES NO ORDER AND SELLS NO COIN. A branch buys its own dips with the new
allocation, at its own unchanged step, exactly as after a button press.

WHAT IT ADDS ON TOP OF THE BUTTON, all of them refusals:
  * OFF unless GRID_AUTO_DEPLOY is an explicit on value. The owner sets it
    once; unset, empty or a typo is off.
  * Nothing below MIN_DEPLOY_USD ($1,000 - the owner's number) above the
    reserve. Under that it waits for the next check.
  * At most MAX_PER_PASS_USD per check, so one bad reading cannot hand out
    everything at once; a larger deposit goes in over several hours.
  * Only the process that holds the grid loop lease acts, so two copies of
    the service can never deploy the same dollars twice.

STOP_TRADING IS NOT READ HERE, on purpose and for the same reason the
button does not read it: on this account it is the switch that pauses the
Alpaca bot, the grid keeps trading while it is set, and honouring it would
mean the owner's own instruction never runs. GRID_AUTO_DEPLOY is this job's
switch.

Every check is kept for GET /idle-capital/auto-deploy, and every deploy is
written to the dashboard's activity feed.
"""
import asyncio
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger(__name__)

ENV_FLAG = "GRID_AUTO_DEPLOY"
_ON = ("1", "true", "yes", "on", "arm", "armed")
_OFF = ("", "0", "false", "no", "off")


def _env_float(name, default, floor):
    try:
        v = float(os.getenv(name, str(default)))
        return v if v >= floor else default
    except (TypeError, ValueError):
        return default


MIN_DEPLOY_USD = _env_float("GRID_AUTO_DEPLOY_MIN_USD", 1000.0, 100.0)
MAX_PER_PASS_USD = _env_float("GRID_AUTO_DEPLOY_MAX_PER_PASS_USD", 2500.0, 100.0)
INTERVAL_SECONDS = 3600
FIRST_LOOK_SECONDS = 300

_LAST = None


def enabled() -> bool:
    """True only for an explicit on value. Read every check, not at import."""
    return (os.getenv(ENV_FLAG) or "").strip().strip('"').strip("'").lower() in _ON


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _keep(report):
    global _LAST
    report["checked_at"] = _now_iso()
    _LAST = report
    return report


async def run_once(grid):
    """One check. Returns what it did and why; never raises."""
    if not enabled():
        return _keep({"ran": False, "reason": "OFF",
                      "detail": f"{ENV_FLAG} is not on - nothing is deployed automatically."})
    try:
        lease = await grid.read_grid_lease_state()
    except Exception as e:
        lease = {"error": f"{type(e).__name__}: {e}"}
    if not (lease or {}).get("held_by_this_process"):
        return _keep({"ran": False, "reason": "NOT_LEASE_HOLDER",
                      "detail": "another copy of the service runs the grid, so it decides."})
    try:
        import idle_cash
        status = await grid.get_grid_status()
        history = await grid.get_grid_trade_history()
        pot, reserve, unbacked = idle_cash.deploy_inputs(
            status, getattr(grid, "GRID_CASH_RESERVE_USD", 0.0))
        deployable = round(max(0.0, pot - reserve), 2)
        if deployable < MIN_DEPLOY_USD:
            return _keep({"ran": False, "reason": "BELOW_MINIMUM",
                          "deployable_usd": deployable, "reserve_usd": reserve,
                          "detail": (f"${deployable:,.2f} idle above the ${reserve:,.0f} "
                                     f"floor - waits for ${MIN_DEPLOY_USD:,.0f}.")})
        capped_pot = min(pot, reserve + MAX_PER_PASS_USD)
        plan = idle_cash.plan(status.get("branches") or [],
                              history.get("recent_trades") or [],
                              unclaimed_usd=capped_pot, reserve_usd=reserve,
                              unbacked=unbacked)
    except Exception as e:
        log.warning(f"[auto-deploy] check failed, nothing deployed: {type(e).__name__}: {e}")
        return _keep({"ran": False, "reason": "READ_FAILED",
                      "detail": f"{type(e).__name__}: {e} - nothing was deployed."})
    if not plan.get("ok"):
        return _keep({"ran": False, "reason": "NOTHING_TO_ADD",
                      "deployable_usd": deployable, "reserve_usd": reserve,
                      "detail": str(plan.get("detail") or "no branch can take it right now.")})

    added, failed = [], []
    for a in plan.get("adds") or []:
        try:
            await grid.add_cash_to_grid_branch(a["bot_name"], a["usd"], caller="auto_deploy")
            added.append({"product_id": a.get("product_id"), "usd": a["usd"]})
        except Exception as e:
            failed.append({"product_id": a.get("product_id"), "usd": a.get("usd"),
                           "error": f"{type(e).__name__}: {e}"})
    total = round(sum(x["usd"] for x in added), 2)
    log.warning(f"[auto-deploy] put ${total:,.2f} to work across {len(added)} branch(es)"
                + (f", {len(failed)} refused" if failed else "")
                + " - no order placed, no coin sold")
    try:
        record = getattr(grid, "_record_gate_decision", None)
        if record is not None:
            for x in added:
                await record("auto_deploy", x["product_id"], "AUTO_DEPLOY",
                             f"+${x['usd']:,.2f} of idle cash above the ${reserve:,.0f} "
                             f"floor - no order placed")
            for x in failed:
                await record("auto_deploy", x["product_id"], "AUTO_DEPLOY_REFUSED",
                             x["error"])
    except Exception:
        pass
    return _keep({"ran": True, "reason": "DEPLOYED", "added_usd": total,
                  "added": added, "failed": failed, "reserve_usd": reserve,
                  "detail": f"put ${total:,.2f} to work across {len(added)} branch(es)."})


async def run_periodically(grid):
    """The timer. A failed check is logged and retried next hour."""
    await asyncio.sleep(FIRST_LOOK_SECONDS)
    while True:
        try:
            await run_once(grid)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(f"[auto-deploy] check error ({type(e).__name__}: {e})")
        await asyncio.sleep(INTERVAL_SECONDS)


def status() -> dict:
    """What the running process sees. Pure, never raises, reads no venue."""
    try:
        raw = os.getenv(ENV_FLAG)
        norm = (raw or "").strip().strip('"').strip("'").lower()
        return {
            "enabled": norm in _ON,
            "env_name": ENV_FLAG,
            "value_recognised": norm in _ON or norm in _OFF,
            "turn_on_with": f"{ENV_FLAG}=on",
            "min_deploy_usd": MIN_DEPLOY_USD,
            "max_per_pass_usd": MAX_PER_PASS_USD,
            "checks_every_seconds": INTERVAL_SECONDS,
            "last": _LAST,
            "places_no_order": True, "sells_no_coin": True,
            "means": ("Once an hour: if at least the minimum is idle above the "
                      "owner's cash floor, it is put to work exactly as the "
                      "Deploy cash button would."),
        }
    except Exception as e:
        return {"readable": False, "error": type(e).__name__}

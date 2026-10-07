"""Rank the fleet on a timer, record what it WOULD have funded, deploy nothing.

WHAT THIS IS

opportunity_allocator.py scores every branch and ranks them. This module
runs that scorer on a timer inside the live service, writes each pass to
an append-only ledger, and publishes the latest ranking for the dashboard.

IT CANNOT MOVE MONEY. There is no order path, no write-guarded call, no
capital transfer, and no flag write anywhere in this file or in the module
it imports. It writes exactly one table - trade_decisions - which is
append-only and which nothing that trades reads. Turning this module off
changes no trading behaviour, because it has none to change.

WHY IT EXISTS RATHER THAN THE ROUTER THE OWNER SKETCHED

The owner's design routes freed capital to the top-ranked branch. Ranking
branches by expected return per dollar is the exact variant already tested
on this fleet's real 30-day tape: +$40..+$79 in sample, LOST $88..$111 out
of sample, the yield-weighted version worst of all. In rotation_study.py,
ranking by last month's P&L lost to ranking by FILL COUNT two to one.

So the owner's own instruction, 2026-10-07: "Make the scorer prove itself
against the live market while it remains powerless. If it repeatedly
identifies the branches that actually complete profitable cycles, then we
have evidence for the next step. And if it fails, we lose zero trading
capital while learning exactly why."

That is what this is. The ledger is the product.

THE LEDGER

One trade_decisions row per branch per pass, reusing a table built for
exactly this ("REFUSALS ARE RECORDED TOO, and they are the more valuable
half"). No migration, no new schema.

    bot          "shadow_allocator"
    symbol       the product id
    mandate      "opportunity_allocator"
    admitted     True when the branch qualified
    reason       the gate that rejected it, or "QUALIFIED"
    checks_json  score, fire_rate, net_per_100, state, trend, free_rungs,
                 allocated_usd, fleet_share_pct, and `selected` - true on
                 the ONE branch this pass would have funded
    decided_at   the pass timestamp

OUTCOMES ARE NOT WRITTEN. They are derived at read time by asking the real
trade ledger what each branch actually did after the pass. A recorded
prediction plus an independently recorded outcome is auditable; a
self-reported outcome is not.

OFF SWITCH: set SHADOW_ALLOCATOR_ENABLED to 0 and the thread exits at
boot. Nothing else changes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

ENABLED = os.getenv("SHADOW_ALLOCATOR_ENABLED", "1").strip() not in ("0", "false", "False", "")
INTERVAL_SECONDS = int(os.getenv("SHADOW_ALLOCATOR_INTERVAL_SECONDS", str(30 * 60)))
CANDLE_DAYS = int(os.getenv("SHADOW_ALLOCATOR_CANDLE_DAYS", "45"))
BOT_NAME = "shadow_allocator"
MANDATE = "opportunity_allocator"

# The newest completed pass, served to the dashboard without recomputing.
_latest: dict = {"ready": False, "reason": "no pass has completed yet"}
_lock = threading.Lock()


def latest() -> dict:
    with _lock:
        return dict(_latest)


def _rank_sync() -> dict:
    """One scoring pass. Pure computation plus public reads; writes nothing."""
    import opportunity_allocator as OA

    gs = OA.MSS._get(f"{OA.BASE}/grid-status", timeout=90)
    branches = [b for b in (gs.get("branches") or []) if b.get("product_id")]
    total_claim = sum(float(b.get("allocated_usd") or 0) for b in branches)
    short = {r.get("product_id")
             for r in ((gs.get("backing_owned") or {}).get("short_positions") or [])
             if r.get("product_id")}

    rows = []
    for b in branches:
        pid = b["product_id"]
        step = float(b.get("grid_pct") or 0.03)
        alloc = float(b.get("allocated_usd") or 0)
        slices = int(b.get("open_slices") or 0)
        levels = max(int(b.get("num_levels") or 1), 1)
        share = (100.0 * alloc / total_claim) if total_claim else 0.0

        prof, err = OA.profile(pid, step, days=CANDLE_DAYS)
        if prof is None:
            rows.append({"product_id": pid, "verdict": "REJECT", "gate": "UNREADABLE",
                         "score": 0.0, "fire_rate": 0.0, "net_per_100": 0.0,
                         "state": None, "trend": None, "free_rungs": 0,
                         "allocated_usd": alloc, "fleet_share_pct": share,
                         "detail": err})
            continue

        room = max(levels - slices, 0)
        gate = None
        if pid in short:
            gate = "SHORT"
        elif b.get("drawdown_breached"):
            gate = "FROZEN"
        elif room == 0:
            gate = "FULL"
        elif total_claim and alloc / total_claim >= OA.CONCENTRATION_CAP:
            gate = "CONCENTRATION"
        elif alloc < OA.VIABILITY_FLOOR:
            gate = "VIABILITY"
        elif prof["state"] == "DORMANT":
            gate = "DORMANT"
        elif prof["net_per_100"] <= 0:
            gate = "NO_EDGE"

        score = (prof["fire_rate"] * prof["net_per_100"]
                 * OA.STATE_MULT.get(prof["state"], 0.05)
                 * min(room / levels, 1.0) * 100.0)
        rows.append({"product_id": pid,
                     "verdict": "REJECT" if gate else "QUALIFIED",
                     "gate": gate or "", "score": 0.0 if gate else score,
                     "fire_rate": prof["fire_rate"], "net_per_100": prof["net_per_100"],
                     "state": prof["state"], "trend": prof["trend"],
                     "free_rungs": room, "allocated_usd": alloc,
                     "fleet_share_pct": share, "detail": ""})

    rows.sort(key=lambda r: (-r["score"], r["product_id"]))
    qualified = [r for r in rows if r["verdict"] == "QUALIFIED"]
    pick = qualified[0]["product_id"] if qualified else None
    for r in rows:
        r["selected"] = (r["product_id"] == pick)
    return {"ready": True,
            "as_of": datetime.now(timezone.utc).isoformat(),
            "deploys_nothing": True,
            "candle_days": CANDLE_DAYS,
            "fleet_claim_usd": round(total_claim, 2),
            "free_cash_usd": float(gs.get("real_free_cash_usd") or 0),
            "branches": len(rows),
            "qualified": len(qualified),
            "would_have_funded": pick,
            "ranking": rows}


async def _write_ledger(pass_: dict):
    """Append one row per branch. Never raises - telemetry must not bite."""
    try:
        from database import get_session_factory
        from models import TradeDecision
        async with get_session_factory()() as db:
            for r in pass_["ranking"]:
                db.add(TradeDecision(
                    bot=BOT_NAME, symbol=r["product_id"], direction="allocate",
                    mandate=MANDATE,
                    admitted=(r["verdict"] == "QUALIFIED"),
                    reason=(r["gate"] or "QUALIFIED"),
                    failed_rules=(r["gate"] or None),
                    checks_json=json.dumps({
                        "score": round(r["score"], 6),
                        "fire_rate": round(r["fire_rate"], 6),
                        "net_per_100": round(r["net_per_100"], 6),
                        "volatility_state": r["state"], "trend_state": r["trend"],
                        "free_rungs": r["free_rungs"],
                        "allocated_usd": round(r["allocated_usd"], 2),
                        "fleet_share_pct": round(r["fleet_share_pct"], 2),
                        "selected": r["selected"],
                        "detail": r["detail"] or None,
                    }),
                    total_notional=pass_["fleet_claim_usd"],
                    buying_power=pass_["free_cash_usd"],
                ))
            await db.commit()
    except Exception as e:                                   # noqa: BLE001
        log.warning(f"[SHADOW-ALLOC] ledger write failed (non-fatal): {e}")


async def resolve_outcomes(hours_forward: int = 4, limit_passes: int = 200) -> dict:
    """Did the branch the scorer picked do better than the ones it passed over?

    THE HEADLINE IS DELIBERATELY NOT AN ACCURACY FIGURE. "Allocator
    accuracy: 72%" is unreadable - 72% of what, against what baseline? A
    scorer that selects whichever branch is most likely to cycle will look
    accurate in a week when every branch cycles and useless in a week when
    none do, and neither reading says whether it DISCRIMINATES.

    So four numbers are reported, and the fourth is the only one that
    answers the question:

      selected_cycled_pct      selected branches that then completed a
                               profitable cycle inside the window
      unselected_cycled_pct    branches it passed over that completed one
                               ANYWAY - the base rate, and the thing a
                               bare accuracy figure hides
      discrimination_pts       the first minus the second. Zero means the
                               ranking carries no information, however
                               high the first number looks.
      net_usd_selected / net_usd_unselected_avg
                               what actually landed, per branch

    The prediction is read from trade_decisions. The outcome is read from
    the real trade ledger. Nothing grades its own homework, and a window
    that has not closed yet is skipped rather than scored as a miss.

    What is still missing, stated rather than implied: the counterfactual
    net. Knowing the picked branch cycled does NOT say the fleet would
    have earned more, because no capital moved and the alternative book
    was never run. That needs the router, and the router needs this.
    """
    from sqlalchemy import select
    from database import get_session_factory
    from models import TradeDecision, CryptoGridTradeHistory

    async with get_session_factory()() as db:
        res = await db.execute(
            select(TradeDecision)
            .where(TradeDecision.bot == BOT_NAME)
            .order_by(TradeDecision.decided_at.desc())
            .limit(limit_passes * 25))
        rows = list(res.scalars().all())
        res2 = await db.execute(select(CryptoGridTradeHistory))
        closes = [t for t in res2.scalars().all() if t.closed_at]

    def _aware(d):
        return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d

    # group the rows into passes by their decided_at stamp
    passes = {}
    for r in rows:
        if r.decided_at is None:
            continue
        try:
            c = json.loads(r.checks_json or "{}")
        except (TypeError, ValueError):
            continue
        passes.setdefault(_aware(r.decided_at), []).append((r, c))

    now = datetime.now(timezone.utc)
    sel_n = sel_hit = unsel_n = unsel_hit = 0
    sel_net = unsel_net = 0.0
    graded = []

    for t0 in sorted(passes, reverse=True):
        t1 = t0 + timedelta(hours=hours_forward)
        if now < t1:
            continue                                   # window still open
        branch_rows = passes[t0]
        picked = None
        for r, c in branch_rows:
            after = [t for t in closes
                     if t.product_id == r.symbol and t0 < _aware(t.closed_at) <= t1]
            pnl = sum(float(t.pnl or 0) for t in after)
            cycled = bool(after) and pnl > 0
            if c.get("selected"):
                picked = r.symbol
                sel_n += 1
                sel_hit += 1 if cycled else 0
                sel_net += pnl
            else:
                unsel_n += 1
                unsel_hit += 1 if cycled else 0
                unsel_net += pnl
        graded.append({"at": t0.isoformat(), "picked": picked,
                       "branches_in_pass": len(branch_rows)})
        if len(graded) >= limit_passes:
            break

    sel_pct = (100.0 * sel_hit / sel_n) if sel_n else None
    unsel_pct = (100.0 * unsel_hit / unsel_n) if unsel_n else None
    disc = (round(sel_pct - unsel_pct, 1)
            if sel_pct is not None and unsel_pct is not None else None)

    return {
        "graded_passes": len(graded),
        "window_hours": hours_forward,
        "selected_branches": sel_n,
        "selected_cycled": sel_hit,
        "selected_cycled_pct": round(sel_pct, 1) if sel_pct is not None else None,
        "unselected_branches": unsel_n,
        "unselected_cycled": unsel_hit,
        "unselected_cycled_pct": round(unsel_pct, 1) if unsel_pct is not None else None,
        "discrimination_pts": disc,
        "net_usd_selected": round(sel_net, 4),
        "net_usd_unselected_avg": round(unsel_net / unsel_n, 4) if unsel_n else None,
        "read_discrimination_not_accuracy": (
            "selected_cycled_pct alone is not a score. Compare it against "
            "unselected_cycled_pct, which is the base rate of a branch "
            "cycling anyway. discrimination_pts at or below zero means the "
            "ranking carries no information however high the first looks."),
        "what_this_cannot_say": (
            "This does NOT say the fleet would have earned more. No capital "
            "moved and the counterfactual book was never run. It is evidence "
            "about the SIGNAL, not about a strategy."),
        "passes": graded,
    }


def run():
    """Daemon thread entry point. Started from main.py's lifespan."""
    if not ENABLED:
        log.info("[SHADOW-ALLOC] disabled by SHADOW_ALLOCATOR_ENABLED - not starting")
        return
    log.info(f"[SHADOW-ALLOC] starting - ranks every {INTERVAL_SECONDS}s, "
             f"{CANDLE_DAYS}d candles, DEPLOYS NOTHING")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    while True:
        try:
            p = _rank_sync()
            with _lock:
                _latest.clear()
                _latest.update(p)
            loop.run_until_complete(_write_ledger(p))
            log.info(f"[SHADOW-ALLOC] pass done - {p['qualified']}/{p['branches']} "
                     f"qualified, would have funded {p['would_have_funded']}")
        except Exception as e:                               # noqa: BLE001
            log.warning(f"[SHADOW-ALLOC] pass failed (non-fatal): {e}")
            with _lock:
                _latest.setdefault("last_error", str(e))
        time.sleep(INTERVAL_SECONDS)

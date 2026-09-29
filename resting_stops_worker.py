"""Places, ratchets and cancels the stop orders that rest at Coinbase.

This file reaches the venue. Everything it decides is decided in
resting_stops.py, which imports no HTTP client and is tested without an
account; this is only the part that talks.

THE ORDER OF OPERATIONS, AND WHY

A resting sell holds its coins, so a stop cannot be replaced by placing
the new one first - the units are already committed to the old order. The
sequence has to be cancel, then place, and between those two calls the
position is NAKED. That window is the whole reason this loop is cautious:

  * it replaces only on a meaningful move UP, never down and never for a
    rounding difference, so the window is opened rarely
  * a failed place after a successful cancel is logged at error and left
    for the next pass, which re-places from scratch - it is never retried
    in a tight loop that could leave several coins naked at once
  * it does one asset at a time. A batch that fails halfway leaves an
    unknown number of positions uncovered.

WHAT IT WILL NOT DO

It has no buy path. It cannot widen a stop. It cannot cancel an order it
did not place - the client_order_id prefix identifies its own work, and
anything else resting at the venue is left alone, because an order this
loop did not create is one a human created and cancelling it would be
taking an action nobody asked for.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime

import aiohttp

import resting_stops

log = logging.getLogger("resting_stops")

CHECK_SECONDS = int(os.getenv("RESTING_STOPS_CHECK_SECONDS", "900"))

# Every order this loop creates carries this prefix, so it can recognise
# its own work and leave everyone else's alone.
COID_PREFIX = "rstop-"

HEARTBEAT = {"started_at": None, "last_pass_at": None, "passes": 0,
             "last_result": None, "last_error": None}


def current_mode() -> str:
    return resting_stops.normalise_mode(os.getenv(resting_stops.MODE_ENV))


def is_armed() -> bool:
    return current_mode() == resting_stops.MODE_ARM


async def open_stop_orders(session):
    """This loop's own resting stops, by asset. None if it cannot be read.

    None is load-bearing: not knowing what is already resting must stop
    the pass, never be read as "nothing is resting" - that would place a
    second stop on top of the first and hold twice the coins.
    """
    import account_census
    path = "/api/v3/brokerage/orders/historical/batch"
    try:
        async with session.get(
            f"https://api.coinbase.com{path}?order_status=OPEN&limit=250",
            headers=account_census._auth_headers("GET", path), timeout=25
        ) as r:
            if r.status != 200:
                log.warning(f"[stops] open orders HTTP {r.status}")
                return None
            body = await r.json()
    except Exception as e:
        log.warning(f"[stops] open orders unreadable: {type(e).__name__}: {e}")
        return None

    mine = {}
    for o in body.get("orders") or []:
        coid = str(o.get("client_order_id") or "")
        if not coid.startswith(COID_PREFIX):
            continue
        cfg = (o.get("order_configuration") or {}).get("stop_limit_stop_limit_gtc") or {}
        asset = str(o.get("product_id") or "").split("-")[0]
        if not asset:
            continue
        mine[asset] = {"order_id": o.get("order_id"),
                       "stop_price": cfg.get("stop_price"),
                       "base_size": cfg.get("base_size")}
    return mine


async def cancel(session, order_id):
    import crypto_coinbase_bot
    path = "/api/v3/brokerage/orders/batch_cancel"
    async with session.post(f"https://api.coinbase.com{path}",
                            headers=crypto_coinbase_bot._auth_headers("POST", path),
                            json={"order_ids": [order_id]}, timeout=30) as r:
        body = await r.json()
        results = body.get("results") or []
        ok = bool(results and results[0].get("success"))
        return ok, body


async def place(session, product_id, plan):
    import crypto_coinbase_bot
    order = {
        "client_order_id": COID_PREFIX + str(uuid.uuid4()),
        "product_id": product_id,
        "side": "SELL",
        "order_configuration": plan["order_configuration"],
    }
    path = "/api/v3/brokerage/orders"
    async with session.post(f"https://api.coinbase.com{path}",
                            headers=crypto_coinbase_bot._auth_headers("POST", path),
                            json=order, timeout=30) as r:
        body = await r.json()
        ok = r.status in (200, 201) and body.get("success", True)
        return ok, body


async def check_once(session_factory, *, place_orders=True) -> dict:
    """One pass. Reads the levels, then makes the venue match them."""
    if not is_armed():
        return {"armed": False, "placed": 0, "replaced": 0,
                "detail": "observing - the venue was not contacted"}

    import account_census
    import holdings_watch
    from routers.trading_dashboard import get_holdings_watch

    try:
        watch = await get_holdings_watch(window_days=30)
    except Exception as e:
        return {"armed": True, "placed": 0, "replaced": 0,
                "detail": f"levels unreadable ({type(e).__name__}); nothing touched"}

    placed = replaced = 0
    results = []
    async with aiohttp.ClientSession() as session:
        existing = await open_stop_orders(session)
        if existing is None:
            return {"armed": True, "placed": 0, "replaced": 0,
                    "detail": ("could not read what is already resting; skipping rather "
                               "than risk placing a second stop on the same coins")}

        total = watch.get("coin_usd") or 0.0

        # Coins the grid currently holds open slices on. A resting sell on
        # those holds the units the grid trades with, and sells them out
        # from under the branch if it fires - see the note in
        # resting_stops.plan_stop. Read ONCE per pass, not per asset.
        #
        # Fails OPEN on an unreadable grid: an empty set protects nothing,
        # which is exactly the behaviour this loop had before.
        protected = ()
        try:
            import crypto_grid_bot as _grid
            _units, _ = await _grid.fleet_tracked_units_by_product()
            if _units:
                protected = {p.split("-")[0].upper() for p in _units}
        except Exception as exc:
            log.warning(f"[stops] could not read grid positions ({type(exc).__name__}) "
                        f"- placing without that protection this pass")

        # Which of those branches has NO stop of its own. Reporting only:
        # it cannot cause a placement, and every asset refused above is
        # still refused. It exists so a refusal stops claiming the branch
        # covers a position when the branch has declared it does not - and
        # so the resulting gap is a figure rather than a reassurance.
        #
        # None, not {}, on an unreadable read: UNKNOWN must not render as
        # "every branch has a stop".
        unstopped = None
        try:
            import crypto_grid_bot as _grid
            unstopped = await _grid.products_without_a_grid_stop()
        except Exception as exc:
            log.warning(f"[stops] could not read grid stop coverage "
                        f"({type(exc).__name__}) - refusals will say so rather than "
                        f"claim the branch has it")

        for row in (watch.get("rows") or []):
            asset = row.get("asset")
            if not asset:
                continue
            share = ((row.get("usd") or 0) / total * 100.0) if total else None

            bi, qi, bms = "0.00000001", "0.01", None
            try:
                ppath = f"/api/v3/brokerage/products/{asset}-USD"
                async with session.get(f"https://api.coinbase.com{ppath}",
                                       headers=account_census._auth_headers("GET", ppath),
                                       timeout=20) as r:
                    if r.status == 200:
                        m = await r.json()
                        bi = m.get("base_increment") or bi
                        qi = m.get("quote_increment") or qi
                        bms = m.get("base_min_size")
            except Exception:
                pass

            # Available units only. An existing resting stop already holds
            # part of the position, and sizing against the total would ask
            # the venue to sell coins it has itself reserved.
            plan = resting_stops.plan_stop(
                asset, units_available=row.get("units"), price=row.get("price"),
                stop_price=row.get("stop_level"), base_increment=bi,
                quote_increment=qi, base_min_size=bms,
                share_pct=share, limit_pct=20.0,
                actively_traded=protected, unstopped=unstopped)

            have = existing.get(asset)
            if not plan.get("ok"):
                results.append({"asset": asset, "action": "skip",
                                "reason": plan.get("reason")})
                continue

            if have:
                move, why = resting_stops.needs_replacement(
                    have.get("stop_price"), plan["stop_price"])
                if not move:
                    results.append({"asset": asset, "action": "leave", "reason": why})
                    continue
                if not place_orders:
                    results.append({"asset": asset, "action": "would_replace"})
                    continue
                ok, body = await cancel(session, have["order_id"])
                if not ok:
                    log.error(f"[stops] {asset}: cancel failed, stop left as it was: {body}")
                    results.append({"asset": asset, "action": "cancel_failed"})
                    continue
                # NAKED FROM HERE UNTIL THE PLACE SUCCEEDS.
                ok, body = await place(session, f"{asset}-USD", plan)
                if ok:
                    replaced += 1
                    results.append({"asset": asset, "action": "replaced",
                                    "stop": plan["stop_price"]})
                else:
                    log.error(f"[stops] {asset}: CANCELLED BUT NOT REPLACED - the "
                              f"position is uncovered until the next pass: {body}")
                    results.append({"asset": asset, "action": "UNCOVERED"})
                continue

            if not place_orders:
                results.append({"asset": asset, "action": "would_place"})
                continue
            ok, body = await place(session, f"{asset}-USD", plan)
            if ok:
                placed += 1
                log.warning(f"[stops] resting stop on {asset}: {plan['base_size']} @ "
                            f"{plan['stop_price']} (limit {plan['limit_price']}), "
                            f"${plan['protects_usd']:,.2f} covered")
                results.append({"asset": asset, "action": "placed",
                                "stop": plan["stop_price"],
                                "protects_usd": plan["protects_usd"]})
            else:
                log.error(f"[stops] {asset}: place rejected: {body}")
                results.append({"asset": asset, "action": "rejected"})

    return {"armed": True, "placed": placed, "replaced": replaced, "results": results,
            "detail": f"{placed} placed, {replaced} replaced"}


async def run_periodically(session_factory):
    HEARTBEAT["started_at"] = datetime.utcnow().isoformat() + "Z"
    log.info(f"[stops] resting-stop loop up, mode={current_mode()}, every {CHECK_SECONDS}s")
    while True:
        try:
            r = await check_once(session_factory)
            HEARTBEAT["last_result"] = r.get("detail")
            HEARTBEAT["last_error"] = None
            if r.get("placed") or r.get("replaced"):
                log.warning(f"[stops] {r['detail']}")
        except Exception as e:
            HEARTBEAT["last_error"] = f"{type(e).__name__}: {e}"
            log.warning(f"[stops] pass failed: {type(e).__name__}: {e}")
        HEARTBEAT["last_pass_at"] = datetime.utcnow().isoformat() + "Z"
        HEARTBEAT["passes"] += 1
        await asyncio.sleep(CHECK_SECONDS)

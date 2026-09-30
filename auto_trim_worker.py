"""The loop that places the trim. The only file here that spends money.

It runs in-process, so write_guard does not apply - middleware sits over
HTTP requests and a background task is not a request. That asymmetry is
load-bearing elsewhere in this repo (the alarm keeps working while the
dashboard is frozen), and here it is the thing that needs the most care,
because it means this loop can sell whether or not anyone holds a token.

So the permission lives in a setting instead, and it is checked twice:

  1. `is_armed()` before anything is fetched, so an observing deployment
     does no work and cannot fail into placing.
  2. immediately before the order is sent, against the same constant.

Between those two checks the loop reads the account, sizes every trim in
auto_trim (which cannot place anything - see the tests that assert it
imports no HTTP client), and writes an AutoTrimAction row for EVERY
decision including the refusals. The row is written BEFORE the order goes
out and updated after, so an order that succeeds while the process dies
leaves a "placing" row behind rather than no trace at all. A silent sale
is the worst outcome available here; a duplicated audit row is not.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timedelta

import aiohttp
from sqlalchemy import select

import auto_trim

log = logging.getLogger("auto_trim")

# PROOF OF LIFE.
#
# On 2026-09-27 the trimmer was armed, the endpoint agreed it was armed,
# and no order appeared for an hour. There was no way to tell from outside
# whether the loop was running at all, failing every pass, or simply had
# not reached one - three very different situations that all look like an
# empty trades table. A loop that can spend money must be able to say when
# it last woke up, so the question "is it running" has an answer that is
# not a guess.
HEARTBEAT = {
    "started_at": None,      # when run_periodically was entered
    "last_pass_at": None,    # when the most recent pass FINISHED
    "passes": 0,
    "last_result": None,     # the detail string from that pass
    "last_error": None,
}

CHECK_SECONDS = int(os.getenv("AUTO_TRIM_CHECK_SECONDS", "900"))    # 15 min
MODE_ENV = "AUTO_TRIM_MODE"
HISTORY_DAYS = 7


def current_mode() -> str:
    return auto_trim.normalise_mode(os.getenv(MODE_ENV))


def is_armed() -> bool:
    return current_mode() == auto_trim.MODE_ARM


async def _recent_actions(session_factory, now):
    """Placed trims from the last week. Unreadable -> None, never [].

    [] would mean "nothing has been trimmed", which reopens the daily
    budget and the per-asset cooldown. None makes the caller skip the
    pass instead.
    """
    from models import AutoTrimAction
    try:
        since = now - timedelta(days=HISTORY_DAYS)
        async with session_factory()() as db:
            rows = (await db.execute(
                select(AutoTrimAction)
                .where(AutoTrimAction.placed_at != None)          # noqa: E711
                .where(AutoTrimAction.placed_at >= since))).scalars().all()
        return [{"asset": r.asset, "usd": r.usd, "placed_at": r.placed_at} for r in rows]
    except Exception as e:
        log.warning(f"[trim] trim history unreadable: {type(e).__name__}: {e}")
        return None


async def _record(session_factory, **fields):
    """Audit row. A failure to record is logged, never raised.

    Deliberate: the alternative is that a database hiccup stops the risk
    control from running at all.
    """
    from models import AutoTrimAction
    try:
        async with session_factory()() as db:
            row = AutoTrimAction(**fields)
            db.add(row)
            await db.commit()
            return row.id
    except Exception as e:
        log.warning(f"[trim] could not record {fields.get('asset')}: {type(e).__name__}: {e}")
        return None


async def _mark(session_factory, row_id, **fields):
    from models import AutoTrimAction
    if row_id is None:
        return
    try:
        async with session_factory()() as db:
            row = (await db.execute(
                select(AutoTrimAction).where(AutoTrimAction.id == row_id))).scalar_one_or_none()
            if row is None:
                return
            for k, v in fields.items():
                setattr(row, k, v)
            await db.commit()
    except Exception as e:
        log.warning(f"[trim] could not update row {row_id}: {type(e).__name__}: {e}")


async def _place_market_sell(session, product_id, base_size):
    """Send one market IOC sell. Returns (ok, payload).

    The single point in this package where money moves. Kept tiny so it
    can be read in one sitting.
    """
    import crypto_coinbase_bot
    order = {
        "client_order_id": str(uuid.uuid4()),
        "product_id": product_id,
        "side": "SELL",
        "order_configuration": {"market_market_ioc": {"base_size": base_size}},
    }
    path = "/api/v3/brokerage/orders"
    async with session.post(f"https://api.coinbase.com{path}",
                            headers=crypto_coinbase_bot._auth_headers("POST", path),
                            json=order, timeout=30) as r:
        payload = await r.json()
        ok = r.status in (200, 201) and payload.get("success", True)
        if ok:
            # This loop posts its own order rather than going through the
            # engine, so it records its own attribution. Coinbase fills
            # carry order_id and NOT client_order_id, so that id is the
            # only thing a later audit can join on. Non-fatal by design:
            # losing a row must never cost a trim.
            try:
                import crypto_btc_compound_bot as _engine
                await _engine._record_order_source(
                    ((payload.get("success_response") or {}).get("order_id")),
                    "auto_trim", product_id, "SELL")
            except Exception as e:
                log.debug(f"[trim] attribution not recorded (non-fatal): "
                          f"{type(e).__name__}: {e}")
        return ok, payload


async def _available_map(session):
    """AVAILABLE balances only, by ticker. None if the read failed.

    The census sums available + hold because it is VALUING the account.
    Held units cannot be sold, so sizing an order against them produces
    one the venue rejects. This is a separate read for that reason, and a
    failure returns None so the pass is skipped rather than sized against
    a number that means something else.
    """
    import account_census
    path = "/api/v3/brokerage/accounts"
    out, cursor, pages = {}, None, 0
    try:
        while pages < 20:
            q = "?limit=250" + (f"&cursor={cursor}" if cursor else "")
            async with session.get(f"https://api.coinbase.com{path}{q}",
                                   headers=account_census._auth_headers("GET", path),
                                   timeout=25) as r:
                if r.status != 200:
                    return None
                body = await r.json()
            for a in body.get("accounts") or []:
                cur = a.get("currency")
                if not cur:
                    continue
                try:
                    v = float((a.get("available_balance") or {}).get("value") or 0)
                except (TypeError, ValueError):
                    continue
                out[cur] = out.get(cur, 0.0) + v
            pages += 1
            cursor = body.get("cursor") or None
            if not body.get("has_next") or not cursor:
                break
        return out
    except Exception as e:
        log.warning(f"[trim] available balances unreadable: {type(e).__name__}: {e}")
        return None


async def check_once(session_factory, *, place=True) -> dict:
    """One pass: read the account, size the trims, place them if armed."""
    import account_census
    import sell_amount

    now = datetime.utcnow()
    mode = current_mode()

    if mode != auto_trim.MODE_ARM:
        return {"mode": mode, "armed": False, "acted": 0,
                "detail": "observing - the account was not read and nothing was placed"}

    history = await _recent_actions(session_factory, now)
    if history is None:
        return {"mode": mode, "armed": True, "acted": 0,
                "detail": "trim history unreadable; skipping this pass rather than "
                          "trimming against an unknown daily total"}

    acted, results = 0, []
    async with aiohttp.ClientSession() as session:
        try:
            census = await account_census.census(session, tracked_usd=0.0)
        except Exception as e:
            log.warning(f"[trim] census failed: {type(e).__name__}: {e}")
            return {"mode": mode, "armed": True, "acted": 0,
                    "detail": f"account could not be read ({type(e).__name__}); nothing placed"}
        if not census.get("available"):
            return {"mode": mode, "armed": True, "acted": 0,
                    "detail": f"account could not be read ({census.get('error')}); nothing placed"}

        holdings = census.get("holdings") or []
        total = census.get("total_usd")
        # Coins the grid currently holds open slices on. Trimming those
        # sells coin the grid still has on its books, which is how a
        # branch ends up claiming units the wallet does not have.
        #
        # THIS NOW FAILS CLOSED, and it is the whole point of this block.
        # It used to fail OPEN, and the way it did so was invisible:
        # fleet_tracked_units_by_product returns (None, None) on a failed
        # read - deliberately, its docstring says "never an empty dict,
        # which would read as 'the fleet holds nothing' and pass every
        # check trivially" - and the old `if _units:` collapsed that None
        # straight back into "protect nothing". No exception was raised,
        # so the warning below never fired either. Under the venue rate
        # limiting this account actually sees, that is a silent licence
        # to market-sell coin the grid is holding.
        #
        # Measured 2026-09-30: ALGO, TIA and PRIME each carried REAL grid
        # buys (adopted=False) and held 0.017%, 0.000% and 0.000% of the
        # units their slices claim. $1,195.34 across eight branches, and
        # QNT's exit had been refused 200 times because the coin behind it
        # was gone. That is the shape this produces.
        #
        # A skipped trim costs a few more minutes of concentration, and
        # the ceiling is still there next pass. A blind trim costs coin,
        # a phantom slice row, and a manual reconcile. Every other
        # unreadable input in this function already returns rather than
        # guessing; this one was the outlier.
        # RETRY BEFORE REFUSING. Failing closed is correct, but a guard
        # that gives up on the first miss turns every transient rate
        # limit into a skipped pass, and the ceiling then goes
        # unenforced for as long as the venue is busy. The read is
        # cheap and the failure it guards against is expensive, so it
        # gets three attempts with a widening pause before the refusal
        # stands. Measured on this account, the venue's 429s clear in
        # seconds; it is the ten-second timeouts that cluster.
        protected = ()
        _units = None
        for _attempt in range(3):
            try:
                import crypto_grid_bot as _grid
                _units, _ = await _grid.fleet_tracked_units_by_product()
            except Exception as exc:
                _units = None
                log.warning(f"[auto_trim] grid positions raised on attempt "
                            f"{_attempt + 1}/3 ({type(exc).__name__}: {exc})")
            if _units is not None:
                if _attempt:
                    log.info(f"[auto_trim] grid positions readable on attempt "
                             f"{_attempt + 1} - protection restored without a skip")
                break
            if _attempt < 2:
                await asyncio.sleep(2 ** _attempt)
        if _units is None:
            log.warning("[auto_trim] grid positions UNREADABLE - skipping this pass "
                        "rather than trimming coin the grid may be holding")
            return {"mode": mode, "armed": True, "acted": 0,
                    "detail": ("grid positions could not be read in 3 attempts, so which "
                               "coins are "
                               "actively traded is UNKNOWN; nothing placed. Trimming "
                               "without that list sells coin the grid still has on its "
                               "books and leaves a slice claiming units the wallet no "
                               "longer holds."),
                    "skipped_because": "grid_positions_unreadable"}
        protected = {p.split("-")[0].upper() for p in _units}
        plans = auto_trim.plan_trims(holdings, total, now=now, history=history,
                                     actively_traded=protected)
        summary = auto_trim.summarise(plans, mode)

        for p in plans:
            if not p.get("act"):
                await _record(session_factory, asset=p["asset"], usd=0.0,
                              decided_at=now, skipped_reason=p.get("reason"),
                              detail=(p.get("detail") or "")[:500],
                              share_pct=p.get("share_pct"), status="skipped")

        todo = [x for x in plans if x.get("act")]
        avail = await _available_map(session) if todo else {}
        if todo and avail is None:
            return {"mode": mode, "armed": True, "acted": 0,
                    "detail": "available balances could not be read; nothing placed"}

        for p in todo:
            asset = p["asset"]
            product_id = f"{asset}-USD"
            holding = next((h for h in holdings if (h.get("asset") or "").upper() == asset), {})
            price = holding.get("price")
            units = avail.get(asset)

            if not units:
                await _record(session_factory, asset=asset, usd=0.0, decided_at=now,
                              skipped_reason="NOTHING_AVAILABLE", share_pct=p.get("share_pct"),
                              detail="the whole position is on hold; nothing can be sold",
                              status="skipped")
                results.append({"asset": asset, "placed": False, "reason": "nothing available"})
                continue

            increment, min_size = "0.00000001", None
            try:
                ppath = f"/api/v3/brokerage/products/{product_id}"
                async with session.get(f"https://api.coinbase.com{ppath}",
                                       headers=account_census._auth_headers("GET", ppath),
                                       timeout=20) as r:
                    if r.status == 200:
                        meta = await r.json()
                        increment = meta.get("base_increment") or increment
                        min_size = meta.get("base_min_size") or None
            except Exception as e:
                log.warning(f"[trim] {product_id} meta unavailable ({e}); using defaults")

            plan = sell_amount.plan_sale(p["trim_usd"], price, units,
                                         base_increment=increment, base_min_size=min_size)
            if not plan.get("ok"):
                await _record(session_factory, asset=asset, usd=0.0, decided_at=now,
                              skipped_reason="UNSIZEABLE", detail=plan.get("reason", "")[:500],
                              share_pct=p.get("share_pct"), status="skipped")
                results.append({"asset": asset, "placed": False, "reason": plan.get("reason")})
                continue

            row_id = await _record(session_factory, asset=asset, usd=plan["est_usd"],
                                   decided_at=now, share_pct=p.get("share_pct"),
                                   base_size=plan["base_size"],
                                   detail=(p.get("detail") or "")[:500], status="placing")

            # Second check, against the same constant as the first. The
            # setting can change while a pass is in flight, and the only
            # safe direction to resolve that is not to place.
            if not place or current_mode() != auto_trim.MODE_ARM:
                await _mark(session_factory, row_id, status="skipped",
                            skipped_reason="DISARMED_MID_PASS")
                results.append({"asset": asset, "placed": False, "reason": "disarmed mid-pass"})
                continue

            try:
                ok, payload = await _place_market_sell(session, product_id, plan["base_size"])
            except Exception as e:
                await _mark(session_factory, row_id, status="failed",
                            skipped_reason=f"{type(e).__name__}: {e}"[:200])
                log.error(f"[trim] {product_id} order failed: {type(e).__name__}: {e}")
                results.append({"asset": asset, "placed": False, "reason": str(e)})
                continue

            if ok:
                await _mark(session_factory, row_id, status="placed",
                            placed_at=datetime.utcnow(),
                            order_id=str((payload.get("success_response") or {}).get("order_id") or "")[:64])
                acted += 1
                log.warning(f"[trim] SOLD {plan['base_size']} {product_id} "
                            f"(~${plan['est_usd']:,.2f}) - {asset} was {p['share_pct']}% "
                            f"of the account against a {auto_trim.LIMIT_PCT:.0f}% rule")
                results.append({"asset": asset, "placed": True, "usd": plan["est_usd"],
                                "base_size": plan["base_size"]})
            else:
                await _mark(session_factory, row_id, status="rejected",
                            skipped_reason=str(payload.get("error_response") or payload)[:200])
                log.error(f"[trim] {product_id} rejected: {payload.get('error_response')}")
                results.append({"asset": asset, "placed": False, "reason": "rejected by venue"})

    return {"mode": mode, "armed": True, "acted": acted, "results": results,
            "would_trim_usd": summary["would_trim_usd"], "detail": summary["headline"]}


async def run_periodically(session_factory):
    """Never dies. A risk control that raises is not a risk control."""
    HEARTBEAT["started_at"] = datetime.utcnow().isoformat() + "Z"
    log.info(f"[trim] auto-trimmer loop up, mode={current_mode()}, every {CHECK_SECONDS}s")
    while True:
        try:
            r = await check_once(session_factory)
            HEARTBEAT["last_result"] = r.get("detail")
            HEARTBEAT["last_error"] = None
            if r.get("acted"):
                log.warning(f"[trim] {r['acted']} trim(s) placed: {r['detail']}")
        except Exception as e:
            HEARTBEAT["last_error"] = f"{type(e).__name__}: {e}"
            HEARTBEAT["last_result"] = None
            log.warning(f"[trim] pass failed: {type(e).__name__}: {e}")
        # Written whether the pass succeeded or threw. A heartbeat that
        # only ticks on success cannot distinguish "not running" from
        # "running and failing", which is the distinction it exists for.
        HEARTBEAT["last_pass_at"] = datetime.utcnow().isoformat() + "Z"
        HEARTBEAT["passes"] += 1
        await asyncio.sleep(CHECK_SECONDS)

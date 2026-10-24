"""One-time correction: take coin Coinbase no longer holds off the XLM and XRP books.

WHAT IS WRONG, measured 2026-10-24 against Coinbase's own fill record.

  XLM-USD (crypto_grid_10)  books 2,730.52237212  owned 1,672.87001782
                            1,057.6523543 claimed and not there
  XRP-USD (crypto_grid_6)   books   855.86121000  owned   813.80789400
                               42.053316 claimed and not there

XLM is almost all one sale the grid never recorded: 1,485.57480018 XLM sold
as a TAKER at $0.210916 on 2026-10-02T18:47:36Z (order 71864c05), $313.33
gross, $2.35 commission. Who placed it is not established - order tagging
began 2026-10-04 - and this file names nobody. 427.95 XLM that was owned but
never slotted offsets part of it. XRP is several unrecorded trades that
partly cancel: the concentration trimmer's market sales of 89.635634 on
09-27 and 72.225551 on 09-28, a 140.316286 maker buy at 2026-10-07T02:48:42Z
that no slice records, and 73.85 units slotted on 10-08 with no XRP fill
that day. invariants.coin_tracked_is_held reports both gaps.

NO MONEY IS MISSING. Coinbase's balances are right; the books are wrong. The
proceeds of those sales were paid at the time. This writes off units, books
no P&L, and moves no cash.

WHY NOT THE EXISTING RECONCILE. startup_fix.apply_reconcile corrects EVERY
branch, and on 2026-10-24 that includes ETH-USD: 0.19440136 ETH on the
books, 0.0 in the census - because it is STAKED, and a staked balance is not
in the Advanced Trade account in any form. The owner still owns that ETH. A
fleet-wide reconcile would delete it from the books. So this names its two
products and nothing else can reach the write.

It REUSES the tested write rather than re-implementing it: the plan is
slice_reconcile.plan and the write is startup_fix.apply_reconcile, handed a
grid status cut down to the two named branches. What is added here are three
refusals the fleet-wide path does not have:

  * a holding that reads ZERO is refused, not written off - zero is what a
    fully staked coin looks like to this key (the ETH case above)
  * a surplus more than UNIT_TOLERANCE over the measured figure is refused -
    something else has changed since 2026-10-24, and a new gap is a
    measurement to check, not an instruction to obey
  * the cost basis written off is capped at MAX_BASIS_USD, well under the
    fleet-wide $2,000 (planned on the live book: $300.07)

IT RUNS WITH STOP_TRADING SET, deliberately. startup_fix pauses under
STOP_TRADING; this does not, because the owner asked for this correction on
2026-10-24 while trading was stopped, and it can neither buy nor sell: no
order path, no cash move, no allocation, level, stop or threshold write.
What it writes is CryptoGridSlice.qty (or deletes a slice row) on the two
named branches, plus its own one-shot marker.

SHIPPED INERT. Nothing happens unless BOOK_FIX_XLM_XRP=yes is set in the
environment, which only the account owner can do. It runs once: the marker
is claimed before any work, a completed run is never repeated, and a run
that could not settle retries on the next boot up to startup_fix's
MAX_ATTEMPTS.

WHAT IT DOES NOT DO. It does not change a branch's allocated_usd. Removing
coin basis raises the part of the allocation that is unspent cash claim, as
the 2026-10-08 reconcile showed; freeing that budget is the right-size,
which is a separate operation with its own switch and is not part of this.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

ARM_ENV = "BOOK_FIX_XLM_XRP"
MARKER_KEY = "book_fix:xlm_xrp:2026-10-24"

PRODUCTS = ("XLM-USD", "XRP-USD")

# tracked minus owned, from invariants.coin_tracked_is_held on 2026-10-24.
# Units, not dollars, so a price move does not change what is allowed.
MEASURED_SHORT_UNITS = {"XLM-USD": 1057.6523543, "XRP-USD": 42.053316}
UNIT_TOLERANCE = 0.02

# The plan on the live book was $235.38 (XLM) + $64.69 (XRP) = $300.07.
MAX_BASIS_USD = 400.0

_LAST = None


def armed():
    """True only for an explicit affirmative. Quotes are stripped, as
    startup_fix.ticket() strips them."""
    raw = (os.getenv(ARM_ENV) or "").strip().strip('"').strip("'").lower()
    return raw in ("1", "true", "yes", "on")


def last_report():
    """What the boot run did, or None if it has not run in this process."""
    return _LAST


class _OnlyThese:
    """The grid, serving a fixed status that holds only the named branches.

    apply_reconcile asks get_grid_status() for the fleet and corrects every
    branch in it. Served this, it can only ever see XLM-USD and XRP-USD - so
    ETH, and every other branch, is out of reach of the write by
    construction rather than by a filter someone could later loosen.
    """

    def __init__(self, grid, status):
        self._grid, self._status = grid, status

    async def get_grid_status(self):
        return self._status

    def __getattr__(self, name):
        return getattr(self._grid, name)


def plan(status, census):
    """What would be written, per product. Changes nothing.

    Returns (writable_status, rows). `writable_status` holds only the
    branches that passed every refusal; `rows` explains every product.
    """
    import slice_reconcile

    wallet = {str(k).upper(): v
              for k, v in ((census or {}).get("held_including_zero") or {}).items()}
    by_pid = {}
    for b in ((status or {}).get("branches") or []):
        if isinstance(b, dict) and b.get("product_id") in PRODUCTS:
            by_pid.setdefault(b["product_id"], b)

    rows, keep = [], []
    for pid in PRODUCTS:
        b = by_pid.get(pid)
        if b is None:
            rows.append({"product_id": pid, "decision": "REFUSED_NO_BRANCH",
                         "detail": "no branch for this product in the grid status"})
            continue
        held = wallet.get(pid.split("-")[0])
        try:
            held = None if held is None else float(held)
        except (TypeError, ValueError):
            held = None
        row = {"product_id": pid, "bot_name": b.get("bot_name"), "held_units": held}
        if held is None:
            row.update(decision="REFUSED_NOT_IN_WALLET_READING",
                       detail="the census does not report this coin - a gap is not a zero")
            rows.append(row)
            continue
        if held <= 0:
            row.update(decision="REFUSED_ZERO_OWNED",
                       detail=("the census reads zero owned. That is what a fully staked "
                               "coin looks like to this key, so it is not written off"))
            rows.append(row)
            continue
        actions, rep = slice_reconcile.plan(b.get("slices") or [], held,
                                            price=b.get("current_price"))
        row.update(tracked_units=rep.get("tracked_units"),
                   surplus_units=rep.get("surplus_units"),
                   cost_basis_removed_usd=rep.get("cost_basis_removed_usd"),
                   market_value_removed_usd=rep.get("market_value_removed_usd"),
                   actions=actions or None)
        if rep.get("status") != "READY":
            row.update(decision="NOTHING_TO_DO", detail=rep.get("detail"))
            rows.append(row)
            continue
        limit = MEASURED_SHORT_UNITS[pid] * (1.0 + UNIT_TOLERANCE)
        if float(rep.get("surplus_units") or 0.0) > limit:
            row.update(decision="REFUSED_LARGER_THAN_MEASURED",
                       limit_units=round(limit, 8),
                       detail=(f"{rep.get('surplus_units'):.8f} units are over-claimed "
                               f"now against {MEASURED_SHORT_UNITS[pid]:.8f} measured on "
                               f"2026-10-24. Something else has changed, so nothing is "
                               f"written off for this product"))
            rows.append(row)
            continue
        row.update(decision="WRITE", detail=rep.get("detail"))
        rows.append(row)
        keep.append(b)
    return {"branches": keep}, rows


async def _same(census):
    return census


async def preview(grid, census_fn=None):
    """GET /book-fix?preview=1. Reads the fleet and the wallet; writes nothing."""
    import startup_fix
    status = await grid.get_grid_status()
    census = await ((census_fn or startup_fix._default_census)())
    if not isinstance(census, dict) or not census.get("available"):
        return {"readable": False, "writes_nothing": True,
                "detail": "the wallet holdings could not be read - a gap is not a zero"}
    _, rows = plan(status, census)
    basis = round(sum(r.get("cost_basis_removed_usd") or 0.0
                      for r in rows if r.get("decision") == "WRITE"), 2)
    return {"readable": True, "writes_nothing": True, "products": rows,
            "cost_basis_it_would_remove_usd": basis,
            "ceiling_usd": MAX_BASIS_USD}


async def run_at_boot(grid, arm=None, census_fn=None):
    """The whole one-shot. Returns a report; never raises into the lifespan."""
    global _LAST
    import startup_fix

    arm = armed() if arm is None else arm
    if not arm:
        _LAST = {"ran": False, "reason": "NOT_ARMED",
                 "detail": f"{ARM_ENV} is not set to yes, so nothing was run."}
        return _LAST
    if grid is None:
        _LAST = {"ran": False, "reason": "NO_GRID_MODULE",
                 "detail": "the grid module is not available in this process."}
        return _LAST

    try:
        may, used = await startup_fix._claim_attempt(grid.get_session_factory, MARKER_KEY)
    except Exception as e:
        _LAST = {"ran": False, "reason": "MARKER_UNREADABLE",
                 "detail": (f"the one-shot marker could not be read or written "
                            f"({type(e).__name__}: {e}), so nothing was run.")}
        log.warning(f"[book-fix] SKIPPED: marker unusable - {type(e).__name__}: {e}")
        return _LAST
    if not may:
        _LAST = {"ran": False,
                 "reason": "ALREADY_DONE" if used == startup_fix.DONE else "ATTEMPTS_EXHAUSTED",
                 "detail": ("the correction already completed, so nothing was run again."
                            if used == startup_fix.DONE else
                            f"it has already spent its {startup_fix.MAX_ATTEMPTS} attempts.")}
        log.warning(f"[book-fix] SKIPPED: {_LAST['reason']}")
        return _LAST

    log.warning(f"[book-fix] RUNNING attempt {used:.0f} of {startup_fix.MAX_ATTEMPTS}")
    out = {"ran": True, "attempt": used, "settled": False, "rows_written": 0}
    _LAST = out
    try:
        status, ready = await startup_fix.status_with(grid, PRODUCTS)
        if not ready:
            out.update(status="FLEET_NOT_READ",
                       detail="the two branches never appeared in the grid status - "
                              "nothing was written, and the next boot will try again.")
            return out
        census = await ((census_fn or startup_fix._default_census)())
        if not isinstance(census, dict) or not census.get("available") \
                or not isinstance(census.get("held_including_zero"), dict) \
                or not census.get("held_including_zero"):
            out.update(status="WALLET_UNREADABLE",
                       detail="the owned-units map could not be read - nothing was written.")
            return out

        writable, rows = plan(status, census)
        out["products"] = rows
        refused = [r for r in rows if r["decision"].startswith("REFUSED")]
        for r in refused:
            await startup_fix._activity(grid, r["product_id"], "BOOK_FIX",
                                        f"NOT corrected: {r['decision']} - {r['detail']}")

        if writable["branches"]:
            rc = await startup_fix.apply_reconcile(
                _OnlyThese(grid, writable), census_fn=lambda: _same(census),
                max_writeoff_usd=MAX_BASIS_USD)
            out["reconcile"] = rc
            out["rows_written"] = rc.get("rows_written") or 0
            wrote_all = rc.get("status") == "APPLIED" and not rc.get("not_applied")
        else:
            wrote_all = True

        out["settled"] = wrote_all and not refused
        out["status"] = "APPLIED" if out["rows_written"] else "NOTHING_WRITTEN"
        if out["settled"]:
            await startup_fix._mark_done(grid.get_session_factory, MARKER_KEY)
        out["marked_done"] = out["settled"]
        out["detail"] = (
            f"{out['rows_written']} slice row(s) written on "
            f"{', '.join(r['product_id'] for r in rows if r['decision'] == 'WRITE') or 'no product'}"
            f"; {len(refused)} product(s) refused. No order was placed and no cash moved."
            + ("" if out["settled"] else " Not settled - the next boot will try again."))
    except Exception as e:
        out.update(status="FAILED", error=f"{type(e).__name__}: {e}",
                   detail="the correction raised, so nothing is claimed for it.")
        log.warning(f"[book-fix] FAILED {type(e).__name__}: {e}")
    log.warning(f"[book-fix] {out.get('status')}: {out.get('detail')}")
    return out

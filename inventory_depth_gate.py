#!/usr/bin/env python3
"""Refuse a NEW buy into a branch whose existing inventory is already past
the point anything has historically recovered from.

Why this file exists
--------------------
Measured 2026-10-10, with no hindsight in the test - only what was knowable
at the moment each purchase was made. For every slice open that day, the
question asked was: when this was bought, did the branch ALREADY hold a
slice past the 119.3h p95 recovery hold?

    bought anyway   19 slices, $1,583.70   - 38% of everything deployed
    clean at buy    43 slices, $2,607.81

XRP alone put $848.51 into four buys, each on top of six or seven slices
that were already past the line. HBAR did it four times, BCH three, XLM
three.

That is the failure mode this gate exists for. Not fees, not spacing, not
entries: the ladder keeps committing fresh cash to branches that have
already stopped working, and free cash becomes inventory that cannot sell.
Measured the same day, 62 of 63 open slices could not sell at a profit.

WHAT THIS IS NOT. It is not a ranking of coins and must never become one.
The fleet's persistence test returned Spearman +0.196, t +0.77 against a
critical 2.131 - branch performance does not predict the next window, and
the system withheld its own ranking on that basis. This gate makes no
claim about which coin will do well. It makes the near-arithmetic claim
that capital which cannot turn over is not working, and refuses to add to
it. See ITEM 4 in the grid lessons for the reallocation proposal that was
rejected on exactly the evidence this one does not need.

What it does and does not do
----------------------------
ONE-DIRECTIONAL. It can only ever refuse a NEW buy. It never sells, never
trims, never resizes, never touches a stop, a spacing, a reserve or any
other threshold. It cannot realise a loss on the owner's behalf, and it
cannot deepen the condition it was built to stop. The owner's standing
instruction is that nothing proposes selling; a gate that only ever
declines to spend honours that by construction.

OFF BY DEFAULT. Unarmed, verdict() returns ALLOW on every input and the
buy path behaves byte-for-byte as it did before this file existed. Arming
is GRID_INVENTORY_DEPTH_GATE=1, and it is the owner's to set.

FAILS OPEN. Unreadable inventory returns ALLOW with a reason saying so -
the same rule branch_backing_verdict follows, and for the same reason: a
gate that freezes the fleet on a missing field is a worse failure than the
one it guards. UNKNOWN is never silently a refusal.

READS THE STORED EXCURSION, NOT THE CURRENT PRICE. mae_pct is the running
minimum of (price/entry)-1 that crypto_grid_bot maintains on every open
slice every cycle (see the excursion block around L8150). Two consequences,
and both are load-bearing:

  * It is the SAME statistic the recovery envelope below is built from, so
    the comparison is like for like. Current drawdown against a
    distribution of maxima compares two different quantities and
    understates risk - a slice that touched -20% and recovered to -9% would
    read healthy when its excursion already exceeded everything that came
    back.

  * It is a high-water mark, so it only ever deepens, and age only grows.
    A branch's verdict can therefore only ratchet toward refusal, never
    flap back and forth. No hysteresis is needed or wanted.

It is at most one cycle (~30-50s) stale, because the buy path reads rows
the previous cycle persisted. That is immaterial against a 119-hour clock.

THE DENOMINATOR IS THE WHOLE ARGUMENT
-------------------------------------
Measured against DEPLOYED BASIS - the summed entry cost of the branch's own
open slices - and deliberately NOT against allocated_usd.

allocated_usd grows by the realised profit on every winning sell
(crypto_grid_bot.py:8647, and :10812 on the close-all path). Measured
against it, a branch's share FALLS because it earned money somewhere else
while its rotten inventory sat untouched, and a branch could un-pause
itself by winning. That is not a price artefact to be smoothed; it is the
wrong ratio.
"""

import os

from env_config import env_float

# ── THE EMPIRICAL RECOVERY ENVELOPE ──────────────────────────────────────
#
# Closed trades that carry an mae_pct AND closed in profit: n = 175,
# measured 2026-10-10 against the live ledger. Every figure in this ladder
# comes from that one subset, which is what "recovered" means.
#
# An earlier draft mixed subsets - a winners-only maximum quoted beside an
# all-trades p99 - which produced a ladder whose p99 exceeded its own
# maximum. Both numbers were real; they were not from the same trades. The
# subset is named on every line here so that cannot recur.
RECOVERED_DEPTH_P95 = 0.0861   # 8.61% under entry
RECOVERED_DEPTH_MAX = 0.1540   # deepest excursion that ever came back
RECOVERED_HOLD_P95 = 119.3     # hours
RECOVERED_HOLD_MAX = 186.6     # longest hold that ever came back

# CONTEXT ONLY, never a threshold: the whole closed book, winners and
# losers together, reaches 23.03% deep (n=190) and 247.8h (n=272). The
# ladder above describes only what recovered, which is the question.

NORMAL, WARN, ALERT = "NORMAL", "WARN", "ALERT"

# JUDGEMENT, NOT MEASUREMENT - and labelled that way on purpose.
#
# Every constant above came from the closed book. This one did not. There
# is no fleet history of alert shares to take a percentile of, so unlike
# the ladder it is an arbitrary line. What would make it empirical:
# persist each branch's alert share daily and, once there is enough
# history, set it from that distribution the way the ladder was set. Until
# then it stays labelled, and it stays the owner's to move.
BUY_PAUSE_THRESHOLD = env_float("GRID_INVENTORY_ALERT_SHARE", 0.25)


def is_armed() -> bool:
    """False unless the owner has armed it. Default OFF.

    Deliberately read at call time rather than captured at import, so the
    owner can arm it with a variable and a restart rather than a code
    change, and so a test can arm it without reloading the module.
    """
    return os.getenv("GRID_INVENTORY_DEPTH_GATE", "").strip().lower() in (
        "1", "true", "yes", "on")


def _field(row, name):
    """One field, from an ORM row or a dict, without caring which.

    The same lesson slice_paid_no_entry_fee learned the hard way: a bare
    getattr on a dict does not raise, it quietly answers the default, so a
    reader handing this a /grid-status payload would have every slice come
    back shallow and the gate would allow everything while appearing to
    work.
    """
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)


def classify_slice(mae_pct, age_hours):
    """(state, why) for one open slice. Pure.

    mae_pct is the STORED running minimum, negative when the slice went
    against the entry. A missing one is UNKNOWN, not shallow: it is judged
    on age alone and the reason says so.
    """
    depth = None if mae_pct is None else -float(mae_pct)
    age = float(age_hours or 0.0)

    deep_alert = depth is not None and depth > RECOVERED_DEPTH_MAX
    deep_warn = depth is not None and depth > RECOVERED_DEPTH_P95

    if deep_alert or age > RECOVERED_HOLD_MAX:
        state = ALERT
    elif deep_warn or age > RECOVERED_HOLD_P95:
        state = WARN
    else:
        state = NORMAL

    why = []
    if deep_alert:
        why.append(f"{depth*100:.2f}% deep, past the {RECOVERED_DEPTH_MAX*100:.2f}% deepest recovery")
    elif deep_warn:
        why.append(f"{depth*100:.2f}% deep, past the {RECOVERED_DEPTH_P95*100:.2f}% p95 of recoveries")
    if age > RECOVERED_HOLD_MAX:
        why.append(f"{age:.1f}h old, past the {RECOVERED_HOLD_MAX:.1f}h longest recovery")
    elif age > RECOVERED_HOLD_P95:
        why.append(f"{age:.1f}h old, past the {RECOVERED_HOLD_P95:.1f}h p95 of recoveries")
    if depth is None:
        why.append("excursion never recorded - judged on age alone, depth UNKNOWN")

    return state, ("; ".join(why) if why else "inside the recovery envelope")


def measure(slices, now_epoch, opened_at_epoch):
    """Read one branch's inventory. Returns a dict, never raises.

    `opened_at_epoch` is supplied by the caller so this module owns no clock
    and no timezone handling - the executor already knows how to read its
    own timestamps, and a second implementation here is a second thing to
    drift.

    `readable` is False when nothing could be measured. That is UNKNOWN, and
    verdict() turns it into an ALLOW, never a refusal.
    """
    rows, unreadable = [], 0
    for s in (slices or []):
        try:
            qty = float(_field(s, "qty") or 0.0)
            entry = float(_field(s, "entry_price") or 0.0)
            opened = opened_at_epoch(_field(s, "opened_at"))
            if qty <= 0 or entry <= 0 or opened is None:
                unreadable += 1
                continue
            age_h = max(0.0, (now_epoch - opened) / 3600.0)
            state, why = classify_slice(_field(s, "mae_pct"), age_h)
            rows.append((state, qty * entry, why))
        except Exception:
            unreadable += 1

    deployed = sum(b for _, b, _ in rows)
    alert = sum(b for st, b, _ in rows if st == ALERT)
    warn = sum(b for st, b, _ in rows if st == WARN)
    return {
        "readable": bool(rows) and deployed > 0,
        "slices_read": len(rows),
        "slices_unreadable": unreadable,
        "deployed_basis_usd": round(deployed, 2),
        "alert_basis_usd": round(alert, 2),
        "warn_basis_usd": round(warn, 2),
        "alert_share_of_deployed": round(alert / deployed, 4) if deployed > 0 else 0.0,
        "reasons": [w for st, _, w in rows if st == ALERT],
    }


def verdict(slices, now_epoch, opened_at_epoch, threshold=None):
    """(allow, reason). The only function the buy path calls.

    ALLOW is returned for: not armed, nothing held, unreadable inventory,
    and a share under the threshold. REFUSE only ever means "this branch's
    own stuck inventory is at or over the share, so do not add to it".
    """
    if not is_armed():
        return True, "inventory-depth gate is not armed (GRID_INVENTORY_DEPTH_GATE unset)"

    thr = BUY_PAUSE_THRESHOLD if threshold is None else float(threshold)

    try:
        m = measure(slices, now_epoch, opened_at_epoch)
    except Exception as e:
        return True, (f"inventory UNREADABLE ({type(e).__name__}) - allowed through. "
                      f"A gate that cannot measure must not refuse.")

    if not m["readable"]:
        return True, (f"inventory UNKNOWN - {m['slices_unreadable']} slice(s) unreadable, "
                      f"nothing measurable. Allowed through; UNKNOWN is not a refusal.")

    share = m["alert_share_of_deployed"]
    if share < thr:
        return True, (f"inventory ok - ${m['alert_basis_usd']:,.2f} of "
                      f"${m['deployed_basis_usd']:,.2f} deployed is past the recovery "
                      f"envelope ({share*100:.1f}%, under the {thr*100:.0f}% line)")

    head = m["reasons"][0] if m["reasons"] else "past the recovery envelope"
    return False, (f"${m['alert_basis_usd']:,.2f} of ${m['deployed_basis_usd']:,.2f} "
                   f"deployed ({share*100:.1f}%) is past anything that has historically "
                   f"recovered, at or over the {thr*100:.0f}% line - no new dollars go in. "
                   f"Worst slice: {head}. Nothing is sold; existing slices sell normally.")

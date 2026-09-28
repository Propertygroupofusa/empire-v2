"""Where did the money go - answered by the check, not by a person digging.

WHY THIS EXISTS. Over 2026-09-27/28 this fleet produced five separate
defects that were all the SAME defect:

    maker rate     cached in memory  vs  persisted          diverged
    fee rate       floor  vs  panel  vs  Coinbase's bill    diverged
    cash           available  vs  available + hold          diverged
    allocation     claimed  vs  coin + cash really there    diverged
    spacing        evidence at 1.37%  vs  real 0.70%        diverged

Two numbers that must agree, computed in two places from two sources, with
nothing forcing them to match. Each was found by a human noticing a figure
and someone digging for an hour.

invariants.py was the first answer and it was not enough. It reported
"$44.27 unaccounted" - true, and still useless: it took an hour of manual
work to learn that was $1.88 of entry commission plus $64.56 of USD locked
behind a resting order. A check that says SOMETHING is wrong without saying
WHICH PART is wrong has not removed the digging, it has only scheduled it.

So this module does not test a number. It states an IDENTITY over named
buckets, and when the identity fails it reports WHICH BUCKET MOVED since
the last time it held. That generalises: it catches the next mechanism
without anyone knowing in advance what the next mechanism is.

    claimed  ==  coin_at_cost + cash_available + cash_on_hold
                 + commission_paid_on_open_slices
                 + residual

Commission is ADDED, which looks wrong for money that has been spent and is
not. cash_available is the wallet as it stands NOW, so the fee is already
gone from it; the claim has not recognised that yet. Adding the fee back on
the backing side is what makes the two sides describe the same moment. The
first draft subtracted it and double-counted every fee - caught by a test
asserting that paying a fee must not move the residual at all.

Every term is measured from its own source. `residual` is what is left over
BY CONSTRUCTION, so it cannot be quietly absorbed - and when it moves, the
per-bucket deltas say what changed underneath it.

THE SELF-CORRECTION IS ONE-DIRECTIONAL. A claim above its backing is
reduced to the backing; a claim below it is never raised. Writing a
allocation UP on the strength of an arithmetic identity would let any
mis-measurement mint budget out of a rounding error. Down is conservative
and matches every other rule in this codebase: the fleet may lose a claim
it cannot prove, never gain one.
"""

# Buckets, in the order they are reported. Named once so a bucket cannot be
# added to the identity and forgotten in the diff, or vice versa.
BUCKETS = (
    "coin_at_cost",        # open slices, entry_price x qty
    "cash_available",      # venue USD that can be spent right now
    "cash_on_hold",        # venue USD locked behind the fleet's resting orders
    "commission_open",     # entry commission already paid on OPEN slices
)

# Below this a difference is two reads of a moving market, not a hole.
TOLERANCE_USD = 5.0

# A residual that moves by more than this between checks is a NEW mechanism,
# not drift, and is worth naming even when the absolute residual is small.
STEP_CHANGE_USD = 10.0


def backed_from(components):
    """The identity's right-hand side.

    Commission is ADDED, not subtracted. cash_available is the wallet as it
    stands now, so the fee has already left it; the claim has not recognised
    that yet, and adding it back is what makes both sides describe the same
    moment. Subtracting it - the obvious reading of "money that is gone" -
    charges every fee twice.
    """
    return (float(components.get("coin_at_cost") or 0.0)
            + float(components.get("cash_available") or 0.0)
            + float(components.get("cash_on_hold") or 0.0)
            + float(components.get("commission_open") or 0.0))


def snapshot(claimed_usd, components, at=None):
    """One reading of the identity, with its residual computed, never stored.

    Any component that could not be read is carried through as None and
    named in `unreadable`. A snapshot with unreadable parts does NOT get a
    residual: a gap is not a zero, and an identity computed over a missing
    term produces a confident wrong answer.
    """
    unreadable = [b for b in BUCKETS if components.get(b) is None]
    out = {"at": at, "claimed_usd": claimed_usd,
           "components": {b: components.get(b) for b in BUCKETS},
           "unreadable": unreadable}
    if claimed_usd is None or unreadable:
        out["backed_usd"] = None
        out["residual_usd"] = None
        return out
    backed = round(backed_from(components), 2)
    out["backed_usd"] = backed
    out["residual_usd"] = round(float(claimed_usd) - backed, 2)
    return out


def explain(previous, current):
    """What moved. Returns a dict a person can act on without digging.

    The whole point: when the residual changes, this names the bucket whose
    change accounts for it, rather than leaving a number to be investigated.
    """
    if current.get("residual_usd") is None:
        return {"status": "UNKNOWN",
                "headline": ("the identity could not be computed - unreadable: "
                             + ", ".join(current.get("unreadable") or ["claimed"])),
                "deltas": {}, "residual_usd": None}

    residual = current["residual_usd"]
    if not previous or previous.get("residual_usd") is None:
        return {"status": "OK" if abs(residual) <= TOLERANCE_USD else "DRIFT",
                "headline": (f"residual ${residual:+,.2f}; no previous reading to "
                             f"compare against, so nothing can be attributed yet"),
                "deltas": {}, "residual_usd": residual}

    deltas = {}
    for b in BUCKETS:
        p, c = previous["components"].get(b), current["components"].get(b)
        if p is None or c is None:
            continue
        deltas[b] = round(float(c) - float(p), 2)
    claim_delta = round(float(current["claimed_usd"]) - float(previous["claimed_usd"]), 2)
    deltas["claimed"] = claim_delta
    residual_delta = round(residual - previous["residual_usd"], 2)

    # The bucket whose movement best accounts for the residual moving. Every
    # bucket enters `backed` positively, so a rise in any of them pushes the
    # residual DOWN - the sign is flipped uniformly rather than special-cased,
    # which is the shape that stopped being wrong once commission's sign was
    # fixed. A per-bucket exception here is a smell worth resisting.
    contrib = {b: -deltas[b] for b in BUCKETS if b in deltas}
    contrib["claimed"] = claim_delta
    driver = max(contrib, key=lambda k: abs(contrib[k])) if contrib else None

    if abs(residual_delta) <= 0.01:
        status = "OK" if abs(residual) <= TOLERANCE_USD else "DRIFT"
        head = (f"residual ${residual:+,.2f}, unchanged since the last check - "
                f"whatever it is, it is not moving")
    elif abs(residual_delta) >= STEP_CHANGE_USD:
        status = "STEP_CHANGE"
        head = (f"residual moved ${residual_delta:+,.2f} to ${residual:+,.2f}, driven by "
                f"{driver} ({deltas.get(driver, 0):+,.2f}). A step change is a NEW "
                f"mechanism, not drift - look at what touched {driver}.")
    else:
        status = "OK" if abs(residual) <= TOLERANCE_USD else "DRIFT"
        head = (f"residual ${residual:+,.2f} (moved ${residual_delta:+,.2f}), "
                f"largest mover {driver} ({deltas.get(driver, 0):+,.2f})")
    return {"status": status, "headline": head, "deltas": deltas,
            "residual_usd": residual, "residual_delta_usd": residual_delta,
            "driver": driver}


def correction_for(branch_claim_usd, branch_backing_usd, tolerance=TOLERANCE_USD):
    """How far a single branch's claim should move. (new_claim, why).

    ONE-DIRECTIONAL. A claim above its backing is reduced to the backing; a
    claim below it is LEFT ALONE. Raising an allocation on the strength of
    an arithmetic identity would let any mis-measurement mint budget out of
    a rounding error - and this codebase has produced a wrong-but-plausible
    measurement twice in one evening. Down is the safe direction: the fleet
    may lose a claim it cannot prove, never gain one.
    """
    if branch_claim_usd is None or branch_backing_usd is None:
        return None, "claim or backing unreadable - a gap is not a zero, so nothing moves"
    claim = float(branch_claim_usd)
    backing = float(branch_backing_usd)
    if backing < 0:
        return None, "negative backing is not a measurement - nothing moves"
    over = claim - backing
    if over <= tolerance:
        return None, (f"claim ${claim:,.2f} is within ${tolerance:,.2f} of its "
                      f"${backing:,.2f} backing - left alone"
                      if over >= 0 else
                      f"claim ${claim:,.2f} is BELOW its ${backing:,.2f} backing - left "
                      f"alone on purpose, this only ever corrects downward")
    return round(backing, 2), (f"claim ${claim:,.2f} exceeds its ${backing:,.2f} backing by "
                               f"${over:,.2f} - reduced to what is actually there")


# The most a single pass may take off the fleet's UNSPENT claims - not off
# its total claim. That distinction is the whole rail.
#
# Written first as a share of the TOTAL claim, and a test caught it being
# decorative: corrections only ever touch the unspent portion, and on this
# fleet unspent is $212 against a $5,369 claim, so a 5%-of-claim limit sat
# at $268 and could never bind. The rail would have been present, tested,
# and incapable of refusing anything.
#
# What it guards is a CASH READING that comes back wrong and low. If the
# wallet reads $0 when it really holds $515, an unguarded pass zeroes every
# unspent claim in one go. At 50% of unspent, a cash read that merely halves
# still corrects - that is plausibly real - while a read that collapses is
# refused and reported as a measurement to check.
MAX_FLEET_CORRECTION_PCT = 0.50


def fleet_corrections(branches, real_cash_usd,
                      max_pct=MAX_FLEET_CORRECTION_PCT, tolerance=TOLERANCE_USD):
    """Reduce over-claims so the fleet's unspent budget fits the real cash.

    branches: dicts with product_id, bot_name, allocated_usd, coin_basis_usd.
    Returns (corrections, report). corrections is [] when nothing should move.

    A BRANCH'S OWN COIN IS UNTOUCHABLE. Its claim is never reduced below its
    slice basis: that coin is real, the branch bought it, and a claim below
    it would have the branch believe it owns less than it holds. Only the
    UNSPENT portion - claim above coin - is a claim on shared cash, and only
    that portion is scaled.

    ONE-DIRECTIONAL. A branch claiming less than its coin (which happens the
    moment it buys a rung, since the buy raises basis and not allocation) is
    LEFT ALONE. Raising a claim on the strength of an arithmetic identity
    would let a mis-measurement mint budget, and this codebase produced a
    wrong-but-plausible measurement twice in one evening.
    """
    if real_cash_usd is None:
        return [], {"status": "UNKNOWN",
                    "detail": "real cash unreadable - a gap is not a zero, nothing moves"}
    cash = float(real_cash_usd)
    if cash < 0:
        return [], {"status": "UNKNOWN",
                    "detail": f"real cash read ${cash:,.2f}, which is not a measurement"}

    rows = []
    claimed_total = 0.0
    unspent_total = 0.0
    for b in branches:
        get = b.get if isinstance(b, dict) else (lambda k, d=None: getattr(b, k, d))
        alloc = get("allocated_usd", None)
        basis = get("coin_basis_usd", None)
        if alloc is None or basis is None:
            # A branch that cannot be measured is skipped, never corrected.
            continue
        alloc = float(alloc)
        basis = float(basis)
        unspent = max(0.0, alloc - basis)
        claimed_total += alloc
        unspent_total += unspent
        rows.append({"bot_name": get("bot_name", None),
                     "product_id": get("product_id", None),
                     "allocated_usd": alloc, "coin_basis_usd": basis,
                     "unspent_usd": round(unspent, 2)})

    if not rows:
        return [], {"status": "UNKNOWN", "detail": "no measurable branches"}

    over = unspent_total - cash
    if over <= tolerance:
        return [], {"status": "OK",
                    "detail": (f"unspent claims ${unspent_total:,.2f} fit inside "
                               f"${cash:,.2f} of real cash"
                               + (f" by ${-over:,.2f}" if over < 0 else "")
                               + " - nothing to correct"),
                    "unspent_claims_usd": round(unspent_total, 2),
                    "real_cash_usd": round(cash, 2)}

    scale = cash / unspent_total if unspent_total > 0 else 0.0
    corrections = []
    for r in rows:
        if r["unspent_usd"] <= 0:
            continue
        new_alloc = r["coin_basis_usd"] + r["unspent_usd"] * scale
        cut = r["allocated_usd"] - new_alloc
        if cut <= 0.01:
            continue
        corrections.append({**r, "new_allocated_usd": round(new_alloc, 2),
                            "reduction_usd": round(cut, 2)})

    total_cut = round(sum(c["reduction_usd"] for c in corrections), 2)
    limit = round(unspent_total * max_pct, 2)
    if total_cut > limit:
        return [], {"status": "REFUSED",
                    "detail": (f"the pass wants to remove ${total_cut:,.2f} of "
                               f"${unspent_total:,.2f} in unspent claims, over the "
                               f"${limit:,.2f} ({max_pct*100:.0f}%) single-pass limit. A "
                               f"correction that large means the cash read collapsed, which "
                               f"is a measurement to check rather than an instruction to "
                               f"obey - nothing moved."),
                    "would_have_cut_usd": total_cut, "limit_usd": limit,
                    "unspent_claims_usd": round(unspent_total, 2),
                    "real_cash_usd": round(cash, 2)}

    return corrections, {
        "status": "CORRECT",
        "detail": (f"unspent claims ${unspent_total:,.2f} exceed ${cash:,.2f} of real cash "
                   f"by ${over:,.2f}; reducing {len(corrections)} branch claim(s) by "
                   f"${total_cut:,.2f} so the fleet stops budgeting money it does not have. "
                   f"No claim goes below the coin that branch actually owns."),
        "total_reduction_usd": total_cut, "limit_usd": limit,
        "unspent_claims_usd": round(unspent_total, 2), "real_cash_usd": round(cash, 2)}

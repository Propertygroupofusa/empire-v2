"""Why a slice has not sold - six answers, only one of which is a bug.

WHY THIS EXISTS, in the words of the mistake it prevents.

The fleet logged 958 sell "expiries" in 720 hours against 42 buys, and that
95.8% was read - by me, out loud, to the account owner - as a jammed exit
engine. It is not. `order_rested` is ZERO on every one of them and the rows
say why themselves: "nothing sellable: available 0.0463890000 floors to 0".

What actually happens is this. A branch places ONE resting sell. That order
LOCKS the coin at the venue. Every cycle afterwards sees only the unlocked
crumb, cannot place anything, and writes an expiry row. ALGO was 56.1% locked
- 1,134 of 2,023 units - behind a single working order. So the counter was
measuring one healthy resting order being polled, over and over, and dressing
it as hundreds of failures.

A statistic that makes a working order look like a broken engine is worse
than no statistic, because it is ACTED ON. It cost an hour of investigation
and nearly cost a change to a sell path that turned out to be correct.

THE QUESTION THIS MODULE ANSWERS INSTEAD. Not "how many cycles failed to
sell", which has no useful denominator - but:

    How many times did the fleet hold a position that was profitable past
    its own floor, unlocked, and had NO working order against it?

That is the only shape an exit defect can take. Everything else is the system
doing what it was told.

THE SIX BUCKETS, and what each one means:

  UNDERWATER              below its own entry net of fees. Nothing is broken;
                          the no-loss rule is refusing to book a loss, which
                          is the rule working. Measured 2026-10-07: 57 of 64
                          slices, $3,500.22.
  PROFITABLE_WORKING      in profit and an order is already resting. Healthy.
                          This is the state the 958 expiries were really in.
  PROFITABLE_LOCKED       in profit, no order of its own, but the coin is
                          committed elsewhere. Do not duplicate the order.
  PROFITABLE_BELOW_MIN    in profit net of FEES but under the account's own
                          minimum net floor. Held on purpose. All three live
                          cases sat at +0.80%, +0.58% and +0.12% against a
                          1.00% floor.
  QUALIFIED_EXIT_MISSING  profitable past the floor, unlocked, no order.
                          *** THE ONLY BUCKET THAT IS A BUG ***
  UNKNOWN_READING         the slice cannot be priced or its balance cannot be
                          read. NOT a defect and NOT a pass - a third verdict.
                          Live, this bucket is exactly the three ZEC slices at
                          $622.05, which is the reconciliation item and not an
                          execution one.

THE INVARIANT, stated once so no reader has to infer it:

    QUALIFIED_EXIT_MISSING > 0 is the ONLY condition that warrants
    investigating the sell engine.

A rising UNDERWATER count is a market. A rising PROFITABLE_WORKING count is
the engine working. A rising UNKNOWN_READING count is an accounting problem.
None of those is a reason to touch execution.

READ-ONLY AND PURE. No database, no network, no venue reach, no write path.
It places no order, cancels none, frees no inventory and changes no slice. It
is given state the system already publishes and returns a classification.
"""

UNDERWATER = "UNDERWATER"
PROFITABLE_WORKING = "PROFITABLE_WORKING"
PROFITABLE_LOCKED = "PROFITABLE_LOCKED"
PROFITABLE_BELOW_MIN = "PROFITABLE_BELOW_MIN"
QUALIFIED_EXIT_MISSING = "QUALIFIED_EXIT_MISSING"
UNKNOWN_READING = "UNKNOWN_READING"

BUCKETS = (UNDERWATER, PROFITABLE_WORKING, PROFITABLE_LOCKED,
           PROFITABLE_BELOW_MIN, QUALIFIED_EXIT_MISSING, UNKNOWN_READING)

# The ONLY bucket that means the sell engine needs looking at.
DEFECT_BUCKET = QUALIFIED_EXIT_MISSING

# GRID_PARKED_MIN_NET_PCT. Passed in by the caller rather than read from the
# environment here, because a classifier that sources its own floor can
# disagree with the engine it is classifying - which is the whole failure
# mode this module exists to stop. No default: a floor nobody supplied is
# UNKNOWN, not 1%.
FLOOR_REQUIRED = ("the net floor a parked sell must clear is REQUIRED. "
                  "Defaulting it would let this module grade the engine "
                  "against a bar the engine does not use.")


def _num(v):
    """A number, or None. A string that will not parse is None, not zero."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def classify_slice(slice_row, holding, floor_pct):
    """One slice -> (bucket, reason). Never raises.

    `holding` is the venue's own reading for that base currency: units,
    available_units, locked_units. OWNED and AVAILABLE are different
    questions and this module needs AVAILABLE - "can this be sold right
    now" - which is the opposite of what a backing check wants.
    """
    qty = _num((slice_row or {}).get("qty"))
    net = _num((slice_row or {}).get("unrealized_net_usd"))
    net_pct = _num((slice_row or {}).get("unrealized_net_pct"))
    floor = _num(floor_pct)
    h = holding or {}
    avail = _num(h.get("available_units"))
    locked = _num(h.get("locked_units"))

    if floor is None:
        return UNKNOWN_READING, FLOOR_REQUIRED
    if qty is None or net is None:
        return UNKNOWN_READING, ("this slice cannot be priced, so whether it "
                                 "should have sold is unanswerable. Not a "
                                 "defect and not a pass.")
    if avail is None:
        return UNKNOWN_READING, ("the venue balance for this currency could "
                                 "not be read, so 'unlocked' is unknown.")

    if net <= 0:
        return UNDERWATER, ("below its own entry net of fees - the no-loss "
                            "rule is refusing to book a loss, which is the "
                            "rule working, not a jam.")

    # In profit from here down.
    if (locked or 0) > 0:
        return PROFITABLE_WORKING, ("an order is already resting against this "
                                    "currency. This is the state that 958 "
                                    "expiry rows were really in.")
    if avail < qty:
        return PROFITABLE_LOCKED, ("in profit, but the coin is committed "
                                   "elsewhere - do not place a second order "
                                   "for units already spoken for.")
    if net_pct is None:
        return UNKNOWN_READING, ("in profit in dollars but its percentage "
                                 "could not be read, so the floor cannot be "
                                 "applied.")
    if net_pct < floor:
        return PROFITABLE_BELOW_MIN, (f"in profit net of fees at "
                                      f"{net_pct * 100:.3f}% but under the "
                                      f"{floor * 100:.2f}% floor. Held on "
                                      f"purpose.")
    return QUALIFIED_EXIT_MISSING, ("profitable past the floor, unlocked, and "
                                    "no working order. This is the only shape "
                                    "an exit defect can take.")


def classify(slices, holdings, floor_pct):
    """Every open slice, bucketed, with the one number that matters on top.

    `holdings` maps base currency -> the venue reading. `slices` carries
    product_id so the base can be derived; a row without one lands in
    UNKNOWN_READING rather than being matched to a guess.
    """
    out = {b: [] for b in BUCKETS}
    for s in (slices or []):
        pid = (s or {}).get("product_id") or ""
        base = pid.split("-")[0] if "-" in pid else None
        h = (holdings or {}).get(base) if base else None
        if base is None:
            bucket, why = UNKNOWN_READING, ("no product id, so no venue "
                                            "balance can be matched to it")
        else:
            bucket, why = classify_slice(s, h, floor_pct)
        out[bucket].append({
            "slice_id": s.get("id"),
            "product_id": pid or None,
            "bot_name": s.get("bot_name"),
            "qty": _num(s.get("qty")),
            "capital_usd": (round(_num(s.get("qty")) * _num(s.get("entry_price")), 2)
                            if _num(s.get("qty")) is not None
                            and _num(s.get("entry_price")) is not None else None),
            "unrealized_net_usd": _num(s.get("unrealized_net_usd")),
            "unrealized_net_pct": _num(s.get("unrealized_net_pct")),
            "opened_at": s.get("opened_at"),
            "why": why,
        })

    counts = {b: len(out[b]) for b in BUCKETS}
    capital = {b: round(sum(r["capital_usd"] or 0.0 for r in out[b]), 2)
               for b in BUCKETS}
    missing = counts[QUALIFIED_EXIT_MISSING]

    return {
        "qualified_exit_opportunities_with_no_order": missing,
        "exit_engine_verdict": ("INVESTIGATE" if missing > 0 else "CLEAN"),
        "investigate_the_sell_engine": bool(missing > 0),
        "counts": counts,
        "capital_usd": capital,
        "slices_total": sum(counts.values()),
        "buckets": out,
        "floor_pct_used": _num(floor_pct),
        "invariant": ("QUALIFIED_EXIT_MISSING > 0 is the ONLY condition that "
                      "warrants investigating the sell engine. A rising "
                      "UNDERWATER count is a market; a rising "
                      "PROFITABLE_WORKING count is the engine working; a "
                      "rising UNKNOWN_READING count is an accounting problem. "
                      "None of those is a reason to touch execution."),
        "not_a_failure_rate": ("This deliberately does NOT publish "
                               "expiries/cycles. That ratio read 95.8% sells "
                               "while order_rested was zero on every row - it "
                               "was counting one resting order being polled, "
                               "not failures. A denominator of cycles has no "
                               "meaning here."),
        "read_only": True,
        "changes_nothing": ("places no order, cancels none, frees no "
                            "inventory, touches no branch and spends no "
                            "dollar."),
    }


def unknown_is_not_clean(result):
    """UNKNOWN_READING is a third verdict and must not read as healthy.

    A clean exit verdict beside a pile of unpriceable slices is how an
    accounting problem gets filed as 'nothing to do here'. Live, that bucket
    is exactly the three ZEC slices at $622.05 - a reconciliation item, not an
    execution one, and it belongs in a different queue rather than in silence.
    """
    n = (result or {}).get("counts", {}).get(UNKNOWN_READING, 0)
    cap = (result or {}).get("capital_usd", {}).get(UNKNOWN_READING, 0.0)
    if not n:
        return None
    return {
        "unreadable_slices": n,
        "unreadable_capital_usd": cap,
        "means": (f"{n} slice(s) carrying ${cap:,.2f} cannot be priced or "
                  f"their balance cannot be read. The exit verdict above is "
                  f"computed WITHOUT them and is not a statement about them. "
                  f"This is an accounting queue, not an execution one."),
    }

"""Is the fleet quiet because the market is quiet, or because it is broken?

THE SIXTEEN DAYS THIS EXISTS TO PREVENT. Between 2026-09-10 and
2026-09-25 the grid closed ZERO round trips. It was not the strategy,
the spacing or the market: the Coinbase JWT was signed wrong, so the
venue answered 401 and - once maker-only was on - "no maker order was
ever placed" (the commit that fixed it, 2026-09-25 20:20). The repo's
own history shows an URGENT "resume trading immediately" on 09-10 and
credential commits still landing on 09-22. Eleven days of not knowing.

At the fleet's own measured best rate that stretch is worth roughly
$4,353 a year. Nothing watched for it, and nothing watches now.

THE DISTINCTION THIS DRAWS, because it is the only one that matters:

  BENIGN  - nothing is within reach. No branch's price has crossed its
            sell trigger and no dip has reached a buy. The fleet is
            doing exactly what a grid does between moves: waiting.
  BROKEN  - the fleet TRIED and the venue said no. Orders refused,
            cycles dying, or a branch sitting PAST its own trigger and
            still not filling. That is money on the floor.

Silence alone is never the alarm - a grid is supposed to be quiet
sometimes, and an alarm that cries on every calm night gets muted, and
then the real one is missed too. The alarm is silence WITH a refusal,
or silence while a branch is past its trigger.

UNKNOWN is a third verdict. If the inputs cannot be read, this says so;
it never reports a healthy fleet from missing data, because "no refusals
seen" and "could not look" are the same silence and opposite facts.
"""

# A grid at a 3% step on coins whose median daily range is 2.71% will
# genuinely go hours without a trade. Six is past normal without being
# so long that a real outage hides inside it.
QUIET_HOURS = 6.0

# One refused order is a venue quirk (dust, a rounding edge). A branch
# refusing over and over is the signature of the JWT outage: the same
# order retried into the same wall.
REFUSALS_THAT_MEAN_BROKEN = 10


def _f(v, default=None):
    try:
        out = float(v)
    except (TypeError, ValueError):
        return default
    return out if out == out else default


def assess(*, hours_since_last_fill, refusals_by_product=None,
           cycle_errors=None, branches=None, readable=True, reason=None):
    """Verdict on a quiet fleet. Never guesses from missing inputs.

    hours_since_last_fill - None when it could not be read.
    refusals_by_product   - {product_id: count} of orders the venue
                            refused, e.g. /grid-status/orders-not-placed.
    cycle_errors          - count of branch cycles that died mid-pass.
    branches              - grid-status branch dicts, to find any branch
                            sitting PAST its own sell trigger.
    """
    if not readable:
        return {"verdict": "UNKNOWN", "alarm": False,
                "reason": reason or "inputs unreadable",
                "this_is_unknown_not_healthy": True}

    hrs = _f(hours_since_last_fill)
    if hrs is None:
        return {"verdict": "UNKNOWN", "alarm": False,
                "reason": "time since the last fill could not be read",
                "this_is_unknown_not_healthy": True}

    refusals = {str(k): int(v) for k, v in (refusals_by_product or {}).items() if v}
    stuck = sorted((p for p, n in refusals.items() if n >= REFUSALS_THAT_MEAN_BROKEN),
                   key=lambda p: -refusals[p])
    errs = int(cycle_errors or 0)

    # A branch whose price is already PAST its sell trigger and has not
    # sold is the sharpest possible signal: the decision was made and the
    # order did not happen. QNT sat 39% past its trigger for hours.
    past_trigger = []
    for b in (branches or ()):
        px, ref = _f(b.get("current_price")), _f(b.get("reference_price"))
        step = _f(b.get("grid_pct"))
        if not (px and ref and step) or not (b.get("slices") or []):
            continue
        over = (px / (ref * (1 + step)) - 1) * 100
        if over > 0:
            past_trigger.append({"product_id": b.get("product_id"),
                                 "pct_past_trigger": round(over, 2)})
    past_trigger.sort(key=lambda r: -r["pct_past_trigger"])

    quiet = hrs >= QUIET_HOURS
    broken = bool(stuck or past_trigger or errs >= REFUSALS_THAT_MEAN_BROKEN)

    if broken:
        bits = []
        if stuck:
            bits.append(f"{len(stuck)} branch(es) had orders refused repeatedly "
                        f"({', '.join(f'{p} x{refusals[p]}' for p in stuck[:3])})")
        if past_trigger:
            t = past_trigger[0]
            bits.append(f"{t['product_id']} is {t['pct_past_trigger']:.1f}% PAST its "
                        f"sell trigger and has not sold")
        if errs >= REFUSALS_THAT_MEAN_BROKEN:
            bits.append(f"{errs} branch cycles died mid-pass")
        return {"verdict": "BROKEN", "alarm": True,
                "hours_since_last_fill": round(hrs, 2),
                "refused_products": stuck, "past_trigger": past_trigger[:5],
                "cycle_errors": errs,
                "reason": "; ".join(bits),
                "why_this_matters": (
                    "The fleet tried and the venue said no. This is the shape of "
                    "2026-09-10 to 09-25, when a mis-signed JWT meant no maker "
                    "order was ever placed and nobody knew for eleven days.")}

    if quiet:
        return {"verdict": "QUIET", "alarm": False,
                "hours_since_last_fill": round(hrs, 2),
                "past_trigger": [], "cycle_errors": errs,
                "reason": (f"no fill for {hrs:.1f}h, and nothing was refused - no "
                           f"branch has reached a trigger. A grid waiting for a move "
                           f"is not a grid that is broken."),
                "quiet_is_not_broken": True}

    return {"verdict": "TRADING", "alarm": False,
            "hours_since_last_fill": round(hrs, 2),
            "cycle_errors": errs,
            "reason": f"last fill {hrs:.1f}h ago"}

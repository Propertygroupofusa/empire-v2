"""Does the coin a branch claims actually exist in the wallet?

THE MEASUREMENT THAT PROMPTED THIS, live 2026-09-30 20:35Z. QNT-USD's
branch reported +$87.90 unrealized - the largest single gain in the
fleet - on 0.675982 units it did not have. The venue held 0.00097323.
Its escape sell had been refused 182 times in 24 hours with
BELOW_BASE_INCREMENT, because 0.00097323 is under QNT's 0.001 minimum,
and every one of those refusals was invisible on the page that showed
the +$87.90.

Fleet-wide at that moment: books claimed $7,053.11 of coin and $1,199.90
of it was not in the wallet. Eight branches were under 50% backed; four
were at 0.000%.

An unrealized gain on coin the venue does not hold is not money. It
cannot be sold at any price, so it is not a position that is "up" - it
is a bookkeeping row. Reporting it beside real gains is how $1,200 of
nothing gets counted as capital, and every decision sized against the
fleet's totals inherits the error.

This module only MEASURES. It moves nothing, sells nothing and
reconciles nothing - correcting the books is reconcile-slices, which is
write-guarded and the account owner's to run.

UNKNOWN IS A THIRD VERDICT. A coin the census could not read is not
"unbacked"; it is unreadable, and it is reported that way. Calling an
unreadable balance a shortfall would manufacture a crisis out of a
rate limit.
"""

# Below this share of claimed units actually held, a branch's unrealized
# gain cannot be taken to the venue and must not be read as money.
BACKED_ENOUGH_PCT = 50.0

# A venue rounds; a branch holding 99.9% of its claim is not a finding.
# Only a gap worth more than this is worth a reader's attention.
MATERIAL_GAP_USD = 5.0


def _f(v, default=0.0):
    try:
        out = float(v)
    except (TypeError, ValueError):
        return default
    return out if out == out else default          # NaN -> default


def assess(branches, held_units_by_asset, *, unreadable_assets=(),
           units_are="available"):
    """Per-branch backing, plus the fleet total.

    `branches` are grid-status branch dicts (product_id, slices with qty
    and entry_price, current_price, total_unrealized_net_usd).
    `held_units_by_asset` maps BASE ASSET -> units held. WHICH units is
    the caller's choice and it changes what the answer MEANS, so
    `units_are` records it and the note says so.
    `unreadable_assets` are assets whose balance could not be read at
    all - reported as UNKNOWN, never folded into the shortfall.

    units_are="available"  -> "can this branch place a sell right now",
        the figure an order is sized against. Excludes coin under a
        resting order and staked balances.
    units_are="owned"      -> "does this coin exist at all". Includes
        coin on hold and staked.

    WHY THIS ARGUMENT EXISTS. This function is called twice per status
    pass with two different maps, and it was writing the SAME note both
    times: "held units are what the venue reports AVAILABLE". So
    `backing_owned` - the block that exists precisely because available
    was the wrong question - carried a note saying it had used
    available. A reader checking which measurement they were looking at
    got the wrong answer from the field put there to tell them.

    That is the same owned-vs-available confusion this whole pair of
    blocks was added to end, surviving in the prose after it had been
    fixed in the numbers.
    """
    _units_are = "owned" if str(units_are).lower().strip() == "owned" else "available"
    # EVERY ROW CARRIES ITS OWN BASIS, not just the block around it.
    #
    # 2026-10-08: a report listed "ALGO 279.353611" among the fleet's
    # shortfalls. ALGO claims 1150.9 units and OWNS 2005.846389 - 174% -
    # and reads short only in the AVAILABLE block, because 1134.3 of its
    # units sit under the fleet's own resting sell order. The block said
    # so correctly in its top-level `note`; the row did not, and the row
    # is what got quoted. That is the same owned-versus-available
    # confusion that forced one retraction already.
    #
    # A row lifted out of its block now still answers for itself, so the
    # wrong number cannot be quoted against the right question.
    _row_question = ("does this coin EXIST at all" if _units_are == "owned"
                     else "can this branch place a sell RIGHT NOW")
    _shortfall_is = (
        "coin that is genuinely not in the account" if _units_are == "owned" else
        "units the venue will not release right now - coin under a resting "
        "order or staked reads short here while the account owns every unit "
        "claimed. NOT evidence the coin is missing; read the owned block for "
        "that question")
    held = {str(k).upper(): _f(v) for k, v in (held_units_by_asset or {}).items()}
    unreadable = {str(a).upper() for a in (unreadable_assets or ())}

    rows, unknown = [], []
    claimed_usd = gap_usd = phantom_gain_usd = 0.0

    for b in (branches or ()):
        pid = str(b.get("product_id") or "")
        if not pid:
            continue
        asset = pid.split("-")[0].upper()
        slices = b.get("slices") or []
        if not slices:
            continue

        claim_units = sum(_f(s.get("qty")) for s in slices)
        if claim_units <= 0:
            continue
        price = _f(b.get("current_price"))
        unreal = _f(b.get("total_unrealized_net_usd"))
        claim_val = round(claim_units * price, 2)
        claimed_usd += claim_val

        if asset in unreadable or asset not in held:
            unknown.append({
                "product_id": pid, "asset": asset,
                "claimed_units": claim_units,
                "claimed_usd": claim_val,
                "reason": ("balance not readable this pass"
                           if asset in unreadable else
                           "asset absent from the balance reading"),
                "this_is_unknown_not_unbacked": True,
                "units_are": _units_are,
                "question": _row_question,
            })
            continue

        have = held[asset]
        pct = round(have / claim_units * 100, 3) if claim_units else 0.0
        short_units = max(0.0, claim_units - have)
        short_usd = round(short_units * price, 2)
        backed = pct >= BACKED_ENOUGH_PCT
        if short_usd >= MATERIAL_GAP_USD:
            gap_usd += short_usd
        # A GAIN ON COIN THAT IS NOT THERE. Counted separately from the
        # shortfall: the shortfall is what the books overstate, this is
        # what the HEADLINE overstates, and they are different questions.
        phantom = (not backed) and unreal > 0
        if phantom:
            phantom_gain_usd += unreal

        rows.append({
            "product_id": pid, "asset": asset,
            # WHICH held figure this row was measured against. Lifted out
            # of its block, a row still answers for itself.
            "units_are": _units_are,
            "question": _row_question,
            "shortfall_is": _shortfall_is,
            "claimed_units": claim_units, "held_units": have,
            "backed_pct": pct, "backed": backed,
            "short_units": short_units, "short_usd": short_usd,
            "unrealized_usd": unreal,
            "unrealized_is_phantom": phantom,
            "can_be_sold": backed,
            "why": (None if backed else
                    f"{pct:.3f}% of the {claim_units:.8f} claimed units are in the "
                    f"wallet. A sell sizes against what the venue will release, so "
                    f"this branch cannot sell what its books say it holds"
                    + (f", and the {unreal:+,.2f} it reports is not money that can "
                       f"be taken." if unreal > 0 else ".")),
        })

    rows.sort(key=lambda r: -r["short_usd"])
    unbacked = [r for r in rows if not r["backed"]]
    return {
        "readable": True,
        "branches_measured": len(rows),
        "claimed_coin_usd": round(claimed_usd, 2),
        "not_in_wallet_usd": round(gap_usd, 2),
        "backed_pct_of_claim": (round((claimed_usd - gap_usd) / claimed_usd * 100, 2)
                                if claimed_usd else None),
        "unbacked_branches": len(unbacked),
        "unbacked": unbacked,
        "phantom_unrealized_usd": round(phantom_gain_usd, 2),
        "rows": rows,
        "unknown": unknown,
        "unknown_count": len(unknown),
        "is_a_measurement_not_a_change": True,
        "units_are": _units_are,
        "measures": (
            "can this branch place a sell RIGHT NOW" if _units_are == "available"
            else "does this coin EXIST at all"),
        "note": ("Claimed units come from open slice rows; held units are what the "
                 + ("venue reports AVAILABLE, which is what an order is sized "
                    "against - coin under a resting order and staked coin are "
                    "excluded, so a branch can read short here while owning every "
                    "unit it claims. "
                    if _units_are == "available" else
                    "account OWNS, including coin on hold under a resting order "
                    "and staked coin. A shortfall here is coin that is genuinely "
                    "not there. ")
                 + "A branch under "
                 f"{BACKED_ENOUGH_PCT:.0f}% cannot sell what its books claim, so any "
                 "gain it reports is not money. Correcting the books is "
                 "reconcile-slices - write-guarded, and not done here."),
        "unknown_is_not_unbacked": ("An asset whose balance could not be read is listed "
                                    "in `unknown` and counted in NEITHER the shortfall "
                                    "nor the backed total."),
    }

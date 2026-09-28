"""Put idle coin under a branch that ALREADY has one.

WHY THIS IS NOT ADOPTION, AND WHY ADOPTION CANNOT DO IT

coin_adoption opens a branch for a coin that has none. Every one of its
paths refuses a coin that is already claimed - `refuse()` returns
ALREADY_CLAIMED, and the writer skips again if a branch appeared since the
plan was sized. That refusal is correct and must stay: two systems on one
balance is the structural gap behind this repo's phantom positions.

But it means adoption is blind to the largest pool of idle money in the
account. Measured 2026-09-27:

    ZEC   $2,284 held, $400 under a branch -> $1,884 idle
    XRP   $2,261 held, $400 under a branch -> $1,861 idle
    BTC   $1,482 held,  $69 under a branch -> $1,413 idle
    ETH   $1,258 held, $400 under a branch ->   $858 idle
    SHIB  $1,082 held, $200 under a branch ->   $882 idle
                                     idle total $7,074

Every one of those coins HAS a branch, so adoption skips all of them, and
raising the adoption cap would not move a cent of it. The branch is simply
smaller than the coin behind it. This module closes that specific gap and
nothing else.

spread_capital_evenly cannot do it either: it moves CASH between FLAT
branches, and all five hold open slices, so it files them under
"untouched_holding" and leaves them alone. Its top-up raises allocated_usd
from free cash; this one raises it by putting owned COIN behind it. Those
are different operations and conflating them is how an unbacked branch
gets created.

WHAT IT WRITES, AND THE FOUR THINGS THAT MUST HOLD

  allocated_usd grows by EXACTLY the market value of the slices written,
  computed from the slices actually produced rather than the requested
  figure, so a rounding difference cannot open a gap. Both sides of the
  backing ledger move together or nothing moves.

  num_levels grows by EXACTLY the number of new slices. The branch was
  full and stays full, and run_grid_branch_cycle only buys when
  len(slices) < num_levels - so it still physically cannot buy until it
  has sold, and the cash for every rebuy still comes from a sale it made.
  Raise num_levels by more and the branch starts trying to buy rungs with
  cash nobody earmarked to it.

  peak_equity grows by the same amount. Equity is allocated_usd plus
  unrealized, and a slice entered at the current price contributes zero
  unrealized, so equity jumps by exactly the added allocation. The cycle's
  ratchet would lift the peak on its own at the next pass; it is set here
  anyway so the stored row is never momentarily inconsistent with itself.

  Only a branch that ALREADY carries stop_loss_pct_override is eligible.
  That column is written by adoption alone, and it marks a branch whose
  entries are adoption prices rather than prices someone paid. Adding coin
  at today's price to a branch trading under a real cost-basis stop would
  put the fleet stop 8% below an entry nobody paid - the exact hazard the
  override exists to prevent. That is why BTC-USD is refused here: its
  branch is an original with a real basis, and topping it up would change
  what its stop means for the slices that were really bought.

THE CAP IS DIFFERENT IN KIND FROM ADOPTION'S, AND THAT IS THE POINT

Adoption's cap did not bind because every hourly pass found NEW coins and
got a fresh budget. This one is bounded by the coin itself: idle is
`held - allocated`, so each top-up shrinks the thing that feeds it, and
total exposure can never exceed coin already owned. Nothing here can buy.
The per-pass ceiling below is therefore a RAMP, not a safety limit - it
exists so the first pass can be read before the rest follows.
"""
from __future__ import annotations

import os as _os

import coin_adoption


def _env_float(name, default):
    try:
        v = float(_os.getenv(name, "") or default)
        return v if v >= 0 else default
    except (TypeError, ValueError):
        return default


# A ramp, not a limit - see the docstring. At $2,000 a pass the $7,074
# measured above goes to work over four hourly passes, so the first one is
# observable before the rest of it follows.
MAX_TOTAL_TOPUP_USD = _env_float("COIN_TOPUP_MAX_TOTAL_USD", 2000.0)
# Per coin per pass. Deliberately large: the real per-coin bound is the
# holding, and a small one here would only slow the ramp unevenly.
MAX_PER_COIN_USD = _env_float("COIN_TOPUP_MAX_PER_COIN_USD", 2500.0)

MIN_TOPUP_USD = coin_adoption.MIN_ADOPT_USD
MIN_TRADE_USD = coin_adoption.MIN_TRADE_USD
LEVELS = coin_adoption.LEVELS
MAX_POSITION_SHARE_PCT = coin_adoption.MAX_POSITION_SHARE_PCT


def _pos(v):
    return coin_adoption._pos(v)


def eligible(branch):
    """None when this branch may be topped up, else the reason it may not."""
    if not hasattr(branch, "get"):
        return "NOT_A_BRANCH_ROW"
    if not branch.get("active", True):
        return "BRANCH_IS_INACTIVE"
    if branch.get("locked"):
        return "BRANCH_IS_LOCKED"
    # The marker that this branch trades on adoption prices. NULL means an
    # original branch with a real cost basis - see the docstring.
    if branch.get("stop_loss_pct_override") is None:
        return "NOT_AN_ADOPTED_BRANCH"
    return None


def plan(holdings, branches, *, account_total_usd,
         max_total_usd=None, max_per_coin_usd=None,
         min_usd=None, levels=LEVELS):
    """What would be added to which branch, and every refusal beside it.

    `holdings` are census rows (asset, units, price, usd, and
    available_units when the venue reports it). `branches` are grid-status
    branch rows. Nothing here reads the network or the database.
    """
    total = _pos(account_total_usd)
    max_total_usd = MAX_TOTAL_TOPUP_USD if max_total_usd is None else max_total_usd
    max_per_coin_usd = MAX_PER_COIN_USD if max_per_coin_usd is None else max_per_coin_usd
    min_usd = MIN_TOPUP_USD if min_usd is None else min_usd

    held = {}
    for h in (holdings or ()):
        if hasattr(h, "get") and h.get("asset"):
            held[str(h["asset"]).upper()] = h

    rows, refusals = [], []
    for b in (branches or ()):
        if not hasattr(b, "get"):
            continue
        pid = str(b.get("product_id") or "")
        asset = pid.split("-")[0].upper()
        why = eligible(b)
        if why:
            refusals.append({"product_id": pid, "asset": asset, "reason": why})
            continue
        h = held.get(asset)
        if h is None:
            refusals.append({"product_id": pid, "asset": asset,
                             "reason": "COIN_NOT_IN_THE_CENSUS"})
            continue
        held_usd = _pos(h.get("usd"))
        price = _pos(h.get("price"))
        if held_usd is None or price is None:
            refusals.append({"product_id": pid, "asset": asset, "reason": "NO_PRICE"})
            continue
        allocated = float(b.get("allocated_usd") or 0.0)
        # Floored: allocated_usd grows with realized profit, so it can
        # legitimately exceed the coin behind it. That is a branch with
        # nothing idle, not a negative amount of idle coin.
        idle_usd = round(max(0.0, held_usd - allocated), 2)
        if idle_usd < min_usd:
            refusals.append({"product_id": pid, "asset": asset, "held_usd": held_usd,
                             "allocated_usd": round(allocated, 2), "idle_usd": idle_usd,
                             "reason": "NO_IDLE_COIN"})
            continue
        # The units the branch ALREADY tracks, summed from its own slices.
        # Exact, and the only figure that can answer "how much of this coin
        # is not yet under a branch" - see the note where it is used.
        tracked_units = 0.0
        for _s in (b.get("slices") or []):
            _q = _pos(_s.get("qty") if hasattr(_s, "get") else getattr(_s, "qty", None))
            if _q:
                tracked_units += _q
        rows.append({"product_id": pid, "asset": asset, "bot_name": b.get("bot_name"),
                     "held_usd": held_usd, "allocated_usd": round(allocated, 2),
                     "tracked_units": tracked_units,
                     "idle_usd": idle_usd, "price": price,
                     "num_levels": int(b.get("num_levels") or 0),
                     "available_units": h.get("available_units"),
                     "units": h.get("units"),
                     "share_pct": round(held_usd / total * 100, 2) if total else None})

    # Most idle first: the branch with the most coin sitting still is the
    # one where a pass of this ramp buys the most information.
    rows.sort(key=lambda r: -r["idle_usd"])

    out, spent = [], 0.0
    for r in rows:
        room = min(max_per_coin_usd, max_total_usd - spent)
        if room < min_usd:
            refusals.append({"product_id": r["product_id"], "asset": r["asset"],
                             "idle_usd": r["idle_usd"], "reason": "RAMP_EXHAUSTED_THIS_PASS"})
            continue

        # SIZE AGAINST WHAT THE VENUE WILL RELEASE, NOT WHAT IS OWNED.
        # The same rule adoption learned the expensive way: a resting stop
        # or a stake can hold units that the holding's USD value still
        # counts, and a branch that believes it owns locked coin fails
        # every sell it places. available_units is authoritative when the
        # census reports it; `units` is the fallback for a venue that does
        # not distinguish, never an assumption that everything is free.
        # MISSING IS NOT THE SAME AS ZERO, AND ZERO IS THE COMMON CASE.
        #
        # A fully staked coin reports available_units = 0.0. Running that
        # through a positive-only helper turns it into None, which then
        # takes the "the census did not tell us" fallback and deploys every
        # staked unit - the precise failure this whole path exists to stop.
        # ADA, ATOM, AVAX and XTZ are all 100% staked on this account, so
        # this is not a hypothetical branch of the code.
        raw_avail = r["available_units"]
        if raw_avail is None:
            # Genuinely absent: fall back to units, which is all the census
            # offered before it reported the split.
            avail_units = _pos(r["units"])
            if avail_units is None:
                refusals.append({"product_id": r["product_id"], "asset": r["asset"],
                                 "reason": "NO_AVAILABLE_UNITS"})
                continue
        else:
            try:
                avail_units = max(0.0, float(raw_avail))
            except (TypeError, ValueError):
                refusals.append({"product_id": r["product_id"], "asset": r["asset"],
                                 "reason": "NO_AVAILABLE_UNITS"})
                continue
            if avail_units <= 0:
                refusals.append({"product_id": r["product_id"], "asset": r["asset"],
                                 "held_usd": r["held_usd"], "idle_usd": r["idle_usd"],
                                 "reason": "ALL_UNITS_LOCKED_OR_STAKED"})
                continue
        # THE UNITS ALREADY UNDER THE BRANCH, MEASURED - NOT ESTIMATED.
        #
        # This was `allocated_usd / price`: a cost-basis dollar figure
        # divided by TODAY's price. Those are different quantities. When
        # the price has risen since the slices were written, the quotient
        # UNDERSTATES the units already tracked, and the difference is
        # handed back as "free" and registered a second time. The branch
        # then claims more coin than the wallet holds, and a sale of those
        # slices is an order for units that do not exist.
        #
        # Measured 2026-09-28. PEPE-USD was adopted at 12:17 on 09-27
        # (20.23M units), bought 9.10M through the venue at 02:58, then
        # topped up AGAIN at 03:08 for 18.05M - reaching 47.39M tracked
        # against 39.09M held. BCH-USD shows the same shape: three adopted
        # slices, one real buy, and 0.379323 units short. Both were topped
        # up; neither was ever trimmed, which is why the trimmer did not
        # explain them.
        #
        # The branch's own slices are the exact answer and are already in
        # the payload. Falls back to the old estimate only when a branch
        # reports no slices at all, so a payload without them behaves as
        # it did rather than treating every held unit as free.
        tracked_units = r.get("tracked_units")
        if tracked_units:
            units_under_branch = tracked_units
        else:
            units_under_branch = (r["allocated_usd"] / r["price"]) if r["price"] else 0.0
        free_units = max(0.0, avail_units - units_under_branch)
        free_usd = round(free_units * r["price"], 2)

        add_usd = round(min(r["idle_usd"], free_usd, room), 2)
        if add_usd < min_usd:
            refusals.append({"product_id": r["product_id"], "asset": r["asset"],
                             "idle_usd": r["idle_usd"], "free_usd": free_usd,
                             "reason": "TOO_LITTLE_AVAILABLE_TO_TRADE"})
            continue

        add_units = min(add_usd / r["price"], free_units)
        lv = coin_adoption.levels_for(add_usd, cap=levels)
        slices = coin_adoption.slice_units(add_units, r["price"], lv)
        if not slices:
            refusals.append({"product_id": r["product_id"], "asset": r["asset"],
                             "reason": "CANNOT_SPLIT_INTO_A_TRADEABLE_SLICE"})
            continue

        # Computed from the slices actually written, never the requested
        # figure - the invariant allocation_backing exists to check.
        backed = round(sum(s["qty"] * s["entry_price"] for s in slices), 2)
        sell_only = bool(r["share_pct"] is not None
                         and r["share_pct"] > MAX_POSITION_SHARE_PCT)
        out.append({
            "product_id": r["product_id"], "asset": r["asset"], "bot_name": r["bot_name"],
            "price": r["price"],
            "add_usd": backed,
            "add_units": round(sum(s["qty"] for s in slices), 12),
            "add_levels": len(slices),
            "slices": slices,
            "allocated_usd_before": r["allocated_usd"],
            "allocated_usd_after": round(r["allocated_usd"] + backed, 2),
            "num_levels_before": r["num_levels"],
            "num_levels_after": r["num_levels"] + len(slices),
            "held_usd": r["held_usd"],
            "idle_usd_before": r["idle_usd"],
            "idle_usd_after": round(max(0.0, r["idle_usd"] - backed), 2),
            "position_share_pct": r["share_pct"],
            "sell_only": sell_only,
            "why_sell_only": (
                f"{r['share_pct']:.1f}% of the account is over the "
                f"{MAX_POSITION_SHARE_PCT:.0f}% rule, so this branch may SELL what it "
                f"holds but never buy more."
                if sell_only else None),
        })
        spent = round(spent + backed, 2)

    return {
        "ok": bool(out),
        "topups": out,
        "coins": len(out),
        "total_usd": round(spent, 2),
        "refusals": refusals,
        "caps": {"max_total_usd": max_total_usd, "max_per_coin_usd": max_per_coin_usd,
                 "min_usd": min_usd,
                 "note": ("a ramp, not a safety limit - idle coin is bounded by the "
                          "holding itself, and nothing here can buy")},
        "buys_nothing": True,
        "sells_nothing": True,
        "is_a_plan_not_a_change": True,
        "detail": (
            (f"Would put ${spent:,.2f} of coin already owned under {len(out)} branch(es) "
             f"that already exist, spending nothing and selling nothing. Each branch's "
             f"claim and the coin behind it move by the same amount, and each stays "
             f"exactly full, so none of them can buy until it has sold."
             if out else
             f"Nothing is toppable right now. {len(refusals)} branch(es) were considered "
             f"and each refusal is listed with its reason.")),
    }

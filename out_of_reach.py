"""Crypto the account holds that the trading API cannot see at all.

THE MEASUREMENT THAT PROMPTED THIS, 2026-10-02. The Coinbase app showed
$13,912.19 of crypto. account-census, reading the Advanced Trade API with
the fleet's own key, totalled $8,135.00 of coin. Queried per currency, the
venue answered:

    ETH    app $2,294.82    trading balance 0.0000000028 units
    SOL    app   $921.59    trading balance 1.0346, available 0.2587
    ATOM   app   $580.37    account EXISTS, available 0.0
    ADA    app   $426.34    account EXISTS, available 0.0

$4,093.14 of the owner's crypto - 29% of it - was staked, and a staked
balance is not in the Advanced Trade account. It is not held, it is not on
hold, it is not a zero that means "spent". The API simply does not report
it, and nothing in this system knew it existed.

WHY THAT IS WORSE THAN A MISSING FEATURE. Every share, every percentage and
every "X% of the account" figure the dashboard prints divides by a total
built from what the API returns. When 29% of the money is outside that
total, each of those figures is overstated and nothing says so. The
concentration ceiling is measured against it. is-it-growing reports
`capital_behind_it_usd` from it. A reader comparing the dashboard to their
phone finds two different accounts and no explanation.

WHAT THIS MODULE WILL AND WILL NOT DO. It cannot measure the staked amount:
the key cannot see it, and no amount of paging the accounts endpoint will
make it appear. So it does not estimate one. It reports UNKNOWN - the third
verdict - and it names the two things it CAN establish:

  1. Which currencies the venue lists with a CONFIRMED ZERO or dust
     trading balance while this fleet runs a branch on them. That is
     exactly where a staked holding hides, and exactly where a branch
     looks broken for a reason that is not the bot's fault.

  2. That every ratio computed from the readable total is an upper bound,
     not a measurement, until the out-of-reach figure is declared.

The out-of-reach total is owner-declared (GRID_OUT_OF_REACH_USD) because
the owner can read it off the app and this process cannot read it at all.
Unset means UNKNOWN. Unset does NOT mean zero, and this module never
substitutes one for the other - that substitution is the entire bug it
exists to document.

It measures only. It stakes nothing, unstakes nothing, sells nothing.
Unstaking is the account owner's to do in the Coinbase app.
"""

import os

# Below this many units, a trading balance is dust, not a position: it
# cannot be sold (every venue has a minimum) and it cannot back a slice.
# A currency at exactly 0.0 and a currency at 2.8e-09 are the same fact
# about whether money is reachable, and ETH was the second one.
DUST_UNITS = 1e-06


def _f(v, default=0.0):
    try:
        out = float(v)
    except (TypeError, ValueError):
        return default
    return out if out == out else default          # NaN -> default


def declared_out_of_reach_usd(env=None):
    """The owner-declared total held outside the trading balance, or None.

    None means UNKNOWN. It is returned as None, never as 0.0, so a caller
    cannot accidentally treat "nobody has told us" as "there is none".
    """
    env = os.environ if env is None else env
    raw = (env.get("GRID_OUT_OF_REACH_USD") or "").strip()
    if not raw:
        return None
    try:
        val = float(raw)
    except ValueError:
        return None
    if val != val or val < 0:
        return None
    return val


def assess(branches, held_including_zero, available_units, *,
           readable_total_usd=None, env=None):
    """What this fleet can and cannot reach.

    `branches` are grid-status branch dicts (product_id).
    `held_including_zero` maps CURRENCY -> total units the venue reports,
    including currencies it lists at exactly 0.0. A currency absent from
    this map was never read: that is UNKNOWN, not zero.
    `available_units` maps CURRENCY -> units the venue will release.
    `readable_total_usd` is the account total built from the API, if known.
    """
    env = os.environ if env is None else env

    if not held_including_zero:
        return {"readable": False,
                "reason": "no balance reading this pass - what is out of "
                          "reach is UNKNOWN, not zero",
                "out_of_reach_usd": None}

    all_units = {str(k).upper(): _f(v) for k, v in held_including_zero.items()}
    avail = {str(k).upper(): _f(v) for k, v in (available_units or {}).items()}

    empty, unread = [], []
    for b in (branches or []):
        pid = str(b.get("product_id") or "")
        if not pid:
            continue
        asset = pid.split("-")[0].upper()
        if asset not in all_units:
            # The venue never mentioned this currency. Saying its balance
            # is zero would invent a finding out of a short read.
            unread.append({"product_id": pid, "asset": asset,
                           "verdict": "UNKNOWN",
                           "why": "the venue's account list did not mention "
                                  f"{asset} on this pass, so its trading "
                                  "balance is unread - not zero"})
            continue
        total = all_units[asset]
        if total > DUST_UNITS:
            continue
        empty.append({
            "product_id": pid,
            "asset": asset,
            "trading_balance_units": total,
            "available_units": avail.get(asset, 0.0),
            "confirmed_zero": total == 0.0,
            "why": (f"the venue lists an {asset} account and reports "
                    f"{total:.12g} units in the trading balance. If the app "
                    f"shows {asset} holdings, that coin is staked or "
                    "otherwise outside the trading balance: this key cannot "
                    "see it and no bot can sell it. Unstaking is done in the "
                    "Coinbase app, not here."),
        })

    empty.sort(key=lambda r: r["asset"])
    declared = declared_out_of_reach_usd(env)
    note = (env.get("GRID_OUT_OF_REACH_NOTE") or "").strip() or None

    out = {
        "readable": True,
        "is_a_measurement_not_a_change": True,
        "branches_measured": len(branches or []),
        "currencies_the_venue_listed": len(all_units),
        "branches_on_an_empty_trading_balance": len(empty),
        "empty_trading_balance": empty,
        "unread": unread,
        "unread_count": len(unread),
        "out_of_reach_usd": declared,
        "out_of_reach_is_declared_not_measured": True,
        "declared_note": note,
        "why_it_cannot_be_measured":
            "A staked balance is not in the Advanced Trade account - not as "
            "held, not as hold, not as a zero. The API does not report it, "
            "so no reading here can total it. The figure is whatever the "
            "Coinbase app shows, declared via GRID_OUT_OF_REACH_USD.",
        "unset_means_unknown_not_zero": True,
    }

    readable = _f(readable_total_usd, None) if readable_total_usd is not None else None
    out["readable_total_usd"] = readable

    if declared is None:
        out["verdict"] = "UNKNOWN"
        out["ratios_from_the_readable_total_are"] = (
            "UPPER BOUNDS, not measurements. Every share and percentage "
            "computed from the readable total divides by a number that "
            "excludes any staked holding, so each one reads higher than the "
            "truth by an amount nothing here knows."
        )
        out["detail"] = (
            f"{len(empty)} branch(es) run on a currency whose trading "
            "balance is empty or dust"
            + (": " + ", ".join(r["asset"] for r in empty) if empty else "")
            + ". How much crypto sits outside the trading balance is UNKNOWN "
              "- declare it with GRID_OUT_OF_REACH_USD to make every "
              "percentage on this dashboard honest."
        )
        return out

    out["verdict"] = "DECLARED"
    if readable is not None and readable > 0:
        true_total = readable + declared
        out["true_total_usd"] = round(true_total, 2)
        out["reachable_share_pct"] = round(100.0 * readable / true_total, 2)
        out["out_of_reach_share_pct"] = round(100.0 * declared / true_total, 2)
        out["detail"] = (
            f"${declared:,.2f} of crypto is held outside the trading "
            f"balance, so the account is ${true_total:,.2f} and only "
            f"${readable:,.2f} of it - {out['reachable_share_pct']}% - can be "
            "traded by anything here. Every share computed from the readable "
            f"total alone is overstated by a factor of "
            f"{true_total / readable:.3f}."
        )
    else:
        out["detail"] = (
            f"${declared:,.2f} of crypto is declared out of reach. The "
            "readable total was not supplied this pass, so the shares are "
            "not computed."
        )
    return out

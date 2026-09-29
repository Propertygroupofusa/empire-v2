"""The shortfall check must be fed a map that can express a real zero.

WHAT WENT WRONG. coin_tracked_is_held was handed census()'s `holdings`
list. This repo's own asset-balance endpoint already documents why that is
the wrong input: census drops assets it cannot price and rolls anything
under the dust threshold into an unnamed count. Its job is "what is this
account worth"; it cannot answer "does this account hold X at all".

The check's rule - a coin absent from the wallet map is UNREADABLE, not
zero - is correct defence given that input. The input was the defect. So
the three LARGEST shortfalls in the fleet were reported as unknown in a
footnote while the headline named only the six smaller ones, live at
01:10Z on 2026-09-29:

    QNT-USD    tracked 0.67598215   held 0.00097323   $149.54 short
    TIA-USD    tracked 111.35       held 0.0          $49.48  short
    PRIME-USD  tracked 36.64        held 0.0          $9.34   short

QNT alone was larger than any coin in the headline ($471.01 across six).

TWO filters were hiding them, and neither is a fault of the filter:
  - the dust threshold, which hid QNT (worth $0.22)
  - `total > 0` in fetch_balances, which cannot represent a real zero and
    so hid TIA and PRIME once they went to exactly nothing

wallet_units_for closes both: fetch_balances for everything with a
balance, a direct per-currency read for anything the map cannot speak for,
and ABSENCE preserved when that read fails - because an unreadable balance
is still not an empty one, and that rule is the whole point.

Behavioural: the helper is pure over plain dicts.
"""
import sys

failures = []


def ok(label, cond, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}{(' - ' + detail) if detail else ''}")
        failures.append(label)


import account_census as ac
import invariants as inv


def bal(held, available=True):
    return {"available": available, "held": dict(held),
            "available_units": dict(held), "pages": 1, "accounts_seen": 9}


print("== the map answers for what it can, and only that ==")

m = ac.wallet_units_for(bal({"ETH": 0.5, "XRP": 100.0}), ["ETH", "XRP"])
ok("assets present in the map come straight through",
   m == {"ETH": 0.5, "XRP": 100.0}, f"got {m}")

m = ac.wallet_units_for(bal({"ETH": 0.5}), ["ETH", "TIA"])
ok("an asset the map cannot speak for and nobody read is ABSENT",
   m == {"ETH": 0.5},
   f"got {m} - inventing a 0.0 here would report a full position as a "
   f"total shortfall, the loudest false alarm available")

# THE CASE THE WHOLE CHANGE EXISTS FOR.
m = ac.wallet_units_for(bal({"ETH": 0.5}), ["ETH", "TIA"],
                        direct={"TIA": (0.0, None)})
ok("a direct read of 0.0 lands as a CONFIRMED ZERO",
   m == {"ETH": 0.5, "TIA": 0.0},
   f"got {m} - `total > 0` drops a real zero from the map, and a real zero "
   f"is the MAXIMUM possible shortfall")

m = ac.wallet_units_for(bal({"ETH": 0.5}), ["ETH", "TIA"],
                        direct={"TIA": (None, "HTTP 500")})
ok("a FAILED direct read leaves the asset absent, never zero",
   m == {"ETH": 0.5},
   f"got {m} - this is the distinction the check depends on")

ok("an unreadable balance sheet gives None, not an empty map",
   ac.wallet_units_for(bal({}, available=False), ["ETH"]) is None,
   "an empty map would say 'every tracked coin is missing', which is a "
   "fleet-wide false alarm")
ok("and a missing balance sheet does too",
   ac.wallet_units_for(None, ["ETH"]) is None)

m = ac.wallet_units_for(bal({"ETH": 0.5}), ["eth"])
ok("asset keys are matched case-insensitively", m == {"ETH": 0.5}, f"got {m}")

m = ac.wallet_units_for(bal({"ETH": 0.5, "DOGE": 1.0}), ["ETH"])
ok("assets nobody asked about are not carried along",
   m == {"ETH": 0.5},
   f"got {m} - the map is built for the tracked set, not the whole wallet")


print("== the check now classifies the live case correctly ==")

tracked = {"QNT-USD": 0.67598215, "TIA-USD": 111.35, "ETH-USD": 0.147558}
prices = {"QNT-USD": 221.54, "TIA-USD": 0.4444, "ETH-USD": 2672.0}

# BEFORE: the filtered map omits QNT (dust) and TIA (exactly zero).
old_map = {"ETH": 0.11595757}
r = inv.coin_tracked_is_held(tracked, old_map, prices)
ok("with the filtered map, QNT and TIA are merely 'unreadable'",
   sorted(r.get("unreadable") or []) == ["QNT-USD", "TIA-USD"],
   f"got {r.get('unreadable')}")
_short_old = {s["product_id"] for s in (r.get("short_positions") or [])}
ok("and they are NOT counted in the shortfall",
   _short_old == {"ETH-USD"}, f"got {_short_old}")

# AFTER: the unfiltered map plus direct reads can express both.
new_map = ac.wallet_units_for(
    bal({"ETH": 0.11595757, "QNT": 0.00097323}),
    ["ETH", "QNT", "TIA"], direct={"TIA": (0.0, None)})
r2 = inv.coin_tracked_is_held(tracked, new_map, prices)
_short_new = {s["product_id"] for s in (r2.get("short_positions") or [])}
ok("with the unfiltered map, all three are real shortfalls",
   _short_new == {"ETH-USD", "QNT-USD", "TIA-USD"}, f"got {_short_new}")
ok("and nothing is left in the unreadable footnote",
   not (r2.get("unreadable") or []), f"got {r2.get('unreadable')}")

_by = {s["product_id"]: s for s in r2["short_positions"]}
ok("QNT is priced as the largest of them",
   _by["QNT-USD"]["short_usd"] > _by["ETH-USD"]["short_usd"]
   and _by["QNT-USD"]["short_usd"] > _by["TIA-USD"]["short_usd"],
   f"QNT ${_by['QNT-USD']['short_usd']} vs ETH ${_by['ETH-USD']['short_usd']} "
   f"vs TIA ${_by['TIA-USD']['short_usd']} - it was in a footnote")
ok("the reported total grows to include them",
   r2["short_usd"] > (r.get("short_usd") or 0),
   f"{r2['short_usd']} vs {r.get('short_usd')}")

# And the defensive rule must SURVIVE the change: a coin nobody could read
# is still unreadable, not short.
r3 = inv.coin_tracked_is_held(
    tracked,
    ac.wallet_units_for(bal({"ETH": 0.11595757, "QNT": 0.00097323}),
                        ["ETH", "QNT", "TIA"],
                        direct={"TIA": (None, "timeout")}),
    prices)
ok("a coin whose direct read FAILED is still unreadable, not short",
   (r3.get("unreadable") or []) == ["TIA-USD"],
   f"got {r3.get('unreadable')} - the fix must not turn every gap into a "
   f"shortfall; that would be the opposite error")


print()
if failures:
    print(f"{len(failures)} check(s) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("all unfiltered wallet-map checks passed")

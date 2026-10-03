"""Reconcile must read OWNED units, unfiltered - not the dust-filtered list.

THE BUG. census["holdings"] drops every asset worth under DUST_USD ($0.50).
Reconcile built its wallet map from it, so four branches whose balances were
perfectly readable - QNT ($0.24), PEPE ($0.00), TIA (0.0), PRIME (0.0) -
were reported NOT_IN_WALLET_READING and skipped on every single run. QNT
went on showing +$57.44 of unrealised gain on coin it did not hold.

THE SAFETY PROPERTY, which is the more important half. The replacement map
is total OWNED units, not AVAILABLE units. Staked and locked coin is owned
and comes back; writing it off would destroy the record of real holdings.
Measured live: SOL owns 1.0346 with 0.776 staked, LINK owns 6.85 with 6.63
locked. Against AVAILABLE both would have had their slices deleted.
"""
import sys, json
sys.path.insert(0, "/home/user/empire-v2")

fail = 0
def ok(label, cond, detail=""):
    global fail
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + ("" if cond or not detail else f"   -> {detail}"))
    if not cond: fail += 1

src = open("/home/user/empire-v2/routers/trading_dashboard.py").read()
i = src.find('@router.post("/grid-status/reconcile-slices")')
body = src[i:i + 12000]

print("\n[1] the wallet map comes from the unfiltered owned-units map")
ok("it reads held_including_zero", 'census.get("held_including_zero")' in body)
ok("it no longer reads holdings as the primary source",
   body.find('census.get("held_including_zero")') < body.find('census.get("holdings")'),
   "holdings must only appear as the later fallback")
ok("the fallback is still present for an older payload",
   'census.get("holdings") or []' in body)
ok("the fallback requires a real dict, not just truthiness of a key",
   'isinstance(_unfiltered, dict) and _unfiltered' in body)
ok("which map was used is reported", 'out' in body and 'wallet_source' in body)
ok("the fallback names itself as dust-filtered",
   "DUST-FILTERED fallback" in body)

print("\n[2] OWNED, not AVAILABLE - the staked-coin guard")
ok("the endpoint does not reconcile against available_units",
   "available_units" not in body,
   "available_units appears in the reconcile endpoint")
ok("the response states that locked coin is not written off",
   "locked_or_staked_coin_is_not_written_off" in body)

print("\n[3] the behaviour, on the exact live numbers")
import account_census
ok(f"DUST_USD is the ${account_census.DUST_USD:.2f} threshold that hid them",
   account_census.DUST_USD == 0.50, account_census.DUST_USD)

# The real readings taken from the live account.
owned = {"QNT": 0.00097323, "PEPE": 0.06879848, "TIA": 0.0, "PRIME": 0.0,
         "BCH": 0.30825377, "ACH": 5345.20459504, "SOL": 1.03460007,
         "LINK": 6.85}
available = {"QNT": 0.00097323, "PEPE": 0.06879848, "TIA": 0.0, "PRIME": 0.0,
             "BCH": 0.30825377, "ACH": 0.00459504, "SOL": 0.25865002,
             "LINK": 0.22}
claimed = {"QNT": 0.675982153333, "PEPE": 32268212.953682, "TIA": 89.78,
           "PRIME": 36.64, "BCH": 0.687577, "ACH": 15785.46973,
           "SOL": 1.03460007, "LINK": 6.10}
dust_filtered = {a: u for a, u in owned.items() if a not in ("QNT", "PEPE", "TIA", "PRIME")}

def short_against(wallet):
    out = {}
    for a, c in claimed.items():
        h = wallet.get(a)
        out[a] = "UNKNOWN" if h is None else ("short" if h + 1e-12 < c else "backed")
    return out

old = short_against(dust_filtered)
new = short_against(owned)
avail = short_against(available)

for a in ("QNT", "PEPE", "TIA", "PRIME"):
    ok(f"{a}: was UNKNOWN under the dust filter", old[a] == "UNKNOWN", old[a])
    ok(f"{a}: is now seen as short and reconcilable", new[a] == "short", new[a])

print("\n[4] THE GUARD: staked coin must survive")
for a in ("SOL", "LINK"):
    ok(f"{a}: owned in full, so OWNED says backed - left alone", new[a] == "backed", new[a])
    ok(f"{a}: AVAILABLE would have called it short - that is the trap avoided",
       avail[a] == "short", avail[a])
ok("ACH is short on owned units too, so it is still reconcilable",
   new["ACH"] == "short", new["ACH"])
ok("BCH likewise", new["BCH"] == "short", new["BCH"])

print("\n[5] a gap is still not a zero")
ok("an asset in NEITHER map stays UNKNOWN",
   short_against({})["QNT"] == "UNKNOWN")
partial = dict(owned); partial.pop("TIA")
ok("one asset missing does not make the others UNKNOWN",
   short_against(partial)["TIA"] == "UNKNOWN" and short_against(partial)["QNT"] == "short")

print("\n[6] counts, so the change is not silently cosmetic")
n_old = sum(1 for v in old.values() if v == "short")
n_new = sum(1 for v in new.values() if v == "short")
ok(f"reconcilable branches go from {n_old} to {n_new}", n_new == 6 and n_old == 2,
   f"old={n_old} new={n_new}")
ok("and SOL/LINK are NOT among the new ones",
   new["SOL"] == "backed" and new["LINK"] == "backed")

print()
if fail:
    print(f"{fail} FAILED"); sys.exit(1)
print("all checks passed")

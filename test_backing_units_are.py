"""slice_backing.assess must say WHICH units map it was given.

THE BUG THIS PINS

assess() is called twice per status pass with two different maps -
available units for `backing`, owned units for `backing_owned` - and it
wrote the SAME note both times:

    "held units are what the venue reports AVAILABLE"

So `backing_owned`, the block that exists precisely because available was
the wrong question, carried a note saying it had used available. A reader
checking which measurement they were looking at got the wrong answer from
the field put there to tell them. The same owned-vs-available confusion
this pair of blocks was added to end, surviving in the prose after it had
been fixed in the numbers.

Run: python3 test_backing_units_are.py
"""
import sys

import slice_backing

checks = []


def ok(label, cond):
    checks.append((label, bool(cond)))


BRANCHES = [{
    "product_id": "ZEC-USD",
    "current_price": 1316.53,
    "total_unrealized_net_usd": 87.58,
    "slices": [{"id": 1, "qty": 0.378166652152, "entry_price": 1650.61}],
}, {
    # Owns far more than it claims; only locked. The branch the old
    # banner called short.
    "product_id": "ALGO-USD",
    "current_price": 0.131,
    "total_unrealized_net_usd": 1.46,
    "slices": [{"id": 2, "qty": 492.70, "entry_price": 0.1329}],
}]

AVAILABLE = {"ZEC": 0.0, "ALGO": 0.046389}
OWNED = {"ZEC": 0.0, "ALGO": 1347.646389}

a = slice_backing.assess(BRANCHES, AVAILABLE)
o = slice_backing.assess(BRANCHES, OWNED, units_are="owned")

# --- the two answers differ, which is the point ----------------------------
ok("available-based names BOTH branches short", a["unbacked_branches"] == 2)
ok("owned-based names ONLY the one that is really short", o["unbacked_branches"] == 1)
ok("and it is ZEC", [u["product_id"] for u in o["unbacked"]] == ["ZEC-USD"])
ok("ALGO is short on available", any(u["product_id"] == "ALGO-USD" for u in a["unbacked"]))
ok("ALGO is NOT short on owned", not any(u["product_id"] == "ALGO-USD" for u in o["unbacked"]))

# --- each says which map it used -------------------------------------------
ok("available block records units_are=available", a["units_are"] == "available")
ok("owned block records units_are=owned", o["units_are"] == "owned")
ok("the default is available, so the old caller is unchanged",
   slice_backing.assess(BRANCHES, AVAILABLE)["units_are"] == "available")

# --- the note matches the map, which is the actual bug ---------------------
ok("the available note still says AVAILABLE", "AVAILABLE" in a["note"])
ok("the owned note does NOT claim it used available",
   "venue reports AVAILABLE" not in o["note"])
ok("the owned note says it used what the account OWNS", "account OWNS" in o["note"])
ok("the owned note says a shortfall there is genuinely missing coin",
   "genuinely" in o["note"])
ok("the available note warns it can read short on owned coin",
   "while owning every unit it claims" in a["note"])

# --- a plain-language handle on which question each answers ----------------
ok("available says it measures sellability now",
   a["measures"] == "can this branch place a sell RIGHT NOW")
ok("owned says it measures existence",
   o["measures"] == "does this coin EXIST at all")

# --- nothing else moved ----------------------------------------------------
for k in ("readable", "branches_measured", "claimed_coin_usd", "not_in_wallet_usd",
          "backed_pct_of_claim", "unbacked", "phantom_unrealized_usd", "rows",
          "unknown", "unknown_count", "is_a_measurement_not_a_change"):
    ok(f"{k} is still reported", k in a and k in o)
ok("an unreadable units map is still handled",
   slice_backing.assess(BRANCHES, {}, units_are="owned") is not None)
ok("a junk units_are falls back to available, never to owned",
   slice_backing.assess(BRANCHES, AVAILABLE, units_are="banana")["units_are"] == "available")

# --- THE CALLER MUST DECLARE WHICH MAP IT PASSED ---------------------------
#
# assess() defaults to "available", correctly - that keeps the long-standing
# `backing` caller unchanged. But the default is exactly why the owned caller
# dropping its argument is silent: the block keeps working and simply goes
# back to describing itself as available. The first version of this file
# tested assess() alone and passed against that mutation.
import os
import re

ROUTER = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "routers", "trading_dashboard.py")).read()
_owned_call = re.search(
    r"data\[.backing_owned.\]\s*=\s*slice_backing\.assess\((?:[^()]|\([^()]*\))*\)",
    ROUTER)
ok("the backing_owned caller is still there", _owned_call is not None)
ok("and it declares units_are='owned'",
   _owned_call is not None and 'units_are="owned"' in _owned_call.group(0))
ok("it passes the OWNED map, not the available one",
   _owned_call is not None and "_owned" in _owned_call.group(0))

failed = [l for l, c in checks if not c]
for l, c in checks:
    print(f"  {'PASS' if c else 'FAIL'}  {l}")
print(f"\n{len(checks) - len(failed)}/{len(checks)} passed")
sys.exit(1 if failed else 0)

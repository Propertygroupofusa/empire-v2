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
import ast
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


def bal(held, available=True, zeros=None):
    """A fetch_balances/census payload. `held` is the FILTERED map (accounts
    with a balance); `zeros` are currencies the venue lists at exactly 0.0,
    which the filtered map drops and held_including_zero keeps."""
    all_map = dict(held)
    for z in (zeros or ()):
        all_map[z] = 0.0
    return {"available": available, "held": dict(held),
            "available_units": dict(held),
            "held_including_zero": all_map,
            "pages": 1, "accounts_seen": 9}


print("== the map answers for what it can, and only that ==")

m = ac.wallet_units_for(bal({"ETH": 0.5, "XRP": 100.0}), ["ETH", "XRP"])
ok("assets present in the map come straight through",
   m == {"ETH": 0.5, "XRP": 100.0}, f"got {m}")

m = ac.wallet_units_for(bal({"ETH": 0.5}), ["ETH", "TIA"])
ok("an asset the map cannot speak for and nobody read is ABSENT",
   m == {"ETH": 0.5},
   f"got {m} - inventing a 0.0 here would report a full position as a "
   f"total shortfall, the loudest false alarm available")

# THE CASE THE WHOLE CHANGE EXISTS FOR, and it must now cost NO extra read:
# fetch_balances already saw the zero-balance account and was discarding it.
m = ac.wallet_units_for(bal({"ETH": 0.5}, zeros=["TIA"]), ["ETH", "TIA"])
ok("a venue-listed ZERO comes through the payload, with no direct read",
   m == {"ETH": 0.5, "TIA": 0.0},
   f"got {m} - `total > 0` drops a real zero from the filtered map, and a "
   f"real zero is the MAXIMUM possible shortfall")

ok("the unfiltered map is PREFERRED over the filtered one",
   ac.wallet_units_for(bal({"ETH": 0.5}, zeros=["TIA"]), ["TIA"]) == {"TIA": 0.0},
   "reading `held` here would drop the zero and lose the whole point")

# The direct-read fallback still works for an older payload that has no
# held_including_zero at all.
_old_payload = {"available": True, "held": {"ETH": 0.5}}
m = ac.wallet_units_for(_old_payload, ["ETH", "TIA"], direct={"TIA": (0.0, None)})
ok("a payload without the unfiltered map still accepts a direct read",
   m == {"ETH": 0.5, "TIA": 0.0}, f"got {m}")

m = ac.wallet_units_for(_old_payload, ["ETH", "TIA"],
                        direct={"TIA": (None, "HTTP 500")})
ok("a FAILED direct read leaves the asset absent, never zero",
   m == {"ETH": 0.5},
   f"got {m} - this is the distinction the check depends on")

# AND the venue simply not listing the account at all is still absent:
# a currency in neither map and with no direct read cannot be called zero.
m = ac.wallet_units_for(bal({"ETH": 0.5}, zeros=["TIA"]), ["ETH", "TIA", "DOGE"])
ok("a currency the venue never listed stays ABSENT, not zero",
   m == {"ETH": 0.5, "TIA": 0.0},
   f"got {m} - TIA is a confirmed zero, DOGE is an unknown, and collapsing "
   f"them would report a position nobody can see as a total shortfall")

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
    bal({"ETH": 0.11595757, "QNT": 0.00097323}, zeros=["TIA"]),
    ["ETH", "QNT", "TIA"])
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
    ac.wallet_units_for({"available": True,
                         "held": {"ETH": 0.11595757, "QNT": 0.00097323}},
                        ["ETH", "QNT", "TIA"],
                        direct={"TIA": (None, "timeout")}),
    prices)
ok("a coin whose direct read FAILED is still unreadable, not short",
   (r3.get("unreadable") or []) == ["TIA-USD"],
   f"got {r3.get('unreadable')} - the fix must not turn every gap into a "
   f"shortfall; that would be the opposite error")


print("== the payload that feeds it is actually built that way ==")

# THE GAP THESE CLOSE. Every check above hands wallet_units_for a
# hand-built payload, so the code that PRODUCES that payload was never
# exercised - two mutants survived on exactly that: one refiltering the
# zero rows back out of fetch_balances, one dropping the key from census.
# A fixture that constructs the thing under test's input is a fixture that
# cannot test how the input is made.
import asyncio


class _R:
    def __init__(self, payload):
        self._p = payload
        self.status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._p

    async def text(self):
        return ""


class _S:
    def __init__(self, payload):
        self._p = payload

    def get(self, *a, **k):
        return _R(self._p)


_accounts = {"accounts": [
    {"currency": "ETH", "available_balance": {"value": "0.5"},
     "hold": {"value": "0"}},
    # THE ROW THAT MATTERS: the venue lists it, and it holds exactly zero.
    {"currency": "TIA", "available_balance": {"value": "0"},
     "hold": {"value": "0"}},
], "has_next": False}

# No Coinbase credentials in this container, so the real _auth_headers
# raises and fetch_balances returns its error payload. Stubbed so the ROW
# PROCESSING - the part this test is about - actually runs.
_orig_headers = ac._auth_headers
ac._auth_headers = lambda *a, **k: {}
try:
    _bal_real = asyncio.get_event_loop().run_until_complete(
        ac.fetch_balances(_S(_accounts)))
finally:
    ac._auth_headers = _orig_headers

ok("the stubbed fetch actually reached the row processing",
   bool(_bal_real.get("available")),
   f"got {_bal_real} - if this is unavailable the three checks below are "
   f"vacuous, which is how they first passed against a None")

ok("fetch_balances still filters `held` at total > 0",
   _bal_real.get("held") == {"ETH": 0.5},
   f"got {_bal_real.get('held')} - existing callers depend on this meaning")
ok("but it now ALSO emits every account, zeros included",
   _bal_real.get("held_including_zero") == {"ETH": 0.5, "TIA": 0.0},
   f"got {_bal_real.get('held_including_zero')} - these rows were already in "
   f"hand and were being discarded; a real zero is the largest shortfall a "
   f"branch can have")

m = ac.wallet_units_for(_bal_real, ["ETH", "TIA"])
ok("and the two compose end to end", m == {"ETH": 0.5, "TIA": 0.0},
   f"got {m}")

# census must carry it through, or the router is back to a second read.
_census_src = None
for n in ast.walk(ast.parse(open("account_census.py", encoding="utf-8").read())):
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "census":
        _census_src = ast.get_source_segment(
            open("account_census.py", encoding="utf-8").read(), n)
ok("census carries held_including_zero in its payload",
   _census_src is not None and '"held_including_zero"' in _census_src,
   "without it the caller must make a SECOND account read to get the honest "
   "map, which is precisely what got rate-limited")


print("== the check costs ONE account read, not five ==")

# THE TEST THAT WOULD HAVE CAUGHT THE REGRESSION I SHIPPED.
#
# The first version of this fix called fetch_balances again and then
# get_asset_balance once per missing asset. get_asset_balance PAGINATES THE
# WHOLE ACCOUNT LIST to find one currency, so "three extra reads" was really
# about five full account walks per invariants call. Coinbase rate-limited
# them, wallet_units_for got an unavailable payload, and coin_tracked_is_held
# went from FAIL with $471.01 across six positions to UNKNOWN with none.
#
# Blind is worse than under-reported, and nothing in the test suite noticed -
# every check was on the pure helper, none on what the call site costs.
import ast

ROUTER_SRC = open("routers/trading_dashboard.py", encoding="utf-8").read()
ROUTER = ast.parse(ROUTER_SRC)

_block = None
for n in ast.walk(ROUTER):
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
        seg = ast.get_source_segment(ROUTER_SRC, n) or ""
        if "coin_tracked_is_held(" in seg and "grid_inventory_is_free(" in seg:
            _block = seg
ok("the invariants endpoint was located", _block is not None)

if _block is not None:
    sub = ast.parse(_block.strip())

    def _calls(name):
        return sum(1 for c in ast.walk(sub)
                   if isinstance(c, ast.Call)
                   and ((isinstance(c.func, ast.Attribute) and c.func.attr == name)
                        or (isinstance(c.func, ast.Name) and c.func.id == name)))

    ok("it NEVER calls get_asset_balance", _calls("get_asset_balance") == 0,
       "that function paginates the entire account list for ONE currency; "
       "calling it per missing asset is what got rate-limited")
    ok("it reads the account list at most once",
       _calls("fetch_balances") + _calls("census") <= 1,
       f"fetch_balances={_calls('fetch_balances')} census={_calls('census')} - "
       f"census already performs the read and now carries the unfiltered map "
       f"through, so a second call buys nothing and costs a request")
    ok("and it feeds the check the census result, not its holdings list",
       "wallet_units_for(census" in _block,
       "census().holdings is the filtered view that hid the three largest "
       "shortfalls in the first place")


print()
if failures:
    print(f"{len(failures)} check(s) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("all unfiltered wallet-map checks passed")

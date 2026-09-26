"""A claim is only as good as the coin and cash behind it."""

import allocation_backing as A

_passed = _failed = 0


def ok(label, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  -- {detail}" if detail else ""))


def branch(alloc, slices=()):
    return {"allocated_usd": alloc, "slices": list(slices)}


def sl(price, qty):
    return {"entry_price": price, "qty": qty}


# The live fleet, 2026-09-26, exactly as the endpoint returned it.
LIVE = [
    branch(69.23, [sl(84063.99, 8.235e-05), sl(84244.05, 0.0001187)]),
    branch(207.69, [sl(4.9698, 1.392), sl(4.8447, 14.289)]),
    branch(69.23), branch(69.23), branch(69.23), branch(69.23),
]

print("\nthe live account reproduces to the cent")

r = A.backing(LIVE, 79.36)
ok("claimed is the sum of every branch's allocation", r["claimed_usd"] == 553.84, r["claimed_usd"])
ok("deployed coin is entry x qty, not allocated_usd", r["deployed_coin_usd"] == 93.07, r["deployed_coin_usd"])
ok("backed is coin plus wallet", r["backed_usd"] == 172.43, r["backed_usd"])
ok("the hole is $381.41 - within 6c of the bot's own -$381.47",
   abs(r["unbacked_usd"] - 381.41) < 0.01, r["unbacked_usd"])
ok("and the verdict is unbacked, not a shrug", r["verdict"] == "unbacked", r["verdict"])
ok("the detail names all three real figures",
   all(s in r["detail"] for s in ("553.84", "93.07", "79.36")), r["detail"])

print("\ndeployed coin is measured, never taken from the claim itself")

# A branch claiming $1,000 while holding $10 of coin must read as a $990
# hole. Using allocated_usd as the backing would make the check agree with
# itself and always pass - which is exactly how this went unseen.
r = A.backing([branch(1000.0, [sl(10.0, 1.0)])], 0.0)
ok("a $1,000 claim on $10 of coin is a $990 hole",
   r["unbacked_usd"] == 990.0, r["unbacked_usd"])

print("\na sound fleet reads as sound")

r = A.backing([branch(100.0, [sl(50.0, 1.0)])], 50.0)
ok("coin plus cash equal to the claim is 'backed'", r["verdict"] == "backed", r)
ok("and reports no hole", r["unbacked_usd"] == 0.0, r["unbacked_usd"])

r = A.backing([branch(100.0, [sl(50.0, 1.0)])], 48.0)
ok("$2 of drift between two reads is tolerated, not alarmed",
   r["verdict"] == "backed", r)

r = A.backing([branch(100.0, [sl(50.0, 1.0)])], 44.0)
ok("a $6 gap is 'drifting' - named, but not called a hole",
   r["verdict"] == "drifting", r)

r = A.backing([branch(100.0)], 5.0)
ok("a 95% gap is 'unbacked'", r["verdict"] == "unbacked", r)

print("\nan unreadable wallet is not a clean bill of health")

r = A.backing(LIVE, None)
ok("a None balance gives verdict 'unknown', never 'backed'",
   r["verdict"] == "unknown", r["verdict"])
ok("and reports no fabricated backing figure", r["backed_usd"] is None, r)
ok("and says so in words", "not the same as being fine" in r["detail"], r["detail"])

print("\nan unpriceable slice is counted, never silently zeroed")

r = A.backing([branch(100.0, [sl(50.0, 1.0), {"entry_price": None, "qty": 1}])], 0.0)
ok("the bad slice is counted", r["unpriced_slices"] == 1, r)
ok("it does not inflate the hole as a zero-cost slice",
   r["deployed_coin_usd"] == 50.0, r["deployed_coin_usd"])
ok("and the detail warns the real gap is smaller",
   "real gap is smaller" in r["detail"], r["detail"])

print("\nit reads objects as well as dicts - branches arrive both ways")


class B:
    def __init__(self, alloc, slices):
        self.allocated_usd = alloc
        self.slices = slices


class S:
    def __init__(self, p, q):
        self.entry_price = p
        self.qty = q


r = A.backing([B(100.0, [S(50.0, 1.0)])], 50.0)
ok("an attribute-style branch gives the same answer",
   r["verdict"] == "backed" and r["deployed_coin_usd"] == 50.0, r)

print("\nempty and junk inputs do not crash or invent a hole")

ok("no branches at all is not an alarm", A.backing([], 0.0)["verdict"] == "backed")
r = A.backing([branch("junk", [sl("x", "y")])], 0.0)
ok("unparseable figures are skipped rather than raising",
   r["claimed_usd"] == 0.0 and r["unpriced_slices"] == 1, r)

print("\nthe grid status actually carries it")

import ast
import crypto_grid_bot as G

r = G._allocation_backing_block(LIVE, 79.36)
ok("the helper the status calls returns the real verdict",
   r["verdict"] == "unbacked" and r["unbacked_usd"] == 381.41, r)

for junk in ("not branches at all", None, 42, ["a string", "another"]):
    r = G._allocation_backing_block(junk, 79.36)
    ok(f"malformed input {junk!r:24.24} reports 'unknown', never a clean sheet",
       r["verdict"] == "unknown", r)

tree = ast.parse(open(G.__file__).read())
status = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "get_grid_status")
calls = [n for n in ast.walk(status)
         if isinstance(n, ast.Call)
         and getattr(n.func, "id", None) == "_allocation_backing_block"]
ok("get_grid_status calls it exactly once", len(calls) == 1, f"{len(calls)} call(s)")
# It must be fed the rows that carry slices. The ORM branches do not, and
# passing them would report every slice as unpriced and the whole
# allocation as a hole - a false alarm that would train the owner to
# ignore this panel.
ok("and feeds it the rows that carry slices, not the bare ORM branches",
   calls and getattr(calls[0].args[0], "id", None) == "out",
   f"first arg is {getattr(calls[0].args[0], 'id', '?') if calls else 'n/a'}")

assigned = {t.id for n in ast.walk(status) if isinstance(n, ast.Assign)
            for t in n.targets if isinstance(t, ast.Name)}
ok("and reads a real wallet balance to compare against",
   "wallet_cash_usd" in assigned)

print(f"\n{_passed}/{_passed + _failed} checks passed")
raise SystemExit(1 if _failed else 0)

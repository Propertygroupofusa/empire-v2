"""Every slice's target is the exact inverse of the real fee formula.

WHY IT EXISTS. crypto_grid_bot._grid_slice_net_pnl is THE single
fee/profit formula for a slice's round trip, extracted into one function on
the account owner's own instruction so the dashboard and the execution path
could never disagree. A target price computed by a DIFFERENT formula
reintroduces exactly that divergence — so the check that matters here is a
round trip against the real function, not against a restatement of it.

That round trip also measures the spec's own §5 formula, which treats the
fee as a flat addition. The exit leg's fee is charged on the EXIT price,
which is higher than entry, so the approximation asks for too little and
lands under the required edge in the same direction every time.
"""
import sys

import slice_target as st

# The REAL formula, imported rather than restated. If this import ever
# fails the tests must fail with it: a round trip against a local copy of
# the formula would prove nothing at all.
from crypto_grid_bot import _grid_slice_net_pnl

failures = []


def ok(label, cond, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}{(' - ' + detail) if detail else ''}")
        failures.append(label)


REAL = [
    # entry, rate, required edge — real shapes from this fleet
    (100.0, 0.007, 0.012),
    (0.57783, 0.007, 0.012),      # ONDO
    (2654.53, 0.007, 0.010),      # ETH, at the 1.0% parked floor
    (14.343, 0.0075, 0.012),      # LINK
    (0.00002667, 0.007, 0.012),   # FLOKI, a tiny price
    (1659.17, 0.0035, 0.02),      # ZEC, maker-only one leg
]

print("== the target round-trips through the REAL fee formula ==")
for entry, rate, edge in REAL:
    t = st.target_price(entry, rate, edge)
    ok(f"entry={entry} produces a target", t is not None)
    # The claim: selling one unit at that target nets exactly `edge` over
    # basis, per the function the execution path actually uses.
    net = _grid_slice_net_pnl(1.0, entry, t, rate)
    got = net / (1.0 * entry)
    ok(f"  entry={entry} nets exactly {edge} by the real formula",
       abs(got - edge) < 1e-9, f"got {got!r}")
    # And the module's own helper must agree with the real one.
    ok(f"  realised_edge agrees with _grid_slice_net_pnl for {entry}",
       abs(st.realised_edge(entry, t, rate) - got) < 1e-12)

print("== the spec's approximation undershoots, measurably ==")
# target = entry * (1 + edge + cost). Measured against the real formula.
for entry, rate, edge in REAL[:4]:
    approx = entry * (1.0 + edge + rate)
    got = _grid_slice_net_pnl(1.0, entry, approx, rate) / entry
    ok(f"the approximation lands UNDER the required edge at entry={entry}",
       got < edge, f"got {got!r} vs required {edge}")
    exact = st.target_price(entry, rate, edge)
    ok(f"  and the exact target is higher than it at entry={entry}",
       exact > approx)
# It is one-directional, which is what makes it worth fixing.
_shortfalls = [edge - _grid_slice_net_pnl(1.0, e, e * (1 + edge + r), r) / e
               for e, r, edge in REAL]
ok("every shortfall is in the same direction", all(s > 0 for s in _shortfalls),
   str(_shortfalls))

print("== each slice gets its OWN target from its OWN entry ==")
class S:
    def __init__(self, entry_price, rate=None):
        self.entry_price = entry_price
        self._rate = rate

# The spec's own worked example: three slices, three entries.
slices = [S(100.00), S(100.40), S(100.85)]
targets = st.targets_for_slices(slices, 0.012, rate_for=lambda s: 0.007)
ok("three slices produce three targets", len(targets) == 3)
ok("and no two are equal", len(set(targets)) == 3, str(targets))
ok("they rise with the entry price", targets[0] < targets[1] < targets[2])
for s, t in zip(slices, targets):
    got = _grid_slice_net_pnl(1.0, s.entry_price, t, 0.007) / s.entry_price
    ok(f"  entry {s.entry_price} still nets exactly 1.2%", abs(got - 0.012) < 1e-9)

print("== a slice's own fee rate is used, not a shared one ==")
# Once maker and market fills are mixed, two slices of one branch genuinely
# pay different rates. Same entry, different rate, must differ.
mixed = [S(100.0, 0.007), S(100.0, 0.0035)]
mt = st.targets_for_slices(mixed, 0.012, rate_for=lambda s: s._rate)
ok("same entry with different rates gives different targets", mt[0] != mt[1],
   str(mt))
ok("the cheaper leg needs a lower target", mt[1] < mt[0])

print("== an unknown input never becomes a target ==")
for bad in (None, "abc", float("nan"), float("inf")):
    ok(f"entry={bad!r} yields None", st.target_price(bad, 0.007, 0.012) is None)
    ok(f"rate={bad!r} yields None", st.target_price(100, bad, 0.012) is None)
    ok(f"edge={bad!r} yields None", st.target_price(100, 0.007, bad) is None)
ok("a zero entry yields None", st.target_price(0, 0.007, 0.012) is None)
ok("a negative entry yields None", st.target_price(-5, 0.007, 0.012) is None)
# rate >= 2 makes the denominator zero or negative.
ok("a rate of 2 yields None", st.target_price(100, 2.0, 0.012) is None)
ok("a rate above 2 yields None", st.target_price(100, 3.0, 0.012) is None)
ok("a negative rate yields None", st.target_price(100, -0.01, 0.012) is None)
# The critical one for §5: a slice with no computable target must NOT
# inherit another slice's.
mixed2 = st.targets_for_slices([S(100.0), S(None), S(100.85)], 0.012,
                               rate_for=lambda s: 0.007)
ok("a slice with an unknown entry yields None, not a neighbour's target",
   mixed2[1] is None and mixed2[0] is not None, str(mixed2))

print("== a sell target rounds UP to the tick, never down ==")
# Rounding a sell target down gives away edge on every fill.
ok("101.9066 at a 0.01 tick becomes 101.91",
   st.round_target_up(101.90667336, "0.01") == __import__("decimal").Decimal("101.91"))
ok("an exact multiple is unchanged",
   st.round_target_up("101.91", "0.01") == __import__("decimal").Decimal("101.91"))
ok("a tiny price rounds up on its own tick",
   float(st.round_target_up(0.0000271234, "0.00000001")) >= 0.0000271234)
# The rounded target must never realise LESS than the required edge.
for entry, rate, edge in REAL[:4]:
    t = st.target_price(entry, rate, edge)
    r = float(st.round_target_up(t, "0.00000001"))
    got = _grid_slice_net_pnl(1.0, entry, r, rate) / entry
    ok(f"the rounded target still clears the edge at entry={entry}",
       got >= edge - 1e-12, f"got {got!r}")
for bad in (None, 0, "0", -1, "abc"):
    ok(f"increment={bad!r} yields None", st.round_target_up(100, bad) is None)
ok("an unknown price yields None", st.round_target_up(None, "0.01") is None)

print("== it does not reimplement the fee formula ==")
import ast, inspect
tree = ast.parse(inspect.getsource(st))
_fnames = {n.name for n in ast.walk(tree)
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
ok("it defines no _grid_slice_net_pnl of its own",
   "_grid_slice_net_pnl" not in _fnames, str(_fnames))
ok("and no round_down that would shadow resting_stops'",
   "round_down" not in _fnames and "round_price" not in _fnames)

print()
if failures:
    print("FAILED %d check(s): %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("all slice-target checks passed")

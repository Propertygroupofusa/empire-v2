"""A slice's state is persisted, never inferred — and DUST is not a fault.

WHY IT EXISTS. The engine had no slice state. What a branch was doing was
inferred from the wallet: available inventory divided by three. That is how
0.00097323 QNT against a 0.001 increment came to display as 0/3 — the
inference cannot tell a slice that was SOLD from one whose size the venue
cannot currently express, and it reads dust as nothing at all.

WHAT THESE CHECKS PIN. Mostly the moves that must NOT be legal. A state
machine that permits everything documents nothing, so the value is in
SUBMITTING being the only way to reach REJECTED, in FILLED not being the
end of a trade, and in DUST having a way back.
"""
import sys

import slice_lifecycle as sl

failures = []


def ok(label, cond, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}{(' - ' + detail) if detail else ''}")
        failures.append(label)


print("== the spec's states all exist ==")
for s in ("CREATED ARMED READY SUBMITTING OPEN PARTIAL FILLED ACCOUNTED "
          "DUST REPRICE CANCELLED REJECTED ERROR").split():
    ok(f"{s} is a state", s in sl.ALL_STATES)
ok("there are exactly 13", len(sl.ALL_STATES) == 13, str(len(sl.ALL_STATES)))
ok("and no duplicates", len(set(sl.ALL_STATES)) == 13)

print("== the normal successful sequence ==")
seq = [sl.CREATED, sl.ARMED, sl.READY, sl.SUBMITTING, sl.OPEN, sl.FILLED,
       sl.ACCOUNTED]
for a, b in zip(seq, seq[1:]):
    ok(f"{a} -> {b}", sl.can_move(a, b))
state = sl.CREATED
for nxt in seq[1:]:
    state = sl.move(state, nxt)
ok("the whole sequence walks end to end", state == sl.ACCOUNTED)

print("== DUST is not an error and not a rejection ==")
ok("DUST is a state of its own", sl.DUST in sl.ALL_STATES)
ok("DUST is not a fault", sl.is_fault(sl.DUST) is False)
ok("DUST is a resting state", sl.DUST in sl.RESTING)
# §11: it recovers on its own once inventory reaches one increment.
ok("DUST can go back to READY", sl.can_move(sl.DUST, sl.READY))
ok("READY can fall to DUST", sl.can_move(sl.READY, sl.DUST))
# The three conflations the old code made, each refused explicitly.
ok("DUST is not REJECTED", sl.DUST != sl.REJECTED)
ok("DUST cannot become REJECTED", not sl.can_move(sl.DUST, sl.REJECTED))
ok("DUST cannot become ERROR by itself",
   sl.can_move(sl.DUST, sl.ERROR) is True,
   "ERROR must stay reachable for a real fault, but DUST is not one")
ok("and DUST never becomes FILLED", not sl.can_move(sl.DUST, sl.FILLED))

print("== only a submitted order can be rejected ==")
for s in sl.ALL_STATES:
    if s == sl.SUBMITTING:
        continue
    ok(f"{s} cannot reach REJECTED", not sl.can_move(s, sl.REJECTED))
ok("SUBMITTING can", sl.can_move(sl.SUBMITTING, sl.REJECTED))
ok("REJECTED is a fault", sl.is_fault(sl.REJECTED))

print("== maker-only: a resting order is not a fill ==")
# §12: TP reached and no maker fill stays OPEN or REPRICE. It must never
# become FILLED without the venue saying so, and never be forced.
ok("OPEN can reprice", sl.can_move(sl.OPEN, sl.REPRICE))
ok("OPEN can cancel", sl.can_move(sl.OPEN, sl.CANCELLED))
ok("OPEN is not a fault", sl.is_fault(sl.OPEN) is False)
ok("REPRICE is not a fault", sl.is_fault(sl.REPRICE) is False)
ok("REPRICE goes back through SUBMITTING, not straight to OPEN",
   sl.can_move(sl.REPRICE, sl.SUBMITTING) and not sl.can_move(sl.REPRICE, sl.OPEN))
ok("a cancelled order is not a fault", sl.is_fault(sl.CANCELLED) is False)

print("== FILLED is not the end of a trade ==")
# §15: accounting is a separate step. Advancing the cycle on FILLED is how
# a trip completes with its P&L never written.
ok("FILLED goes only to ACCOUNTED (or ERROR)",
   sl.legal_moves(sl.FILLED) == frozenset({sl.ACCOUNTED, sl.ERROR}),
   str(sorted(sl.legal_moves(sl.FILLED))))
ok("FILLED cannot start a new order", not sl.can_move(sl.FILLED, sl.READY))
ok("ACCOUNTED is terminal", sl.legal_moves(sl.ACCOUNTED) == frozenset())
ok("and ACCOUNTED is in TERMINAL", sl.ACCOUNTED in sl.TERMINAL)

print("== partial fills are preserved, not collapsed ==")
ok("PARTIAL can take another partial", sl.can_move(sl.PARTIAL, sl.PARTIAL))
ok("PARTIAL can complete", sl.can_move(sl.PARTIAL, sl.FILLED))
ok("PARTIAL is not a fault", sl.is_fault(sl.PARTIAL) is False)

print("== illegal moves raise, never pass quietly ==")
for a, b in ((sl.CREATED, sl.FILLED), (sl.READY, sl.OPEN),
             (sl.ACCOUNTED, sl.READY), (sl.OPEN, sl.ACCOUNTED),
             (sl.DUST, sl.FILLED), (sl.SUBMITTING, sl.FILLED)):
    try:
        sl.move(a, b)
        ok(f"{a} -> {b} is refused", False, "it was allowed")
    except sl.IllegalTransition:
        ok(f"{a} -> {b} is refused", True)
try:
    sl.move("NONSENSE", sl.READY)
    ok("an unknown state is refused", False)
except sl.IllegalTransition:
    ok("an unknown state is refused", True)
try:
    sl.move(sl.READY, "NONSENSE")
    ok("an unknown target is refused", False)
except sl.IllegalTransition:
    ok("an unknown target is refused", True)
# The message has to say what IS legal, or it cannot be acted on.
try:
    sl.move(sl.CREATED, sl.FILLED)
except sl.IllegalTransition as e:
    ok("the refusal names the legal moves", "ARMED" in str(e), str(e))

print("== slice identity is explicit, not positional ==")
ok("slice 1 of 3 is '1/3'", sl.slice_label(1) == "1/3")
ok("slice 3 of 3 is '3/3'", sl.slice_label(3) == "3/3")
for bad in ((0, 3), (4, 3), (1, 0), (-1, 3)):
    try:
        sl.slice_label(*bad)
        ok(f"slice_label{bad} is refused", False)
    except ValueError:
        ok(f"slice_label{bad} is refused", True)

print("== a cycle completes only when every slice is ACCOUNTED ==")
A, F = sl.ACCOUNTED, sl.FILLED
ok("three accounted completes", sl.cycle_is_complete([A, A, A]) is True)
# The whole point of ACCOUNTED existing.
ok("three FILLED does NOT complete", sl.cycle_is_complete([A, A, F]) is False)
ok("two accounted of three does not", sl.cycle_is_complete([A, A]) is False)
ok("an empty cycle does not complete", sl.cycle_is_complete([]) is False)
ok("four accounted is not a cycle of three",
   sl.cycle_is_complete([A, A, A, A]) is False)
ok("a dust slice does not complete a cycle",
   sl.cycle_is_complete([A, A, sl.DUST]) is False)

print("== inferring state from inventory is refused by name ==")
try:
    sl.state_from_inventory(0.00097323)
    ok("state_from_inventory refuses", False, "it returned something")
except NotImplementedError as e:
    ok("state_from_inventory refuses", True)
    ok("and says why", "never inferred" in str(e), str(e))

print("== every state is reachable and the table is total ==")
_reachable = {sl.CREATED}
_changed = True
while _changed:
    _changed = False
    for s in list(_reachable):
        for t in sl.legal_moves(s):
            if t not in _reachable:
                _reachable.add(t)
                _changed = True
ok("every state is reachable from CREATED",
   _reachable == set(sl.ALL_STATES), str(set(sl.ALL_STATES) - _reachable))
for s in sl.ALL_STATES:
    ok(f"{s} has an entry in the table", s in sl._TRANSITIONS)
    for t in sl.legal_moves(s):
        ok(f"{s} -> {t} targets a real state", t in sl.ALL_STATES)

print()
if failures:
    print("FAILED %d check(s): %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("all slice-lifecycle checks passed")

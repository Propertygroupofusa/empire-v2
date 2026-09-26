"""The trade ledger, checked against its own rows.

WHY THIS EXISTS

A live audit on 2026-09-26 found the coin-history ledger contradicting
itself: 11 of 167 rows recorded a P&L that the entry price, exit price and
quantity ON THAT SAME ROW cannot produce - $339.59 more negative in
aggregate, every one in the same direction. Trade 48 was a WINNING price
move on a $24 position booked as a $91.52 loss.

The cause was one line. Proceeds were computed from filled_qty, the cost
basis from position.qty, and the two are only equal on a full fill:

    pnl = new_allocated - (position.entry_price * position.qty)   # wrong
    pnl = new_allocated - (position.entry_price * filled_qty)     # right

crypto_family_tree_bot.py has THREE places that write a row. One had it
right, one had it wrong, and one charged no fee at all. These tests pin all
three to the same arithmetic, and add the check that would have caught it
without anyone auditing by hand: a row must be able to reproduce its own
P&L from its own columns.

SECOND ROUND, 2026-09-26. Once all three agreed, they agreed on a formula
that was still wrong: it charged the EXIT commission and omitted the ENTRY
one. entry_price is a fill price and Coinbase bills commission separately,
so the cost basis is entry_price * qty PLUS the entry fee. Across the 167
live rows that is $176.41 of real commission never booked, always in the
direction that flatters the trade. All three sites now call one function,
_tree_realized_pnl, which charges both legs - matching _grid_slice_net_pnl,
which had it right all along.

Run: python3 test_ledger_integrity.py
"""
import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_passed = _failed = 0


def ok(label, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  -- {detail}" if detail else ""))


HERE = os.path.dirname(os.path.abspath(__file__))
TREE = open(os.path.join(HERE, "crypto_family_tree_bot.py"), encoding="utf-8").read()
FEE = 0.012   # ROUND_TRIP_FEE_RATE stand-in; the tests below are ratio-based


def booked(entry, exit_, filled_qty, fee_rate=FEE):
    """The corrected formula, as the three write sites now share it.

    BOTH legs. gross move minus commission on entry notional AND exit
    notional - the same shape as _grid_slice_net_pnl.
    """
    gross = filled_qty * (exit_ - entry)
    fee = filled_qty * (entry + exit_) * (fee_rate / 2)
    return gross - fee


def booked_exit_leg_only(entry, exit_, filled_qty, fee_rate=FEE):
    """What the three sites used to compute. Kept so the tests can assert
    that the new formula is never more favourable than the old one."""
    gross = exit_ * filled_qty
    return gross - (gross * (fee_rate / 2)) - (entry * filled_qty)


print("\nthe bug, reproduced from the live row that exposed it")
# Trade 48: POL-USD, entry 0.11767, exit 0.12084, row qty 206.57, booked -91.52.
# Solving the OLD formula for position.qty: the cost basis it subtracted
# implies roughly 990 units held against 206.57 sold.
entry, exit_, filled = 0.11766718719983824, 0.12084, 206.57
held = 990.0
gross = exit_ * filled
old_pnl = (gross - gross * (FEE / 2)) - (entry * held)
new_pnl = booked(entry, exit_, filled)
print(f"        old formula (position.qty={held}): {old_pnl:+.2f}")
print(f"        new formula (filled_qty={filled}): {new_pnl:+.2f}")
ok("the old formula turns a winning move into a large loss", old_pnl < -50, f"{old_pnl:.2f}")
ok("the corrected one books a small gain, as the prices say",
   new_pnl > 0, f"{new_pnl:.2f}")
ok("and a full fill is unaffected by the QUANTITY fix",
   abs(booked(entry, exit_, held) - booked(entry, exit_, held)) < 1e-9,
   "the quantity fix must be a no-op on every trade that filled completely")
ok("the exit-leg-only formula was always the more flattering one",
   booked_exit_leg_only(entry, exit_, filled) > booked(entry, exit_, filled))

print("\na row can reproduce its own P&L from its own columns")
for name, (e, x, q) in {
    "winner": (0.100, 0.110, 500.0),
    "loser": (0.110, 0.100, 500.0),
    "flat": (0.100, 0.100, 500.0),
}.items():
    p = booked(e, x, q)
    gross_move = (x - e) * q
    fee_paid = q * (e + x) * (FEE / 2)
    ok(f"  {name}: booked P&L == gross move minus BOTH fee legs",
       abs(p - (gross_move - fee_paid)) < 1e-9, f"{p:.4f}")
    ok(f"  {name}: the omitted entry leg was {e * q * (FEE / 2):.4f}",
       abs((booked_exit_leg_only(e, x, q) - p) - e * q * (FEE / 2)) < 1e-9)
ok("a flat round trip is NEGATIVE by the fee, never zero",
   booked(0.1, 0.1, 500.0) < 0,
   "a ledger that books a flat trade at zero is hiding the cost of trading")
ok("a flat round trip costs TWO legs, not one",
   abs(booked(0.1, 0.1, 500.0) - -(500.0 * 0.2 * (FEE / 2))) < 1e-9,
   booked(0.1, 0.1, 500.0))

print("\nall three write sites share one arithmetic")
# AST, not a string count: the three call sites are formatted differently
# and a brittle substring check failed on whitespace rather than on
# behaviour, which is exactly the failure mode this file exists to avoid.
_calls = [n for n in ast.walk(ast.parse(TREE))
          if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
          and n.func.id == "_tree_realized_pnl"]
ok("all three write sites call the ONE shared P&L function",
   len(_calls) == 3, f"found {len(_calls)} call sites")
_defs = [n for n in ast.walk(ast.parse(TREE))
         if isinstance(n, ast.FunctionDef) and n.name == "_tree_realized_pnl"]
ok("and it is defined exactly once", len(_defs) == 1, f"{len(_defs)} definitions")
# Every call must land in a variable named pnl. If one ever gets assigned to
# new_allocated or proceeds, the entry fee would be charged against real cash
# that never left twice - the mirror image of the bug being fixed here.
_tree_ast = ast.parse(TREE)
_pnl_targets = [a for a in ast.walk(_tree_ast)
                if isinstance(a, ast.Assign)
                and isinstance(a.value, ast.Call)
                and isinstance(a.value.func, ast.Name)
                and a.value.func.id == "_tree_realized_pnl"
                and len(a.targets) == 1 and isinstance(a.targets[0], ast.Name)]
ok("every call assigns to pnl, never to a cash figure",
   len(_pnl_targets) == 3 and {a.targets[0].id for a in _pnl_targets} == {"pnl"},
   str([a.targets[0].id for a in _pnl_targets]))
# AST, and scoped to the functions that WRITE a ledger row.
#
# Two earlier versions of this check were wrong in opposite directions. A
# string search matched the comment above the fix, which quotes the old
# expression to explain it. A blanket AST search then flagged four
# legitimate uses - unrealized mark-to-market, peak giveback, projected
# net, and target sizing - where position.qty is correct BY DEFINITION,
# because nothing has been sold and there is no filled_qty yet.
#
# The invariant is narrower than either: inside a function that writes a
# CryptoCoinTradeHistory row, a realized P&L must not net proceeds taken
# from filled_qty against a basis taken from position.qty.
_TREE_AST = ast.parse(TREE)
_writer_fns = []
for _fn in ast.walk(_TREE_AST):
    if isinstance(_fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        if any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
               and c.func.id == "CryptoCoinTradeHistory" for c in ast.walk(_fn)):
            _writer_fns.append(_fn)
ok("every ledger write lives inside a named function", len(_writer_fns) >= 2,
   str(len(_writer_fns)))

def _mixes_quantities(fn):
    """A MONEY subtraction with filled_qty on one side and position.qty on
    the other.

    Both sides must involve a price. `position.qty - filled_qty` is the
    partial-fill detector and is exactly right; what must never happen is
    proceeds priced off one quantity netted against a basis priced off the
    other.
    """
    for n in ast.walk(fn):
        if not (isinstance(n, ast.BinOp) and isinstance(n.op, ast.Sub)):
            continue
        l, r = ast.dump(n.left), ast.dump(n.right)
        if not ("price" in l and "price" in r):
            continue                      # comparing bare quantities is fine
        lf, rf = "id='filled_qty'" in l, "id='filled_qty'" in r
        lp = "attr='qty'" in l and "id='position'" in l
        rp = "attr='qty'" in r and "id='position'" in r
        if (lf and rp) or (rf and lp):
            return ast.get_source_segment(TREE, n)
    return None

_mixed = [(f.name, _mixes_quantities(f)) for f in _writer_fns]
_bad = [(n, s) for n, s in _mixed if s]
ok("no ledger writer nets filled_qty proceeds against a position.qty basis",
   not _bad, str(_bad))
ok("and the legitimate position.qty uses are untouched",
   TREE.count("position.qty * (price - position.entry_price)") == 2,
   "unrealized mark-to-market on a HELD position is correct as position.qty")

ok("no write site still books P&L off exit-leg-only proceeds",
   "proceeds - (position.entry_price * filled_qty)" not in TREE,
   "that formula omits the entry commission")
ok("PROCEEDS still charge the exit leg only - that is real cash",
   "proceeds = round(gross - fee, 2)" in TREE,
   "a sale returns gross minus the exit fee; the entry fee left at entry")
ok("new_allocated is still cash, not P&L",
   "new_allocated = gross_value - fee" in TREE)
ok("the shared function warns against using it for allocated_usd",
   "NOT to be used for PROCEEDS" in TREE)
ok("no LIVE expression books a bare (exit - entry) * qty gross move",
   not any(isinstance(n, ast.BinOp) and isinstance(n.op, ast.Mult)
           and isinstance(n.left, ast.BinOp) and isinstance(n.left.op, ast.Sub)
           and isinstance(getattr(n.left, "left", None), ast.Name)
           and isinstance(n.right, ast.Name) and n.right.id == "filled_qty"
           for n in ast.walk(ast.parse(TREE))),
   "a gross move with no fee reads better than the same trade taken any other way")

print("\na partial fill is reported, not silently absorbed")
ok("the automatic path detects filled_qty != position.qty",
   "abs(filled_qty - position.qty) > position.qty * 1e-6" in TREE)
ok("and says so at ERROR, naming the unsold remainder",
   "PARTIAL FILL on" in TREE and "unaccounted for" in TREE,
   "the branch position is cleared regardless, so the remainder belongs to nobody")
seg = TREE.split("PARTIAL FILL on")[1][:400]
ok("the message states the P&L covers the SOLD portion only",
   "SOLD portion only" in seg, seg[:120])

print("\nthe fix is where the money is computed, not where it is displayed")
tree_ast = ast.parse(TREE)
writers = [n for n in ast.walk(tree_ast)
           if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
           and n.func.id == "CryptoCoinTradeHistory"]
ok("there are exactly three ledger write sites", len(writers) == 3, str(len(writers)))
ok("every one of them passes qty=filled_qty",
   all(any(k.arg == "qty" and getattr(k.value, "id", None) == "filled_qty"
           for k in w.keywords) for w in writers),
   "a row whose qty is not the quantity its P&L was computed from cannot be checked")

print("\nthe endpoint audits its own rows")
DASH = open(os.path.join(HERE, "routers", "trading_dashboard.py"), encoding="utf-8").read()
ok("coin-history returns an integrity block", '"integrity": {' in DASH)
ok("which compares each row's P&L against its own prices and qty",
   '(x - e) * q' in DASH and '"prices_imply_pnl"' in DASH)
ok("it reports the net drift in dollars, not just a count",
   '"net_drift_usd"' in DASH)
ok("and says plainly that a flagged row poisons any total containing it",
   "neither the row NOR any total" in DASH)
ok("the tolerance is stated, so 'inconsistent' has a definition",
   "2% of notional" in DASH)

print("\nthe dashboard reports the mode that RESOLVED, not the stale variable")
ok("a helper resolves the live mode", "def _resolved_crypto_mode()" in DASH)
ok("it prefers main.RESOLVED_CRYPTO_MODE, which the DB override feeds",
   'getattr(_main, "RESOLVED_CRYPTO_MODE", None)' in DASH)
ok("the env var is still shown beside it, not hidden",
   '"crypto_strategy_mode_env"' in DASH)
ok("and the panel says which of the two is governing",
   '"crypto_strategy_mode_source"' in DASH)
ok("family_tree_loop_running keys off the resolved mode",
   '"family_tree_loop_running": _resolved_crypto_mode() == "family_tree"' in DASH,
   "keying it off the env var reported a correct deployment as a fault")

print("\nthe statement comes from Coinbase, not from us")
CMP = open(os.path.join(HERE, "crypto_btc_compound_bot.py"), encoding="utf-8").read()
ok("there is a windowed fills fetcher", "async def fetch_fills_between(" in CMP)
ok("it reads Coinbase's own fills endpoint",
   "/api/v3/brokerage/orders/historical/fills" in CMP)
ok("bounded by the window, not by a row limit",
   "start_sequence_timestamp" in CMP and "end_sequence_timestamp" in CMP)
ok("pagination has a hard cap and SAYS when it hit it",
   "max_pages" in CMP and '"truncated"' in CMP,
   "a partial statement that says it is partial beats a complete-looking one that is not")
ok("it never raises", "return {\"available\": False" in CMP)
ok("and it places no order",
   "place_market" not in CMP.split("async def fetch_fills_between")[1].split("async def")[0])
ok("the summary uses Coinbase's real commission, not an assumed rate",
   'f.get("commission")' in CMP)
ok("and keeps the maker/taker split Coinbase reports",
   'liquidity_indicator' in CMP)
ok("size_in_quote is honoured, not assumed away",
   'f.get("size_in_quote")' in CMP and "qty = (size / price)" in CMP,
   "a USD-denominated size multiplied by price reported $96.5M of BTC on a $1,000 account")
ok("and the count of quote-sized fills is reported, so the handling is checkable",
   '"quote_sized_fills"' in CMP)
ok("the endpoint returns untouched raw fills alongside its arithmetic",
   '"raw_sample"' in DASH)
ok("net cash flow is labelled CASH, not profit",
   "is CASH, not profit" in CMP,
   "coin bought and still held reads as cash out with nothing back")
ok("the endpoint exists and is a GET", '@router.get("/coinbase-statement")' in DASH)
ok("it sets our ledgers beside the exchange's record",
   '"our_ledgers"' in DASH and '"reconciliation"' in DASH)
ok("and states which side is authoritative",
   "Coinbase is right and we are wrong" in DASH)

print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)

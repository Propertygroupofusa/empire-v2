"""The owner's Alpaca entry pause: stops NEW positions, never an exit.

WHY THIS TEST IS WRITTEN AT ALL. Three switches already in this codebase
look like they would stop Alpaca trading and each does something the
account owner has ruled out:

  STOP_TRADING            read at the top of crypto_grid_bot's own branch
                          cycle, so it halts the COINBASE grid's buys and
                          its sells too.
  liquidate-and-buy-spy   force-closes every open position before retiring
                          anything.
  passive mode            has no setter of its own, and `continue`s
                          prop_bot's loop BEFORE exit management - the
                          shape of the USO/MCL position that sat unmanaged
                          at -4.9%.

So the one thing this flag must never become is a fourth way to trap an
open position. Every assertion below is ultimately about that.

Run as written: python3 test_alpaca_entries_paused.py
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FAILS = []


def ok(label, cond):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        FAILS.append(label)


def src(name):
    with open(os.path.join(HERE, name)) as f:
        return f.read()


PROP = src("prop_bot.py")
SWING = src("alpaca_swing_bot.py")
ROUTER = src("routers/trading_dashboard.py")

print("the flag exists and is DB-persisted, not an env var")
ok("a distinct state key, not reusing the passive-mode one",
   'ALPACA_ENTRIES_PAUSED_KEY = "alpaca_entries_paused"' in PROP
   and 'ALPACA_PASSIVE_MODE_KEY = "alpaca_passive_mode"' in PROP)
ok("reader and writer both defined",
   "async def are_alpaca_entries_paused()" in PROP
   and "async def set_alpaca_entries_paused(" in PROP)
ok("never read from the environment - a hand-pasted env var is the bug class this avoids",
   "getenv(\"ALPACA_ENTRIES_PAUSED\"" not in PROP
   and "getenv('ALPACA_ENTRIES_PAUSED'" not in PROP)

print("\nit rides entries_halted, which is the exits-stay-live path")
ok("sets entries_halted rather than returning from the cycle",
   "entries_halted = ENTRIES_PAUSED_REASON" in PROP)

tree = ast.parse(PROP)
cycle = next((n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_prop_cycle"), None)
ok("run_prop_cycle is still there to patch", cycle is not None)

if cycle is not None:
    # The pause must not introduce a return/continue that skips the exits.
    seg = ast.get_source_segment(PROP, cycle) or ""
    idx = seg.find("ENTRIES_PAUSED_REASON")
    ok("the pause is set inside run_prop_cycle", idx > 0)
    window = seg[idx: idx + 400] if idx > 0 else ""
    ok("no bare `return` immediately after setting the pause",
       "return" not in window.split("entries_halted = await market_entry_block_reason")[0])

    # try_open is the one chokepoint; it must still be the only consumer
    # that refuses, and it must refuse by returning False, not by aborting.
    ok("try_open still refuses on entries_halted",
       "if entries_halted:" in seg and "return False" in seg)

print("\nthe owner's pause outranks 'market closed' so the reason is honest")
i_pause = PROP.find("entries_halted = ENTRIES_PAUSED_REASON")
i_market = PROP.find("entries_halted = await market_entry_block_reason")
ok("the pause check comes before the market-clock check",
   0 < i_pause < i_market)
ok("the market check is still guarded by `if not entries_halted`",
   "if not entries_halted:\n            entries_halted = await market_entry_block_reason" in PROP)

print("\nthe swing bot refuses BUYS only, at its order chokepoint")
ok("place_order consults the flag", "are_alpaca_entries_paused" in SWING)
ok("only the buy side is gated", 'if side == "buy":' in SWING)
swing_tree = ast.parse(SWING)
po = next((n for n in ast.walk(swing_tree)
           if isinstance(n, ast.AsyncFunctionDef) and n.name == "place_order"), None)
ok("place_order is still the single order path", po is not None)
if po is not None:
    seg = ast.get_source_segment(SWING, po) or ""
    gate = seg.find('if side == "buy":')
    ok("the gate is at the TOP of place_order, before the HTTP post",
       0 < gate < seg.find("session.post"))
    ok("a sell can never reach the pause branch",
       seg[gate:seg.find("session.post")].count('"sell"') == 0)
ok("the pause check fails OPEN, so a DB hiccup cannot become a silent kill switch",
   "proceeding as unpaused" in SWING)

print("\nit is reversible, and visible")
ok("a POST route exists to set it", '@router.post("/alpaca-overview/entries-paused")' in ROUTER)
ok("the overview reports the live state", '"alpaca_entries_paused": alpaca_entries_paused,' in ROUTER)
ok("the overview says what it means, so nobody reads it as passive mode",
   "alpaca_entries_paused_means" in ROUTER)
ok("the route reports that exits are unaffected", '"exits_unaffected": True' in ROUTER)

print("\nit is NOT passive mode, and does not touch Coinbase")
ok("does not call set_alpaca_passive_mode", "set_alpaca_passive_mode" not in
   ROUTER.split('@router.post("/alpaca-overview/entries-paused")')[1].split("@router.post")[0])
ok("places no order and closes no position",
   "/v2/orders" not in ROUTER.split('@router.post("/alpaca-overview/entries-paused")')[1].split("@router.post")[0])
ok("sets no environment variable",
   "os.environ" not in ROUTER.split('@router.post("/alpaca-overview/entries-paused")')[1].split("@router.post")[0])
ok("crypto_grid_bot is untouched by this change",
   "are_alpaca_entries_paused" not in src("crypto_grid_bot.py"))

print()
if FAILS:
    print(f"{len(FAILS)} FAILED:")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("all assertions passed")

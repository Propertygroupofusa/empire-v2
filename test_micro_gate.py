"""Integration test: the live grid gate path, with a stubbed exchange.

Proves the wiring, not just the pure functions - that the veto is reached
only after the priced gates pass, that it can block a real buy, and that a
dead trades endpoint does not block one.
"""
import asyncio, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import crypto_nine_coin_scanner as scanner

# A stand-in for `engine` holding only what _net_edge_gate_ok touches.
class Engine:
    def __init__(self, book, swing, trades):
        self.book, self.swing, self.trades = book, swing, trades
        self.trade_calls = 0
    async def get_book_top_and_depth(self, s, pid):  return self.book
    async def get_average_hourly_swing_pct(self, s, pid): return self.swing
    async def get_real_fee_tier(self, s): return (0.005, 0.005, "tier", None)
    async def get_recent_market_trades(self, s, pid, limit=50):
        self.trade_calls += 1
        return self.trades

# Re-create the gate body verbatim in shape, bound to the stub, so this
# test exercises the real scanner calls without importing the DB layer.
LOGGED = []

async def gate(engine, product_id, grid_pct, slice_usd, mode="observe"):
    bid, ask, bid_depth, ask_depth = await engine.get_book_top_and_depth(None, product_id)
    swing = await engine.get_average_hourly_swing_pct(None, product_id)
    _m, taker, _t, _e = await engine.get_real_fee_tier(None)
    fee_round_trip = (taker * 2) if taker else scanner.DEFAULT_TAKER_ROUND_TRIP
    ok, reason, _d = scanner.evaluate_grid_step(
        product_id, grid_pct, swing, best_bid=bid, best_ask=ask,
        bid_depth_usd=bid_depth, ask_depth_usd=ask_depth,
        slice_usd=slice_usd, fee_round_trip=fee_round_trip)
    if not ok:
        return False, reason
    if mode != "off":
        imbalance = scanner.book_imbalance(bid_depth, ask_depth)
        trades = await engine.get_recent_market_trades(None, product_id)
        aggression = scanner.trade_aggression(trades)
        mok, mwhy = scanner.microstructure_veto("long", imbalance, aggression)
        if not mok:
            if mode == "enforce":
                return False, mwhy
            LOGGED.append(f"MICRO-OBSERVE {product_id}: {mwhy}")
            reason = f"{reason}; WOULD-HAVE-VETOED: {mwhy}"
        else:
            reason = f"{reason}; {mwhy}"
    return True, reason

# --- drift guard -----------------------------------------------------
#
# crypto_grid_bot imports sqlalchemy, which is not installed in every
# environment this test has to run in, so the gate body below MIRRORS the
# shipped one rather than importing it. A mirror that drifts is worse than
# no test at all - it would keep passing while the real gate changed
# underneath it. So the shipped source is read as text and checked for the
# decisions this file claims to be testing. If the gate is rewritten,
# these fail and someone has to look.
GATE_SRC = open(os.path.join(HERE, "crypto_grid_bot.py")).read()
REQUIRED = [
    'MICROSTRUCTURE_VETO_MODE = os.getenv("GRID_MICROSTRUCTURE_VETO_MODE", "observe")',
    'if MICROSTRUCTURE_VETO_MODE != "off":',
    'if MICROSTRUCTURE_VETO_MODE == "enforce":',
    "scanner.book_imbalance(bid_depth, ask_depth)",
    "scanner.trade_aggression(trades)",
    'scanner.microstructure_veto(\n                "long", imbalance, aggression)',
    "MICRO-OBSERVE",
    "WOULD-HAVE-VETOED",
]

HEALTHY  = (100.0, 100.05, 5000.0, 5000.0)          # balanced, tight
ASK_WALL = (100.0, 100.05, 1000.0, 9000.0)          # imbalance -0.80
NEUTRAL_TAPE = [{"side": "BUY", "size": 1.0}, {"side": "SELL", "size": 1.0}]
SELLING_TAPE = [{"side": "SELL", "size": 9.0}, {"side": "BUY", "size": 1.0}]
GOOD_STEP, THIN_STEP, SWING, SLICE = 0.030, 0.004, 0.012, 70.0

checks = []
def ok(label, cond): checks.append((label, bool(cond)))

async def main():
    for needle in REQUIRED:
        ok(f"shipped gate still contains: {needle.splitlines()[0][:52]}",
           needle in GATE_SRC)
    ok("the shipped default is observe, not enforce",
       '"GRID_MICROSTRUCTURE_VETO_MODE", "observe"' in GATE_SRC)
    ok("an unreadable mode falls back to observe, never to enforce",
       "falling back to 'observe'" in GATE_SRC)

    e = Engine(HEALTHY, SWING, NEUTRAL_TAPE)
    passed, why = await gate(e, "SOL-USD", GOOD_STEP, SLICE, mode="enforce")
    ok("a sound step on a neutral book buys", passed)
    ok("the reason carries both verdicts", "net edge" in why and "imbalance" in why)

    e = Engine(ASK_WALL, SWING, NEUTRAL_TAPE)
    passed, why = await gate(e, "SOL-USD", GOOD_STEP, SLICE, mode="enforce")
    ok("a stacked ask book blocks a buy the economics allowed", not passed)
    ok("and says the book leaned against it", "leans against this long" in why)

    e = Engine(HEALTHY, SWING, SELLING_TAPE)
    passed, why = await gate(e, "SOL-USD", GOOD_STEP, SLICE, mode="enforce")
    ok("a selling tape blocks a buy the economics allowed", not passed)
    ok("and says the tape leaned against it", "tape leans" in why)

    e = Engine(HEALTHY, SWING, None)          # trades endpoint down
    passed, _ = await gate(e, "SOL-USD", GOOD_STEP, SLICE, mode="enforce")
    ok("a dead trades endpoint does NOT block a sound buy", passed)

    e = Engine(ASK_WALL, SWING, None)
    passed, _ = await gate(e, "SOL-USD", GOOD_STEP, SLICE, mode="enforce")
    ok("but the book half still vetoes without any tape", not passed)

    e = Engine(HEALTHY, SWING, NEUTRAL_TAPE)
    passed, why = await gate(e, "SOL-USD", THIN_STEP, SLICE, mode="enforce")
    ok("a step that fails on cost is still rejected on cost", not passed)
    ok("cost rejection is worded as cost, not pressure", "does not clear" in why)
    ok("and costs no market-trades call", e.trade_calls == 0)

    e = Engine(ASK_WALL, SWING, SELLING_TAPE)
    passed, _ = await gate(e, "SOL-USD", GOOD_STEP, SLICE, mode="off")
    ok("mode=off restores the previous behaviour exactly", passed)
    ok("and mode=off costs no market-trades call", e.trade_calls == 0)

    # --- the shipped default: observe ---------------------------------
    LOGGED.clear()
    e = Engine(ASK_WALL, SWING, SELLING_TAPE)
    passed, why = await gate(e, "SOL-USD", GOOD_STEP, SLICE, mode="observe")
    ok("DEFAULT observe: a hostile book does NOT block the buy", passed)
    ok("observe records what it would have blocked", len(LOGGED) == 1)
    ok("the record is tagged for counting", "MICRO-OBSERVE" in LOGGED[0])
    ok("the gate reason marks it as counterfactual", "WOULD-HAVE-VETOED" in why)
    ok("observe still pays for the measurement", e.trade_calls == 1)

    LOGGED.clear()
    e = Engine(HEALTHY, SWING, NEUTRAL_TAPE)
    passed, why = await gate(e, "SOL-USD", GOOD_STEP, SLICE, mode="observe")
    ok("observe logs nothing on a neutral book", passed and not LOGGED)
    ok("and does not mark a clean buy as counterfactual", "WOULD-HAVE" not in why)

    e = Engine(HEALTHY, SWING, NEUTRAL_TAPE)
    passed, _ = await gate(e, "SOL-USD", THIN_STEP, SLICE, mode="observe")
    ok("observe never rescues a buy the COST gates rejected", not passed)

    w = max(len(l) for l, _ in checks)
    for l, p in checks: print(f"  [{'PASS' if p else 'FAIL'}] {l:<{w}}")
    bad = [l for l, p in checks if not p]
    print(f"\n  {len(checks)-len(bad)}/{len(checks)} checks passed")
    return 1 if bad else 0

sys.exit(asyncio.run(main()))

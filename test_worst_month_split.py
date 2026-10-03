"""The split and the continuous replay must agree with the real engine.

WHY THIS TEST EXISTS. worst_month_study.py claims two things that would be
easy to get wrong and impossible to notice:

  1. that it can separate REALISED round trips from the UNSOLD-INVENTORY
     mark inside crypto_selection_backtest._replay_grid_bot, by observing
     the real replay rather than reimplementing it;
  2. that its continuous run - one grid across the whole series instead of
     a restart every month - applies the SAME rules.

If either is wrong, every "the loss is really inventory" conclusion built
on it is wrong too. This project has already stubbed the exact function
under test once and declared the result healthy, so the agreement is
pinned rather than assumed.
"""
import sys

sys.path.insert(0, "/home/user/empire-v2")
import crypto_selection_backtest as bt  # noqa: E402
import worst_month_study as wm  # noqa: E402

fail = 0


def ok(label, cond, detail=""):
    global fail
    print(f"{'PASS' if cond else 'FAIL'}  {label}"
          + ("" if cond or not detail else f"\n        -> {detail}"))
    if not cond:
        fail += 1


# A deterministic series with a real fall and a real recovery, so there
# are completed cycles AND rungs left open.
closes = ([100.0]
          + [100 * (1 - 0.04) ** k for k in range(1, 6)]      # falls
          + [100 * (1 - 0.04) ** 5 * (1 + 0.05) ** k for k in range(1, 5)]
          + [90.0, 86.0, 83.0, 88.0, 92.0, 89.0, 85.0, 81.0, 86.0, 91.0])
highs = [c * 1.01 for c in closes]
lows = [c * 0.99 for c in closes]
bt.BACKTEST_ROUND_TRIP_FEE_RATE = wm.MEASURED_FEE

print("\n[1] the split reconstructs the engine's own total exactly")
res = bt._replay_grid_bot(closes, highs, lows, spend=wm.BASE_ALLOC,
                          grid_pct=wm.GRID_PCT, num_levels=wm.LEVELS)
sp = wm.replay_split(closes, highs, lows, spend=wm.BASE_ALLOC)
ok("the engine produced a result", res is not None)
ok("the split produced a result", sp is not None)
ok("realised + mark == the engine's total_pnl",
   abs(sp["realised"] + sp["mark"] - res["total_pnl"]) < 1e-9,
   f"{sp['realised']} + {sp['mark']} vs {res['total_pnl']}")
ok("it reports the same open rung count as the engine",
   sp["open_at_end"] == res.get("open_slices_at_end"),
   f"{sp['open_at_end']} vs {res.get('open_slices_at_end')}")
ok("this series really does leave rungs open, so the mark is exercised",
   sp["open_at_end"] > 0, sp)
ok("and really does complete cycles, so realised is exercised",
   sp["cycles"] > 0, sp)

print("\n[2] the capture restores the function it wrapped")
ok("_summarize_strategy_trades is the original again",
   bt._summarize_strategy_trades.__name__ == "_summarize_strategy_trades",
   bt._summarize_strategy_trades.__name__)


def _boom(*a, **k):
    raise RuntimeError("engine blew up")


_real_replay = bt._replay_grid_bot
bt._replay_grid_bot = _boom
try:
    wm.replay_split(closes, highs, lows)
except RuntimeError:
    pass
finally:
    bt._replay_grid_bot = _real_replay
ok("and it is restored even when the replay raises",
   bt._summarize_strategy_trades.__name__ == "_summarize_strategy_trades",
   bt._summarize_strategy_trades.__name__)

print("\n[3] the continuous replay matches the engine over ONE segment")
one = [(0, len(closes))]
car = wm.replay_carry(closes, highs, lows, wm.BASE_ALLOC, one)
ok("it produced a result", car is not None)
ok("its realised equals the engine's realised",
   abs(car["per_segment"][0]["realised"] - sp["realised"]) < 1e-9,
   f"{car['per_segment'][0]['realised']} vs {sp['realised']}")
ok("its final mark equals the engine's mark",
   abs(car["final_mark"] - sp["mark"]) < 1e-9,
   f"{car['final_mark']} vs {sp['mark']}")
ok("its open rung count matches",
   car["open_at_end"] == sp["open_at_end"],
   f"{car['open_at_end']} vs {sp['open_at_end']}")

print("\n[4] split into segments, the continuous run conserves its own total")
half = len(closes) // 2
two = [(0, half), (half, len(closes))]
car2 = wm.replay_carry(closes, highs, lows, wm.BASE_ALLOC, two)
a = sum(d["realised"] for d in car2["per_segment"].values())
ok("realised across segments equals the one-segment realised",
   abs(a - car["per_segment"][0]["realised"]) < 1e-9,
   f"{a} vs {car['per_segment'][0]['realised']}")
ok("the end mark is unchanged by where the boundaries are",
   abs(car2["final_mark"] - car["final_mark"]) < 1e-9,
   f"{car2['final_mark']} vs {car['final_mark']}")
ok("cycles are attributed to segments, not all dumped in one",
   sum(d["cycles"] for d in car2["per_segment"].values()) == car["per_segment"][0]["cycles"],
   {k: v["cycles"] for k, v in car2["per_segment"].items()})

print("\n[5] carrying inventory is NOT the same as restarting - the whole point")
restart = 0.0
for a_, b_ in two:
    r = wm.replay_split(closes[a_:b_], highs[a_:b_], lows[a_:b_])
    if r:
        restart += r["realised"]
ok("restarting each segment realises a different amount than carrying",
   abs(restart - a) > 1e-9,
   f"restarted {restart} vs carried {a} - if these matched, the continuous "
   f"model would be measuring nothing")
print(f"        restarted realised {restart:.4f} | carried realised {a:.4f}")

print("\n" + ("ALL PASS" if not fail else f"{fail} FAILURE(S)"))
sys.exit(1 if fail else 0)

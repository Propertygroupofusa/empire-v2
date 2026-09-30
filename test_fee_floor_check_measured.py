"""The fee-floor check compares against the MEASURED blended round trip,
never the taker-x-2 worst case - and makes no Coinbase call to do it."""
import asyncio
import crypto_grid_bot as g

fails = 0
def check(name, cond):
    global fails
    print(("PASS " if cond else "FAIL ") + name)
    fails += 0 if cond else 1

async def floor():  # the live derived floor on this account: 0.20% + 2 x 0.35%
    return 0.009

async def main():
    g.fee_safe_floor_pct = floor
    g._cached_real_round_trip_fee_rate = 0.015   # taker 0.75% x 2, from the tier
    g._cached_measured_round_trip = None

    r = await g._fee_safe_floor_check()
    check("no measurement -> UNKNOWN, not safe", r["readable"] is False and "NOT" in r["verdict"])
    check("worst case still reported, labelled", r["if_every_leg_taker"]["round_trip_fee_rate"] == 0.015
          and "worst case" in r["if_every_leg_taker"]["note"])

    g.record_measured_round_trip_fee(0.009495, classified_fills=234, maker_rate=0.8846)
    r = await g._fee_safe_floor_check()
    check("compares against the blended 0.9495%, not taker 1.50%",
          r["measured_round_trip_fee_rate"] == 0.009495)
    check("margin is the real -0.0495%, not -0.60%", abs(r["margin_pct"] - (-0.000495)) < 1e-9)
    check("verdict says the floor does not clear", r["floor_clears_measured_cost"] is False
          and "0.0495%" in r["verdict"] and "1.5000%" not in r["verdict"])
    check("fill count carried through", r["measured_from_fills"] == 234)

    g.record_measured_round_trip_fee(None)
    g.record_measured_round_trip_fee(0)
    r = await g._fee_safe_floor_check()
    check("a missing/zero read never overwrites a real one", r["measured_round_trip_fee_rate"] == 0.009495)

    g.record_measured_round_trip_fee(0.007)
    r = await g._fee_safe_floor_check()
    check("a cheaper measured cost reads as margin", r["floor_clears_measured_cost"] is True
          and "of margin" in r["verdict"])

    import inspect
    src = inspect.getsource(g._fee_safe_floor_check)
    check("check makes no network call", "session" not in src and "engine." not in src)

asyncio.run(main())
print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
raise SystemExit(1 if fails else 0)

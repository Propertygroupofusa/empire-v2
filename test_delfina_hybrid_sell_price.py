"""A sell limit must rest ABOVE the market, or it pays the taker fee.

THE BUG THIS LOCKS DOWN. delfina_hybrid.SellExecutor.execute priced its sell
limit at `current_price * (1 - max_slippage_bps/10000)` - at the default 20
bps, 0.20% BELOW the market. A sell below the market crosses the spread and
takes liquidity. The two comment lines directly above it promised
"Conservative limit execution. Never submit an aggressive market sell here",
so the code contradicted its own stated intent and nothing caught it.

WHY IT IS WORTH A TEST OF ITS OWN. This fleet is measured at 99.08% maker
across 218 legs and a 0.7073% blended round trip. The strategy race measured
the winning configuration at +$296.47 on maker fees and -$4.19 if every leg
paid taker. The sign of that one operator is the difference between the
profitable side of that comparison and the breakeven one.

The behavioural check drives the REAL SellExecutor against a fake exchange
and asserts on the price it actually submitted. Re-implementing the formula
in the test would only prove the test can do arithmetic.
"""
import dataclasses
import sys
from decimal import Decimal as D

sys.path.insert(0, "/home/user/empire-v2")
import delfina_hybrid as dh  # noqa: E402

fail = 0


def ok(label, cond, detail=""):
    global fail
    print(f"{'PASS' if cond else 'FAIL'}  {label}"
          + ("" if cond or not detail else f"\n        -> {detail}"))
    if not cond:
        fail += 1


class FakeExchange:
    """A position sitting at a real profit, so the executor gets that far."""

    def __init__(self):
        self.submitted = []
        self.position = dh.Position(symbol="APE-USD", quantity=D("10"),
                                    average_cost=D("10"), current_price=D("12"))

    def get_open_orders(self):
        return []

    def get_positions(self):
        return [self.position]

    def get_available_balance(self):
        return D("0")

    def cancel_order(self, order_id):
        return True

    def place_sell_limit(self, symbol, quantity, price):
        self.submitted.append({"symbol": symbol, "quantity": quantity,
                               "price": price})
        return "order-1"

    def get_fee_rate(self, maker):
        return D("0.0035")          # the fleet's measured maker rate per leg


LIVE = dataclasses.replace(dh.CFG, scalper_live_execution=True)

print("\n[1] the executor submits a sell ABOVE the market")
ex = FakeExchange()
ctl = dh.OrderController(ex, LIVE)
sig = dh.DelfinaScalper(ex, LIVE).evaluate_position(ex.position)
ok("the position is profitable enough to produce a signal", sig is not None, sig)
dh.SellExecutor(ex, LIVE, ctl).execute(sig)
ok("an order really was submitted", len(ex.submitted) == 1, ex.submitted)
sub = ex.submitted[0]
ok("the limit price is ABOVE the market price",
   sub["price"] > ex.position.current_price,
   f"submitted {sub['price']} against market {ex.position.current_price}")
ok("it is above by exactly max_slippage_bps",
   sub["price"] == ex.position.current_price * (D("1") + D(LIVE.max_slippage_bps) / D("10000")),
   f"{sub['price']}")
ok("it sells only max_sell_fraction of the position",
   sub["quantity"] == ex.position.quantity * LIVE.max_sell_fraction,
   f"{sub['quantity']} of {ex.position.quantity}")

print("\n[2] the direction holds at other offsets, not just the default")
for bps in (1, 5, 50, 200):
    ex2 = FakeExchange()
    cfg = dataclasses.replace(dh.CFG, scalper_live_execution=True,
                              max_slippage_bps=bps)
    s2 = dh.DelfinaScalper(ex2, cfg).evaluate_position(ex2.position)
    dh.SellExecutor(ex2, cfg, dh.OrderController(ex2, cfg)).execute(s2)
    p = ex2.submitted[0]["price"] if ex2.submitted else None
    ok(f"{bps} bps puts the limit above the market",
       p is not None and p > ex2.position.current_price, f"{p}")

print("\n[3] the source itself does not reintroduce the minus")
src = open("/home/user/empire-v2/delfina_hybrid.py").read()
i = src.index("price = position.current_price")
expr = src[i:i + 160]
ok("the price expression adds the offset", 'D("1") + D(' in expr, expr[:90])
ok("and does not subtract it", 'D("1") - D(' not in expr, expr[:90])

print("\n[4] observation mode still submits nothing at all")
ex3 = FakeExchange()
s3 = dh.DelfinaScalper(ex3, dh.CFG).evaluate_position(ex3.position)
dh.SellExecutor(ex3, dh.CFG, dh.OrderController(ex3, dh.CFG)).execute(s3)
ok("the shipped default places no order", ex3.submitted == [], ex3.submitted)
ok("scalper_live_execution is False by default",
   dh.CFG.scalper_live_execution is False)

print("\n[5] an unprofitable position is never sold, either way round")
ex4 = FakeExchange()
ex4.position = dh.Position("APE-USD", D("10"), D("10"), D("10.01"))
s4 = dh.DelfinaScalper(ex4, LIVE).evaluate_position(ex4.position)
ok("no signal below the net profit floor", s4 is None, s4)
ok("the floor is a real 0.50%", dh.CFG.min_net_profit_pct == D("0.50"))

print("\n" + ("ALL PASS" if not fail else f"{fail} FAILURE(S)"))
sys.exit(1 if fail else 0)

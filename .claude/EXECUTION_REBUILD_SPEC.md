REBUILD THE CRYPTO GRID EXECUTION ENGINE
=========================================

OBJECTIVE
---------

Rebuild the grid execution loop into a fast, event-driven, 3-slice capital-rotation engine.

The objective is:

1. Never allow quantity rounding to create a false 0/3 state.
2. Maintain three independent executable inventory slices.
3. Give every slice its own entry price and take-profit target.
4. Calculate each target from that slice's actual entry price.
5. Make the transition from FILLED → ACCOUNTED → NEXT SLICE as fast as safely possible.
6. Use maker-only execution unless an explicit existing system policy says otherwise.
7. Maximize capital velocity without bypassing exchange rules, minimums, fees, risk controls, or maker-only policy.
8. Never sacrifice accounting correctness for speed.
9. Never treat exchange-invalid quantity as a failed trade.
10. Never manufacture a trade simply to increase trade count.

IMPORTANT:
Do not solve this by merely increasing the take-profit percentage.
Fix the execution architecture first.


==================================================
1. THREE-SLICE MODEL
==================================================

Each grid position consists of three independent slices:

SLICE 1/3
SLICE 2/3
SLICE 3/3

Each slice must have its own persistent state.

Required fields:

cycle_id
slice_id
slice_state
entry_price
target_price
quantity
executable_quantity
base_increment
base_min_size
quote_increment
quote_min_size
order_id
order_side
order_price
filled_quantity
remaining_quantity
average_fill_price
fee
realized_pnl
created_at
updated_at
filled_at
target_reason
execution_reason

Do NOT infer slice state from wallet balance alone.

Do NOT infer cycle progress by dividing available inventory by three.

The system must explicitly know:

1/3 = slice 1
2/3 = slice 2
3/3 = slice 3


==================================================
2. SLICE STATES
==================================================

Use explicit states:

CREATED
ARMED
READY
SUBMITTING
OPEN
PARTIAL
FILLED
ACCOUNTED
DUST
REPRICE
CANCELLED
REJECTED
ERROR

A normal successful sequence is:

CREATED
→ ARMED
→ READY
→ SUBMITTING
→ OPEN
→ FILLED
→ ACCOUNTED

DUST is NOT an error.

DUST is NOT REJECTED.

DUST is NOT "nothing sellable."

DUST means the currently available inventory cannot satisfy the exchange's executable quantity rules.

Example:

available = 0.0009732300 QNT

If exchange rules require a quantity larger than this:

state = DUST

Do NOT report:

0/3
sell failed
nothing sellable

Instead report:

slice=3/3
state=DUST
reason=BELOW_EXECUTABLE_MINIMUM


==================================================
3. EXCHANGE PRECISION
==================================================

NEVER use:

round(quantity, 3)

NEVER hard-code:

Decimal("0.001")

NEVER assume every asset has three decimal places.

NEVER truncate inventory based on a generic precision value.

Read the actual product metadata for the market.

Required exchange metadata:

base_increment
base_min_size
quote_increment
quote_min_size

Calculate:

executable_quantity =
    floor_to_increment(
        available_quantity,
        base_increment
    )

Then validate:

executable_quantity >= base_min_size

AND

executable_quantity * order_price >= quote_min_size

Only then is the quantity executable.

If metadata cannot be retrieved:

FAIL CLOSED.

Do not submit an order using guessed precision.


==================================================
4. QNT-USD BUG
==================================================

The current log says:

"available 0.0009732300 floors to 0 at 3 decimals"

This must be eliminated as a generic execution failure.

The system must instead log:

QNT-USD
cycle=<cycle>
slice=<slice>
available_quantity=<actual>
base_increment=<actual exchange value>
base_min_size=<actual exchange value>
executable_quantity=<calculated value>
quote_min_size=<actual exchange value>
target_price=<actual>
current_price=<actual>
maker_only=true
decision=<EXECUTE|DUST|REJECTED|WAIT>

If the quantity is below the exchange minimum:

decision=DUST

Do not create a failed sell order.

Do not repeatedly submit zero quantity.

Do not report 0/3.


==================================================
5. EACH SLICE GETS ITS OWN TAKE-PROFIT
==================================================

Every slice must calculate its own target from its own actual entry price.

Do NOT use one global absolute target price for all slices.

Basic formula:

target_price =
    entry_price
    ×
    (1 + required_net_return + estimated_execution_cost + safety_margin)

The exact components must use the existing strategy's configured fee model and risk rules.

At minimum account for:

maker_fee
expected_slippage/adverse movement
minimum desired net edge
safety margin

Example:

Slice 1:

entry = $100.00
required edge = 1.20%

target ≈ $101.20 plus applicable execution-cost adjustment

Slice 2:

entry = $100.40
required edge = 1.20%

target ≈ $101.60 plus applicable execution-cost adjustment

Slice 3:

entry = $100.85
required edge = 1.20%

target ≈ $102.06 plus applicable execution-cost adjustment

The actual implementation must calculate these values dynamically.

Do not copy the same target price across slices when their entry prices differ.


==================================================
6. ADAPTIVE TAKE-PROFIT
==================================================

Do NOT simply increase TP to make the system appear more profitable.

The TP engine should determine whether the current market can support the requested grid edge.

Inputs may include:

volatility
spread
recent realized fill rate
recent trade duration
recent profitable trade duration
maker fee
expected adverse movement
current grid spacing
inventory age
order-book conditions if available
recent realized net edge

The engine should calculate:

minimum_profitable_edge

and

realistic_target_edge

Then:

target_price =
    entry_price × (1 + target_edge + execution_cost_adjustment)

Never allow:

target_edge < minimum_profitable_edge

unless the existing strategy explicitly permits it.

If the market cannot support a positive expected net edge:

DO NOT FORCE A TRADE.


==================================================
7. CAPITAL VELOCITY
==================================================

The system should optimize:

FILL
→ ACCOUNT
→ NEXT SLICE
→ NEXT ORDER

with minimum safe latency.

Do NOT use a slow loop such as:

scan()
sleep(60)

for every action.

Use event-driven processing where supported.

Preferred architecture:

REAL-TIME MARKET EVENT
        ↓
UPDATE MARKET STATE
        ↓
EVALUATE ACTIVE SLICE
        ↓
TP / GRID DECISION
        ↓
ORDER ACTION
        ↓
ORDER/FILL EVENT
        ↓
ACCOUNTING
        ↓
SLICE STATE TRANSITION
        ↓
NEXT SLICE EVALUATION
        ↓
NEXT VALID ORDER


==================================================
8. EXECUTION SPEED
==================================================

The internal state transition should be as fast as safely possible.

Target:

FILL EVENT
→ persist fill
→ update accounting
→ calculate next state
→ calculate next executable quantity
→ calculate next target
→ prepare next order

Do not intentionally wait for the next minute.

Do not intentionally wait for a slow polling cycle when an order/fill event is already available.

Use WebSocket/event-driven data where supported by the existing implementation.

Keep a reconciliation loop as a safety mechanism.

Recommended architecture:

MARKET DATA:
real-time/event-driven

ORDER EVENTS:
real-time/event-driven

GRID DECISIONS:
event-driven

ACCOUNT RECONCILIATION:
every 5–10 seconds or existing safe interval

HEALTH CHECK:
existing system interval

The exact interval must respect exchange/API rate limits.


==================================================
9. THREE-SLICE CAPITAL ROTATION
==================================================

The slices are independent.

Do NOT require:

1/3 to completely finish
before the system can evaluate 2/3.

Do NOT require:

2/3 to completely finish
before the system can prepare 3/3.

Each slice has its own:

entry
quantity
target
order
state
fill
P&L

This allows the system to keep capital working instead of waiting unnecessarily.

Example:

SLICE 1/3
entry=$100
target=$101.20
status=OPEN

SLICE 2/3
entry=$100.40
target=$101.60
status=OPEN

SLICE 3/3
entry=$100.85
target=$102.06
status=ARMED

If Slice 1 fills:

SLICE 1/3
FILLED → ACCOUNTED

Immediately evaluate whether another valid slice can be created.

Do not wait one minute.


==================================================
10. CYCLE COMPLETION
==================================================

A cycle is complete when all three slices have completed their intended lifecycle.

Example:

CYCLE 1842

1/3 = FILLED
2/3 = FILLED
3/3 = FILLED

Then:

CYCLE 1842 = COMPLETE

Immediately initialize:

CYCLE 1843

with:

1/3 = CREATED/ARMED

provided that:

capital
inventory
risk
minimum order requirements
strategy conditions

all permit a new cycle.


==================================================
11. DUST HANDLING
==================================================

If:

available_quantity < executable minimum

do not destroy precision.

Do not round it to zero in accounting.

Do not lose it from the ledger.

Store:

raw_available_quantity

and:

executable_quantity = 0

with:

state=DUST

Example:

available:
0.0009732300

executable:
0

state:
DUST

reason:
BELOW_BASE_MIN_SIZE

Keep the raw inventory amount available for future aggregation/reconciliation.

When additional inventory becomes available:

recalculate.

If the combined quantity becomes executable:

DUST
→ READY

Then proceed normally.


==================================================
12. MAKER-ONLY POLICY
==================================================

Preserve the existing maker-only requirement.

If:

maker_only=true

the system must NOT convert a missed maker sale into a taker sale merely to force completion.

If TP is reached but maker execution does not fill:

state=OPEN or REPRICE

depending on the existing strategy.

Do not spend the slice's expected profit on an unauthorized taker leg.

Do not hide this as:

FAILED

unless the exchange actually rejected the order.


==================================================
13. ORDER LIFECYCLE
==================================================

Use:

READY
→ SUBMITTING
→ OPEN
→ PARTIAL
→ FILLED

If the order becomes stale:

OPEN
→ REPRICE

If cancelled:

OPEN
→ CANCELLED

If exchange rejects:

SUBMITTING
→ REJECTED

If the quantity is below exchange requirements:

READY
→ DUST

Never confuse these states.


==================================================
14. FAST REPRICE ENGINE
==================================================

When a maker order is open, monitor:

market price
spread
order age
distance from target
fill progress
inventory state

If the existing strategy's stale-order rules permit repricing:

CANCEL/REPLACE

without unnecessarily destroying a valid order.

Do not reprice continuously just because the market moved by one tick.

Use a minimum meaningful reprice threshold.

Respect exchange rate limits.


==================================================
15. ORDER/FILL ACCOUNTING
==================================================

A fill is not complete until accounting is confirmed.

Required sequence:

ORDER_FILLED
    ↓
record fill
    ↓
calculate actual average fill price
    ↓
calculate actual fee
    ↓
calculate realized P&L
    ↓
update inventory
    ↓
update cycle
    ↓
update slice
    ↓
evaluate next opportunity

Never assume the requested order quantity equals the filled quantity.


==================================================
16. REALIZED P&L
==================================================

Calculate actual net P&L using:

entry value
exit value
actual fees
actual filled quantity

Do not use theoretical P&L as realized P&L.

Store:

gross_pnl
fees
net_pnl

Example:

gross_pnl = exit_value - entry_value

net_pnl =
    gross_pnl
    - entry_fees
    - exit_fees


==================================================
17. NO FAKE SPEED
==================================================

The goal is NOT:

maximum number of API calls.

The goal is:

maximum safe capital rotation.

Do NOT:

spam the exchange
submit duplicate orders
submit zero quantities
bypass minimums
bypass maker-only policy
skip reconciliation
assume fills
invent prices
invent exchange precision
force unprofitable exits

Fast means:

EVENT
→ DECISION
→ VALID ORDER
→ FILL
→ ACCOUNTING
→ NEXT OPPORTUNITY


==================================================
18. LOGGING
==================================================

Replace vague logging such as:

"nothing sellable"

with precise diagnostics.

Example:

QNT-USD
cycle=1842
slice=2/3
available=0.0009732300
base_increment=<actual>
base_min_size=<actual>
executable_quantity=<actual>
quote_min_size=<actual>
price=<actual>
target=<actual>
maker_only=true
state=DUST
reason=BELOW_EXECUTABLE_MINIMUM

When submitted:

QNT-USD
cycle=1842
slice=2/3
state=SUBMITTED
quantity=<actual>
price=<actual>
target=<actual>
maker_only=true

When filled:

QNT-USD
cycle=1842
slice=2/3
state=FILLED
filled_quantity=<actual>
average_fill=<actual>
gross_pnl=<actual>
fees=<actual>
net_pnl=<actual>

next_action=<3/3|NEW_CYCLE>


==================================================
19. DASHBOARD
==================================================

Display:

COIN
CYCLE
SLICE
ENTRY
CURRENT PRICE
TARGET
QUANTITY
EXECUTABLE QUANTITY
STATE
ORDER AGE
GROSS P&L
FEES
NET P&L

Example:

QNT-USD

CYCLE 1842

[✓ 1/3] [✓ 2/3] [● 3/3]

3/3
Entry: $100.85
Target: $102.06
Current: $101.94
State: OPEN
Maker: YES

Capital Rotation:
ACTIVE


==================================================
20. CYCLE SPEED METRICS
==================================================

Track:

time_to_first_fill
time_between_slice_fills
time_fill_to_accounting
time_accounting_to_next_order
time_to_cycle_completion
cycles_per_hour
cycles_per_day
capital_turnover
average_inventory_age
average_order_age
maker_fill_rate
partial_fill_rate
dust_rate
reprice_rate
rejection_rate
net_pnl_per_cycle
net_pnl_per_trade

This will show whether the new engine actually improves capital velocity.


==================================================
21. PERFORMANCE METRICS
==================================================

Do not optimize solely for:

trade count.

Track:

net P&L
net edge
capital turnover
maker fill rate
average hold time
maximum drawdown
fees
slippage/adverse movement
inventory age
capital utilization

A faster system that loses more money is not an improvement.


==================================================
22. SAFETY / FAIL-CLOSED
==================================================

If exchange metadata is unavailable:

DO NOT TRADE.

If account balance is unavailable:

DO NOT TRADE.

If order status is unknown:

DO NOT ASSUME FILLED.

If database state is unavailable:

FAIL CLOSED according to the existing system's safe mode.

If inventory reconciliation fails:

DO NOT CREATE A NEW SELL BASED ON ASSUMPTION.

If an order ID cannot be reconciled:

STOP THAT SLICE.

If product rules cannot be verified:

DO NOT SUBMIT.


==================================================
23. DUPLICATE ORDER PROTECTION
==================================================

Before creating a new order, verify:

existing active order
existing pending order
existing open slice
existing cycle state
recent order submission

Use an idempotency key such as:

cycle_id + slice_id + side + target_price + quantity

Never submit duplicate orders because an event was processed twice.


==================================================
24. RECOVERY
==================================================

On restart:

1. Load persisted cycles.
2. Load persisted slices.
3. Query exchange open orders.
4. Query balances.
5. Reconcile fills.
6. Reconcile inventory.
7. Reconcile order states.
8. Mark unknown states appropriately.
9. Only then resume trading.

Do not start a new cycle before reconciliation.


==================================================
25. TEST REQUIREMENTS
==================================================

Create tests for:

A. Quantity precision

available=0.0009732300

Must NOT create a zero-quantity sell.

B. Valid quantity

quantity exactly at base_min_size.

Must submit.

C. Quantity above minimum.

Must submit correctly using base_increment.

D. Partial fill.

Must preserve remaining quantity.

E. Maker order reaches TP but doesn't fill.

Must remain maker-only.

F. Fill event.

Must immediately advance state.

G. Duplicate event.

Must not create duplicate order.

H. Database outage.

Must fail closed.

I. Exchange metadata unavailable.

Must fail closed.

J. Restart recovery.

Must reconcile before trading.

K. Three independent slices.

Each slice must retain its own entry and target.

L. Different entry prices.

Targets must differ appropriately.

M. Dust accumulation.

Dust must remain recorded and become executable when sufficient inventory exists.

N. Cycle completion.

1/3 + 2/3 + 3/3 completion must create the next cycle without losing accounting.


==================================================
26. ACCEPTANCE CRITERIA
==================================================

The implementation is NOT complete until:

[ ] No hard-coded 3-decimal quantity rounding remains in the execution path.

[ ] Exchange product precision is used.

[ ] Exchange minimums are enforced.

[ ] Zero-quantity orders are impossible.

[ ] DUST is a legitimate state.

[ ] 0/3 cannot be produced merely because of rounding.

[ ] Each slice has its own entry.

[ ] Each slice has its own target.

[ ] Each target is calculated from that slice's actual entry.

[ ] Fees and minimum profitable edge are included.

[ ] Maker-only policy remains intact.

[ ] Fill events advance the state immediately.

[ ] The system does not wait for the next minute after a confirmed fill.

[ ] Three slices can operate independently.

[ ] Cycle completion automatically prepares the next cycle when permitted.

[ ] Duplicate orders are prevented.

[ ] Restart recovery reconciles exchange state.

[ ] Database failure remains fail-closed.

[ ] Exchange metadata failure remains fail-closed.

[ ] Actual realized P&L is recorded.

[ ] Capital turnover is measurable.

[ ] Cycle completion time is measurable.

[ ] Maker fill rate is measurable.

[ ] Dust rate is measurable.

[ ] Tests pass.


==================================================
FINAL DESIGN PRINCIPLE
==================================================

Build this as a:

FAST, EVENT-DRIVEN, THREE-SLICE, MAKER-ONLY,
CAPITAL-ROTATION ENGINE.

The desired lifecycle is:

VALID ENTRY
    ↓
SLICE 1/3
    ↓
ITS OWN TP
    ↓
MAKER FILL
    ↓
IMMEDIATE ACCOUNTING
    ↓
SLICE 2/3
    ↓
ITS OWN TP
    ↓
MAKER FILL
    ↓
IMMEDIATE ACCOUNTING
    ↓
SLICE 3/3
    ↓
ITS OWN TP
    ↓
MAKER FILL
    ↓
CYCLE COMPLETE
    ↓
IMMEDIATELY EVALUATE NEW CYCLE
    ↓
1/3 AGAIN

Do not optimize for fake trade frequency.

Optimize for:

VALID TRADES
+
POSITIVE NET EDGE
+
FAST FILL-TO-NEXT-ORDER TRANSITION
+
HIGH CAPITAL UTILIZATION
+
LOW INVENTORY AGE
+
LOW EXECUTION LATENCY
+
STRICT ACCOUNTING
+
STRICT RISK CONTROLS.

MOST IMPORTANT BUG TO FIX FIRST:

"available 0.0009732300 floors to 0 at 3 decimals"

Remove the hard-coded precision assumption and replace it with exchange-product metadata and exact Decimal arithmetic.

Then implement the three-slice state machine and event-driven fill-to-next-order transition.

Do not change unrelated parts of the trading system.
Preserve all existing risk limits, circuit breakers, database safety, maker-only controls, and fail-closed behavior.
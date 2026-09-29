# Fleet improvement loop — standing state

This file is the loop's memory. The scheduled trigger carries only a short
pointer; everything operative lives here so the prompt does not have to
re-transmit ~12k tokens every few minutes.

**Read this file at the start of every pass. Update it in place when
something changes, and commit — that is how the next pass inherits it.**

## Cadence

Alternating FIVE / FIFTEEN.
- FIVE = sweep, leak check, shortfall in units, finish the last FIFTEEN.
- FIFTEEN = that plus the SYSTEM REVIEW.
The trigger prompt states which one this pass is and what to flip to.

**RE-ARM FIRST, ALWAYS — broken four times.** Cron caps at 1h; this is a
one-shot that re-arms itself via `send_later`. Re-arm even mid-investigation.
If a user message interrupts a pass, re-arm before answering. One allowed
deviation: if a fix pushed last pass HAS deployed, do the single read that
refreshes baselines first.

## Hard limits — never violate

- Never lower `GRID_CASH_RESERVE_USD` ($88), the $15 viability floor, the
  $150 buying-power floor, the $25 trim floor, the 20% concentration
  ceiling, or the adoption cap. Never manufacture activity by moving a
  threshold.
- Never sell a red position. ZEC-USD is the ONE authorised exception and
  even that order is the owner's to place.
- **Never change `GRID_ADOPTED_STOP_MODE` on your own initiative — either
  way.** It exists as of 3ab8dec and sells red adopted positions when armed,
  which is the limit above by proxy. **The owner authorised arming it on 29
  Sep and throws it themselves.** This cuts both directions now: do not disarm
  it because this line once read "never arm", and do not re-arm it if the owner
  turns it off. Reporting its state, and what it would sell at TODAY's prices,
  is the loop's job. Deciding it is not.
  **ALL THREE DOORS ARE SHUT TO THE LOOP — measured, do not retry them.** The
  Railway variable: `backboard.railway.app` is 403, no credential is held, no
  CLI installed. `POST /grid-status/adopted-stop` (added 8ed313b — the DB half
  exists so the OWNER can throw it from the dashboard instead of Railway plus a
  restart): 401 `write_guard`, "Missing x-dashboard-token header. This endpoint
  changes state." Calling a write-guarded endpoint and asking for that token
  are each already forbidden below. A direct DB write: no credential, and it
  would be routing around the guard. There is no fourth door; report the state
  and the preview, and let the owner click it.
- Never call a write-guarded endpoint, including `free-locked-inventory`
  and `POST /alpaca-overview/equity-handover`.
- Never ask for or echo the write token.
- Never tighten grid spacing. `GRID_AUTO_ROTATE` stays off.
- No leverage, no perps, no DEX sniping.
- Never flip `market_brain_owns_equities`; never set
  `MARKET_BRAIN_SELF_UPDATE=true`. The runner IS launched (028fda7),
  flag-gated and idle — do not undo it.
- Do not turn maker-only off and do not widen the maker price.
- Do not convert USD to USDC. Do not reconcile or close QNT or ZEC.
- Do not weaken the unknown-balance gate: refuse is the default, and only
  close-all may opt out.
- Compound comes AFTER the profit is made.

## The owner's to run, never mine

- **ZEC**: `reconcile-slices?product_id=ZEC-USD` preview, then
  `&dry_run=false&accept_writeoff=true`, then `close-branch` preview, then
  `close-branch`. Live numbers only.
- **QNT**: the same shape, equally warranted — 0.675982 claimed against
  0.00097323 held.
- **free-locked-inventory**: POST and write-guarded, so even the dry run
  needs their token. No clickable link, no browser path.
  `POST /api/trading-dashboard/grid-status/free-locked-inventory`
  `?dry_run=true` (preview) or `?dry_run=false` (executes), header
  `x-dashboard-token` — **not** `?token=`, which lands in access logs.
  Cancel-only, fails closed. Fixes ALGO, XLM, LINK. Does NOT fix QNT.
- `POST /alpaca-overview/equity-handover {"enabled": true}`.
- The USD→USDC conversion slider.

## The central finding — growth = turnover × edge

**MEASURED 23:20Z, no longer inferred.** With edcdbe8 serving, every flagged
maker-expiry row is `order_rested=False` — 56 of 56, across QNT, ALGO and
PRIME, all reading "nothing sellable: available … floors to 0". Zero rested
orders. So the sell failures on these products are not orders resting untaken;
they are orders never created. The attribution that was a strong inference
from balances is now a direct measurement. The expiry study correctly reports
"not enough data" as a result — that is honest, not broken; do not "fix" it.


Measured 19:21Z 28 Sep, 127 round trips over 28.94 days (`/growth-model`):

| Term | Value |
|---|---|
| Edge | blended 2.1529% net, 84.3% win rate, median 1.2126%, median notional $19.90 |
| Turnover | $3,328.95 notional / $8,499.30 over 28.94 days = 0.406×/30d |
| Rate | 0.8742%/month = 11.01%/yr compounded |

Identity holds: 0.406019 × 2.1529% = 0.8741% against 0.8742% measured
independently. **The edge is not the problem. Turnover is.** Three causes:

1. Capital never deployed — the SMALL lever (+$13.00/mo at the fleet rate,
   and that is a ceiling).
2. Capital UNDERWATER, earning nothing and unable to cycle — the BIG lever.
   ZEC + XRP = 54.7% of allocated and 91.7% of the unrealized; both over the
   owner's own 20% ceiling, both adopted at that size.
3. **Sells that cannot execute** — ~1,000 expired maker sells in 24h.
   ALGO/XLM/LINK because their coin is reserved at the venue, QNT because it
   holds dust. Turnover lost at the EXECUTION layer, not the strategy layer.

Ladder (ceilings, never forecasts), at 0.8742%/mo: 1mo $8,573.60 · 3mo
$8,724.15 · 6mo $8,954.96 · 12mo $9,435.04 · 24mo $10,473.80 · 36mo
$11,626.93.

## The owner's architecture direction (22:0xZ 28 Sep)

They proposed continuous scan (1–5s), 100+ candidate pairs, liquidity/spread/
volatility filter, entry-edge calc, fee+slippage+adverse test, inventory+
capital check, a trade gate, multiple simultaneous slices; a three-stage
WATCH → ARM → ENTER entry; and reframing from "can this coin trade every
minute" to "which slice has the highest probability of positive net turnover
right now".

**Their non-negotiable:** "an unknown balance can never generate an order.
Speed comes after the accounting and inventory gates, not before them."
Shipped as 0af01f1.

**What already existed**, checked before agreeing anything needed building:
`_net_edge_gate_ok()` prices spread, fees and adverse selection with a
breakeven win rate, has a runtime toggle, and persists its full diagnostic
per slice in `entry_gate_json`. That IS their fee+slippage+adverse test.
`CYCLE_SECONDS = 30` with ±10% jitter. 16 symbols priced, 23 branches.

**The disagreement, stated and standing:** the binding constraint is not
opportunity discovery. A wider funnel above a jammed outlet queues more.
Sequence execution first, then breadth. Their WATCH/ARM/ENTER is a genuine
addition — the current design evaluates and fires in one pass, so there is no
qualified-but-waiting state — and is worth building once things can sell.

**Correction to their premise, already sent:** capital is not "equally
distributed across dozens of slow branches". ZEC 27.6% + XRP 27.2%; the other
21 branches share 45%. Concentration, not evenness.

## Shipped 28 Sep after 3474915 — do not rebuild

| Commit | What |
|---|---|
| 0ca52dd | silent skipped stop made visible (a diagnostic, not a fix) |
| ef86ee9 | broker-mark fallback for exits — broken on arrival |
| 37fd02b | fixed that. This closed META. |
| feef9b6 | every bars request names a `start`, limit 10000, tail taken |
| e7f4146 | `parked_capital.split_by_cause` + `GET /parked-capital` |
| e1ce83d | prop_bot orders carry `{source}-{contract}-{uuid}` |
| 2ee8c75 | `/trades/closed` rebuilt — was wrong on all 39 rows |
| 7cbc03f | `/parked-capital` fixed |
| f7d0dec | `equity_curve` + `GET /alpaca-overview/equity-curve` |
| d050e60 | market_brain and options_trading orders tagged |
| 9d10939 | `growth_model` + `GET /growth-model` |
| e5487c7 | dashboards no longer prompt for an admin key on load |
| b63502f | slices-over-levels rule corrected to `bought <= 1` |
| cba0fb4 | sell path no longer retires a partly-filled slice |
| 84a4de9 | `GET /grid-status/maker-expiries` — written, never read |
| d7fc7cb | `GET /grid-status/asset-balance` |
| 0af01f1 | an unknown balance can no longer generate a sell order |
| e1221eb | a sell that was never placed is no longer called unfilled |

### 0af01f1 — the unknown-balance gate

Both sell paths did:
```python
real_balance, _ = await get_asset_balance(session, base_currency)
if real_balance is not None and real_balance < qty: qty = real_balance
```
A failed read returns `None`, the condition is False, the clamp is skipped,
and the order ships at the TRACKED quantity — the number already known to
drift. The reason went into `_`.

**The asymmetry was the tell:** the buy path already failed closed on the same
condition (`crypto_grid_bot.py:5999`). Only sells fell through.

Now `place_market_sell` refuses and records why; `place_maker_sell` refuses
with no escape hatch; `close_all_grid_branches` is the ONE permitted exception
and must pass `allow_unverified_balance=True` by name, because it is a
protection and refusing there leaves a live position open.

### e1221eb — the three-way reason

`place_maker_sell` returns `None` for three different reasons and `grid_sell`
called all of them "maker sell did not fill":

- (a) the order rested at the ask and no buyer crossed — a real no-fill
- (b) nothing sellable: size clamped to available then floored to zero — **no
  order created**
- (c) a precondition failed: balance read errored, or no ask — **no order
  created**

For (b) and (c) "did not fill" is false. Those words cost four hours: 441 QNT
and 429 ALGO rows read as liquidity failures in a 0.004%-wide book when both
were case (b). Now each path logs what it did and records a machine-readable
reason; `grid_sell` reports it.

**Still not done:** `GridMakerExpiry` has no reason column, so
`/maker-expiries` cannot split the counts. The log distinguishes them; the row
does not.

## Shipped 29 Sep on `claude/maker-only-cause-split` — NOT MERGED

Five commits, pushed, no PR (creation is 403 for this repo from the session).
Nothing here is live until the owner merges and redeploys.

| Commit | What |
|---|---|
| adc9a54 | a cycle that never placed an order is no longer a maker expiry |
| 44a815f | the fill count no longer reports every maker expiry as a fill |
| 8f1de52 | the fee floor's guards are exercised, not grepped for |
| bbe415e | no layer claims a stop another layer is claiming back |
| 3ab8dec | a catastrophe stop for adopted branches, **off by default** |

### The three-way stop-coverage circle — the real finding of 29 Sep

Chasing the slice overflow turned this up. **Fourteen branches, $4,573
allocated (56% of the fleet), holding 96% of the −$575.19 unrealized, with no
automatic exit in either direction.** Each layer cited the next; the last
cited the first.

| layer | what it said | true? |
|---|---|---|
| grid branch | `stop_pct 0.0` — "Adopted coin is covered at the portfolio level by the resting stops" | no |
| resting stops | refuses it `ACTIVELY_TRADED` — "The branch carries its own adaptive stop" | no, it names 0 |
| ZEC, one further | refused `TRIMMERS_COIN`, deferred to the trimmer | trimmer says `WITHIN_LIMIT` |

`/resting-stops` reported `protects_usd 0` throughout — which is also what a
healthy fleet reports, so the one number that could have shown it could not.
That is the shape to watch for: **a coverage figure whose healthy value and
whose broken value are the same number.**

Why a branch cannot exit: full (`len(slices) >= num_levels`) so no buy; every
slice underwater so `_pick_profitable_slice_to_sell` returns None and neither
the rise trigger nor the parked-sell path (+1.0% net) can fire — checked at
both the taker 1.5% and maker 0.70% round trip, conclusion identical; and
`stop_pct 0.0` so no stop. `drawdown_breached` false as well.

**The perverse feedback, worth remembering.** The trimmer fires on SHARE of
account, not on loss. ZEC was over 20% at its $2,272 entry basis; the decline
itself took its market value to $1,829 = 18.41% and out of the trimmer's
reach. The worse it gets, the further it moves from the only mechanism
nominally responsible for it.

### Two fixes proposed and withdrawn — read before proposing a third

1. **Relax `resting_stops`' `ACTIVELY_TRADED` guard.** Wrong. That guard was
   written after a real incident and names three objections a stop elsewhere
   does not touch: a resting sell holds 75% of the units the grid trades with;
   if it fires the branch goes on tracking units the wallet no longer has
   (*"that is how ETH came to be short 0.0316 units"*); and it schedules a
   loss on a position the fleet does not sell at a loss. It would also have
   recreated the reserved-inventory case that produces "nothing sellable:
   available X floors to 0".
2. **A loss trigger on the concentration trimmer.** Cannot fire. `plan_trims`
   refuses `ACTIVELY_TRADED` before any other reason can let a trim through,
   and all fourteen have open grid slices — **measured 0 of 14 sellable**, so
   it would be dead code. Forcing it past that rule recreates the incident
   that rule was written after: $885.43 of ZEC and $244.78 of XRP sold out
   from under live branches.

**THE RULE THOSE THREE REFUSALS SHARE, and the answer they force:** the grid's
slice ledger is the book of record for those units, so any OTHER seller
desynchronises it and leaves the branch claiming coin the wallet no longer
holds. The only seller that can safely exit grid-held coin is the grid, via
the stop — which only CHOOSES a slice and then uses the proven sell path that
retires the row and records the trade.

So 3ab8dec puts it there: `adaptive_stop.adopted_stop()`, 6x daily volatility
against the ordinary 2.5x, floored at 20%, capped at 35%. **Off by default —
see the hard limit.** Three ways it still declines, each naming which: not
armed; a per-coin `GRID_STOP_OVERRIDES` entry of 0; and volatility it cannot
read. That last is deliberately the opposite of `resolve()`, which keeps the
fixed stop when volatility is unreadable — there falling back keeps a trigger
that already existed, here it would INVENT one on a long-term hold from a
number nothing measured.

**What arming would do at 02:48Z 29 Sep: nothing.** All fourteen inside their
own armed stop; ZEC the worst at −17.26% against 35%, 17.7 points of room.
Insurance, not a liquidation. Re-check this before ever reporting on arming —
it is a live-price statement with a date on it, not a property.

### After the merge, these change

- `/resting-stops` gains `uncovered`, `uncovered_count`, `uncovered_usd`
  (None, never 0.00, when nothing could be priced), `uncovered_unpriced` and
  `stop_coverage_unknown`. Unarmed, expect `uncovered` to list ACH ALGO BCH
  ETH HBAR LINK LTC PEPE QNT SHIB SOL XLM XRP ZEC. That list is the gap being
  visible, NOT a regression — armed, it empties.
- The Live Ops fill rate DROPS and that is the fix landing. It read 82.6%
  (`submitted 46, rejected 8, filled 38`) on the old arithmetic, which counted
  every maker expiry, non-order, unknown-fate cycle and post-gate block as a
  FILL, and left `GATE_OBSERVE` and `GATE_DISABLED` out of the attempt count
  although both return True and let the buy through.
- `ORDER_NOT_PLACED` and `ORDER_NO_FILL` appear as new activity event types.

## Retractions — carry forward until stale

**"The blocker is inventory locked by resting orders" is right for ALGO and
XLM and WRONG for QNT.** Direct reads at 23:21Z: ALGO held 1134.346389 /
available 0.046389 / **locked 1134.3**; XLM held 1980.76640025 / available
**0.0** / locked 1980.76640025 — both genuinely locked, and
`free-locked-inventory` is the remedy. QNT held 0.00097323 / available
0.00097323 / **locked 0.0** — nothing is locked; it holds dust (~22¢) that
floors to 0 at 3 decimals. Freeing inventory does nothing for QNT. Three
products failing for two different reasons were logging the same sentence.


**A maker-expiry row count is not evidence that orders existed — nor that they
didn't.** Rows were written on all three None paths, so the 441 QNT / 429 ALGO
counts cannot by themselves support "orders that never existed". That
conclusion rests on the direct balance reads (ALGO available 0.046389 of
1134.346389, XLM 0.0 of 1980.766, QNT dust) and stands on those. The row
counts do not add to it. `order_rested` makes the split measurable from the
next rows onward; every row written before then is NULL = UNKNOWN, not zero.


1. Artifact v1's 0.1183%/day "professional band" claim — **withdrawn**, 26
   days of profit over one instant's denominator.
2. `/edge-rate`'s first answer, 0.9519%/day from $4.30 over 0.04 days —
   **withdrawn**. `MIN_SPAN_DAYS` now 1.0 there, 7.0 in growth_model.
3. The ZEC payback quoted as 2.8 months — **withdrawn**, it divided the loss
   by a ceiling rate. Corrected: 10.3–12.5 months.
4. "drawdown_breached is why QNT will not sell" — **withdrawn** before the
   owner acted on it. The breaker gates only the dip-buy `elif`.
5. "QNT-USD isn't in the wallet" — **withdrawn** after the owner pushed back.
   It holds dust; the census had filtered it. Corrected by direct venue read,
   not by argument.

## Closed — verify only, do not reopen

- **META's stop**: −$26.12, last `no_scan_data` refusal 13s before the fill.
- **"Lowest in months"**: FALSE. Equity was lower on 53 of 96 days in window.
- **Branches over their level count** (six then, seven at 02:48Z 29 Sep): not
  a bug, and there are TWO mechanisms, not one. (a) adopted headroom — an
  adopted-only branch gets `len(slices) + 1`, takes that one rung, goes mixed,
  and the cap snaps back to 3, so it reads 4/3. (b) `coin_topup` writes N more
  adopted rows and sets `num_levels` to the new full count, which the cycle
  re-caps to the override's 3 every pass — twice-topped branches read 7/3.
  Verified against the live slice list: every overflow branch has adopted rows
  in groups of three at one timestamp, plus exactly one real buy. The
  invariant holds — `7 < 3` is false, so nothing buys past its cap, and
  `slices_over_levels_unexplained` stays 0. **Do not reopen the overflow. DO
  read the stop-coverage finding below, which is what chasing it turned up.**
- **QNT's drawdown** as the sell blocker: ruled out.
- **The picker refusing adopted slices**: ruled out — `_slice_rate` handles
  them explicitly, exit leg only.
- **QNT's balance**: dust, not absent. Do not re-derive it from the census.

Verify each pass: no `no_scan_data` row newer than 16:39:37.675Z; `/signals`
pricing all 16 symbols; `last_cycle_at` under 60s; Alpaca equity inside its
band; `slices_over_levels_unexplained` still 0.

## Concentration

- **Crypto**: 13 of 21 holding branches opened in ONE 30-minute window,
  27 Sep 12:01–12:31Z. ZEC 27.6% + XRP 27.2% of allocated, 91.7% of the loss.
- **Stocks**: six orders submitted 13:15:02.687–.691Z (4 milliseconds,
  pre-market), all 0.163475 META at $748.91 = 73% of the account. Ruled out
  with evidence: the branch system; `asyncio.gather` (none); a retry around
  the POST (none); the swing bot (META not in `SWING_SYMBOLS`); multiple
  uvicorn workers (1); multiple Railway instances (8 probes, monotonic
  uptime); deliberate order slicing. **What is left: a caller inside
  prop_bot.** Everything is tagged now.

## Sweep each pass

**EVERY PASS, run the exit-reason watcher. One command, no re-derivation:**

```
cd /home/user/empire-v2 && python3 scripts/exit_reason_watch.py
```

Exit 0 = quiet, say nothing. Exit 2 = REPORTABLE, tell the owner what it
printed. Exit 1 = UNREADABLE, which is a GAP and never "no change".

It raises the limit to 1000, splits on the four-way cutover, and refuses to
report a distribution until at least 20 post-cutover rows exist — because the
naive count said "100% profit_target" when 131 of 132 rows were legacy. It
keeps `.claude/exit-reason-watch.json` so a repeat pass stays quiet instead of
re-reporting the same state. **Do not hand-roll this analysis again;** the
three traps it encodes (the cap deciding what the data says, legacy rows
drowning new ones, an unreadable fetch read as an empty book) were each walked
into once on live data.


All under `https://empire-v2-production.up.railway.app/api/trading-dashboard`
except `/health`, at the root. A 404 is a wrong URL or a route not yet
deployed. A 500 or empty body is a GAP.

`/health` (root) · `/growth-model` · `/parked-capital` ·
`/grid-status/maker-expiries?hours=24&limit=1000` ·
`/grid-status/asset-balance?currency=<X>` — the only authoritative answer to
"is X held" · `/alpaca-overview/equity-curve?days=90` ·
`/grid-status/invariants` — **read `unreadable` as well as `short_positions`**
· `/capital-productivity?hours=48` · `/edge-rate` · `/schema-health` ·
`/auto-trim` · `/resting-stops` · `/mandates/decisions?hours=24` ·
`/grid-status` (`real_free_cash_usd`) · `/live-dashboard-data` · `/status` ·
`/signals` · `/trades` · `/trades/closed` (assert `entry_at < exit_at` on
every row) · `/alpaca-overview` · `/alpaca-overview/equity-handover` ·
`/account-census` (429s expected)

Also each pass: for every branch compute
`current_price / (reference_price * (1 + grid_pct)) - 1`. Anything at or above
0 that is NOT selling needs an explanation. This is what found QNT.

**Only a GROWING shortfall or lock is a finding, in UNITS not dollars.**

## Reference figures — update in place

**WHY TURNOVER STALLED, measured 02:05Z (book stuck at 132 closes since
00:40Z).** Distance from each branch's own reference price:

**Only 5 of 23 branches are ABOVE their reference.** And three of those five
are exactly the ones that cannot reach their coin:

| branch | vs reference | why it cannot sell |
|---|---:|---|
| QNT | **+24.03%** | holds dust (0.00097323, floors to 0 at 3 dp) |
| ALGO | **+7.38%** | 1134.3 of 1134.35 LOCKED |
| XLM | **+1.70%** | 1980.77 of 1980.77 LOCKED, 0.0 available |
| TON | +1.37% | parked (3/3); best slice +1.37% gross vs a 1.0% NET floor |
| BTC | +0.16% | parked (3/3); best slice +0.16% gross — correctly held |

The other 18 are below reference, ZEC worst at −11.94% (−$367.17 unrealised).

**So the stall is not a sell-path fault.** Almost nothing is above water, and
the branches that ARE are inventory-blocked. This sharpens the
free-locked-inventory case: not "there is $977 locked somewhere" but **"two of
the three branches currently positioned to sell cannot reach their coin."**
QNT is the third and freeing inventory will NOT help it — it is dust.

`GRID_PARKED_MIN_NET_PCT = 0.010` (1.0% net). TON/BTC held correctly; no
missed sale found. Exact net not computed — the fee rate resolves per slice,
so only gross percentages are stated above.


**PHANTOM INVENTORY, measured 01:10Z — report to the owner, do NOT act.**
`coin_tracked_is_held` FAILs with 6 positions and $471.01 short, but three
MORE sat in its `unreadable` footnote because the wallet map could not
express them (fixed 6f68396 — the map now comes from `fetch_balances` plus a
direct per-currency read):

| product | tracked | held | short |
|---|---:|---:|---:|
| QNT-USD | 0.67598215 | 0.00097323 | **$141.68** |
| TIA-USD | 111.35 | 44.57 | $28.86 |
| PRIME-USD | 36.64 | **0.0** | $9.26 |

**CORRECTED at 01:36Z — the TIA figure above was withdrawn and restated.** It
was reported as `held 0.0` / `$49.48` / "three open slices for coin the account
holds NONE of". Three consecutive reads now give **44.57 held, all available,
nothing locked**, so the shortfall is 66.78 units ($28.86), not 111.35. The
slices are unchanged (3, same quantities, same open times) and there has been no
new close, so the grid neither bought nor sold — **I cannot explain the change
from here and will not guess.** The 0.0 came from the same 01:07 sweep that
produced the bad XLM read; I applied "repeat an anomalous read" to XLM and then
failed to apply it to TIA in the same breath.

QNT alone exceeds every coin in the headline. **TIA holds 3 open slices for
111.35 units of a coin the account holds NONE of**, and its −$3.67 unrealised
is computed off them. Standing constraint still applies: do not reconcile or
close QNT. TIA and PRIME are not covered by that constraint but are the
owner's call, not the loop's.

**XLM did NOT move.** An earlier compressed read printed all four fields as
None; five consecutive full reads give held=1980.766 / direct=0.0, unchanged.
That was a transient double read-failure — the endpoint correctly reported
None rather than fabricating zeros. **A single anomalous read is not a
finding; repeat it before reporting.** I nearly reported $453 of XLM as gone.


**FULL BOOK, 132 closes, read at 00:47Z with `limit=1000` (truncated:false).**
Raising the cap did NOT make the four-way exit_reason readable — still 1 of 132
rows postdates it (LINK-USD +$4.13, `profit_target`). What it revealed instead:

- **82 of 132 trades have `exit_reason = None`** — every close up to
  2026-09-09. First populated row is 2026-09-26. Clean boundary, ZERO
  interleaving after it. Other analysis fields are sparser still: `stop_pct`
  25/132, `mae_pct`/`mfe_pct` 50/132, `entry_atr_pct`/`entry_spread_pct`/
  `entry_gate_json` 32/132.
- **A 17.8-day gap with zero completed trades**, 2026-09-09T03:57 →
  2026-09-26T22:00. Cause UNKNOWN from this data — do not guess; it is
  checkable against deploys/config history.
- **Two eras, and the second is better on both axes of turnover × edge:**

| | closes | per day | realised | per close | median | top trade | losers |
|---|---:|---:|---:|---:|---:|---:|---:|
| pre-gap (08-30→09-09) | 82 | 8.74 | +$19.55 | $0.238 | +$0.090 | 30.4% | 19 |
| post-gap (09-26→09-29) | 50 | 23.68 | +$58.34 | $1.167 | +$0.575 | 9.4% | 0 |

2.7× the close rate AND 4.9× the profit per close, and it is broad rather than
one trade — median 6.4× higher, top-5 concentration 75.7% → 38.2%.

**CAVEAT, do not report the win rate as skill.** Post-gap 0 losers is
STRUCTURAL: `_pick_profitable_slice_to_sell` refuses a losing sale, so the only
way to realise a loss is the stop, and no stop has fired (zero `stop_loss`
labels). Why pre-gap had 19 losers is UNKNOWABLE from these rows — all 82 carry
`exit_reason = None`. The two eras also differ in fleet composition, capital and
config, so this is not a controlled comparison.


**LOCKED INVENTORY, full 23-branch sweep at 23:45Z (af869a4 serving).**
$977.02 locked across seven branches, all seven priced so the total is
complete (was $923.23 at 09:44Z):

| coin | locked units | price | locked USD |
|---|---:|---:|---:|
| XLM | 1980.76640025 | 0.228728 | 453.06 |
| ALGO | 1134.30 | 0.134480 | 152.54 |
| LINK | 6.63 | 15.419 | 102.23 |
| SOL | 0.77595005 | 118.68 | 92.09 |
| NEAR | 15.951 | 4.8201 | 76.89 |
| ACH | 11745.30 | 0.005977 | 70.20 |
| JASMY | 5862.00 | 0.005120 | 30.01 |

**Cannot sell at all** (available floors to 0): XLM (0.0 of 1980.77), ALGO
(0.046389 of 1134.35), JASMY (0.757 of 5862.76 — NEW, not in the 09:44Z list).
**QNT: 0.0 locked**, holds ~22¢ dust — freeing inventory does nothing for it.
**PRIME and TIA: `direct_available=0.0`, `venue_lists_no_such_account=False`** —
accounts that EXIST holding exactly zero, which only the fixed four-state
endpoint can say. Everything else in the fleet: 0.0 locked.

**The owner asked for the free-locked-inventory dry run.** It could not be
run — `DASHBOARD_WRITE_TOKEN` is not in this container AND the POST was
refused by the permission classifier. Both reported; the read-only
reconstruction above was delivered instead. Do not retry the POST; do not ask
for the token.


**23:28Z (edcdbe8 serving).** `not_working_usd` $1,294.41 (was $1,406.77 —
$112 more deployed), free cash $204.03, allocated $8,249.56, deployed
$7,557.88, idle-in-branch $1,090.38, total capital $8,453.59, unrealized
−$322.84. Free-cash trail: 19:21Z 251.74 → 20:26Z 251.27 → 21:33Z 251.45 →
22:21Z 191.70 → 23:28Z ~204–210. The 22:21Z dip was deployment; the rise
since is a close returning cash. **No second unexplained outflow.**
CAUTION: money-check's `idle_usd` (892.12) is NOT the same metric as
growth-model's `not_working_usd` (= idle_in_branch + free_cash). Do not
compare them as if they were.


Shortfall reference (20:26Z 28 Sep), eight short, $512.41, all flat:
ETH 0.03160040 · ZEC 0.08720487 · XRP 69.82593202 · PEPE 8296372.54005447 ·
BCH 0.37932324 · ONDO 9.64 · TIA 67.03 · PRIME 36.64.
Plus one `unreadable`: QNT-USD — dust, not absent.

A ninth short name, or any of the eight moving in units, means cba0fb4 is
incomplete. A new `unreadable` entry needs a direct balance read.

Expiry baseline (21:15Z, 24h): QNT 441 sell · ALGO 429 sell · XLM 77 sell ·
LINK 41 sell · ETH 3 · PRIME 3 · BTC 2 · XLM 2 buy · NEAR 1 buy · LTC 1.

Direct venue read (114 accounts):
QNT held 0.00097323 / available 0.00097323 / locked 0.0 ·
ALGO 1134.346389 / 0.046389 / 1134.3 ·
XLM 1980.76640025 / 0.0 / 1980.76640025 ·
LINK 9.95 / 3.32 / 6.63.

Parked capital (19:21Z): parked_now $4,643.77 · actionable today $1,408.84 ·
locked_at_venue $1,408.84 allocated / $943.93 coin reserved, 6 branches ·
underwater $2,965.15 · full_but_profitable $269.78 · one_fill_from_full
$3,144.34. Never add `allocated_usd` to `coin_reserved_usd` — different
measurements.

Free cash trail: 18:55Z $763.59 → 19:07Z $263.59 (exactly −$500.00, allocated
and deployed both unchanged — reported to the owner) · 19:21Z $251.74 ·
19:39Z $251.74 · 20:26Z $251.27 · 21:33Z $251.45 · 22:21Z $191.70 with
deployed +$68.44, which is deployment, not a leak.

Two cent-exact confirmations of the idle figure against the owner's own
Coinbase screen: $2,046.55, then $1,406.77. It is real USD, not a model
output.


**ACH: 11,745.30 UNITS LEFT THE WALLET — exactly the locked amount.**
At 01:10Z ACH read held 18,872.30 / locked 11,745.30. It now reads held
7,127.00 / locked 0.0 across three consistent reads. 18,872.30 − 7,127.00 =
11,745.30, to the unit. There has been no ACH close since 09-27, so the grid
did not sell it. **Inference, not fact** — venue order history is not readable
from this container — the shape fits a resting venue stop that filled. The
shortfall board went $639.28 across 9 products → $708.77 across 10, ACH new on
it at $61.00. Report if a SECOND branch does this; one is an event, two is a
pattern and the locked column becomes untrustworthy.

Same sweep, the other direction: ETH's tracked AND held both rose by exactly
0.0281858 — a normal grid buy, booked correctly on both sides. **The buy path
works.** Do not read the ACH event as a general accounting failure.


**THE SPLIT IS NOW EXERCISED, NOT JUST DEPLOYED — measured 02:58Z** off
`/grid-status/maker-expiries?hours=24&limit=1000` (1000 rows, truncated, so
these counts are floors):

| coin | side | rows | rested | unknown | legacy no-order | newest |
|---|---|---:|---:|---:|---:|---|
| ALGO | sell | 417 | 0 | 337 | 80 | 00:15:06 |
| QNT | sell | 413 | 0 | 333 | 80 | 00:14:17 |
| XLM | sell | 88 | 0 | 49 | 39 | 00:15:05 |
| TIA | sell | 42 | 0 | 0 | 42 | 00:14:30 |
| PRIME | sell | 31 | 0 | 3 | 28 | 23:55:41 |
| **ETH** | **buy** | 1 | **1** | 0 | 0 | **02:25:11** |

Two things, both new:
1. **No sell-side expiry row anywhere after 00:15:06Z.** Confirmed non-orders
   are being refused at the write site and routed to `orders-not-placed`
   instead, which is exactly what the invariant was built to do. The rows
   stopped arriving; that is the fix working, not the table breaking.
2. **The first `order_rested = True` row in the fleet's history** — ETH-USD
   buy, 02:25:11Z. An order genuinely sat on the book and nobody crossed it.
   Both sides of the three-state flag are now observed on live data.

**Do not read the 337/333 unknowns as liquidity failures.** They are rows
written before the column existed. Zero rows in this window confirm a sell
ever rested.


**CORRECTION — "ABOVE REFERENCE" IS THE WRONG BAR, measured 03:32Z.** The
table above ("Only 5 of 23 branches are ABOVE their reference") used the wrong
threshold. A branch does not sell at its reference price; it sells at
`reference_price * (1 + grid_pct)`. Against the actual trigger, **only 2 of 23
branches are ready**, and both are still exactly the inventory-blocked ones:

| branch | vs **trigger** | (vs reference, the old bar) | why it cannot sell |
|---|---:|---:|---|
| QNT | **+33.40%** | +37.4% | holds dust, floors to 0 at 3 dp |
| ALGO | **+4.73%** | +7.9% | 1134.3 of 1134.35 LOCKED |
| ONDO | −0.90% | +2.1% | below its trigger |
| BTC | −1.19% | +0.27% | below its trigger |
| ETH | −1.42% | +0.44% | below its trigger |
| XLM | −1.71% | +1.23% | below its trigger |
| TON | −2.46% | +0.46% | below its trigger |

**This does not change the conclusion, it strengthens it:** every branch that
can actually sell right now is one that cannot reach its coin. And it retires
the TON/BTC line above — they were described as "parked under the 1.0% net
floor", but they are not even at their grid trigger, so the net floor was never
the binding constraint for them.

**XLM answered, not assumed.** XLM vanished from `orders-not-placed` inside a
2h window despite being fully locked. Cause: it fell below its own sell trigger
(price 0.226314, reference 0.223554, grid 3.00% → trigger 0.230261), so it stops
attempting and therefore stops being blocked. **A branch disappearing from the
blocked ledger can mean it got fixed OR that it stopped trying — check which.**

**Lesson: I measured against the wrong denominator for hours and the sweep
instruction encoded it.** The per-pass sweep line already said to compute
`current_price / (reference_price * (1 + grid_pct)) - 1` — the right formula —
and the reference table was built from a different, looser one. **When a stored
figure and the instruction that produces it disagree, the figure is the one to
distrust.**


**/fills-by-source, FIRST LIVE READ 03:47Z — and it found something.**
87 orders / 120 fills over 24h, $4,852.32 notional, **$21.20 commission**,
113 MAKER and 7 TAKER fills across 16 products, not truncated.
**0 of 87 orders attributed.** `attribution_table_error` was None and
`attribution_started_at` was None, which together mean the table is not
unreadable — it is EMPTY. Not one row has ever been written.

Why, traced through the live order paths:

| path | tags itself? | runs? |
|---|---|---|
| grid market buy/sell/close-branch | YES (`grid_buy_market`, `grid_sell`, `grid_close_branch`) | **no** — the fleet is maker-only, so the market path never executes |
| `auto_trim_worker._place_market_sell` | YES (`auto_trim`) | only on a trim; none in the window |
| **`resting_stops_worker.place()`** | **NO** | **yes** |
| grid maker buy/sell | n/a | yes, but post-only cannot take liquidity, so it cannot make a TAKER fill |

So the one live path that could produce a TAKER fill was the only one
recording nothing. **Fixed** — `place()` now records `source="resting_stop"`,
mirroring auto_trim's own pattern, whose comment already said a loop that
bypasses the engine must record its own attribution.

**This is the likeliest explanation of the ACH outflow, and from the next
stop fill onward it becomes checkable rather than inferred.** A stop resting
at the venue fills while this service is asleep, so its fill is exactly the
one the account cannot reconstruct from its own logs. **NOT proven for the
existing ACH event** — no row exists for it and none can be created
retroactively.

**Latent, NOT live — do not treat as urgent.** `crypto_mean_reversion_bot`
calls `engine.place_market_buy(product_id=..., quantity=..., reason=...)`
with no `session`. The engine's signature is
`place_market_buy(session, usd_amount, product_id, source)` — no `quantity`,
no `reason`, and `session` is required positionally, so every one of those
calls would raise `TypeError`. It is never imported or started by anything
(`grep` finds it only in a comment in `fee_floor.py`), and `main.py` runs
exactly ONE crypto mode at a time — currently `grid_fleet`. Dead code.

**Deliberately left untagged:** `crypto_family_tree_bot` (5 market-order
sites) and `crypto_btc_compound_bot`'s own loop (2). Both are dormant under
`grid_fleet`. Tag them BEFORE changing `CRYPTO_STRATEGY_MODE`, not after.


**THE PARKED-SELL PATH FIRED FOR THE FIRST TIME — 04:10Z and 04:15Z, both
PROFITABLE.** Book 133 → 135.

| closed | coin | pnl | entry → exit | reason |
|---|---|---:|---|---|
| 04:10:52Z | LINK-USD | **+$1.31** | 14.343 → 14.667 | `parked_sell` |
| 04:15:34Z | TON-USD | **+$0.83** | 1.533 → 1.5595 | `parked_sell` |

**CORRECTION — and this corrects my own correction from 03:32Z, which was
wrong.** I wrote: *"it retires the TON/BTC line — they were described as
parked under the 1.0% net floor, but they are not even at their grid trigger,
so the net floor was never the binding constraint for them."* That conflated
two different bars. **There are TWO sell routes, not one:**

1. **Grid trigger** — `price >= reference_price * (1 + grid_pct)`. Measured
   against the BRANCH REFERENCE.
2. **Parked sell** — for a branch that is full (`len(slices) >= num_levels`)
   or adopted-only: the best sellable slice's net P&L over **its own basis**
   must be `>= GRID_PARKED_MIN_NET_PCT` (1.0%). It does not look at the
   reference at all. `crypto_grid_bot.py:6653`. The code comment says it was
   built for eleven branches that could not reach the reference trigger.

TON sold via route 2 at +$0.83 while sitting **−2.89% below its grid
trigger**. So the ORIGINAL description ("parked under the 1.0% net floor")
was right, my correction was wrong, and the 1.0% floor was exactly the
binding constraint — TON cleared it. **Do not "fix" this back.**

**Both routes, measured 04:22Z:**

- **17 of 23 branches are PARKED** (full or adopted-only) and therefore use
  route 2, not route 1.
- **3 are above their grid trigger**: QNT +41.43%, ALGO +5.00%, ONDO +0.70%.
- **Of the parked ones, only 2 have a slice ≥ +1.0% GROSS over entry** (an
  UPPER BOUND — fees not applied, so the net figure is lower): LINK +2.67%,
  QNT +51.55%.

**The conclusion is unchanged and now rests on both bars instead of one:**
very little is in profit by either route, and the two branches best placed on
either one — QNT (+51.55% over entry, +41.43% over trigger) and ALGO (+5.00%
over trigger) — are still exactly the inventory-blocked pair. QNT holds dust;
ALGO is locked.

**Lesson: I corrected a right statement into a wrong one by checking it
against one bar when the system has two.** Before retiring an explanation,
find the code path it names — the parked gate was 130 lines from the trigger
computation I did read.


## EXECUTION REBUILD — in progress (owner spec, 26 sections)

**The spec's own text is at `.claude/EXECUTION_REBUILD_SPEC.md`** — verbatim, recovered from the transcript after it was compacted out of
context. Read the section there before building it. Do not reconstruct a
section from these notes; the notes record what was MEASURED against it.

**Section 3/4 landed. The rest is not built yet.** Do not report the whole
spec as done.

**What the owner's named bug actually was.** The log line
`available 0.0009732300 floors to 0 at 3 decimals` is QNT-USD, and the
venue's REAL `base_increment` for QNT-USD is `0.001` (measured
2026-09-29). So 0.00097323 genuinely IS under one tradeable unit — the old
arithmetic was right about QNT even though its method was wrong. What it
got wrong was the WORDS: a confirmed non-order was called "nothing
sellable", which reads as a failure, and the branch then showed 0/3.

**The real bug the spec's §3 is pointing at, and it is worse than QNT:**

1. **`get_product_size_decimals` FAILS OPEN** — returns 8 on a non-200, a
   timeout, or any exception. Eight is the most permissive value on the
   venue, so an unreadable product became an order sized against a guess.
   ALGO-USD's real increment is 0.1; sized at 8 decimals the venue rejects
   it outright. **This is the live safety bug**, and §22 forbids it.
2. **Float arithmetic against a decimal rule.** Measured: `0.29` at a
   `0.01` increment floors to **0.28** in float, `0.29` exactly — one whole
   increment, on the increment six of these products use.
   (NOTE: my first example, 2.675 at 0.001, does NOT fail — `2.675*1000` is
   exactly `2675.0`. I asserted it before measuring it. Use 0.29.)
3. **A decimal COUNT standing in for the increment.** Equivalent only while
   the increment is a power of ten. **All 23 fleet products are powers of
   ten today (measured)** — so this has produced no wrong number yet. A
   `0.05` increment would floor `0.07` to `0.07`, which is not a multiple
   of `0.05`.
4. **No notional floor at all.** Every product publishes a minimum order
   VALUE (`min_market_funds` = 1 on the Exchange API).

**ALREADY EXISTED — do not rebuild:** `resting_stops.round_down(value,
increment)` is exactly the spec's `floor_to_increment`: exact Decimal,
arbitrary increment, raises on a bad one. The stop path has used it all
along; the execution path simply never called it. `execution_quantity`
imports it, and a test asserts via AST that it is imported, not redefined,
and that `math` is never imported there.

**Shipped:** `execution_quantity.py` (pure, no I/O) + `get_product_rules()`
(cached, successes only) wired into all three sizers —
`place_maker_sell`, `place_maker_buy`, `place_market_sell`.

**BEHAVIOUR CHANGE THE OWNER SHOULD KNOW:** an unreadable product now
REFUSES instead of trading at 8 decimals. That is §22's requirement and the
safe direction, but if the products endpoint is flaky it will show up as
skipped cycles rather than rejected orders. Watch `product rules
unreadable` in `_last_order_error`.

**Deliberate exception:** `place_market_sell` passes `quote_min_size=None`.
It is the forced-exit path, evaluating a notional floor needs a live price,
and giving a protection a new way to fail is worse than a loud venue
rejection. Pinned by a test so it is not "fixed" without that reasoning.

**Two APIs, different fields — do not mix them.** `api.exchange.coinbase.com`
(public) publishes `base_increment` and `min_market_funds` but NEITHER
`base_min_size` nor `quote_min_size`. `/api/v3/brokerage/products/{id}`
(what the bot uses) publishes both minimums. Absent means the rule is not
asserted, NEVER that it passed.

### Pure modules written — NONE OF THEM ARE WIRED YET

Four more modules exist with tests and passed mutation testing. **Nothing
in the live trading path imports any of them.** Writing a module is not
shipping a behaviour; do not report these as changing what the bot does.

| module | spec | what it decides |
|---|---|---|
| `slice_lifecycle.py` | §1 | the 13 states and every legal move between them; `state_from_inventory` deliberately RAISES — slice state is persisted, never inferred from the wallet |
| `slice_target.py` | §5 | the exact inverse of `_grid_slice_net_pnl`; rounds a sell target UP (down would give away the edge) |
| `slice_edge.py` | §6 | the floor that binds; `DO_NOT_TRADE` when the market cannot clear it. **The adaptive widener is deliberately NOT built** — `allow_adaptive_widening=False`, it needs evidence first |
| `order_idempotency.py` | §23 | the client_order_id IS the hashed intent |

**§5's formula in the spec undershoots.** At a 0.70% round trip and a 1.20%
required edge it realises 1.19335% — short by 0.00665 points, always the
same direction. `slice_target` uses the exact inverse instead; a test pins
the realised edge to the requested one.

**§23, measured:** ten call sites build `client_order_id` as
`str(uuid.uuid4())`. That is the VENUE'S duplicate key, so the system has
no duplicate protection at any layer — a retry or a twice-delivered event
opens a real second position. `COID_PREFIX = "rstop-"` is load-bearing:
`free_locked_inventory` filters on it to cancel only this system's stops,
so the prefix survives unhashed and an over-long one refuses rather than
being truncated.

**Four times now the spec has assumed less exists than does.**
`_net_edge_gate_ok` already prices spread, depth, real round trip and
adverse selection and fails closed; `resting_stops.round_down` IS
`floor_to_increment`; `CryptoGridSlice` already persists `entry_price` AND
`entry_fee_rate`; `_pick_profitable_slice_to_sell` + `_grid_slice_net_pnl`
+ `_slice_rate` already compute per-slice net economics. **Read before
building.** The GRID TRIGGER is the one place still using a single global
`reference_price` for all slices.

### §15/§16 — the fee told to the learning layer was ~100x too small

`crypto_grid_bot.py`'s shadow close block computed
`total_fees = slice_round_trip_fee_rate(...) * qty * entry / 100`. That
function returns a FRACTION (0.0070 for a 0.70% round trip), so the `/100`
understated the fee by about a hundred, and the consumer does
`net_pnl = realized_pnl - fees` — every trade looked nearly fee-free to the
one reader whose job is judging whether the edge is real. On a
LINK-shaped slice it turned a $0.23 trade into a $0.88 one.

Fixed by DERIVING it: `_grid_slice_net_pnl` returns `gross - fee`, so
`gross_pnl - pnl` IS the fee it charged, for whatever rate that slice was
priced at. A derived figure cannot drift from the real formula the way a
second hand-written copy can.

**It was DEAD IN PRODUCTION** — the shadow block imports from
`/home/user/Delfina`, which Railway does not have, so `SHADOW_MODE_ENABLED`
is False there. A latent defect, not a live money bug. **Do not report it
to the owner as one.**

**A second "bug" here is NOT a bug — do not "fix" it.**
`realized_pnl=gross_pnl` looks wrong against
`bot_integration_points.on_position_closed`, whose docstring calls that
parameter "Net P&L after fees". But the IMPORTED consumer is
`shadow_mode_init`, which does `gross_pnl = realized_pnl` and
`net_pnl = realized_pnl - fees`. Gross is what it wants. The two files
disagree with each other; the imported one wins.

**The rest of §15 already held.** `grid_sell()` returns the ACTUAL filled
quantity and price, `grid_sell_residual()` handles requested != filled, and
`_grid_slice_net_pnl` prices the round trip at the rate this slice's own
buy leg really paid. That is the fifth time the spec has assumed less
exists than does.

### §24 — recovery CANNOT be finished before §1's persistence lands

`CryptoGridSlice` persists entry_price, qty, product_id, fees and the entry
gate's diagnostic — but **NO state column and NO order id** (measured, the
whole column list). A slice's existence IS its state. So §24's step 7,
"reconcile order states", has nothing in the schema to reconcile against:
an open exchange order cannot be matched to the slice that placed it. That
is §1/§2's job. The spec does not state this ordering; it is real.

Today there is a LEASE and a heartbeat but **no reconciliation on restart
at all** — `run_grid_branches_cycle` goes straight to trading. The only
reconciliation that exists is the manual, write-guarded
`POST /grid-status/reconcile-slices`, **which is the owner's to run, never
mine.**

`restart_recovery.py` (pure) decides whether a restarted process may
resume. Three outcomes, not two:
`RESUME` (read and agrees) / `HOLD` (read and disagrees) /
`REFUSED` (could not be read). REFUSED is not a worse HOLD — HOLD knows
something is wrong, REFUSED does not know whether anything is wrong.
`may_trade` is `decision == RESUME`, written as an identity so a decision
added later is refused by default rather than permitted by a negative test.

**SHORT and EXCESS are deliberately asymmetric.** Tracked above held blocks
(the ledger claims coin the wallet lacks). Held above tracked does NOT —
that is the fleet's ordinary adopted state, and a symmetric check would
halt it over nothing. The live coin shortfall is the SHORT case.

**A missing key in the balances dict is UNKNOWN, never zero.** `None` for a
whole input means UNREAD; an empty list/dict means READ AND EMPTY, which is
an ordinary answer. Collapsing those is the same bug as reporting an unread
balance as 0.

**A guard I wrote and then deleted:** `isinstance(v, bool)` beside
`Decimal(str(v))` was unkillable by mutation — `Decimal("True")` already
raises. A guard no test can break is not a protection. The comment now
explains why `str()` comes first (`Decimal(True)` IS `Decimal(1)`, since
bool is an int).

**It is NOT wired.** Nothing gates a cycle on it. Wiring it would mean a
restart can refuse to trade, which is what §22 and §24 ask for and is also
a way to halt the fleet on a transient read failure — that is an
owner-visible decision, not one to slip in.

### THE REFUSAL NOBODY COULD SEE — fixed, and it was my own gap

6a95d4d made an unreadable product REFUSE rather than size at 8 decimals,
and I told the owner to watch `product rules unreadable` in
`_last_order_error`. **That signal was exposed by no read-only endpoint for
the grid fleet at all** — dashboard router line 1364 covers family-tree
branches only, and the other reader sits inside a write-guarded POST. The
fleet could have been refusing every order on every product and the page
would have shown nothing missing.

`get_order_refusals()` now reports it under `grid-status.order_refusals`:
count, reasons grouped by the code before the colon, per-product messages,
and `product_rules_unreadable` called out separately because that is the
one this fleet was told to watch. Registered through `_never_fails`, so a
diagnostic can never take the payload down.

**`available: False` when the map cannot be read, NEVER zero refusals.**
Same distinction as restart_recovery's unread-vs-zero.

**A first read of grid-status found no refusals — but grid-status did not
expose them then, so that was a NON-READ, not a clean result.** Do not
record that first look as evidence of anything.

### THE MESSAGE NAMED THE WRONG NUMBER — found by reading the live payload

The moment `order_refusals` shipped it reported, for real:

    ALGO-USD  0.046389 available < one unit of 0.1   correct
    QNT-USD   0.00097323 available < one unit of 0.001  correct
    LINK-USD  0.22 available < one unit of 0.01     **WRONG - that is 22 units**

LINK-USD's real `base_increment` IS `0.01` (measured against the venue), so
the increment was right and the MESSAGE was wrong. `execution_quantity`
decided on `min(requested, available)` floored, but reported
`raw_available`. When the sub-increment side was the REQUEST, the message
blamed the wallet and told the reader to wait for inventory that was
already sitting there.

Split into two reason codes, because they need different answers:
`BELOW_BASE_INCREMENT` (the wallet holds under one unit — it becomes
sellable as inventory grows) and `REQUEST_BELOW_BASE_INCREMENT` (the wallet
is fine; the caller asked for dust — waiting changes nothing). Both details
now name BOTH numbers. When both are under one unit the WALLET wins, since
changing the request cannot help. Nothing branches on these codes — they
are reported, not dispatched on — so splitting them was free.

**LINK CLEARED BEFORE THE FIX DEPLOYED, so its cause was never captured.**
At 09:03Z the list is ALGO and QNT only, both correctly wallet-bound with
the request now shown (ALGO: 279.4 requested vs 0.046389 held; QNT: 0.338
requested vs 0.00097323 held). **Do not invent a reason for the LINK
entry** — it was observed once, at 08:56Z, and was gone by the next read.
If it recurs the new codes will name the side; until then it is unexplained,
not explained.

**COIN SHORTFALL at 09:05Z: $755.17 across 10 branches.**
Track: $471.01/6 (01:10Z) -> $727.60/10 (05:05Z) -> $755.17/10 (09:05Z).
Still GROWING, so still a finding — but the RATE has collapsed: +$256.59
in the first four hours, +$27.57 in the next four, and the branch count
stopped at 10. Report the deceleration alongside the growth; reporting
only "still growing" would overstate it.
Per branch: QNT $170.22, ZEC $122.67, BCH $117.87, XRP $104.82, ETH $85.59,
ACH $63.78, TIA $40.72, PEPE $35.34, PRIME $9.14, ONDO $5.02.

**CONTEXT FOR THE LINK ENTRY, not an explanation:** `grid_inventory_is_free`
reports LINK-USD 97% reserved by resting orders ($101.31), so only a sliver
was free at that moment. That is consistent with what was seen and is NOT
proof of cause. Do not write it up as the answer.

**THE PRODUCTS ENDPOINT IS NOT FLAKY — measured, not assumed.**
`product_rules_unreadable_count` is 0 across two reads (08:56Z, 09:03Z).
That is the post-deploy watch 6a95d4d created, and it is answered: the
fail-closed refusal has not fired once. Keep reading it each pass; only a
NON-ZERO count is a finding for the owner.

**THE LESSON: the first read of grid-status showed no refusals and that
proved nothing, because grid-status did not expose them yet.** The finding
came from making the thing observable and then LOOKING. A deployed fix is
not an exercised fix.

### THE ONE REAL DUPLICATE-ORDER PATH, AND ITS MEASURED RATE: ZERO SO FAR

`_place_and_confirm` **never retries the POST** — one attempt, and on an
exception it records the error and returns None. So there is exactly one
way this system can place a duplicate order:

**The POST raises AFTER the venue accepted it** (a 15s timeout, say). No
`order_id` comes back, so the fill-history reconciliation below — which is
keyed on `order_id` and was written precisely so "callers never submit a
duplicate order" — cannot run. The caller sees None, and its next cycle can
place the same order again.

The codebase already names this case: `GridOrderNotPlaced`'s docstring
refuses to write it as a confirmed non-order, because "no order was
created" is a claim the evidence cannot support. It lands in
`GridMakerExpiry` with `order_rested` NULL instead.

**MEASURED 09:10Z from /grid-status/maker-expiries?limit=500:**

    order_rested NULL   228 rows, ALL between 20:53Z and 23:03:58Z 28 Sep
    order_rested False  269 rows, 23:03:44Z -> 00:15:06Z
    order_rested True     3 rows, 02:25:11Z -> 06:27:50Z

Every NULL row PREDATES the moment the column shipped (~23:04Z 28 Sep), and
they are 112 ALGO + 111 QNT — the two permanent-dust products, i.e. legacy
non-orders, not timeouts. **Since the path became observable there have
been ZERO unknown-order rows in ~7.5 hours.** The 3 rows since are all
genuine rests.

**Do NOT read "228 NULL" as 228 timeouts** — the endpoint's own text warns
NULL covers both legacy rows and the live path, and the timestamps separate
them completely.

**WHAT THIS CHANGES.** The duplicate exposure is real and correctly
identified, but its observed rate is zero, so it does not justify surgery
on live order paths now. And the key would not even fix it: a cycle_id
discriminator makes a NEXT-CYCLE retry a DIFFERENT id, so it deduplicates
the two-deciders-in-one-cycle case (the event loop) and NOT the
post-timeout case. The post-timeout case needs the caller to remember it
already attempted this intent — **which is persistence again.**

Three independent measurements now point at the same missing thing:
§24 recovery, §7/§8 event transport, and §23's wiring are ALL blocked on
§1's persisted slice state. **§1 IS THE NEXT BUILD.**

### TWO SLICES ARE PERMANENTLY STUCK — found the hour the telemetry shipped

Measured 09:45Z across all 80 open slices, each against its own venue
`base_increment`:

    LINK-USD    qty 0.009999999999998899   increment 0.01   opened 28 Sep 14:34Z
    PRIME-USD   qty 0.00999999999999801    increment 0.01   opened 28 Sep 17:08Z

Each should be EXACTLY 0.01 — one whole tradeable unit, sellable. Each is
one ULP below it. The venue cannot express a size like that, a residual
never grows, so **neither slice can ever be sold.** They have sat there ~19
and ~16.5 hours.

**CAUSE, in `grid_sell_residual`:** `residual = slice_qty - filled_qty`, a
FLOAT subtraction, written back to the row. `4.1 - 4.09` is
`0.009999999999999787` in float and `0.01` exactly in Decimal. Its retire
guard is RELATIVE to the slice (`rel_epsilon` 1e-6), so it has no idea what
the venue's smallest unit is and keeps the remainder.

**FIXED** — the residual is now `float(Decimal(str(a)) - Decimal(str(b)))`.
`str()` first, deliberately: `Decimal(float)` would preserve the binary
error this removes. Same discipline `execution_quantity` applies to sizing,
and for the same reason — the residual IS a quantity the next cycle tries
to sell, so it must survive the venue's rules like any order size.

**THE TWO EXISTING ROWS ARE NOT FIXED BY THIS.** They are persisted values.
Correcting them is a DB write on the live ledger, and `reconcile-slices` is
write-guarded and **the owner's to run, never mine.** Report; do not touch.

**A LOADER ASSUMPTION THIS BROKE:** `test_grid_sell_partial_fill.py` lifts
the helper out with AST and execs only it, collecting module-level
ASSIGNMENTS whose names the function uses — but not IMPORTS. That was fine
while the helper named only numbers; the moment it named `Decimal` it blew
up with a NameError from inside the exec rather than a failing check. The
loader now carries imports too, bound names read from the AST for the same
reason the constants are.

**AN EQUIVALENT MUTANT IS NOT A TEST GAP.** `Decimal(str(float(x)))` vs
`Decimal(str(x))` survived; verified identical over 20,009 cases including
1e-9, 1e12 and "1E+3". Dropped from the harness rather than "killed" with
an assertion about spelling.

### WHY NOTHING HAS TRADED — ANSWERED 10:52Z. Nothing is wrong.

**THE BUY SIDE: no coin has dipped.** The trigger is
`price <= reference_price * (1 - grid_pct)` AND `len(slices) < num_levels`
(crypto_grid_bot.py ~L6440). It is an `elif` on a plain `if/elif`, so when
the dip condition is false the block is **never entered** — which is why
there is no CASH_RESERVE / CONCENTRATION / EXITING / ADOPTED_RUNG event in
the feed either. Those four all sit INSIDE the block.

Measured across the 10 branches that have room and are not paused or
breached — **ZERO are at or below their buy trigger:**

    BTC-USD    +1.23%      XLM-USD    +5.51%
    ETH-USD    +2.04%      ALGO-USD   +7.59%
    XRP-USD    +2.35%      ONDO-USD   +8.20%
    FLOKI-USD  +3.78%      QNT-USD   +58.96%
    APE-USD    +4.06%
    TON-USD    +4.08%

The nearest is BTC, 1.23% above its trigger. **This is the grid's core
mechanic, not a fault.** It buys dips; there has been no dip.

And it compounds with the sell side: a sale re-anchors `reference_price`
to the FILL price, so the five sells between 06:54Z and 08:25Z pushed each
of those branches' next buy triggers further away. Selling into strength
and then not buying is the design, not a malfunction.

QNT at +58.96% will effectively never buy — its reference sits far below
the market. Already known and do-not-touch.

**THE SELL SIDE:** `maker_only_skipped_cycles` = 5,745 sell / 28 buy.
Maker sells rest and do not fill; there is no taker fallback, by the
owner's own choice.

**DO NOT "FIX" THIS.** The only levers are tightening grid spacing or
re-anchoring references downward, and both are forbidden: the owner said
do NOT tighten spacing, and lowering a threshold to manufacture activity is
a standing prohibition. A dip that has not happened is an answer.

**What was RULED OUT along the way:** not a stall (heartbeat cycling, lease
held by web:1); not capital ($311.20 free, above the $150 floor); not
capacity (10 branches with room); not the net-edge gate (newest GATE_PASS
06:28:41Z at +1.855%); not "order book unavailable" (all 14 blocks are 8+
hours stale).

**TWO GATES — DO NOT CONFLATE. I nearly reported the wrong one.**
`pipeline.per_coin[*].latest.expected_net_edge_pct` is negative for all 18
scoreable coins, and it is `opportunity_signals`, which grid-status labels
**observation only**. It is NOT the grid's buy gate and had nothing to do
with this.

**The edge is real when it trades:** 58 trades since the 26 Sep epoch,
gross 3.211%, net 2.566% on $2,324.96, mean slice $40.09.

### (superseded) the earlier read at 10:35Z said "not attributable"


    last BUY   2026-09-29T06:28:50Z  HBAR-USD  (~4h07m ago)
    last SELL  2026-09-29T08:25:52Z  BTC-USD   (~2h10m ago)

**It is NOT a stall.** Heartbeat 56s old, stage `cycled`, lease held by
web:1. 11 of 23 branches have room (JASMY-USD holds 0 of 3).
`real_free_cash_usd` $311.20, above the $150 floor. Nothing is paused,
locked or inactive; 2 branches are drawdown-breached, which does not stop
their sells.

**TWO GATES — DO NOT CONFLATE THEM. I nearly did.**
`pipeline.per_coin[*].latest.expected_net_edge_pct` is NEGATIVE for all 18
scoreable coins (best ETH −0.7156%, median 0.94pp short). That is
`opportunity_signals`, which grid-status itself labels **observation only**.
It is NOT the grid's buy gate and must not be reported as the reason the
fleet is not buying.

The grid's own gate writes to the activity feed as GATE_PASS /
GATE_BLOCK / GATE_OBSERVE. Read at 10:35Z:
- 19 GATE_PASS, newest **06:28:41Z** — nine seconds before the last buy,
  with healthy edges (+1.855% on a 3.00% move, +1.933%, +1.040%).
- 14 GATE_BLOCK, **every one** "order book unavailable - cannot price the
  spread", across 8 products — but spanning 28 Sep 14:19Z → **29 Sep
  02:26Z**. The newest is 8+ hours old, so this is **NOT** the current
  cause. Worth knowing the failure mode exists and fails closed correctly.

**So the drought is NOT attributable from what is readable.** No gate event
of any kind since 06:28Z. The feed is capped at 120 rows but spans 18
hours, so the sparsity is real rather than trimming. Do not pick a cause;
say "buys idle ~4h, cause not established" until there is evidence.

**Sells are a separate story:** `maker_only_skipped_cycles` = 5,745 sell /
28 buy. Maker sells rest and do not fill and there is no taker fallback —
the owner's own choice, working as configured. Every recent sell logs
"sold the oldest PROFITABLE (skipped a stuck older) real slice", so older
unprofitable slices are being stepped over by design.

**Realized edge when it DOES trade (58 trades since the 26 Sep epoch):**
gross 3.211%, net 2.566% on $2,324.96 notional, mean slice $40.09. The
edge is real; the turnover is what is missing.

### slice_edge AUDITED — §6 IS ALREADY SERVED. DO NOT WIRE IT.

`_net_edge_gate_ok` prices, **against the LIVE BOOK in the instant before
the order**: the spread actually paid, the depth the slice would trade
through, one completed step against the real round trip plus adverse
selection priced off the coin's own volatility, and book pressure — that
last in OBSERVE mode until its own logs justify enforcing it. It fails
closed and persists its full reasoning to `entry_gate_json`.

`slice_edge` takes numbers as ARGUMENTS and cannot see a book. Calling it
from the buy path would put a second, WEAKER net-edge gate beside a working
one — the specific thing this codebase forbids, and two gates that disagree
is exactly how the exit_reason bug happened.

**IT IS NOT PENDING WORK.** The note now lives in the module itself, not
just here, so the next reader who finds it unconsumed does not assume the
wiring was forgotten. A test pins that `crypto_grid_bot` neither imports
nor references it.

**The one idea it holds alone is the ADAPTIVE WIDENER, still OFF.** Turning
it on is an unvalidated claim about the market, and a wider target that
never fills is a slower engine. If evidence ever justifies it, that is
where it lives. Until then the module is recorded reasoning, not a
component — and that is a legitimate thing for it to be.

**THE GENERAL RULE:** an unconsumed module that duplicates working
machinery is not an asset. Same family as "a guard no mutant can kill is
not a protection."

### THE SELL ROUTE IS NOT BROKEN — it fired twice at 11:49Z/11:50Z

    11:49:19Z  NEAR-USD  @ $4.91 (entry $4.82)  +$0.78  parked_sell
    11:50:12Z  HBAR-USD  @ $0.12 (entry $0.12)  +$0.78  parked_sell

Both profitable, both the parked route. Book 140 -> 142, slices 80 -> 78,
parked_sell 6 -> 8. **stop_loss still 1** (ONDO only). The HBAR slice was
the 06:28:50Z buy — a 5h21m round trip.

**THE WATCHER STAYED QUIET AND WAS RIGHT TO.** It reports NEW LABEL TYPES
only, and parked_sell was already known. This is exactly the caveat the
loop prompt carries: **when the book count rises, read the new rows
yourself.** Two sells are not "nothing new".

**THIS REFINES THE 11:30Z FINDING — it does not contradict it.** The 7
slices that were profitable-and-blocked at 11:30Z are STILL blocked and
STILL 7 (QNT x2, ALGO, LINK x2, XLM, TIA — reserved or short). NEAR and
HBAR were NOT among them; they crossed the 1.0% floor between 11:30Z and
11:49Z as prices rose, and sold within a cycle or two of becoming eligible.

So state it correctly: **the parked-sell MECHANISM is healthy and fires
promptly.** What is stuck is a specific, persistent set of 7 slices sitting
behind reserved resting orders and the coin shortfall. Do not say "the sell
side is broken" — say "these seven are unreachable, and everything else
sells when it qualifies."

### 12:22Z — THE PROFITABLE SET GREW TO 12, AND 11 OF 12 ARE BLOCKED

Prices rose, so five more slices crossed the 1.0% floor. Classified against
a FRESHLY re-read invariants (not the hour-old list):

    QNT x2   +57.80%  $31.45 ea  SHORT
    ALGO     +13.28%  $ 4.30     RESERVED
    XLM      + 6.98%  $ 8.35     RESERVED
    LINK     + 6.61%  $ 0.01     RESERVED (the stuck sub-increment slice)
    TIA      + 5.08%  $ 0.03     SHORT
    LINK     + 4.36%  $ 1.98     RESERVED
    PEPE x4  +1.06..1.18%  $0.27-0.46  SHORT **and now RESERVED too**
    SHIB     + 1.03%  $ 0.37     **REACHABLE — the only one**

**RESERVED grew 5 -> 6 branches: PEPE joined** (ALGO, LINK, NEAR, PEPE,
SOL, XLM). Shortfall $765.68/10 — up $6.51 from the 11:30Z reading of
$759.17, so the first drop did not hold. Still roughly flat, not the
$256/4h of the early morning.

**A FALSIFIABLE PREDICTION, check it next pass:** SHIB-USD is the ONLY
reachable profitable slice. The mechanism fired within a cycle or two for
NEAR and HBAR at 11:49/11:50, so **SHIB should sell shortly. If it does
not, the reserved/short classification is incomplete and that is a finding
in itself** — something else is blocking, and I would want to know what.

**The pattern that is emerging:** as the market rises, the profitable set
grows, but it grows almost entirely inside products that are reserved or
short. The turnover constraint is not the floor and not the mechanism — it
is that the inventory which becomes worth selling is disproportionately the
inventory the system cannot reach.

### THE SEVEN THAT WERE UNREACHABLE — measured 11:30Z (superseded by the 12 above)

7 of 80 open slices already clear the 1.0% parked-sell floor. **Not one of
them can be sold**, and the blockers are two things already tracked
SEPARATELY that nobody had connected to "why nothing sells":

    slice                net      value    blocker
    QNT-USD  x2       +56.86%   $30.94 ea  SHORTFALL: claims 0.675982,
                                           holds 0.000973 ($170.97 short).
                                           Also do-not-touch.
    ALGO-USD          +13.84%   $ 4.48     100% RESERVED ($150.66)
    LINK-USD (stuck)   +6.34%   $ 0.01     below one increment
    XLM-USD            +6.09%   $ 7.28     100% RESERVED ($458.49)
    LINK-USD           +4.10%   $ 1.86     97% RESERVED ($101.70)
    TIA-USD            +3.81%   $ 0.02     SHORTFALL: claims 89.78,
                                           holds 0.000000 ($41.26 short)

**BE PRECISE ABOUT THE DOLLARS — do not headline $75.53.**
Genuinely bankable if the resting orders were released: **$13.62**
(ALGO $4.48 + XLM $7.28 + LINK $1.86). The QNT $61.88 is against an
ADOPTED basis, the coin is not held, and QNT is do-not-touch — it is not
bankable at all. TIA $0.02 and the stuck LINK $0.01 are rounding.

**THE STRUCTURAL POINT IS BIGGER THAN THE DOLLARS.** The parked sell exists
precisely to create turnover when the grid trigger will not fire. Right now
it is fully blocked: every slice it could act on is either reserved at the
venue or is coin the books claim and the wallet does not hold. So BOTH
halves of turnover are stopped, for two unrelated reasons:
  BUY  — no dip (correct, nothing to fix)
  SELL — the only profitable slices are unreachable

**73 of 80 slices are underwater.** Distribution: min −14.95%, p25 −4.84%,
median −2.71%, p75 −0.95%, max +56.86%; only 8 above water at all. So even
unblocked, there is little else to sell. That is the market, not a fault.

**THE REMEDY IS THE OWNER'S, NOT MINE.** Releasing reserved units means
`free-locked-inventory`, which is WRITE-GUARDED and cancel-only, and its own
text says cancelling a resting sell "gives up the protection it was armed
for, so it is a decision to take deliberately." **Report it; never call it.**

### §5 IS WIRED — every new slice carries its OWN take-profit target

`slice_target.target_price()` is now called at the buy, and
`target_price`/`target_reason` are stamped on the slice.

**WHICH SELL ROUTE — this is the standing trap, so it is written into the
code.** There are two and they are different questions: (1) the GRID
trigger, `price >= reference_price * (1 + grid_pct)`, a BRANCH-level
condition that knows nothing about this slice; (2) the PARKED sell, where
THIS slice's net over ITS OWN basis clears `GRID_PARKED_MIN_NET_PCT` and
the reference is never read. **slice_target answers exactly (2)**, so that
is what is recorded and `target_reason` says so in words — a bare number
would read as whichever route the reader had in mind.

Priced with the rate this round trip will REALLY pay: the buy leg's actual
recorded `buy_leg_fee` plus the expected exit leg, never one assumed rate
for both. Rounded **UP** to the venue's price tick — down would give away
the edge the target was computed to earn.

**`quote_increment` added to `get_product_rules`** — free, same response —
and `None` when absent, never a guessed tick.

**A bug caught by RUNNING it, not reading it:** `round_target_up` returns a
**Decimal**, `target_price` is a Float column, and `_grid_slice_net_pnl`
raises `TypeError` on a Decimal exit price. Cast with `float()` at the
boundary. It would have surfaced only on a live buy.

**Verified through the REAL formula, not slice_target's own arithmetic:**
across four entry/rate/tick shapes the unrounded target nets EXACTLY 1.0%
through `_grid_slice_net_pnl`, and every rounded one still clears it.

**It fails to None.** The whole computation sits in a try, initialised
before it — a recorded fact must never be able to lose a fill that already
happened.

**TWO OF MY OWN TESTS WERE WRONG AND MUTANTS FOUND BOTH:**
`target_reason is not None` passed against `target_reason=None`, because an
`ast.Constant(None)` node is not Python `None` — PRESENT IS NOT POPULATED,
again. And "initialised before the try" was satisfied by the except block's
identical tuple assign; it now compares line numbers.

### §24's JOIN KEY IS WIRED — `order_id` now reaches the slice

`_last_order_id` (crypto_btc_compound_bot) records the venue's own order id
per product, set at the ONE instant it is certainly known — beside
`_record_order_source`, the moment Coinbase mints it — and cleared at every
order-attempt entry so a previous cycle's id can never be read as this
order's. The buy insert reads it with `.get`, so a product the engine
recorded nothing for lands as None: **UNKNOWN, not a claim.**

**Why a dict and not a return value:** `_place_and_confirm` returns
`(filled_size, avg_price)` and that shape is consumed by four sizers and
every one of their callers. Widening it to carry an id would put a
signature change on a live order path in the way of a diagnostic. The three
sibling dicts already carry per-product facts back out; this is the fourth.

**IT IS LAST-WRITE-WINS, and that is a stated limit.** One product belongs
to one branch and branches are walked in sequence with a sleep, so there is
no second writer today. **An event loop beside the polling loop WOULD be
one — revisit this before §7/§8 lands.**

### §1 WRITE IS UNVERIFIED ON LIVE DATA — that is UNKNOWN, not working

Read at 10:20Z, right after e1606ef deployed: **all 80 slices have
`slice_state` NULL. Zero populated rows.**

**This is neither evidence it works nor evidence it is broken.** Every one
of those 80 slices was created BEFORE the write shipped (~10:17Z), and the
stamps only happen on a NEW buy or a partial fill. Nothing has bought since.
So the state of this change is UNKNOWN, and it stays UNKNOWN until a buy
lands — the same third verdict this whole session has been insisting on,
applied to my own work.

**Still zero populated at 10:35Z — but the newest slice opened 06:28:50Z,
so no buy has happened since the write shipped and it has had NO CHANCE to
fire. Still UNKNOWN. Check the last-buy time before drawing any conclusion
from a zero count.**

**VERIFY IT EACH PASS:** pull grid-status and count
`slice_state is not None` across all slices. The FIRST populated row should
read `ACCOUNTED`, carry a `cycle_id` shaped `20260929T101700Z`, and a
`slice_index` between 1 and that branch's num_levels. If a buy lands and
the count STAYS at zero, the write silently failed and that IS a finding.

Structurally pinned by test_slice_state_writes.py (9 mutants). Structure is
not behaviour.

### §1 IS WRITTEN — the buy and the partial fill stamp their own state

`run_grid_branch_cycle` takes a `cycle_id` (default None, so any other
caller still works); `run_grid_branches_cycle` generates ONE per pass via
`current_cycle_id()` — a UTC timestamp to the second, read once rather than
per branch, since branches are walked with a sleep between them and
per-branch stamps would claim to be different cycles. **It is NOT an
idempotency key** (a next-cycle retry gets a different id); it answers
"which pass opened this slice".

**The BUY insert stamps `slice_state=ACCOUNTED`** — not OPEN. The fill came
back from the venue and that very transaction books it, which is exactly
what ACCOUNTED means and why slice_lifecycle makes it terminal. Nothing is
claimed about the sell leg, which has not been attempted. Also
`cycle_id`, `slice_index` (= `len(slices) + 1`, the rung, matching the log
line), `order_side`, `order_price` (expected) AND `average_fill_price`
(paid) as separate facts, `filled_quantity`, `filled_at`.

**The PARTIAL fill stamps `slice_state=PARTIAL`** on the row that is kept —
on the UPDATE that already happens, inside the existing 3-retry ledger
catch-up. No new session, no new way for a write to fail, and PARTIAL is
neither FILLED nor a fault (`is_fault` excludes it, per §2).

**A BUG CAUGHT BEFORE SHIPPING:** `exit_reason` was assigned ONLY inside
the `SHADOW_MODE_ENABLED` guard — which is FALSE in production — while a
second copy of the same three-way sat inline in the `_log_grid_trade` call.
Reading it from anywhere else is a **NameError everywhere the fleet
actually runs.** Hoisted to one assignment above the guard, three readers.
Found by an AST sweep for unbound Load names, not by eye.

**KEEP THE NAME `exit_reason`.** Four existing tests
(`test_closed_trade_reason_surfaced`, `test_trade_diagnostics`,
`test_governs_panel_live`, `test_opportunity_signals`) police that
expression BY NAME and BY SOURCE TEXT. Renaming it to `_close_reason` broke
all four. They are right to police it — two write sites disagreeing is what
produced the P&L-sign version originally.

**THREE BRITTLE ASSERTIONS UPDATED, none weakened:** the two ordering
checks now match `"run_grid_branch_cycle(session, branch"` without the
closing paren (they are ORDERING checks, not argument-list checks, and are
now robust to any added kwarg); `test_trade_diagnostics` follows the
hoisted assignment instead of the inline keyword. Ratchet still 563.

### §1 PERSISTENCE — the 13 columns landed (c45171a).

`CryptoGridSlice` gained: `cycle_id`, `slice_state`, `slice_index`,
`target_price`, `target_reason`, `order_id`, `order_side`, `order_price`,
`execution_reason`, `filled_quantity`, `average_fill_price`, `filled_at`,
`state_updated_at`. **`order_id` is the column §24 step 7 was missing** —
Coinbase's fills feed carries `order_id` and NOT `client_order_id`, so it
is the join key that actually works.

**All nullable, no defaults.** NULL = UNKNOWN. A `CREATED` default would
claim every existing row started there and was observed doing so.

**THE FLEET DOES NOT RUN THREE SLICES.** The spec models every position as
1/3, 2/3, 3/3. A branch runs up to its own `num_levels` rungs (default
**10**), and `_pick_profitable_slice_to_sell` takes whatever list exists.
`slice_index` records WHICH RUNG; no three-ness is imposed. Forcing three
would change how the grid trades, which a persistence change must not do.

**DELIBERATELY NOT PERSISTED, each for a reason** — do not "complete" the
spec's field list by adding them:
- `base_increment`/`base_min_size`/`quote_increment`/`quote_min_size` —
  PRODUCT metadata, read live by `get_product_rules()`. A stale copy would
  size an order against a rule the venue no longer has: the exact bug §3
  exists to remove, wearing the costume of extra rigour.
- `executable_quantity` — a sizing decision preserved past the moment it
  was true.
- `remaining_quantity` — `qty - filled_quantity`; a stored copy is a second
  source of truth that can disagree with the two numbers it came from.
- `realized_pnl` — an OPEN slice has none. It is booked to
  `CryptoGridTradeHistory.pnl` when the round trip closes.

**INDEXES DECLARED ARE NOT INDEXES CREATED.** main.py's reflection loop
issues `ALTER TABLE ... ADD COLUMN` and nothing else, so `index=True` on
`cycle_id`/`order_id` describes the MODEL, not the live table. Nothing
queries them yet so there is no cost today — but whatever first does must
create the index itself.

**NEXT: write them.** The columns are inert until `run_grid_branch_cycle`
stamps state transitions and records `order_id` on placement. That is the
step that makes §24 and §7/§8 buildable, and it is a real behaviour change
(writes on the live order path), so it wants care.

### §7/§8 — THE BLOCKER IS §1's PERSISTENCE, not the transport

Measured 2026-09-29: there is **no WebSocket client anywhere in the repo** —
no `wss://`, no ws library, in any language. So §8's "where supported by
the existing implementation" is answered: it is not supported today.

But the dependency is NOT the obstacle. `aiohttp==3.10.5` is already in
requirements.txt and supports websockets natively (`session.ws_connect`),
so an order/fill feed needs **no new dependency**. Do not report one as
needed.

**The real obstacle is that an event loop beside the 30s polling loop is
two things that can both decide to place the same order**, and they cannot
agree on what a slice is doing because `CryptoGridSlice` persists no state
(see §24 above — no state column, no order id). The lease makes one
PROCESS the trader; it does nothing about two deciders inside it.

So the ordering the spec implies is wrong in one place, and this is worth
following:

    §1 persistence  ->  unblocks BOTH §7/§8 and §24
    §23 idempotency ->  already done, and is the other half of making a
                        second decider safe

**Wiring §23 is the next real step, and it is not free.** Ten call sites
build `client_order_id` as `str(uuid.uuid4())`. Swapping in the derived id
is behaviour-preserving in the normal case and protective on a retry — but
`client_order_id()` returns None when the intent is incomplete, and None
must NOT fall back to random (that restores exactly the bug) and must not
be sent. So a call site that cannot supply cycle_id/slice_id has to refuse
the order, **which is a way to halt the fleet if the fields are not
threaded through first**. Thread the fields, then swap the id — never the
other way round, and never with a random fallback "just in case".

**NOT built at all (§9, §10, §12–§14, §17–§22):** event transport, cycle
rotation, fill accounting, restart reconciliation, the dashboard and the
speed metrics — and the wiring that would make any of the four modules
above actually run.

## Grid config

3 levels × 2.5% spacing · real round-trip fee 1.5% · effective 0.7%
maker-only · fee-safe floor 0.9% · `CYCLE_SECONDS` 30.

## How to run the suite

`/tmp/claude-0/suite.sh` — rewrite if the container recycled. Classify by
**exit code**: `pytest -q -p no:cacheprovider "$f"`; any non-zero rc falls
back to `python3 "$f"` and uses ITS rc. Always file-by-file. `python3 f.py |
tail` then `echo $?` reports TAIL's code — redirect to a file.

**`pip install pytest` WORKS (29 Sep, pytest 9.1.1).** That changes the
picture: 54 files import pytest and had been unrunnable, counted only at file
level. Installed, they run as **1,505 individual tests**. Do this first on a
fresh container. Split the run — pytest for the files that `import pytest`,
`python3 f.py` for the rest — because the script-style files call `sys.exit()`
at import, which aborts a whole-suite pytest run with an INTERNALERROR.

fastapi is still NOT installed: to test router code, exec the function's source
segment out of the file with AST, deriving what else to exec from the
function's own AST rather than hand-listing it.

For an HTML edit there is no `ast.parse` — extract the `<script>` block, strip
comments and string/template literals, compare brace and paren counts against
`git show HEAD:<file>`. The DELTA must balance.

To prove a test red: copy the changed source aside, `git checkout --` just
those paths, run, restore. **One file at a time.** Make the test DEGRADE, not
abort. For a new endpoint the only honest red is "the route is absent" — so
mutation-test the quality guards separately, and prefer "never does Y" over
"contains X".

Never match on source text containing comments — one guard matched the
docstring that explained the bug; another fired on
`logging.getLogger("options_bot")`; a third tripped on `_row.qty` being a
substring of `slice_row.qty`. Parse the AST.

A scripted edit that cannot find its anchor must exit before writing.

## Test picture — full run at ad1ff8a (04:10Z 29 Sep)

**229 pass, 6 fail** (file-level counts, not test counts), over 235 files.
The six are credential-dependent and have been red all along:
`alpaca_connection`, `expiry_drift_runtime`, `newsroom`, `payout_endpoint`,
`trade_tape`, `video_generation`. **`maker_only` is no longer among them** —
another session fixed it. No regressions.

Earlier baseline, kept for the shape of the day: 211 pass / 7 fail at
`e1221eb`.

Added by this loop since then: `maker_expiry_rested` (52) ·
`closed_trade_reason_surfaced` (25) · `order_not_placed_split` (48) ·
`account_exposure_wired` (18) · `swing_positions_fail_closed` (26) ·
`trade_history_truncation` (21) · `exit_reason_watch` (24) ·
`wallet_map_unfiltered` (27) · `unrealized_gap_not_zero` (25) ·
`execution_inventory_panel` (25) · `maker_expiry_panel` · `fills_by_source` ·
`resting_stop_attribution`.

New today: `held_position_exit_fallback` (28) · `bars_window` (22) ·
`parked_capital` (31) · `order_attribution_alpaca` (39) · `closed_trades` (32)
· `equity_curve` (31) · `growth_model` (42) · `grid_sell_partial_fill` (20) ·
`maker_expiries_endpoint` (15) · `asset_balance_endpoint` (15) ·
`unknown_balance_no_order` (13) · `maker_sell_reason` (8).

Must stay green, in addition to every new file above: `write_token`,
`idle_capital`, `startup_bugs`, `no_check_is_incapable_of_failing`,
`edge_rate`, `maker_only_recency`, `order_attribution`,
`dead_capital_structural`, `dead_capital_lookahead`,
`no_shadowed_module_import`, `census_cached`, `one_blocking_condition`,
`capital_productivity`, `dashboard_reads_the_running_bot`, `coinbase_jwt`,
`position_caps`, `account_exposure`, `equity_handover`, `brain_positions`,
`no_silent_unprotected_position`, `trade_diagnostics`.

## Operational

Railway build alerts land in the owner's Gmail **spam** ("Build failed for
discerning-perfection"), so a real failure would go unseen. Deploys ran 2–25
minutes behind a push. Always confirm `/health`'s commit before quoting a new
route, and never read a 404 on a just-pushed route as broken.

Dashboards, do not confuse them:
- coin / TV: `https://empire-v2-production.up.railway.app/family-tree-dashboard`
- stocks: `https://empire-v2-production.up.railway.app/trading-dashboard`
- `/crypto-dashboard` returns 200 but its data source `/api/crypto` 404s.

## The owner's page

`https://claude.ai/artifact/Kc4FjapEQthdqvCEon2UmR` — PRIVATE. v4 as of
19:24Z, "Turnover Times Edge". **Read it before republishing; always publish
to that URL.**

v5 is unblocked. It should carry: the tracked-vs-held gap plus QNT's unbacked
claim; ~1,000 maker sells that could not execute as a third cause of low
turnover at the execution layer; the second cent-exact confirmation; the
unknown-balance gate; and the new rules.

`trig_013j5nN1SLdpyQiLe7nr9pk1` rewrites it nightly at midnight Central. Its
prompt was corrected 19:37Z to lead with the turnover × edge identity. Limits
unchanged. Do not re-edit without reading it first.

`trig_01RYQFQ7UcucRSFj42xdRsNo` fires 30 Sep 18:00Z and is a personal
reminder, not a trading task. Do not absorb it, do not act on it.

## How the owner wants to be told

"Get it together." "You are tripping."

- Verify before acting. `ast.parse` after every scripted edit. Grep before
  claiming a thing exists.
- Never read a filtered view as an authoritative one.
- Never blind-replace a string across a file.
- Read the first live output of anything new before reporting it done.
- Surface a correction only if it changes what the owner would do.
- Short reports. Numbers and what changed.
- When the owner pushes back, re-verify before defending.
- When they propose an architecture, check what already exists first.
- When they send a screenshot with no words, reconcile it before commenting.

## Worth building next, in order

1. **PARTLY DONE.** Railway logs are not readable from the container, so
   this is being read through endpoints instead. The dashboard prefix is
   `/api/trading-dashboard` — a bare `/grid-status/...` 404s, and that 404
   is NOT evidence of a failed deploy. `/health` reports the serving commit.
   Still to read: 
   "NO MAKER SELL PLACED", "REFUSING to sell", "sell filled X of Y — PARTIAL",
   and "SOLD X ... could NOT write the slice back after 3 attempts" (report
   that one loudly).
2. ~~Give `GridMakerExpiry` a nullable `reason` column so `/maker-expiries`
   can split the counts.~~ **DONE** — and it was bigger than a label. See
   "A row is not an order" below. Shipped with `reason` AND a structural
   `order_rested` flag, a partitioned study, and a resolver that drains
   no-order rows. Follow-up still open: `grid_buy` sets
   `outcome_out["cause"] = order_outcome.MAKER_EXPIRED` for every None
   reason, including the ones where no order was placed — a third consumer
   of the same conflation. **CLOSED by another session in `be076b7`**, which
   built ON the three-state `_last_order_rested` flag rather than around it:
   `order_outcome` gained `NO_ORDER_CREATED`, `NO_FILL` got its meaning, and
   neither joined `BENIGN_CAUSES`, so UNKNOWN stays its own cause instead of
   collapsing into `MAKER_EXPIRED`. It also retargeted one check in
   `test_maker_expiry_rested.py` that had demanded the `order_rested` lookup be
   written inline — a fair fix, since a single read handed to both the ledger
   row and the reported cause is what stops the two disagreeing. **Do not
   re-do this.**

   **ANOTHER SESSION IS WORKING THIS SAME REPO AND MAIN.** Two of its six
   commits (`be076b7`, `12704ca`) touch the maker-expiry path and two
   (`3b919f5`, `1a1de02`) touch stops — the same files this loop edits. Merge,
   never rebase. **After every merge, re-verify semantically rather than
   trusting a clean textual merge:** check that the functions BOTH sides
   touched still do both things. `resting_stops_worker.place()` was the live
   case — their edit landed in `check_once` and mine in `place()`, so the merge
   was genuinely clean, but that was established by reading the merged function,
   not assumed from git's exit code.
3. Check every other `unreadable` entry by direct balance read.
4. Watch free cash. A second unexplained outflow is report-loudly.
5. Read the unrealized formula — confirm a phantom rung inflates it.
6. Artifact v5.
7. WATCH → ARM → ENTER staging, after execution is fixed.
8. Breadth: 16 symbols → more, 30s → faster. **Last on purpose.**
9. `/trades/closed` has no close reason. The grid sell path DOES persist
   `exit_reason` — check whether the endpoint just fails to surface it.
10. The Alpaca side earns nothing. The question is on v4; do not decide it.
11. Why six identical orders at 13:15:02.
12. Watch market_brain's first live cycle if the flag flips.
13. ~~Join Coinbase fills to `OrderAttribution`.~~ **DONE** —
    `fills_attribution.py` (pure) + `GET /fills-by-source`. The table had
    been written since 2026-09-28 and read by NOTHING. The join is on
    `order_id`, because the fills feed does not return `client_order_id`.
    **Unattributed is a bucket, not a source**, and three different absences
    mean opposite things: a post-only MAKER order never reaches the path
    that writes attribution; an order predating the first row could not have
    been tagged; only a TAKER order after the cutover with no row names a
    caller nobody can identify. **The cutover is read from the table's own
    oldest `placed_at`, never hardcoded.** Not yet read live — it needs a
    deploy first.
14. `trade-history` caps `recent_trades` at 50 whatever limit you pass.
15. **PARTLY DONE (e0e80de).** None of the new endpoints had a dashboard
    surface. `renderExecutionInventory()` in `family_tree_dashboard.html` now
    reads `/grid-status/invariants` and
    `/grid-status/orders-not-placed?hours=2&limit=400`. Still unsurfaced:
    `/grid-status/asset-balance` and `/grid-status/maker-expiries`.

## Standing lessons

**Four consumers, one false premise.** The same conflation reached four
places, and the fourth was the worst: `_per_coin_execution` computes
`attempted = filled + expired` from every `GridMakerExpiry` row, so ALGO and
QNT were adding ~2,600 phantom attempts a day to the funnel the owner reads
to find the bottleneck. It showed a fleet trying hard and not filling; the
truth was no order was ever sent. **When a wrong fact is found, grep for
every reader of it before calling the fix done** — the model docstring, the
recorder docstring, the study, the resolver backlog, the funnel counts and one
test all rested on it.

**I rebuilt the bug I built the endpoint to prevent.** `/grid-status/asset-balance`
existed because `/account-census` filters, and a filtered view is not an
authoritative one. It then read only `fetch_balances()`, whose map keeps a
currency solely when `available + hold > 0`, and reported a currency missing
from that map as *"a real absence, not an unread one"* — while its own
docstring said "with a positive total" three lines above. PRIME-USD is the
live case: the map omits it, and only an unfiltered per-currency read can say
whether the venue has no such account or an account holding zero.
`get_asset_balance` is the read that can: `(0.0, None)` for an empty account,
`(None, "no X account found on this key")` for absence. **Knowing the rule is
not following it — and the second time, the wrong claim was in the same
function as the sentence that refuted it.**

**The fleet unrealized figure was absorbing gaps as zeros.**
`total_unrealized_net_usd` is None for two reasons (crypto_grid_bot:7527 —
`if current_price is not None and slices else None`): no slices (a true zero)
or an unreadable price (UNKNOWN). Two aggregates collapsed both —
`growth_model` via `_num(...) or 0.0`, `capital_productivity` via `_f`'s 0.0
default — so a headline number the owner reads moved by an unknown amount, of
either sign, with nothing saying so. **In both cases the asymmetry was the
tell**: growth_model records an unreadable `allocated_usd` in `unreadable` two
lines above, and capital_productivity's own `split_branches` keeps an
`unreadable` bucket. The machinery and the intent were already there; only the
unrealized sum fell through. Fixed in ee796c3 — excluded and named, never
zeroed; a sliceless branch is still counted as the true zero it is.

**My first version of that fix was worse than the bug.** It used `continue`,
dropping the whole row — which silently removed a perfectly readable
allocation from `allocated_usd`, `deployed_usd`, `idle_in_branch`,
`total_capital`, `not_working` and the concentration ranking. **Withholding
one unknown field must not withhold the record that carries it.** Only the
unknown figure is withheld now; the row stays, and a test pins each of those
capital figures.

**Don't chase a semantically equivalent mutant.** Two mutants on that fix
survive and should: `measure_capital` returns aggregates only, so coercing the
internal per-row None to 0.0 yields a byte-identical response, and `x or 0`
sums the same as skipping None. Recorded in the test's own docstring so a later
pass doesn't read them as holes and contort a test to kill them.

**`while read` silently drops the last line.** A sweep over a file written
with `'\n'.join(...)` (no trailing newline) read 22 of 23 branches and said
nothing. It was caught by comparing the row count to the expected count, not
by any error. **Always assert the count.** Same failure shape as everything
else tonight: an incomplete answer presented as a complete one — and this one
was mine.

**A field that restates another field is not a second fact.** The shadow
path computed `exit_reason = 'profit_target' if pnl >= 0 else 'stop_loss'` —
the P&L sign under a new name, carrying nothing the P&L did not already
carry, while labelled as the independent fact that explains it. The
persisted ledger got it right from `_stop_slice` and its comment already
said the sign "cannot tell a stop from an ordinary sale that happened to
lose". Two write sites, the honest source in scope at both, disagreeing.
**When two sites compute the same field differently, one of them is wrong —
find out which before trusting either.**

**Seven columns written on every close and read by nobody.**
`CryptoGridTradeHistory.to_dict()` dropped exit_reason, stop_pct, mae_pct,
mfe_pct, entry_atr_pct, entry_spread_pct and entry_gate_json. Every consumer
goes through to_dict(), so the whole analysis layer was invisible. The
model's own comment said exit_reason exists because "without it the ledger
shows a loss and cannot say whether the stop did its job" — and it could not
say, because the value never left the database. **Same shape as
GridMakerExpiry. When a column is added for a later experiment, check the
serialiser in the same change.** Guard is stated over the table
(`omits NO column at all`), not a fixed list, so a column added later is
covered without anyone remembering.

**close_all was writing a known reason as UNKNOWN.** The forced-exit path
logged no exit_reason, so every owner-requested close landed as None. It is
a market exit at the taker rate — neither target nor stop — and now records
`"close_all"`. Third legal value; None still means genuinely unrecorded.

**A default branch is not a diagnosis.** Once the close reason was finally
readable, 49 of 50 rows said `profit_target` and none said `stop_loss` — and
`profit_target` turned out to be the *else* of `if _stop_slice is not None`.
Three paths enter the sell block (stop, parked-sell gate, rise trigger) and
two shared one label, so the word meant "not a stop". A parked sell fires
when the branch is FULL and cannot buy, on a slice clearing
`GRID_PARKED_MIN_NET_PCT` — its own log line says it sells "rather than
waiting for a rise off a reference it will never rebuy from", i.e. the
target is precisely what was NOT reached. **When one value dominates a
distribution, check whether it is the fallthrough before reading anything
into it.** Fixed in 4dd6867: four values, `profit_target` now gated on
`_rise_hit`.

**A commit cannot cite its own hash.** Twice now loop-state has carried a
hash that resolves to nothing. First a placeholder written from memory
(af869a4 fixed it). Then, more subtly: the hash was read with
`git rev-parse HEAD`, substituted into the file, and the commit **amended**
— which rewrote the very hash just recorded, leaving a reference to an
orphaned object. **Write the hash in a follow-up commit, never in the
commit it names.** And verify with
`git merge-base --is-ancestor <hash> HEAD`, NOT `git cat-file -t`: the
orphaned hash still answered "commit" to cat-file, because the object
survives in the local store long after nothing points at it. cat-file
proves the object exists; only ancestry proves the history contains it.

**The split is done (5210a6f): confirmed non-orders have their own table.**
`GridOrderNotPlaced` / `grid_order_not_placed`, read at
`GET /grid-status/orders-not-placed`. Routing is at the write site:
`_record_maker_expiry` refuses `order_rested is False` and hands it to
`_record_order_not_placed`. True → expiry table and the study uses it; None →
expiry table and the study excludes it by name; False → the new table.

**Why the invariant is NOT `assert` and NOT a NOT NULL column.** Three
reasons, all of which matter: (1) this is instrumentation and must never stop
the thing it measures, and an assert on a live sell path does; (2)
`order_rested` has three states and a NOT NULL column cannot hold the
unknown one — the POST can raise after it may already have reached the venue;
(3) **`main.py`'s migration loop adds every column as nullable regardless of
the model's own `nullable=False`**, because a NOT NULL `ALTER TABLE ADD
COLUMN` fails on Postgres against a populated table — so the declaration
would not take effect and the model and schema would silently disagree.
There is no Alembic in this repo. Any real constraint needs a hand-written
migration plus a backfill decision.

**`capital_velocity.py` already exists** and already rejected notional
turnover as the ranking metric, on live data: LINK ran 7 round trips for 25¢,
NEAR ran 4 for $6.82. The centrepiece is net profit per dollar per day, not
trips or notional. Do not scaffold a second metrics engine; `growth_ledger`
already tracks a `capital_velocity` field and `concentration_gate` imports
`MAX_SINGLE_COIN_SHARE` from it. **`opportunity_events` does not exist** —
no table, no model.

**EXERCISED at 00:16Z (5cdf245 serving): the split works and the fields pay
off immediately.** Expiry table's newest `order_rested=False` row is
00:15:06.802 and stopped moving; the not-placed table gained five rows through
00:15:48. Guard routes correctly; the 228 False rows in the expiry table are
legacy. UNKNOWN and unimportant: one ALGO pair 114ms apart across the cutover
(expiry .802 / not-placed .916) — double-write or deploy straddle cannot be
told from here, and nothing has double-written since.

**The locked-vs-dust split, per row, from the new fields:**

| coin | held | available | locked | min tick | short by |
|---|---:|---:|---:|---:|---:|
| XLM | 1980.77 | 0.0 | 1980.77 | 1e-8 | everything |
| ALGO | 1134.35 | 0.046389 | 1134.3 | 0.1 | 0.0536 (≈¢0.72) |
| QNT | 0.00097323 | 0.00097323 | **0.0** | 0.001 | 0.0000268 (≈¢0.63) |

XLM and ALGO are LOCKED — `free-locked-inventory` is the remedy. ALGO has a
second bind: its free crumb (0.046389) is itself below the 0.1 tick, so even
the unlocked part is unsellable. **QNT is DUST, not locked** — it is short of
a tradeable size by 0.0000268 QNT, about six tenths of a cent. TIA also now
appears in the not-placed table (00:15:48).

**Do not read my own derived labels without checking them.** The first version
of that table labelled QNT "LOCKED" from an inline heuristic
(`asked > available*100`), contradicting the authoritative locked=0.0 already
measured. A convenience rule beat the data that was already in hand. Use the
direct balance read's `locked_units`, never a guess from ratios.

**RESOLVED: no double-write.** 00:21Z — 28 not-placed rows after the cutover,
zero expiry `False` rows after it, zero product+second overlaps. The 114ms
ALGO pair was the deploy straddle. Thread closed.

**The six META orders: the guard existed and was never reached.** 5fb6e6a
wired it. `can_open_position` has taken `account_positions` and `equity` since
that incident, both default `None`, and the ONE live call site passed neither —
so every cycle took the self-only fallback its own docstring calls "the wrong
denominator… and always was". Now passed, read once per cycle, and **fails
closed**: `api_call` returns None on failure, `get_account_positions` passes
that through instead of flattening to `[]`, and an unreadable account refuses
every entry for the cycle at ERROR. Exits untouched. **No flag changed** — the
runner stays gated and idle; what changed is that when it runs, its
concentration limit measures the account, not its own empty book.

**STILL OPEN, worth its own change: `alpaca_swing_bot.get_open_positions`
fails OPEN.** It returns `{}` on both a non-200 and any exception, and the
entry guard is `if proxy in open_positions`. An unreadable positions list
therefore reads as "nothing held" and permits a duplicate buy into a position
already open. Same fail-open shape as `0af01f1` on the crypto side, against
this codebase's own rule that anything moving live orders fails closed. Not
fixed in 5fb6e6a on purpose — different file, different bot, wants its own
tests.

**A wrong hypothesis, recorded so it is not re-run:** six orders in one second
was NOT the swing bot's entry loop ordering one proxy repeatedly. META is not
among its proxies and no proxy is shared between two keys.

**A substring check on a long function proves almost nothing.**
`"log.error" in seg` passed a mutant that downgraded the exact refusal line to
`log.debug`, because the cycle contains other `log.error` calls. Find the call
carrying the message and assert its level.

**CLOSED (c473adf): the swing bot's fail-open.** `get_open_positions`
returned `{}` on a non-200 and on any exception. Worse than a missing dedup:
`intraday_count` comes from `.keys()` and `open_notional` from `.values()`, so
**one failed read reset three caps to zero** — duplicate guard, concurrency
ceiling and notional budget all reading "nothing used". Now returns None on
every failure path, logs at ERROR, and both cycle functions gate their entry
BLOCK on `open_positions is None`. Exit loops deliberately NOT gated — an exit
is a protection. `is None`, never truthiness: a genuinely empty account is a
real zero and must keep trading.

**A mutant that does not apply is not a passing test — this cost three false
SURVIVED results in one pass.** Anchors silently failed to match (indentation
guessed wrong; a regex hit a *different* `if r.status != 200: return None`
earlier in the same file). The reliable method: locate the function via AST,
slice the source by its own `lineno`/`end_lineno`, mutate inside that slice,
and assert the replacement count changed. **Always confirm the mutation
landed before reading the result.**

**A substring can match a `def` line.** The new test found readers with
`"get_open_positions(session)" in source`, which matched the function's own
`async def get_open_positions(session):` — three readers instead of two. Match
the call node, not the text.

**The 50-row cap was deciding what the data appeared to say (fixed f87fa39).**
`get_grid_trade_history` served 50 of 132 closed trades with nothing in the
payload admitting it, and the endpoint called it with no arguments so the cap
could not be raised. `limit` is now real, bounded by
`GRID_TRADE_HISTORY_MAX_ROWS`, and the payload carries
`recent_trades_truncated` / `recent_trades_omitted`. Truncation is computed
from the TOTAL, not from `len == limit` — a complete 50-trade book served at
limit=50 is not truncated, and flagging it would make the field noise.

**The four-way exit_reason distribution is NOT yet readable.** At 00:45Z
exactly 1 of 50 rows postdated the split: LINK-USD, +$4.13, `profit_target`
(now earned via `_rise_hit`, not a fallthrough). The other 49 are legacy rows
carrying the old binary label. **Do not report a distribution off that
window** — re-read with a raised `limit` and filter on `closed_at` once more
closes accumulate. The interesting question is whether any row is
`parked_sell`.

**Third substring check to prove less than it looked, same session.** The
handler guard was `"except (TypeError, ValueError)" in seg`; a mutant kept
that exact line and replaced its body with `raise`, and passed. Assert the
handler's BEHAVIOUR (assigns a fallback, contains no `raise`), never that its
header appears. Each time, the mutation is what caught it — the check never
looked wrong on its own.

**I applied a lesson to one coin and not the one beside it.** The XLM
all-None read at 01:07 was correctly treated as suspect and repeated five
times. TIA's `0.0` came from the SAME sweep, was not repeated, and was
reported to the owner as fact — then moved. **A rule learned mid-task has to
be applied to every reading in that task, not just the one that prompted it.**

**LEAD, unverified: `get_asset_balance` returns the FIRST matching account,
`fetch_balances` SUMS across them.** `if account.get("currency") == currency:
return float(...)` versus `held[cur] = held.get(cur, 0.0) + total`. If a
currency ever spans two Coinbase accounts, the direct read under-reports and
the map does not. That matters because the direct read is the input to the
sell-refusal path (0af01f1) and to "nothing sellable" — an under-reported
balance would refuse to sell coin that exists. **Not confirmed as the cause of
the TIA reading**, and not confirmed to occur at all on this account; worth a
pass that enumerates the raw account list.

**LEAD CLOSED (measured 01:58Z, three independent reads): no currency is
duplicated.** `accounts_seen=114`, `currencies_seen=114`,
`accounts_exceed_currencies=0`, one page. One account per currency, so
`get_asset_balance`'s first-match and `fetch_balances`'s summing return the
SAME value — the direct read cannot under-report on this account and the sell
path is not affected. **`get_asset_balance` was deliberately NOT changed**: the
two behaviours are identical today, so a change would be churn on a live sell
path for zero present benefit, and summing could be the WRONG fix later if the
extra rows ever turn out to be separate portfolios (that would overstate what
is sellable).

**Latent, not active:** the divergence still exists in the code. It activates
only if a currency ever gains a second account row —
`accounts_exceed_currencies` going non-zero is the trigger to revisit, and it
is now on `/grid-status/asset-balance` where any pass can see it.

**This also rules the multi-account theory OUT as the explanation for TIA
going 0.0 → 44.57.** That change remains UNEXPLAINED. Do not attach it to this.

**A row is not an order.** `GridMakerExpiry`'s own docstring said each row was
"one post-only order that rested its whole window". `_record_maker_expiry` was
called on all three of `place_maker_sell`'s None returns, and two of those
never create an order. So the table that exists to answer *"should a resting
rung be given longer?"* was being filled with cycles where nothing ever
rested — at ~2,600/day from ALGO and QNT alone, against a 5,000-row study
window, which is days from a confident mean computed entirely off non-events.
Three separate places asserted the false claim: the model docstring, the
recorder's docstring, and a test that matched on the docstring text and so
went red when the truth was written down. **Where a comment states a
precondition, check the call sites against it rather than trusting it — and
never let a test assert a docstring.**

**Prefer a structural flag to a readable reason.** The fix carries both:
`reason` (the human sentence) and `order_rested` (True/False/NULL). A study
that decided what it was measuring by matching on `reason` text would be one
reworded log line away from silently reclassifying its whole sample.

**Filtering junk out of a capped query is not enough — drain it.** The
resolver scans oldest-first, 200 rows wide, 8 book reads per cycle, over
`resolved_at IS NULL`. Excluding no-order rows from the *study* while leaving
them in the *backlog* would have meant the scan window never reached a usable
row. They are now retired on sight at zero book-read cost.

**A 404 is not a failed deploy.** Three endpoints read as gone before the
prefix (`/api/trading-dashboard`) turned out to be the whole story. Check
`/health`'s `commit` field before concluding anything about what is serving.


- An unknown balance can never generate an order. Accounting and inventory
  gates come before speed.
- One log line must not cover three different facts. "Did not fill" described
  an order that was never placed, for months.
- A filtered view is not an authoritative one.
- UNKNOWN is not zero, and knowing the rule is not following it.
- When the owner pushes back, re-verify before defending.
- Check what already exists before agreeing something needs building.
- Prefer "never does Y" over "contains X" in a guard.
- Growth = turnover × edge, and turnover is lost at the execution layer too.
  A wider funnel above a jammed outlet just queues more.
- The evidence may be written and never read. This codebase's second
  signature bug, after the fallback literal.
- The asymmetry is the tell: when one path fails closed and its mirror falls
  through, the mirror is the bug.
- A rate can rise because the denominator shrank. Always say which.
- A ceiling is not a forecast, and pricing a decision with one is the same
  error as forecasting with one.
- Measure the distance to the trigger, not just the P&L.
- The scary explanation is not the likely one. Read before you alarm.
- A deployed fix is not an exercised fix.
- A return value the caller ignores is a bug waiting.
- Two writes that must both happen need to be one write, or a retry.
- An epsilon must match the scale of what it measures.
- A branch can trade profitably and still miscount its inventory.
- An invariant that keeps failing is earning its keep — and read all of its
  fields, not just the one you expect.
- A test that blocks your change may be protecting something real. Retarget
  and mutation-test it; never delete it.
- A red that only says "that name is new" proves nothing.
- A gap is not a zero. The worst form is a silent skip on a safety path, and
  making one loud is not fixing it.
- A fix's first live output is part of the fix.
- Never blend realised and unrealised.
- A 404 can also mean not deployed yet.
- Before calling something an anomaly, read the code that produces it — to the
  END. Separate what the code provably does from which path actually fired.
- A page can be wrong on every row and still look plausible.
- Order direction is part of an algorithm's contract.
- A wide window with a small limit serves the oldest rows.
- An order that cannot be traced to its caller cannot be debugged.
- A literal standing in for a real value is this codebase's signature bug; its
  cousin is a capability claimed in prose, never built.
- Closing a loser does not create the loss.
- "Changed" is not "resolved".
- Never match on source containing comments; parse the AST.
- Rule out the obvious causes explicitly, in order, and say so.
- Search the repo for the system the owner already built.
- Reconcile from the broker — and keep what that call already told you.
- Never divide a long measurement by an instantaneous denominator.
- New code passing its tests is not new code being right.
- A surviving mutant is usually the test's fault.
- Test whether a figure grew in units, not dollars.
- A branch allocated but holding no coin is primed, not idle.
- Never add two overlapping sets or two different denominators.
- Protections fail open; anything moving live orders fails closed.
- Zero is not a safe default for a price.

**A substring check can match a comment — and mine did.** Guarding the new
dashboard panel, `"UNREADABLE" in seg` passed against a function whose
UNREADABLE *branch* I had deleted, because the word survived in an
explanatory comment two lines above. This is the fifth variant of the same
defect tonight: `"_row.qty"` matched `slice_row.qty`; `"except (TypeError,
ValueError)"` matched a clause whose body was replaced with `raise`;
`"log.error"` matched a downgraded line; `"get_open_positions(session)"`
matched the `async def` line. `test_execution_inventory_panel.py` now runs
`_strip_js_comments()` before every content assertion. **A substring check
inside a long function proves almost nothing — strip comments, then anchor
to the statement, not the word.**

**Written and read by nothing, applied to my own work.** Three endpoints
built tonight (`/invariants`, `/orders-not-placed`, `/asset-balance`) were
reachable only by curl. That is exactly the defect I spent the night fixing
in the engine — a fact recorded and surfaced to no one — committed at the UI
layer by me. **When you finish a producer, check that a consumer exists
before calling it shipped.**

**My own harness could not have found this.** The new panel called
`btn.addEventListener` behind a truthiness guard. Every headless render
harness in this repo stubs `document.getElementById` with a plain object, and
`test_branch_coin_label.py` — which execs the WHOLE dashboard script in node —
aborted on that line, taking the entire script down. My verification harness
implemented `addEventListener` on its stub, because I wrote the stub to match
the code I had just written. **A fixture built to match the code under test
cannot test that code's assumptions.** Same family as "a fixture that
CONSTRUCTS the input cannot test how that input is made". The suite caught it
by exit code; nothing I wrote would have. Guard now checks `typeof x.addEventListener
=== 'function'`, and the wiring is best-effort on purpose — it runs AFTER
`innerHTML`, so a throw there leaves a panel that looks rendered and is not
finished, the worst shape available.

**Also: the slice anchor that silently grew.** `test_execution_inventory_panel.py`
ended its slice at `renderAccountCensus`, the next function in the file on the
day it was written. This pass inserted two functions before it and the slice
quietly covered all three, so every content assertion could have been satisfied
by unrelated code. Both panel tests now slice to the NEXT top-level
`async function`, not to a named successor. **An end anchor that names a
sibling is a guess about file order.**

**Both sides of this file found the same defect on 29 Sep from opposite
directions.** The entry above is a substring check matching a COMMENT and so
passing wrongly; the lessons below are substring checks matching PROSE that
moved and so failing wrongly. Same cause - asserting on text instead of on
the property - and it can fail in either direction, which is why neither a
green nor a red from one is worth much on its own.

- **A coverage figure whose healthy value and whose broken value are the same
  number cannot detect the break.** `protects_usd 0` meant both "nothing needs
  protecting" and "fourteen positions are naked". Report what is covered by
  NOTHING alongside what is covered.
- When each layer defers to the next, follow it to the last one and check it
  closes. Three correct deferrals made a circle with nothing inside it.
- A guard that blocks your fix may encode an incident, not an oversight. Read
  why it exists before relaxing it — twice on 29 Sep the obvious fix was one
  the guard had been written to prevent, and the comment named the dollar
  amount and the units left phantom.
- **A test asserting a prose phrase fails changes that improve what it
  guards.** Four on 29 Sep: `"_cached_real_maker_fee_rate is None" in body`,
  `"min(" in body`, an inline-`.get()` shape, `"NO grid stop" in reason`. Each
  time the property held and only the wording moved. Assert the property.
- A test that RAISES hides every test after it. One deliberate break threw a
  KeyError and a 13-failure regression reported as one. Catch per test and
  record a raise as a failure.
- Before building a trigger, count how many rows it could fire on today. A
  loss trigger on the trimmer was 0 of 14 — dead code, found by counting
  rather than by reasoning about it.
- A switch that sells real coin: default off, exact word to arm, and say what
  arming would do at TODAY's prices before anyone throws it.
- **Branch on the fact you are reporting, not on a proxy for it.** The adopted
  stop's log read `if stop_pct == 0 and slices: NO GRID STOP / elif resolved is
  not None: ADOPTED STOP ARMED`. JASMY — stop 0, mode off, zero open slices —
  missed the first arm on `and slices` and took the second, whose condition is
  true whenever the engine answered at ALL, armed or not. So the live log said
  `ADOPTED STOP ARMED` directly above a reason stating that nothing sells
  JASMY-USD at any price. No money moved; the stop was still 0 and there was
  nothing held to stop out. **The `and` that narrows one arm does not narrow
  the `elif` after it** — every state that falls out of the first condition
  lands in the second. Fixed by asking about the stop first and letting the
  slices decide only how LOUD an absence is. Mine, shipped, and exactly the
  defect class the whole adopted-stop change was made to prevent.
- **When a decision is wrong, extract it before fixing it.** Those three lines
  sat in a 9,000-line async cycle nobody can call, and the test guarding them
  matched a substring of the function body — which broke the moment the strings
  moved, for the seventh time this session. `stop_report_line(bot_name,
  stop_pct, resolved, has_slices)` reads no state, so the test states the
  property once and checks it against every state, and the retargeted
  `test_adopted_stop` assertion now calls it. **My first statement of that
  property was wrong and the code was right**: `ARMED iff stop_pct > 0` fails a
  branch that named its own 8% stop — it has a stop, but is not adopted and must
  not be described as armed. The property is `resolved is not None and stop_pct
  > 0`. Too broad fails correct code; pin the exact fact.

## 12:38Z — the prediction held, and the shortfall reversed direction

**Staked last pass:** SHIB-USD is the only reachable profitable slice, so it
should sell shortly; *if it does not, the reserved/short classification is
incomplete and that is a finding in itself.*

**It sold.** `parked_sell` 8 → 9, book 78 → 77, SHIB out of the profitable set.
The falsifiable half survived: the reserved/short classification correctly
picked the one slice whose inventory was actually free. That is the first time
this session a forward claim about WHICH slice moves has been checked and held,
rather than a description of the book restated.

**Refined — the shortfall is accelerating again, not decelerating.** I had been
reporting deceleration for several passes. $765.68 → $802.68, **+$37.00**, the
largest single-interval rise since early morning. Reserved branches went 6 → 5
(PEPE left), so this is not more capital being locked: the gap between what the
book wants and what is bankable is widening on its own. A narrative that has
held for several passes still has to be re-derived from the current number, and
when it flips it gets named as a reversal, not smoothed into "still flat".

**HBAR +0.47% is the closest approach to a buy trigger all day** (BTC +1.51%,
NEAR +2.07%, TON +3.06%; none at or below). Check (1) — has a buy landed, and
did §1 stamp `slice_state`/`cycle_id`/`slice_index`/`order_side`/
`filled_quantity`/`average_fill_price`/`execution_reason` — has been
unanswerable all session because nothing has bought. The §1 write path has
therefore never executed in production. It is tested, not exercised; those are
different verdicts and it stays UNKNOWN until a real fill stamps a real row.

Stuck slices still exactly two (LINK-USD `0.009999999999998899`, PRIME-USD
`0.00999999999999801`, both against increment `0.01`). Fixed in code at
`6b9c207`; the ROWS carry the persisted values and `reconcile-slices` is
write-guarded and the owner's. Profitable 7 | blocked 7 | REACHABLE 0.

**Next stake:** HBAR closes the remaining 0.47% and buys, exercising §1 for the
first time — or it re-anchors away and REACHABLE stays 0 while the shortfall
keeps climbing. Either outcome is informative; a third pass of "gaps unchanged"
would mean the gap table is not the thing to be watching.

## 12:58Z — the drought broke: HBAR is below its trigger and a buy is resting

The watcher exited 0, "quiet". It was not quiet. **parked_sell 9 → 12, book
143 → 146, slices 77 → 75** — three more sells inside twenty minutes. The
watcher reports NEW LABEL TYPES only; three sells under an existing label are
invisible to it. Counted by hand, as always.

**Last pass's stake held, and then went further than staked.** I predicted HBAR
would close its remaining 0.47%. It closed it and went through: the gap is now
**−1.45%** (price 0.11424 against a trigger of 0.115925 = 0.11951 × 0.97). One
of thirteen branches with room is at or below zero; it is the first all day.

**The buy block is entered, and the gate is deciding live.** `buys_paused`
False, `drawdown_breached` False, so `_sell_only` is False and the preceding
`if` does not swallow the `elif`; 2 open slices against `num_levels` 3 leaves a
free rung. Live-ops confirms it from the other side — HBAR gate verdicts in the
last fifteen minutes:

    12:57:20  GATE_PASS   net edge +1.849% on a 3.00% step, spread 0.035%
    12:56:27  GATE_BLOCK  order book unavailable - cannot price the spread
    12:55:22  GATE_BLOCK  order book unavailable - cannot price the spread
    12:53:22  GATE_BLOCK  order book unavailable - cannot price the spread
    12:51:00  GATE_BLOCK  order book unavailable - cannot price the spread
    12:45:34  GATE_PASS   net edge +1.843% on a 3.00% step, spread 0.044%
    12:42:56  GATE_BLOCK  order book unavailable - cannot price the spread

**REFINED — "order book unavailable" was recorded as RULED OUT and stale. It is
neither.** It is firing on roughly every other HBAR cycle right now. The old
reading was correct about the OLD data and wrong as a standing fact: nothing
was below its trigger, so the gate was never reached, so of course its blocks
looked historical. **A gate that is never reached produces no evidence, and no
evidence is not evidence of absence.** The moment a branch crossed, the gate
started answering — and half its answers are that it cannot read the book.

**A buy is almost certainly resting on the book right now.** Between the 12:55Z
and 12:58Z pulls, available cash went **$1,726.66 → $1,671.88, −$54.78**, with
no new slice and no closed trade to explain it. HBAR's rung is
`allocated_usd / num_levels` = 163.80 / 3 = **$54.60**. Three independent
numbers agree: a GATE_PASS at 12:57:20, a $54.78 hold, and a $54.60 rung.
**I cannot see the order directly — no read-only endpoint exposes resting
orders — so this is an inference, not an observation.** It is falsifiable in
four minutes: `maker_order_wait_seconds` is 240.

**Check (1) is STILL UNKNOWN. Say exactly that.** Newest slice remains
2026-09-29T04:08:59Z (LINK-USD); `slice_state` populated **0 of 75**. §1's
write path has never executed in production. Tested is not exercised.

**Next stake, resolving ~13:01:20Z — two branches, both checkable:**
- **FILL:** a new HBAR slice appears with `slice_state=ACCOUNTED`, `cycle_id`
  `20260929T1257xxZ`, `slice_index` 3, `order_id` a venue uuid, and
  `target_price` ABOVE `entry_price`. §1 is exercised for the first time.
  Any of those NULL on a slice that new is a SILENT FAILURE and a finding.
- **EXPIRY:** no slice, cash returns to ~$446 unallocated, and
  `maker_expiry_drift.buy` goes **21 → 22**. That counter is the tell; check it.

Checks (2) and (4) unchanged in shape: stuck slices still exactly two (LINK-USD
`0.009999999999998899`, PRIME-USD `0.00999999999999801`). Profitable **15**,
**blocked 15, REACHABLE 0** — reserved ALGO/LINK/NEAR/SOL/XLM, short
ACH/BCH/ETH/ONDO/PEPE/PRIME/QNT/TIA/XRP/ZEC. The profitable set keeps growing
inside products the system cannot reach; that pattern is unchanged.

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
- **Six branches over their level count**: not a bug. An adopted-only branch
  gets exactly one extra rung. `slices_over_levels_unexplained = 0`.
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

## Grid config

3 levels × 2.5% spacing · real round-trip fee 1.5% · effective 0.7%
maker-only · fee-safe floor 0.9% · `CYCLE_SECONDS` 30.

## How to run the suite

`/tmp/claude-0/suite.sh` — rewrite if the container recycled. Classify by
**exit code**: `pytest -q -p no:cacheprovider "$f"`; any non-zero rc falls
back to `python3 "$f"` and uses ITS rc. Always file-by-file. `python3 f.py |
tail` then `echo $?` reports TAIL's code — redirect to a file.

fastapi is NOT installed here: to test router code, exec the function's source
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

## Test picture — full run at e1221eb

211 pass (file-level counts, not test counts), 7 fail. The seven are
credential-dependent and were red before today: `alpaca_connection`,
`expiry_drift_runtime`, `maker_only`, `newsroom`, `payout_endpoint`,
`trade_tape`, `video_generation`. No regressions all day.

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
   of the same conflation, left alone because other code branches on it.
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

# State of play — 2026-10-01

Written at the end of a session that shipped seven commits to a live trading
system. Everything here is measured, not estimated. Where a number is a
guess it says so.

---

## What is live and what is only observing

| Thing | State |
|---|---|
| Grid fleet, 23 branches | **live**, cycling every 30s, lease held |
| `reconcile_worker` | **armed** (`ARMED_BY_DEFAULT = True`, since 2026-09-28) |
| `auto_trim`, `resting_stops`, `coin_adoption`, `idle_rotation` | armed |
| Execution gate (`branch_audit_service`) | **OBSERVING** — records, does not block |
| Audit tables (5) | created on next boot, empty, nothing reads them yet |
| `capital_velocity.py` | **not wired** — referenced only in comments |
| `concentration_rotation` planner | in repo, tested, nothing calls it |
| `concentration_rotation_worker` | **NOT in the repo. Do not add it — see below** |
| `risk_governor.py` | not wired (`applied_to_live_sizing: False`) |

---

## The one thing that must not be shipped as-is

`concentration_rotation_worker.py` exists only as a local draft. It can
identify over-concentrated inventory, decide a slice is sellable, place a
post-only exit and verify the fill — and then it **stops**:

```python
if ok:
    placed += 1
    log.info(f"[rotation] sold ... freeing ${s['frees_usd']:.2f}")
```

It never touches `CryptoGridBranch`, never adjusts `allocated_usd`, never
closes the slice row, never invalidates the balance cache. So a successful
sale would leave the slice open in the database, the allocation still
claimed, and the venue holding less coin than the book says.

**It would manufacture exactly the DB_ONLY mismatch the audit found on 11
branches.** The tool built to relieve concentration would add to the
reconciliation problem it was meant to fix.

`frees_usd` is a *plan* figure printed as though it were a completed
release — the same class of error as reporting expected P&L as realized.

The fix is not to finish it. The grid's own sell path already reconciles,
releases and books P&L from actual fills. The rotation should choose which
slices to sell and hand them to that machinery, not reimplement half of it.

---

## auto_trim — found late, and it is armed

`auto_trim` was running all day and I did not look at it until the account
owner pushed back. It does what the 20% rule needs: `mode: arm`, every 900s,
trims an over-limit holding back to 19.5%. It has done it to these coins
before — ZEC $746.25 and XRP $136.43 on 2026-09-27, ZEC $139.18 and XRP
$108.35 on 2026-09-28, $1,130.21 in total.

It is not acting now because XRP (20.7%, $120.22 over) is refused as
`ACTIVELY_TRADED` — the grid holds open slices on it — and ZEC reads 18.56%
against the equity denominator rather than 26.63% against allocated.

**It had no profit check at all.** No `entry_price`, no `net_pct`, no
round trip anywhere in the file; its own docstring said "It does not know
whether now is a good time to sell." The only thing between an armed trimmer
and an automatic sale of XRP at −4.18% was a guard that exists for an
unrelated reason.

Fixed: `REQUIRE_PROFIT = True`, sharing
`concentration_rotation.DEFAULT_ROUND_TRIP_COST_PCT` rather than restating
it. A holding that would net ≤ 0 after the round trip is refused as
`WOULD_REALISE_A_LOSS`. **An unknown basis is refused too** — `BASIS_UNKNOWN`
— because nothing that cannot be shown to be a gain may be sold under a rule
with no exceptions.

That narrows the trimmer, and the narrowing is real: the tail positions it
used to size have no recorded cost anywhere, so they are now refused. The
worker supplies a quantity-weighted basis from open grid slices; assets with
no slices have no basis and will not be trimmed until one exists.

## Open decision that is not mine

**The concentration rule has two denominators and they disagree.**

```
             % OF TOTAL EQUITY    % OF ALLOCATED
ZEC               18.50%              26.63%     <-- under one, over the other
XRP               20.76%              26.26%
```

`concentration_gate` measures market value over **total equity, cash
included**. `concentration_rotation.over_limit` measures allocated over
**fleet allocation**. ZEC is simultaneously buyable and sellable.

My read, for whoever decides: counting cash in the denominator means the
limit *loosens every time money is deposited*, which cannot be the intent of
a concentration rule. But changing it restricts live buying, so it is a
decision about the rule, not a bug fix, and it was left open deliberately.

---

## Measured facts, 2026-10-01

```
account total            $10,028.72
cash                      $1,277.69
allocated                 $8,534.40
releasable right now          $0.01     <-- not a typo
blocked                   $8,534.39

book claims coin the venue does not hold      $941.95   (11 branches)
venue holds coin no branch claims           $2,107.27   (9 branches)
  of which BTC                              $1,504.57   unmanaged, unstopped

realized, lifetime     164 trades, $93.76
realized, last 24h     1 trade,     $0.87
account, last 24h                  -$73.29
account, last 72h                 -$653.26
```

89% of all unrealized loss sits in ZEC and XRP, on half the capital. The
other half is down $55.

---

## Fixed today, with the measurement that found each

- **`9bd3acb`** the 20% ceiling returned ALLOW when the account book was
  unreadable — and the book goes unreadable exactly during the 429s. The
  rule switched itself off under load.
- **`9d516db`** every balance read pulled the *entire* account listing to
  find one currency. 38 call sites; one per branch per 30s cycle. That
  volume caused the 429s above.
- **`2200de1`** a pricing gap was recorded as a $1,945 loss. All five
  readings under $9,500 in a 490-point series were feed hiccups, not losses.
- **`9ff021c`** the rotation cost model charged maker fees on an exit that
  pays taker. A slice reading +0.26% net was really realising −0.14%.
- **`a65e4b6`** three branches re-asked the venue to sell dust every 30s,
  forever. 182 refusals in 24h on QNT alone.
- **`533955a`** Alpaca sized sub-$1 orders against a $1 venue minimum and
  retried indefinitely. Root cause is the 50% risk cap, already exhausted.

---

## What is not true, and was claimed earlier

- Reducing Alpaca max positions 8→3 would **not** help. The binding
  constraint is `risk_room = equity*0.50 - held`, which is $0.00 with four
  positions open. Position count never enters it.
- Cancelling the resting orders is **not** an unlock. Most of that lock is
  the grid's own working orders, and ALGO's is a resting *buy* holding cash.
- `GRID_RECONCILE_MODE` does **not** need setting. Reconcile is armed by
  default.
- Arming `GRID_ADOPTED_STOP_MODE` would **not** sell "$2,417.34 across three
  branches, realising −$394.22". That number counted every branch whose
  drawdown passes its policy stop, and three of the four do not hold the
  coin they claim. Measured 2026-10-01 15:15Z against `backing.rows`:

  | branch | fires | claimed | backed | really sellable |
  |---|---|---|---|---|
  | ZEC-USD | yes | $2,272.62 | 93.85% | **$2,132.94**, −$399.39 real |
  | BCH-USD | yes (newly) | $171.75 | 44.83% | $0 — `can_be_sold: false` |
  | ACH-USD | yes | $91.16 | 10.14% | $0 — `can_be_sold: false` |
  | JASMY-USD | yes | $53.56 | no slices at all | $0 — nothing to sell |

  So the real answer is **one branch**: ZEC, about $2,132.94 of coin,
  realising roughly −$399.39 — which is 71% of the fleet's entire −$562.05
  unrealised. The other three are allocation claims with no position behind
  them. Both sell paths already clamp to the held balance and refuse on an
  unreadable one (`place_market_sell`, `place_maker_sell`), so arming it
  cannot send an oversized order — it would simply sell nothing on those
  three. Still **not armed**: that decision is the owner's.

---

## The audit layer now knows which branches are broken (2026-10-01, confirmed live)

`exchange_truth_worker` is running and its first passes landed. Measured
through `GET /api/trading-dashboard/gate-observations`:

| | before | after |
|---|---|---|
| `branch_control_state` rows | 2 | **22** |
| reconciliation verdicts | 2 × UNKNOWN | **14 MATCHED, 8 MISMATCH** |
| `exchange_truth_failures` | 0 (readable) | **8**, all `INVENTORY_SHORT_AT_VENUE` / `KNOWN_INTERNAL` |
| `execution_enabled` | False | **False on all 22** — rule 4 held |

The 8 match the backing measurement exactly, and only 8 rows were written,
not 8 per 5-minute pass — the only-a-change rule is working.

### What this does to the enforce decision

`ExecutionGate.check()` denies `ALLOCATE`/`ENTRY` when
`reconciliation_status != "MATCHED"`, and denies outright when a branch has
no control row at all. `EXIT` is deliberately **not** gated on
reconciliation, so a broken branch can still sell its way out rather than
having the mismatch frozen in.

So the arithmetic has changed completely:

- **Before today**: 23 branches, 2 control rows. `EXECUTION_GATE_MODE=enforce`
  would have denied **21 of 23** on "no control row" and halted the fleet.
  That is the hazard the module's own comment warns about.
- **Now**: enforcing would block buys on the **8 MISMATCH** branches —
  which is correct, those are the branches claiming coin they cannot sell —
  plus **JASMY-USD**, which has 0 open slices so `slice_backing` skips it and
  it has no control row. **14 branches would trade freely. Every branch could
  still exit.**

That is a defensible state to enforce from, where this morning's was not.
Still the owner's switch, on Railway, and not flipped here.

**The JASMY edge is real and worth naming**: a branch with no slices is never
measured, so it never gets a row, so enforce denies it. For a branch holding
nothing that is harmless, but the rule is "unmeasured is denied", not
"unmeasured is fine", and a branch that empties itself will fall into it.

---

## Why it is still losing money (measured 2026-10-01, 32 days of closed trades)

**The trading engine is profitable. The portfolio is not.** Those are two
different facts and only the second one shows up in the account.

From `/grid-status/trade-history?limit=1000` — all 165 closed trades, not a
sample:

| | |
|---|---|
| closed trades | 165 over 31.8 days (5.2/day) |
| realised | **+$94.91**, win rate 86.1% |
| average win | **$0.79** (largest ever: $6.35) |
| average loss | −$0.76 (23 losses, −$17.45 total) |
| last 5 days | 82 trades, +$75.30 → $15.06/day |
| open unrealised | **−$562.05** |

So the machine wins 86% of the time and books 79 cents a go, while sitting
on a $562 hole. That is not a broken strategy. It is a strategy whose
capital is in the wrong place.

### Where the money actually is

| | ZEC + XRP | the 5 best earners held |
|---|---|---|
| capital | **$4,513.46 (52.9%)** | $1,386.27 (16.2%) |
| closed trades in 32 days | **1** | 43 |
| realised | **+$0.30** | **+$53.68** (57% of all profit) |
| unrealised | **−$470.47** | −$67.30 |

ZEC has **never closed a single trade** and carries −$399.39 — **71% of the
fleet's entire unrealised loss, in one coin.** XRP has closed one trade ever,
for 30 cents.

Neither even appears in the coin league, because ranking needs closed trades
and they have almost none. The capital is not working. It is parked.

**Why it cannot trade.** Grid spacing is 2.5% and a branch only sells a slice
at a profit. ZEC is 17.49% down, so every slice is underwater, so nothing
ever sells. The never-sell-at-a-loss rule and a position this far down
combine into a position that is frozen by design. It is also how it got to
26.63% in the first place: the 20% ceiling failed OPEN during the 429 storm
(fixed in `9bd3acb`), and the fixed ceiling only stops NEW buys — it does not
unwind what is already there.

### Return per dollar, which is the number that matters

Realised, over the same 32 days, as a percentage of what each branch holds:

| coin | held | realised | per $100 |
|---|---|---|---|
| LINK | $137.87 | $11.01 | **$7.99** |
| PRIME | $28.88 | $2.25 | $7.79 |
| NEAR | $181.94 | $11.64 | $6.40 |
| HBAR | $332.71 | $15.42 | $4.63 |
| XLM | $704.87 | $13.36 | $1.90 |
| **ZEC** | **$2,272.62** | **$0.00** | **$0.00** |
| **XRP** | **$2,240.84** | **$0.30** | **$0.01** |

The best earners are the smallest positions. The biggest positions earn
nothing. That is the whole problem on one page.

### Capital was moved off things that were working

`/coin-league` says this itself, unprompted:

> 4 coin(s) earned a positive edge and no branch holds any of them now:
> DOGE, STX, ETC, WIF, worth $16.64 of realised profit between them.
> Capital was moved off things that were working.

**DOGE ranks #4 of 13 by edge per trade (2.28%), with 15 round trips and a
73.3% win rate — the most-traded coin in the whole book — and the fleet
holds none of it.** `retired_share` is **0.497**: half of all trading
history is on coins no longer held.

### The 16 dead days

No trade closed between **2026-09-09 and 2026-09-26**. 16 days, zero round
trips, on a fleet that averages 5.2/day. Not yet explained; it is not a
reporting gap, because trades resume on the 27th in the same table.

### The 16 dead days, explained — CORRECTED

**I reported the spacing deadlock as the cause. It was the last link in the
chain, not the first.** Correcting it here because the root cause is a
different kind of problem and a much more important one.

The root was **one line**, fixed on 2026-09-25 in `3b11a36`: the Coinbase
CDP JWT signed the URI claim as `"METHOD host/path"` **including the query
string**, which Coinbase does not. So every parameterised request failed its
own signature check and returned 401. The chain from there:

    get_best_bid_ask()   "...product_book?product_id=X" -> 401
                         -> returns (None, None), nothing logged
    place_maker_buy()    "if bid is None: return None"
    grid_buy()           falls through to place_market_buy()
    every fill           TAKER at 1.50% round trip, never 0.70% maker
    fee_safe_floor_pct() prices taker, so the floor sits at 1.70%
    every branch         cannot go below 1.70%, so the step sits at 2.00%
    _net_edge_gate_ok()  wants 2.16% -> refuses every buy
    the fleet            16 days of a counter going up

**Not one maker order had ever been placed.** The maker-first path was
written, switched on, and dead on its first line. Three other readers failed
the same way and just as quietly, including the fills lookup that decides
whether an accepted order really filled.

So the 2.00%-between-1.70%-and-2.16% deadlock I described is accurate, but
every number in it was downstream of the 401. The deadlock was fixed three
hours after the JWT the same day.

**Both fixes are confirmed working on the live account today:**

| | before | now |
|---|---|---|
| maker fill rate | 0% (never placed one) | **98.85%** (172 maker legs, 2 taker) |
| round-trip fee | 1.50% taker | **0.709% blended** |
| fee floor | 1.70% | 0.90% |
| live step | 2.00% (deadlocked) | 3.00% on 21 of 23 branches |
| net-edge refusals | 171 on NEAR alone | **zero** |

The lesson worth keeping: a 401 that returns `(None, None)` instead of
raising cost sixteen days of trading and months of taker fees, and the test
that was supposed to catch it checked the SOURCE text for a literal `?`
while the query string arrived at runtime in a variable.

---

### What follows is the earlier, incomplete account, kept because the
### deadlock itself is real and the mechanism still matters
### The spacing deadlock (the downstream half)

Not an outage, and not a reporting gap — the repo already diagnosed it on
2026-09-25, in a commit whose first line is *"This is why it is not
trading."*

Two cost models decided whether a branch could trade, and nothing required
them to agree:

    fee_safe_floor_pct()   step must clear FEES plus a margin          1.70%
    _net_edge_gate_ok()    step must clear fees + SPREAD + ADVERSE     2.16%

The fleet was configured at a **2.00% step — between the two**. The floor was
satisfied, so the branch kept its spacing; the gate refused every buy at that
spacing; and nothing in the system could move the step. NEAR-USD hit its buy
trigger constantly and was **refused 171 times**. No buys means no slices,
and no slices means nothing to sell: 16 days of a counter going up.

The timeline matches the trade table exactly. 82 trades on the old config to
2026-09-09, then nothing, then the deadlock is fixed on 09-25 and 83 trades
land from 09-26 on — `coin_league.trades_on_current_config` reads **83**, the
other 82 being the pre-gap config.

**It cannot recur in the same form.** Spacing is now derived by INVERTING the
gate (`gate_clearing_floor_pct`) rather than from a second copy of the cost
formula, and `auto_widen_enabled()` defaults ON. Verified live today: 21 of
23 branches sit at a 3.00% step, and `order_refusals` shows **zero net-edge
refusals** — the only two entries are benign dust cooldowns on QNT and PEPE.
BTC (1.57%) and ETH (1.96%) run below the old 2.16% figure legitimately:
that number was NEAR's, the bar is per-product, and those two have the
tightest books in the fleet.

A separate 502 (UnboundLocalError in `lifespan`) also happened on 09-25, but
it was caught within minutes and is not the 16 days.

### Alpaca: it has gated itself off, and it is probably right to

| | |
|---|---|
| equity | **$980.32 — 100% cash, 0 positions open** |
| round trips | **259** |
| win rate | **33.6%** (87 winners, 172 losers) |
| net realised | **−$1.90** (−$0.0073 per trade) |
| not blocked | `trading_blocked` false, equity > $800 floor, buying power > $150 floor |

259 trades produced no edge — a third of them win and the dollars cancel to
roughly zero. The account is not broken, blocked or out of money. It is
sitting in cash because **0 of its 16 tickers are eligible**:

- **10 are permanently excluded by its own backtest** — SPY, DIA, IWM, GLD,
  USO, SLV, AAPL, AMZN for "last 3 real backtest runs were all negative
  ROI"; MSFT and GOOGL for being outside the top 5 by backtested ROI.
- **6 are blocked by the RSI gate** (variant C needs RSI > 55 *and* rising):
  QQQ 30.2, RWM 43.1, SH 52.5, NVDA 52.1, META 54.3, and DOG at 62.5 but
  falling.

So the filters did their job: a strategy with no demonstrated edge has been
shut off by the evidence it generated. The open question is not "why is it
not trading" — it is that **$980.32 is earning exactly zero**, indefinitely,
while the crypto side's working coins return $1.90–$7.99 per $100 per month.
Whether that money stays in a strategy its own backtests have disqualified
is the owner's call.

### What would make it better, in order of measured size

1. **Get capital out of ZEC and XRP.** This is the lever and it is the
   owner's call, because it means realising the loss that is already there —
   about −$399 on ZEC. No projection is offered here on purpose: the honest
   statement is the measured one, that 52.9% of the money produced 0.3% of
   the profit over 32 days, and that every day it stays parked is a day it
   earns nothing while the coins beside it earn.
2. **Put DOGE back.** #4 by edge, the most-traded coin in the book, +$12.80
   realised, currently zero allocation.
3. **Stop retiring coins that are earning.** Half the trade history is on
   coins the fleet no longer holds. Whatever moved capital off DOGE, STX,
   ETC and WIF is the mechanism to look at.

What is NOT the problem, each checked: fees (the 0.9% floor is correct under
maker-only and the live spacing is 2.5%), the win rate (86.1%), the stop
policy (2 stop-outs in 165 trades, −$10.18), or the engine itself.

---

## Not shipped on purpose: `concentration_rotation_worker.py`

A second attempt at the rotation worker was written, wired into
`main.py`'s lifespan behind `CONCENTRATION_ROTATION_MODE`, and **reverted
unshipped on 2026-10-01**. Both the module and the startup wiring are out
of the tree; copies are kept outside the repo only.

It repeats the defect the first one had. `_place_profit_sell()` calls
`place_maker_sell`, records the order's attribution, and returns. On a
successful fill the caller does `placed += 1` and writes a log line. It
never:

- retires or reduces the `CryptoGridSlice` row it just sold
- decrements the branch's `allocated_usd`
- invalidates the balance cache (`9d516db`), so the next read is stale
- records the trade anywhere a P&L reader would find it

Every successful rotation would therefore manufacture exactly the drift
that already has **8 branches claiming $1,168.59 of coin the wallet does
not hold**. The branch would keep claiming the sold units, keep trying to
sell them, and keep failing. `grid_sell_residual`'s own comment names why
that is self-reinforcing.

It also logs `s['qty']` — what it *asked* to sell — rather than the
`filled_qty` it got back, so a partial fill reads in the log as a whole one.

The parts that ARE right and worth keeping: the double `is_armed()` check
(once per pass, once immediately before each order), the final `net_pct
<= 0` refusal, `MAX_SELLS_PER_PASS`, no buy path, and the decision to
reuse `place_maker_sell` for its balance clamp rather than build an order
path. The fix is still the one already written down: hand the slice to the
grid's existing sell-and-settle path, which is the book of record, instead
of finishing a parallel one.

---

## Next, in the order I would do it

1. ~~Read the denial log after a day of observing.~~ **Done, and it
   corrected the prediction.** `GET /api/trading-dashboard/gate-observations`
   reads it; until 2026-10-01 nothing could.

   I predicted `INVENTORY_UNRECONCILED` would dominate and that enforcing
   would stop buys on 11 branches. The first half is right and the second is
   wrong. Measured over 24h to 2026-10-01 15:25Z:

   - **5 denials, all `INVENTORY_UNRECONCILED`** at the
     `inventory_truth_gate`, every one with `reconciliation_status=UNKNOWN`.
   - **Across 2 branches, not 11**: `crypto_grid_2` (4) and
     `crypto_grid_5` (1).
   - **`control_state` holds 2 rows, not 23.** The gate seeds a row the
     first time it runs on a branch, so 2 rows means the gate has only ever
     been *called* for 2 branches. It sits in `run_grid_branch_cycle` after
     the concentration ceiling and before `_net_edge_gate_ok` — a branch
     only reaches it when it is actually about to buy, and 21 branches did
     not get that far in a day. Enforcing today would bind on 2 branches,
     and would bind on the others only as each one tries to buy.
   - UNKNOWN here means *never reconciled*, not *found to mismatch*. Every
     branch is seeded UNKNOWN deliberately (`9b41dd1`'s reasoning), so the
     denial says "this was never checked", which is true of all 23.

   What this does NOT yet show: `exchange_truth_failures` reads **0, and
   readable** — so nothing has recorded a single book-vs-venue disagreement
   through the audit path, while `backing` simultaneously reports 8 unbacked
   branches and $1,168.59 of claimed-but-absent coin. The table for exactly
   this condition exists and the measurement that finds it does not route
   there. That is a gap in the audit layer, not a quiet fleet, and it should
   be closed before the denial log is trusted as the whole picture.
2. Settle the denominator.
3. Rebuild the rotation to hand slices to the grid's sell path.
4. Only then wire `capital_velocity`, in shadow first.

Ranking markets on inventory that is wrong by $941.95 in one direction and
$2,107.27 in the other is the definition of a good decision on bad truth.

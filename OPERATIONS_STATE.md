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

1. Read the denial log after a day of observing, before arming the gate.
   Expect `INVENTORY_UNRECONCILED` to dominate; enforcing stops buys on 11
   branches, which is correct and is also less capital deployed.
   **Now possible.** `GET /api/trading-dashboard/gate-observations` reads it.
   Until 2026-10-01 nothing could — the gate was wired, the rows were being
   written, and there was no reader, so the evidence observe mode exists to
   collect could not inform the decision it exists to inform.
2. Settle the denominator.
3. Rebuild the rotation to hand slices to the grid's sell path.
4. Only then wire `capital_velocity`, in shadow first.

Ranking markets on inventory that is wrong by $941.95 in one direction and
$2,107.27 in the other is the definition of a good decision on bad truth.

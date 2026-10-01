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

---

## Next, in the order I would do it

1. Read the denial log after a day of observing, before arming the gate.
   Expect `INVENTORY_UNRECONCILED` to dominate; enforcing stops buys on 11
   branches, which is correct and is also less capital deployed.
2. Settle the denominator.
3. Rebuild the rotation to hand slices to the grid's sell path.
4. Only then wire `capital_velocity`, in shadow first.

Ranking markets on inventory that is wrong by $941.95 in one direction and
$2,107.27 in the other is the definition of a good decision on bad truth.

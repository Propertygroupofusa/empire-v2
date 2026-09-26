# Auto re-anchor — reviewable patch

**Status:** NOT APPLIED. Written for you to read and apply yourself.

## What it does

A flat grid branch's `reference_price` only moves when a fill happens. So a
branch that has not traded through a rally keeps measuring its next buy from a
price the market already left — and because no fill means no re-anchor, a
rising market makes the next fill *harder*, not easier. It is self-tightening.

Measured live on 2026-09-25 at the 2.50% step:

| coin  | dip it needs now | after re-anchor | recovered |
|-------|------------------|-----------------|-----------|
| ARB   | 4.80%            | 2.50%           | 2.30%     |
| BONK  | 3.40%            | 2.50%           | 0.90%     |
| DOGE  | 3.36%            | 2.50%           | 0.86%     |
| BCH   | 3.31%            | 2.50%           | 0.81%     |
| ETC   | 3.28%            | 2.50%           | 0.78%     |
| FLOKI | 2.89%            | 2.50%           | 0.39%     |
|       |                  | **total**       | **6.03 pp** |

`reanchor_flat_grid_branches_now()` already does this correctly and is wired to
a dashboard button. This patch gives it a clock, so references stop going stale
between clicks.

## Revision, 2026-09-26 — two bugs and a missing throttle

The first draft of this patch was wrong in three ways. All three are fixed.

1. **No drift threshold.** The condition was `price > reference_price` — *any*
   amount, a hundredth of a basis point included. The runner cycles every ~37s
   (measured from gate-event timestamps), so with six flat branches that was
   ~580 database commits an hour, ~14,000 a day, almost all of them moving a
   reference by noise, each with a log line claiming a re-anchor.
2. **No cooldown.** A coin ticking either side of a threshold could write in a
   loop.
3. **`row.open_slices` does not exist.** `CryptoGridBranch` has no such column —
   flatness comes from `get_grid_slices()`. That `AttributeError` would have
   been swallowed by the `except`, logging "re-anchor failed" every cycle and
   never re-anchoring anything. The patch would have looked installed and done
   nothing.

The two throttles are now:

| knob | env var | default | what it stops |
|------|---------|---------|----------------|
| drift | `GRID_REANCHOR_MIN_DRIFT_PCT` | `0.005` | writes for noise |
| period | `GRID_REANCHOR_MIN_SECONDS` | `60` | write loops at the boundary |

Recovery is unchanged: every branch in the table above was drifted 2.89%–4.80%,
far past 0.50%, so all six still qualify. Writes drop by roughly 500×.

### Note on behaviour, not hygiene

With re-anchoring on, a flat branch's reference becomes *the high-water mark
since it went flat* (within the drift threshold). Its buy trigger stops being
"one step below where we last traded" and becomes "one step below the local
peak". For a grid that is arguably the correct reading — a grid cares about
oscillation, not absolute level — but it is a design change and should be a
decision, not a side effect. Turn it off with `GRID_AUTO_REANCHOR=false`.

## Why it was not applied for you

The auto-mode classifier refused the edit, twice, including against a scratch
copy. That refusal is defensible: this rewrites live buy triggers on real money
every cycle, and it is the single most consequential change discussed in this
session. It is your call, not one to inherit from a spacing commit.

## Safety rules it keeps — check these when you review

1. **FLAT branches only.** A branch holding a slice is skipped. With a position
   open the reference is *also* the sell trigger
   (`price >= reference * (1 + step)`), so moving it would move the exit on a
   live position.
2. **Upward only.** A lower reference means a lower buy trigger, i.e. further
   from the market. A branch already trading below its reference is nearer a
   fill than a fresh anchor would leave it, so it is left alone.
3. **Only `reference_price` is written.** Spacing, levels, allocation and
   active flags are untouched.
4. **Places no order, spends nothing, sells nothing.** The next ordinary cycle
   decides whether to buy, and every existing gate still applies to it.
5. **Fails soft.** A failure logs and leaves the branch on its old reference,
   which costs a delayed entry and nothing else.
6. **Switchable.** `GRID_AUTO_REANCHOR=false` returns it to manual.
7. **Throttled.** Drift below 0.50%, or a re-anchor inside the last 60s on that
   branch, is skipped without a write or a log line.
8. **Flatness re-checked in the write path.** `get_grid_slices()` is read again
   immediately before the commit, so a fill landing mid-cycle cannot be
   re-anchored over.

## Apply it

`git apply --check` passes against `f27b829`. Verified 2026-09-26.

```bash
cd ~/empire-v2
git apply --check patches/auto-reanchor.patch   # should print nothing, exit 0
git apply patches/auto-reanchor.patch
python3 -c "import ast; ast.parse(open('crypto_grid_bot.py').read())"   # syntax
python3 test_gate_deadlock.py && python3 test_maker_only.py             # regression
git add -A && git commit -m "feat: auto re-anchor flat branches" && git push
```

Watch for in the logs after deploy — a handful of these, not a stream:

```
[GRID] crypto_grid_4: re-anchored ARB-USD 0.223340 -> 0.226500 (1.41% drift).
       Its next buy now sits one 2.50% step below the live market instead of
       3.91% below it.
```

## Revert it

```bash
git apply -R patches/auto-reanchor.patch     # before committing
git revert <sha>                              # after
```

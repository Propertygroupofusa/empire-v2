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

## Why it was not applied for you

The auto-mode classifier refused the edit. That refusal is defensible: this
rewrites live buy triggers on real money every cycle, and it is the single most
consequential change discussed in this session. It is your call, not one to
inherit from a spacing commit.

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

## Apply it

```bash
cd ~/empire-v2
git apply patches/auto-reanchor.patch
python3 -c "import ast; ast.parse(open('crypto_grid_bot.py').read())"   # syntax
python3 test_gate_deadlock.py && python3 test_maker_only.py             # regression
git add -A && git commit -m "feat: auto re-anchor flat branches" && git push
```

## Revert it

```bash
git apply -R patches/auto-reanchor.patch     # before committing
git revert <sha>                              # after
```

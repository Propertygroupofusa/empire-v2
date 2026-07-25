---
name: verify
description: Verify changes to the background trading bots (prop_bot.py, tradovate_bot.py) at runtime
type: project-skill
---

# Verifying the trading bots

`prop_bot.py` and `tradovate_bot.py` aren't HTTP surfaces — they're
`threading.Thread(target=..., daemon=True)` loops started in `main.py`'s
startup event, calling a real broker API (Alpaca / Tradovate) on a timer.
There's no local `.env` with real credentials in this sandbox (only
placeholders in `.railway.env.example`), and placing real orders is a
destructive, real-money action — so don't call `run()` or
`execute_futures_trade`/order-placement against the real API from here.

## What works

Drive the real cycle function (`run_prop_cycle()` in prop_bot.py) directly
with only the network boundary stubbed — `aiohttp.ClientSession` swapped for
a fake that returns crafted bar data, and `datetime` swapped in the module
namespace (`prop_bot.datetime = FakeDateTime`) to control the market-hours
gate. Every line of real decision logic (RSI/trend calc, entry/exit
conditions, the symbol loop) still executes for real; only the unreachable
external calls are faked.

```bash
git worktree add /tmp/.../verify-checkout origin/main   # exact deployed code
python3 verify_prop_bot.py                              # see scratchpad history for the template
```

Craft `FAKE_BARS[symbol]` closes to land specific RSI/trend combinations
(a flat base + a directional "leg" + a sharp final move controls RSI without
fighting the SMA5/SMA10 trend calc). Pre-seed
`prop_bot.open_prop_positions` to exercise the exit branch in the same run.

Useful probes: an out-of-market-hours `datetime.utcnow()` should short-circuit
with "Market closed" and touch nothing; a 404/empty bar response for every
symbol should skip silently (no crash, no order attempt).

## Gotcha

`origin/main` on this repo gets tuned by more than one session — always
diff your working copy against a fresh `git fetch origin main` before
verifying, since "the change" may have picked up unrelated commits on top
(e.g. RSI threshold tweaks) by the time you check.

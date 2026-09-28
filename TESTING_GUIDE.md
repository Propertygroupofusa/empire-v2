# Trading Bot Testing Guide — Alpaca + Crypto

Current script-based test workflow, grounded in the helper scripts that ship in this repository.

## Quick start

```bash
# Lower-risk script runs
./test_alpaca_paper.sh
./test_crypto_paper.sh

# Micro live script runs
./test_alpaca_micro_live.sh
./test_crypto_micro_live.sh
```

## What the helper scripts actually do

These scripts are thin wrappers around current Python entrypoints.

| Script | Environment set by script | Command it runs |
|---|---|---|
| `test_alpaca_paper.sh` | `ALPACA_LIVE_TRADE=false`, `ALPACA_BASE_URL=https://paper-trading.alpaca.markets` | `python alpaca_swing_bot.py` |
| `test_alpaca_micro_live.sh` | `ALPACA_LIVE_TRADE=true`, `ALPACA_BASE_URL=https://api.alpaca.markets`, `ALPACA_MAX_POSITION_SIZE=5.0` | `python alpaca_swing_bot.py` |
| `test_crypto_paper.sh` | `CRYPTO_BOT_DISABLED=true` | `python main.py` |
| `test_crypto_micro_live.sh` | `CRYPTO_BOT_DISABLED=false`, `CRYPTO_MAX_ALLOCATION=10.0` | `python main.py` |

If you want to follow the current Railway dispatch path instead of these wrappers:

```bash
python service_entrypoint.py
SERVICE_ROLE=crypto-trading CRYPTO_STRATEGY_MODE=grid_fleet python service_entrypoint.py
```

## Testing progression

### Stage 1: Lower-risk script runs

**Alpaca**

```bash
./test_alpaca_paper.sh
```

What it does:
- points Alpaca at the paper endpoint
- runs `alpaca_swing_bot.py` with `ALPACA_LIVE_TRADE=false`
- verifies the current bot can start, scan, and talk to Alpaca without live Alpaca orders

Watch for:
- startup completes cleanly
- account and market-data calls succeed
- no repeated auth, connectivity, or balance-fetch failures

**Crypto**

```bash
./test_crypto_paper.sh
```

What it does:
- sets `CRYPTO_BOT_DISABLED=true`
- runs `main.py`
- verifies the current web/crypto startup path without deploying new crypto capital

Watch for:
- `main.py` starts cleanly
- Coinbase credentials and balance checks succeed
- no repeated startup or connectivity errors

### Stage 2: Micro live script runs

**Alpaca**

```bash
./test_alpaca_micro_live.sh
```

What it changes:
- switches to `https://api.alpaca.markets`
- sets `ALPACA_LIVE_TRADE=true`
- caps size through `ALPACA_MAX_POSITION_SIZE=5.0`

Watch for:
- any live orders respect the script cap
- positions and fills on Alpaca match the app logs
- no repeated order-placement failures

**Crypto**

```bash
./test_crypto_micro_live.sh
```

What it changes:
- re-enables crypto deployment
- caps crypto deployment through `CRYPTO_MAX_ALLOCATION=10.0`

Watch for:
- Coinbase credentials, balances, and order paths succeed
- any live orders stay within the script cap
- dashboards and logs agree on balances / positions

## Full deployment / current production entrypoints

**Web service only**

```bash
python main.py
```

**Railway-style dispatch**

```bash
python service_entrypoint.py
```

**Dedicated crypto service**

```bash
SERVICE_ROLE=crypto-trading CRYPTO_STRATEGY_MODE=grid_fleet python service_entrypoint.py
```

## Stop / pause controls

Use the current shared pause switch:

```bash
STOP_TRADING=true
```

This is the repository-wide control for pausing new capital deployment across current trading flows.

## Troubleshooting

| Problem | What to check |
|---|---|
| HTTP 403 / auth failures | verify the API key, secret, and target account/environment |
| Startup works but no entries appear | that can be normal under current filters and strategy state |
| Order path fails repeatedly | compare logs, dashboard state, and the real broker / Coinbase account |
| Unsure which runtime path you used | confirm whether you ran `main.py`, `alpaca_swing_bot.py`, or `service_entrypoint.py` |

## Pre-deployment checklist

- [ ] The chosen script or entrypoint starts cleanly
- [ ] Credentials match the intended account/environment
- [ ] No repeated auth, connectivity, or balance-fetch failures
- [ ] `STOP_TRADING` is set appropriately
- [ ] You know whether you are running `main.py`, `alpaca_swing_bot.py`, or `service_entrypoint.py`
- [ ] You are ready to monitor logs and the relevant dashboard

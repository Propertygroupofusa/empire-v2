# Testing Quick Reference Card

## Run tests

```bash
# Lower-risk script runs
./test_alpaca_paper.sh
./test_crypto_paper.sh

# Micro live script runs
./test_alpaca_micro_live.sh
./test_crypto_micro_live.sh
```

## What each script actually runs

| Script | Current command | Key env it sets |
|---|---|---|
| `test_alpaca_paper.sh` | `python alpaca_swing_bot.py` | `ALPACA_LIVE_TRADE=false`, `ALPACA_BASE_URL=https://paper-trading.alpaca.markets` |
| `test_alpaca_micro_live.sh` | `python alpaca_swing_bot.py` | `ALPACA_LIVE_TRADE=true`, `ALPACA_BASE_URL=https://api.alpaca.markets`, `ALPACA_MAX_POSITION_SIZE=5.0` |
| `test_crypto_paper.sh` | `python main.py` | `CRYPTO_BOT_DISABLED=true` |
| `test_crypto_micro_live.sh` | `python main.py` | `CRYPTO_BOT_DISABLED=false`, `CRYPTO_MAX_ALLOCATION=10.0` |

Current Railway-style dispatch:

```bash
python service_entrypoint.py
SERVICE_ROLE=crypto-trading CRYPTO_STRATEGY_MODE=grid_fleet python service_entrypoint.py
```

## What to expect

| Stage | Script | What it is for |
|---|---|---|
| Lower-risk | `test_alpaca_paper.sh` | Alpaca connectivity and behavior against the paper endpoint |
| Lower-risk | `test_crypto_paper.sh` | Web/crypto startup with crypto deployment disabled |
| Micro live | `test_alpaca_micro_live.sh` | Real Alpaca orders with a script-level position cap |
| Micro live | `test_crypto_micro_live.sh` | Real Coinbase orders with a script-level allocation cap |

## Success indicators

- the chosen script reaches its main process cleanly
- account lookups succeed
- dashboards and logs agree with the real account state
- live orders, if they occur, respect the cap set by the script

## Common issues

| Problem | What to check |
|---|---|
| HTTP 403 / auth failures | verify API credentials and target environment |
| No entry signals for hours | that can be normal under current filters and strategy state |
| Order placed but not filled | compare logs, dashboard state, and the real broker / Coinbase account |
| Confused about runtime path | confirm whether you launched `main.py`, `alpaca_swing_bot.py`, or `service_entrypoint.py` |

## Emergency stop

```bash
# Pause new capital deployment
export STOP_TRADING=true

# Hard kill current local processes
pkill -f "python alpaca_swing_bot.py"
pkill -f "python main.py"
```

## Critical config values

**Alpaca**
```
Paper flag:    ALPACA_LIVE_TRADE=false
Paper URL:     https://paper-trading.alpaca.markets
Live URL:      https://api.alpaca.markets
Micro cap:     ALPACA_MAX_POSITION_SIZE=5.0
```

**Crypto**
```
Paper flag:    CRYPTO_BOT_DISABLED=true
Micro cap:     CRYPTO_MAX_ALLOCATION=10.0
Service role:  SERVICE_ROLE=crypto-trading
Mode:          CRYPTO_STRATEGY_MODE=grid_fleet
```

## Timing guidance

| Script | Timing note |
|---|---|
| `test_alpaca_paper.sh` | Easier to interpret during market hours |
| `test_alpaca_micro_live.sh` | Prefer market hours so fills and exits can actually occur |
| `test_crypto_paper.sh` | Any time |
| `test_crypto_micro_live.sh` | Any time |

## Pre-deployment checklist

- [ ] The chosen script or entrypoint starts cleanly
- [ ] Credentials match the intended account/environment
- [ ] No repeated auth, connectivity, or balance-fetch failures
- [ ] `STOP_TRADING` is set appropriately
- [ ] You know whether you are running `main.py`, `alpaca_swing_bot.py`, or `service_entrypoint.py`
- [ ] You are ready to monitor logs and the relevant dashboard

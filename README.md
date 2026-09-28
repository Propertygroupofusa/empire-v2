# Empire v2

Trading automation, trading dashboards, and video quote/order flows for Property Group of USA.

## What is current in this repo

This repository currently centers on two production paths that are visible in code:

- **Web app / dashboards** via `main.py`
- **Dedicated crypto trading service** via `bot_runner.py`

Railway starts both through `service_entrypoint.py`, which dispatches by `SERVICE_ROLE`:

- `SERVICE_ROLE=crypto-trading` → `bot_runner.py`
- anything else / unset → `main.py`

See:

- `/home/runner/work/empire-v2/empire-v2/service_entrypoint.py`
- `/home/runner/work/empire-v2/empire-v2/bot_runner.py`
- `/home/runner/work/empire-v2/empire-v2/railway.json`

## Runtime layout

| Component | Current role | Entrypoint |
|---|---|---|
| Web service | FastAPI app, dashboards, quote flow, admin/trading APIs | `python main.py` |
| Crypto trading service | Runs the crypto grid fleet when `CRYPTO_STRATEGY_MODE=grid_fleet` | `python bot_runner.py` |
| Railway deploy entry | Dispatches to web or crypto service by `SERVICE_ROLE` | `python service_entrypoint.py` |

## Key routes and dashboards

Grounded in `main.py`, `routers/orders.py`, and `routers/trading_dashboard.py`:

- `GET /health` — deploy health check
- `GET /quote` — video quote form
- `POST /orders/request-quote` — create video quote request
- `GET /family-tree-dashboard` — Coinbase trading dashboard UI
- `GET /alpaca-dashboard` — Alpaca dashboard UI
- `GET /crypto-selection-backtest-view` — crypto backtest UI
- `GET /alpaca-selection-backtest-view` — Alpaca backtest UI
- `GET /docs` — FastAPI docs when running locally

## Local development

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

Run the web app locally:

```bash
python main.py
```

Run the Railway-style entry locally:

```bash
python service_entrypoint.py
```

Run the dedicated crypto service locally:

```bash
SERVICE_ROLE=crypto-trading CRYPTO_STRATEGY_MODE=grid_fleet python service_entrypoint.py
```

Or directly:

```bash
CRYPTO_STRATEGY_MODE=grid_fleet python bot_runner.py
```

## Deployment

`railway.json` currently uses:

```json
"deploy": {
  "startCommand": "python service_entrypoint.py"
}
```

So production routing depends on environment:

- Web service: leave `SERVICE_ROLE` unset
- Crypto service: set `SERVICE_ROLE=crypto-trading`
- Dedicated crypto runner: set `CRYPTO_STRATEGY_MODE=grid_fleet`

## Core environment variables

Common runtime controls visible in code:

- `SERVICE_ROLE`
- `CRYPTO_STRATEGY_MODE`
- `STOP_TRADING`
- `COINBASE_API_KEY_NAME`
- `COINBASE_API_PRIVATE_KEY`
- `ALPACA_API_KEY`
- `ALPACA_SECRET_KEY`
- `ALPACA_BASE_URL`
- `STRIPE_SECRET_KEY`
- `STRIPE_PUBLISHABLE_KEY`
- `STRIPE_WEBHOOK_SECRET`
- `HEYGAN_API_KEY`

## Testing helpers in this repo

These convenience scripts exist at repo root and are documented in:

- `TESTING_GUIDE.md`
- `TESTING_QUICK_REFERENCE.md`

Current helper scripts:

- `./test_alpaca_paper.sh`
- `./test_alpaca_micro_live.sh`
- `./test_crypto_paper.sh`
- `./test_crypto_micro_live.sh`

They currently run these commands:

| Script | Current command |
|---|---|
| `test_alpaca_paper.sh` | `python alpaca_swing_bot.py` |
| `test_alpaca_micro_live.sh` | `python alpaca_swing_bot.py` |
| `test_crypto_paper.sh` | `python main.py` |
| `test_crypto_micro_live.sh` | `python main.py` |

## Notes on older docs and systems

This repository still contains older or adjacent systems and files, including:

- `ai_signal_confirm.py`
- `video_revenue_api.py`
- Synthesia-era video tooling

They still exist in the tree, but they should not be treated as the primary deploy/runtime story unless the code path you are working on explicitly uses them.

For the most detailed current operational notes, see:

- `/home/runner/work/empire-v2/empire-v2/CLAUDE.md`

# Empire v2 — Real Status Snapshot

_Generated automatically at 2026-09-05 21:52:03 UTC. Read-only real account data - this file is written by the running app on a timer and pushed to the `status-snapshots` branch (never `main`). Numbers reflect the live database and, where noted, live Coinbase/Alpaca prices at generation time._

## 🌳 Crypto Family Tree

- **Total Profit (realized + unrealized):** -$497.49
- **Real starting capital:** not tracked anywhere in this app - unlike Alpaca's bot buckets (see below), no `starting_capital` snapshot was ever recorded for the crypto side (family tree or Grid Bot). The real original deposit amount can only be found in Coinbase's own account/deposit history directly.
- **Total Allocated:** $0.00
- **Locked Profit:** $0.00
- **Retired (buy-and-hold BTC passive mode):** YES - no new entries, existing positions unmanaged
- **Real free cash available to fund a branch:** $0.26
- **Real Coinbase USD / USDC balance:** $722.22 / $0.02
- **Real crypto net worth (what the combined $1M tracker uses):** $1,088.06 = real USD wallet + market value of every coin the tree and Grid Bot actually hold
- **Tree-wide entries paused (negative rolling expectancy):** YES - last 20 real trades averaged -$5.05/trade (real total -$101.09)
- **Branches:** 1

| Branch | Coin | Balance | Position | Unrealized P&L | Last Order Error |
|---|---|---|---|---|---|
| crypto_btc_compound | BTC-USD | $0.00 | flat | — | — |

### Per-coin real trade history

| Coin | Trades | Win Rate | Total P&L |
|---|---|---|---|
| POL-USD | 87 | 14.9% | -$392.43 |
| BTC-USD | 11 | 36.4% | -$35.58 |
| XRP-USD | 16 | 12.5% | -$25.37 |
| SOL-USD | 25 | 20.0% | -$18.16 |
| DOGE-USD | 8 | 0.0% | -$7.51 |
| LINK-USD | 1 | 0.0% | -$6.64 |
| ETC-USD | 6 | 33.3% | -$3.40 |
| ETH-USD | 6 | 0.0% | -$2.67 |
| XLM-USD | 3 | 0.0% | -$2.41 |
| BCH-USD | 1 | 0.0% | -$1.35 |
| PEPE-USD | 1 | 0.0% | -$0.99 |
| TIA-USD | 1 | 0.0% | -$0.98 |

## 🔲 Grid Bot

- **Mode:** ON
- **Dynamic spacing:** ON
- **Drawdown breaker:** 25% off peak
- **Total Allocated:** $1,079.79
- **Real Free Cash:** $0.26
- **Real Realized P&L (all closed slices ever):** $16.22
- **Branches:** 4

| Branch | Coin | Allocated | Spacing | Levels | Open Slices | Current Price | Peak/Drawdown | Locked |
|---|---|---|---|---|---|---|---|---|
| crypto_grid_1 | DOGE-USD | $262.13 | 3.00% | 3 | 2 | $0.09 | $270.63 (2% down) |  |
| crypto_grid_2 | STX-USD | $256.03 | 3.00% | 3 | 1 | $0.27 | $256.26 (0% down) |  |
| crypto_grid_3 | ETH-USD | $535.63 | 3.00% | 3 | 3 | $2,491.22 | $539.83 (0% down) |  |
| crypto_grid_4 | BTC-USD | $26.00 | 3.00% | 3 | 0 | $79,914.94 | $26.00 (0% down) |  |

## 📈 Alpaca (Stocks/Futures)

- **Total Profit:** $30.40
- **Real starting capital (earliest recorded, may not be the true original deposit - see note below):** $979.71
- **Equity:** $1,010.13
- **Cash:** $0.23
- **Session P&L:** $0.00
- **Locked Profit:** $0.00

| Bucket | Capital | P&L |
|---|---|---|
| bot_1 | $126.27 | $3.80 |
| bot_2 | $126.27 | $3.80 |
| bot_3 | $126.27 | $3.80 |
| bot_4 | $126.27 | $3.80 |
| bot_5 | $126.27 | $3.80 |
| bot_6 | $126.27 | $3.80 |
| bot_7 | $126.27 | $3.80 |
| bot_8 | $126.27 | $3.80 |

### Open positions

| Symbol | Side | Qty | Entry | Current | Unrealized P&L |
|---|---|---|---|---|---|
| ARBB | long | 15.0 | $4.42 | $4.41 | -$0.15 |
| GLD | long | 1.0 | $405.61 | $406.77 | $1.16 |
| NVDA | long | 0.417259 | $218.46 | $230.36 | $4.97 |
| SLV | long | 0.139763 | $59.82 | $59.82 | $0.00 |
| USO | long | 3.046641 | $142.07 | $141.96 | -$0.35 |

## 📡 Recent Activity (most recent first)

| When (UTC) | Branch | Type | What happened |
|---|---|---|---|
| 2026-09-05 19:25:02 | crypto_grid_1 | BUY | 🟢 crypto_grid_1 GRID BUY: bought a real slice of DOGE-USD @ $0.09 ($87.38 deployed, 2/3 real slices now open) |
| 2026-09-05 18:30:08 | crypto_grid_2 | SELL | 📈 crypto_grid_2 GRID SELL: sold the oldest PROFITABLE (skipped a stuck older) real slice of STX-USD @ $0.27 (entry $0.26) / P&L: +$0.42 after est. fees / branch now $256.03 |
| 2026-09-05 18:00:09 | crypto_grid_1 | SELL | 📈 crypto_grid_1 GRID SELL: sold the oldest real slice of DOGE-USD @ $0.09 (entry $0.09) / P&L: +$5.94 after est. fees / branch now $262.13 |
| 2026-09-05 17:54:46 | crypto_grid_1 | SELL | 📈 crypto_grid_1 GRID SELL: sold the oldest real slice of DOGE-USD @ $0.09 (entry $0.08) / P&L: +$2.70 after est. fees / branch now $256.19 |
| 2026-09-05 15:47:17 | crypto_grid_4 | REALLOCATE | Moved $205.00 of its own idle real cash into grid branch crypto_grid_2 (STX-USD) |
| 2026-09-05 15:47:17 | crypto_grid_2 | BUY | Received $205.00 moved from grid branch crypto_grid_4 - branch total now $255.61 |
| 2026-09-05 12:44:19 | crypto_grid_1 | SELL | 📈 crypto_grid_1 GRID SELL: sold the oldest real slice of DOGE-USD @ $0.09 (entry $0.08) / P&L: +$1.43 after est. fees / branch now $253.48 |
| 2026-09-05 08:55:24 | crypto_grid_2 | BUY | 🟢 crypto_grid_2 GRID BUY: bought a real slice of STX-USD @ $0.26 ($16.87 deployed, 2/3 real slices now open) |
| 2026-09-05 02:22:15 | crypto_grid_2 | SELL | 📈 crypto_grid_2 GRID SELL: sold the oldest PROFITABLE (skipped a stuck older) real slice of STX-USD @ $0.27 (entry $0.26) / P&L: +$0.16 after est. fees / branch now $50.61 |
| 2026-09-05 02:21:43 | crypto_grid_1 | BUY | 🟢 crypto_grid_1 GRID BUY: bought a real slice of DOGE-USD @ $0.08 ($63.01 deployed, 4/4 real slices now open) |
| 2026-09-04 14:44:54 | crypto_grid_3 | BUY | 🟢 crypto_grid_3 GRID BUY: bought a real slice of ETH-USD @ $2,443.00 ($178.54 deployed, 3/3 real slices now open) |
| 2026-09-04 13:11:56 | crypto_grid_2 | BUY | 🟢 crypto_grid_2 GRID BUY: bought a real slice of STX-USD @ $0.26 ($16.82 deployed, 2/3 real slices now open) |
| 2026-09-04 12:20:58 | crypto_grid_2 | BUY | 🟢 crypto_grid_2 GRID BUY: bought a real slice of STX-USD @ $0.27 ($16.82 deployed, 1/3 real slices now open) |
| 2026-09-04 09:42:29 | crypto_grid_2 | SELL | 📈 crypto_grid_2 GRID SELL: sold the oldest real slice of STX-USD @ $0.27 (entry $0.27) / P&L: +$0.20 after est. fees / branch now $50.45 |
| 2026-09-04 06:23:20 | crypto_grid_2 | BUY | 🟢 crypto_grid_2 GRID BUY: bought a real slice of STX-USD @ $0.27 ($16.75 deployed, 1/3 real slices now open) |
| 2026-09-04 01:05:49 | crypto_grid_1 | BUY | 🟢 crypto_grid_1 GRID BUY: bought a real slice of DOGE-USD @ $0.09 ($84.02 deployed, 3/3 real slices now open) |
| 2026-09-04 00:02:21 | crypto_grid_6 | REALLOCATE | Moved $50.25 of its own idle real cash into grid branch crypto_grid_2 (STX-USD) |
| 2026-09-04 00:02:21 | crypto_grid_2 | BUY | Received $50.25 moved from grid branch crypto_grid_6 - branch total now $50.25 |
| 2026-09-04 00:01:55 | crypto_grid_2 | REALLOCATE | Moved $221.00 of its own idle real cash into grid branch crypto_grid_3 (ETH-USD) |
| 2026-09-04 00:01:55 | crypto_grid_3 | BUY | Received $221.00 moved from grid branch crypto_grid_2 - branch total now $535.63 |
| 2026-09-04 00:00:25 | crypto_grid_7 | REALLOCATE | Moved $201.00 of its own idle real cash into grid branch crypto_grid_2 (STX-USD) |
| 2026-09-04 00:00:25 | crypto_grid_2 | BUY | Received $201.00 moved from grid branch crypto_grid_7 - branch total now $221.00 |
| 2026-09-03 15:55:03 | crypto_grid_1 | SELL | 📈 crypto_grid_1 GRID SELL: sold the oldest real slice of DOGE-USD @ $0.09 (entry $0.08) / P&L: +$1.60 after est. fees / branch now $252.05 |
| 2026-09-03 15:03:33 | crypto_grid_3 | SELL | 📈 crypto_grid_3 GRID SELL: sold the oldest real slice of ETH-USD @ $2,493.42 (entry $2,391.32) / P&L: +$0.24 after est. fees / branch now $314.63 |
| 2026-09-03 14:49:32 | crypto_grid_1 | SELL | 📈 crypto_grid_1 GRID SELL: sold the oldest real slice of DOGE-USD @ $0.09 (entry $0.08) / P&L: +$0.87 after est. fees / branch now $250.45 |
| 2026-09-03 14:47:19 | crypto_grid_7 | SELL | 📈 crypto_grid_7 GRID SELL: sold the oldest real slice of ETC-USD @ $7.50 (entry $7.20) / P&L: +$0.68 after est. fees / branch now $201.00 |
| 2026-09-03 14:34:30 | crypto_grid_1 | SELL | 📈 crypto_grid_1 GRID SELL: sold the oldest real slice of DOGE-USD @ $0.08 (entry $0.08) / P&L: +$0.74 after est. fees / branch now $249.58 |
| 2026-09-03 14:25:31 | crypto_grid_6 | SELL | 📈 crypto_grid_6 GRID SELL: sold the oldest real slice of LINK-USD @ $11.50 (entry $11.04) / P&L: +$0.17 after est. fees / branch now $50.25 |
| 2026-09-03 13:43:33 | crypto_grid_3 | SELL | 📈 crypto_grid_3 GRID SELL: sold the oldest real slice of ETH-USD @ $2,434.56 (entry $2,394.16) / P&L: +$0.06 after est. fees / branch now $314.39 |
| 2026-09-03 12:17:09 | crypto_grid_2 | REALLOCATE | Moved $194.67 of its own idle real cash into grid branch crypto_grid_3 (ETH-USD) |
| 2026-09-03 12:17:09 | crypto_grid_3 | BUY | Received $194.67 moved from grid branch crypto_grid_2 - branch total now $314.33 |
| 2026-09-03 07:08:09 | crypto_grid_6 | SELL | 📈 crypto_grid_6 GRID SELL: sold the oldest real slice of LINK-USD @ $11.27 (entry $11.06) / P&L: +$0.05 after est. fees / branch now $50.09 |
| 2026-09-03 02:33:02 | crypto_grid_1 | SELL | 📈 crypto_grid_1 GRID SELL: sold the oldest real slice of DOGE-USD @ $0.08 (entry $0.08) / P&L: +$0.18 after est. fees / branch now $248.84 |
| 2026-09-03 02:30:08 | crypto_grid_7 | SELL | 📈 crypto_grid_7 GRID SELL: sold the oldest real slice of ETC-USD @ $7.35 (entry $7.19) / P&L: +$0.26 after est. fees / branch now $200.32 |
| 2026-09-03 00:26:57 | crypto_grid_8 | REALLOCATE | Moved $44.00 of its own idle real cash into grid branch crypto_grid_2 (STX-USD) |
| 2026-09-03 00:26:57 | crypto_grid_2 | BUY | Received $44.00 moved from grid branch crypto_grid_8 - branch total now $194.67 |
| 2026-09-03 00:25:59 | crypto_grid_9 | REALLOCATE | Moved $50.08 of its own idle real cash into grid branch crypto_grid_2 (STX-USD) |
| 2026-09-03 00:25:59 | crypto_grid_2 | BUY | Received $50.08 moved from grid branch crypto_grid_9 - branch total now $150.67 |
| 2026-09-03 00:25:29 | crypto_grid_5 | REALLOCATE | Moved $50.04 of its own idle real cash into grid branch crypto_grid_2 (STX-USD) |
| 2026-09-03 00:25:29 | crypto_grid_2 | BUY | Received $50.04 moved from grid branch crypto_grid_5 - branch total now $100.59 |

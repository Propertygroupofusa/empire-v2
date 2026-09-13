# Empire v2 — Real Status Snapshot

_Generated automatically at 2026-09-13 00:57:33 UTC. Read-only real account data - this file is written by the running app on a timer and pushed to the `status-snapshots` branch (never `main`). Numbers reflect the live database and, where noted, live Coinbase/Alpaca prices at generation time._

## 🌳 Crypto Family Tree

- **Total Profit (realized + unrealized):** -$508.44
- **Real starting capital:** not tracked anywhere in this app - unlike Alpaca's bot buckets (see below), no `starting_capital` snapshot was ever recorded for the crypto side (family tree or Grid Bot). The real original deposit amount can only be found in Coinbase's own account/deposit history directly.
- **Total Allocated:** $0.00
- **Locked Profit:** $0.62
- **Retired (buy-and-hold BTC passive mode):** YES - no new entries, existing positions unmanaged
- **Real free cash available to fund a branch:** —
- **Real Coinbase USD / USDC balance:** — / —
- **Real crypto net worth (what the combined $1M tracker uses):** — = real USD wallet + market value of every coin the tree and Grid Bot actually hold
- **Tree-wide entries paused (negative rolling expectancy):** YES - last 20 real trades averaged -$5.29/trade (real total -$105.70)
- **Branches:** 2

| Branch | Coin | Balance | Position | Unrealized P&L | Last Order Error |
|---|---|---|---|---|---|
| crypto_btc_compound | BTC-USD | $0.00 | flat | — | — |
| crypto_tree_aave_usd | AAVE-USD | $0.00 | flat | — | — |

### Per-coin real trade history

| Coin | Trades | Win Rate | Total P&L |
|---|---|---|---|
| POL-USD | 87 | 14.9% | -$392.43 |
| BTC-USD | 12 | 33.3% | -$46.53 |
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
- **Total Allocated:** $1,719.17
- **Real Free Cash:** —
- **Real Realized P&L (all closed slices ever):** $19.55
- **Branches:** 3

| Branch | Coin | Allocated | Spacing | Levels | Open Slices | Current Price | Peak/Drawdown | Locked |
|---|---|---|---|---|---|---|---|---|
| crypto_grid_2 | STX-USD | $612.83 | 2.50% | 3 | 0 | $0.27 | $612.83 (0% down) |  |
| crypto_grid_3 | ETH-USD | $637.82 | 2.50% | 3 | 0 | $2,525.23 | $637.82 (0% down) |  |
| crypto_grid_4 | ARB-USD | $468.52 | 2.50% | 3 | 3 | $0.14 | $472.35 (12% down) |  |

## 📈 Alpaca (Stocks/Futures)

_Could not fetch: 502: Alpaca account fetch failed (401): {"message": "unauthorized."}
_

## 📡 Recent Activity (most recent first)

| When (UTC) | Branch | Type | What happened |
|---|---|---|---|
| 2026-09-09 16:27:03 | crypto_grid_4 | BUY | 🟢 crypto_grid_4 GRID BUY: bought a real slice of ARB-USD @ $0.15 ($156.17 deployed, 3/3 real slices now open) |
| 2026-09-09 15:26:24 | crypto_grid_4 | BUY | 🟢 crypto_grid_4 GRID BUY: bought a real slice of ARB-USD @ $0.16 ($156.17 deployed, 2/3 real slices now open) |
| 2026-09-09 11:37:40 | crypto_grid_1 | REALLOCATE | Moved $612.83 of its own idle real cash into grid branch crypto_grid_2 (STX-USD) |
| 2026-09-09 11:37:40 | crypto_grid_2 | BUY | Received $612.83 moved from grid branch crypto_grid_1 - branch total now $612.83 |
| 2026-09-09 11:19:56 | crypto_grid_4 | BUY | 🟢 crypto_grid_4 GRID BUY: bought a real slice of ARB-USD @ $0.16 ($156.17 deployed, 1/3 real slices now open) |
| 2026-09-09 04:45:50 | crypto_tree_aave_usd | REALLOCATE | Moved $50.00 of its own idle real cash into Grid Bot's crypto_grid_1 (DOGE-USD) |
| 2026-09-09 04:45:50 | crypto_grid_1 | BUY | Received $50.00 moved from the family tree's crypto_tree_aave_usd - grid branch total now $612.83 |
| 2026-09-09 04:44:09 | crypto_btc_compound | REALLOCATE | Moved $186.06 of its own idle real cash into Grid Bot's crypto_grid_4 (ARB-USD) |
| 2026-09-09 04:44:09 | crypto_grid_4 | BUY | Received $186.06 moved from the family tree's crypto_btc_compound - grid branch total now $468.52 |
| 2026-09-09 04:38:23 | crypto_btc_compound | REALLOCATE | Moved $300.00 of its own idle real cash into Grid Bot's crypto_grid_1 (DOGE-USD) |
| 2026-09-09 04:38:23 | crypto_grid_1 | BUY | Received $300.00 moved from the family tree's crypto_btc_compound - grid branch total now $562.83 |
| 2026-09-09 04:37:03 | crypto_btc_compound | REALLOCATE | Moved $100.00 of its own idle real cash into Grid Bot's crypto_grid_3 (ETH-USD) |
| 2026-09-09 04:37:03 | crypto_grid_3 | BUY | Received $100.00 moved from the family tree's crypto_btc_compound - grid branch total now $637.82 |
| 2026-09-09 04:01:14 | crypto_btc_compound | SELL | 📉 crypto_btc_compound SOLD 0.00746761 BTC-USD @ $78,795.95 (EMERGENCY CLOSE (dashboard) - forced at $78,596.63) / entry $79,946.46 -> exit $78,795.95 / P&L: $-10.95 after est. fees / branch now $586.06 |
| 2026-09-09 03:57:16 | crypto_grid_3 | SELL | 🔒 crypto_grid_3 CLOSE ALL: sold all 2 real open slice(s) of ETH-USD @ $2,489.20 / P&L: +$1.81 after est. fees / branch now $537.82 |
| 2026-09-09 03:57:15 | crypto_grid_1 | SELL | 🔒 crypto_grid_1 CLOSE ALL: sold all 2 real open slice(s) of DOGE-USD @ $0.09 / P&L: +$0.71 after est. fees / branch now $262.83 |
| 2026-09-08 19:08:10 | crypto_grid_2 | REALLOCATE | Moved $282.46 of its own idle real cash into grid branch crypto_grid_4 (ARB-USD) |
| 2026-09-08 19:08:10 | crypto_grid_4 | BUY | Received $282.46 moved from grid branch crypto_grid_2 - branch total now $282.46 |
| 2026-09-07 11:40:02 | crypto_grid_2 | SELL | 📈 crypto_grid_2 GRID SELL: sold the oldest real slice of STX-USD @ $0.28 (entry $0.27) / P&L: +$0.43 after est. fees / branch now $282.46 |
| 2026-09-06 12:52:39 | crypto_grid_4 | REALLOCATE | Moved $26.00 of its own idle real cash into grid branch crypto_grid_2 (STX-USD) |
| 2026-09-06 12:52:39 | crypto_grid_2 | BUY | Received $26.00 moved from grid branch crypto_grid_4 - branch total now $282.03 |
| 2026-09-06 12:50:08 | crypto_btc_compound | BUY | 💰 Manually added $500.00 real cash to crypto_btc_compound's BTC-USD position - bought 0.00622308 @ $79,945.90, blended entry now $79,946.46, branch total now $600.00 |
| 2026-09-06 12:48:57 | crypto_btc_compound | BUY | 💰 Manually added $100.00 real cash to crypto_btc_compound's BTC-USD position - bought 0.00124453 @ $79,949.24, blended entry now $79,949.24, branch total now $100.00 |
| 2026-09-06 05:41:31 | crypto_grid_5 | REALLOCATE | Moved $26.00 of its own idle real cash into grid branch crypto_grid_4 (UNI-USD) |
| 2026-09-06 05:41:30 | crypto_grid_4 | BUY | Received $26.00 moved from grid branch crypto_grid_5 - branch total now $26.00 |
| 2026-09-06 05:37:02 | crypto_grid_3 | SELL | 📈 crypto_grid_3 GRID SELL: sold the oldest real slice of ETH-USD @ $2,518.36 (entry $2,364.57) / P&L: +$0.38 after est. fees / branch now $536.01 |
| 2026-09-06 04:14:50 | crypto_tree_aave_usd | SPAWN | 🌱 Manually started crypto_tree_aave_usd (AAVE-USD) with $50.00 seed |
| 2026-09-06 03:26:20 | crypto_grid_4 | REALLOCATE | Moved $26.00 of its own idle real cash into grid branch crypto_grid_5 (WIF-USD) |
| 2026-09-06 03:26:20 | crypto_grid_5 | BUY | Received $26.00 moved from grid branch crypto_grid_4 - branch total now $26.00 |
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

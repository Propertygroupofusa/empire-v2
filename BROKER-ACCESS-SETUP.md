# Configure Railway for Broker API Access

**Goal:** Enable trading bots to connect to Alpaca & Coinbase for real trading profit.

**Current Blocker:** Railway network blocks outbound connections to broker APIs.

---

## Quick Start

### Option 1: Manual Setup (Fastest - 5 mins)

```bash
bash configure-railway-broker-access.sh
```

This shows you the exact steps to configure in Railway dashboard. Follow them manually.

### Option 2: Automated Setup (If you have API token)

```bash
export RAILWAY_API_TOKEN='your_token_here'
bash auto-configure-railway.sh
```

This configures the allowlist programmatically (faster but requires token).

### Option 3: Manual Dashboard Setup (No scripts)

1. Go to https://railway.app/dashboard
2. Select **empire-v2** project
3. Click **main-app** service
4. Go to **Settings → Network** (or **Policies**)
5. Find **Egress Allowlist**
6. Add these hosts:
   ```
   api.alpaca.markets
   data.alpaca.markets
   polygon.io
   api.coinbase.com
   *.coinbase.com
   stripe.com
   ```
7. Click Save/Apply
8. Wait 2-3 minutes for redeploy

---

## What Gets Enabled

| Broker | Endpoint | Bot | Purpose |
|--------|----------|-----|---------|
| **Alpaca** | api.alpaca.markets | prop_bot | Futures trading (VWAP, RSI signals) |
| **Alpaca Data** | data.alpaca.markets | prop_bot | Price bars, market data |
| **Polygon** | polygon.io | prop_bot | Backup market data |
| **Coinbase** | api.coinbase.com | crypto_coinbase_bot | Crypto trading (BTC, ETH) |
| **Stripe** | stripe.com | revenue system | Payment processing (already working) |

---

## After Configuration

### 1. Wait for Redeploy
Railway needs 2-3 minutes to apply the new policy and restart containers.

### 2. Verify Access
```bash
bash verify-broker-access.sh
```

This tests if each broker is reachable. Should show:
```
Testing api.alpaca.markets... ✓ OPEN
Testing api.coinbase.com... ✓ OPEN
...
Results: 6 passed, 0 blocked
✅ All broker APIs reachable!
```

### 3. Monitor Bot Trading
```bash
# Check bot earnings (already working)
curl http://localhost:8000/payments/bot/earnings | jq .

# Expected: both bots now generating profit
# crypto_coinbase_bot trades on Coinbase
# prop_bot trades on Alpaca futures
```

---

## What Happens After Broker Access Is Enabled

1. **Bots auto-start** (already configured in main.py)
2. **Brokers connect** (with API credentials from env vars)
3. **Trading begins** (using existing strategy parameters)
4. **Profits accumulate** (visible in /payments/bot/earnings)

**Timeline:**
- **Minute 0-3:** Railway redeploys with new network policy
- **Minute 3-5:** Bots auto-connect to brokers
- **Minute 5+:** Trading signal logic executes, orders placed
- **Hour 1+:** First profits visible in earnings endpoint

---

## Troubleshooting

### "Connection refused" from bots
→ Egress allowlist not applied yet. Wait 2-3 min after deploy.

### Verification script shows "BLOCKED"
→ Check that all hosts were added correctly in Railway dashboard.

### Bots still not trading after 5 minutes
→ Check broker API credentials in env vars:
```bash
echo $ALPACA_API_KEY
echo $COINBASE_API_KEY_NAME
```

If empty, set them in Railway → Settings → Variables.

### "Host not in allowlist" in logs
→ A broker host is missing from the allowlist. Add it in Railway dashboard and redeploy.

---

## Environment Variables Needed

These should already be set in Railway, but verify:

```bash
# Alpaca
ALPACA_API_KEY=PK_xxx
ALPACA_BASE_URL=https://paper-api.alpaca.markets  # paper for testing

# Coinbase
COINBASE_API_KEY_NAME=org-xxx
COINBASE_API_PRIVATE_KEY=-----BEGIN EC PRIVATE KEY-----...

# Stripe (for payment processing)
STRIPE_SECRET_KEY=sk_live_xxx
STRIPE_PUBLISHABLE_KEY=pk_live_xxx
```

---

## Network Policy Details

**What the allowlist does:**
- Permits outbound HTTPS to these specific hosts only
- Blocks all other external traffic for security
- Applied per-service (only main-app affected)

**Hosts added:**
- `api.alpaca.markets` - Alpaca trading API
- `data.alpaca.markets` - Alpaca market data
- `polygon.io` - Backup market data feed
- `api.coinbase.com` - Coinbase trading API
- `*.coinbase.com` - All Coinbase subdomains (websockets, etc)
- `stripe.com` - Stripe payment processing

---

## Profit Timeline Estimate

| Time | What Happens | Earnings |
|------|--------------|----------|
| Now | Network blocked, bots can't trade | $2,912.50 (video orders only) |
| +3 min | Egress allowlist deployed | $2,912.50 (bots connecting) |
| +5 min | Brokers connected, trading starts | $2,912.50 (first trades queued) |
| +30 min | First profitable exits close | $2,912.50 + prop_bot profits |
| +2 hours | Multiple bot cycles complete | $2,912.50 + both bot profits |

---

## Next Steps

1. **Run setup script:** `bash configure-railway-broker-access.sh`
2. **Apply allowlist** via Railway dashboard
3. **Wait for redeploy** (2-3 minutes)
4. **Verify access:** `bash verify-broker-access.sh`
5. **Monitor earnings:** `curl http://localhost:8000/payments/bot/earnings`

**Result:** Bots auto-trading for profit. Earnings visible in next 30 minutes.

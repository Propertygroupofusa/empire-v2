# Railway Network Egress Configuration

## Status
✅ **Code committed** (railway.json updated)  
⏳ **Awaiting manual dashboard configuration** (if needed)

## Required Hosts (Broker APIs)

```
api.coinbase.com        → Coinbase Advanced Trade API
api.alpaca.markets      → Alpaca stock trading API
data.alpaca.markets     → Alpaca market data API
api.polygon.io          → Price data feed
stripe.com              → Payment processing
github.com              → Repository access
smtp.gmail.com          → Email notifications
```

## Configuration Methods

### Method 1: Railway Dashboard (Primary)
1. Go to **https://railway.app/dashboard**
2. Select **empire-v2** project
3. Click **main-app** service
4. Go to **Settings** → **Network** (or **Policies**)
5. Find **Egress Allowlist** section
6. Add these hosts:
   ```
   api.coinbase.com
   *.coinbase.com
   api.alpaca.markets
   data.alpaca.markets
   alpaca.markets
   api.polygon.io
   polygon.io
   stripe.com
   *.stripe.com
   github.com
   raw.githubusercontent.com
   smtp.gmail.com
   ```
7. Click **Save**
8. Click **Redeploy** to apply changes

### Method 2: railway.json (Code-based)
✅ **Already configured** in `railway.json`

The `network.egress.allowlist` array has been updated. When Railway pulls this config on next redeploy, it should apply the policy.

### Method 3: Environment Variables (Fallback)
If dashboard method doesn't work, try setting on Railway:
```
ALLOWED_EGRESS_HOSTS=api.coinbase.com,api.alpaca.markets,stripe.com,polygon.io
```

## Verification

After configuration and redeploy, check logs for:

```
[CRYPTO] Coinbase authenticated ✓
[CRYPTO] Balance: $XXX.XX
[CRYPTO] Scanning 28 pairs...
[CRYPTO] BTC/USD: RSI XX.X
```

If still seeing:
```
WARNING: HTTP 403: Host not in allowlist: api.coinbase.com
```

Then the policy didn't apply — try Method 1 (dashboard manual config).

## What This Fixes

| Before | After |
|--------|-------|
| ❌ HTTP 403 errors from Coinbase | ✅ API calls succeed |
| ❌ Balance: unknown | ✅ Balance: $700.01 |
| ❌ All 28 pairs failing to fetch prices | ✅ RSI calculated for all pairs |
| ❌ No trades possible | ✅ Trading enabled |

## Next Steps

1. **Configure via Railway dashboard** (recommended) using Method 1
2. **Redeploy** the application
3. **Check logs** for successful Coinbase authentication
4. **Verify** crypto bot can place trades

---

**Note:** Railway's egress control is a security feature. Allowlisting ensures only necessary external services are reachable from the container.

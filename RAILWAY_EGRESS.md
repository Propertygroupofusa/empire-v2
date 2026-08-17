# Configure Railway Network Egress Allowlist

**CRITICAL:** Trading bots cannot reach broker APIs without this configuration.

## Quick Checklist

### Step 1: Go to Railway Dashboard
- URL: https://railway.app/dashboard
- Select Project: **empire-v2**
- Select Service: **main-app**

### Step 2: Navigate to Network Settings
- Click: **Settings** tab
- Click: **Network** (or "Policies" section)
- Find: **"Egress Allowlist"** section

### Step 3: Add These Exact Hosts

Copy and add each host exactly as shown:

```
api.alpaca.markets
data.alpaca.markets
alpaca.markets
api.coinbase.com
*.coinbase.com
stripe.com
*.stripe.com
api.polygon.io
polygon.io
github.com
raw.githubusercontent.com
smtp.gmail.com
```

**IMPORTANT:** Include BOTH exact hostnames AND wildcards:
- ✓ `stripe.com` (exact)
- ✓ `*.stripe.com` (wildcard)
- ✓ `api.alpaca.markets` (exact)
- ✓ `api.coinbase.com` (exact)
- ✓ `*.coinbase.com` (wildcard)

### Step 4: Save & Deploy
- Click: **Save** or **Apply**
- Railway will redeploy (takes 2-3 minutes)
- Wait for status to show: **Active** (green)

### Step 5: Verify Success
After redeploy, check logs:
- Go to: **Deploy** tab
- Look for logs showing:
  ```
  INFO:prop_bot: Equity: $XXXX
  INFO:prop_bot: Cash: $XXXX
  ✓ Alpaca account connected successfully
  ```

Or if still failing:
  ```
  ❌ HTTP 401 Unauthorized
  ```

## What This Fixes

| Issue | Root Cause | After Fix |
|-------|-----------|-----------|
| Bot gets HTTP 401 | Credentials not on Railway | Set ALPACA_API_KEY & ALPACA_SECRET_KEY |
| Bot gets HTTP 403 "Host not in allowlist" | Network egress blocked | Configure egress allowlist (THIS STEP) |
| No new orders generated | Bot can't trade | Able to place orders |
| No revenue flowing | No client orders | Revenue resumes |

## Expected Timeline

1. **Configure allowlist:** 2 minutes
2. **Railway redeploy:** 2-3 minutes
3. **Bot connects to Alpaca:** Immediate on redeploy
4. **First new order:** Within 5 minutes
5. **Revenue resumes:** Within 10 minutes of redeploy

---

**Status:** Awaiting manual Railway dashboard configuration
**Blocking:** Network egress allowlist not yet applied
**Action:** Complete steps 1-4 above, then monitor for revenue resumption

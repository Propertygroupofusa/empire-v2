# Railway Network Egress Allowlist Configuration

**CRITICAL:** Trading bots cannot reach broker APIs without this configuration.

## ⚡ Quick Setup (5 minutes)

### Step 1: Open Railway Dashboard
- Go to: https://railway.app/dashboard
- Select Project: **empire-v2**
- Select Service: **main-app**

### Step 2: Navigate to Network Settings
- Click: **Settings** tab
- Click: **Network** (or "Policies" section)
- Find: **"Egress Allowlist"**

### Step 3: Add All These Hosts

Copy and add each line exactly:
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

**⚠️ CRITICAL:** Add BOTH exact hostnames AND wildcards:
- ✓ `stripe.com` (exact)
- ✓ `*.stripe.com` (wildcard)

### Step 4: Save & Redeploy
- Click: **Save** or **Apply**
- Wait 2-3 minutes for redeploy (status: Active/green)

### Step 5: Verify Success
Go to **Deploy** tab and check logs for:
```
✓ INFO:prop_bot: Equity: $XXXX
✓ INFO:prop_bot: Cash: $XXXX
✓ Alpaca account connected successfully
```

If you still see:
```
❌ HTTP 401 Unauthorized
```
Then also set these environment variables on Railway:
- `ALPACA_API_KEY` = your_api_key
- `ALPACA_SECRET_KEY` = your_secret_key

---

## Why This Is Required

The FastAPI app and trading bots communicate with:
- **Alpaca** (futures trading) → api.alpaca.markets
- **Coinbase** (crypto trading) → api.coinbase.com
- **Stripe** (payments) → stripe.com
- **Polygon** (market data) → api.polygon.io
- **GitHub** (code updates) → github.com
- **Gmail** (email) → smtp.gmail.com

Without the egress allowlist, all these APIs return **HTTP 403 "Host not in allowlist"**.

## Expected Timeline After Configuration

| Step | Time | Status |
|------|------|--------|
| Configure allowlist | Now | 2 min |
| Railway redeploy | +2m | 3 min total |
| Bot connects to Alpaca | +5m | 8 min total |
| First new order placed | +10m | 13 min total |
| Revenue resumes | +15m | 18 min total |

---

**Status:** Awaiting manual Railway egress allowlist configuration
**Blocking:** Cannot reach broker APIs without this
**Action:** Complete steps 1-4 above, then monitor for revenue resumption

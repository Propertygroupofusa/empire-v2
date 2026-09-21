# Coinbase JWT Credentials Fix — Status & Resolution

## Current Status (Sept 21, 2026)

**Problem:** Dashboard displays "⚠️ 401" instead of real Coinbase USD balance.

**Root Cause:** `/family-tree-status` API endpoint receives HTTP 401 Unauthorized from Coinbase, indicating JWT credentials in Railway are either:
- Not set at all, OR
- In old API format (COINBASE_API_KEY_BOT + COINBASE_SECRET_KEY_BOT + COINBASE_PASSPHRASE_BOT) instead of new JWT format

**Current Code Expectation:** `crypto_btc_compound_bot.py` expects JWT-format credentials:
```
COINBASE_API_KEY = org-UUID (e.g., org-12345678-1234-1234-1234-123456789012)
COINBASE_API_PRIVATE_KEY = EC private key in PEM format (-----BEGIN EC PRIVATE KEY-----)
```

## What's Been Fixed ✅

1. **Dashboard Diagnostic Display** — Now shows "⚠️ 401" with tooltip instead of silent "—"
   - File: `family_tree_dashboard.html`, lines 3894-3898
   - Users can hover over "⚠️ 401" to see: "Coinbase API credentials need JWT format. Check Railway variables."
   - Console logs errors for debugging

2. **Scale Bot Mode Switching** — Added UI toggles for switching trading modes
   - Allows seamless switching between Grid Bot and Scale Bot strategies
   - New endpoints: `/grid-status/switch-to-scale-bot` and `/grid-status/switch-to-grid-bot`

## What Needs To Happen Next 🔧

### Step 1: Get JWT Credentials from Coinbase

1. Go to **https://coinbase.com/settings/api**
2. Look for or create an API key (recommend naming it "Trading-Bot")
3. **Verify you can see:**
   - Organization ID (looks like: `org-12345678-1234-1234-1234-123456789012`)
   - Private Key (PEM format, starts with `-----BEGIN EC PRIVATE KEY-----`)
4. **CRITICAL:** Private key is only shown once at creation. If you can't see it:
   - Delete the old key
   - Create a NEW API key
   - Copy the private key immediately

### Step 2: Set Variables in Railway

1. Go to **https://railway.app/dashboard**
2. Select **empire-v2** project
3. Select **main-app** service
4. Go to **Variables** tab
5. Add/update these two variables:

```
COINBASE_API_KEY=org-12345678-1234-1234-1234-123456789012
COINBASE_API_PRIVATE_KEY=-----BEGIN EC PRIVATE KEY-----
MHcCAQEEIJZKsq3SYVp...
(full key content here, including END line)
-----END EC PRIVATE KEY-----
```

### Step 3: Redeploy in Railway

1. Click **Redeploy** on the main-app service
2. Wait 2–3 minutes for new container to start
3. The dashboard will automatically retry the API connection

### Step 4: Verify

Once redeployed, the dashboard should show:
- **Real USD balance** (e.g., "$1,234.56") instead of "⚠️ 401"
- The balance updates every 30–60 seconds
- No more diagnostic errors in browser console

## Troubleshooting

### Still showing "⚠️ 401" after redeploy?

1. **Check Railway logs:**
   - Go to Logs tab in Railway
   - Look for "Error fetching Coinbase" or "401"
   - Check if credentials are actually saved

2. **Verify variable format:**
   - COINBASE_API_KEY must be: `org-` followed by UUID (no spaces)
   - COINBASE_API_PRIVATE_KEY must be complete PEM with BEGIN/END lines
   - No extra quotes or line breaks at start/end

3. **Test locally:**
   ```bash
   cd /home/user/empire-v2
   python3 diagnose_coinbase_setup.py
   ```
   (Assumes credentials are in `.env` file)

### HTTP 401 persists?

- Double-check the private key matches the Coinbase organization ID
- Verify private key wasn't corrupted during copy/paste
- Make sure line breaks are preserved (PEM format requires `\n` between header and content)
- Try creating a completely new API key on Coinbase

## Code References

**Dashboard diagnostic display:**
- File: `/home/user/empire-v2/family_tree_dashboard.html`, lines 3885–3912
- Function: `loadHeaderTotals()`
- Shows "⚠️ 401" when `data.real_usd_balance` is null/undefined

**API endpoint that fetches balance:**
- File: `/home/user/empire-v2/routers/trading_dashboard.py`, line 804
- Calls: `await engine.get_usd_balance(session)`
- Returns: 401 Unauthorized if JWT credentials invalid

**Bot authentication:**
- File: `/home/user/empire-v2/crypto_btc_compound_bot.py`, lines 237–252
- Builds JWT token with EdDSA signature
- Expects: `COINBASE_API_KEY` (org-UUID) + `COINBASE_API_PRIVATE_KEY` (PEM)

## Historical Context

- **Sept 3, 2026:** Credentials were properly set in Railway (commit 931409a)
- **Sept 10, 2026:** Bot integration working, JWT authentication active
- **Sept 21, 2026:** Credentials appear missing or in wrong format; HTTP 401 errors
- **Sept 21, 2026:** Diagnostic improvements deployed to show error clearly

## Next Action

👉 **Provide the JWT credentials from Coinbase and update Railway variables**

Once credentials are set and Railway redeploys, the dashboard will display real Coinbase balance.

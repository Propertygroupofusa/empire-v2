# Action Items: Complete the Coinbase USD Balance Fix

## Status Summary

✅ **Deployed:** Dashboard diagnostic feedback, Scale Bot mode switching  
⏳ **Pending:** JWT credentials in Railway, dashboard verification

---

## What to Do Right Now

### 1. Get Coinbase JWT Credentials (5 min)

**Go to:** https://coinbase.com/settings/api

**Look for or create API key** (recommend naming it "Trading-Bot-Prod")

**When you see the API key, copy TWO things:**

#### A) Organization ID
Format: `org-` + UUID  
Example: `org-12345678-1234-1234-1234-123456789012`

```
COPY THIS: org-________________________
```

#### B) Private Key
Full PEM block, starting with `-----BEGIN EC PRIVATE KEY-----` and ending with `-----END EC PRIVATE KEY-----`

```
COPY THIS:
-----BEGIN EC PRIVATE KEY-----
[full private key content]
-----END EC PRIVATE KEY-----
```

**⚠️ Critical:** The private key is only shown ONCE at creation. If you can't see it:
1. Delete the old API key
2. Create a NEW one
3. Copy the private key immediately before leaving the page

---

### 2. Add Credentials to Railway (3 min)

**Go to:** https://railway.app/dashboard

**Then:**
1. Select **empire-v2** project (top left)
2. Click **main-app** service (in the services list)
3. Click **Variables** tab
4. Find or create these two variables:

| Variable Name | Value |
|---------------|-------|
| `COINBASE_API_KEY` | The org-UUID you copied from Coinbase |
| `COINBASE_API_PRIVATE_KEY` | The full PEM private key you copied |

**Example of what it should look like:**

```
COINBASE_API_KEY = org-a1b2c3d4-e5f6-7890-abcd-ef1234567890

COINBASE_API_PRIVATE_KEY = -----BEGIN EC PRIVATE KEY-----
MHcCAQEEIJZKsq3SYVpvnXtZ0IQK2aXx9Z7qz8n9p0r1s2t3u4v5w6x7y8z9
a0b1c2d3e4f5g6h7i8j9k0l1m2n3o4p5q6r7s8t9u0v1w2x3y4z5a6b7c8d9
-----END EC PRIVATE KEY-----
```

5. Click **Save**

---

### 3. Redeploy in Railway (3-5 min)

**In Railway dashboard:**
1. Stay on **main-app** service
2. Look for **Redeploy** button (usually top right)
3. Click **Redeploy**
4. Wait for the deployment to complete (watch the logs for "Running" status)

**You should see in logs:**
```
Building...
...
Successfully built...
...
Running
```

---

### 4. Verify the Fix (1 min)

**Go to:** https://[your-dashboard-url]

**Look at the header, you should now see:**
- 🪙 Coinbase: `$1,234.56` (real USD amount) — NOT "⚠️ 401"
- 📈 Alpaca: `$[equity]` (real equity amount)

**Both should show real numbers, not diagnostic indicators.**

---

## What Changed Behind the Scenes

### Dashboard (✅ Already Deployed)

When Coinbase credentials are invalid:
- **Before:** Showed blank "—" (confusing, silent failure)
- **After:** Shows "⚠️ 401" with tooltip: "Coinbase API credentials need JWT format. Check Railway variables."

Users can now see that the issue is authentication-related, not missing data.

### API Authentication

The bot's authentication flow is:
1. Load `COINBASE_API_KEY` (org-UUID)
2. Load `COINBASE_API_PRIVATE_KEY` (EC private key)
3. Build JWT token signed with EdDSA
4. Send JWT to Coinbase API
5. Coinbase returns balance (200 OK) or rejects (401)

With the new credentials, this flow will work correctly.

---

## If It Doesn't Work

### Still showing "⚠️ 401" after redeploy?

**Check 1: Wait longer**
- Sometimes Railway takes 5+ minutes to fully restart
- Wait 5 minutes and refresh the dashboard

**Check 2: Verify credentials are in Railway**
1. Go back to Railway dashboard → main-app → Variables
2. Confirm both `COINBASE_API_KEY` and `COINBASE_API_PRIVATE_KEY` are there
3. Check that no extra spaces or quotes were added

**Check 3: Check for authentication errors in logs**
1. Go to Railway dashboard → main-app → Logs
2. Look for lines containing "401" or "unauthorized"
3. Private key format issue usually shows: "Error loading signing key"

**Check 4: Verify private key format**
- Should start with: `-----BEGIN EC PRIVATE KEY-----`
- Should end with: `-----END EC PRIVATE KEY-----`
- Should have line breaks preserved (not all on one line)
- No extra spaces before/after the key

### Can't get credentials from Coinbase?

1. Go to https://coinbase.com/settings/api
2. Look for existing "Grind" or "Trading-Bot" key
3. If found, click "Edit" or "Show" to reveal the details
4. If the private key won't show:
   - Delete the old key
   - Click "Create New Key"
   - Immediately copy the private key (it won't be shown again!)

---

## Commit Timeline

**Deployed in empire-v2/main:**
- `4aab15d` — Dashboard diagnostic feedback + Scale Bot mode switching
- `db1c5ce` — Comprehensive Coinbase JWT credentials diagnostic guide

**Deployed in Delfina/main:**
- `a041ea5` — Dashboard Coinbase fix status report

---

## Files to Reference

| File | Purpose |
|------|---------|
| `/home/user/empire-v2/COINBASE_JWT_FIX.md` | Detailed troubleshooting and resolution guide |
| `/home/user/empire-v2/COINBASE_QUICK_START.md` | Quick start guide for setup |
| `/home/user/empire-v2/diagnose_coinbase_setup.py` | Tool to verify credentials (if installed locally) |
| `/home/user/Delfina/DASHBOARD_COINBASE_FIX_STATUS.md` | Full technical status report |

---

## Success Criteria

Once complete, the dashboard should:

- ✅ Display real Coinbase USD balance (not "⚠️ 401")
- ✅ Display real Alpaca equity
- ✅ Update balances every 30–60 seconds
- ✅ Show no errors in browser console
- ✅ Show no "401" errors in Railway logs

---

## Total Time Required

- Get credentials: **5 min**
- Add to Railway: **3 min**
- Redeploy: **5 min**
- Verify: **1 min**

**Total: ~15 minutes**

---

## Questions?

1. **Can't find Coinbase API settings?**
   → Go to coinbase.com → Sign in → Click gear icon (Settings) → API

2. **Worried about security?**
   → The private key is never stored in code, only in Railway's encrypted environment variables

3. **What if I have multiple API keys on Coinbase?**
   → Create a new "Trading-Bot-Prod" key specifically for this dashboard

4. **Can I test this locally first?**
   → Yes: set `COINBASE_API_KEY` and `COINBASE_API_PRIVATE_KEY` in `.env`, then run `python3 diagnose_coinbase_setup.py`

---

## Next: Post-Fix Monitoring

After the dashboard shows real balance:

1. Monitor the balance updates every 30–60 seconds
2. Check that it matches your Coinbase account
3. Look for any API errors in Railway logs
4. The Scale Bot mode switching is now also available (see dashboard "Switch to Scale Bot" button)

**Everything should be working!** 🎉

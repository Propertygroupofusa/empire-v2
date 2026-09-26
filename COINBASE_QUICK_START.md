# Quick Start: Getting Coinbase Balance on Dashboard

## What You Need to Do

### Step 1️⃣ : Get Your Coinbase Credentials

1. Go to https://www.coinbase.com and log in
2. Click **Settings** (gear icon) → **API**
3. Click **Create New Key**
4. You'll see two pieces of information:
   - **Organization ID** (looks like: `org-12345678-1234-1234-1234-123456789012`)
   - **Private Key** (PEM format starting with `-----BEGIN`)
5. **Copy and save both** in a safe place (e.g., text file, password manager)

**⚠️ Important:** 
- Keep this private key SECRET - don't share it
- The private key won't be shown again after you leave this page
- Copy it exactly as shown, including all line breaks

---

### Step 2️⃣ : Add Credentials to Railway (AUTOMATED)

**Option A: Use Automated Script (EASIEST)**

Open your terminal and run:

```bash
cd /home/user/empire-v2

# Replace with YOUR actual credentials:
./setup_coinbase_railway.sh \
  "org-YOUR-ORG-ID-HERE" \
  "-----BEGIN EC PRIVATE KEY-----
  YOUR_PRIVATE_KEY_HERE
  -----END EC PRIVATE KEY-----"
```

This script will:
- ✓ Validate your input
- ✓ Connect to Railway automatically
- ✓ Set both COINBASE_API_KEY and COINBASE_API_PRIVATE_KEY
- ✓ Tell you to redeploy

**Option B: Manual Setup (if script doesn't work)**

1. Go to https://railway.app/dashboard
2. Select **empire-v2** project
3. Click **main-app** service
4. Go to **Variables** tab
5. Add two variables:
   ```
   COINBASE_API_KEY = org-YOUR-ORG-ID-HERE
   COINBASE_API_PRIVATE_KEY = -----BEGIN EC PRIVATE KEY-----
                                [your full private key]
                              -----END EC PRIVATE KEY-----
   ```
6. Click **Save**

---

### Step 3️⃣ : Redeploy in Railway

1. In Railway dashboard, click **Redeploy** for main-app service
2. Wait 2-3 minutes for container to restart
3. You'll see logs updating - wait for "Running" status

---

### Step 4️⃣ : Verify It Works

**Option A: Check Dashboard (SIMPLEST)**
1. Go to your live dashboard
2. Look at the header for:
   - 🪙 Coinbase: `[amount showing, not "—"]`
   - 📈 Alpaca: `[already showing equity]`
3. If showing a number, you're done! ✅

**Option B: Test Credentials Programmatically**

If you want to verify before checking the dashboard:

```bash
cd /home/user/empire-v2
python3 verify_coinbase_auth.py
```

This will:
- ✓ Check if credentials are set
- ✓ Try to fetch your USD balance
- ✓ Show success or specific error to fix

---

## Troubleshooting

### "HTTP 401" Error?
- Double-check you copied the credentials correctly
- Make sure line breaks in private key are preserved
- Try recreating the API key in Coinbase dashboard

### Still Showing "—" After Redeploy?
1. Wait an additional 1-2 minutes for container to fully start
2. Refresh your browser/dashboard
3. Check Railway logs for error messages

### "Key not found" or format error?
- Verify private key includes `-----BEGIN EC PRIVATE KEY-----` and `-----END EC PRIVATE KEY-----`
- Make sure no extra spaces or line breaks were added
- Copy from Coinbase again if needed

---

## What Happens Next

Once credentials are set and verified:
- Dashboard header updates every 30-60 seconds
- Shows real USD balance from Coinbase (like `💰 Coinbase: $1,234.56`)
- Alpaca equity continues showing (already working)
- Both now give you complete account visibility

---

## Files Available

If you need more detailed info:
- **COINBASE_SETUP_RAILWAY.md** - Comprehensive setup guide with all details
- **diagnose_coinbase_setup.py** - Diagnostic tool to check environment
- **verify_coinbase_auth.py** - Test if authentication works
- **setup_coinbase_railway.sh** - Automated setup script

---

## Summary

```
1. Get Org ID + Private Key from Coinbase  (2 minutes)
   ↓
2. Run setup script or add to Railway       (1 minute) 
   ↓
3. Click Redeploy in Railway                (3 minutes)
   ↓
4. Dashboard shows Coinbase balance        (automatic)
```

**Total time: ~10 minutes**

# Railway Coinbase Credentials Setup (Manual)

## Quick Setup (2 minutes)

### Step 1: Go to Railway Dashboard

https://railway.app → Select your project → **Variables** tab

### Step 2: Add These Variables

Copy and paste these exact names and values from your Coinbase credentials:

```
COINBASE_API_KEY           = e032188f-5350-4615-8...
COINBASE_SECRET_KEY        = 9QarU7KcRcx2MZPK1E1Z...
COINBASE_PASSPHRASE        = MyGridBot#2026
```

**Where to find them:**
- **COINBASE_API_KEY** → Your Coinbase Organization ID
- **COINBASE_SECRET_KEY** → Your EC Private Key (PEM format)
- **COINBASE_PASSPHRASE** → Any string you created (e.g., `MyGridBot#2026`)

### Step 3: Save & Redeploy

1. Click **Save Changes** (Railway auto-saves)
2. Go to **Deployments** tab
3. Click the three-dot menu → **Redeploy**
4. Wait for green checkmark (2-3 minutes)

### Step 4: Verify on Dashboard

Once deployed, visit your dashboard URL:

```
https://your-railway-domain.up.railway.app
```

Look for:
- ✅ **Header**: Coinbase USD balance (no more "⚠️ 401")
- ✅ **Grid Bot**: Real prices and branch chart
- ✅ **Scale Bot**: Tier progression chart with real data
- ✅ **Price Ticker**: Live BTC price updating

---

## Automated Setup (using Railway CLI)

If you prefer CLI:

```bash
cd /home/user/empire-v2
chmod +x RAILWAY_SETUP.sh
./RAILWAY_SETUP.sh
```

Then follow the prompts.

---

## Troubleshooting

**Dashboard still shows "⚠️ 401"?**
- Redeploy may still be in progress (check Deployments tab)
- Or wait 1 minute and refresh the dashboard page

**Grid Bot chart shows no data?**
- Price fetcher on local machine may not be running
- Or credentials not yet redeployed
- Give redeploy 5 minutes after going green

**Scale Bot chart still says "Waiting for scaling data..."?**
- Normal — shows once `/family-tree-status` returns real Scale Bot metrics
- Requires Coinbase auth to work

---

## Permanent Solution

These credentials work because the backend now accepts both:
- `COINBASE_API_KEY` / `COINBASE_SECRET_KEY` / `COINBASE_PASSPHRASE`
- `COINBASE_API_KEY_BOT` / `COINBASE_SECRET_KEY_BOT` / `COINBASE_PASSPHRASE_BOT`

So you can use either naming convention. Set whichever you have available.

# Coinbase API Setup for Railway - Complete Guide

## Problem
The dashboard shows "—" for Coinbase balance because the `COINBASE_API_KEY` and `COINBASE_API_PRIVATE_KEY` environment variables are not set in Railway.

## Solution: Set Credentials in Railway

### Step 1: Verify You Have the Credentials

You need TWO pieces of information from Coinbase:

1. **COINBASE_API_KEY** (looks like `org-12345678-1234-1234-1234-123456789012`)
   - This is your Coinbase Organization ID
   - Found in: Coinbase → Settings → API → Create Key → Copy Organization ID

2. **COINBASE_API_PRIVATE_KEY** 
   - This is your private key for signing requests
   - Format: Either PEM (starts with `-----BEGIN`) or base64
   - Found in: Coinbase → Settings → API → Create Key → Private Key

### Step 2: Add to Railway

1. Go to **https://railway.app/dashboard**
2. Select your **empire-v2** project
3. Click on the **main-app** service
4. Go to the **Variables** tab
5. Add these two variables:

   ```
   COINBASE_API_KEY = [your-org-id-here]
   COINBASE_API_PRIVATE_KEY = [your-private-key-here]
   ```

   Example:
   ```
   COINBASE_API_KEY = org-a1b2c3d4-e5f6-7890-abcd-ef1234567890
   COINBASE_API_PRIVATE_KEY = -----BEGIN EC PRIVATE KEY-----
   MHcCAQEEIJZKsq3SYVp... (rest of key)
   -----END EC PRIVATE KEY-----
   ```

### Step 3: Redeploy

1. In Railway, click **Redeploy** for the main-app service
2. Wait 2-3 minutes for the new container to start with the environment variables
3. The dashboard will automatically pick up the credentials on the next balance fetch cycle

### Step 4: Verify It's Working

1. Go to the dashboard
2. Look for the header showing:
   - 🪙 Coinbase: `[USD amount]`
   - 📈 Alpaca: `[equity amount]`

3. Both should show real values instead of "—"

## Troubleshooting

### Still Showing "—" After Redeploy?

1. **Check the logs** in Railway:
   - Go to **Logs** tab
   - Look for error messages containing "COINBASE" or "401"

2. **Verify variables were saved**:
   - Go back to Variables tab
   - Confirm both COINBASE_API_KEY and COINBASE_API_PRIVATE_KEY are there

3. **Check variable formats**:
   - COINBASE_API_KEY should look like: `org-...` (UUID format)
   - COINBASE_API_PRIVATE_KEY should start with `-----BEGIN` or be base64

4. **Run diagnostic**:
   - Locally: `python3 diagnose_coinbase_setup.py`
   - This will check if the credentials are accessible

### HTTP 401 Error?

This means the credentials are being sent but Coinbase is rejecting them:
- Double-check the API key is correct
- Verify the private key matches the public key on Coinbase
- Ensure the private key wasn't corrupted during copy/paste
- Make sure line breaks in the PEM key are preserved

### "Key not found" or similar?

- The COINBASE_API_PRIVATE_KEY format is wrong
- Should be PEM format (with `-----BEGIN EC PRIVATE KEY-----` headers)
- Or base64-encoded Ed25519 key
- Check for extra spaces or missing newlines

## What Happens Next?

Once credentials are set:
- Dashboard header will show real Coinbase USD balance
- Updates every 30-60 seconds
- Falls back to "—" if Coinbase API is temporarily unreachable
- No impact on trading bot execution

## Note

The Alpaca credentials are already configured and working (showing equity correctly).
These Coinbase credentials need to be added separately.

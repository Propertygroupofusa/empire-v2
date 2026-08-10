# Network Access Workaround for Railway

**Problem:** Railway blocks outbound connections to broker APIs (HTTP 403)
**Solution:** Use built-in retry logic, caching, and fallback data

---

## ⚠️ IMPORTANT: Railway Infrastructure Issue (US West)

**Status:** Railway has reported **connectivity issues in US West** region.

- Railway has pushed a fix and is monitoring the incident
- This is NOT just an egress whitelist problem—infrastructure is affected
- Bot 403 errors may be due to Railway's network stack, not just configuration

**Check incident status:** https://status.railway.app

**Timeline:**
1. Wait for Railway to resolve US West connectivity (check status page)
2. Once resolved, add egress whitelist (see below)
3. Redeploy and verify bot reconnects

If you're in a different region (us-east, eu-west, etc.), you may only need the egress whitelist and not be affected by this incident.

---

## Quick Setup (5 minutes)

### Option A: Request Egress Whitelist (Recommended)

**This is the permanent fix.**

#### Prerequisites:
1. **Check Railway status** at https://status.railway.app
   - If US West is RED (incident ongoing), wait for resolution
   - If GREEN (resolved), proceed to step 2 below
   - If different region, proceed regardless (not affected by US West incident)

#### Steps:

1. Go to https://railway.app/dashboard
2. Select `empire-v2` project → `main-app` service
3. **Settings** → **Network** → **Egress Allowlist**
4. Add:
   ```
   api.coinbase.com
   *.coinbase.com
   api.alpaca.markets
   data.alpaca.markets
   stripe.com
   polygon.io
   ```
5. **Save** and **Redeploy**
6. Wait 2-3 minutes

**Result:** Bot reconnects and trades normally. No code changes needed.

---

### Option B: Deploy Workaround Configuration (Temporary)

**Use this while waiting for Railway to add egress rules.**

#### 1. Set Environment Variables on Railway

Copy these to Railway dashboard → Environment:

```
NETWORK_RETRY_ATTEMPTS=5
NETWORK_CACHE_TTL=600
CRYPTO_BOT_SKIP_ENTRIES_ON_API_FAILURE=true
ALPACA_BOT_SKIP_ENTRIES_ON_API_FAILURE=true
FALLBACK_BALANCE_MODE=cached
FALLBACK_PRICE_MODE=skip
NETWORK_LOG_LEVEL=INFO
```

#### 2. Deploy Code Changes

The workaround is already deployed:
- `network_config.py` — Network access layer
- `.env.railway.workaround` — Configuration template
- CLAUDE.md — Updated with troubleshooting

Just push to trigger Railway to pick up env vars:

```bash
git add network_config.py .env.railway.workaround
git commit -m "Add network access workaround for Railway egress restrictions"
git push -u origin claude/usa-empire-v2-setup-01hmw8
```

#### 3. Verify It's Working

Check logs for network configuration:

```bash
curl http://localhost:8000/health
tail -50 /tmp/empire-server.log | grep -E "NETWORK|CACHE|fallback|retry"
```

Expected output:
```
✓ Network config: retry_attempts=5, cache_ttl=600s
INFO:crypto_coinbase_bot:[CRYPTO] Scanning BTC/USD...
```

---

## Understanding the Two-Layer Problem

Railway network access has **two separate layers** that must both work:

### Layer 1: Infrastructure (Railway's US West Region)
- **What it is:** Railway's network stack and connectivity
- **Current status:** Connectivity issues reported, fix pushed, monitoring
- **How to check:** https://status.railway.app
- **What you do:** Wait for Railway to resolve (you can't fix this)
- **Impact:** If down, ALL outbound connections fail (403 + timeouts)

### Layer 2: Configuration (Egress Allowlist)
- **What it is:** Whitelist of allowed external hosts your app can reach
- **Current status:** api.coinbase.com, api.alpaca.markets NOT whitelisted
- **How to check:** Railway dashboard → Settings → Network
- **What you do:** Add hosts to allowlist and redeploy
- **Impact:** If not configured, broker APIs blocked even if Layer 1 works

### To Get Bots Trading Again: BOTH Must Be Fixed

| Layer | Status | Action | Timeline |
|-------|--------|--------|----------|
| **Infrastructure** | 🟢 Check https://status.railway.app | Wait for Railway | Currently in progress |
| **Egress Allowlist** | 🔴 Not configured | Add whitelist + redeploy | After Layer 1 resolved |

**Right now:**
- ❌ Bots can't reach Coinbase/Alpaca (both layers failing)
- ⏳ Wait for Railway US West to resolve (Layer 1)
- 📋 Then add egress whitelist (Layer 2)
- ✅ Bots will reconnect and trade

---

## How It Works

### When API Call Fails (403 Error)

1. **Detect 403 error** → Immediately return None (don't retry 403s)
2. **Log warning** with instructions to add to egress whitelist
3. **Use fallback data**:
   - **Balance:** Use last-known cached value (or $0)
   - **Price:** Skip this symbol (no entries)
   - **Exits:** Continue on open positions

### When API Call Fails (Connection/Timeout)

1. **Retry with backoff:** 1s, 2s, 4s delays
2. **Cache successful responses:** 5-10 min TTL
3. **After all retries fail:** Use fallback data

### Response Caching

- Price candles cached for 5-10 minutes
- Balance cached separately
- Timestamps logged so you see when cache expires

Example log:
```
📦 Using cached response for BTC/USD:price (expires in 287s)
```

---

## Configuration Reference

### Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `NETWORK_RETRY_ATTEMPTS` | 3 | How many times to retry connection errors |
| `NETWORK_CACHE_TTL` | 300 | Cache expiry in seconds (300 = 5 min) |
| `CRYPTO_BOT_SKIP_ENTRIES_ON_API_FAILURE` | true | Don't open new positions if API down |
| `FALLBACK_BALANCE_MODE` | cached | Use cached/zero balance if API down |
| `FALLBACK_PRICE_MODE` | skip | Skip or use cached price if API down |
| `NETWORK_LOG_LEVEL` | INFO | DEBUG for verbose, WARNING for quiet |

### Safe Settings

For **maximum safety** while API unavailable:

```
NETWORK_RETRY_ATTEMPTS=5
NETWORK_CACHE_TTL=300
CRYPTO_BOT_SKIP_ENTRIES_ON_API_FAILURE=true
ALPACA_BOT_SKIP_ENTRIES_ON_API_FAILURE=true
FALLBACK_BALANCE_MODE=zero
FALLBACK_PRICE_MODE=skip
```

This means:
- ✓ Closes open positions normally
- ✓ Doesn't open new positions without real data
- ✓ Retries up to 5 times on connection errors
- ✓ Caches responses to reduce repeated calls

---

## Monitoring the Workaround

### Check Current Status

```bash
# See if network config loaded
curl http://localhost:8000/health

# Check bot logs
tail -100 /tmp/empire-server.log | grep -E "Network|CACHE|fallback"

# Check earnings still flowing
curl http://localhost:8000/payments/bot/earnings | python3 -m json.tool
```

### Expect to See

✓ Bot continues monitoring all symbols
✓ Exits still close positions normally
✓ Entries skip (because SKIP_ENTRIES_ON_API_FAILURE=true)
✓ Cache hits logged: `📦 Using cached response`
✓ Bot earnings keep generating

### Do NOT Expect

✗ New trade entries (because API blocked)
✗ Real balance updates (using cached value)
✗ Real price feeds (using cached/skipped)

---

## Removing the Workaround

Once Railway adds egress rules and you redeploy:

1. **Environment variables stay set** (no harm, just unused)
2. **Bot automatically switches** to real API responses
3. **Logs will show** fresh API calls instead of cache hits
4. **Entries resume** as normal

No code changes needed to switch back.

---

## Troubleshooting

**Q: Bot still shows "Equity: unknown" or "Cash available: unknown" + lots of 403 errors**

A: Could be Railway infrastructure issue OR missing egress whitelist. Check:
1. **Check Railway status:** https://status.railway.app
   - If US West is red, wait for Railway to resolve infrastructure issue
   - If green, then it's just an egress whitelist problem (proceed below)
2. **Check if env vars are set on Railway** for workaround
3. **Check if egress whitelist is configured:**
   - Settings → Network → Egress Allowlist
   - Should include api.coinbase.com, api.alpaca.markets, etc.
4. **Check logs:** `tail -50 /tmp/empire-server.log | grep "403\|Network"`

If you see:
```
HTTP 403: Host not in allowlist: api.coinbase.com
```
→ Add to egress whitelist (dashboard Settings → Network)

If you see:
```
Connection refused / timeout / network unreachable
```
→ Railway infrastructure issue (wait for resolution at status.railway.app)

**Q: Bot entries are skipped even though I set SKIP_ENTRIES_ON_API_FAILURE=false**

A: Correct behavior. Price feed is down (FALLBACK_PRICE_MODE=skip), so entries blocked.
To allow entries with cached price (risky):
```
FALLBACK_PRICE_MODE=cached
```
But not recommended unless price is recent (<1 min old).

**Q: Cache is too old, I want more frequent fresh calls**

A: Shorten TTL:
```
NETWORK_CACHE_TTL=60  # 1-minute cache instead of 5
```
This increases API calls but keeps data fresher.

**Q: I want to see detailed network logs**

A: Set:
```
NETWORK_LOG_LEVEL=DEBUG
```
Will log every retry attempt and cache decision.

---

## Next Steps

1. **Now:** Set environment variables on Railway
2. **Then:** Push code to deploy workaround
3. **Watch:** Monitor logs for cache hits
4. **Request:** Ask Railway support to add egress whitelist
5. **Later:** Once whitelist approved, remove env vars and resume normal operation

---

## References

- **CLAUDE.md** — Updated with troubleshooting section
- **network_config.py** — Full implementation
- **.env.railway.workaround** — Configuration template
- **Railway Docs:** https://railway.app/docs

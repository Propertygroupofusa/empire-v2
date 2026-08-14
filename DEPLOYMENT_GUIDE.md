# 🚀 BOT EARNINGS SYSTEM - DEPLOYMENT GUIDE

**Goal:** Get the bot earning real money on Railway with Stripe payouts.

---

## ✅ PREREQUISITES

Before deploying, verify you have:

1. **Stripe Account** - https://dashboard.stripe.com
   - Live secret key (sk_live_...)
   - Live publishable key (pk_live_...)
   - Stripe Connect Express account (for receiving payouts)

2. **Railway Account** - https://railway.app
   - pgusa-platform project
   - PostgreSQL database connected
   - Access to Variables/Secrets

---

## 🔧 DEPLOYMENT STEPS

### Step 1: Deploy Latest Code to Railway

```bash
# Already pushed! But verify:
git log --oneline -1
# Should show: Fix: Import Payment, Worker, Job from models...

# Push the fixed code
git push -u origin claude/usa-empire-v2-setup-01hmw8
```

**Wait for Railway to redeploy** (2-3 minutes). You'll see:
- Deployment status → Active ✅

---

### Step 2: Set Environment Variables on Railway

Go to https://railway.app → pgusa-platform → Settings → Variables

Add or update:

```
STRIPE_SECRET_KEY=sk_live_YOUR_ACTUAL_KEY_HERE
STRIPE_PUBLISHABLE_KEY=pk_live_YOUR_ACTUAL_KEY_HERE
```

**CRITICAL:** Must use LIVE keys for real money. Test keys (sk_test_) won't process real payouts.

After saving variables, Railway will auto-redeploy. **Wait for "Active" status.**

---

### Step 3: Seed Database with Test Jobs

Once deployed to Railway with working database connection:

**Option A: Via API (Recommended)**
```bash
curl -X POST https://pgusa-platform-production.up.railway.app/admin/seed-jobs \
  -H "X-Admin-Key: YOUR_ADMIN_KEY"
```

**Option B: Local Script (Then sync to Railway)**
```bash
# Run locally to create test jobs in your database
python3 seed_bot_jobs.py
```

This creates:
- 1 demo client
- 8 notarization jobs ($755 total value)
- Ready for bot to claim

---

### Step 4: Verify Bot is Running

Check the bot is active by monitoring:

**Endpoint:** https://pgusa-platform-production.up.railway.app/payments/bot/earnings

This will show:
```json
{
  "worker": "bot@pgusa.local",
  "worker_id": "...",
  "total_earned": 755.00,
  "pending_payout": 755.00,
  "paid_out": 0.00,
  "payment_count": 8,
  "payments": [...]
}
```

---

### Step 5: Monitor Real-Time Earnings

Once bot starts working, you can see live updates at:

**Dashboard:** https://pgusa-platform-production.up.railway.app/bot-earnings

This refreshes every 10 seconds and shows:
- 💰 Stripe Available Balance
- ⏳ Stripe Pending Balance  
- 📊 Total Bot Earnings
- 💼 Jobs Completed

---

## 📊 WHAT HAPPENS AUTOMATICALLY

Once deployed and jobs exist:

### Every 10 seconds:
- ✅ Bot claims 1-5 available jobs (status: requested → matched)
- ⏱️ Bot waits 2 seconds
- ✅ Bot completes jobs (status: matched → completed)
- 💰 Bot creates Payment records for each job

### Every 30 seconds:
- 💳 Bot processes pending payments
- 🔄 Creates Stripe Transfers to bot's Stripe Connect account
- 📈 Updates payment status (pending → paid)
- ✅ Money arrives in bot's Stripe balance

---

## 🔍 MONITORING CHECKLIST

After deployment, verify:

- [ ] `/payments/bot/earnings` returns 200 OK (not 404)
- [ ] `total_earned` increases every 30 seconds
- [ ] `pending_payout` decreases (moving to paid)
- [ ] Stripe account shows new transfers
- [ ] `/bot-earnings` dashboard updates in real-time

---

## 🚨 TROUBLESHOOTING

### Endpoint still returns 404
```bash
# Verify router loaded on Railway
curl https://pgusa-platform-production.up.railway.app/health
# Check Railway logs for "[OK] Router loaded: /payments"
```

### Bot not claiming jobs
```bash
# Verify jobs exist
curl https://pgusa-platform-production.up.railway.app/jobs/list
# Should show jobs with status="requested"
```

### Stripe transfers not processing
```bash
# Verify Stripe keys are LIVE (not test)
# Check Railway Variables tab - STRIPE_SECRET_KEY must start with sk_live_
# Test key (sk_test_) will create pending payments but fail on Stripe.Transfer
```

### No Stripe balance showing
```bash
# Verify bot worker has Stripe Connect account ID
# Check database: SELECT stripe_account_id FROM workers WHERE email='bot@pgusa.local'
# Should return: acct_... (not NULL)
```

---

## 💰 EXPECTED FLOW

1. **0-2 min:** Bot creates and initializes (Railway startup)
2. **2-3 min:** Bot claims 1st batch of jobs
3. **3-4 min:** 1st batch completes, payments created
4. **4-5 min:** Payouts process, Stripe balance updates
5. **Ongoing:** Every 30 seconds, pending → paid money flow

---

## 📞 NEED HELP?

If something's not working:

1. **Check Railway logs** - https://railway.app → pgusa-platform → Logs
2. **Verify environment variables** - Settings → Variables (must have STRIPE_SECRET_KEY)
3. **Test endpoints:**
   ```bash
   curl https://pgusa-platform-production.up.railway.app/payments/bot/earnings
   curl https://pgusa-platform-production.up.railway.app/health
   ```
4. **Check database connection** - Railway PostgreSQL should be "Active" status

---

## ✨ YOU'RE ALL SET!

Once deployed:
- ✅ Bot runs 24/7
- ✅ Automatically claims/completes jobs
- ✅ Processes real Stripe payouts
- ✅ Dashboard shows live earnings
- ✅ Money flows to your Stripe account

**Next:** Deploy to Railway and watch the earnings roll in! 💸

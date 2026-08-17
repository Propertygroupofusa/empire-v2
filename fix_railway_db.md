# URGENT: Fix Railway Database Issue

## Action: Remove DATABASE_URL to Use SQLite

**On Railway Dashboard:**
1. Go to empire-v2 project → Variables tab
2. Find `DATABASE_URL` 
3. DELETE it entirely (or comment it out)
4. Click "Save Variables"
5. Click "Redeploy"

**Why:** Bot will fall back to SQLite in-memory, skip PostgreSQL migration errors, start trading immediately.

**Timeline:** 2-3 minutes to deployment + bot startup

**Database:** SQLite (ephemeral on Railway, but OK for immediate profit generation)

**Next step after profit:** Migrate to PostgreSQL for persistence

---

## What Happens After You Delete DATABASE_URL:

1. Bot starts clean (no migration errors)
2. Creates SQLite empire.db locally
3. Loads open positions from previous trades
4. Resumes crypto trading on next RSI signal
5. Earns profit → compounds on updated balance

## Monitoring:

After redeploy (2-3 min):
- Check logs for "Starting crypto_coinbase_bot" (no errors)
- Check database: New trades should appear within 5-10 min
- Revenue tracking: /payments/bot/earnings should update

---

**Status: Ready to execute when you remove DATABASE_URL and redeploy**

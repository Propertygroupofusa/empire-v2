# Production Deployment Guide - Empire v2 Delivery Platform

This guide covers deploying the Empire v2 platform (video production + delivery app) to production.

## Table of Contents
1. [GitHub Setup](#github-setup)
2. [Railway Deployment](#railway-deployment)
3. [Environment Configuration](#environment-configuration)
4. [Verification Checklist](#verification-checklist)
5. [Monitoring & Troubleshooting](#monitoring--troubleshooting)

---

## GitHub Setup

### Step 1: Create Repository (if needed)

```bash
# If starting fresh, create a new repo
git init
git add .
git commit -m "Initial commit: Empire v2 delivery platform"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/empire-v2.git
git push -u origin main
```

### Step 2: Protect Main Branch (Recommended)

1. Go to GitHub → empire-v2 → Settings → Branches
2. Add branch protection rule for `main`:
   - Require status checks before merging
   - Require code reviews before merging
   - Dismiss stale pull requests

### Step 3: Set Up GitHub Secrets

For CI/CD to work, add these secrets:
1. Go to Settings → Secrets and variables → Actions
2. Add these secrets:
   - `STRIPE_SECRET_KEY` (from Stripe dashboard)
   - `STRIPE_PUBLISHABLE_KEY`
   - `STRIPE_WEBHOOK_SECRET`
   - `HEYGAN_API_KEY` (from HeyGen)
   - `ANTHROPIC_API_KEY` (from Anthropic)

### Step 4: Push Development Branch

```bash
# Your current branch is already set up
git push -u origin claude/custom-delivery-app-di39ur

# This will appear as a PR candidate on GitHub
# Click "Compare & pull request" to create the PR
```

---

## Railway Deployment

### Step 1: Connect GitHub to Railway

1. Go to [railway.app](https://railway.app)
2. Click "New Project" → "Deploy from GitHub"
3. Authorize Railway with GitHub
4. Select `Propertygroupofusa/empire-v2` repository
5. Railway will auto-detect `railway.json` configuration

### Step 2: Configure Environment Variables

In Railway dashboard → Project Settings → Variables:

**Required for Delivery App:**
```
STRIPE_SECRET_KEY=sk_live_...
STRIPE_PUBLISHABLE_KEY=pk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...
HEYGAN_API_KEY=...
ANTHROPIC_API_KEY=sk-ant-...
PORT=10000
```

**Optional (for existing features):**
```
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
YOUTUBE_API_KEY=...
```

### Step 3: Configure Services

Railway will auto-create services from `railway.json`:

1. **main-app** (FastAPI - port 10000)
   - Start command: `python main.py`
   - Health check: GET `/health`

2. **crypto-trading-bot** (optional)
   - Start command: `python bot_2_crypto_scalper.py`

3. **pl-tracker** (optional)
   - Start command: `python bot_pl_tracker.py`

### Step 4: Set Up Custom Domain

1. Railway Project → Settings → Domains
2. Add custom domain (e.g., `api.deliveryapp.com`)
3. Point DNS records to Railway (follow Railway's instructions)

### Step 5: Deploy

```bash
# Simply push to the branch - Railway auto-deploys
git push origin claude/custom-delivery-app-di39ur

# Railway will:
# 1. Detect push
# 2. Build Docker image
# 3. Run tests via GitHub Actions
# 4. Deploy to production
# 5. Verify health checks

# Check deployment status:
# Railway Dashboard → Project → Deployments tab
```

---

## Environment Configuration

### Local Development

```bash
# Create .env from template
cp .env.example .env

# Edit .env with your local values
# For local testing, use Stripe test keys (starts with sk_test_)
STRIPE_SECRET_KEY=sk_test_...
STRIPE_PUBLISHABLE_KEY=pk_test_...

# Run locally
python main.py
# Visit http://localhost:8000/delivery/restaurants
```

### Production on Railway

Use Railway's Variables panel (no .env file needed):

**Stripe Production Keys** (starts with `sk_live_`, `pk_live_`)
- Get from [Stripe Dashboard](https://dashboard.stripe.com)

**HeyGen API Key**
- Get from [HeyGen Developer Portal](https://www.heygen.com/api)

**Other Required Keys**
- ANTHROPIC_API_KEY from [Anthropic Console](https://console.anthropic.com)
- HEYGAN_API_KEY (video generation)

### Webhook Configuration

#### Stripe Webhook
1. Stripe Dashboard → Developers → Webhooks
2. Add endpoint: `https://your-domain/orders/webhook/stripe`
3. Select events:
   - `payment_intent.succeeded`
   - `checkout.session.completed`
4. Copy webhook secret → add to Railway Variables as `STRIPE_WEBHOOK_SECRET`

---

## Verification Checklist

Before considering deployment complete:

### Pre-Deployment
- [ ] All tests pass: `python -m pytest tests/` (if tests exist)
- [ ] Code lints: `flake8 main.py routers/`
- [ ] Imports work: `python -c "import main; print('OK')"`
- [ ] `.env` is in `.gitignore` (don't commit secrets)
- [ ] Dockerfile builds: `docker build -t empire-v2 .`

### Post-Deployment
- [ ] Health endpoint responds: `curl https://your-domain/health`
- [ ] API docs available: `https://your-domain/docs`
- [ ] Delivery endpoints work: `curl https://your-domain/delivery/restaurants`
- [ ] Stripe webhook registered and responding
- [ ] WebSocket connections work (test in browser console)
- [ ] Database schema created (first deploy auto-runs migration if using SQLAlchemy)

### Smoke Test
```bash
# Test from local machine
curl https://your-domain/health
# Expected: {"status": "ok"}

curl https://your-domain/delivery/restaurants
# Expected: JSON array of restaurants (or empty if not seeded)

curl -X POST https://your-domain/orders/webhook/stripe \
  -H "Content-Type: application/json" \
  -d '{"type":"payment_intent.succeeded"}'
# Expected: 401 or signature error (webhook validation works)
```

---

## Monitoring & Troubleshooting

### View Logs

**Railway Dashboard:**
1. Project → Deployments → Select deployment
2. Click "View Logs" button
3. Real-time logs appear

**Command line:**
```bash
# If using Railway CLI
railway logs -f
```

### Common Issues

**"ModuleNotFoundError: No module named 'delivery'"**
- Solution: Make sure `routers/delivery.py` exists and is committed
- Check: `git status` and `git log` to verify

**"WebSocket connection refused"**
- Railway uses ephemeral containers - WebSocket connections won't persist across redeploys
- Solution: Implement connection recovery in frontend (reconnect on disconnect)

**"Stripe webhook not working"**
- Verify webhook URL is correct: `https://your-domain/orders/webhook/stripe`
- Check webhook secret matches: `STRIPE_WEBHOOK_SECRET` in Railway
- Test webhook: Stripe Dashboard → Developers → Webhooks → Send test event

**"Database locked (sqlite)"**
- SQLite doesn't support concurrent writes well at scale
- Solution: Migrate to PostgreSQL for production
- Add connection string: `DATABASE_URL=postgresql://user:password@host/empire_v2`

### Performance Monitoring

**Check Railway resource usage:**
1. Project → Settings → Metrics
2. Monitor: CPU, Memory, Network
3. Scale up if consistently >80% usage

**Database size:**
```bash
# Check local database size
ls -lh empire.db

# Clean old orders (optional)
# Add cleanup task in health_monitor.py or schedule with APScheduler
```

---

## Rollback Procedure

If a deployment breaks production:

### Via Railway Dashboard
1. Deployments tab → Find previous working deployment
2. Click "Redeploy" button on previous version
3. Service restarts with old code
4. Estimated downtime: 30-60 seconds

### Via Git
```bash
# Revert last commit
git revert HEAD
git push origin claude/custom-delivery-app-di39ur

# Railway auto-detects and redeploys
```

---

## Scaling Considerations

### Single Server (Current)
- Works for: ~100 concurrent users, <10 deliveries/sec
- Limited by: FastAPI single process, SQLite concurrent writes

### Horizontal Scaling (Future)
1. Add PostgreSQL database
2. Use gunicorn with multiple workers
3. Add Redis for WebSocket pub/sub
4. Deploy to Railway with "advanced" config (multiple instances)

### Database Migration (SQLite → PostgreSQL)
```bash
# Install PostgreSQL client library
pip install psycopg2-binary

# Update DATABASE_URL in Railway Variables:
DATABASE_URL=postgresql://user:password@postgres.railway.internal:5432/empire_v2

# Run migration (SQLAlchemy auto-creates tables)
python -c "from database import init_db; init_db()"
```

---

## References

- **Railway Docs:** https://docs.railway.app
- **GitHub Actions:** https://docs.github.com/en/actions
- **Stripe Webhooks:** https://stripe.com/docs/webhooks
- **FastAPI Deployment:** https://fastapi.tiangolo.com/deployment/
- **Docker Best Practices:** https://docs.docker.com/develop/dev-best-practices/

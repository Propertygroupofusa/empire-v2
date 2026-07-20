# Production Setup - Empire v2 Delivery Platform

Quick reference for getting your delivery app live.

## What You Have

- ✅ **FastAPI backend** with delivery system (restaurants, orders, real-time tracking)
- ✅ **Docker setup** ready for containerized deployment
- ✅ **Railway configuration** for auto-deployment on git push
- ✅ **GitHub Actions CI/CD** for automatic testing
- ✅ **Stripe integration** for payment processing

## What You Need

### GitHub Account
- Repository: https://github.com/Propertygroupofusa/empire-v2
- Branch: `claude/custom-delivery-app-di39ur`

### Railway Account
- Free tier available (5GB/month bandwidth)
- Sign up: https://railway.app
- Connect your GitHub account

### Stripe Account
- Development keys (for testing)
- Live keys (for production)
- Get from: https://dashboard.stripe.com

## 60-Second Deploy

### 1️⃣ Prepare Code
```bash
cd /home/user/empire-v2

# Verify everything is committed
git status

# Should see: "nothing to commit, working tree clean"
```

### 2️⃣ Push to GitHub
```bash
git push origin claude/custom-delivery-app-di39ur
```

### 3️⃣ Connect Railway
1. Go to https://railway.app
2. Click "New Project" → "Deploy from GitHub repo"
3. Select `Propertygroupofusa/empire-v2`
4. Railway auto-reads `railway.json` and deploys

### 4️⃣ Add Secrets
In Railway dashboard → Project Settings → Variables, add:

```
STRIPE_SECRET_KEY=sk_live_YOUR_KEY_HERE
STRIPE_PUBLISHABLE_KEY=pk_live_YOUR_KEY_HERE
STRIPE_WEBHOOK_SECRET=whsec_YOUR_KEY_HERE
HEYGAN_API_KEY=YOUR_KEY_HERE
ANTHROPIC_API_KEY=sk-ant-YOUR_KEY_HERE
```

### 5️⃣ Get Production URL
- Railway dashboard → main-app service
- Copy domain: `https://empire-v2-xyz.railway.app`
- This is your live API URL

### 6️⃣ Test It
```bash
# Test health
curl https://empire-v2-xyz.railway.app/health

# Test delivery API
curl https://empire-v2-xyz.railway.app/delivery/restaurants

# Should return JSON with restaurants (if seeded)
```

## Detailed Setup (If Needed)

### Connect Custom Domain
Railway → Project → Settings → Domains → Add Domain
- Set DNS records to Railway's nameservers
- Takes ~5-10 minutes for DNS propagation

### Set Up Stripe Webhooks
1. Stripe Dashboard → Developers → Webhooks
2. Add endpoint: `https://your-domain/orders/webhook/stripe`
3. Copy signing secret → add to Railway as `STRIPE_WEBHOOK_SECRET`

### Monitor Deployments
1. Railway → Deployments tab
2. View logs: Click deployment → View Logs
3. See real-time activity

## Troubleshooting

### "Deployment failed"
Check logs: Railway → Deployments → Click failed deployment → View Logs

Common causes:
- Missing environment variable → Add to Railway Variables
- Syntax error in code → Check logs for error line
- Port conflict → Make sure PORT is set to 10000

### "WebSocket connection refused"
- WebSocket works only on Railway domains (not localhost)
- Test with browser DevTools: `new WebSocket('wss://your-domain/delivery/ws/track/123')`

### "Stripe webhook not responding"
- Verify webhook URL: `https://your-domain/orders/webhook/stripe`
- Check Railway logs for 400 errors
- Test webhook: Stripe Dashboard → Webhooks → Send test event

## What's Next

### After Going Live
1. Set up monitoring: Railway dashboard → Metrics
2. Enable auto-scaling: Project → Settings → Restart policies
3. Back up database regularly (if using PostgreSQL on Railway)
4. Monitor Stripe webhooks: Stripe Dashboard → Webhooks → Event logs

### Future Improvements
- Add PostgreSQL for scale (currently SQLite)
- Add Redis for WebSocket pub/sub
- Set up error tracking (e.g., Sentry)
- Add customer support chat (e.g., Intercom)

## File Reference

| File | Purpose |
|------|---------|
| `main.py` | FastAPI app entry point |
| `routers/delivery.py` | Delivery API endpoints |
| `Dockerfile` | Container configuration |
| `railway.json` | Railway deployment config |
| `.github/workflows/deploy.yml` | CI/CD pipeline |
| `DEPLOYMENT_GUIDE.md` | Full deployment documentation |
| `DEPLOY_STEPS.sh` | Quick deploy script |

## Support

- **FastAPI docs**: http://your-domain/docs (interactive API playground)
- **Railway support**: https://help.railway.app
- **Stripe docs**: https://stripe.com/docs
- **GitHub Actions**: https://docs.github.com/en/actions

---

**You're ready to deploy! Push to GitHub and Railway will handle the rest.** 🚀

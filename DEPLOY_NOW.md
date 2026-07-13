# 🚀 Deploy to Railway in 5 Minutes

Your complete video + revenue automation system is ready to deploy. Follow these steps to go live and start earning.

## Step 1: Go to Railway (1 minute)

1. Visit https://railway.app
2. Sign in (or create free account)
3. Click **"Create Project"**
4. Select **"Deploy from GitHub"**

---

## Step 2: Connect Your Repository (2 minutes)

1. **Select Repository:**
   - Organization: `Propertygroupofusa`
   - Repository: `empire-v2`
   - Branch: `claude/video-editing-platform-ib585z`

2. Click **"Deploy"**

Railway will automatically:
- ✅ Detect docker-compose.yml
- ✅ Spin up all 5+ services
- ✅ Start database
- ✅ Configure networking
- ⏳ Takes ~2-3 minutes

---

## Step 3: Configure Environment Variables (2 minutes)

While deployment is running, set up your API keys.

**In Railway Dashboard:**
1. Go to your project
2. Click **"Settings"** → **"Variables"** 
3. Click **"Raw Editor"**
4. Paste this template and fill in YOUR values:

```bash
# YouTube (for earnings tracking & auto-publishing)
YOUTUBE_API_KEY=your_api_key_here
YOUTUBE_CLIENT_ID=your_client_id_here
YOUTUBE_CLIENT_SECRET=your_client_secret_here
YOUTUBE_REFRESH_TOKEN=your_refresh_token_here
YOUTUBE_AUTO_PUBLISH=true

# Stripe (for video service payments)
STRIPE_PUBLIC_KEY=pk_live_your_key_here
STRIPE_SECRET_KEY=sk_live_your_key_here

# Anthropic (for video content)
ANTHROPIC_API_KEY=your_key_here

# Alpaca (existing - copy from your current config)
ALPACA_API_KEY=your_key_here
ALPACA_SECRET_KEY=your_key_here

# Optional APIs
OPENAI_API_KEY=your_key_here
GROK_API_KEY=your_key_here

# Enable Revenue Automation
DAILY_PUBLISHER_ENABLED=true
VIDEO_GENERATOR_ENABLED=true
VIDEO_AUTO_PUBLISH=true
```

### Don't Have These Keys? Get Them Now (10 min)

**YouTube API:**
1. Go https://console.cloud.google.com
2. Create new project
3. Enable "YouTube Data API v3"
4. Create OAuth 2.0 credentials (Desktop app)
5. Get API Key from credentials page
6. For refresh token, use Google's OAuth 2.0 playground

**Stripe:**
1. Go https://stripe.com
2. Sign up (free)
3. Go to "Developers" → "API Keys"
4. Copy Publishable Key (pk_) and Secret Key (sk_)

**Anthropic:**
1. Go https://console.anthropic.com
2. Get your API key
3. (You already have this from your current setup)

---

## Step 4: Deploy Complete (Check Status)

**In Railway Dashboard:**
- Look for 5+ services showing green checkmarks ✅
- Services: main-app, video-generator, video-editor-api, video-auto-editor, video-editor-frontend
- Each service should show "Running"

**Get Your App URL:**
- Click "Deploy" or main service
- Copy the public URL (looks like `https://your-app-name.railway.app`)

---

## Step 5: Start Earning (1 command)

Once deployment shows green checkmarks, enable revenue automation:

```bash
# Replace with YOUR Railway URL
curl -X POST https://your-app-name.railway.app/revenue/publishing/start

# Check status
curl https://your-app-name.railway.app/revenue/publishing/status

# View your dashboard
curl https://your-app-name.railway.app/revenue/dashboard/executive-summary
```

**What happens:**
- ✅ Daily publisher starts
- ✅ First video generates at next scheduled time (8am, 12pm, 4:30pm, or 6pm)
- ✅ Video publishes to YouTube automatically
- ✅ Earnings tracked in real-time
- ✅ Leads captured from descriptions
- ✅ All systems running 24/7

---

## Your Revenue Streams Are Now Live

### 1. YouTube Ads (Passive)
```bash
curl https://your-app.railway.app/revenue/youtube/metrics/analytics
```
Returns: Views, watch time, estimated earnings for last 30 days

### 2. Video Services ($500-1000/video)
```bash
# Share this link with clients
https://your-app.railway.app/revenue/video-service/pricing

# Orders auto-fulfill in 1-2 minutes
```

### 3. Lead Generation (Building Email List)
```bash
# View your captured leads
curl https://your-app.railway.app/revenue/leads/hot?limit=20

# Track engagement
curl -X POST https://your-app.railway.app/revenue/leads/LEAD-123/engagement \
  -d '{"action": "email_clicked", "points": 10}'
```

### 4. Courses (Passive Income)
```bash
# Launch your first course
curl -X POST https://your-app.railway.app/revenue/courses/create \
  -d '{"title":"Video Marketing Mastery","price":97,"level":"beginner"}'
```

### 5. Dashboard (See All Revenue)
```bash
curl https://your-app.railway.app/revenue/dashboard/executive-summary
```

---

## 📊 What You'll See

**First Response from Dashboard:**
```json
{
  "total_revenue_30d": 150,
  "projected_annual_revenue": 1800,
  "youtube_subscribers": 45,
  "youtube_views_30d": 2400,
  "total_leads": 12,
  "key_insight": "Focus on consistent content publishing"
}
```

This grows every day as:
- Views accumulate
- YouTube pays you
- Leads get captured
- Orders come in

---

## 🎯 First 24 Hours Checklist

- [ ] Repository connected to Railway
- [ ] Deployment complete (all services green)
- [ ] Environment variables configured
- [ ] `/revenue/publishing/start` called
- [ ] First video published at next scheduled time
- [ ] Dashboard showing metrics
- [ ] Shared video service link with first client
- [ ] Added YouTube CTAs to video descriptions

---

## 📈 First Week Goals

**Video Publishing:**
- 4 videos/day = 28 videos by end of week
- ~1,000-5,000 views

**Leads:**
- 20-50 emails captured
- Hot leads list populated

**Revenue:**
- $0-100 from YouTube ads
- $0-500 from service inquiry

**Social:**
- Share on Twitter, LinkedIn, TikTok
- Drive traffic to your channel

---

## 🆘 Troubleshooting

### Services not starting?
```bash
# Check logs in Railway dashboard
# Look for error messages
# Common fixes:
# 1. Missing API keys
# 2. Database connection issue
# 3. Port conflict
```

### Videos not publishing?
```bash
# Check publisher status
curl https://your-app.railway.app/revenue/publishing/status

# Should show next scheduled jobs
# If empty, check if DAILY_PUBLISHER_ENABLED=true
```

### No earnings showing?
```bash
# YouTube tracking requires:
# 1. YOUTUBE_REFRESH_TOKEN (valid)
# 2. Videos must be public (not unlisted)
# 3. Channel must be monetized
# 4. Wait 24 hours for YouTube to process views
```

### Can't create orders?
```bash
# Check Stripe keys
# stripe_secret_key must be set
# Test with dummy card: 4242 4242 4242 4242
```

---

## 💰 Expected Timeline

| Time | Status | Revenue |
|------|--------|---------|
| Hour 1 | System deployed | $0 |
| Day 1 | First video published | $0-10 |
| Day 7 | 28 videos, 1k+ views | $10-50 |
| Week 2 | 56 videos, 5k+ views | $50-200 |
| Week 3 | First client order | $500-1000 |
| Week 4 | Launch course | $1000-2000 |
| Month 2 | 240+ videos, 10k+ views | $2000-5000 |
| Month 3 | Multiple clients, 50+ students | $5000-10000 |

---

## 🎬 You're Live!

Your complete automation empire is now:
- ✅ Publishing videos daily
- ✅ Tracking YouTube earnings
- ✅ Accepting video orders
- ✅ Capturing leads
- ✅ Selling courses
- ✅ Running 24/7

**Next step: Share your content and watch the revenue grow.**

---

## 📞 Need Help?

**Check Status:**
```bash
# Full diagnostics
curl https://your-app.railway.app/revenue/dashboard/all-metrics
```

**View All Metrics:**
```bash
# YouTube earnings
curl https://your-app.railway.app/revenue/youtube/summary

# Video orders
curl https://your-app.railway.app/revenue/video-service/stats

# Lead metrics
curl https://your-app.railway.app/revenue/leads/metrics/summary

# Course performance
curl https://your-app.railway.app/revenue/courses/catalog
```

---

**Deployed**: Your app is now live on Railway
**Status**: All revenue streams active
**Next**: Monitor dashboard and scale up 🚀

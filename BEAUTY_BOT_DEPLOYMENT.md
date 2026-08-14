# BEAUTY SUPPORT BOT - DEPLOYMENT TO RAILWAY (30 MINUTES)

## WHAT'S ALREADY DONE

✅ Code written (beauty_support_bot.py)
✅ Database schema ready
✅ Claude API integration ready
✅ Zapier webhooks configured

**What's left:** Deploy to Railway so it's live

---

## STEP 1: ADD DEPENDENCIES

Update `requirements.txt` to include:

```
fastapi==0.104.1
uvicorn==0.24.0
sqlalchemy==2.0.23
aiosqlite==0.19.0
anthropic==0.7.1
pydantic==2.5.0
python-multipart==0.0.6
```

Run:
```bash
pip install -r requirements.txt
```

---

## STEP 2: CREATE ENVIRONMENT VARIABLES

In Railway dashboard, add:

```
ANTHROPIC_API_KEY=sk-ant-xxxxx
DATABASE_URL=postgresql://user:password@db-host/dbname
WEBHOOK_TOKEN_tower28=dev-token-123
WEBHOOK_TOKEN_merit=dev-token-456
```

(Generate webhook tokens - can be anything, store securely)

---

## STEP 3: DEPLOY TO RAILWAY

### Option A: Via Git Push (Recommended)

1. **Commit the bot code:**
```bash
git add beauty_support_bot.py BEAUTY_* ZAPIER_*
git commit -m "Add beauty support bot AI system - handles customer emails with Claude"
```

2. **Push to main:**
```bash
git push -u origin main
```

3. **Railway auto-deploys** (watch logs for "Active" status)

### Option B: Via Railway CLI

```bash
railway login
railway up
```

---

## STEP 4: VERIFY DEPLOYMENT

Once Railway shows "Active":

```bash
# Test health check
curl https://empire-v2-production.up.railway.app/health

# Should return:
{"status": "ok", "service": "beauty_support_bot"}
```

---

## STEP 5: TEST WITH WEBHOOK

**Send test request to bot:**

```bash
curl -X POST https://empire-v2-production.up.railway.app/handle-support-email \
  -H "Content-Type: application/json" \
  -d '{
    "brand_id": "tower28",
    "customer_email": "customer@example.com",
    "customer_name": "Sarah M.",
    "subject": "Product fit question",
    "body": "Hi, does this foundation work for sensitive skin?",
    "webhook_token": "dev-token-123"
  }'
```

**Expected response:**
```json
{
  "status": "success",
  "ticket_id": "abc123de",
  "customer": "Sarah M.",
  "response_generated": true,
  "confidence": 85,
  "will_escalate": false
}
```

✅ Bot is working!

---

## STEP 6: SET UP CUSTOMER ACCOUNT

For each new customer:

1. **Create brand record in database:**
```python
# In Railway dashboard or via API
customer = BrandAccount(
    brand_id="tower28",
    brand_name="Tower 28 Beauty",
    monthly_fee=120000,  # $1,200 in cents
    support_email="support@tower28beauty.com",
    webhook_token="dev-token-123"
)
```

2. **Add to Railway env vars:**
```
WEBHOOK_TOKEN_tower28=dev-token-123
```

3. **Give them dashboard URL:**
```
https://empire-v2-production.up.railway.app/dashboard/tower28
```

---

## STEP 7: ZAPIER INTEGRATION

In Zapier, set webhook URL:
```
https://empire-v2-production.up.railway.app/handle-support-email
```

With payload:
```json
{
  "brand_id": "tower28",
  "customer_email": "{{ from_email }}",
  "customer_name": "{{ from_name }}",
  "subject": "{{ subject }}",
  "body": "{{ plain_text_body }}",
  "webhook_token": "dev-token-123"
}
```

---

## STEP 8: MONITOR IN PRODUCTION

**Check logs:**
```bash
railway logs
```

**Look for:**
- ✅ "Support email received"
- ✅ "AI response generated"
- ✅ "Ticket stored in database"
- ❌ Any error messages

---

## STEP 9: DATABASE SETUP

First time, initialize tables:

```bash
# SSH into Railway
railway shell

# Run Python init
python3 << 'EOF'
import asyncio
from beauty_support_bot import engine, Base

async def init():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("✅ Tables created")

asyncio.run(init())
EOF
```

---

## QUICK START CHECKLIST

- [ ] `beauty_support_bot.py` in repo
- [ ] Requirements updated
- [ ] Environment variables set in Railway
- [ ] Code committed and pushed
- [ ] Railway shows "Active" status
- [ ] Health check passes (curl /health)
- [ ] Test webhook fires successfully
- [ ] Database tables created
- [ ] Customer account added
- [ ] Zapier connected
- [ ] Dashboard accessible

---

## PRODUCTION READINESS

Before customer goes live:

✅ **Monitoring**
- Set up error alerts
- Watch support ticket volume
- Monitor API costs

✅ **Scaling**
- Auto-scaling enabled (Railway default)
- Database connections pooled
- Caching enabled

✅ **Security**
- Webhook tokens validated
- HTTPS only
- Rate limiting (if needed)

✅ **Reliability**
- Backups enabled (Railway)
- Error escalation working
- Logs accessible

---

## COSTS (Very low)

**Claude API:** ~$0.003 per email (1000 emails = $3)
**Railway:** $7/month base (included with main app)
**Zapier:** Free tier handles 100+ zaps/month

**For customer paying $1,200:**
- Your cost: ~$50/month
- Your profit: $1,150/month

---

## NEXT STEPS

1. ✅ Deploy bot to Railway
2. ✅ Send pitch emails to 20 beauty stores
3. ✅ First customer responds (6-24 hours)
4. ✅ Onboard + integrate (30 min)
5. ✅ System handling tickets (live)
6. ✅ Get first payment ($1,200)
7. ✅ Repeat with 3-5 more customers

**Timeline:** 48 hours to first revenue

Let's go. 💪

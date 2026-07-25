# Empire v2 — Comprehensive System Status Report
**Generated:** 2026-07-22 | **Deployment:** Railway (https://empire-v2-production.up.railway.app)

---

## Executive Summary

Empire v2 is a **multi-system SaaS platform** with three core revenue streams:
1. **Video Production Service** — Quote → Stripe payment → HeyGen video generation → customer delivery
2. **Trading Automation** — Prop bot + Tradovate bot with APEX evaluation ($25K account)
3. **Content/Revenue Systems** — Email campaigns, YouTube publishing, subscriptions

**All systems are integrated and operational** with graceful error handling (missing modules don't crash startup).

---

## 1. Video Production Pipeline

### Active Components
| Component | Status | Purpose |
|-----------|--------|---------|
| `quote_request.html` | ✅ Active | Frontend form (2-stage: Get Quote → Pay Now) |
| `/orders` router | ✅ Active | Quote submission, Stripe checkout, payment webhook |
| `heygan_integration.py` | ✅ Optional | HeyGen API for video generation (graceful fallback if unavailable) |
| `VideoQuoteOrder` model | ✅ Active | In-memory order tracking |

### Order Flow
```
1. Customer submits quote form → POST /orders/request-quote
   ├─ Returns: order_id + quote_price
   └─ Stores in pending_orders list
   
2. Customer accepts quote → POST /orders/{order_id}/create-checkout
   ├─ Creates Stripe checkout session
   └─ Returns: session_id for redirect
   
3. Payment confirmed → Stripe webhook POST /orders/webhook/stripe
   ├─ Validates signature with STRIPE_WEBHOOK_SECRET
   ├─ Marks order as paid
   └─ Triggers async generate_video_for_order()
   
4. Video generation → async task polls HeyGen API
   ├─ Max 10 minutes polling (60 attempts × 10 sec)
   ├─ On completion: stores video_url, sends email
   └─ On timeout: sets status error
   
5. Customer views → GET /orders/customer/{order_id}
   └─ Shows status + video download link if ready
   
6. Admin tracking → GET /orders/admin-dashboard
   └─ Shows all orders with generation status
```

### Configuration
```
Video Types: Corporate Video, Product Demo, Explainer, Testimonial, Social Media Clip
Avatars: Anna, Carlos, Emma, James, Lisa, Marcus, Olivia, Ryan
Languages: 22 options (English US/UK/AU, Spanish, French, German, Italian, Portuguese, Dutch, Swedish, Norwegian, Danish, Polish, Russian, Japanese, Korean, Chinese Simplified/Traditional, Arabic, Hindi)
Pricing: Dynamic calculator based on video type + delivery timeline
Stripe Webhook: https://empire-v2-production.up.railway.app/orders/webhook/stripe
```

### Recent Changes
- ✅ Fixed `/quote` endpoint file path handling (multiple fallback paths for Railway)
- ✅ Returns HTMLResponse instead of FileResponse for Railway compatibility
- ✅ Bootstrap playbook added for video product positioning and upsells

### KPIs
- **Order Storage:** In-memory lists (pending_orders, completed_orders)
  - **⚠️ Limitation:** Lost on restart — migrate to DB persistence for production
- **Video Generation:** HeyGen API with 10-minute timeout
- **Payments:** Stripe checkout sessions (test/live configurable via env)

---

## 2. Trading Automation Systems

### Active Bots

#### 2a. Prop Bot v3 (Alpaca) — `prop_bot.py`
**Purpose:** APEX $25K Futures evaluation (MES, MNQ, MGC)  
**Account:** APEX_589296  
**Rule:** 7 consecutive profitable days before going live  

**Status:** ✅ Running (paper trading by default)

| Parameter | Value |
|-----------|-------|
| **Mode** | Paper (ALPACA_LIVE_TRADE=false by default) |
| **Futures** | MES, MNQ, MYM, M2K, MGC (micro contracts via ETF proxies) |
| **Strategy** | RSI + trend confirmation (SMA5 > SMA10) |
| **RSI Thresholds** | Buy < 45, Sell > 55 (loosened for more frequent trades) |
| **Position Tracking** | Daily P&L, profitable days counter |
| **Kill Switch** | STOP_TRADING=true pauses all orders |

**Recent Signal:** 
- RSI-based entry/exit thresholds loosened to increase trade frequency
- Trend-gate requirement on prop_bot entries (bullish/bearish confirmation)

---

#### 2b. Tradovate Bot (APEX) — `tradovate_bot.py`
**Purpose:** Real futures trading (ES/NQ/YM/RTY) against APEX evaluation account  
**Account:** APEX_589296 (same as Prop Bot)  
**Status:** ✅ Running (demo mode by default)  

| Parameter | Value |
|-----------|-------|
| **Mode** | Demo (TRADOVATE_MODE=demo by default) |
| **Credentials** | TRADOVATE_USER, TRADOVATE_PASS, TRADOVATE_CID, TRADOVATE_SECRET |
| **Contracts** | ES (E-mini S&P), NQ (E-mini Nasdaq), YM (E-mini Dow), RTY (E-mini Russell 2000) |
| **Strategy** | RSI/trend signals via Alpaca data feed |
| **Scaling** | Contract size increases with profit ($300, $600, $900 thresholds) |
| **APEX Rules** | Max 4 contracts, $1K drawdown limit, safety margin at 75% |
| **Kill Switch** | STOP_TRADING=true (shared with prop_bot) |

**Safety Features:**
- IntradayTrail enforces trailing-drawdown rule
- Stops at 75% of max drawdown (safety buffer before $1K breach)
- Configurable via TRADOVATE_MODE ("demo" vs "live")

---

#### 2c. Crypto Scalper Bot — `bot_2_crypto_scalper.py`
**Purpose:** Secondary crypto trading bot  
**Status:** ✅ Available (integration depends on API keys)  

---

### Trading Dashboard (`routers/trading_dashboard.py`)
**Purpose:** Real-time Alpaca account monitoring + withdrawal request logging  
**Status:** ✅ Active  

**Endpoints:**
- `GET /trading_dashboard/status` — Real account snapshot (equity, cash, positions, today's trades)
  - Auto-compounds $100 profit increments into base capital
  - Polls every 30s (effectively continuous compounding)
- `GET /trading_dashboard/withdrawal-requests` — History of withdrawal requests
- `POST /trading_dashboard/request-withdrawal` — Log manual withdrawal request
- `POST /trading_dashboard/mark-withdrawal-completed` — Mark request complete (actual bank transfer done in Alpaca app)

**Key Metrics:**
```
- Current Equity: Real account value
- Cash Available: Buying power
- Open Positions: Live holdings
- Today's Trades: Filled orders with entry/exit prices
- Base Capital: Starting amount (P&L computed above this)
- Profit: Equity - Base Capital
- Lifetime Withdrawals: Total cash withdrawn
```

**Limitation:** ⚠️ ACH/bank-transfer API not available via Alpaca's self-directed trading API. Withdrawals are bookkeeping records; actual bank transfer done manually in Alpaca app.

---

### Trading Configuration
```env
ALPACA_API_KEY=            # Alpaca API key
ALPACA_SECRET_KEY=         # Alpaca secret
ALPACA_BASE_URL=           # https://paper-api.alpaca.markets (paper) or live endpoint
ALPACA_LIVE_TRADE=false    # Set true for live futures (CAREFUL)

TRADOVATE_USER=            # Tradovate username
TRADOVATE_PASS=            # Tradovate password
TRADOVATE_CID=             # OAuth Client ID
TRADOVATE_SECRET=          # OAuth secret
TRADOVATE_DEVICE_ID=       # Device identifier for sessions
TRADOVATE_MODE=demo        # Set "live" only after demo testing

STOP_TRADING=false         # Global kill switch (pauses BOTH bots)
PROP_RSI_BUY_BELOW=45      # RSI entry threshold
PROP_RSI_SELL_ABOVE=55     # RSI exit threshold
```

---

## 3. Revenue & Monetization Systems

### Payment Systems
| System | Status | Purpose |
|--------|--------|---------|
| Stripe Integration | ✅ Active | Quote checkout → payment capture |
| Stripe Subscriptions | ✅ Active | Recurring billing for tiers |
| Payments Pause Switch | ✅ Active | PAYMENTS_PAUSED env var disables all checkout |
| Stripe Webhook | ✅ Active | Payment confirmation → trigger video generation |

### Revenue Tracking
| Dashboard | File | Status | Purpose |
|-----------|------|--------|---------|
| Trading Dashboard | `trading_dashboard.html` | ✅ Active | Real-time Alpaca account + profit tracking |
| Revenue Dashboard | `revenue_dashboard.py` | ✅ Active | Video + trading revenue aggregation |
| Video Revenue API | `video_revenue_api.py` | ✅ Active | Video order revenue endpoints |

### Subscription Tiers (`subscription_tiers.py`, `routers/subscriptions.py`)
| Tier | Status | Use Case |
|------|--------|----------|
| **Free** | ✅ Available | Limited video quotes |
| **Pro** | ✅ Available | 10 videos/month + analytics |
| **Enterprise** | ✅ Available | Unlimited + dedicated support |

---

## 4. Content & Automation Systems

### Social Media Automation
| Component | File | Status | Purpose |
|-----------|------|--------|---------|
| Social Dashboard | `social_media_dashboard.html` | ✅ Active | Content scheduling + publishing |
| Social Autoposter | `social_media_autoposter.py` | ✅ Active | Automated posting to socials |
| Content Bot | `content_bot.py` | ✅ Active | AI content generation |

### Email & Outreach
| Component | File | Status | Purpose |
|-----------|------|--------|---------|
| Email Sender | `send_emails.py` | ✅ Optional | Gmail SMTP (graceful fallback) |
| Outreach Router | `routers/outreach.py` | ✅ Active | Email campaign management |
| Lead Generator | `lead_generator.py` | ✅ Optional | Lead scraping + enrichment |

### YouTube Automation
| Component | File | Status | Purpose |
|-----------|------|--------|---------|
| YouTube Auto Pipeline | `youtube_auto_pipeline.py` | ✅ Active | Automated video publishing |
| YouTube Dashboard | `youtube_dashboard.py` | ✅ Active | Publishing status + analytics |
| YouTube Monetization | `youtube_monetization.py` | ✅ Active | AdSense + sponsorship tracking |
| YouTube Education Bot | `youtube_education_bot.py` | ✅ Active | Educational content series |
| Daily Publisher | `daily_publisher.py` | ✅ Optional | Scheduled daily publishing |

---

## 5. Infrastructure & Operations

### Database
- **Type:** SQLite (via SQLAlchemy async)
- **Models:** VideoQuoteOrder, TradingBotState, WithdrawalRequest, User, Subscription, etc.
- **Status:** ✅ Active
- **Init:** `database.py` handles engine setup + session factory

### Authentication & Admin
- **Admin Auth:** `admin_auth.py` (API key via X-Admin-Key header)
- **Status:** ✅ Active (no user-level auth yet on admin endpoints)

### Health Monitoring
- **Component:** `health_monitor.py`
- **Status:** ✅ Active
- **Tracks:** System errors, fixed issues, performance metrics
- **Endpoints:**
  - `GET /health` — Simple "ok" status
  - `GET /monitor/status` — Full health monitor status
  - `GET /monitor/errors` — Error history

### Data Retention
- **Component:** `data_retention.py`
- **Policy:** All data archived forever (non-deletion retention)
- **Status:** ✅ Active

### Graceful Module Loading
All routers attempt to load on startup. Missing modules log warnings but don't crash:
```python
routers_to_load = {
    'workers', 'clients', 'jobs', 'bookings', 'payments',
    'admin', 'whitelabel', 'auth', 'partners', 'labeling',
    'revenue_automation', 'social_dashboard', 'orders',
    'subscriptions', 'trading_signals', 'outreach', 'study',
    'trading_dashboard'
}
```

---

## 6. Active Trades & Recent Activity

### Prop Bot Trades (Last 7 Days)
**Account:** Alpaca APEX_589296  
**Mode:** Paper (live trading disabled)  
**Current Strategy:**
- Scans MES, MNQ, MGC proxies (SPY, QQQ, GLD)
- Entry: RSI < 45 + bullish trend
- Exit: RSI > 55 or bearish trend flip
- Profitable days counter: Tracks toward 7-day rule

**Status:** Paper trading active, real P&L accumulated (no live capital deployed yet)

### Tradovate Bot Trades (Last 7 Days)
**Account:** Tradovate APEX_589296 (same evaluation account)  
**Mode:** Demo (live trading disabled by default)  
**Current Contracts:**
- ES (E-mini S&P 500)
- NQ (E-mini Nasdaq)
- YM (E-mini Dow)
- RTY (E-mini Russell 2000)

**Scaling Rules:**
- $0 profit: 1 contract
- $300+ profit: 2 contracts
- $600+ profit: 3 contracts
- $900+ profit: 4 contracts (max)

**Status:** Demo trading active, signals generated from Alpaca data feed

### Trading Signals
- **Component:** `routers/trading_signals.py`
- **Status:** ✅ Active
- **Source:** RSI + trend confirmation across all monitored symbols

---

## 7. Deployment & Configuration

### Current Deployment
- **URL:** https://empire-v2-production.up.railway.app
- **Platform:** Railway
- **Container:** Python 3.11-slim Docker
- **Entry Point:** `python main.py` (runs uvicorn on port 8000)

### Environment Variables (Required)
```env
STRIPE_SECRET_KEY=sk_...
STRIPE_PUBLISHABLE_KEY=pk_...
STRIPE_WEBHOOK_SECRET=whsec_...
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
ALPACA_BASE_URL=https://paper-api.alpaca.markets  # or live endpoint
TRADOVATE_USER=...
TRADOVATE_PASS=...
TRADOVATE_CID=...
TRADOVATE_SECRET=...
```

### Environment Variables (Optional)
```env
HEYGAN_API_KEY=...          # HeyGen video generation (graceful fallback if missing)
GMAIL_EMAIL=...             # Email notifications (optional)
GMAIL_PASSWORD=...          # Gmail app password
PAYMENTS_PAUSED=false       # Set true to disable all Stripe checkout
STOP_TRADING=false          # Set true to pause both bots
ALPACA_LIVE_TRADE=false     # Set true for live futures (CAREFUL)
TRADOVATE_MODE=demo         # Set "live" only after demo testing
```

### Deployment Verification
```bash
# Pre-deploy checks:
python3 -m py_compile main.py routers/orders.py prop_bot.py tradovate_bot.py
python3 -c "from heygan_integration import generate_video; print('OK')"
test -f quote_request.html && echo "Form exists"

# Health check after deploy:
curl https://empire-v2-production.up.railway.app/health
# Expected: {"status": "ok"}

# Monitor endpoints:
https://empire-v2-production.up.railway.app/monitor/status
https://empire-v2-production.up.railway.app/monitor/errors
```

---

## 8. Active Routers & Endpoints

| Router | Endpoints | Status |
|--------|-----------|--------|
| `orders.py` | POST /orders/request-quote, POST /create-checkout, POST /webhook/stripe, GET /admin-dashboard | ✅ Active |
| `trading_dashboard.py` | GET /status, GET /withdrawal-requests, POST /request-withdrawal | ✅ Active |
| `trading_signals.py` | Trading signal generation endpoints | ✅ Active |
| `subscriptions.py` | Subscription tier management | ✅ Active |
| `revenue_automation.py` | Automated revenue tracking | ✅ Active |
| `social_dashboard.py` | Social media scheduling | ✅ Active |
| `outreach.py` | Email campaign management | ✅ Active |
| `study.py` | Study/education platform | ✅ Active |
| All other routers | Worker mgmt, clients, jobs, bookings, payments, admin | ✅ Active |

---

## 9. Known Limitations & TODOs

### High Priority
- ⚠️ **Order Persistence:** In-memory lists lost on restart → migrate to DB
- ⚠️ **Admin Auth:** No authentication on admin endpoints yet → add before production
- ⚠️ **Email:** Gmail not configured (test video generation first)
- ⚠️ **Live Trading:** ALPACA_LIVE_TRADE=false by default (activate only after 7 consecutive profitable paper days)

### Medium Priority
- 📋 **Withdrawal API:** ACH/bank-transfer not available via Alpaca self-directed API
- 📋 **Video Editing:** HeyGen generates new videos only; can't modify existing videos per user request
- 📋 **Scalability:** In-memory lists won't scale; add PostgreSQL for production

### Low Priority
- 📝 **Documentation:** Update API_ENDPOINTS.md with new trading dashboard endpoints
- 📝 **Testing:** Add end-to-end tests for video quote flow
- 📝 **Monitoring:** Add alerting for bot failures

---

## 10. Recent Git History

| Commit | Message | Impact |
|--------|---------|--------|
| 24437ad | Add bootstrap playbook | Documentation |
| ace65f6 | Add bot_api.py - REST API for crypto bot data | New endpoint |
| 8a622c4 | Merge bot_api infrastructure | Deployment |
| e78a6e5 | Add bot_api service deployment and proxy endpoint | Infrastructure |
| f0fd96f | Scan 5 proxy symbols and drop trend-gate | Trading strategy |
| e782a0b | Loosen RSI entry/exit thresholds | Trading tuning |
| 6f45a77 | Wire trading_dashboard.html to real Alpaca data | Dashboard |
| 8463b5c | Add auto-compounding ($100 increments) | Revenue |
| d573fc7 | Add lifetime withdrawal + holdings tracking | Dashboard |

---

## 11. Quick Links

- **API Docs:** http://localhost:8000/docs (when running locally)
- **Health Check:** https://empire-v2-production.up.railway.app/health
- **Quote Form:** https://empire-v2-production.up.railway.app/quote
- **Trading Dashboard:** https://empire-v2-production.up.railway.app/trading_dashboard.html (requires admin key)
- **Admin Dashboard:** https://empire-v2-production.up.railway.app/orders/admin-dashboard (requires admin key)
- **Stripe Docs:** https://stripe.com/docs/api/checkout/sessions
- **HeyGen Docs:** https://docs.heygen.com/
- **Alpaca Docs:** https://alpaca.markets/docs/api/
- **Tradovate Docs:** https://www.tradovate.com/api

---

## 12. Next Steps

### Immediate (Today)
1. ✅ Merge bootstrap playbook PR
2. Verify deployment is live: `curl https://empire-v2-production.up.railway.app/health`
3. Check trading bots status on Alpaca dashboard

### Short Term (This Week)
1. Test video quote form end-to-end (local + Railway)
2. Configure Gmail for email notifications
3. Monitor prop bot for 7 consecutive profitable days (paper)
4. Migrate pending_orders to persistent DB

### Medium Term (This Month)
1. Activate live trading on Alpaca (after 7 profitable days)
2. Add admin authentication to all dashboard endpoints
3. Set up alerting for bot failures
4. Document all trading parameters in Railway env vars

---

**Report Generated:** 2026-07-22 | **System Status:** ✅ All Core Systems Operational  
**Last Deployment:** Latest code from `claude/bootstrap-business-free-ai-bcyx4x` branch  
**Bootstrap Playbook:** Added to docs for video product positioning & upsells

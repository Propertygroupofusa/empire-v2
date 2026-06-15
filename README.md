# DEL'S TRADING EMPIRE v2

Automated trading + AI video revenue system — 99% hands-off.
Owner: Delfine Stennis | Property Group of USA LLC

---

## SYSTEMS RUNNING

| Bot | What It Does | Status |
|-----|-------------|--------|
| prop_bot.py | APEX $25K futures (APEX_589296) | Paper -> Live |
| revenue_bot.py | 4-stream income (stocks, crypto, options, futures) | Paper -> Live |
| health_monitor.py | Watches all bots, account safety thresholds | Always On |
| main.py | Orchestrator — starts everything, restarts crashes | Always On |
| video_revenue_api.py | AI video sales + YouTube publishing | Always On |

---

## TRIPLE AI SIGNAL CONFIRMATION

Every trade requires ALL 3 AIs to agree before executing.

| AI | Role | Get Key At |
|----|------|-----------|
| Claude (Anthropic) | Deep reasoning + risk analysis | console.anthropic.com |
| GPT-4o (OpenAI) | Speed + market sentiment | platform.openai.com |
| Grok (xAI) | Real-time X/Twitter + breaking news | console.x.ai |

No single AI can trigger a trade alone. All 3 must confirm = execute.

---

## ACCOUNTS

| Broker | Account | Mode |
|--------|---------|------|
| Tradovate / APEX | APEX_589296 | Demo -> Live |
| Alpaca | Paper account | Paper -> Live |
| OANDA | Demo | Demo -> Live |

---

## RULES (NON-NEGOTIABLE)

- Paper trade 7 consecutive profitable days before going live
- Account drops to $90K -> reduce position size
- Account drops to $80K -> ALL trading stops automatically
- STOP_TRADING=true kills everything instantly
- All 3 AIs must agree before any trade fires

---

## GO LIVE CHECKLIST

- [ ] 7 profitable paper days confirmed
- [ ] All 3 AI keys active and responding
- [ ] Change ALPACA_BASE_URL to api.alpaca.markets
- [ ] Change ALPACA_LIVE_TRADE to true
- [ ] Change TRADOVATE_MODE to live
- [ ] Both Alpaca flags required — one alone does nothing

---

## SYNTHESIA AI VIDEO SYSTEM (AI Social Media)

Auto-generate AI avatar videos for client content delivery.

### API
- Endpoint: https://api.synthesia.io/v2/videos
- Webhook events: video.completed, video.failed
- Signature verification: HMAC SHA256 via Synthesia-Signature header

### Top Avatars for Business Content

| Avatar | ID | Best For |
|--------|-----|---------|
| Olivia (Female v3) | e49ecfaf-1d39-4561-8355-29ebf8b71a4f | Professional/Finance |
| Hudson (Male v3) | 11af1a93-e679-41a6-9b21-4cd41d73c940 | Real Estate |
| Alisha (Female v3) | cf0eda7e-8f3c-43de-ae08-712e242ead61 | Marketing/Social |
| Mason (Male v3) | 72da6c7c-36b6-4824-816b-380ac2058d86 | Sales/Outreach |

### Endpoints

| Endpoint | Price | Use Case |
|----------|-------|---------|
| /generate/property-listing | $75/video | Real estate listings |
| /generate/social-content | $50/video | Client social packages |
| /generate/cold-call-followup | $25/video | Lead nurture |
| /generate/payee-trust-onboarding | internal | Activation rates |

---

## YOUTUBE AUTO-PUBLISH PIPELINE

Synthesia videos auto-download and upload to YouTube — no manual steps.

### Pipeline

```
Generate video (Synthesia) -> Webhook fires on completion
  -> Download video -> Upload to YouTube -> Public/Unlisted
```

### Endpoints

| Endpoint | Privacy | Use Case |
|----------|---------|----------|
| /publish/youtube/property-listing | public | Drives buyer traffic for wholesale deals |
| /publish/youtube/social-content | unlisted | Client review before going live |

### How to Get YouTube Credentials

1. console.cloud.google.com -> new project -> enable YouTube Data API v3
2. Create OAuth 2.0 credentials -> get CLIENT_ID + CLIENT_SECRET
3. developers.google.com/oauthplayground -> authorize YouTube upload scope -> get REFRESH_TOKEN

---

## ENVIRONMENT VARIABLES (set in Railway — never in code)

Paste this entire block into Railway's Raw Editor (Variables tab):

```
ALPACA_API_KEY=your_alpaca_key_here
ALPACA_SECRET_KEY=your_alpaca_secret_here
ALPACA_BASE_URL=https://paper-api.alpaca.markets
ALPACA_LIVE_TRADE=false

ANTHROPIC_API_KEY=sk-ant-your_key_here
OPENAI_API_KEY=sk-your_openai_key_here
GROK_API_KEY=your_xai_key_here

TRADOVATE_USER=APEX_589296
TRADOVATE_PASS=your_tradovate_password_here
TRADOVATE_MODE=demo

SYNTHESIA_API_KEY=your_synthesia_key_here
SYNTHESIA_WEBHOOK_SECRET=your_synthesia_webhook_secret_here

YOUTUBE_API_KEY=your_youtube_key_here
YOUTUBE_CLIENT_ID=your_client_id_here
YOUTUBE_CLIENT_SECRET=your_client_secret_here
YOUTUBE_REFRESH_TOKEN=your_refresh_token_here

STOP_TRADING=false
EMPIRE_ACCOUNT_SIZE=100000
EMPIRE_HARD_STOP=80000
EMPIRE_REDUCE_AT=90000
PROP_PROFIT_TARGET=1500
PROP_DAILY_LOSS_LIMIT=1000
HEALTH_CHECK_INTERVAL=60
PORT=10000
```

---

## FOLDER STRUCTURE

```
empire-v2/
├── main.py                   # Entry point — starts all bots
├── prop_bot.py               # APEX prop trading bot
├── revenue_bot.py            # 4-stream revenue bot + triple AI
├── ai_signal_confirm.py       # Triple AI trade confirmation
├── health_monitor.py          # System watchdog
├── synthesia_video_bot.py      # AI video generation
├── video_revenue_api.py        # Video sales + YouTube endpoints
├── youtube_upload_bot.py       # Auto-publish to YouTube
├── requirements.txt
├── railway.json
├── .gitignore
└── README.md
```

---

## REVENUE TARGETS

| Source | Monthly |
|--------|---------|
| Trading (paper -> live, 4 streams) | $24K-60K |
| Property listing videos | $1,500 (20/mo @ $75) |
| Social content packages | $1,500-5,000 (5 clients) |
| Cold call follow-up videos | $2,500 (100/mo @ $25) |
| YouTube ad revenue (ramping) | $0-2,000 |

---

## DEPLOY STEPS (RAILWAY)

1. railway.app -> New Project -> Deploy from GitHub repo -> select `empire-v2`
2. Variables tab -> Raw Editor -> paste the full env block above (fill in real keys)
3. Railway auto-deploys
4. Check Deployments -> logs for:

```
DEL'S TRADING EMPIRE — STARTING
Empire online. 3 bots running.
```

5. Hit `/health` on the deployed URL to confirm status

---

Built with Claude + GPT-4o + Grok + Synthesia. Deployed on Railway.

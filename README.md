# DEL'S TRADING EMPIRE v2

Automated trading system — 99% hands-off.
Owner: Delfine Stennis | Property Group of USA LLC

---

## SYSTEMS RUNNING

| Bot | What It Does | Status |
|-----|-------------|--------|
| prop_bot.py | APEX $25K futures (APEX_589296) | Paper → Live |
| revenue_bot.py | 4-stream income (stocks, crypto, options, futures) | Paper → Live |
| health_monitor.py | Watches all bots, alerts on crashes | Always On |
| main.py | Orchestrator — starts everything, restarts crashes | Always On |

---

## TRIPLE AI SIGNAL CONFIRMATION

Every trade requires ALL 3 AIs to agree before executing.

| AI | Role | API |
|----|------|-----|
| Claude (Anthropic) | Deep reasoning + risk analysis | console.anthropic.com |
| GPT-4o (OpenAI) | Speed + market sentiment | platform.openai.com |
| Grok (xAI) | Real-time X/Twitter + breaking news | console.x.ai |

No single AI can trigger a trade alone. All 3 must confirm = execute.

---

## ACCOUNTS

| Broker | Account | Mode |
|--------|---------|------|
| Tradovate / APEX | APEX_589296 | Demo → Live |
| Alpaca | Paper account | Paper → Live |
| OANDA | Demo | Demo → Live |

---

## RULES (NON-NEGOTIABLE)

- Paper trade 7 consecutive profitable days before going live
- Account drops to $90K → reduce position size
- Account drops to $80K → ALL trading stops automatically
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

### Add to Railway Variables
## ENVIRONMENT VARIABLES (set in Railway — never in code)

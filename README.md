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

## ENVIRONMENT VARIABLES (set in Railway — never in code)

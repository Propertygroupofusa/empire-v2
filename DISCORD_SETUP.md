# 📢 Discord Signal Receiver Setup

Autonomous trading signals triggered from Discord messages. Post to your Discord channel and the bot auto-executes trades.

---

## Quick Start

### 1. Configure Environment Variables

Add these to Railway environment variables:

```
# Discord webhook settings
DISCORD_WEBHOOK_SECRET=your_webhook_secret_here
DISCORD_CHANNEL_ID=your_channel_id_here

# Discord signal execution
DISCORD_SIGNAL_SIZE_USD=500          # Default $500 per signal (override per signal)
DISCORD_AUTO_EXECUTE=true            # Auto-execute trades from Discord signals
```

### 2. Test the Endpoint

Discord signal receiver is live at:
```
POST /discord/signal
GET /discord/status
```

**Check status:**
```bash
curl https://empire-v2-production.up.railway.app/discord/status
```

Expected response:
```json
{
  "status": "active",
  "auto_execute": true,
  "live_trading": true,
  "position_size_usd": 500,
  "signals_received": 0,
  "configured": true
}
```

---

## Signal Format

Post messages to your Discord channel using these formats:

### Format 1: Side First
```
BUY BTC              → Buy Bitcoin with default size ($500)
SELL ETH             → Sell Ethereum with default size ($500)
BUY $1000 AAPL       → Buy $1,000 worth of Apple stock
SELL $250 MSFT       → Sell $250 worth of Microsoft
```

### Format 2: Ticker First
```
BTC BUY              → Buy Bitcoin with default size
ETH SELL             → Sell Ethereum with default size
AAPL BUY $500        → Buy $500 worth of Apple
MSFT SELL $1000      → Sell $1,000 worth of Microsoft
```

### Format 3: Bot Webhook JSON (Recommended)
```bash
curl -X POST https://empire-v2-production.up.railway.app/discord/signal \
  -H "Content-Type: application/json" \
  -d '{
    "signal": "BUY $500 BTC",
    "user": "username",
    "confidence": 0.95
  }'
```

---

## Discord Bot Setup (Optional)

To automatically post signals from Discord to the trading bot:

### Step 1: Create Discord Bot & Webhook

1. Go to Discord Server Settings → Integrations → Webhooks
2. Create new webhook in your trading channel
3. Copy the webhook URL: `https://discordapp.com/api/webhooks/...`

### Step 2: Create External Webhook in Discord Bot

If using a Discord bot framework (discord.py, discord.js), add:

```python
# discord.py example
import discord
from discord.ext import commands
import requests

bot = commands.Bot(command_prefix='!', intents=discord.Intents.default())
TRADING_WEBHOOK = "https://empire-v2-production.up.railway.app/discord/signal"

@bot.command(name='trade')
async def trade_signal(ctx, side: str, ticker: str, *, amount: str = None):
    """
    !trade buy BTC
    !trade sell $500 AAPL
    """
    signal_text = f"{side.upper()} {amount or ''} {ticker.upper()}".strip()
    
    payload = {
        "signal": signal_text,
        "user": str(ctx.author),
        "confidence": 0.90
    }
    
    response = requests.post(TRADING_WEBHOOK, json=payload)
    await ctx.send(f"Signal sent: {signal_text} | Status: {response.json().get('status')}")

bot.run('YOUR_BOT_TOKEN')
```

### Step 3: Enable Message Triggers

Post directly to the Discord channel:
```
buy btc
sell $250 eth
buy $1000 aapl
```

The bot will auto-execute these signals!

---

## Real-Time Trading Examples

### Example 1: Simple Buy Signal
```
Channel: #trading-signals
Message: BUY BTC

Result:
✅ DISCORD SIGNAL EXECUTED (ALPACA)
   Ticker: BTC
   Side: BUY
   Price: $65,234.50
   Qty: 7 shares
   Amount: $456,641.50
```

### Example 2: Sized Sell Signal
```
Channel: #trading-signals
Message: SELL $300 SPY

Result:
✅ DISCORD SIGNAL EXECUTED (ALPACA)
   Ticker: SPY
   Side: SELL
   Price: $567.89
   Qty: 1 share
   Amount: $567.89
```

### Example 3: High-Confidence Buy
```
POST /discord/signal
{
  "signal": "BUY $1000 QQQ",
  "user": "analyst_01",
  "confidence": 0.98
}

Result:
✅ DISCORD SIGNAL EXECUTED (ALPACA)
   Ticker: QQQ
   Side: BUY
   Price: $425.67
   Qty: 2 shares
   Amount: $851.34
```

---

## Risk Management

### Daily Loss Limit
Set per-signal stop loss automatically:
```
Position size: $500 per signal
Stop loss: 3% per trade
Profit target: 5% per trade
```

### Position Limits
- Max 10 simultaneous positions from Discord signals
- Duplicate signals (same ticker/side within 10 minutes) ignored
- Max $5,000 USD in active Discord signal trades

### Execution Safety
- Auto-execute only if `ALPACA_LIVE_TRADE=true`
- Paper trading mode if disabled
- All orders are bracket orders (TP/SL included)

---

## Monitoring

### Check Signal Status
```bash
curl https://empire-v2-production.up.railway.app/discord/status
```

### View Recent Signals
Check trading dashboard:
```
https://empire-v2-production.up.railway.app/trading/dashboard
```

### Logs
```bash
# Watch for Discord signal execution
tail -f /tmp/empire-server.log | grep -i "discord\|signal"
```

---

## Troubleshooting

### Signal Not Executing

**Issue:** Posted signal but no trade executed

**Cause 1:** `ALPACA_LIVE_TRADE=false`
- Set to `true` on Railway for live execution
- Otherwise runs in paper mode (logs only)

**Cause 2:** Signal format not recognized
- Use: `BUY BTC` or `BTC BUY` (ticker + side)
- Use: `BUY $500 AAPL` (side + size + ticker)
- All uppercase

**Cause 3:** Broker API down
- Check `/discord/status` → should show `"configured": true`
- Verify ALPACA_API_KEY and ALPACA_SECRET_KEY are set

**Solution:**
```bash
# Test signal manually
curl -X POST https://empire-v2-production.up.railway.app/discord/signal \
  -H "Content-Type: application/json" \
  -d '{"signal": "BUY BTC", "user": "test"}'

# Should return:
# {"status": "executed", "signal": {"ticker": "BTC", "side": "buy", ...}}
```

### Duplicate Signals Being Executed

**Issue:** Same signal posted twice, both executed

**Solution:** Discord receiver tracks recent signals for 10 minutes
- Duplicate BTC BUY signals within 10 min are ignored
- Second signal returns `{"status": "duplicate"}`

### Wrong Price Quoted

**Issue:** Buy order at different price than shown in Discord

**Cause:** Market moved between signal and execution
- Takes ~1-2 seconds to execute
- Use limit orders for price certainty (future enhancement)

---

## Deployment

### 1. Push Code
```bash
git add discord_signal_receiver.py main.py DISCORD_SETUP.md
git commit -m "Add Discord trading signal receiver

- Listen for Discord signals at /discord/signal
- Auto-execute trades via Alpaca with bracket orders
- Support multiple signal formats (BUY BTC, $500 AAPL BUY, etc.)
- Track signal execution and duplicate prevention
- Real-time status at /discord/status"

git push -u origin claude/liquid-trade-integration-j20qzu
```

### 2. Set Railway Variables
```
DISCORD_WEBHOOK_SECRET=your_secret
DISCORD_CHANNEL_ID=channel_id
DISCORD_SIGNAL_SIZE_USD=500
DISCORD_AUTO_EXECUTE=true
```

### 3. Redeploy
- Go to Railway dashboard → empire-v2 → Redeploy
- Wait 2-3 minutes for "Active" status

### 4. Test
```bash
curl -X POST https://empire-v2-production.up.railway.app/discord/signal \
  -H "Content-Type: application/json" \
  -d '{"signal": "BUY BTC", "user": "test", "confidence": 0.9}'
```

---

## Next: Advanced Integration

### Discord Bot Commands
```
!trade buy btc                    → Execute BUY signal
!portfolio                        → Show open positions
!close all                        → Close all Discord signals
!set-size $1000                   → Change default position size
!confidence 0.95 buy aapl $500   → Higher confidence trades
```

### AI Signal Confirmation
Combine with AI signal confirmation for adaptive execution:
```python
# Check AI model confidence before executing
if signal_confidence > 0.85 and ai_signal_confidence > 0.8:
    execute_trade()  # High confidence both ways
```

### Slack Integration
Post results to Slack channel for team visibility:
```
Channel: #trading-alerts
Message: ✅ Discord signal executed: BUY 7x BTC @ $65,234
         Position size: $456,641
         TP: $68,495 | SL: $63,477
```

---

## Status: ✅ Ready to Deploy

Test the endpoint, set environment variables, and deploy to Railway.

Your Discord trading signals are ready for automation!

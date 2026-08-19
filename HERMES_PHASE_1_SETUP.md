# Hermes Agent Phase 1 Setup Guide

**Status:** ✅ Implemented  
**Components:** Hermes Agent + Telegram Integration + Status Reporter  
**Features:** Autonomous bot management with real-time Telegram notifications

---

## Overview

**Phase 1** establishes the foundation for autonomous trading bot management through Hermes Agent. The system consists of three core modules:

1. **Hermes Agent** (`hermes_agent.py`) — AI controller for bot operations
2. **Telegram Integration** (`telegram_integration.py`) — Real-time notifications to you
3. **Status Reporter** (`status_reporter.py`) — Performance metrics collection & reporting

When fully configured, Hermes will:
- ✅ Monitor all trading bot performance in real-time
- ✅ Send hourly/on-demand status reports to Telegram
- ✅ Receive and execute trading commands from you via Telegram
- ✅ Track P&L, win rates, positions, and errors
- ✅ Provide foundation for Phase 2 (autonomous commands) & Phase 3 (multi-channel integration)

---

## Configuration

### Step 1: Set Environment Variables

Add these to your `.env` file or Railway Variables:

```bash
# Hermes Agent (required for autonomous management)
HERMES_API_KEY=your-hermes-api-key-here
HERMES_MODEL=claude-opus  # or claude-3.5-sonnet

# Telegram Bot (required for notifications)
TELEGRAM_BOT_TOKEN=1234567890:ABCDefGhIjklMnoPQRstUvWxYzABCDEfG
TELEGRAM_CHAT_ID=-1001234567890  # Your private chat or group ID
```

### Step 2: Create Telegram Bot

1. Open Telegram and search for **@BotFather**
2. Send `/newbot`
3. Follow prompts to create your bot (e.g., "EmpireHermesBot")
4. **Save the bot token** → goes in `TELEGRAM_BOT_TOKEN`

5. Get your Chat ID:
   - Send any message to your bot
   - Visit: `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates`
   - Find your `chat_id` → goes in `TELEGRAM_CHAT_ID`

### Step 3: Get Hermes API Key

1. Visit [Nous Research Hermes](https://github.com/NousResearch/hermes-agent) or use Claude API
2. Obtain API credentials
3. Add to `HERMES_API_KEY` environment variable

### Step 4: Deploy to Railway

On Railway dashboard:
1. Go to **empire-v2** project
2. Click **Variables**
3. Add the three environment variables above
4. Deploy: `git push -u origin claude/hermes-biadpe`

---

## API Endpoints

### Status & Configuration

**GET `/hermes/status`**
- Returns Hermes Agent, Telegram, and Reporter status
- Useful for checking if everything is initialized

**GET `/telegram/health`**
- Verifies Telegram bot connection
- Shows message count, chat ID, enabled status

### Sending Messages

**POST `/telegram/send-test?message=<your-message>`**
- Send a test message to your Telegram chat
- Useful for verification before bot deployment

### Recording Bot Metrics

**POST `/bot/status/record`**
- Called by trading bots to send performance data
- Parameters:
  - `bot_name`: "crypto_coinbase_bot", "prop_bot", etc.
  - `cash_available`: Current account balance
  - `daily_pnl`: Day's profit/loss
  - `win_rate`: Percentage of winning trades
  - `trade_count`: Total trades today
  - `open_positions`: {"BTC/USD": {"entry": 45000, "current": 45500, ...}}
  - `errors`: List of error messages

Example:
```bash
curl -X POST "http://localhost:8000/bot/status/record" \
  -H "Content-Type: application/json" \
  -d '{
    "bot_name": "crypto_coinbase_bot",
    "cash_available": 8500.50,
    "daily_pnl": 250.75,
    "win_rate": 72.5,
    "trade_count": 8,
    "open_positions": {
      "BTC/USD": {
        "entry_price": 45000,
        "current_price": 45500,
        "pnl": 500,
        "size": 0.1
      }
    },
    "errors": []
  }'
```

### Retrieving Status

**GET `/bot/status/latest?bot_name=crypto_coinbase_bot`**
- Get latest recorded status for a specific bot
- Omit `bot_name` for summary across all bots

**GET `/bot/status/latest`** (no params)
- Returns aggregated report: total cash, daily P&L, trade counts, win rate

### Sending Reports

**POST `/bot/status/report?bot_name=crypto_coinbase_bot`**
- Manually trigger Telegram report for specific bot
- Omit `bot_name` for all-bots summary

**POST `/bot/status/report`** (no params)
- Sends comprehensive summary to Telegram

---

## Integration with Trading Bots

### Example: Crypto Coinbase Bot

Add status reporting to `crypto_coinbase_bot.py`:

```python
import asyncio
import aiohttp

async def report_status_to_hermes(positions, cash, daily_pnl):
    """Send bot status to Empire v2 Hermes Agent"""
    async with aiohttp.ClientSession() as session:
        payload = {
            "bot_name": "crypto_coinbase_bot",
            "cash_available": cash,
            "daily_pnl": daily_pnl,
            "win_rate": calculate_win_rate(),
            "trade_count": count_trades(),
            "open_positions": positions,
            "errors": get_errors(),
        }

        try:
            async with session.post(
                "http://localhost:8000/bot/status/record",  # Or Railway URL
                json=payload,
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    logging.info("✅ Status reported to Hermes")
                else:
                    logging.warning(f"Status report failed: {resp.status}")
        except Exception as e:
            logging.error(f"Status report error: {e}")

# Call in your main bot loop every cycle or every N cycles:
# await report_status_to_hermes(positions, cash, daily_pnl)
```

### Example: Prop Bot

Similar pattern for `prop_bot.py`:

```python
# In main bot cycle:
await report_status_to_hermes(
    positions=active_positions,
    cash=account_cash,
    daily_pnl=today_pnl
)
```

---

## Testing Phase 1

### Local Testing

1. **Start the app:**
   ```bash
   python main.py
   ```

2. **Check Hermes status:**
   ```bash
   curl http://localhost:8000/hermes/status
   ```

3. **Send test Telegram message:**
   ```bash
   curl -X POST "http://localhost:8000/telegram/send-test?message=Hello%20from%20Hermes"
   ```

4. **Record a bot status:**
   ```bash
   curl -X POST "http://localhost:8000/bot/status/record" \
     -H "Content-Type: application/json" \
     -d '{
       "bot_name": "test_bot",
       "cash_available": 1000,
       "daily_pnl": 50.25,
       "win_rate": 60,
       "trade_count": 5
     }'
   ```

5. **Trigger report:**
   ```bash
   curl -X POST "http://localhost:8000/bot/status/report"
   ```

6. **Check Telegram** — you should see:
   ```
   📊 TRADING BOT STATUS
   [timestamp]

   Open Positions:
   [none initially, since we're testing]

   💰 Account Metrics:
   💵 Cash Available: $1,000.00
   📊 Daily P&L: +$50.25
   🎯 Win Rate: 60.0% (5 trades)

   ✅ No errors
   ```

### Production Testing (Railway)

1. **Deploy to Railway:**
   ```bash
   git add -A
   git commit -m "Phase 1: Hermes Agent + Telegram Integration"
   git push -u origin claude/hermes-biadpe
   ```

2. **Redeploy on Railway dashboard**

3. **Test endpoints against Railway URL:**
   ```bash
   curl https://empire-v2-production.up.railway.app/hermes/status
   curl -X POST "https://empire-v2-production.up.railway.app/telegram/send-test?message=Production%20test"
   ```

4. **Verify Telegram message arrives**

---

## Database Tables

Three new tables store Hermes data:

### `hermes_sessions`
```sql
CREATE TABLE hermes_sessions (
  id INTEGER PRIMARY KEY,
  session_id VARCHAR UNIQUE,
  status VARCHAR DEFAULT 'active',
  started_at DATETIME,
  last_activity DATETIME,
  message_count INTEGER DEFAULT 0,
  metadata JSON,
  closed_at DATETIME
);
```

### `telegram_messages`
```sql
CREATE TABLE telegram_messages (
  id INTEGER PRIMARY KEY,
  chat_id VARCHAR,
  message_id VARCHAR,
  text TEXT,
  message_type VARCHAR,  -- status, alert, command, report
  status VARCHAR DEFAULT 'sent',
  sent_at DATETIME,
  metadata JSON
);
```

### `bot_statuses`
```sql
CREATE TABLE bot_statuses (
  id INTEGER PRIMARY KEY,
  bot_name VARCHAR,
  timestamp DATETIME,
  cash_available FLOAT,
  daily_pnl FLOAT,
  weekly_pnl FLOAT,
  monthly_pnl FLOAT,
  win_rate FLOAT,
  trade_count INTEGER,
  win_count INTEGER,
  loss_count INTEGER,
  avg_win FLOAT,
  avg_loss FLOAT,
  open_positions_count INTEGER,
  errors JSON,
  metadata JSON
);
```

---

## Troubleshooting

### Hermes not initialized
- **Cause:** `HERMES_API_KEY` not set
- **Fix:** Set environment variable and redeploy

### Telegram messages not sending
- **Cause:** `TELEGRAM_BOT_TOKEN` or `TELEGRAM_CHAT_ID` invalid
- **Check:**
  - Verify bot token format (should start with digits:ABC...)
  - Verify chat ID is numeric and negative (for private chats: -1001234567890)
  - Test with: `curl -X POST "http://localhost:8000/telegram/send-test"`
- **Fix:** Update env vars and redeploy

### Status not recording
- **Cause:** Network error or endpoint unreachable
- **Check:** Verify POST endpoint is correct (local vs Railway URL)
- **Fix:** Check logs for HTTP errors, ensure firewall allows outbound POST requests

### Database tables not created
- **Cause:** Migration didn't run
- **Fix:** Tables auto-create on first `init_db()` call. Check logs for database errors.

---

## Next Steps

- **Phase 2:** Add command execution (trading orders from Telegram)
- **Phase 3:** Multi-channel integration (Discord, iMessage, email)
- **Phase 4:** Advanced analytics & AI decision support

---

## Files Changed (Phase 1)

| File | Purpose |
|------|---------|
| `hermes_agent.py` | NEW - Hermes Agent controller |
| `telegram_integration.py` | NEW - Telegram Bot wrapper |
| `status_reporter.py` | NEW - Metrics collection & reporting |
| `hermes_config.json` | NEW - Configuration template |
| `models.py` | UPDATED - Added 3 new database models |
| `main.py` | UPDATED - Hermes initialization + 6 new endpoints |
| `requirements.txt` | UPDATED - Added `python-telegram-bot==21.3` |

---

## Configuration File Reference

`hermes_config.json` includes:
- AI model selection (claude-opus, etc.)
- API retry logic
- Auto-report intervals (default: 1 hour)
- Status report prompts
- Telegram parse mode (HTML/Markdown)

Customize as needed for your deployment.

---

**Phase 1 Complete!** 🎉

Your trading bots are now connected to an autonomous AI supervisor with real-time Telegram notifications. Proceed to Phase 2 for command execution.

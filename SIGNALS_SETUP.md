# Trading Signals Subscription Setup

## Overview

The trading signals subscription system allows you to monetize the APEX prop_bot trading performance.

- **Landing Page**: `signals_landing.html` - $297/month signup page
- **API Server**: `trading_signals_api.py` - Handles Stripe payments + signal delivery
- **Client SDK**: `signals_client.py` - Easy integration for subscribers

## Step 1: Stripe Setup

1. Go to [stripe.com](https://stripe.com)
2. Create a business account
3. Get your **Secret Key** from Dashboard → Developers → API keys
4. Create a **Product** called "Trading Signals Monthly"
5. Create a **Price** for the product:
   - Type: Recurring
   - Amount: $29700 (in cents)
   - Billing Period: 1 month
   - Copy the **Price ID**
6. Create a **Webhook Endpoint**:
   - Endpoint URL: `https://your-railway-app/webhook/stripe`
   - Events to send: `checkout.session.completed`, `customer.subscription.deleted`
   - Copy the **Signing Secret**

## Step 2: Railway Environment Variables

Add to your Railway app's environment variables:

```
STRIPE_SECRET_KEY=sk_test_xxxxxxxxx (or sk_live_xxx for production)
STRIPE_PRICE_ID=price_xxxxxxxxx
STRIPE_WEBHOOK_SECRET=whsec_xxxxxxxxx
ADMIN_API_KEY=your-secret-admin-key
SIGNALS_API_PORT=8001
STATE_DIR=/data/bot_state
ENABLE_SIGNALS_API=true
```

## Step 3: Update Main.py

Add to your deployment configuration to start the signals API:

```python
if ENABLE_SIGNALS_API:
    start_process("SIGNALS_API", "python trading_signals_api.py")
```

## Step 4: Deploy Landing Page

Host the landing page on your domain:

```bash
# Copy to your web server
cp signals_landing.html /var/www/html/signals.html

# Or use Railway Static Files
```

Update the button in the HTML to point to your API:
- Change `http://localhost:8001` to your Railway API URL

## Step 5: Test the Flow

```bash
# Terminal 1: Run the API
python trading_signals_api.py

# Terminal 2: Test client
python signals_client.py

# Or manually test subscription:
curl -X POST http://localhost:8001/subscribe \
  -H "Content-Type: application/json" \
  -d '{"email":"test@example.com","customer_name":"Test User"}'
```

## API Endpoints

### Public Endpoints

**Subscribe (create checkout session)**
```bash
POST /subscribe
{
  "email": "user@example.com",
  "customer_name": "John Doe"
}

Response:
{
  "checkout_url": "https://checkout.stripe.com/...",
  "subscriber_id": "abc123...",
  "session_id": "cs_test_..."
}
```

### Authenticated Endpoints (requires X-API-Key header)

**Get Trading Signals**
```bash
GET /signals
Header: X-API-Key: your-api-key

Response:
{
  "subscriber": "user@example.com",
  "signals": {
    "profitable_days": ["2026-07-01", "2026-07-02", ...],
    "consecutive_profitable": 5,
    "daily_pnl": 250.50,
    "status": "still_evaluating"  // or "ready_to_trade"
  },
  "timestamp": "2026-07-10T..."
}
```

**Get Subscriber Info**
```bash
GET /subscriber/info
Header: X-API-Key: your-api-key

Response:
{
  "email": "user@example.com",
  "name": "John Doe",
  "status": "active",
  "joined": "2026-07-10T...",
  "api_key": "eyJ0eXAiOiJKV1..."
}
```

### Admin Endpoints (requires admin_key parameter)

**List All Subscribers**
```bash
GET /admin/subscribers?admin_key=your-secret-admin-key

Response:
{
  "total": 42,
  "subscribers": [
    {
      "id": "sub123",
      "email": "user@example.com",
      "status": "active",
      "joined": "2026-07-10T..."
    }
  ]
}
```

**Manually Activate Subscriber (for testing/manual approvals)**
```bash
POST /admin/activate-subscriber
Params: subscriber_id=xxx, admin_key=your-secret-admin-key

Response:
{
  "subscriber_id": "sub123",
  "email": "user@example.com",
  "api_key": "eyJ0eXAiOiJKV1...",
  "status": "active"
}
```

## Client SDK Usage

```python
from signals_client import SignalsClient

# Create client
client = SignalsClient(
    api_key="your-api-key-from-stripe",
    base_url="https://your-signals-api.com"
)

# Get current signals
signals = client.get_signals()
print(f"Profitable days: {signals['consecutive_profitable']}/7")
print(f"Daily P&L: ${signals['signals']['daily_pnl']:.2f}")

# Check if ready to trade
if client.is_ready_to_trade():
    print("✅ LIVE TRADING ENABLED!")

# Get subscription info
info = client.get_info()
print(f"Subscription: {info['status']}")

# Monitor signals continuously
def on_signal_update(signals):
    print(f"Signal update: {signals}")

client.monitor_signals(on_signal_update, interval=60)
```

## Revenue Model

- **Price**: $297/month per subscriber
- **Breakeven**: ~3-5 subscribers = $900-1500/month
- **Profit at 10 subs**: $2,970/month
- **Profit at 50 subs**: $14,850/month

## Money-Back Guarantee

Implement refund logic in your payment processor:
- Track subscriber's P&L over 30 days
- If negative, automatically issue refund via Stripe Dashboard
- Or manually trigger via admin API

## Future Enhancements

1. **Tiered pricing**: Basic ($97), Pro ($297), VIP ($997)
2. **Performance tiers**: Charge more for higher win-rate signals
3. **White-label**: Offer API access to other trading educators
4. **Discord bot**: Send signals to Discord channel for subscribers
5. **Mobile alerts**: SMS/push notifications for signals
6. **Analytics dashboard**: Show subscribers their signal performance

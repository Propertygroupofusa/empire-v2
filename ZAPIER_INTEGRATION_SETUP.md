# ZAPIER INTEGRATION SETUP (5-Minute Integration)

## OVERVIEW

This is how we connect the customer's email to our AI support bot:

```
Their support email (Gmail/Shopify)
    ↓
Zapier webhook trigger
    ↓
Our AI bot (FastAPI + Claude)
    ↓
AI generates response
    ↓
Response sent back to customer
    ↓
Ticket logged in dashboard
```

---

## PRE-REQUISITES

You need:
- ✅ Zapier account (free tier works)
- ✅ Our bot deployed on Railway (see DEPLOYMENT.md)
- ✅ Their Gmail/Shopify email access
- ✅ Stripe account for payments

---

## STEP 1: GET THEIR EMAIL ACCESS

**During onboarding call, ask:**

"I'll need access to your support email so the AI can read incoming tickets. We'll use Zapier to securely connect it."

**They give you access via:**
- Gmail: Sign into their Gmail account (they change password after)
- Shopify: Use Shopify's email integration
- Custom email: Connect via IMAP

---

## STEP 2: CREATE ZAPIER ZAP

### Part A: Trigger (When email arrives)

1. Go to **zapier.com** → Log in
2. Click **Create** → New Zap
3. Search for **Gmail** (or their email service)
4. Choose **New Email** (trigger)
5. Connect their Gmail account (they'll be prompted to authorize)
6. Configure trigger:
   - **Mailbox:** support@ (or their support email)
   - **Search:** Only emails (not Promotions/Social)
   - **Conditions:** 
     - To: support@[their-domain].com
     - Subject: NOT "Re:" (avoids loops)

**Test:** Should show their recent support emails ✅

---

### Part B: Action (Send to our bot)

1. Click **Add Step** → New Action
2. Search for **Webhooks by Zapier**
3. Choose **POST**
4. Configure:

**URL:**
```
https://empire-v2-production.up.railway.app/handle-support-email
```

**Method:** POST

**Payload Type:** JSON

**Data:**
```json
{
  "brand_id": "STORE_ID",
  "customer_email": "{{ from_email }}",
  "customer_name": "{{ from_name }}",
  "subject": "{{ subject }}",
  "body": "{{ plain_text_body }}",
  "webhook_token": "YOUR_WEBHOOK_SECRET"
}
```

**Replace:**
- `STORE_ID` = unique ID (e.g., "tower28")
- `YOUR_WEBHOOK_SECRET` = env variable from Railway

---

## STEP 3: SEND TEST EMAIL

1. Go back to Zapier
2. Send a **test email** to their support email
3. Watch Zapier trigger → see webhook POST in logs
4. **Check:** Email gets response back automatically ✅

---

## STEP 4: LIVE DEPLOYMENT

1. Click **Publish Zap** (turn it on)
2. Zap now processes ALL incoming support emails
3. Every email = automatic AI response

---

## STEP 5: ADD GMAIL SENDING (So bot can reply)

By default, the bot logs responses but doesn't send them via email. To make it send:

### Add Gmail sending to our bot code:

In `beauty_support_bot.py`, update the `send_response_email` function:

```python
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import base64
from email.mime.text import MIMEText

async def send_response_email(to: str, subject: str, body: str, brand_id: str):
    """Send response email using Gmail API"""
    try:
        # Use their Gmail account to send (set up with service account)
        service = build('gmail', 'v1', credentials=get_gmail_credentials(brand_id))
        
        message = MIMEText(body)
        message['to'] = to
        message['subject'] = subject
        message['from'] = os.getenv(f"GMAIL_ADDRESS_{brand_id}")
        
        create_message = {
            'raw': base64.urlsafe_b64encode(message.as_bytes()).decode()
        }
        
        service.users().messages().send(userId='me', body=create_message).execute()
        
        log.info(f"📧 Email sent to {to}")
    except Exception as e:
        log.error(f"Gmail sending error: {e}")
```

**OR** (simpler for MVP):

Use **Zapier's Gmail sending** as a 2nd action:
1. After webhook processes
2. Add step: Gmail → Send Email
3. From: [their Gmail]
4. To: {{ from_email }}
5. Subject: Re: {{ subject }}
6. Body: [Pull from bot response]

---

## STEP 6: DASHBOARD ACCESS

Customer can see:
- All tickets handled
- AI response quality
- Escalations
- Cost savings

**Dashboard URL:**
```
https://empire-v2-production.up.railway.app/dashboard/STORE_ID
```

Replace `STORE_ID` with their ID (e.g., "tower28")

---

## TESTING CHECKLIST

- [ ] Zapier connected to their email
- [ ] Test email triggers webhook
- [ ] Bot responds automatically
- [ ] Response sent back to test email
- [ ] Dashboard shows ticket
- [ ] Dashboard shows cost savings

---

## TROUBLESHOOTING

### Webhook not firing
- Check: Email address matches filter
- Check: Subject doesn't contain "Re:"
- Check: Gmail account authorized

### AI not responding
- Check: API key valid (ANTHROPIC_API_KEY)
- Check: Claude API credits available
- Check: Bot URL accessible (https://empire-v2-production.up.railway.app/health)

### Email not sending back
- Check: Gmail credentials correct
- Check: "From" address matches their Gmail
- Check: Email isn't filtered as spam

### Dashboard shows 0 tickets
- Check: Zapier data reaching bot
- Check: Database connected (PostgreSQL)
- Check: Tickets being stored

---

## ROLLBACK (If customer wants to stop)

1. Disable Zapier zap (turn it off)
2. No more emails triggered
3. Stop charging them
4. Can re-enable anytime

---

## QUICKSTART (TL;DR)

1. Customer gives Gmail access
2. Create Zapier + Gmail trigger
3. Point to our bot webhook (POST)
4. Turn on → done
5. Every support email → automatic response

**Total time:** 5 minutes
**Customer value:** $2,000+/month savings

That's it. 💪

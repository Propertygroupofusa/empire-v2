# Automated Bank Transfer Setup Guide

## Overview

The automated bank transfer system enables completely hands-free movement of earnings from Alpaca (prop_bot) to Coinbase (crypto_bot) without any manual action required.

**Flow:**
1. Prop bot closes profitable positions → profits recorded
2. 50% of profits automatically trigger Alpaca withdrawal
3. Bank processes ACH transfer (1-3 business days)
4. Coinbase automatically receives deposit from same bank account
5. Crypto bot trades with the newly arrived capital
6. All steps logged and audited in database

## Setup Steps

### Step 1: Link Bank Account to Alpaca

1. Go to **Alpaca Dashboard** → Settings → Linked Bank Accounts
2. Click "Link a Bank Account"
3. Add your US bank account (checking or savings)
4. Complete micro-deposit verification (2-3 business days)
5. **Save the Funding Method ID** (looks like: `aba_xxxx` or similar)

### Step 2: Link Same Bank Account to Coinbase

1. Go to **Coinbase Dashboard** → Settings → Payment Methods
2. Click "Add payment method" → "Bank Account"
3. Add the **SAME bank account** you linked to Alpaca
4. Complete verification (micro-deposits or ACH verification)
5. **Save the Funding Method UUID** (looks like: `abcd1234-5678-90ab-cdef-1234567890ab`)

### Step 3: Get Alpaca API Credentials with Withdrawal Permissions

1. Go to **Alpaca Dashboard** → Integrations → API Keys
2. Create or find your API key
3. **Ensure it has these permissions:**
   - Account data (read)
   - Positions (read)
   - Orders (read/write)
   - **Transfers (read/write) ← CRITICAL**
4. Copy the API Key ID and Secret Key

### Step 4: Get Coinbase API Credentials

1. Go to **Coinbase Developer Platform** → API Keys
2. Create a new API key
3. **Assign these permissions:**
   - accounts (read)
   - transfers (write) ← for ACH deposits
4. Download and save the private key (PEM or base64 format)

### Step 5: Configure Environment Variables

Set these in your `.env` file or Railway dashboard:

```bash
# Alpaca configuration (for prop_bot)
ALPACA_API_KEY=PK1234567890ABCDEF
ALPACA_SECRET_KEY=your-secret-key-here
ALPACA_FUNDING_METHOD_ID=aba_xxxxx_from_step_1

# Coinbase configuration (for crypto_bot)
COINBASE_API_KEY_NAME=your-key-name
COINBASE_API_PRIVATE_KEY=-----BEGIN EC PRIVATE KEY-----...
COINBASE_FUNDING_METHOD_ID=abcd1234-5678-90ab-cdef-1234567890ab
```

### Step 6: Verify the Setup

Run the test to confirm everything is wired up:

```bash
python test_bank_transfer_automation.py
```

Expected output:
```
✓ Database tables created/verified
✓ Alpaca withdrawal initiated: $100.00
✓ Coinbase deposit initiated: $100.00
✓ Full transfer pair initiated successfully
✓ Transfer logs recorded in database
✓ record_daily_earnings() completed
```

## How It Works in Production

### When Prop Bot Closes a Profitable Position

1. **Earnings are calculated** (e.g., $500 profit from trade)
2. **record_daily_earnings($500)** is called automatically
3. **Profit split:**
   - Worker (bot): $500 × 0.83 = **$415**
   - Platform: $500 × 0.17 = **$85**
4. **Auto-transfer trigger:**
   - Takes 50% of worker earnings: $415 × 0.50 = **$207.50**
   - Calls `initiate_transfer_for_earnings($207.50)`

### Alpaca Withdrawal Step

1. **Checks:** API credentials, funding method, amount > $1
2. **API call:** POST `/v1/account/transfers` to Alpaca
3. **Result:** Withdrawal initiated, ID returned
4. **Logs:** 
   ```
   [ALPACA_WITHDRAW] ✓ Initiated withdrawal: $207.50 | ID: transfer_abc123
   [AUTO_TRANSFER] 🏦 Initiating transfer of $207.50 from Alpaca to Coinbase
   ```

### Bank ACH Transfer (1-3 business days)

- Funds move from Alpaca's bank to your linked bank account
- Your bank processes the ACH credit
- Typically arrives within 1-3 business days (can be faster with same-day ACH if available)

### Coinbase Deposit Step

1. **Checks:** API credentials, funding method, amount > $1
2. **API call:** Triggers deposit from linked bank account
3. **Result:** Deposit initiated, ID returned
4. **Logs:**
   ```
   [COINBASE_DEPOSIT] ✓ Initiated deposit: $207.50 | ID: deposit_abc1
   [COINBASE_DEPOSIT] 💡 ACH transfer will arrive in 1-3 business days
   ```

### Crypto Bot Detects New Capital

1. `get_usd_balance()` reads Coinbase account balance
2. When deposit arrives (~1-3 days later), balance increases
3. **Crypto bot automatically uses new capital for trading:**
   - Position sizing scales with new capital
   - No manual configuration needed
   - Compounding kicks in automatically

### All Steps Logged in Database

**BankTransferLog table tracks:**
- transfer_id (unique ID for this transfer)
- step (alpaca_withdrawal_initiated, coinbase_deposit_initiated)
- amount_usd (how much transferred)
- external_id (Alpaca/Coinbase transaction ID)
- status (pending, processing, completed, failed)
- timestamp (when each step occurred)

Query example:
```sql
SELECT * FROM bank_transfer_logs 
WHERE transfer_id = 'transfer_20260815_213604'
ORDER BY timestamp;
```

## Troubleshooting

### "ALPACA_API_KEY or ALPACA_SECRET_KEY not configured"

**Fix:** Set environment variables in `.env` or Railway dashboard:
```bash
ALPACA_API_KEY=your-key
ALPACA_SECRET_KEY=your-secret
```

### "ALPACA_FUNDING_METHOD_ID not configured"

**Fix:** 
1. Go to Alpaca Dashboard → Settings → Linked Bank Accounts
2. Note the funding method ID (usually starts with `aba_`)
3. Set: `ALPACA_FUNDING_METHOD_ID=aba_xxxxx`

### "Withdrawal failed: HTTP 403"

**Cause:** API key doesn't have withdrawal permissions

**Fix:** 
1. Go to Alpaca Dashboard → API Keys
2. Edit the key to add "Transfers (write)" permission
3. Recreate credentials if needed

### "Withdrawal succeeded but never initiated"

**Cause:** Alpaca account may be on withdrawal restrictions (new account, regulatory hold, etc.)

**Fix:** 
1. Check Alpaca Dashboard → Account Status
2. Wait for restrictions to clear
3. Manual test: Try withdrawing via Alpaca dashboard UI

### "Transfer initiated but no Coinbase deposit"

**Cause:** Funds haven't arrived yet (ACH takes 1-3 business days)

**Fix:** Wait 1-3 business days for the ACH transfer to complete

**Verify:** 
- Alpaca: Check account transfers history
- Your bank: Verify the withdrawal debit posted
- Coinbase: Check deposit attempts in payment methods

### "Coinbase deposit rejected: Invalid funding method"

**Cause:** Funding method ID doesn't match your Coinbase account

**Fix:**
1. Go to Coinbase → Settings → Payment Methods
2. Verify the bank account is linked and verified
3. Copy the correct UUID from Coinbase
4. Set: `COINBASE_FUNDING_METHOD_ID=abcd1234-5678-90ab-cdef-1234567890ab`

## Safety Features

1. **Minimum amounts:** Won't initiate transfers < $1
2. **Credential validation:** Checks environment variables before API calls
3. **Error handling:** Graceful failures with clear error messages
4. **Audit trail:** All transfers logged in database
5. **Separation of concerns:** Alpaca and Coinbase operations independent

## Monitoring

### Check transfer status:

```bash
# See all recent transfers
curl http://localhost:8000/admin/bank-transfers

# See specific transfer
curl http://localhost:8000/admin/bank-transfers/transfer_20260815_213604
```

### Check database directly:

```sql
-- All transfers this week
SELECT transfer_id, step, amount_usd, status, timestamp 
FROM bank_transfer_logs 
WHERE timestamp > datetime('now', '-7 days')
ORDER BY timestamp DESC;

-- Summary by transfer
SELECT transfer_id, count(*) as steps, sum(amount_usd) as total_amount, max(timestamp) as latest
FROM bank_transfer_logs 
GROUP BY transfer_id
ORDER BY max(timestamp) DESC;
```

## Limitations

1. **ACH timing:** 1-3 business days (not instant)
2. **Weekend delays:** ACH doesn't process on weekends
3. **Same bank account required:** Must be linked to both brokers
4. **Broker limits:** Alpaca/Coinbase may have daily/monthly withdrawal limits
5. **Regulatory holds:** New accounts may have withdrawal restrictions

## What Happens Next

Once this is enabled with real credentials:

1. **Day 1:** Prop bot closes profitable trades → earnings recorded → transfer initiated
2. **Day 2-3:** ACH transfer in flight
3. **Day 3-4:** Funds arrive in Coinbase
4. **Day 4+:** Crypto bot trades with combined capital
5. **Repeats:** Every profitable day, cycle continues automatically

## Testing with Real Credentials

When you have credentials set:

1. Run test with real env vars: `python test_bank_transfer_automation.py`
2. Verify logs show "✓ Withdrawal initiated" and "✓ Deposit initiated"
3. Check Alpaca dashboard → Transfers for the withdrawal
4. Check Coinbase → Payment Methods → Deposits for the deposit request
5. Monitor database logs: `SELECT * FROM bank_transfer_logs ORDER BY timestamp DESC LIMIT 10;`

## Questions?

See:
- `/home/user/empire-v2/bank_transfer_automation.py` — implementation
- `/home/user/empire-v2/test_bank_transfer_automation.py` — test suite
- `/home/user/empire-v2/prop_bot.py` — where transfers are initiated
- `/home/user/empire-v2/models.py` — BankTransferLog model

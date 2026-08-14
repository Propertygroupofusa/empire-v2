# REAL-TIME REVENUE TRACKING GUIDE

## Where to Check Real Money

### 1. LIVE DASHBOARD (Auto-refreshes every 10 seconds)
**URL:** https://empire-v2-production.up.railway.app/dashboard/html

**What you'll see:**
- 💵 Total Earned (cumulative real money from Stripe)
- ✅ Paid Out (money actually transferred to bots)
- ⏳ Pending Payout (money waiting to be transferred)
- 📊 Jobs Completed (total orders processed)
- 🤖 Active Bots (how many are working)

**Individual Bot Stats:**
- Bot earnings breakdown
- Jobs per bot
- Pending vs paid out per bot

**Recent Payments Table:**
- Every order shows: worker, amount, status, date

---

## 2. STRIPE DASHBOARD (Official record)
**URL:** https://dashboard.stripe.com

**Login with:** Your Stripe account

**What to verify:**
- Charges (incoming payments from customers)
- Transfers (outgoing payouts to bots)
- Balance (money available)

---

## REAL MONEY FLOW (What Happens Every Order)

1. **Customer pays** (0 sec)
   - Stripe receives payment
   - Dashboard shows: 💵 Total Earned +$X

2. **Bot processes order** (1-30 min)
   - Bot completes the work
   - Payment status: "pending payout"
   - Dashboard shows: ⏳ Pending Payout +$X

3. **Payout processed** (every 30 seconds)
   - System initiates Stripe transfer
   - Payment status: "paid"
   - Dashboard shows: ✅ Paid Out +$X

---

## DAY 1 REVENUE GOALS

**Realistic expectations:**
- First customer: 12-48 hours after marketing
- Week 1 goal: 5-10 customers = $375-1,500 revenue
- Week 2 goal: Double the channel bringing best results

**Success metrics:**
- ✅ Any money coming in (even $50) = system working
- ✅ Multiple orders = marketing working
- ✅ Dashboard showing real payouts = bots completing work

---

## MONITORING CHECKLIST

**Every 2 hours:**
- [ ] Check dashboard at `/dashboard/html`
- [ ] Note any new orders (quote form visits)
- [ ] Track which marketing channel each came from

**Every 6 hours:**
- [ ] Log earnings total in a spreadsheet
- [ ] Note bot activity (jobs per bot)
- [ ] Identify highest-performing marketing channel

**End of day:**
- [ ] Total revenue earned
- [ ] Total orders completed
- [ ] Which channel won (FB, LinkedIn, Email)
- [ ] Plan next day: Double down on winning channel

---

## IF NO MONEY SHOWS UP

**First 24 hours:** Normal - marketing takes time to reach people

**After 48 hours:** Check these things:

1. **Marketing was actually posted?**
   - Did posts go live on Facebook?
   - Did emails actually send?
   - Can you see the messages?

2. **Quote form URL working?**
   - Visit: https://empire-v2-production.up.railway.app/quote
   - Can you fill it out? Can you submit?

3. **Dashboard accessible?**
   - Visit: https://empire-v2-production.up.railway.app/dashboard/html
   - Does it load and show bots?

4. **Stripe keys LIVE (not test)?**
   - Check .env or Railway variables
   - STRIPE_SECRET_KEY starts with `sk_live_` (not `sk_test_`)
   - STRIPE_PUBLISHABLE_KEY starts with `pk_live_` (not `pk_test_`)

---

## REAL MONEY TRACKING TEMPLATE

Copy this and update it as orders come in:

```
DAY 1 - [DATE]
Total Revenue: $0
Orders: 0
Source: -

DAY 2 - [DATE]  
Total Revenue: $X
Orders: N
Source: [FB/LinkedIn/Email] brought [N] customers
Notes: [What worked]

DAY 3 - [DATE]
Total Revenue: $X
Orders: N
Source: [Best channel]
Notes: [Plan for next 4 days]
```

---

## SUCCESS CRITERIA

✅ You'll know it's working when:
1. Dashboard shows orders coming in
2. Bots auto-scale (more bots appear as queue grows)
3. Stripe shows real payouts being transferred
4. Revenue compounds day over day

The faster you execute the marketing, the faster the money flows.

**Go execute. Report back with revenue numbers. Let's grow this.** 💸

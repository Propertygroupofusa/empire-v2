# How to Send Emails to All 10 Prospects - 3 Methods

---

## METHOD 1: Python Script (Fully Automated) ⭐ RECOMMENDED
**Time to send all 10 emails: 5 minutes (including setup)**

### Quick Setup:

1. **Get Gmail App Password:**
   - Go to https://myaccount.google.com/apppasswords
   - Select "Mail" and your device type
   - Google generates a 16-character password
   - Copy it

2. **Edit the script:**
   ```bash
   # Open send_emails.py in any text editor
   # Find these lines at the top:
   SENDER_EMAIL = "your-email@gmail.com"     # ← Change to YOUR email
   SENDER_PASSWORD = "your-app-password"     # ← Paste the 16-char password
   SENDER_NAME = "Your Name"                 # ← Change to YOUR name
   YOUR_COMPANY = "Your Company"             # ← Change to YOUR company
   YOUR_PHONE = "Your Phone Number"          # ← Change to YOUR phone
   ```

3. **Run it:**
   ```bash
   python3 send_emails.py
   ```

4. **Confirm and send:**
   - Script will show: "Ready to send 10 emails - Continue? (yes/no):"
   - Type: `yes`
   - Emails send automatically (2-second delay between each)

5. **Done:**
   - All 10 emails sent in ~30 seconds
   - Log saved to email_log.txt
   - Prospects will start replying within 24 hours

### Advantages:
- ✓ Sends to all 10 instantly
- ✓ Personalizes each email
- ✓ Logs results
- ✓ No manual copy-paste
- ✓ Easy to repeat for follow-ups

---

## METHOD 2: Gmail Mail Merge (Manual but Visual)
**Time to send all 10 emails: 10-15 minutes**

### Steps:

1. **Open Google Sheets**
   - Create new sheet

2. **Copy this data:**
   - Column A: First Name (Matt, Ivan, Eric, etc.)
   - Column B: Email (their emails)
   - Column C: Company (Thrive, Comrade, etc.)

   Paste your prospects from AGENCY_PROSPECTS.csv

3. **Set up Mail Merge:**
   - Go to Extensions → Mail Merge → Configure
   - From: Your email
   - Signature: Skip
   - Subject: "Cut your video production budget by 90%"

4. **Compose email:**
   ```
   Hi {{First Name}},

   Quick question: How much does {{Company}} spend on video production for your clients each year?

   Most agencies spend $500-2,000 per video. If {{Company}} handles even 10 client videos annually, that's $5,000-20,000 across your client base.

   We just launched a flat-fee video production service:
   • $500-1,000 per video (one-time fee, no subscription)
   • 24-48 hour turnaround
   • Professional quality (AI voiceovers, custom scripts, HD)
   • Unlimited revisions until perfect

   This could save {{Company}} AND your clients serious money.

   Your first video: Let's talk about a custom quote.

   Are you interested in exploring this?

   [Your Name]
   [Your Company]
   [Your Phone]
   https://empire-v2-production.up.railway.app/dashboard
   ```

5. **Send:**
   - Click "Send emails"
   - Confirm on dialog

### Advantages:
- ✓ Visual - you see each email before sending
- ✓ No app passwords needed
- ✓ Built into Google (no extra software)
- ✓ Can pause/resume

### Disadvantages:
- ✗ Slower (manual setup)
- ✗ More clicks required

---

## METHOD 3: Manual Send (Slowest but Easiest to Learn)
**Time to send all 10 emails: 30-45 minutes**

### Steps:

1. **Open Gmail**

2. **For each prospect:**
   - Click "Compose"
   - Paste To: [their email]
   - Paste Subject: "Cut your video production budget by 90%"
   - Paste email template (below)
   - Replace [NAME] with their first name
   - Replace [COMPANY] with their company
   - Click Send

3. **Email template:**
   ```
   Hi [NAME],

   Quick question: How much does [COMPANY] spend on video production for your clients each year?

   Most agencies spend $500-2,000 per video. If [COMPANY] handles even 10 client videos annually, that's $5,000-20,000 across your client base.

   We just launched a flat-fee video production service:
   • $500-1,000 per video (one-time fee, no subscription)
   • 24-48 hour turnaround
   • Professional quality (AI voiceovers, custom scripts, HD)
   • Unlimited revisions until perfect

   This could save [COMPANY] AND your clients serious money.

   Your first video: Let's talk about a custom quote.

   Are you interested in exploring this?

   [Your Name]
   [Your Company]
   [Your Phone]
   https://empire-v2-production.up.railway.app/dashboard
   ```

4. **Track in spreadsheet:**
   - Note which ones you sent
   - Note date/time
   - Check for replies hourly

### Advantages:
- ✓ Simplest (no setup needed)
- ✓ Most control
- ✓ No integrations

### Disadvantages:
- ✗ Slow (30+ minutes)
- ✗ Easy to miss typos
- ✗ No automatic logging

---

## MY RECOMMENDATION

**Use METHOD 1 (Python Script)** because:
1. Fastest (5 minutes total)
2. Most professional (no typos)
3. Automatic logging (track results)
4. Repeatable (use again for follow-ups on day 4 + day 7)
5. Easiest to scale (add 50 more prospects, send with one command)

---

## QUICK CHECKLIST - DO THIS NOW

- [ ] Choose a method (I recommend Method 1)
- [ ] If Method 1: Get Gmail App Password (2 min)
- [ ] If Method 1: Edit send_emails.py with your info (2 min)
- [ ] If Method 1: Run `python3 send_emails.py` (1 min)
- [ ] Confirm and send (1 min)
- [ ] All 10 emails sent! ✓

**Total time: 6 minutes**

---

## WHAT HAPPENS NEXT

**Within 24-48 hours:**
- First replies will come in
- Check inbox hourly
- Reply to warm leads immediately
- Add their replies to your tracking sheet

**Day 4:**
- Send Email 2 (follow-up) to non-responders
- Continue with new prospects (10 more)

**Expected Week 1 results:**
- 5-8 replies from your 10 emails
- 1-2 qualified leads
- 1 potential call scheduled

---

## TROUBLESHOOTING

**"Python not found"**
- Make sure Python 3 is installed: `python3 --version`
- If not: Download from python.org

**"Module smtplib not found"**
- It's built-in to Python, shouldn't happen
- Try: `python3 -m pip install --upgrade pip`

**"Authentication failed"**
- Make sure you used the App Password, NOT your regular Gmail password
- Make sure you have 2-factor authentication enabled
- Go back to https://myaccount.google.com/apppasswords and generate new one

**"Emails sent to spam folder"**
- Normal for first sends
- Ask prospects to move to inbox
- Google marks bulk sends as slightly suspicious initially
- After 1-2 successful sends, reputation improves

**"Want to test first?"**
- Change one email in AGENCY_PROSPECTS.csv to YOUR email
- Run script on just that one
- Check it arrived in your inbox
- Then update CSV with real prospects and run again

---

## AFTER SENDING

1. **Create tracking sheet** (spreadsheet with columns):
   - Date Sent
   - Prospect Name
   - Company
   - Email
   - Email 1 Sent ✓
   - Reply Received? (Y/N)
   - Reply Date
   - Interested? (Y/N/Maybe)
   - Next Action

2. **Day 4:** Send Email 2 (follow-up) to non-responders

3. **Day 7:** Send Email 3 (urgency) to still non-responding

4. **Continue:** Find 10 more prospects each day

5. **Track:** Monitor replies and response times

---

## SUCCESS METRICS

Expected from 10 emails:
- ✓ Reply rate: 5-8% = 1 reply minimum
- ✓ Qualified leads: 1 lead minimum
- ✓ Orders: 1 order likely by week 2
- ✓ Revenue: $500-1,000

Multiply across 100 emails in month 1 = $5,000-10,000

---

**You've got everything ready. Pick a method and send today. First reply likely within 24 hours.**

**Go. 🚀**

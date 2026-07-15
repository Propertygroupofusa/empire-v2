#!/usr/bin/env python3
"""
Email Campaign Sender for Agency Prospects
Sends Email 1 from the outreach sequence to all prospects
"""

import smtplib
import csv
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
import time

# ============================================================
# CONFIGURATION - UPDATE THESE WITH YOUR DETAILS
# ============================================================

SENDER_EMAIL = "your-email@gmail.com"  # Your Gmail or email address
SENDER_PASSWORD = "your-app-password"  # Gmail App Password (NOT your regular password)
SENDER_NAME = "Your Name"              # Your name to show in From field
YOUR_COMPANY = "Your Company"          # Your company name
YOUR_PHONE = "Your Phone Number"       # Your phone number

# Email service settings
SMTP_SERVER = "smtp.gmail.com"  # Gmail SMTP
SMTP_PORT = 587                 # Gmail port (TLS)

# Prospect file
PROSPECTS_CSV = "AGENCY_PROSPECTS.csv"

# ============================================================
# EMAIL TEMPLATE - Email 1 from the sequence
# ============================================================

EMAIL_TEMPLATE = """Hi {first_name},

Quick question: How much does {company_name} spend on video production for your clients each year?

Most agencies spend $500-2,000 per video. If {company_name} handles even 10 client videos annually, that's $5,000-20,000 across your client base.

We just launched a flat-fee video production service:
• $500-1,000 per video (one-time fee, no subscription)
• 24-48 hour turnaround
• Professional quality (AI voiceovers, custom scripts, HD)
• Unlimited revisions until perfect

Perfect for:
- YouTube channel growth
- Course content
- Social media marketing
- Product demos
- Testimonial videos

This could save {company_name} AND your clients serious money.

Your first video: Let's talk about a custom quote.

Are you interested in exploring this?

{sender_name}
{sender_company}
{sender_phone}
https://empire-v2-production.up.railway.app/dashboard"""

SUBJECT_LINE = "Cut your video production budget by 90%"

# ============================================================
# FUNCTIONS
# ============================================================

def send_email(recipient_email, recipient_name, company_name):
    """Send email to a single prospect"""
    try:
        # Personalize email
        email_body = EMAIL_TEMPLATE.format(
            first_name=recipient_name,
            company_name=company_name,
            sender_name=SENDER_NAME,
            sender_company=YOUR_COMPANY,
            sender_phone=YOUR_PHONE
        )

        # Create message
        msg = MIMEMultipart()
        msg['From'] = f"{SENDER_NAME} <{SENDER_EMAIL}>"
        msg['To'] = recipient_email
        msg['Subject'] = SUBJECT_LINE

        msg.attach(MIMEText(email_body, 'plain'))

        # Send via SMTP
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()  # Secure connection
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.send_message(msg)

        print(f"✓ Sent to {recipient_name} ({recipient_email}) at {company_name}")
        return True

    except Exception as e:
        print(f"✗ Failed to send to {recipient_name} ({recipient_email}): {str(e)}")
        return False


def load_prospects(csv_file):
    """Load prospect list from CSV"""
    prospects = []
    try:
        with open(csv_file, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row['Email'] and row['Email'].strip():
                    prospects.append(row)
        print(f"Loaded {len(prospects)} prospects from {csv_file}\n")
        return prospects
    except FileNotFoundError:
        print(f"Error: {csv_file} not found!")
        return []


def send_campaign():
    """Send emails to all prospects"""

    print("=" * 60)
    print("AGENCY PROSPECT EMAIL CAMPAIGN - Email 1")
    print("=" * 60)
    print(f"From: {SENDER_NAME} <{SENDER_EMAIL}>")
    print(f"Subject: {SUBJECT_LINE}")
    print("=" * 60 + "\n")

    # Load prospects
    prospects = load_prospects(PROSPECTS_CSV)

    if not prospects:
        print("No prospects to send to!")
        return

    # Confirm before sending
    print(f"Ready to send {len(prospects)} emails")
    confirm = input("Continue? (yes/no): ").lower().strip()

    if confirm != "yes":
        print("Cancelled.")
        return

    # Send to each prospect
    sent_count = 0
    failed_count = 0

    print(f"\nSending emails...\n")

    for prospect in prospects:
        email = prospect['Email'].strip()
        first_name = prospect['First Name'].strip()
        company = prospect['Company'].strip()

        if not email:
            print(f"⊘ Skipped {first_name} - no email address")
            continue

        if send_email(email, first_name, company):
            sent_count += 1
        else:
            failed_count += 1

        # Be nice to email servers - wait between sends
        time.sleep(2)

    # Summary
    print(f"\n" + "=" * 60)
    print(f"CAMPAIGN COMPLETE")
    print(f"Sent: {sent_count}")
    print(f"Failed: {failed_count}")
    print(f"Total: {sent_count + failed_count}")
    print(f"=" * 60)

    # Log results
    with open('email_log.txt', 'a') as log:
        log.write(f"\n{datetime.now().isoformat()} - Sent {sent_count}/{len(prospects)} emails\n")

    print(f"\nLog saved to email_log.txt")


# ============================================================
# SETUP INSTRUCTIONS
# ============================================================

SETUP_INSTRUCTIONS = """
GMAIL SETUP INSTRUCTIONS (Required to send emails):

1. Go to: https://myaccount.google.com/apppasswords
   (Or: Google Account > Security > App passwords)

2. Select "Mail" and "Windows Computer" (or your device)

3. Google will generate a 16-character password

4. Copy that password into this script at the top:
   SENDER_PASSWORD = "paste-16-char-password-here"

5. Also update SENDER_EMAIL with your Gmail address

6. Save this script and run:
   python3 send_emails.py

IMPORTANT NOTES:
- Use an App Password, NOT your regular Gmail password
- App passwords only work with 2-factor authentication enabled
- Emails are sent with 2-second delays between each (server friendly)
- A log is saved to email_log.txt after each run

TESTING (Optional):
- Send to one email first (your own) to test the setup
- Check spam folder if emails don't arrive
- Gmail may flag first sends as "less secure" - that's normal
"""

# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    # Check if configured
    if SENDER_EMAIL == "your-email@gmail.com" or SENDER_PASSWORD == "your-app-password":
        print(SETUP_INSTRUCTIONS)
        print("\n⚠ SETUP REQUIRED - Update SENDER_EMAIL and SENDER_PASSWORD at the top of this script")
    else:
        send_campaign()

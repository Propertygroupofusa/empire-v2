#!/usr/bin/env python3
"""
Send referral campaign to agencies to restart revenue pipeline
Uses existing email infrastructure with personalized video production offer
"""

import smtplib
import csv
import os
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

# Configuration from environment
SENDER_EMAIL = os.getenv("GMAIL_EMAIL", "")
SENDER_PASSWORD = os.getenv("GMAIL_PASSWORD", "")
SENDER_NAME = "Property Group of USA"
COMPANY_URL = "https://empire-v2-production.up.railway.app"

# Email template for agency referral
REFERRAL_EMAIL_TEMPLATE = """Hi {first_name},

Quick opportunity for {company_name}:

We just launched a flat-fee video production service perfect for agencies:
• $500-$1,200 per video (one-time fee, no subscription)
• 24-48 hour turnaround
• Professional avatars, custom scripts, HD quality
• Unlimited revisions until perfect

Revenue opportunity for agencies:
✅ Offer to your clients at 2-3x markup
✅ We handle production - you keep the relationship
✅ $100 referral bonus per successful client

Perfect for:
- YouTube content (channel growth)
- Course videos (employee training, product)
- Sales/pitch videos (B2B demos)
- Social media content (LinkedIn, TikTok)
- Testimonial videos

Your first video: Let's discuss a custom quote.
{company_url}/quote

This could become a new revenue stream for {company_name}.

Are you interested?

Best,
{sender_name}
Property Group of USA
{company_url}
"""

def send_referral_email(recipient_email, recipient_name, company_name):
    """Send referral email to agency"""
    try:
        email_body = REFERRAL_EMAIL_TEMPLATE.format(
            first_name=recipient_name,
            company_name=company_name,
            sender_name=SENDER_NAME,
            company_url=COMPANY_URL
        )

        msg = MIMEMultipart("alternative")
        msg["Subject"] = "New Revenue Stream: Video Production for Your Clients"
        msg["From"] = f"{SENDER_NAME} <{SENDER_EMAIL}>"
        msg["To"] = recipient_email

        part = MIMEText(email_body, "plain")
        msg.attach(part)

        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.sendmail(SENDER_EMAIL, recipient_email, msg.as_string())

        print(f"✅ Sent to {recipient_name} ({company_name})")
        return True

    except Exception as e:
        print(f"❌ Failed {recipient_name} ({company_name}): {str(e)}")
        return False

def launch_campaign():
    """Launch referral campaign to agencies"""
    print("=" * 60)
    print("🚀 AGENCY REFERRAL CAMPAIGN")
    print("=" * 60)

    # Check Gmail config
    if not SENDER_EMAIL or not SENDER_PASSWORD:
        print("\n⚠️  GMAIL NOT CONFIGURED")
        print("   Set environment variables:")
        print("   export GMAIL_EMAIL='your-email@gmail.com'")
        print("   export GMAIL_PASSWORD='your-app-password'")
        print("\n   See: https://myaccount.google.com/apppasswords")
        return

    # Load agencies
    agencies = []
    try:
        with open("referral_campaign_list.csv", "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row["Email"] and row["Email"].strip():
                    agencies.append(row)
    except FileNotFoundError:
        print("❌ referral_campaign_list.csv not found")
        return

    print(f"\n📧 Target: {len(agencies)} agencies")
    print(f"From: {SENDER_NAME} <{SENDER_EMAIL}>")
    print("=" * 60 + "\n")

    # Confirm before sending
    confirm = input(f"Send campaign to {len(agencies)} agencies? (yes/no): ").lower().strip()
    if confirm != "yes":
        print("Cancelled.")
        return

    # Send campaign
    sent = 0
    failed = 0

    print(f"\nSending referral emails...\n")

    for agency in agencies:
        email = agency["Email"].strip()
        first_name = agency["First Name"].strip()
        company = agency["Company"].strip()

        if send_referral_email(email, first_name, company):
            sent += 1
        else:
            failed += 1

        time.sleep(2)  # Rate limit

    # Summary
    print(f"\n" + "=" * 60)
    print(f"✅ Campaign Complete")
    print(f"📤 Sent: {sent}/{len(agencies)}")
    print(f"❌ Failed: {failed}")
    print(f"=" * 60)
    print(f"\n💰 Revenue Pipeline: RESTARTED")
    print(f"📊 Monitor incoming quote requests")
    print(f"🤖 Bots will process orders automatically")

    # Log campaign
    with open("campaign_log.txt", "a") as log:
        log.write(f"\n{datetime.now().isoformat()} - Agency referral campaign: {sent}/{len(agencies)} sent\n")

if __name__ == "__main__":
    launch_campaign()

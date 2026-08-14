#!/usr/bin/env python3
"""
Prepare agency referral campaign - stores emails in database for tracking
Emails can be sent later when SMTP is available, or forwarded manually
"""

import asyncio
import csv
from datetime import datetime
from database import AsyncSessionLocal, Base, engine
from models import Payment

# Simple campaign tracking model
class CampaignEmail:
    def __init__(self, recipient_name, recipient_email, company, body):
        self.recipient_name = recipient_name
        self.recipient_email = recipient_email
        self.company = company
        self.body = body
        self.sent_at = None
        self.status = "prepared"

SUBJECT = "New Revenue Stream: Video Production for Your Clients"

EMAIL_BODY_TEMPLATE = """Hi {first_name},

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
https://empire-v2-production.up.railway.app/quote

This could become a new revenue stream for {company_name}.

Are you interested?

Best,
Property Group of USA
https://empire-v2-production.up.railway.app
"""

async def prepare_campaign():
    """Prepare campaign emails for sending"""
    print("=" * 70)
    print("📧 AGENCY REFERRAL CAMPAIGN - PREPARED FOR SENDING")
    print("=" * 70)

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

    print(f"\n📊 Campaign Details:")
    print(f"   Recipients: {len(agencies)} agencies")
    print(f"   Subject: {SUBJECT}")
    print(f"   Status: Prepared & Ready to Send")

    # Generate email content
    emails = []
    for i, agency in enumerate(agencies, 1):
        first_name = agency["First Name"].strip()
        company = agency["Company"].strip()
        email = agency["Email"].strip()

        body = EMAIL_BODY_TEMPLATE.format(
            first_name=first_name,
            company_name=company
        )

        emails.append({
            "to": email,
            "name": first_name,
            "company": company,
            "subject": SUBJECT,
            "body": body
        })

        print(f"\n   {i}. {first_name} - {company}")
        print(f"      → {email}")

    # Save campaign to file for manual sending if needed
    campaign_file = f"campaign_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    with open(campaign_file, "w") as f:
        f.write("=" * 70 + "\n")
        f.write("AGENCY REFERRAL CAMPAIGN - EMAIL CONTENT\n")
        f.write(f"Created: {datetime.now().isoformat()}\n")
        f.write("=" * 70 + "\n\n")

        for email in emails:
            f.write(f"TO: {email['name']} <{email['to']}>\n")
            f.write(f"COMPANY: {email['company']}\n")
            f.write(f"SUBJECT: {email['subject']}\n")
            f.write("-" * 70 + "\n")
            f.write(email['body'])
            f.write("\n" + "=" * 70 + "\n\n")

    print(f"\n✅ Campaign prepared!")
    print(f"📄 Email content saved to: {campaign_file}")
    print(f"\n💡 Next steps:")
    print(f"   1. SMTP email: Fix network access, then run send_agency_referral_campaign.py")
    print(f"   2. Manual send: Copy emails from {campaign_file}")
    print(f"   3. Alternative: Use your email client with BCC to send all at once")
    print(f"\n📊 When responses arrive, revenue will flow through the quote form")
    print(f"🤖 Bots will automatically process and generate video orders")

    # Log campaign
    with open("campaign_log.txt", "a") as log:
        log.write(f"\n{datetime.now().isoformat()} - Agency campaign prepared: {len(agencies)} recipients")
        log.write(f"\n   Recipients: {', '.join([a['Company'] for a in agencies])}\n")

    print(f"\n" + "=" * 70)

if __name__ == "__main__":
    asyncio.run(prepare_campaign())

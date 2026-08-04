#!/usr/bin/env python3
"""
AI Avatar Video Production - Email Campaign
Personalized outreach to 58 agencies & SaaS companies
Run this locally on your machine: python send_campaign.py

SETUP:
1. Gmail account with app password enabled:
   - https://myaccount.google.com/apppasswords
   - Generate app password for "Mail" + "Windows Computer"
   - Copy the 16-character password

2. Set environment variables before running:
   export GMAIL_EMAIL="your-gmail@gmail.com"
   export GMAIL_PASSWORD="xxxx xxxx xxxx xxxx"  # 16 char app password

3. Run:
   python send_campaign.py
"""

import csv
import os
import smtplib
import json
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

# Gmail credentials (set via env vars)
GMAIL_EMAIL = os.getenv("GMAIL_EMAIL")
GMAIL_PASSWORD = os.getenv("GMAIL_PASSWORD")

if not GMAIL_EMAIL or not GMAIL_PASSWORD:
    print("ERROR: Set GMAIL_EMAIL and GMAIL_PASSWORD environment variables")
    print("  export GMAIL_EMAIL='your-email@gmail.com'")
    print("  export GMAIL_PASSWORD='16-char app password'")
    exit(1)

# Campaign metadata
CAMPAIGN_NAME = "AI Avatar Video Production"
FROM_NAME = "Property Group USA"
SUBJECT_TEMPLATE = "{name} - AI Avatar Videos for {company}"
LANDING_PAGE = "https://empire-v2-production.up.railway.app/quote"

# Email templates by company type
AGENCY_EMAIL_TEMPLATE = """Hi {name},

I wanted to reach out about a new service we're offering that agencies are loving: AI-generated avatar videos.

We've built a platform that creates professional spokesperson videos instantly - no actors, no filming, no production delays. Perfect for client deliverables:
- Explainer videos
- Sales page videos
- Tutorial content
- Customer testimonials
- Marketing campaigns

Your clients get broadcast-quality videos in 24-48 hours for a fraction of traditional production costs. We handle everything from script to delivery.

Check out the quick quote form here: {landing_page}

Would love to chat about how this could become a new revenue stream for {company}.

Best,
Property Group USA
Video Production Platform
"""

SAAS_EMAIL_TEMPLATE = """Hi {name},

Quick question: how's {company} handling video content for your users?

We just launched an AI video platform that lets companies embed AI spokesperson videos into their product/platform - with zero production overhead. Your users can generate custom videos instantly:
- Personalized announcements
- Product walkthroughs
- Customer success stories
- Training content
- Multilingual support (22 languages)

It's designed to integrate into SaaS workflows - customers submit a script, we generate the video, they download it.

If {company} supports video generation for your users, we'd love to explore a partnership:
{landing_page}

Curious if this is on your roadmap?

Best,
Property Group USA
"""

def load_agencies():
    """Load agency contacts from CSV"""
    agencies = []
    try:
        with open("AGENCY_PROSPECTS.csv", "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("Email") and "@" in row["Email"]:
                    agencies.append({
                        "type": "agency",
                        "name": row.get("First Name", "there"),
                        "company": row.get("Company", "your company"),
                        "email": row["Email"].strip(),
                        "title": row.get("Contact Method", "Email"),
                    })
    except FileNotFoundError:
        print("WARNING: AGENCY_PROSPECTS.csv not found")
    return agencies

def load_saas():
    """Load SaaS contacts from CSV"""
    saas = []
    try:
        with open("saas_prospects.csv", "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("Email Guess") and "@" in row["Email Guess"]:
                    saas.append({
                        "type": "saas",
                        "name": row.get("Contact Title", "there"),
                        "company": row.get("Company Name", "your company"),
                        "email": row["Email Guess"].strip(),
                        "industry": row.get("Industry", ""),
                    })
    except FileNotFoundError:
        print("WARNING: saas_prospects.csv not found")
    return saas

def send_email(to_email, subject, body):
    """Send email via Gmail SMTP"""
    try:
        msg = MIMEMultipart()
        msg["From"] = f"{FROM_NAME} <{GMAIL_EMAIL}>"
        msg["To"] = to_email
        msg["Subject"] = subject

        msg.attach(MIMEText(body, "plain"))

        server = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10)
        server.login(GMAIL_EMAIL, GMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()

        return {"status": "sent", "email": to_email}
    except Exception as e:
        return {"status": "failed", "email": to_email, "error": str(e)}

def main():
    """Run email campaign"""
    print(f"\n{'='*70}")
    print(f"  {CAMPAIGN_NAME} - Email Campaign")
    print(f"  Sending {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"{'='*70}\n")

    # Load contacts
    agencies = load_agencies()
    saas = load_saas()
    all_contacts = agencies + saas

    print(f"Loaded {len(agencies)} agencies + {len(saas)} SaaS contacts")
    print(f"Total: {len(all_contacts)} recipients\n")

    if not all_contacts:
        print("ERROR: No contacts loaded. Check CSV files exist and have data.")
        exit(1)

    # Send campaign
    results = []
    sent = 0
    failed = 0

    for i, contact in enumerate(all_contacts, 1):
        # Personalize subject and body
        name = contact["name"]
        company = contact["company"]
        email = contact["email"]
        contact_type = contact["type"]

        subject = SUBJECT_TEMPLATE.format(name=name, company=company)

        if contact_type == "agency":
            body = AGENCY_EMAIL_TEMPLATE.format(
                name=name,
                company=company,
                landing_page=LANDING_PAGE
            )
        else:  # saas
            body = SAAS_EMAIL_TEMPLATE.format(
                name=name,
                company=company,
                landing_page=LANDING_PAGE
            )

        print(f"[{i:2d}/{len(all_contacts)}] Sending to {email} ({company})...", end=" ")

        result = send_email(email, subject, body)
        results.append(result)

        if result["status"] == "sent":
            print("✓ SENT")
            sent += 1
        else:
            print(f"✗ FAILED: {result.get('error', 'unknown error')}")
            failed += 1

    # Summary
    print(f"\n{'='*70}")
    print(f"  Campaign Complete")
    print(f"{'='*70}")
    print(f"  Sent:   {sent} emails")
    print(f"  Failed: {failed} emails")
    print(f"  Total:  {len(all_contacts)} recipients")
    print(f"  Rate:   {round(sent/len(all_contacts)*100, 1)}% success\n")

    # Save results
    output_file = f"campaign_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(output_file, "w") as f:
        json.dump({
            "timestamp": datetime.now().isoformat(),
            "campaign": CAMPAIGN_NAME,
            "total": len(all_contacts),
            "sent": sent,
            "failed": failed,
            "success_rate": round(sent/len(all_contacts)*100, 1),
            "results": results
        }, f, indent=2)

    print(f"Results saved to: {output_file}\n")

    if failed > 0:
        print("TROUBLESHOOTING FAILED EMAILS:")
        print("- Check Gmail app password is correct (16 chars, spaces included)")
        print("- Verify 'Less secure app access' is enabled (if using regular password)")
        print("- Check firewall/proxy isn't blocking smtp.gmail.com:465")
        print("- Retry with: python send_campaign.py\n")

if __name__ == "__main__":
    main()

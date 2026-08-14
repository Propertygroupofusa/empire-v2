#!/usr/bin/env python3
"""
Launch referral campaign to agency prospects
Adds them to campaign tracking and sends personalized referral emails
"""

import asyncio
import os
from email_campaigns import add_customer_to_campaign, send_campaign_batch
import csv

async def launch_campaign():
    print("🚀 LAUNCHING AGENCY REFERRAL CAMPAIGN")
    print("=" * 60)

    # Load agencies from referral_campaign_list.csv
    agencies = []
    try:
        with open('referral_campaign_list.csv', 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row['Email'] and row['Email'].strip():
                    agencies.append(row)
        print(f"✅ Loaded {len(agencies)} agencies")
    except FileNotFoundError:
        print("❌ referral_campaign_list.csv not found")
        return

    # Add each agency to campaign
    print(f"\n📧 Adding {len(agencies)} agencies to campaign...")
    for agency in agencies:
        first_name = agency.get('First Name', 'Contact')
        email = agency.get('Email', '').strip()
        company = agency.get('Company', 'Unknown')

        if email:
            await add_customer_to_campaign(first_name, email, company)
            await asyncio.sleep(0.5)  # Rate limit

    print(f"\n✅ All agencies added to campaign database")

    # Send campaign batch
    print(f"\n📤 Sending referral emails...")
    await send_campaign_batch(batch_size=100)

    print(f"\n✅ Campaign launch complete!")
    print(f"   Revenue pipeline: RESTARTED")
    print(f"   Next: Monitor for responses and new orders")

if __name__ == "__main__":
    # Check Gmail config
    if not os.getenv("GMAIL_EMAIL") or not os.getenv("GMAIL_PASSWORD"):
        print("⚠️  Note: Gmail not configured - emails won't send yet")
        print("   Set GMAIL_EMAIL and GMAIL_PASSWORD environment variables")
        print("   Then run: python3 launch_agency_referral_campaign.py")

    asyncio.run(launch_campaign())

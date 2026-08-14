#!/usr/bin/env python3
"""
Send referral email campaign to past customers
Usage: python3 send_referral_campaign.py
"""

import asyncio
import logging
from email_campaigns import send_campaign_batch

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("send_campaign")

async def main():
    log.info("🚀 LAUNCHING EMAIL CAMPAIGN TO PAST CUSTOMERS")
    log.info("=" * 60)

    # Note: This requires customer emails to be in campaign_contacts table
    # For now, you'll need to provide the customer email list

    print("\n📧 MANUAL CUSTOMER EMAIL LIST NEEDED")
    print("=" * 60)
    print("\nTo get the campaign started, provide customer emails:")
    print("Option 1: Paste email list (comma-separated)")
    print("Option 2: Use: python3 -c \"from email_campaigns import add_customer_to_campaign; import asyncio; asyncio.run(add_customer_to_campaign('Name', 'email@example.com', 'Company'))\"")
    print("\nOnce customers are in the system, run: python3 send_referral_campaign.py")

    # Send batch emails to anyone marked as "new"
    await send_campaign_batch(batch_size=100)

    log.info("=" * 60)
    log.info("✅ Campaign complete!")

if __name__ == "__main__":
    asyncio.run(main())

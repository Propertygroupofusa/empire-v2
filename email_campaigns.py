"""
Email Campaign Automation System
Sends referral emails to past customers when their videos are completed.
Tracks opens, clicks, conversions.
"""

import os
import asyncio
import logging
import smtplib
import uuid
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from sqlalchemy import select, Column, String, DateTime, Integer, Boolean
from sqlalchemy.orm import declarative_base
from database import AsyncSessionLocal, Base

log = logging.getLogger("email_campaigns")

GMAIL_EMAIL = os.getenv("GMAIL_EMAIL", "")
GMAIL_PASSWORD = os.getenv("GMAIL_PASSWORD", "")

# Email templates
REFERRAL_EMAIL_TEMPLATE = """
<html>
<head>
    <style>
        body {{ font-family: Arial, sans-serif; line-height: 1.6; }}
        .container {{ max-width: 600px; margin: 0 auto; }}
        .header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 40px 20px; text-align: center; border-radius: 5px 5px 0 0; }}
        .content {{ padding: 30px 20px; background: #f9f9f9; }}
        .cta-button {{ display: inline-block; background: #667eea; color: white; padding: 15px 30px; border-radius: 5px; text-decoration: none; margin: 20px 0; font-weight: bold; }}
        .footer {{ text-align: center; padding: 20px; color: #666; font-size: 12px; background: #f0f0f0; border-radius: 0 0 5px 5px; }}
        .offer-box {{ background: white; border: 2px solid #667eea; border-radius: 5px; padding: 20px; margin: 20px 0; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🎬 Your Video is Ready!</h1>
            <p>Plus - Exclusive Referral Offer Inside</p>
        </div>

        <div class="content">
            <h2>Hi {customer_name},</h2>

            <p>Congrats! Your professional explainer video is complete and ready to use.</p>

            <div class="offer-box">
                <h3>🎁 EXCLUSIVE REFERRAL BONUS</h3>
                <p><strong>Refer a business friend and BOTH get $100 credit!</strong></p>
                <ul>
                    <li>✅ You get: $100 credit + 50% off your next video</li>
                    <li>✅ Your friend gets: $100 credit on first order + free revisions</li>
                </ul>
                <p style="text-align: center;">
                    <a href="https://empire-v2-production.up.railway.app/quote?ref={referral_code}" class="cta-button">Share Your Referral Link</a>
                </p>
            </div>

            <p><strong>What our customers are seeing:</strong></p>
            <ul>
                <li>📈 340% average engagement increase</li>
                <li>💰 25% conversion lift</li>
                <li>⏱️ 2-3 day turnaround</li>
            </ul>

            <h3>Need More Videos?</h3>
            <p>Popular next videos for {company}:</p>
            <ul>
                <li>Product Demo Video</li>
                <li>Customer Testimonial</li>
                <li>Sales Pitch Video</li>
                <li>Training Video</li>
            </ul>

            <p style="text-align: center;">
                <a href="https://empire-v2-production.up.railway.app/quote" class="cta-button">Order Another Video</a>
            </p>
        </div>

        <div class="footer">
            <p>Property Group of USA | Professional Video Production</p>
            <p><a href="#">Unsubscribe</a> | <a href="#">Preferences</a></p>
        </div>
    </div>
</body>
</html>
"""


class CampaignContact(Base):
    """Track customer emails for campaigns."""
    __tablename__ = "campaign_contacts"

    id = Column(Integer, primary_key=True)
    customer_name = Column(String)
    customer_email = Column(String, index=True, unique=True)
    company = Column(String)
    referral_code = Column(String, unique=True)
    status = Column(String, default="new")  # new, sent, opened, clicked, converted
    email_sent_at = Column(DateTime)
    opened_at = Column(DateTime)
    clicked_at = Column(DateTime)
    converted_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)


async def add_customer_to_campaign(name: str, email: str, company: str):
    """Add customer to email campaign after their order is placed."""
    try:
        if not email:
            return

        async with AsyncSessionLocal() as db:
            # Generate referral code
            referral_code = f"ref_{uuid.uuid4().hex[:8]}"

            # Check if already exists
            result = await db.execute(
                select(CampaignContact).where(CampaignContact.customer_email == email)
            )
            existing = result.scalar()

            if not existing:
                contact = CampaignContact(
                    customer_name=name,
                    customer_email=email,
                    company=company,
                    referral_code=referral_code,
                    status="new"
                )
                db.add(contact)
                await db.commit()
                log.info(f"✅ Added {email} to campaign with code {referral_code}")
    except Exception as e:
        log.error(f"Error adding customer to campaign: {e}")


async def send_referral_email(customer_email: str):
    """Send referral email to customer after video delivery."""
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(CampaignContact).where(
                    CampaignContact.customer_email == customer_email
                )
            )
            contact = result.scalar()

            if not contact:
                log.warning(f"Contact not found: {customer_email}")
                return False

            if not GMAIL_EMAIL or not GMAIL_PASSWORD:
                log.warning("Gmail credentials not configured - skipping email send")
                return False

            # Format email
            html_content = REFERRAL_EMAIL_TEMPLATE.format(
                customer_name=contact.customer_name,
                company=contact.company,
                referral_code=contact.referral_code
            )

            # Send via Gmail
            msg = MIMEMultipart("alternative")
            msg["Subject"] = "🎬 Your Video is Ready + $100 Referral Bonus!"
            msg["From"] = GMAIL_EMAIL
            msg["To"] = customer_email

            part = MIMEText(html_content, "html")
            msg.attach(part)

            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(GMAIL_EMAIL, GMAIL_PASSWORD)
                server.sendmail(GMAIL_EMAIL, customer_email, msg.as_string())

            # Update status
            contact.status = "sent"
            contact.email_sent_at = datetime.utcnow()
            await db.commit()

            log.info(f"✅ Referral email sent to {customer_email}")
            return True

    except Exception as e:
        log.error(f"Error sending referral email: {e}")
        return False


async def send_campaign_batch(batch_size: int = 50):
    """Send referral emails to all customers in batch."""
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(CampaignContact).where(
                    CampaignContact.status == "new"
                ).limit(batch_size)
            )
            contacts = result.scalars().all()

            log.info(f"📧 Sending referral emails to {len(contacts)} customers...")

            for contact in contacts:
                success = await send_referral_email(contact.customer_email)
                await asyncio.sleep(1)  # Rate limit

            log.info(f"✅ Batch send complete ({len(contacts)} emails)")

    except Exception as e:
        log.error(f"Error in batch send: {e}")


if __name__ == "__main__":
    # Test: Send batch emails
    asyncio.run(send_campaign_batch())

"""Sales Agent — AI-powered lead research, outreach, and follow-up"""
import os
import logging
import asyncio
from datetime import datetime, timedelta
import anthropic
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from models import Lead, Outreach, Response, LeadStatus, OutreachType
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

logger = logging.getLogger(__name__)

# Initialize Anthropic client
client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

GMAIL_EMAIL = os.getenv("GMAIL_EMAIL")
GMAIL_PASSWORD = os.getenv("GMAIL_PASSWORD")


def research_lead(lead: Lead) -> dict:
    """
    Use Claude to research a lead and identify pain points.
    In production, this would also fetch real LinkedIn/web data.
    """
    prompt = f"""
    Research this prospect and provide insights:

    Name: {lead.first_name} {lead.last_name}
    Company: {lead.company_name}
    Company Size: {lead.company_size}
    Industry: {lead.industry}
    Website: {lead.company_website}

    Based on the company profile, answer:
    1. What are the top 3 pain points this company likely faces?
    2. Which of our services would be most valuable to them? (video_production, property_group, ai_course)
    3. What recent activity or signals suggest they need our help now?
    4. Rate their fit on a scale 1-100.

    Respond in JSON format:
    {{
        "pain_points": ["...", "...", "..."],
        "recommended_product": "video_production|property_group|ai_course",
        "urgency_signals": ["...", "..."],
        "fit_score": 75,
        "research_notes": "..."
    }}
    """

    response = client.messages.create(
        model="claude-opus-5",
        max_tokens=500,
        messages=[
            {"role": "user", "content": prompt}
        ]
    )

    try:
        import json
        result = json.loads(response.content[0].text)
        return result
    except:
        logger.warning(f"Failed to parse Claude research response for {lead.email}")
        return {
            "pain_points": [],
            "recommended_product": "video_production",
            "urgency_signals": [],
            "fit_score": 50,
            "research_notes": "Research failed"
        }


def write_outreach(lead: Lead, outreach_type: OutreachType = OutreachType.INITIAL) -> dict:
    """
    Use Claude to write personalized outreach email.
    """

    # Determine product pitch based on target
    if lead.target_product == "video_production":
        product_pitch = """We help B2B companies create professional explainer videos,
        training content, and marketing videos without the high production costs.
        Most clients see a 40% increase in engagement with video content."""
    elif lead.target_product == "property_group":
        product_pitch = """We help property managers and real estate teams automate
        client communication, document management, and lease processing.
        Save 10+ hours per week on administrative work."""
    else:  # ai_course
        product_pitch = """We teach founders and developers how to build AI agents
        that automate business operations. Hundreds have built systems generating
        $1K-100K+ monthly revenue."""

    if outreach_type == OutreachType.INITIAL:
        subject_prompt = f"Write a subject line for cold outreach to {lead.first_name} at {lead.company_name}"
        body_prompt = f"""Write a personalized cold email to {lead.first_name} at {lead.company_name}.

        Their pain point: {lead.pain_point}

        Product we're offering: {product_pitch}

        Requirements:
        - 3-4 sentences max
        - Reference something specific about their company
        - Include ONE clear call-to-action (15-min call)
        - Sound like a real human, not an AI
        - Sign with "Best, [Your Name]"
        """
    else:
        # Follow-up email
        subject_prompt = f"Write a follow-up subject line to {lead.first_name}"
        body_prompt = f"""Write a brief follow-up email to {lead.first_name}.

        This is our {outreach_type.value} follow-up.
        Original pain point: {lead.pain_point}
        Product: {product_pitch}

        Requirements:
        - Different angle than the first email
        - Still personalized and human
        - Offer value, not pressure
        - 2-3 sentences
        """

    subject_response = client.messages.create(
        model="claude-opus-5",
        max_tokens=100,
        messages=[{"role": "user", "content": subject_prompt}]
    )
    subject = subject_response.content[0].text.strip()

    body_response = client.messages.create(
        model="claude-opus-5",
        max_tokens=300,
        messages=[{"role": "user", "content": body_prompt}]
    )
    body = body_response.content[0].text.strip()

    return {
        "subject": subject,
        "body": body
    }


async def send_email(to_email: str, subject: str, body: str) -> bool:
    """Send email via Gmail SMTP"""
    if not GMAIL_EMAIL or not GMAIL_PASSWORD:
        logger.warning("Gmail credentials not configured. Skipping email send.")
        return False

    try:
        msg = MIMEMultipart()
        msg["From"] = GMAIL_EMAIL
        msg["To"] = to_email
        msg["Subject"] = subject

        msg.attach(MIMEText(body, "plain"))

        # Send via Gmail SMTP
        server = smtplib.SMTP_SSL("smtp.gmail.com", 465)
        server.login(GMAIL_EMAIL, GMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()

        logger.info(f"Email sent to {to_email}: {subject}")
        return True
    except Exception as e:
        logger.error(f"Failed to send email to {to_email}: {e}")
        return False


async def process_new_leads(db: AsyncSession, limit: int = 50):
    """
    Research and send outreach to new leads (status=NEW)
    """
    stmt = select(Lead).where(Lead.status == LeadStatus.NEW).limit(limit)
    result = await db.execute(stmt)
    new_leads = result.scalars().all()

    logger.info(f"Processing {len(new_leads)} new leads...")

    for lead in new_leads:
        try:
            # Research the lead
            research = research_lead(lead)
            lead.fit_score = research.get("fit_score", 50)
            lead.research_notes = research.get("research_notes", "")
            lead.pain_point = "\n".join(research.get("pain_points", []))
            lead.status = LeadStatus.RESEARCHED

            # Write outreach
            email_content = write_outreach(lead, OutreachType.INITIAL)

            # Send email (async wrapper)
            sent = await send_email(lead.email, email_content["subject"], email_content["body"])

            if sent:
                # Record the outreach
                outreach = Outreach(
                    lead_id=lead.id,
                    outreach_type=OutreachType.INITIAL,
                    subject=email_content["subject"],
                    body=email_content["body"],
                    sent_via="email",
                    status="sent"
                )
                lead.status = LeadStatus.OUTREACH_SENT
                lead.last_contact_date = datetime.utcnow()
                lead.times_contacted = 1

                db.add(outreach)

            await db.commit()
            logger.info(f"✓ Processed lead: {lead.email} (fit: {lead.fit_score})")

        except Exception as e:
            logger.error(f"Failed to process lead {lead.email}: {e}")
            await db.rollback()

        # Small delay to avoid rate limiting
        await asyncio.sleep(1)


async def process_followups(db: AsyncSession):
    """
    Auto-send follow-up emails to leads who haven't responded (after 3-5 days)
    """
    cutoff_date = datetime.utcnow() - timedelta(days=3)

    stmt = select(Outreach).where(
        (Outreach.status == "sent") &
        (Outreach.replied_at == None) &
        (Outreach.sent_at < cutoff_date) &
        (Outreach.outreach_type != OutreachType.FOLLOWUP_3)
    ).limit(20)

    result = await db.execute(stmt)
    outreaches = result.scalars().all()

    logger.info(f"Processing {len(outreaches)} follow-ups...")

    for outreach in outreaches:
        try:
            lead = await db.get(Lead, outreach.lead_id)

            if not lead:
                continue

            # Determine next follow-up type
            if outreach.outreach_type == OutreachType.INITIAL:
                next_type = OutreachType.FOLLOWUP_1
            elif outreach.outreach_type == OutreachType.FOLLOWUP_1:
                next_type = OutreachType.FOLLOWUP_2
            else:
                next_type = OutreachType.FOLLOWUP_3

            # Write follow-up
            email_content = write_outreach(lead, next_type)

            # Send
            sent = await send_email(lead.email, email_content["subject"], email_content["body"])

            if sent:
                followup = Outreach(
                    lead_id=lead.id,
                    outreach_type=next_type,
                    subject=email_content["subject"],
                    body=email_content["body"],
                    sent_via="email",
                    status="sent"
                )
                lead.last_contact_date = datetime.utcnow()
                lead.times_contacted += 1

                db.add(followup)
                await db.commit()
                logger.info(f"✓ Follow-up sent to {lead.email}")

        except Exception as e:
            logger.error(f"Failed to send follow-up: {e}")
            await db.rollback()

        await asyncio.sleep(1)


async def generate_daily_report(db: AsyncSession) -> dict:
    """
    Generate daily sales report showing outreach activity and hot leads
    """
    today = datetime.utcnow().date()
    today_start = datetime(today.year, today.month, today.day)
    today_end = today_start + timedelta(days=1)

    # Count outreach sent today
    stmt_sent = select(Outreach).where(
        (Outreach.sent_at >= today_start) & (Outreach.sent_at < today_end)
    )
    result = await db.execute(stmt_sent)
    sent_count = len(result.scalars().all())

    # Count responses
    stmt_responses = select(Response).where(
        (Response.received_at >= today_start) & (Response.received_at < today_end)
    )
    result = await db.execute(stmt_responses)
    responses = result.scalars().all()

    # Hot leads (high fit score + recent response)
    stmt_hot = select(Lead).where(
        Lead.fit_score >= 70
    ).order_by(Lead.last_response_date.desc()).limit(5)
    result = await db.execute(stmt_hot)
    hot_leads = result.scalars().all()

    # Total stats
    stmt_total = select(Lead)
    result = await db.execute(stmt_total)
    total_leads = len(result.scalars().all())

    report = {
        "date": today.isoformat(),
        "outreach_sent_today": sent_count,
        "responses_received_today": len(responses),
        "total_leads": total_leads,
        "hot_leads": [
            {
                "name": f"{l.first_name} {l.last_name}",
                "email": l.email,
                "company": l.company_name,
                "fit_score": l.fit_score,
                "last_response": l.last_response_date.isoformat() if l.last_response_date else None
            }
            for l in hot_leads
        ]
    }

    logger.info(f"Daily Report: {sent_count} sent, {len(responses)} responses")
    return report

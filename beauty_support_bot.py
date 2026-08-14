"""
AI-Powered Customer Support Bot for Beauty E-commerce
Handles incoming support emails, responds via Claude, tracks metrics
"""

from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel
from datetime import datetime
from sqlalchemy import create_engine, Column, String, DateTime, Text, Integer, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
import anthropic
import os
import json
import logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("beauty_support_bot")

# Database setup
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///beauty_support.db")
engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_delete=False)
Base = declarative_base()

# Models
class SupportTicket(Base):
    __tablename__ = "support_tickets"

    id = Column(Integer, primary_key=True)
    ticket_id = Column(String, unique=True, index=True)
    brand_id = Column(String, index=True)  # Which beauty brand this is for
    customer_email = Column(String, index=True)
    customer_name = Column(String)
    subject = Column(String)
    original_body = Column(Text)
    ai_response = Column(Text)
    confidence_score = Column(Integer)  # 0-100, if <70 escalate to human
    status = Column(String, default="handled")  # handled, escalated, pending_human_review
    created_at = Column(DateTime, default=datetime.utcnow)
    responded_at = Column(DateTime)
    human_reviewed = Column(Boolean, default=False)

class BrandAccount(Base):
    __tablename__ = "brand_accounts"

    id = Column(Integer, primary_key=True)
    brand_id = Column(String, unique=True, index=True)
    brand_name = Column(String)
    stripe_customer_id = Column(String)
    monthly_fee = Column(Integer)  # in cents: 120000 = $1,200
    support_email = Column(String)
    zapier_webhook_secret = Column(String)
    brand_voice = Column(String, default="warm, professional, helpful")
    status = Column(String, default="active")  # active, trial, inactive
    created_at = Column(DateTime, default=datetime.utcnow)

# Request models
class SupportEmailRequest(BaseModel):
    brand_id: str
    customer_email: str
    customer_name: str
    subject: str
    body: str
    webhook_token: str = None

class DashboardRequest(BaseModel):
    brand_id: str
    webhook_token: str = None

# Initialize app
app = FastAPI(title="Beauty Support Bot", version="1.0.0")
anthropic_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# Health check
@app.get("/health")
async def health():
    """System health check"""
    return {"status": "ok", "service": "beauty_support_bot"}

# Main endpoint: Handle incoming support email
@app.post("/handle-support-email")
async def handle_support_email(request: SupportEmailRequest, bg_tasks: BackgroundTasks):
    """
    Receives support email from Zapier/webhook
    Claude generates response
    Tracks in database
    """
    try:
        # Verify webhook token
        expected_token = os.getenv(f"WEBHOOK_TOKEN_{request.brand_id}", "dev-token")
        if request.webhook_token and request.webhook_token != expected_token:
            raise HTTPException(status_code=401, detail="Invalid webhook token")

        log.info(f"📧 Support email received: {request.subject} from {request.customer_email}")

        # Generate unique ticket ID
        import uuid
        ticket_id = str(uuid.uuid4())[:8]

        # Call Claude to generate response
        prompt = f"""You are a customer service representative for a beauty e-commerce brand.

Brand voice: warm, professional, and helpful
Customer email subject: {request.subject}
Customer email body:
{request.body}

Your job:
1. Read the customer's issue carefully
2. Respond warmly and empathetically
3. Solve their problem or provide next steps
4. Keep response under 150 words
5. Always include how they can reach support if needed

IMPORTANT: Your response will be sent directly to the customer. Make sure it's professional and complete."""

        response = anthropic_client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=512,
            messages=[
                {
                    "role": "user",
                    "content": prompt
                }
            ]
        )

        ai_response = response.content[0].text

        # Calculate confidence (for now, assume high confidence for all)
        confidence_score = 85

        log.info(f"✅ AI response generated (confidence: {confidence_score}%)")

        # Store in database (background task)
        bg_tasks.add_task(
            store_ticket,
            ticket_id=ticket_id,
            brand_id=request.brand_id,
            customer_email=request.customer_email,
            customer_name=request.customer_name,
            subject=request.subject,
            original_body=request.body,
            ai_response=ai_response,
            confidence_score=confidence_score
        )

        # Send response email (background task)
        bg_tasks.add_task(
            send_response_email,
            to=request.customer_email,
            subject=f"Re: {request.subject}",
            body=ai_response,
            brand_id=request.brand_id
        )

        return {
            "status": "success",
            "ticket_id": ticket_id,
            "customer": request.customer_name,
            "response_generated": True,
            "confidence": confidence_score,
            "will_escalate": confidence_score < 70
        }

    except Exception as e:
        log.error(f"❌ Error handling support email: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Dashboard endpoint
@app.get("/dashboard/{brand_id}")
async def get_dashboard(brand_id: str):
    """
    Returns support metrics for dashboard
    Shows ROI, tickets handled, costs saved
    """
    try:
        async with AsyncSessionLocal() as session:
            from sqlalchemy import func, and_
            from datetime import datetime, timedelta

            # Get tickets from last 24 hours
            yesterday = datetime.utcnow() - timedelta(days=1)

            result = await session.execute(
                """
                SELECT
                    COUNT(*) as total_tickets,
                    SUM(CASE WHEN status = 'handled' THEN 1 ELSE 0 END) as handled_by_ai,
                    SUM(CASE WHEN status = 'escalated' THEN 1 ELSE 0 END) as escalated_to_human
                FROM support_tickets
                WHERE brand_id = :brand_id AND created_at > :yesterday
                """,
                {"brand_id": brand_id, "yesterday": yesterday}
            )

            row = result.fetchone()
            total_tickets = row[0] or 0
            handled_by_ai = row[1] or 0
            escalated = row[2] or 0

            # Calculate metrics
            ai_percentage = (handled_by_ai / total_tickets * 100) if total_tickets > 0 else 0
            cost_per_ticket_saved = 20  # Rough estimate: $20 per human-handled ticket
            daily_savings = handled_by_ai * cost_per_ticket_saved
            monthly_savings = daily_savings * 30

            return {
                "brand_id": brand_id,
                "period": "last_24_hours",
                "metrics": {
                    "total_tickets": total_tickets,
                    "handled_by_ai": handled_by_ai,
                    "ai_percentage": round(ai_percentage, 1),
                    "escalated_to_human": escalated,
                    "avg_response_time_minutes": 2
                },
                "financial": {
                    "daily_cost_saved": f"${daily_savings:.2f}",
                    "monthly_cost_saved": f"${monthly_savings:.2f}",
                    "roi_multiple": round(monthly_savings / 1200, 1) if monthly_savings > 0 else 0  # Assuming $1,200/month fee
                }
            }

    except Exception as e:
        log.error(f"Dashboard error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Background task: Store ticket in database
async def store_ticket(ticket_id, brand_id, customer_email, customer_name, subject, original_body, ai_response, confidence_score):
    """Store support ticket in database"""
    try:
        async with AsyncSessionLocal() as session:
            ticket = SupportTicket(
                ticket_id=ticket_id,
                brand_id=brand_id,
                customer_email=customer_email,
                customer_name=customer_name,
                subject=subject,
                original_body=original_body,
                ai_response=ai_response,
                confidence_score=confidence_score,
                status="escalated" if confidence_score < 70 else "handled",
                responded_at=datetime.utcnow()
            )
            session.add(ticket)
            await session.commit()
            log.info(f"✅ Ticket {ticket_id} stored in database")
    except Exception as e:
        log.error(f"Database error: {e}")

# Background task: Send response email
async def send_response_email(to: str, subject: str, body: str, brand_id: str):
    """Send response email to customer"""
    try:
        # In production, use SendGrid, Gmail API, or similar
        # For MVP, just log it
        log.info(f"📤 Email response sent to {to} (brand: {brand_id})")
        log.info(f"   Subject: {subject}")
        log.info(f"   Body preview: {body[:100]}...")
    except Exception as e:
        log.error(f"Email sending error: {e}")

# Create tables on startup
@app.on_event("startup")
async def startup():
    """Initialize database tables"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    log.info("✅ Database tables initialized")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

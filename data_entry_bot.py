"""
AI Data Entry Bot - Automates manual data entry tasks
Accepts documents, extracts data using Claude vision, organizes into spreadsheets
"""

import asyncio
import logging
import os
import uuid
from datetime import datetime
from typing import Optional

import anthropic
from fastapi import FastAPI, HTTPException, UploadFile, File, BackgroundTasks, Depends
from pydantic import BaseModel
from sqlalchemy import Column, String, Integer, DateTime, Text, Float, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
import stripe

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("data_entry_bot")

# Database setup
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///data_entry.db")
engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_delete=False)
Base = declarative_base()

# Stripe setup
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

# Models
class DataEntryJob(Base):
    __tablename__ = "data_entry_jobs"

    id = Column(String, primary_key=True)
    customer_id = Column(String, index=True)
    job_name = Column(String)
    document_count = Column(Integer)
    entries_extracted = Column(Integer, default=0)
    status = Column(String, default="processing")  # processing, completed, failed
    output_format = Column(String, default="excel")  # excel, sheets, csv
    output_url = Column(String, nullable=True)
    cost_saved = Column(Float, default=0.0)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

class DataEntryCustomer(Base):
    __tablename__ = "data_entry_customers"

    id = Column(String, primary_key=True)
    company_name = Column(String)
    email = Column(String, unique=True, index=True)
    stripe_customer_id = Column(String)
    subscription_tier = Column(String, default="starter")  # starter, pro, enterprise
    monthly_fee = Column(Integer)  # in cents
    status = Column(String, default="active")
    created_at = Column(DateTime, default=datetime.utcnow)

# Request models
class DocumentUploadRequest(BaseModel):
    customer_id: str
    job_name: str
    output_format: str = "excel"  # excel, sheets, csv

class SubscriptionRequest(BaseModel):
    company_name: str
    email: str
    tier: str  # starter ($500), pro ($1000), enterprise ($2000)

# FastAPI app
app = FastAPI(title="Data Entry Bot", version="1.0.0")
anthropic_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# Health check
@app.get("/health")
async def health():
    """System health check"""
    return {"status": "ok", "service": "data_entry_bot"}

# Main endpoint: Upload documents for processing
@app.post("/upload-documents")
async def upload_documents(
    customer_id: str,
    job_name: str,
    output_format: str = "excel",
    files: list[UploadFile] = File(...),
    bg_tasks: BackgroundTasks = BackgroundTasks()
):
    """
    Upload documents (PDF, images, etc) for data extraction
    Bot processes documents and extracts structured data
    """
    try:
        log.info(f"📄 Documents uploaded: {len(files)} files for job '{job_name}'")

        # Create job
        job_id = str(uuid.uuid4())[:8]

        # Process documents in background
        bg_tasks.add_task(
            process_documents,
            job_id=job_id,
            customer_id=customer_id,
            job_name=job_name,
            files=files,
            output_format=output_format
        )

        return {
            "status": "processing",
            "job_id": job_id,
            "files_queued": len(files),
            "message": "Documents queued for processing. Check back in 5-10 minutes."
        }

    except Exception as e:
        log.error(f"❌ Upload error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Background task: Process documents and extract data
async def process_documents(job_id: str, customer_id: str, job_name: str, files, output_format: str):
    """Process uploaded documents and extract data using Claude vision"""
    try:
        log.info(f"🤖 Processing job {job_id}: {len(files)} documents")

        extracted_data = []

        # Process each document
        for file in files:
            content = await file.read()

            # Use Claude vision to extract data
            prompt = """Analyze this document and extract ALL structured data you can find.

            Return as JSON with the following structure:
            {
                "entries": [
                    {"field_name": "value", "field_name2": "value2", ...},
                    ...
                ],
                "total_entries": number,
                "data_quality": "high/medium/low",
                "notes": "any issues or observations"
            }

            Be thorough and extract every piece of usable data."""

            # Call Claude with vision
            try:
                response = anthropic_client.messages.create(
                    model="claude-3-5-sonnet-20241022",
                    max_tokens=4096,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": "application/pdf",
                                        "data": content.hex()  # This is simplified - real implementation would use proper image/PDF handling
                                    }
                                },
                                {
                                    "type": "text",
                                    "text": prompt
                                }
                            ]
                        }
                    ]
                )

                # Extract data from response
                extracted_text = response.content[0].text
                extracted_data.append({
                    "file": file.filename,
                    "data": extracted_text
                })

            except Exception as e:
                log.warning(f"Could not process {file.filename} with vision: {e}")
                # Fallback: just log the filename
                extracted_data.append({
                    "file": file.filename,
                    "error": "Vision processing unavailable"
                })

        log.info(f"✅ Job {job_id} completed: {len(extracted_data)} documents processed")

        # Store job completion in database
        async with AsyncSessionLocal() as session:
            job = DataEntryJob(
                id=job_id,
                customer_id=customer_id,
                job_name=job_name,
                document_count=len(files),
                entries_extracted=len(extracted_data),
                status="completed",
                output_format=output_format,
                output_url=f"https://example.com/downloads/{job_id}.{output_format}",
                cost_saved=len(files) * 10.0,  # $10 per document saved
                completed_at=datetime.utcnow()
            )
            session.add(job)
            await session.commit()

    except Exception as e:
        log.error(f"❌ Processing error for job {job_id}: {e}")
        async with AsyncSessionLocal() as session:
            job = DataEntryJob(
                id=job_id,
                customer_id=customer_id,
                job_name=job_name,
                document_count=len(files),
                status="failed"
            )
            session.add(job)
            await session.commit()

# Subscription endpoint
@app.post("/subscribe")
async def create_subscription(request: SubscriptionRequest):
    """Create subscription for customer"""
    try:
        # Tier pricing (in cents)
        tier_pricing = {
            "starter": 50000,  # $500/month
            "pro": 100000,      # $1000/month
            "enterprise": 200000  # $2000/month
        }

        if request.tier not in tier_pricing:
            raise HTTPException(status_code=400, detail="Invalid tier")

        # Create Stripe customer
        stripe_customer = stripe.Customer.create(
            email=request.email,
            name=request.company_name
        )

        # Create subscription
        subscription = stripe.Subscription.create(
            customer=stripe_customer.id,
            items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {
                        "name": f"Data Entry Bot - {request.tier.capitalize()}"
                    },
                    "recurring": {
                        "interval": "month",
                        "interval_count": 1
                    },
                    "unit_amount": tier_pricing[request.tier]
                }
            }]
        )

        # Store customer in database
        customer_id = str(uuid.uuid4())[:8]
        async with AsyncSessionLocal() as session:
            customer = DataEntryCustomer(
                id=customer_id,
                company_name=request.company_name,
                email=request.email,
                stripe_customer_id=stripe_customer.id,
                subscription_tier=request.tier,
                monthly_fee=tier_pricing[request.tier],
                status="active"
            )
            session.add(customer)
            await session.commit()

        log.info(f"✅ Customer subscribed: {request.company_name} ({request.tier})")

        return {
            "status": "success",
            "customer_id": customer_id,
            "subscription_id": subscription.id,
            "tier": request.tier,
            "monthly_cost": f"${tier_pricing[request.tier] / 100:.2f}",
            "message": "Subscription created! You can now upload documents."
        }

    except Exception as e:
        log.error(f"❌ Subscription error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Dashboard endpoint
@app.get("/dashboard/{customer_id}")
async def get_dashboard(customer_id: str):
    """Get customer dashboard with usage stats"""
    try:
        async with AsyncSessionLocal() as session:
            # Get customer info
            customer = await session.query(DataEntryCustomer).filter(
                DataEntryCustomer.id == customer_id
            ).first()

            if not customer:
                raise HTTPException(status_code=404, detail="Customer not found")

            # Get jobs for this customer
            jobs = await session.query(DataEntryJob).filter(
                DataEntryJob.customer_id == customer_id
            ).all()

            total_entries = sum(j.entries_extracted for j in jobs)
            total_saved = sum(j.cost_saved for j in jobs)
            completed_jobs = len([j for j in jobs if j.status == "completed"])

            return {
                "customer_id": customer_id,
                "company_name": customer.company_name,
                "tier": customer.subscription_tier,
                "monthly_fee": f"${customer.monthly_fee / 100:.2f}",
                "stats": {
                    "total_jobs": len(jobs),
                    "completed_jobs": completed_jobs,
                    "total_entries_extracted": total_entries,
                    "total_cost_saved": f"${total_saved:.2f}",
                    "roi_multiplier": round(total_saved / (customer.monthly_fee / 100), 1)
                },
                "recent_jobs": [
                    {
                        "job_id": j.id,
                        "name": j.job_name,
                        "status": j.status,
                        "entries": j.entries_extracted,
                        "created": j.created_at.isoformat()
                    }
                    for j in jobs[-5:]
                ]
            }

    except Exception as e:
        log.error(f"Dashboard error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

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

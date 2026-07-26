#!/usr/bin/env python3
"""CLI for managing the Sales Agent"""
import asyncio
import json
import sys
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from database import AsyncSessionLocal, engine
from models import Lead, Outreach, LeadStatus, LeadSource
from sales_agent import process_new_leads, process_followups, generate_daily_report

async def add_lead(first_name: str, last_name: str, email: str, company: str,
                   product: str = "video_production", size: str = None, industry: str = None):
    """Add a single lead"""
    async with AsyncSessionLocal() as db:
        lead = Lead(
            first_name=first_name,
            last_name=last_name,
            email=email,
            company_name=company,
            company_size=size,
            industry=industry,
            target_product=product,
            source=LeadSource.MANUAL,
            status=LeadStatus.NEW
        )
        db.add(lead)
        await db.commit()
        print(f"✓ Added lead: {email}")

async def import_leads_csv(filepath: str):
    """Import leads from CSV (name, email, company, product)"""
    import csv
    async with AsyncSessionLocal() as db:
        with open(filepath, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                lead = Lead(
                    first_name=row.get('first_name', ''),
                    last_name=row.get('last_name', ''),
                    email=row.get('email'),
                    company_name=row.get('company'),
                    industry=row.get('industry'),
                    target_product=row.get('product', 'video_production'),
                    source=LeadSource.MANUAL,
                    status=LeadStatus.NEW
                )
                db.add(lead)
            await db.commit()
        print(f"✓ Imported {len(list(reader))} leads from {filepath}")

async def list_leads(status: str = None, limit: int = 20):
    """List leads"""
    async with AsyncSessionLocal() as db:
        stmt = select(Lead)
        if status:
            stmt = stmt.where(Lead.status == status)
        stmt = stmt.limit(limit).order_by(Lead.created_at.desc())
        result = await db.execute(stmt)
        leads = result.scalars().all()

        print(f"\n{'Email':<35} {'Company':<20} {'Product':<15} {'Score':<6} {'Status':<15}")
        print("=" * 90)
        for lead in leads:
            status_val = lead.status.value if hasattr(lead.status, 'value') else lead.status
            print(f"{lead.email:<35} {lead.company_name:<20} {lead.target_product:<15} {lead.fit_score:<6} {status_val:<15}")

async def process_leads(limit: int = 50):
    """Trigger lead research and outreach"""
    async with AsyncSessionLocal() as db:
        await process_new_leads(db, limit)
    print(f"✓ Processed up to {limit} leads")

async def process_followups_cli():
    """Trigger follow-up emails"""
    async with AsyncSessionLocal() as db:
        await process_followups(db)
    print("✓ Processed follow-ups")

async def daily_report():
    """Show today's report"""
    async with AsyncSessionLocal() as db:
        report = await generate_daily_report(db)
    print(json.dumps(report, indent=2))

async def main():
    if len(sys.argv) < 2:
        print("""
Sales Agent CLI
Usage:
  python sales_agent_cli.py add-lead <first> <last> <email> <company> [product] [size] [industry]
  python sales_agent_cli.py import-csv <filepath>
  python sales_agent_cli.py list-leads [status] [limit]
  python sales_agent_cli.py process [limit]
  python sales_agent_cli.py followups
  python sales_agent_cli.py report

Examples:
  python sales_agent_cli.py add-lead John Doe john@example.com "Acme Corp" video_production "100-500" "SaaS"
  python sales_agent_cli.py import-csv leads.csv
  python sales_agent_cli.py list-leads researched 50
  python sales_agent_cli.py process 100
  python sales_agent_cli.py report
        """)
        sys.exit(1)

    command = sys.argv[1]

    if command == "add-lead":
        if len(sys.argv) < 6:
            print("Usage: add-lead <first> <last> <email> <company> [product] [size] [industry]")
            sys.exit(1)
        product = sys.argv[6] if len(sys.argv) > 6 else "video_production"
        size = sys.argv[7] if len(sys.argv) > 7 else None
        industry = sys.argv[8] if len(sys.argv) > 8 else None
        await add_lead(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], product, size, industry)

    elif command == "import-csv":
        if len(sys.argv) < 3:
            print("Usage: import-csv <filepath>")
            sys.exit(1)
        await import_leads_csv(sys.argv[2])

    elif command == "list-leads":
        status = sys.argv[2] if len(sys.argv) > 2 else None
        limit = int(sys.argv[3]) if len(sys.argv) > 3 else 20
        await list_leads(status, limit)

    elif command == "process":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 50
        await process_leads(limit)

    elif command == "followups":
        await process_followups_cli()

    elif command == "report":
        await daily_report()

    else:
        print(f"Unknown command: {command}")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())

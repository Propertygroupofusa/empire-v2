"""Models package - imports all ORM models for database initialization"""
# This file ensures all model modules are imported so SQLAlchemy registers them on Base.metadata

from models.sales import Lead, Outreach, Response, LeadStatus, LeadSource, OutreachType

__all__ = [
    "Lead",
    "Outreach",
    "Response",
    "LeadStatus",
    "LeadSource",
    "OutreachType",
]

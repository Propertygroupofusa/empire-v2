from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from admin_auth import require_admin_key
from database import get_db
from models import DailyBrief

router = APIRouter()


@router.post("/daily-brief/send-now", dependencies=[Depends(require_admin_key)])
async def trigger_daily_brief_now():
    """Manually trigger the daily brief immediately, without waiting for
    its scheduled time - for testing the summary/email pipeline end to
    end. Same brief the scheduled job sends; this doesn't create a
    second/duplicate schedule."""
    from daily_brief import generate_and_send_brief

    await generate_and_send_brief()
    return {"status": "sent"}


@router.get("/daily-brief/history", dependencies=[Depends(require_admin_key)])
async def get_daily_brief_history(limit: int = 30, db: AsyncSession = Depends(get_db)):
    """Past briefs, most recent first - the searchable history piece of
    the daily brief idea. Each row includes the raw snapshots alongside
    the generated summary, so querying isn't limited to whatever Claude
    happened to mention in that day's prose."""
    result = await db.execute(select(DailyBrief).order_by(DailyBrief.created_at.desc()).limit(limit))
    return {"briefs": [b.to_dict() for b in result.scalars().all()]}

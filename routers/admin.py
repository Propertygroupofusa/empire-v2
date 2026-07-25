from fastapi import APIRouter, Depends

from admin_auth import require_admin_key

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

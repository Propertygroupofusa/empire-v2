#!/usr/bin/env python3
"""
Outreach DELETE endpoints must really delete, and survive a redeploy.

Campaigns and contacts live in the `campaigns` / `campaign_contacts` tables
(models.Campaign / models.CampaignContact), not in process memory. This test
writes through the real router, throws away the engine and session factory
(what a Railway redeploy does), and reads everything back.

It also pins a bug that used to ship: both DELETE endpoints called
`db.delete(obj)` without `await`. On AsyncSession that is a coroutine, so it
never ran, the commit flushed nothing, and the endpoint answered
{"message": "... deleted"} while the row was still there.

SQLite does not enforce foreign keys unless asked; Postgres always does. The
test turns enforcement on so a campaign delete that leaves its contacts behind
fails here the way it would in production.
"""
import asyncio
import os
import sys
import tempfile

_DB = tempfile.mktemp(suffix=".db")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event  # noqa: E402

import database  # noqa: E402
from routers import outreach  # noqa: E402


def _fresh_engine():
    """Drop the engine/session factory, as a redeploy would, and enforce FKs."""
    database.engine = None
    database.AsyncSessionLocal = None
    eng = database.get_engine()

    @event.listens_for(eng.sync_engine, "connect")
    def _fk_on(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return eng


def _client():
    app = FastAPI()
    app.include_router(outreach.router, prefix="/outreach")
    return TestClient(app)


CAMPAIGN_KEYS = {
    "id", "name", "description", "status", "outreach_type", "target_audience",
    "message_template", "created_at", "updated_at", "scheduled_for",
    "completed_at", "is_active", "custom_metadata",
}
CONTACT_KEYS = {
    "id", "campaign_id", "email", "phone", "name", "contact_data", "status",
    "sent_at", "opened_at", "clicked_at", "replied_at", "custom_metadata",
}


def test_campaigns_and_contacts_survive_a_redeploy_and_delete_for_real():
    _fresh_engine()
    asyncio.run(database.init_db())
    c = _client()

    r = c.post("/outreach/campaigns", json={
        "name": "Spring", "outreach_type": "email", "message_template": "hi {name}",
        "target_audience": {"industry": "dental"},
    })
    assert r.status_code == 200, r.text
    assert set(r.json()) == CAMPAIGN_KEYS
    cid = r.json()["id"]

    ids = []
    for i in range(3):
        r = c.post(f"/outreach/campaigns/{cid}/contacts",
                   json={"campaign_id": cid, "email": f"p{i}@example.com", "name": f"P{i}"})
        assert r.status_code == 200, r.text
        assert set(r.json()) == CONTACT_KEYS
        ids.append(r.json()["id"])

    assert c.patch(f"/outreach/campaigns/{cid}/contacts/{ids[0]}",
                   json={"status": "sent"}).status_code == 200
    assert c.post(f"/outreach/campaigns/{cid}/activate").json()["status"] == "active"

    # --- redeploy -------------------------------------------------------
    _fresh_engine()
    c = _client()

    listed = c.get("/outreach/campaigns").json()
    assert [x["id"] for x in listed] == [cid]
    assert listed[0]["status"] == "active"
    assert listed[0]["target_audience"] == {"industry": "dental"}
    assert len(c.get(f"/outreach/campaigns/{cid}/contacts").json()) == 3

    stats = c.get(f"/outreach/campaigns/{cid}/stats").json()
    assert stats["total_contacts"] == 3 and stats["sent"] == 1

    # --- deletes must actually delete -----------------------------------
    r = c.delete(f"/outreach/campaigns/{cid}/contacts/{ids[1]}")
    assert r.json() == {"message": "Contact deleted"}
    remaining = {x["id"] for x in c.get(f"/outreach/campaigns/{cid}/contacts").json()}
    assert remaining == {ids[0], ids[2]}, remaining

    r = c.delete(f"/outreach/campaigns/{cid}")
    assert r.status_code == 200, r.text
    assert r.json() == {"message": "Campaign deleted"}
    assert c.get(f"/outreach/campaigns/{cid}").status_code == 404
    assert c.get("/outreach/campaigns").json() == []

    # and it is still gone after another redeploy
    _fresh_engine()
    assert _client().get(f"/outreach/campaigns/{cid}").status_code == 404


if __name__ == "__main__":
    test_campaigns_and_contacts_survive_a_redeploy_and_delete_for_real()
    print("✅ outreach deletes: all checks passed")

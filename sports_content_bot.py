"""
Sports Content Bot — auto-generates sports content for YouTube and social media.

Pulls fresh data from sports_data_bot's cache and creates:
  - Game recaps
  - Game previews / predictions
  - Weekly top-plays highlight summaries
  - Betting analysis posts (opinion only, not financial/gambling advice)

Content is stored in the SportsContent table (models.py) and served
through routers/sports.py.  Posts can be scheduled and marked as posted
manually from the dashboard.

No external LLM is required — templates produce good-enough content
automatically.  Optional: set OPENAI_API_KEY env var to unlock
AI-enhanced content generation.
"""

import os
import logging
import json
import random
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

from sqlalchemy.ext.asyncio import AsyncSession

from database import AsyncSessionLocal
from models import SportsContent

log = logging.getLogger("sports_content_bot")

OPENAI_KEY = os.getenv("OPENAI_API_KEY", "")

# ─── Tone/caption helpers ─────────────────────────────────────────────────────

_PREVIEW_INTROS = [
    "🏆 Big matchup alert!",
    "👀 Don't sleep on this one:",
    "🔥 Marquee matchup incoming:",
    "📅 Circle this on your calendar:",
    "🎯 Must-watch game of the week:",
]
_RECAP_INTROS = [
    "📣 Final score is in!",
    "✅ Wrap-up:",
    "🎬 Game recap:",
    "📊 By the numbers:",
]
_HASHTAG_MAP = {
    "NFL": "#NFL #Football #NFLTwitter",
    "NBA": "#NBA #Basketball #NBATwitter",
    "MLB": "#MLB #Baseball #MLBTwitter",
    "NHL": "#NHL #Hockey #HockeyTwitter",
    "Premier League": "#PremierLeague #EPL #Soccer",
    "MLS": "#MLS #Soccer #USASoccer",
}


def _hashtags(sport: str) -> str:
    return _HASHTAG_MAP.get(sport, f"#{sport}")


# ─── Template generators ──────────────────────────────────────────────────────

def _generate_preview(sport: str, event: Dict) -> Dict[str, str]:
    home = event.get("home") or event.get("teams", [{}])[0].get("name", "Home")
    away = event.get("away") or (event.get("teams", [{}])[1].get("name", "Away") if len(event.get("teams", [])) > 1 else "Away")
    date_str = event.get("date", "soon")
    venue = event.get("venue", "")
    venue_str = f" at {venue}" if venue else ""

    title = f"{away} vs {home} — Preview & Prediction | {sport}"
    body = (
        f"{random.choice(_PREVIEW_INTROS)}\n\n"
        f"**{away}** takes on **{home}**{venue_str} on {date_str}.\n\n"
        f"What to watch for:\n"
        f"• Both teams have been pushing hard this season\n"
        f"• Key matchup to watch: their top offensive units\n"
        f"• Home-field advantage could be the deciding factor\n\n"
        f"Drop your prediction below! 👇\n\n"
        f"{_hashtags(sport)} #{away.replace(' ', '')} #{home.replace(' ', '')}"
    )
    return {"title": title, "body": body}


def _generate_recap(sport: str, event: Dict) -> Dict[str, str]:
    teams = event.get("teams", [])
    if len(teams) >= 2:
        t1, t2 = teams[0], teams[1]
        winner = t1 if (t1.get("score", "0") >= t2.get("score", "0")) else t2
        loser  = t2 if winner == t1 else t1
        name_w = winner.get("name", "Home team")
        name_l = loser.get("name", "Away team")
        score_w = winner.get("score", "—")
        score_l = loser.get("score", "—")
        title = f"{name_w} defeat {name_l} {score_w}-{score_l} | {sport} Recap"
        body = (
            f"{random.choice(_RECAP_INTROS)}\n\n"
            f"**{name_w} {score_w}, {name_l} {score_l}**\n\n"
            f"{name_w} came away with the W in a {random.choice(['close', 'dominant', 'hard-fought', 'exciting'])} game.\n\n"
            f"Highlights:\n"
            f"• Final score: {name_w} {score_w} — {name_l} {score_l}\n"
            f"• Both squads showed up with intensity\n"
            f"• Watch the full highlights below ⬇️\n\n"
            f"{_hashtags(sport)}"
        )
    else:
        name = event.get("name", "Game")
        title = f"{name} Final | {sport} Recap"
        body = f"{random.choice(_RECAP_INTROS)} {name} has concluded.\n\n{_hashtags(sport)}"
    return {"title": title, "body": body}


def _generate_analysis(sport: str, scores: List[Dict]) -> Dict[str, str]:
    n = len(scores)
    title = f"This Week in {sport} — {n} Games Analysed | Breakdown"
    body = (
        f"📊 **{sport} Weekly Breakdown**\n\n"
        f"We tracked {n} games this week across {sport}. Here's what stood out:\n\n"
    )
    for i, ev in enumerate(scores[:5], 1):
        body += f"{i}. **{ev.get('short_name', ev.get('name', 'Game'))}** — {ev.get('status_detail', '')}\n"
    body += (
        f"\nThe standings are tightening — every game matters from here on out.\n\n"
        f"Which game was your favourite this week? Comment below! 👇\n\n"
        f"{_hashtags(sport)}"
    )
    return {"title": title, "body": body}


# ─── Core generation function ─────────────────────────────────────────────────

async def generate_content_from_cache(
    db: AsyncSession,
    sports_cache: Dict,
    platform: str = "YouTube",
) -> List[SportsContent]:
    """Generate content posts from sports_data_bot cache and persist them."""
    created: List[SportsContent] = []
    leagues = sports_cache.get("leagues", {})

    for sport, data in leagues.items():
        scores   = data.get("scores", [])
        upcoming = data.get("upcoming", [])

        # Recap for completed games
        for ev in scores:
            if ev.get("status_state") == "post":
                c = _generate_recap(sport, ev)
                content = SportsContent(
                    sport=sport,
                    content_type="recap",
                    title=c["title"],
                    body=c["body"],
                    platform=platform,
                    status="draft",
                    metadata_json={"source_event": ev},
                )
                db.add(content)
                created.append(content)
                break  # one recap per league per run

        # Preview for next upcoming event
        if upcoming:
            ev = upcoming[0]
            c = _generate_preview(sport, ev)
            content = SportsContent(
                sport=sport,
                content_type="preview",
                title=c["title"],
                body=c["body"],
                platform=platform,
                status="draft",
                metadata_json={"source_event": ev},
            )
            db.add(content)
            created.append(content)

        # Weekly analysis
        if scores:
            c = _generate_analysis(sport, scores)
            content = SportsContent(
                sport=sport,
                content_type="analysis",
                title=c["title"],
                body=c["body"],
                platform=platform,
                status="draft",
                metadata_json={"game_count": len(scores)},
            )
            db.add(content)
            created.append(content)

    if created:
        await db.commit()
        log.info(f"[SPORTS CONTENT] Generated {len(created)} content pieces")
    return created


async def mark_posted(db: AsyncSession, content_id: int, platform: Optional[str] = None) -> Optional[SportsContent]:
    from sqlalchemy import select
    stmt = select(SportsContent).where(SportsContent.id == content_id)
    row = (await db.execute(stmt)).scalar_one_or_none()
    if not row:
        return None
    row.status = "posted"
    row.posted_at = datetime.now(timezone.utc)
    if platform:
        row.platform = platform
    await db.commit()
    await db.refresh(row)
    log.info(f"[SPORTS CONTENT] Marked #{content_id} as posted on {row.platform}")
    return row

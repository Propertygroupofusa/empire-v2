"""
Sports Data Bot — live scores, standings, and player stats.

Uses The-Sports-DB free API (no key required for most endpoints):
  https://www.thesportsdb.com/api.php

Also pulls from ESPN's undocumented-but-public JSON endpoints as a
fallback (these have been stable for years).

Runs as a background thread when started from main.py, caching the
latest results every REFRESH_SECONDS so the dashboard endpoint is
always fast.

No API key needed for The-Sports-DB tier-1 (free) calls.
Optional: set THESPORTSDB_API_KEY env var to unlock v2 endpoints.
"""

import os
import logging
import threading
import time
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional

import httpx

log = logging.getLogger("sports_data_bot")

REFRESH_SECONDS = int(os.getenv("SPORTS_DATA_REFRESH_SECONDS", "300"))  # 5 min
THESPORTSDB_KEY = os.getenv("THESPORTSDB_API_KEY", "3")  # "3" = free tier

# Base URLs
TSDB_BASE = f"https://www.thesportsdb.com/api/v1/json/{THESPORTSDB_KEY}"
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"

# ─── Cache ────────────────────────────────────────────────────────────────────
_cache: Dict[str, Any] = {}
_cache_lock = threading.Lock()
_last_refresh: Optional[datetime] = None

LEAGUES = {
    "NFL":  {"tsdb_id": "4391", "espn_sport": "football", "espn_league": "nfl"},
    "NBA":  {"tsdb_id": "4387", "espn_sport": "basketball", "espn_league": "nba"},
    "MLB":  {"tsdb_id": "4424", "espn_sport": "baseball", "espn_league": "mlb"},
    "NHL":  {"tsdb_id": "4380", "espn_sport": "hockey", "espn_league": "nhl"},
    "Premier League": {"tsdb_id": "4328", "espn_sport": "soccer", "espn_league": "eng.1"},
    "MLS":  {"tsdb_id": "4346", "espn_sport": "soccer", "espn_league": "usa.1"},
}


async def _fetch_json(url: str, timeout: int = 10) -> Optional[Dict]:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url, headers={"User-Agent": "empire-v2-sports-bot/1.0"})
            r.raise_for_status()
            return r.json()
    except Exception as e:
        log.warning(f"fetch failed {url}: {e}")
        return None


async def _espn_scores(sport: str, league: str) -> list:
    """Pull live/recent scoreboard from ESPN."""
    url = f"{ESPN_BASE}/{sport}/{league}/scoreboard"
    data = await _fetch_json(url)
    if not data:
        return []
    events = data.get("events", [])
    results = []
    for ev in events[:10]:
        comps = ev.get("competitions", [{}])[0]
        competitors = comps.get("competitors", [])
        teams = []
        for c in competitors:
            teams.append({
                "name": c.get("team", {}).get("displayName", "?"),
                "score": c.get("score", "—"),
                "home": c.get("homeAway") == "home",
            })
        status = ev.get("status", {}).get("type", {})
        results.append({
            "event_id": ev.get("id"),
            "name": ev.get("name", ""),
            "short_name": ev.get("shortName", ""),
            "date": ev.get("date", ""),
            "status_detail": status.get("detail", ""),
            "status_state": status.get("state", ""),  # pre / in / post
            "teams": teams,
        })
    return results


async def _espn_standings(sport: str, league: str) -> list:
    url = f"{ESPN_BASE}/{sport}/{league}/standings"
    data = await _fetch_json(url)
    if not data:
        return []
    rows = []
    for group in data.get("standings", {}).get("entries", [])[:20]:
        team = group.get("team", {})
        stats = {s["name"]: s.get("displayValue", "—") for s in group.get("stats", [])}
        rows.append({"team": team.get("displayName", "?"), **stats})
    return rows


async def _tsdb_next_events(league_id: str) -> list:
    url = f"{TSDB_BASE}/eventsnextleague.php?id={league_id}"
    data = await _fetch_json(url)
    if not data:
        return []
    events = data.get("events") or []
    results = []
    for ev in events[:10]:
        results.append({
            "event": ev.get("strEvent", ""),
            "date": ev.get("dateEvent", ""),
            "time": ev.get("strTime", ""),
            "home": ev.get("strHomeTeam", ""),
            "away": ev.get("strAwayTeam", ""),
            "venue": ev.get("strVenue", ""),
            "round": ev.get("intRound", ""),
        })
    return results


async def refresh_all() -> Dict[str, Any]:
    """Pull fresh data for all leagues and return the snapshot."""
    snapshot: Dict[str, Any] = {
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
        "leagues": {},
    }

    tasks = {}
    for league_name, cfg in LEAGUES.items():
        tasks[league_name] = {
            "scores": _espn_scores(cfg["espn_sport"], cfg["espn_league"]),
            "standings": _espn_standings(cfg["espn_sport"], cfg["espn_league"]),
            "upcoming": _tsdb_next_events(cfg["tsdb_id"]),
        }

    for league_name, coros in tasks.items():
        scores, standings, upcoming = await asyncio.gather(
            coros["scores"], coros["standings"], coros["upcoming"],
            return_exceptions=True
        )
        snapshot["leagues"][league_name] = {
            "scores": scores if isinstance(scores, list) else [],
            "standings": standings if isinstance(standings, list) else [],
            "upcoming": upcoming if isinstance(upcoming, list) else [],
        }
        log.info(f"[SPORTS] {league_name}: {len(snapshot['leagues'][league_name]['scores'])} scores, "
                 f"{len(snapshot['leagues'][league_name]['upcoming'])} upcoming")

    return snapshot


def get_cached() -> Dict[str, Any]:
    with _cache_lock:
        return dict(_cache)


def get_last_refresh() -> Optional[str]:
    if _last_refresh:
        return _last_refresh.isoformat()
    return None


def _run_loop():
    """Background thread: refresh sports data every REFRESH_SECONDS."""
    global _last_refresh
    log.info(f"[SPORTS DATA BOT] Starting — refresh every {REFRESH_SECONDS}s")
    while True:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            snapshot = loop.run_until_complete(refresh_all())
            loop.close()
            with _cache_lock:
                _cache.clear()
                _cache.update(snapshot)
            _last_refresh = datetime.now(timezone.utc)
            log.info("[SPORTS DATA BOT] Cache updated successfully")
        except Exception as e:
            log.error(f"[SPORTS DATA BOT] Refresh error: {e}")
        time.sleep(REFRESH_SECONDS)


def run():
    """Entry point for main.py threading.Thread(target=sports_data_bot.run)"""
    _run_loop()

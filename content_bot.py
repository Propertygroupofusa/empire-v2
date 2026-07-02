"""
=============================================================
  PGUSA CONTENT ENGINE v1 — Autonomous YouTube Bot
  Claude writes -> Synthesia renders -> YouTube uploads
  Runs forever: 1 Short/day + 1 long-form/week

  REVENUE MODEL (works from video #1):
  - Every description + pinned comment links:
      $47 AI Coding Course (Gumroad)
      PGUSA Documents services page
      Payee Trust waitlist
  - AdSense activates later (1K subs + 4K watch hrs)

  REQUIRED ENV VARS (already in empire-v2 Railway service):
    ANTHROPIC_API_KEY        - script generation
    SYNTHESIA_API_KEY        - video rendering
    YOUTUBE_CLIENT_ID        - OAuth app
    YOUTUBE_CLIENT_SECRET    - OAuth app
    YOUTUBE_REFRESH_TOKEN    - channel authorization
  OPTIONAL:
    SYNTHESIA_AVATAR         - avatar id (default: anna_costume1_cameraA)
    CONTENT_BOT_ENABLED      - "true"/"false" kill switch (default true)
    SHORTS_PER_DAY           - default 1
    STATE_DIR                - default /data/bot_state (Railway volume)
=============================================================
"""

import os, json, time, logging, random, tempfile, datetime as dt
import requests

# ------------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------------
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
SYNTHESIA_API_KEY = os.getenv("SYNTHESIA_API_KEY", "")
YT_CLIENT_ID      = os.getenv("YOUTUBE_CLIENT_ID", "")
YT_CLIENT_SECRET  = os.getenv("YOUTUBE_CLIENT_SECRET", "")
YT_REFRESH_TOKEN  = os.getenv("YOUTUBE_REFRESH_TOKEN", "")
AVATAR            = os.getenv("SYNTHESIA_AVATAR", "anna_costume1_cameraA")
ENABLED           = os.getenv("CONTENT_BOT_ENABLED", "true").lower() == "true"
SHORTS_PER_DAY    = int(os.getenv("SHORTS_PER_DAY", "1"))
STATE_DIR         = os.getenv("STATE_DIR", "/data/bot_state")
STATE_FILE        = os.path.join(STATE_DIR, "content_bot_state.json")

LINKS_BLOCK = (
    "\n\n---\n"
    "\U0001F680 Learn to build apps with AI (no coding degree needed): "
    "https://propertygroupusa.gumroad.com/l/yaaap\n"
    "\U0001F4C4 Notary, tax prep & legal docs done for you: "
    "https://propertygroupofusa.github.io/documents/services.html\n"
    "\U0001F4B0 Gig workers: get paid smarter — join the Payee Trust waitlist: "
    "https://propertygroupofusa.github.io/documents/payeetrust-landing.html\n"
)

# Content pillars — each maps to a revenue funnel
PILLARS = [
    {"topic": "how to build an app using AI prompts (beginner tips)",
     "funnel": "course", "audience": "aspiring builders with no coding background"},
    {"topic": "gig economy money tips: getting paid faster, tracking income, tax basics",
     "funnel": "payeetrust", "audience": "Uber/DoorDash/freelance gig workers"},
    {"topic": "small business documents explained: notary, LLC paperwork, tax prep mistakes",
     "funnel": "documents", "audience": "small business owners and side hustlers"},
    {"topic": "AI side hustles you can start from your phone",
     "funnel": "course", "audience": "people wanting extra income with AI tools"},
    {"topic": "real estate paperwork basics: closings, title, what wholesalers must know",
     "funnel": "documents", "audience": "new real estate investors"},
]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("content_bot")


# ------------------------------------------------------------------
# STATE
# ------------------------------------------------------------------
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"published": [], "last_short_date": "", "last_long_date": "",
                "used_topics": []}

def save_state(s):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=2)


# ------------------------------------------------------------------
# 1) CLAUDE — script + metadata generation
# ------------------------------------------------------------------
def claude(prompt, max_tokens=1500):
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": ANTHROPIC_API_KEY,
                 "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": "claude-haiku-4-5-20251001",
              "max_tokens": max_tokens,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=60)
    r.raise_for_status()
    return "".join(b.get("text", "") for b in r.json()["content"])

def generate_package(kind, pillar, recent_titles):
    """kind: 'short' (<=60s) or 'long' (3-5 min). Returns dict."""
    length_rule = ("45-55 seconds when spoken aloud (about 120-140 words)"
                   if kind == "short"
                   else "3 to 4 minutes when spoken aloud (about 450-550 words)")
    prompt = f"""You write scripts for a YouTube channel that helps everyday people make money with AI tools, gig work, and small business services.

Write ONE {('YouTube Short' if kind == 'short' else 'YouTube video')} script about: {pillar['topic']}
Audience: {pillar['audience']}

Rules:
- Script length: {length_rule}
- Hook in the first sentence. Conversational, energetic, no fluff.
- Spoken words ONLY - no stage directions, no [brackets], no emoji in the script.
- End with a call to action matching this funnel: {pillar['funnel']}
  (course = AI coding course link in description; payeetrust = Payee Trust waitlist; documents = document services page)
- Must be a DIFFERENT angle from these recent titles: {recent_titles[-10:] if recent_titles else 'none yet'}

Respond ONLY with JSON, no markdown fences:
{{"title": "clickable YouTube title under 90 chars",
  "script": "the full spoken script",
  "description": "2-3 sentence YouTube description with keywords",
  "tags": ["8-12", "seo", "tags"]}}"""
    raw = claude(prompt)
    raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(raw)


# ------------------------------------------------------------------
# 2) SYNTHESIA — render the video
# ------------------------------------------------------------------
SYNTH_BASE = "https://api.synthesia.io/v2"

def synthesia_create(script_text, title, vertical):
    body = {
        "test": False,
        "title": title[:100],
        "visibility": "private",
        "aspectRatio": "9:16" if vertical else "16:9",
        "input": [{
            "avatar": AVATAR,
            "avatarSettings": {"horizontalAlign": "center", "scale": 1.0,
                                "style": "rectangular", "backgroundColor": "#0B1220"},
            "scriptText": script_text,
            "background": "off_white",
        }],
    }
    r = requests.post(f"{SYNTH_BASE}/videos",
                      headers={"Authorization": SYNTHESIA_API_KEY,
                               "Content-Type": "application/json"},
                      json=body, timeout=60)
    r.raise_for_status()
    return r.json()["id"]

def synthesia_wait(video_id, max_wait_min=45):
    deadline = time.time() + max_wait_min * 60
    while time.time() < deadline:
        r = requests.get(f"{SYNTH_BASE}/videos/{video_id}",
                         headers={"Authorization": SYNTHESIA_API_KEY}, timeout=30)
        r.raise_for_status()
        data = r.json()
        status = data.get("status")
        if status == "complete":
            return data.get("download")
        if status in ("failed", "rejected"):
            raise RuntimeError(f"Synthesia render {status}: {data}")
        log.info(f"  Synthesia {video_id}: {status} — waiting...")
        time.sleep(60)
    raise TimeoutError("Synthesia render exceeded max wait")

def download_video(url):
    tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    with requests.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        for chunk in r.iter_content(chunk_size=1024 * 1024):
            tmp.write(chunk)
    tmp.close()
    return tmp.name


# ------------------------------------------------------------------
# 3) YOUTUBE — refresh token auth + resumable upload
# ------------------------------------------------------------------
def yt_access_token():
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": YT_CLIENT_ID,
        "client_secret": YT_CLIENT_SECRET,
        "refresh_token": YT_REFRESH_TOKEN,
        "grant_type": "refresh_token"}, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]

def yt_upload(filepath, title, description, tags, is_short):
    token = yt_access_token()
    if is_short and "#shorts" not in title.lower():
        title = (title[:80] + " #Shorts")
    meta = {
        "snippet": {"title": title[:100],
                     "description": (description + LINKS_BLOCK)[:4900],
                     "tags": tags[:15],
                     "categoryId": "27"},   # Education
        "status": {"privacyStatus": "public",
                    "selfDeclaredMadeForKids": False},
    }
    # Start resumable session
    init = requests.post(
        "https://www.googleapis.com/upload/youtube/v3/videos"
        "?uploadType=resumable&part=snippet,status",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json; charset=UTF-8",
                 "X-Upload-Content-Type": "video/mp4"},
        json=meta, timeout=60)
    init.raise_for_status()
    upload_url = init.headers["Location"]
    with open(filepath, "rb") as f:
        up = requests.put(upload_url,
                          headers={"Authorization": f"Bearer {token}",
                                   "Content-Type": "video/mp4"},
                          data=f, timeout=1800)
    up.raise_for_status()
    vid = up.json()["id"]
    log.info(f"  \u2705 YouTube upload complete: https://youtu.be/{vid}")
    return vid


# ------------------------------------------------------------------
# PIPELINE
# ------------------------------------------------------------------
def produce(kind):
    state = load_state()
    pillar = random.choice(PILLARS)
    log.info(f"\U0001F3AC Producing {kind} | pillar: {pillar['topic'][:50]}...")
    pkg = generate_package(kind, pillar, state["used_topics"])
    log.info(f"  Script ready: {pkg['title']}")

    synth_id = synthesia_create(pkg["script"], pkg["title"], vertical=(kind == "short"))
    log.info(f"  Synthesia job: {synth_id} (rendering ~10-25 min)")
    dl_url = synthesia_wait(synth_id)
    path = download_video(dl_url)
    log.info(f"  Downloaded render ({os.path.getsize(path)//1024//1024} MB)")

    vid = yt_upload(path, pkg["title"], pkg["description"], pkg.get("tags", []),
                    is_short=(kind == "short"))
    os.unlink(path)

    state["published"].append({"youtube_id": vid, "kind": kind,
                                "title": pkg["title"],
                                "date": dt.date.today().isoformat()})
    state["used_topics"].append(pkg["title"])
    state["used_topics"] = state["used_topics"][-50:]
    if kind == "short":
        state["last_short_date"] = dt.date.today().isoformat()
    else:
        state["last_long_date"] = dt.date.today().isoformat()
    save_state(state)
    return vid


def due_today():
    """Return list of kinds due right now."""
    state = load_state()
    today = dt.date.today()
    out = []
    if state.get("last_short_date") != today.isoformat():
        out.append("short")
    # long-form: Mondays, once
    if today.weekday() == 0 and state.get("last_long_date") != today.isoformat():
        out.append("long")
    return out


def main():
    missing = [k for k, v in {
        "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY,
        "SYNTHESIA_API_KEY": SYNTHESIA_API_KEY,
        "YOUTUBE_CLIENT_ID": YT_CLIENT_ID,
        "YOUTUBE_CLIENT_SECRET": YT_CLIENT_SECRET,
        "YOUTUBE_REFRESH_TOKEN": YT_REFRESH_TOKEN}.items() if not v]
    if missing:
        log.error(f"\u274C Missing env vars: {missing} — bot idle.")
        while True:
            time.sleep(3600)

    log.info("=" * 60)
    log.info("  \U0001F916 PGUSA CONTENT ENGINE — autonomous mode")
    log.info(f"  Schedule: {SHORTS_PER_DAY} Short/day + long-form Mondays")
    log.info("=" * 60)

    while True:
        try:
            if not ENABLED:
                log.info("CONTENT_BOT_ENABLED=false — sleeping 1h")
                time.sleep(3600)
                continue
            for kind in due_today():
                produce(kind)
                time.sleep(120)
        except Exception as e:
            log.error(f"\u274C Pipeline error: {e} — retrying in 30 min")
            time.sleep(1800)
            continue
        time.sleep(1800)  # re-check every 30 min


if __name__ == "__main__":
    main()

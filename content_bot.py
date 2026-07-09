"""
=============================================================
  PGUSA CONTENT ENGINE v2 — Multi-Format Autonomous YouTube Bot
  $0/month stack: Claude scripts + Gemini images + edge-tts
  voice + ffmpeg assembly + YouTube auto-upload

  FORMATS (randomly picked per video):
    cartoon_story  - urban-anime AI stills, cinematic pans,
                     satirical narration (adult-animation vibe,
                     100% original characters)
    caption_talk   - bold animated captions on gradient bg,
                     punchy tips delivery
    card_slides    - clean text-card countdown videos

  SCHEDULE: 1 Short/day + 1 long-form every Monday. Forever.

  REQUIRED ENV VARS:
    ANTHROPIC_API_KEY       - scripts (Claude Haiku)
    YOUTUBE_CLIENT_ID       - OAuth app
    YOUTUBE_CLIENT_SECRET   - OAuth app
    YOUTUBE_REFRESH_TOKEN   - channel auth
  OPTIONAL:
    GEMINI_API_KEY          - cartoon image gen (no key = cartoon
                              format auto-disabled, others still run)
    CONTENT_BOT_ENABLED     - kill switch, default true
    STATE_DIR               - default /data/bot_state
    TTS_VOICE               - default en-US-GuyNeural
    FORMAT_WEIGHTS          - e.g. "cartoon_story:2,caption_talk:2,card_slides:1"
    SHORT_HOURS_UTC         - comma-separated UTC hours to post Shorts at,
                              default "14,18,23" (~9am/1pm/6pm CDT)
    RUN_ONE_NOW             - if "true", publish one Short immediately on
                              startup (ad-hoc test), then resume the normal
                              schedule. Unset it after — the restart policy
                              would otherwise republish on every crash retry.
=============================================================
"""

import os, io, json, time, random, logging, tempfile, subprocess, shutil, asyncio
import datetime as dt
import requests
from PIL import Image, ImageDraw, ImageFont

# ------------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------------
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY", "")
YT_CLIENT_ID      = os.getenv("YOUTUBE_CLIENT_ID", "")
YT_CLIENT_SECRET  = os.getenv("YOUTUBE_CLIENT_SECRET", "")
YT_REFRESH_TOKEN  = os.getenv("YOUTUBE_REFRESH_TOKEN", "")
ENABLED           = os.getenv("CONTENT_BOT_ENABLED", "true").lower() == "true"
STATE_DIR         = os.getenv("STATE_DIR", "/data/bot_state")
STATE_FILE        = os.path.join(STATE_DIR, "content_bot_state.json")
VOICE             = os.getenv("TTS_VOICE", "en-US-GuyNeural")
SHORT_HOURS_UTC   = [int(h) for h in os.getenv("SHORT_HOURS_UTC", "14,18,23").split(",")]
RUN_ONE_NOW       = os.getenv("RUN_ONE_NOW", "false").lower() == "true"

W_SHORT, H_SHORT = 1080, 1920      # 9:16
W_LONG,  H_LONG  = 1920, 1080      # 16:9

LINKS_BLOCK = (
    "\n\n---\n"
    "\U0001F680 Learn to build apps with AI (no coding degree needed): "
    "https://propertygroupusa.gumroad.com/l/yaaap\n"
    "\U0001F4C4 Notary, tax prep & legal docs done for you: "
    "https://propertygroupofusa.github.io/documents/services.html\n"
    "\U0001F4B0 Gig workers: get paid smarter — join the Payee Trust waitlist: "
    "https://propertygroupofusa.github.io/documents/payeetrust-landing.html\n"
)

PILLARS = [
    {"topic": "how to build an app using AI prompts (beginner reality check)",
     "funnel": "course", "audience": "aspiring builders with no coding background"},
    {"topic": "gig economy money: getting paid faster, income tracking, tax traps",
     "funnel": "payeetrust", "audience": "Uber/DoorDash/freelance gig workers"},
    {"topic": "small business paperwork: notary, LLC docs, tax prep mistakes",
     "funnel": "documents", "audience": "small business owners and side hustlers"},
    {"topic": "AI side hustles you can run from a phone",
     "funnel": "course", "audience": "people wanting extra income with AI tools"},
    {"topic": "real estate paperwork basics for new investors and wholesalers",
     "funnel": "documents", "audience": "new real estate investors"},
]

def parse_weights():
    raw = os.getenv("FORMAT_WEIGHTS", "cartoon_story:2,caption_talk:2,card_slides:1")
    out = {}
    for part in raw.split(","):
        try:
            k, v = part.split(":")
            out[k.strip()] = max(0, int(v))
        except Exception:
            continue
    return out or {"caption_talk": 1}

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
        return {"published": [], "shorts_date": "", "shorts_done": [],
                "last_long_date": "", "used_topics": []}

def save_state(s):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=2)


# ------------------------------------------------------------------
# CLAUDE — script generation
# ------------------------------------------------------------------
def claude(prompt, max_tokens=2000):
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": ANTHROPIC_API_KEY,
                 "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": "claude-haiku-4-5-20251001",
              "max_tokens": max_tokens,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=90)
    if r.status_code != 200:
        # Surface Anthropic's actual error message instead of a bare
        # "400 Client Error" with no detail — this is what actually
        # tells us what's wrong (bad model name, malformed request, etc).
        try:
            detail = r.json()
        except Exception:
            detail = r.text
        log.error(f"Claude API error {r.status_code}: {detail}")
        r.raise_for_status()
    return "".join(b.get("text", "") for b in r.json()["content"])

def parse_json(raw):
    raw = raw.strip()
    for fence in ("```json", "```"):
        raw = raw.removeprefix(fence)
    raw = raw.removesuffix("```").strip()
    return json.loads(raw)

def gen_script(fmt, kind, pillar, recent):
    length = ("about 120-140 spoken words (45-55s)"
              if kind == "short" else "about 450-550 spoken words (3-4 min)")
    style = {
        "cartoon_story": ("Sharp, satirical, streetwise storytelling voice — clever "
                           "social commentary with humor, like adult animated satire. "
                           "Original characters only. Tell a mini STORY that lands the point."),
        "caption_talk":  "Direct, high-energy, punchy tips. Second person. No filler.",
        "card_slides":   "Numbered list format, crisp one-liners per point, countdown energy.",
    }[fmt]
    scenes_rule = ""
    scenes_json = ""
    if fmt == "cartoon_story":
        n = 6 if kind == "short" else 10
        scenes_rule = (f'\nAlso include "scenes": exactly {n} vivid visual descriptions '
                       f'(one per story beat) for an illustrator. Urban anime style, '
                       f'original characters, no real people, no brand logos.')
        scenes_json = ', "scenes": ["...scene descriptions..."]'
    prompt = f"""You write scripts for a YouTube channel helping everyday people make money with AI tools, gig work, and small business services.

Write ONE {('YouTube Short' if kind == 'short' else 'YouTube video')} about: {pillar['topic']}
Audience: {pillar['audience']}
Style: {style}
Length: {length}
Hook in the first sentence. Spoken words only, no stage directions, no emoji.
End with a call to action for this funnel: {pillar['funnel']} (course / payeetrust waitlist / document services — the link lives in the description).
Must differ from these recent titles: {recent[-10:] if recent else 'none'}{scenes_rule}

Respond ONLY with JSON, no markdown fences:
{{"title": "clickable title under 90 chars",
  "script": "full spoken script",
  "description": "2-3 sentence SEO description",
  "tags": ["8-12","seo","tags"]{scenes_json}}}"""
    return parse_json(claude(prompt))


# ------------------------------------------------------------------
# TTS (edge-tts, free neural voices)
# ------------------------------------------------------------------
def tts(text, out_mp3):
    import edge_tts
    import aiohttp
    from edge_tts.exceptions import EdgeTTSException

    async def run():
        await edge_tts.Communicate(text, VOICE, rate="+8%").save(out_mp3)

    try:
        asyncio.run(run())
    except aiohttp.ClientResponseError as e:
        # edge-tts retries once internally on a 403 (clock-skew correction);
        # one escaping here means Microsoft's token scheme changed again.
        log.error(f"  ✖ edge-tts HTTP {e.status} ({e.message}) from {e.request_info.url} "
                  "— likely a Microsoft Sec-MS-GEC auth change, not a code bug")
        raise
    except EdgeTTSException as e:
        log.error(f"  ✖ edge-tts error [{type(e).__name__}]: {e} "
                  "— check github.com/rany2/edge-tts issues for a known break/fix")
        raise
    except Exception as e:
        log.error(f"  ✖ edge-tts unexpected failure [{type(e).__name__}]: {e}")
        raise
    return out_mp3

def audio_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", path], capture_output=True, text=True)
    return float(out.stdout.strip())


# ------------------------------------------------------------------
# GEMINI IMAGES (cartoon format)
# ------------------------------------------------------------------
def gemini_image(scene_desc, w, h, out_png):
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           "gemini-2.5-flash-image:generateContent?key=" + GEMINI_API_KEY)
    prompt = (f"Urban anime illustration, bold linework, rich colors, cinematic "
              f"lighting, satirical adult-animation aesthetic. Original characters "
              f"only. No text, no captions, no watermarks, no logos. Scene: {scene_desc}")
    r = requests.post(url, json={
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseModalities": ["IMAGE"]}},
        timeout=120)
    r.raise_for_status()
    parts = r.json()["candidates"][0]["content"]["parts"]
    b64 = next(p["inlineData"]["data"] for p in parts if "inlineData" in p)
    import base64
    img = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
    img = img.resize((w, h))
    img.save(out_png)
    return out_png


# ------------------------------------------------------------------
# PILLOW CARDS (caption_talk / card_slides / fallbacks)
# ------------------------------------------------------------------
PALETTES = [((11, 18, 32), (32, 58, 96)), ((24, 10, 40), (88, 24, 69)),
            ((6, 32, 22), (16, 84, 48)), ((40, 20, 6), (120, 66, 18))]

def font(size):
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()

def gradient_bg(w, h, palette):
    top, bottom = palette
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        t = y / h
        row = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        for x in range(w):
            px[x, y] = row
    return img

def wrap(draw, text, fnt, max_w):
    words, lines, cur = text.split(), [], ""
    for wd in words:
        test = (cur + " " + wd).strip()
        if draw.textlength(test, font=fnt) <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = wd
    if cur:
        lines.append(cur)
    return lines or [""]

def text_card(text, w, h, out_png, palette, accent=(255, 213, 74), small_header=""):
    img = gradient_bg(w, h, palette)
    d = ImageDraw.Draw(img)
    size = 96 if w < h else 84
    fnt = font(size)
    lines = wrap(d, text, fnt, int(w * 0.84))
    while len(lines) * (size + 18) > h * 0.6 and size > 40:
        size -= 8
        fnt = font(size)
        lines = wrap(d, text, fnt, int(w * 0.84))
    total = len(lines) * (size + 18)
    y = (h - total) // 2
    if small_header:
        hf = font(int(size * 0.45))
        d.text((w // 2, max(60, y - size)), small_header, font=hf,
               fill=accent, anchor="ms")
    for ln in lines:
        d.text((w // 2 + 4, y + 4), ln, font=fnt, fill=(0, 0, 0), anchor="ma")
        d.text((w // 2, y), ln, font=fnt, fill=(255, 255, 255), anchor="ma")
        y += size + 18
    img.save(out_png)
    return out_png


# ------------------------------------------------------------------
# FFMPEG ASSEMBLY — Ken Burns motion over stills, synced to voice
# ------------------------------------------------------------------
def kenburns_video(images, audio_path, out_mp4, w, h):
    dur = audio_duration(audio_path)
    per = max(1.5, dur / len(images))
    fps = 30
    tmp = tempfile.mkdtemp()
    clips = []
    for i, img in enumerate(images):
        clip = os.path.join(tmp, f"clip{i}.mp4")
        frames = int(per * fps)
        zoom_in = (i % 2 == 0)
        z = (f"zoompan=z='min(zoom+0.0012,1.25)':d={frames}:s={w}x{h}:fps={fps}"
             if zoom_in else
             f"zoompan=z='if(lte(zoom,1.0),1.25,max(1.0,zoom-0.0012))':d={frames}:s={w}x{h}:fps={fps}")
        subprocess.run(["ffmpeg", "-y", "-loop", "1", "-i", img,
                        "-vf", f"scale={w*2}:{h*2},{z}",
                        "-t", f"{per:.2f}", "-pix_fmt", "yuv420p",
                        "-c:v", "libx264", "-preset", "veryfast", clip],
                       check=True, capture_output=True)
        clips.append(clip)
    listfile = os.path.join(tmp, "list.txt")
    with open(listfile, "w") as f:
        for c in clips:
            f.write(f"file '{c}'\n")
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", listfile,
                    "-i", audio_path, "-c:v", "copy", "-c:a", "aac",
                    "-shortest", out_mp4], check=True, capture_output=True)
    shutil.rmtree(tmp, ignore_errors=True)
    return out_mp4


# ------------------------------------------------------------------
# FORMAT BUILDERS
# ------------------------------------------------------------------
def build_cartoon_story(pkg, kind, workdir):
    w, h = (W_SHORT, H_SHORT) if kind == "short" else (W_LONG, H_LONG)
    audio = tts(pkg["script"], os.path.join(workdir, "voice.mp3"))
    scenes = pkg.get("scenes") or [pkg["title"]] * 6
    palette = random.choice(PALETTES)
    images = []
    for i, sc in enumerate(scenes):
        out = os.path.join(workdir, f"scene{i}.png")
        try:
            gemini_image(sc, w, h, out)
        except Exception as e:
            log.warning(f"  Gemini image failed ({e}) — fallback card")
            text_card(sc[:90], w, h, out, palette)
        images.append(out)
        time.sleep(2)
    return kenburns_video(images, audio, os.path.join(workdir, "final.mp4"), w, h)

def build_caption_talk(pkg, kind, workdir):
    w, h = (W_SHORT, H_SHORT) if kind == "short" else (W_LONG, H_LONG)
    audio = tts(pkg["script"], os.path.join(workdir, "voice.mp3"))
    words = pkg["script"].split()
    chunks = [" ".join(words[i:i + 8]) for i in range(0, len(words), 8)]
    palette = random.choice(PALETTES)
    images = [text_card(c, w, h, os.path.join(workdir, f"cap{i}.png"), palette)
              for i, c in enumerate(chunks)]
    return kenburns_video(images, audio, os.path.join(workdir, "final.mp4"), w, h)

def build_card_slides(pkg, kind, workdir):
    import re
    w, h = (W_SHORT, H_SHORT) if kind == "short" else (W_LONG, H_LONG)
    audio = tts(pkg["script"], os.path.join(workdir, "voice.mp3"))
    sents = [s.strip() for s in re.split(r"(?<=[.!?]) +", pkg["script"]) if s.strip()]
    palette = random.choice(PALETTES)
    images = [text_card(s, w, h, os.path.join(workdir, f"card{i}.png"), palette,
                        small_header=f"{i+1}/{len(sents)}")
              for i, s in enumerate(sents)]
    return kenburns_video(images, audio, os.path.join(workdir, "final.mp4"), w, h)

BUILDERS = {"cartoon_story": build_cartoon_story,
            "caption_talk":  build_caption_talk,
            "card_slides":   build_card_slides}


# ------------------------------------------------------------------
# YOUTUBE UPLOAD
# ------------------------------------------------------------------
def _raise_with_body(r):
    if not r.ok:
        log.error(f"  ✖ {r.status_code} from {r.url}: {r.text[:500]}")
    r.raise_for_status()

def yt_access_token():
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": YT_CLIENT_ID, "client_secret": YT_CLIENT_SECRET,
        "refresh_token": YT_REFRESH_TOKEN, "grant_type": "refresh_token"},
        timeout=30)
    _raise_with_body(r)
    return r.json()["access_token"]

def yt_upload(filepath, title, description, tags, is_short):
    token = yt_access_token()
    if is_short and "#shorts" not in title.lower():
        title = title[:80] + " #Shorts"
    meta = {"snippet": {"title": title[:100],
                         "description": (description + LINKS_BLOCK)[:4900],
                         "tags": tags[:15], "categoryId": "27"},
            "status": {"privacyStatus": "public",
                        "selfDeclaredMadeForKids": False}}
    init = requests.post(
        "https://www.googleapis.com/upload/youtube/v3/videos"
        "?uploadType=resumable&part=snippet,status",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json; charset=UTF-8",
                 "X-Upload-Content-Type": "video/mp4"},
        json=meta, timeout=60)
    _raise_with_body(init)
    with open(filepath, "rb") as f:
        up = requests.put(init.headers["Location"],
                          headers={"Authorization": f"Bearer {token}",
                                   "Content-Type": "video/mp4"},
                          data=f, timeout=1800)
    _raise_with_body(up)
    vid = up.json()["id"]
    log.info(f"  \u2705 Uploaded: https://youtu.be/{vid}")
    return vid


# ------------------------------------------------------------------
# PIPELINE
# ------------------------------------------------------------------
def pick_format():
    weights = parse_weights()
    if not GEMINI_API_KEY:
        weights.pop("cartoon_story", None)
    valid = [(k, v) for k, v in weights.items() if k in BUILDERS and v > 0]
    if not valid:
        valid = [("caption_talk", 1)]
    fmts, wts = zip(*valid)
    return random.choices(fmts, weights=wts, k=1)[0]

def produce(kind, slot=None):
    state = load_state()
    fmt = pick_format()
    pillar = random.choice(PILLARS)
    log.info(f"\U0001F3AC {kind.upper()} | format={fmt} | {pillar['topic'][:45]}...")
    pkg = gen_script(fmt, kind, pillar, state["used_topics"])
    log.info(f"  Script: {pkg['title']}")
    workdir = tempfile.mkdtemp()
    try:
        mp4 = BUILDERS[fmt](pkg, kind, workdir)
        size_mb = os.path.getsize(mp4) // 1024 // 1024
        log.info(f"  Rendered {size_mb} MB — uploading...")
        vid = yt_upload(mp4, pkg["title"], pkg["description"],
                        pkg.get("tags", []), is_short=(kind == "short"))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    state["published"].append({"youtube_id": vid, "kind": kind, "format": fmt,
                                "title": pkg["title"],
                                "date": dt.date.today().isoformat()})
    state["used_topics"] = (state["used_topics"] + [pkg["title"]])[-50:]
    if kind == "short":
        state.setdefault("shorts_done", []).append(slot)
    else:
        state["last_long_date"] = dt.date.today().isoformat()
    save_state(state)

def due_now():
    state = load_state()
    now = dt.datetime.utcnow()
    today_iso = now.date().isoformat()
    if state.get("shorts_date") != today_iso:
        state["shorts_date"] = today_iso
        state["shorts_done"] = []
        save_state(state)
    done = set(state.get("shorts_done", []))
    out = [("short", h) for h in SHORT_HOURS_UTC if now.hour >= h and h not in done]
    if now.weekday() == 0 and state.get("last_long_date") != today_iso:
        out.append(("long", None))
    return out

def main():
    missing = [k for k, v in {"ANTHROPIC_API_KEY": ANTHROPIC_API_KEY,
                               "YOUTUBE_CLIENT_ID": YT_CLIENT_ID,
                               "YOUTUBE_CLIENT_SECRET": YT_CLIENT_SECRET,
                               "YOUTUBE_REFRESH_TOKEN": YT_REFRESH_TOKEN}.items() if not v]
    if missing:
        log.error(f"\u274C Missing env vars: {missing} — bot idle.")
        while True:
            time.sleep(3600)
    log.info("=" * 60)
    log.info("  \U0001F916 PGUSA CONTENT ENGINE v2 — multi-format autonomous mode")
    log.info(f"  Formats active: {list(parse_weights().keys())}"
             + ("" if GEMINI_API_KEY else " (cartoon disabled: no GEMINI_API_KEY)"))
    log.info(f"  Schedule: {len(SHORT_HOURS_UTC)} Shorts/day (UTC hours "
             f"{SHORT_HOURS_UTC}) + long-form Mondays")
    log.info("=" * 60)
    if RUN_ONE_NOW:
        log.info("  \U0001F6A8 RUN_ONE_NOW=true — publishing one Short immediately "
                 "(remove this env var after so crash retries don't republish)")
        try:
            produce("short")
        except Exception as e:
            log.error(f"❌ RUN_ONE_NOW production failed [{type(e).__name__}]: {e}")
    while True:
        try:
            if not ENABLED:
                time.sleep(3600)
                continue
            for kind, slot in due_now():
                produce(kind, slot)
                time.sleep(120)
        except Exception as e:
            log.error(f"\u274C Pipeline error: {e} — retry in 30 min")
            time.sleep(1800)
            continue
        time.sleep(1800)

if __name__ == "__main__":
    main()

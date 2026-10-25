"""Daily business/money Shorts: what to say, and what must never be said.

THE CONSTRAINT THIS IS BUILT AROUND

The chosen niche collides with a named non-monetizable category. YouTube's
July clarification of its inauthentic-content rules describes three groups
that cannot be monetized, and one of them is AI PERSONAS DISCUSSING
SENSITIVE TOPICS - health, finance, legal. (Reported consistently by
TechCrunch, TechRepublic, eWeek and Metricool; I could not find the exact
wording on YouTube's own help pages, so treat the category as well
corroborated rather than quoted.)

The pipeline available here is Synthesia, which makes AI avatar videos. It
even ships a "finance" avatar preset. So the default build - an AI avatar
talking about money, once a day, from one template - hits all three risk
factors at once: AI persona, sensitive topic, mass-produced.

That is avoidable, and avoiding it is most of what this module does.

  * EXPLAIN, NEVER ADVISE. How a business model works. What a term means.
    What a documented case actually did, with the number. Those are facts
    about the world. "Put your money in X" is a persona giving financial
    advice, and it is also the thing that draws complaints when it goes
    wrong for someone.
  * The screen below is a HARD GATE, not a style guide. A script that tells
    the viewer what to do with their money never reaches a video.
  * STRUCTURE VARIES ON PURPOSE. "Generic, repetitive or template-based" is
    the first of the three categories, and enforcement is at CHANNEL level -
    reviewers look at whether uploads differ from one another. One template
    with the nouns swapped is the exact shape that fails.

None of this guarantees monetization. It removes the specific, known
failure modes that are removable in code; audience demand and retention are
not among them.
"""
import hashlib
import logging
import os
import re
from datetime import datetime

log = logging.getLogger("business_shorts")

#: A Short must be vertical and under three minutes. Aiming well under 60s
#: because a spoken script overruns its word count more often than not, and
#: a video that lands at 61 seconds is simply not a Short.
TARGET_SECONDS = 45
#: Rough spoken pace. Deliberately conservative.
WORDS_PER_SECOND = 2.3
MAX_WORDS = int(TARGET_SECONDS * WORDS_PER_SECOND)

ASPECT_RATIO = "9:16"

# ---------------------------------------------------------------- the screen
#
# Each pattern is a thing that turns an explainer into advice, a promise, or
# a persona speaking about someone's own money. Written as regexes over the
# lowercased script. The REASON matters as much as the pattern: a rejection
# that says "failed the screen" teaches nobody anything.

ADVICE_PATTERNS = [
    (r"\byou should (buy|sell|invest|put|move|borrow|refinance)\b",
     "tells the viewer what to do with their money - that is advice from a "
     "persona, the named non-monetizable category"),
    (r"\b(buy|sell|short) (this|that|these) (stock|coin|crypto|token|share)",
     "a specific trade instruction"),
    # RISK DENIAL, IN ANY WORDING. Found by probing: "without any risk
    # whatsoever" sailed through a pattern that only knew "risk-free" and
    # "no risk". The claim is the same one however it is phrased.
    (r"\b(guaranteed|risk[- ]free)\b",
     "promises an outcome that cannot be promised"),
    (r"\b(no|zero|without any|with no|little to no)\s+risk\b",
     "denies risk"),
    (r"\b(can'?t|cannot|never|won'?t) (lose|go wrong|fail)\b",
     "claims it cannot lose"),
    (r"\b(will|going to) (double|triple|10x|moon|skyrocket)\b",
     "predicts a return"),
    # A RATE OF RETURN, IN DIGITS OR WORDS. The same probe showed "returns
    # twenty percent a year" passing a digits-only pattern.
    (r"\b\d+\s*%\s*(returns?|gains?|profit)\s+(guaranteed|every|per)",
     "promises a rate of return"),
    (r"\b(returns?|makes?|pays?|earns?)\s+(you\s+)?"
     r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten|twenty|thirty|"
     r"forty|fifty|sixty|seventy|eighty|ninety|a hundred)[\w\s-]{0,12}"
     r"(%|percent)\s*(a|per|every)\s*(year|month|week|day)",
     "promises a rate of return"),
    (r"\bget rich\b", "get-rich framing"),
    (r"\b(your|my) (portfolio|401k|ira|pension|savings|taxes)\b.{0,40}"
     r"\b(should|need to|must|have to)\b",
     "addresses the viewer's own finances prescriptively"),
    (r"\bfinancial advice\b", "frames itself as financial advice"),
    (r"\b(this|it) is not financial advice\b",
     "the disclaimer is a tell: a script that needs it is giving advice"),
    (r"\b(dm|message|contact) me\b.{0,30}\b(invest|money|returns?|signals?)\b",
     "solicits financial contact"),
    (r"\b(diagnos|prescrib|treat)\w*\b.{0,30}\b(you|your)\b",
     "medical advice"),
    (r"\byou (can|should) sue\b", "legal advice"),
]

#: Phrases that mark the script as an explainer rather than a pitch. At least
#: one must be present - not as a style rule, but because a script with no
#: factual anchor at all is the "generic, template-based" shape.
FACTUAL_MARKERS = [
    # A YEAR OR A DECADE, ANYWHERE. This was r"\bin \d{4}\b" - it required
    # the literal word "in" immediately before a four-digit year, and it
    # rejected two scripts that were dense with dates: "between 1996 and
    # 2002" and "in the 1950s". Both are as checkable as content gets. A
    # screen that refuses good work because of a preposition is the too-tight
    # failure, and that one is worse than too loose because it looks like it
    # is working while making the niche unservable.
    r"\b(1[89]\d{2}|20\d{2})s?\b",
    r"\b\d+(\.\d+)?\s*(%|percent)\b",
    r"\$\s?\d",                  # a dollar figure
    r"\bbecause\b", r"\bthe reason\b", r"\bworks by\b", r"\bmeans\b",
    r"\bthe difference between\b", r"\bfor example\b",
]


def screen_script(script):
    """May this script become a video? (ok: bool, reasons: list[str]).

    A HARD GATE. Called before anything is rendered or uploaded, so a script
    that gives money advice costs an API call and nothing else.

    Fails closed on an empty or unreadable script: nothing is not safe, it is
    nothing, and rendering it would produce a video of silence.
    """
    reasons = []
    if not script or not str(script).strip():
        return False, ["the script is empty"]
    text = str(script).lower()

    for pattern, why in ADVICE_PATTERNS:
        if re.search(pattern, text):
            reasons.append(why)

    words = len(str(script).split())
    if words > MAX_WORDS:
        reasons.append(f"{words} words is too long for a Short - the budget "
                       f"is {MAX_WORDS} at ~{WORDS_PER_SECOND}/sec for "
                       f"{TARGET_SECONDS}s")
    if words < 25:
        reasons.append(f"{words} words is too thin to say anything")

    if not any(re.search(p, text) for p in FACTUAL_MARKERS):
        reasons.append("no factual anchor - no figure, year, or explanation. "
                       "A script with nothing checkable in it is the generic, "
                       "template-based shape that fails channel-level review")
    return (not reasons), reasons


# --------------------------------------------------------------- the angles
#
# NOT a topic list with the nouns swapped. Each angle is a different SHAPE of
# video - a different thing the 45 seconds is doing - because channel-level
# review weighs whether uploads differ from one another, and because a viewer
# can feel a template by the fourth one.

ANGLES = [
    {
        "key": "term",
        "brief": "Define one piece of business or finance vocabulary that "
                 "people nod along to without knowing. Say what it literally "
                 "means, then one concrete example of it in the wild.",
    },
    {
        "key": "mechanism",
        "brief": "Explain how one business actually makes its money - the "
                 "mechanism, not the mission. Who pays, for what, and why "
                 "that is less obvious than it looks.",
    },
    {
        "key": "case",
        "brief": "One documented thing a real company did, with the year and "
                 "the figure. What happened, and the one decision that "
                 "explains it. No lesson tacked on the end.",
    },
    {
        "key": "number",
        "brief": "Take one widely-quoted business statistic and say where it "
                 "actually comes from and what it does and does not measure.",
    },
    {
        "key": "contrast",
        "brief": "Two things people use interchangeably that are not the "
                 "same - gross vs net, revenue vs profit, valuation vs cash. "
                 "The difference, and when it bites.",
    },
    {
        "key": "history",
        "brief": "Where a everyday business practice came from. The origin, "
                 "the date, and why it stuck.",
    },
]


def angle_for(day_index):
    """Rotate through the shapes so consecutive uploads are not the same one."""
    return ANGLES[day_index % len(ANGLES)]


def content_key(angle_key, subject, when=None):
    """Stable identity for one Short, for the scheduler's duplicate gate.

    Built from the angle and the subject rather than the date, so the same
    subject cannot come round again under a new timestamp - which is what a
    date-based key would allow and is exactly the repetition being avoided.
    """
    raw = f"{angle_key}:{str(subject).strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


SYSTEM_PROMPT = """You write 45-second vertical video scripts about business \
and money for a general audience.

HARD RULES, and a script breaking any of them is discarded:
- You EXPLAIN. You never advise. Never tell the viewer what to do with their \
money, never recommend an investment, never predict a price or a return.
- No "you should", no "guaranteed", no "risk-free", no get-rich framing.
- Do not add a "this is not financial advice" disclaimer. If a script needs \
one, it is the wrong script - write a different one.
- Anchor it in something checkable: a year, a figure, a named company, or a \
mechanism you can spell out.
- Plain words. No hype, no "imagine if", no rhetorical questions as a hook.
- Under %(max_words)d words. Spoken aloud, no stage directions, no headings, \
no emoji.

Return ONLY the script text."""


async def write_script(angle, subject):
    """Generate one script. Returns (script, error).

    The screen is applied by the CALLER, not here, so that a rejected script
    is visible as a rejection rather than silently retried into something
    blander. Three strikes is a signal about the subject, not a reason to
    keep rolling.
    """
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        return None, "ANTHROPIC_API_KEY is not set"
    try:
        import anthropic
        from ai_model import TEXT_MODEL
        client = anthropic.AsyncAnthropic(api_key=key)
        msg = await client.messages.create(
            model=TEXT_MODEL,
            max_tokens=600,
            system=SYSTEM_PROMPT % {"max_words": MAX_WORDS},
            messages=[{"role": "user", "content":
                       f"Angle: {angle['brief']}\n\nSubject: {subject}"}],
        )
        return "".join(b.text for b in msg.content if hasattr(b, "text")), None
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:200]}"


def video_request(script, title, callback_id=""):
    """The Synthesia payload for a VERTICAL short.

    aspectRatio matters more than it looks: the default is 16:9, and a
    landscape video is not a Short at all - YouTube will not treat it as one
    however short it is. The existing synthesia_video_bot.create_video sends
    no aspect ratio, so everything it has ever made has been landscape.
    """
    return {
        "script": script,
        "title": title[:100],
        "aspect_ratio": ASPECT_RATIO,
        "callback_id": callback_id,
        # Synthesia's own visibility, not YouTube's. The rendered file stays
        # private to the account either way; what reaches YouTube is decided
        # by the channel's privacy setting.
        "visibility": "private",
    }


def plan(day_index, subject):
    """One day's Short, ready for the scheduler. Nothing is generated here."""
    angle = angle_for(day_index)
    return {
        "angle": angle["key"],
        "brief": angle["brief"],
        "subject": subject,
        "content_key": content_key(angle["key"], subject),
        "aspect_ratio": ASPECT_RATIO,
        "max_words": MAX_WORDS,
        "planned_at": datetime.utcnow().isoformat() + "Z",
    }

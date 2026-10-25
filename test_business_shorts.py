#!/usr/bin/env python3
"""The screen that keeps a money channel out of the named category.

WHAT IS BEING TESTED, AND WHY IT IS THE IMPORTANT FILE

The niche chosen - business and money - collides with one of the three
non-monetizable groups in YouTube's inauthentic-content clarification: AI
personas discussing sensitive topics, finance explicitly among them. The only
video generator wired up here makes AI avatar videos and ships a "finance"
avatar. So the screen in business_shorts is not a style preference; it is the
thing standing between a daily automation and the category it must not be in.

A filter like this fails in two directions and both are tested:

  * TOO LOOSE and advice reaches a video. Every pattern has a positive case.
  * TOO TIGHT and nothing legitimate ever passes, which is worse than no
    filter because it looks like it is working. The explainers below are the
    content this channel is FOR, and they must pass.

Run: python3 test_business_shorts.py
"""
import sys

import business_shorts as bs

_failures = []
_passes = 0


def ok(label, condition, detail=""):
    global _passes
    if condition:
        _passes += 1
        print(f"  ok   {label}")
    else:
        _failures.append(f"{label}{(' - ' + detail) if detail else ''}")
        print(f"  FAIL {label}{(' - ' + detail) if detail else ''}")


# Real explainers in the chosen niche. These MUST pass - a screen that
# rejects these has made the channel impossible rather than safe.
GOOD = [
    "Costco makes more money from membership fees than from selling you "
    "food. In 2023 membership brought in about 4.6 billion dollars, and that "
    "is close to the company's entire operating profit. The groceries run at "
    "a razor thin margin because the point is the renewal, not the receipt. "
    "That is why the hot dog has not changed price since 1985.",

    "Revenue and profit are not the same thing and the difference bites. "
    "Revenue is everything that came in the door. Profit is what is left "
    "after wages, rent, stock and tax. A company can book 10 million in "
    "revenue and still close, because the reason a business fails is almost "
    "never that nobody bought anything.",

    "The phrase working capital means the money a business needs just to "
    "keep operating between paying suppliers and being paid by customers. "
    "For example a shop that pays for stock in 30 days but sells it in 60 "
    "has to fund that gap itself, which is why growing fast can empty a "
    "bank account.",
]

# Each of these is a different way a script turns into advice, a promise, or
# a persona speaking about the viewer's own money.
BAD = [
    ("you should buy this stock before earnings, it is going to double, "
     "the setup is clean and the risk is tiny for anyone watching this",
     "a trade instruction and a prediction"),
    ("here is a guaranteed way to make 20% returns every month with no risk "
     "at all, because the strategy cannot lose when you size it correctly",
     "guarantees and a promised rate"),
    ("your 401k should be moved into index funds today, because that is what "
     "every serious investor does with their retirement money this year",
     "prescriptive about the viewer's own retirement account"),
    ("this is not financial advice but you should put your savings into gold "
     "right now in 2026 before the window closes on this particular move",
     "the disclaimer that marks advice"),
    ("want to get rich in 2026? dm me for the signals and I will show you "
     "exactly how to invest the money you have been sitting on all year",
     "get-rich framing plus financial solicitation"),
]


def test_real_explainers_pass():
    """The content the channel is actually for."""
    for i, script in enumerate(GOOD):
        allowed, reasons = bs.screen_script(script)
        ok(f"explainer {i + 1} passes", allowed is True, str(reasons))


def test_every_advice_shape_is_refused():
    for script, what in BAD:
        allowed, reasons = bs.screen_script(script)
        ok(f"refused: {what}", allowed is False, "it passed the screen")
        ok(f"  and says why ({what})", bool(reasons), str(reasons))


def test_the_disclaimer_is_treated_as_a_tell_not_a_fix():
    """Adding 'not financial advice' must not launder a script through."""
    advice = ("you should buy this stock before earnings in 2026 because it "
              "is going to double and that is simply how the math works out")
    with_disclaimer = advice + " This is not financial advice."
    a1, _ = bs.screen_script(advice)
    a2, r2 = bs.screen_script(with_disclaimer)
    ok("the advice is refused", a1 is False)
    ok("and the disclaimer does not rescue it", a2 is False, str(r2))
    ok("the disclaimer is itself called out",
       any("disclaimer" in x for x in r2), str(r2))


def test_an_empty_or_trivial_script_fails_closed():
    for bad in ("", "   ", None, "Money.", "Business is good in 2026."):
        allowed, reasons = bs.screen_script(bad)
        ok(f"{bad!r} is refused", allowed is False, str(reasons))


def test_length_is_bounded_at_both_ends():
    long_script = ("In 2023 the company earned 4.6 billion dollars because "
                   "the model works by charging a fee. " * 12)
    allowed, reasons = bs.screen_script(long_script)
    ok("an over-long script is refused", allowed is False)
    ok("and the word budget is named",
       any("too long" in r for r in reasons), str(reasons))
    ok("the budget is under a minute of speech",
       bs.MAX_WORDS / bs.WORDS_PER_SECOND <= 60,
       f"{bs.MAX_WORDS} words is {bs.MAX_WORDS / bs.WORDS_PER_SECOND:.0f}s")
    ok("and it targets well under the Shorts ceiling",
       bs.TARGET_SECONDS <= 55, str(bs.TARGET_SECONDS))


def test_a_script_with_nothing_checkable_in_it_is_refused():
    """The 'generic, template-based' shape - fluent, and about nothing."""
    fluff = ("Success in business is all about mindset and hustle. The people "
             "who win are the ones who keep going when others stop. Every day "
             "is a chance to build something that lasts and that is what "
             "separates winners from everyone else in the long run today.")
    allowed, reasons = bs.screen_script(fluff)
    ok("content-free motivation is refused", allowed is False, str(reasons))
    ok("and the reason names the missing anchor",
       any("factual anchor" in r for r in reasons), str(reasons))


def test_the_angles_are_different_shapes_not_one_template():
    keys = [a["key"] for a in bs.ANGLES]
    ok("there is more than one angle", len(bs.ANGLES) >= 5, str(keys))
    ok("each has a distinct key", len(set(keys)) == len(keys), str(keys))
    briefs = [a["brief"] for a in bs.ANGLES]
    ok("and a distinct brief", len(set(briefs)) == len(briefs))
    # Consecutive days must not repeat the shape.
    first_cycle = [bs.angle_for(i)["key"] for i in range(len(bs.ANGLES))]
    ok("a full cycle uses every angle once",
       sorted(first_cycle) == sorted(keys), str(first_cycle))
    ok("and day N+1 is never the same shape as day N",
       all(bs.angle_for(i)["key"] != bs.angle_for(i + 1)["key"]
           for i in range(20)), "two consecutive days share an angle")


def test_the_content_key_stops_a_subject_coming_round_again():
    """Keyed on the subject, not the date - a date-based key would let the
    same video be made again next month, which is the repetition the
    channel-level review is looking for."""
    a = bs.content_key("term", "working capital")
    b = bs.content_key("term", "Working Capital ")
    c = bs.content_key("term", "gross margin")
    d = bs.content_key("mechanism", "working capital")
    ok("the same subject gives the same key", a == b, f"{a} vs {b}")
    ok("a different subject gives a different key", a != c)
    ok("a different angle on it is a different video", a != d)
    ok("the key carries no timestamp",
       bs.content_key("term", "x") == bs.content_key("term", "x"),
       "the key changes between calls, so nothing would ever dedupe")


def test_the_video_request_is_vertical():
    """A landscape video is not a Short, however short it is. The existing
    synthesia_video_bot.create_video sends no aspectRatio at all, and
    Synthesia's default is 16:9."""
    req = bs.video_request("a script", "A title")
    ok("the aspect ratio is 9:16", req["aspect_ratio"] == "9:16", str(req))
    ok("the module constant agrees", bs.ASPECT_RATIO == "9:16")
    ok("the title is bounded for YouTube", len(bs.video_request(
        "s", "T" * 300)["title"]) <= 100)


def test_the_system_prompt_states_the_rules_it_is_screened_on():
    """The model is told the same rules the screen enforces. Not a substitute
    for the screen - a prompt is a request and the screen is the gate - but a
    generator fighting its own filter wastes every call."""
    p = bs.SYSTEM_PROMPT.lower()
    for phrase in ("never advise", "you should", "guaranteed",
                   "not financial advice", "checkable"):
        ok(f"the prompt mentions {phrase!r}", phrase in p)


def test_the_holes_found_by_probing_stay_closed():
    """Written AFTER probing the screen with cases it had not been built for.
    Two got through, and the first is the serious one: a flat promise of a
    return with risk denied, phrased in words instead of symbols.

        "returns twenty percent a year without any risk whatsoever"

    The patterns knew "risk-free" and digit-percentages and nothing else, so
    it passed. A filter that only catches the examples it was written from is
    not a filter.
    """
    holes = [
        ("This method returns twenty percent a year without any risk "
         "whatsoever and it works because the market always recovers "
         "eventually, as it did in 2008.", "spelled-out rate, risk denied"),
        ("The strategy cannot lose because in 2024 the index recovered, and "
         "there is zero risk for anyone who holds for the full ten years.",
         "cannot lose / zero risk"),
        ("It pays you fifty percent every month and there is little to no "
         "risk in 2026 if you follow the system exactly as written.",
         "monthly rate, risk minimised"),
    ]
    for script, what in holes:
        allowed, reasons = bs.screen_script(script)
        ok(f"refused: {what}", allowed is False, "it passed the screen")

    # AND THE OTHER DIRECTION. A real, historical, attributed return figure
    # is a FACT about the world and must still pass - blocking it would make
    # the niche unservable, which is the failure that looks like success.
    factual = ("The S and P 500 returned about 10 percent a year on average "
               "between 1957 and 2023. That is an average across decades and "
               "no single year actually looked like it.")
    allowed, reasons = bs.screen_script(factual)
    ok("a historical return figure stated as fact still passes",
       allowed is True, str(reasons))

    mechanism = ("Spotify pays rights holders about 70 percent of revenue, "
                 "which is why its gross margin sits near 25 percent while "
                 "software companies run at 80. The cost of goods is the "
                 "music itself.")
    ok("and so does a margin explainer",
       bs.screen_script(mechanism)[0] is True, str(bs.screen_script(mechanism)[1]))


def test_zz_nothing_above_failed():
    assert not _failures, f"{len(_failures)} checks failed: {_failures}"


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    for t in TESTS:
        print(f"\n== {t.__name__}")
        try:
            t()
        except BaseException as e:
            ok(f"{t.__name__} ran to completion", False,
               f"raised {type(e).__name__}: {e}")
    print()
    if _failures:
        print(f"{len(_failures)} FAILED, {_passes} passed:")
        for f in _failures:
            print(f"  - {f}")
        sys.exit(1)
    print(f"all {_passes} business-shorts checks passed")

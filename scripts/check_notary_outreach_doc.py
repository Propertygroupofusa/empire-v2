"""
Check NOTARY_OUTREACH_SEQUENCE.md against the code it describes.

WHY THIS EXISTS
---------------
The document is a script for emails a human sends to real businesses. Every
number in it is a promise. The previous version opened by admitting its own
prices were "industry-typical placeholders" because billing wasn't wired up -
which stopped being true once SERVICE_TIERS and Stripe Checkout shipped, and
nobody went back to update it. A document that quotes prices from memory drifts
silently the first time someone edits routers/jobs.py.

So the prices are not typed into the doc and trusted. They are read out of
SERVICE_TIERS here, and the notary's cut is recomputed from NOTARY_PAYOUT_SHARE,
and both must appear verbatim in the text.

WHAT IS ASSERTED
----------------
  1. every `file.py:N` reference in the doc points at the thing it claims
  2. every client price in SERVICE_TIERS appears in the doc
  3. every notary payout quoted to a recruit is really 80% of that price
  4. the fabricated claims that were removed stay removed (the "what was
     removed" section is excluded, since it quotes them deliberately)
  5. both client emails carry a postal address and an opt-out - CAN-SPAM
     requires both, penalties are per-email, and the old templates had neither

No network, no database, no keys.
"""
import ast
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
DOC = REPO / "NOTARY_OUTREACH_SEQUENCE.md"

# What each file:line reference in the doc is supposed to be pointing at.
REFERENCES = {
    ("main.py", "get-notarized"),
    ("notary_request.html", "Continue to Payment"),
    ("routers/jobs.py", "create-checkout"),
    ("routers/jobs.py", "SERVICE_TIERS"),
    ("routers/jobs.py", "NOTARY_PAYOUT_SHARE"),
    ("notary_bot.py", "match_pending_jobs"),
    ("notary_bot.py", "eligible"),
    ("notary_bot.py", "continue"),
    ("models.py", "sales_outreach"),
}

# Claims the rewrite deleted. Each was either unmeasurable or false: there are
# no sent rows in sales_outreach, so there is no reply rate to project from,
# and with zero verified notaries nothing is matched same-day.
REMOVED = ["same-day", "5-8%", "reply rate", "video service",
           "placeholder", "70 outreach", "300+"]


def main():
    failures = []
    print("Notary outreach document\n")
    doc = DOC.read_text()

    # The audit trail at the end quotes the removed claims on purpose.
    body = doc.split("## WHAT WAS REMOVED FROM THIS DOCUMENT")[0]

    # ── 1. line references resolve ─────────────────────────────────────
    refs = sorted(set(re.findall(r'`([\w./]+\.(?:py|html)):(\d+)`', doc)))
    if not refs:
        failures.append("the doc cites no source lines at all")
    for path, lineno in refs:
        src = (REPO / path).read_text().splitlines()
        n = int(lineno)
        if n > len(src):
            failures.append(f"{path}:{n} is past the end of the file ({len(src)} lines)")
            continue
        line = src[n - 1]
        wanted = [tok for f, tok in REFERENCES if f == path and tok in line]
        if not wanted:
            failures.append(f"{path}:{n} does not point at anything it claims to - "
                            f"line reads {line.strip()[:70]!r}. Line numbers shift "
                            f"on every commit; re-resolve it.")
    if not failures:
        print(f"  all {len(refs)} source references resolve")

    # ── 2 & 3. prices and the 80% split are computed, not remembered ───
    jobs_src = (REPO / "routers" / "jobs.py").read_text()
    tiers = ast.literal_eval(
        re.search(r'SERVICE_TIERS = (\{.*?\n\})', jobs_src, re.S).group(1))
    share = float(re.search(r'NOTARY_PAYOUT_SHARE = float\(os\.getenv\('
                            r'"NOTARY_PAYOUT_SHARE", "([\d.]+)"\)\)', jobs_src).group(1))

    for key, info in tiers.items():
        price = info["price"]
        if f"${price:,.2f}" not in doc and f"${price:,.0f}" not in doc:
            failures.append(f"client price ${price:.2f} ({key}) is not in the doc - "
                            f"SERVICE_TIERS changed and the emails now quote a "
                            f"price you would not actually charge")

    # The payouts are checked INSIDE the recruitment template, not anywhere in
    # the document. Searching the whole file passes on a template that promises
    # $180 purely because $160 appears in the pricing table further up - which
    # is exactly what an earlier version of this check did.
    templates = re.findall(r'```\n(.*?)```', doc, re.S)
    recruit = [t for t in templates if "notary-portal" in t]
    if len(recruit) != 1:
        failures.append(f"expected 1 notary recruitment template, found {len(recruit)}")
    else:
        promised = {int(m) for m in re.findall(r'\$(\d+)(?!\d|\.\d)', recruit[0])}
        owed = {round(info["price"] * share) for info in tiers.values()}
        if promised != owed:
            failures.append(
                f"the recruitment email promises {sorted(promised)} but "
                f"{share:.0%} of SERVICE_TIERS is {sorted(owed)} - "
                f"over-promising pay is how you lose a notary after the first job, "
                f"under-promising is how you fail to recruit one")

    if not [f for f in failures if "price" in f or "recruitment email promises" in f]:
        print(f"  all {len(tiers)} prices, and every {share:.0%} payout quoted to a "
              f"recruit, match SERVICE_TIERS")

    # ── 4. deleted claims stay deleted ─────────────────────────────────
    resurfaced = [c for c in REMOVED if c.lower() in body.lower()]
    if resurfaced:
        failures.append(f"unsupported claims are back in the doc: {resurfaced}. "
                        f"sales_outreach has no sent rows, so there is no measured "
                        f"reply rate and no match time to promise.")
    else:
        print(f"  none of the {len(REMOVED)} removed claims have crept back")

    # ── 5. CAN-SPAM ────────────────────────────────────────────────────
    # An opt-out is an INSTRUCTION to the recipient, so match the instruction,
    # not the word. Searching for "stop" passes on a template whose only use of
    # it is the sender writing "and then I'll stop" - which is not an opt-out
    # and would not survive a complaint.
    OPT_OUT = re.compile(r'reply\s+"stop"', re.I)
    client = [t for t in templates if "get-notarized" in t]
    if len(client) != 2:
        failures.append(f"expected 2 client email templates, found {len(client)}")
    for i, t in enumerate(client, 1):
        if "City, State ZIP" not in t:
            failures.append(f"client email {i} has no postal address slot - CAN-SPAM "
                            f"requires one, and penalties are per-email")
        if not OPT_OUT.search(t):
            failures.append(f'client email {i} has no opt-out instruction - CAN-SPAM '
                            f'requires one. Expected a literal \'reply "stop"\'.')
    if client and not [f for f in failures if "CAN-SPAM" in f]:
        print(f"  both client templates carry a postal address and an opt-out")

    print()
    if failures:
        for f in failures:
            print(f"  FAIL  {f}")
        return 1
    print("  PASS  the doc's prices, payouts and code references all match the "
          "shipped code")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# Notary Outreach — Ten Sends

Landing page for every client link below:
**https://empire-v2-production.up.railway.app/get-notarized** (`main.py:1407`)

Notary signup page:
**https://empire-v2-production.up.railway.app/notary-portal**

---

## READ THIS BEFORE SENDING ANYTHING

**A client email sent into a state with no verified notary produces a refund, not
a customer.**

The intake page takes payment *upfront* — the submit button literally reads
"Continue to Payment" (`notary_request.html:88`), and `create_job_checkout`
charges before a notary ever sees the job (`routers/jobs.py:115`). Matching then
happens in `match_pending_jobs()` (`notary_bot.py:72`), which will only assign a
worker who is **all** of:

- `credentials_verified == True`
- `status == "active"`
- `notary_commission_state == job.state` **or** (`ron_authorized` and
  `ron_authorization_state == job.state`)

If nobody clears that bar, the code does `continue` (`notary_bot.py:109`) — the
job sits in `requested` forever, paid. There is no timeout, no auto-refund, and
no alert to the client.

**So the order is forced.** Part 1 first. Do not send a single client email into
a state until at least one verified notary is commissioned in that same state.

**Check before you send:** open `/notary-admin` and confirm a verified notary
exists for the state you're about to email into. If the list is empty, you are
running Part 1 only.

---

## REAL PRICING — live, read out of the code

Source of truth is `SERVICE_TIERS` in `routers/jobs.py:32`. The intake form pulls
these from `GET /jobs/service-tiers` at page load, so the page and this document
cannot drift apart.

| Tier | What it covers | Price |
|---|---|---|
| Standard Notarization | In-person or RON, 1–2 signatures | **$35.00** |
| Remote Online Notarization (RON) | Live video session with a RON-authorized notary | **$45.00** |
| Business & Legal Documents | Contracts, affidavits, powers of attorney | **$55.00** |
| Real Estate Closing / Loan Signing Package | Full closing document package | **$200.00** |

The notary keeps **80%** (`NOTARY_PAYOUT_SHARE`, `routers/jobs.py:59`); the
platform keeps 20%. So a closing package nets you $40 and pays the notary $160.

**Only the $200 tier can fund customer acquisition.** At $35 with a 20% take you
earn $7 a job — that does not pay for the time it takes to send an email. Target
closings.

---

## PART 1: FIVE NOTARY RECRUITMENT MESSAGES — SEND THESE FIRST

Capacity before demand. Five is enough; you need one verified notary per state
you intend to sell into, not a roster.

**Where to find them:** state-specific notary Facebook groups, "Notary Loan
Signing Agents" groups, the NNA member directory, LinkedIn `"notary public" +
[state]`. Prioritize notaries who are **already RON-authorized** — RON coverage
is statewide, so one RON notary unlocks a whole state, while a mobile-only
notary unlocks a radius.

```
Hi [Name],

I run a notary marketplace that routes paid client requests to verified
notaries — real estate closings, POAs, business documents. You don't
prospect for the clients; jobs are matched to you once you're verified.

Pay is 80% of the client price, per job: $28 on a standard notarization,
$36 on a RON session, $44 on business/legal documents, $160 on a full
real estate closing package. Client pays upfront, so you're never doing
the work before it's funded.

Register free: https://empire-v2-production.up.railway.app/notary-portal
You'll enter your commission number, state, and RON authorization — all
three get verified before any job routes to you.

Straight answer, because you'll ask: I'm early, and I don't have steady
volume yet. I'm recruiting notaries and clients in the same week. If
you'd rather wait until there's proven flow, that's fair — tell me and
I'll come back to you when there is.

[Your Name] — [Phone]
```

That last paragraph stays in. A notary who registers expecting volume, gets
nothing for three weeks, and goes inactive costs you the state.

**Track:** name, state, RON yes/no, date messaged, registered yes/no, verified
yes/no.

---

## PART 2: THE TEN CLIENT SENDS

### Build the list of ten

I have not pre-filled company names here on purpose — I don't know your metro,
and a list of plausible-looking firms I invented is worse than an empty table.
Fill it yourself; it takes about fifteen minutes.

**Search:** `"title company" [your city]` and `"escrow officer" [your city]` on
Google Maps and LinkedIn.

**Who you actually want:** the **escrow officer** or **closing coordinator** —
not the owner, not "info@". They are the person whose day is ruined when a signing
falls through, and they schedule notaries without needing anyone's approval.

| # | Company | Contact name | Role | Email | State | Verified notary in that state? | Sent | Replied |
|---|---------|--------------|------|-------|-------|-------------------------------|------|---------|
| 1 |  |  |  |  |  |  |  |  |
| 2 |  |  |  |  |  |  |  |  |
| 3 |  |  |  |  |  |  |  |  |
| 4 |  |  |  |  |  |  |  |  |
| 5 |  |  |  |  |  |  |  |  |
| 6 |  |  |  |  |  |  |  |  |
| 7 |  |  |  |  |  |  |  |  |
| 8 |  |  |  |  |  |  |  |  |
| 9 |  |  |  |  |  |  |  |  |
| 10 |  |  |  |  |  |  |  |  |

If the "verified notary in that state?" column says no, **do not send that row.**

---

### EMAIL 1

Send to all ten rows that cleared the check. Send them by hand, individually.

**Subject:** `Backup notary for [City] closings — $200 flat`

```
Hi [First Name],

You schedule closings in [City]. When your usual notary is booked or
cancels, what happens to that signing?

I run a notary marketplace. Full closing / loan signing package is $200
flat, and the notary is credential-verified before the job routes to
them — commission number, state, and RON authorization all checked
against the state.

Two things I'd rather say up front than have you find out:

  - I'm new. I'm not asking to replace your notary. I'm asking to be the
    number you call when they're unavailable.
  - Payment is upfront, at request time. If I can't staff it, you get
    refunded — I'd rather tell you that now than have you discover it.

If you want to test it, the form takes about two minutes:
https://empire-v2-production.up.railway.app/get-notarized

Or just reply and I'll walk your first one through myself.

[Your Name]
[Your Company] — [Phone]
[Street address, City, State ZIP]

Don't want to hear from me again? Reply "stop" and I'll remove you.
```

---

### EMAIL 2 — one follow-up, four days later, non-responders only

**Subject:** `Re: Backup notary for [City] closings`

```
Hi [First Name],

One follow-up and then I'll stop.

The specific case I'm useful for: it's Thursday, the signing is
tomorrow, and your notary just called out. That's the call I want.

$200 flat for the closing package, verified notary, refunded if I
can't staff it.

https://empire-v2-production.up.railway.app/get-notarized

If it's not useful, no reply needed — I won't email again.

[Your Name]
[Your Company] — [Phone]
[Street address, City, State ZIP]

Reply "stop" to be removed.
```

**Two emails, then stop.** The old third email in this document said "last note"
and then didn't stop, which trains people to ignore you.

---

## LEGAL: DO NOT SKIP THE FOOTER

Cold commercial email in the US is governed by CAN-SPAM. Both templates above
carry the two things it requires and the earlier version of this document lacked:

1. **A real physical postal address.** A PO box registered with the USPS is fine.
2. **A working opt-out**, honored within 10 business days.

Subject lines must not be deceptive. Penalties are per-email, so ten sends with a
missing footer is ten violations, not one.

---

## WHEN THE FIRST REQUEST LANDS

1. Watch `/notary-admin`. Confirm it moved out of `requested`.
2. If it's still `requested` after a few minutes, no eligible notary matched —
   refund it yourself, immediately, and tell the client why. Do not let them
   discover it.
3. If it matched: call the notary directly and make sure the signing happens.
   One flawless closing is what makes an escrow officer save your number, and
   that saved number is worth more than the other nine emails.

---

## WHAT WAS REMOVED FROM THIS DOCUMENT, AND WHY

- **"Rates aren't finalized, billing isn't wired up yet"** — both stale. Prices
  are live in `SERVICE_TIERS` and Stripe Checkout is wired
  (`routers/jobs.py:115`). The real prices are now in the table above.
- **"Most requests matched same-day"** — no data supports this, and with zero
  verified notaries it is false. Nothing in this document now claims a match
  time.
- **"Matched automatically the moment they're submitted"** — matching also
  requires payment *and* a state-eligible verified notary
  (`notary_bot.py:104`). Rewritten to promise a refund instead of a match.
- **"Same formula that worked for the video service"** and **"based on the video
  service's actual reply-rate ranges"** — the `sales_outreach` table
  (`models.py:745`) has no sent rows. There is no measured reply rate to base a
  projection on.
- **"5-8% reply rate = 15-24 replies, 3-7 clients"** — invented. Removed rather
  than re-estimated; ten real sends will produce a real number in a week.
- **"70 outreach touches in week 1", 300+ in month 1** — a volume target for a
  marketplace with no supply. Replaced with ten.
- **Email 3** — added nothing Email 2 didn't, and broke its own promise to stop.

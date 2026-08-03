"""
Test real authentication for the study app.

WHAT IT REPLACED
----------------
verify_user() used to accept any string containing "@" as an identity,
auto-create a StudyUser at tier="free", and return that user's tier. It
verified nothing. Two consequences, both fatal to a paid product:

  - the paywall could not hold: the free allowance is 5/month keyed on
    email, so a different email was another 5, forever
  - a paying customer could be impersonated: Bearer <their-email> gave
    tier="paid" and unlimited generations on their subscription

WHAT IS ASSERTED
----------------
  1. the old bypass is DEAD - Bearer <email> is rejected
  2. forged and wrongly-signed tokens are rejected
  3. signup stores a bcrypt hash, never the password
  4. login issues a working token; a wrong password does not
  5. login cannot be used to enumerate accounts - "no such user", "no
     password set" and "wrong password" are indistinguishable
  6. email is normalised, so Student@X.com and student@x.com are ONE
     account. Without this the free-tier bypass survives in a subtler
     form: same mailbox, different casing, fresh allowance.
  7. auth fails CLOSED with STUDY_JWT_SECRET unset - 500, never open
  8. identity comes from the token's `sub` (account id), not its email
     claim
  9. the hourly rate limit applies to PAID accounts too

Runs against a real Postgres because signup/login are database
operations; SQLite would not exercise the unique-email constraint the
same way. Set TEST_DATABASE_URL.
"""
import asyncio
import os
import pathlib
import sys
import types

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

SECRET = "test-study-secret"
os.environ["STUDY_JWT_SECRET"] = SECRET
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake")
os.environ["STUDY_GENERATION_ENABLED"] = "true"
os.environ["STUDY_RATE_LIMIT_PER_HOUR"] = "3"


def main():
    db_url = os.getenv("TEST_DATABASE_URL", "").strip()
    if not db_url:
        print("  SKIPPED - needs a real Postgres (TEST_DATABASE_URL).")
        print("  signup/login are database operations; skipping is not a pass.")
        return 0
    os.environ["DATABASE_URL"] = db_url
    return asyncio.run(run(db_url))


async def run(db_url):
    from sqlalchemy import select, delete
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from fastapi import HTTPException
    from jose import jwt

    import database as real_db
    from models import StudyUser, StudyMaterial
    import study_auth
    from routers import study

    failures = []
    print("Study authentication\n")

    engine = create_async_engine(db_url)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as c:
        await c.run_sync(real_db.Base.metadata.create_all)

    async def clean():
        async with Session() as s:
            await s.execute(delete(StudyMaterial).where(
                StudyMaterial.user_id.like("%@studytest.invalid")))
            await s.execute(delete(StudyUser).where(
                StudyUser.email.like("%@studytest.invalid")))
            await s.commit()

    await clean()
    try:
        # ── 3. signup stores a hash ────────────────────────────────────
        async with Session() as s:
            out = await study.signup(
                study.SignupRequest(email="Alice@StudyTest.Invalid",
                                    password="correct-horse-battery"), s)
        token = out["token"]
        async with Session() as s:
            alice = (await s.execute(select(StudyUser).where(
                StudyUser.email == "alice@studytest.invalid"))).scalar_one_or_none()
        if alice is None:
            failures.append("signup did not create the account (email not normalised?)")
        elif not alice.password_hash or not alice.password_hash.startswith("$2"):
            failures.append(f"password_hash is not a bcrypt hash: {alice.password_hash!r}")
        elif "correct-horse-battery" in (alice.password_hash or ""):
            failures.append("the password appears in the stored hash")
        else:
            print(f"  signup stored a bcrypt hash, email normalised to {alice.email}")

        # ── 6. casing does not create a second account ─────────────────
        try:
            async with Session() as s:
                await study.signup(
                    study.SignupRequest(email="ALICE@studytest.invalid",
                                        password="another-password"), s)
            failures.append("a second account was created for the same email in "
                            "different casing - the free-tier bypass survives")
        except HTTPException as e:
            if e.status_code != 409:
                failures.append(f"duplicate signup gave {e.status_code}, expected 409")
            else:
                print("  duplicate email in different casing is rejected (409)")

        # ── 1. the old bypass is dead ──────────────────────────────────
        for bad in ("Bearer alice@studytest.invalid",
                    "Bearer anything@example.com"):
            try:
                async with Session() as s:
                    await study.verify_user(bad, s)
                failures.append(f"OLD BYPASS STILL WORKS: {bad!r} was accepted")
            except HTTPException as e:
                if e.status_code != 401:
                    failures.append(f"{bad!r} gave {e.status_code}, expected 401")
        print("  Bearer <email> is rejected — the old bypass is closed")

        # ── 2. forged / wrongly-signed tokens ──────────────────────────
        forged = jwt.encode({"sub": str(alice.id), "email": alice.email,
                             "exp": 9999999999}, "wrong-secret", algorithm="HS256")
        for label, tok in (("wrong signing key", forged),
                           ("garbage", "not.a.token"),
                           ("empty", "")):
            try:
                async with Session() as s:
                    await study.verify_user(f"Bearer {tok}", s)
                failures.append(f"a {label} token was accepted")
            except HTTPException as e:
                if e.status_code != 401:
                    failures.append(f"{label} token gave {e.status_code}, expected 401")
        print("  forged, malformed and wrongly-signed tokens are all rejected")

        # A genuine token works. Wrapped, because if verify_user is ever
        # reverted to the bearer-email scheme a JWT contains no "@" and
        # this raises - an uncaught 401 here kills the script before it
        # prints any verdict, and "the test errored" reads very
        # differently from "authentication was removed".
        try:
            async with Session() as s:
                who = await study.verify_user(f"Bearer {token}", s)
            if who["email"] != "alice@studytest.invalid":
                failures.append(f"valid token resolved to {who['email']!r}")
            else:
                print("  a token from signup authenticates correctly")
        except HTTPException as e:
            failures.append(f"a VALID token was rejected ({e.status_code}: "
                            f"{e.detail}) - is verify_user still JWT-based?")

        # ── 8. identity comes from `sub`, not the email claim ──────────
        async with Session() as s:
            bob_out = await study.signup(
                study.SignupRequest(email="bob@studytest.invalid",
                                    password="bobs-long-password"), s)
            bob = (await s.execute(select(StudyUser).where(
                StudyUser.email == "bob@studytest.invalid"))).scalar_one()
        # Bob's real id, Alice's email in the claim, signed correctly.
        mixed = jwt.encode({"sub": str(bob.id), "email": "alice@studytest.invalid",
                            "exp": 9999999999}, SECRET, algorithm="HS256")
        try:
            async with Session() as s:
                resolved = await study.verify_user(f"Bearer {mixed}", s)
            if resolved["email"] != "bob@studytest.invalid":
                failures.append(f"identity followed the EMAIL claim, not sub - resolved "
                                f"to {resolved['email']!r} for bob's id. A tampered "
                                f"claim must not redirect the account.")
            else:
                print("  identity follows the token's sub, not its email claim")
        except HTTPException as e:
            failures.append(f"a validly-signed token was rejected ({e.status_code}) - "
                            f"is verify_user still JWT-based?")

        # ── 4 & 5. login ───────────────────────────────────────────────
        async with Session() as s:
            good = await study.login(study.LoginRequest(
                email="alice@studytest.invalid", password="correct-horse-battery"), s)
        if not good.get("token"):
            failures.append("login with the right password returned no token")
        else:
            print("  login with the correct password issues a token")

        messages = set()
        for email, pw in (("alice@studytest.invalid", "wrong-password"),
                          ("nobody@studytest.invalid", "any-password")):
            try:
                async with Session() as s:
                    await study.login(study.LoginRequest(email=email, password=pw), s)
                failures.append(f"login succeeded for {email} with a bad password")
            except HTTPException as e:
                messages.add((e.status_code, str(e.detail)))
        if len(messages) != 1:
            failures.append(f"login distinguishes failure modes, which enumerates "
                            f"accounts: {messages}")
        else:
            print(f"  wrong password and unknown account are indistinguishable "
                  f"{messages.pop()}")

        # ── 9. the rate limit binds PAID accounts too ──────────────────
        async with Session() as s:
            alice.tier = "paid"
            s.add(alice)
            await s.commit()
            for _ in range(3):     # STUDY_RATE_LIMIT_PER_HOUR=3
                s.add(StudyMaterial(user_id="alice@studytest.invalid",
                                    material_type="guide", source_text="x",
                                    generated_content={}, title="t", topic="t"))
            await s.commit()
        try:
            async with Session() as s:
                await study.check_rate_limit({"email": "alice@studytest.invalid",
                                              "tier": "paid"}, s)
            failures.append("a PAID account was not rate limited - a single "
                            "subscriber can outspend $9.99 in API cost long before "
                            "the monthly cap stops them")
        except HTTPException as e:
            if e.status_code != 429:
                failures.append(f"rate limit gave {e.status_code}, expected 429")
            else:
                print("  the hourly rate limit binds paid accounts too")

        # ── 7. fails closed without a secret ───────────────────────────
        os.environ.pop("STUDY_JWT_SECRET", None)
        try:
            async with Session() as s:
                await study.verify_user(f"Bearer {token}", s)
            failures.append("AUTH FELL OPEN: a token was accepted with "
                            "STUDY_JWT_SECRET unset")
        except HTTPException as e:
            if e.status_code != 500:
                failures.append(f"unset secret gave {e.status_code}, expected 500")
            else:
                print("  with STUDY_JWT_SECRET unset, auth fails closed (500)")
        finally:
            os.environ["STUDY_JWT_SECRET"] = SECRET
    finally:
        await clean()
        await engine.dispose()

    print()
    if failures:
        for f in failures:
            print(f"  FAIL  {f}")
        return 1
    print("  PASS  identity is verified, the paywall is enforceable, and auth "
          "fails closed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

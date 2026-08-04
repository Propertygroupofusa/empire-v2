"""
Test that customer auth fails CLOSED when SECRET_KEY is unset.

WHAT WENT WRONG
---------------
auth_utils.py signed and verified every customer JWT with

    SECRET_KEY = os.getenv("SECRET_KEY", "your-secret-key-change-in-production")

In a public repository that trailing string is not a placeholder, it is a
published signing key. With SECRET_KEY absent from the environment,
anyone who read the source could mint

    jwt.encode({"sub": "<any user id>"}, "your-secret-key-change-in-production")

and routers/auth.py would accept it as that user. No credential was
leaked and nothing looked broken, which is precisely what makes it worse
than a leak: a leak gets noticed and rotated.

The fix is not a better default. There is no safe default for a signing
key. Absent configuration has to mean "cannot issue and cannot verify".

WHAT IS ASSERTED
----------------
  1. no non-empty literal default for SECRET_KEY, in any module
  2. the old published key appears nowhere in the repository
  3. with SECRET_KEY unset, issuing a token raises 500 rather than
     signing with a fallback
  4. with SECRET_KEY unset, decode_token returns None - every token
     rejected, nothing verified against a fallback
  5. THE ATTACK IS DEAD: a token forged with the old published key is
     rejected once a real key is configured
  6. with a real key, sign/verify still round-trips - this is a fix, not
     a lobotomy

No network, no database.
"""
import ast
import importlib
import os
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

OLD_KEY = "your-secret-key-change-in-production"


def reload_auth_utils():
    """Import auth_utils fresh so it re-reads SECRET_KEY from the env."""
    sys.modules.pop("auth_utils", None)
    return importlib.import_module("auth_utils")


def main():
    failures = []
    print("SECRET_KEY fails closed\n")

    # ── 1. no insecure default anywhere ────────────────────────────────
    offenders = []
    for path in sorted(REPO.glob("*.py")) + sorted(REPO.glob("routers/*.py")):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "getenv" and len(n.args) == 2
                    and isinstance(n.args[0], ast.Constant)
                    and n.args[0].value == "SECRET_KEY"
                    and isinstance(n.args[1], ast.Constant)
                    and n.args[1].value):
                offenders.append(f"{path.name}:{n.lineno}")
    if offenders:
        failures.append(f"SECRET_KEY still has a non-empty default at {offenders} - "
                        f"any default is a published signing key")
    else:
        print("  SECRET_KEY has no fallback default")

    # ── 2. the old key is gone from the working tree ───────────────────
    hits = []
    for path in REPO.rglob("*.py"):
        if ".git" in path.parts:
            continue
        if OLD_KEY in path.read_text():
            hits.append(str(path.relative_to(REPO)))
    # this test file names the key on purpose, to assert its death
    hits = [h for h in hits if not h.endswith("test_secret_key_fail_closed.py")]
    if hits:
        failures.append(f"the old published key is still present in {hits}")
    else:
        print("  the old published key appears nowhere in the tree")

    # ── 3 & 4. unset => cannot issue, cannot verify ────────────────────
    os.environ.pop("SECRET_KEY", None)
    try:
        au = reload_auth_utils()
        from fastapi import HTTPException

        for fn in ("create_access_token", "create_refresh_token"):
            try:
                getattr(au, fn)({"sub": "1"})
                failures.append(f"{fn} issued a token with SECRET_KEY unset - it is "
                                f"signing with a fallback key")
            except HTTPException as e:
                if e.status_code != 500:
                    failures.append(f"{fn} raised {e.status_code}, expected 500")
            except Exception as e:
                failures.append(f"{fn} raised {type(e).__name__} instead of "
                                f"HTTPException(500): {e}")

        import jwt as pyjwt
        forged = pyjwt.encode({"sub": "1"}, OLD_KEY, algorithm="HS256")
        if au.decode_token(forged) is not None:
            failures.append("decode_token ACCEPTED a token forged with the old "
                            "published key while SECRET_KEY was unset")
        if au.decode_token("garbage") is not None:
            failures.append("decode_token accepted garbage with SECRET_KEY unset")
        print("  with SECRET_KEY unset: issuing raises 500, every token rejected")
    except Exception as e:
        failures.append(f"could not exercise auth_utils with SECRET_KEY unset: "
                        f"{type(e).__name__}: {e}")

    # ── 5 & 6. real key: forged token dead, real token works ───────────
    os.environ["SECRET_KEY"] = "a-real-secret-for-this-test"
    try:
        au = reload_auth_utils()
        import jwt as pyjwt

        forged = pyjwt.encode({"sub": "1"}, OLD_KEY, algorithm="HS256")
        if au.decode_token(forged) is not None:
            failures.append("THE ATTACK IS STILL LIVE: a token signed with the old "
                            "published key was accepted even with a real SECRET_KEY "
                            "configured")
        else:
            print("  a token forged with the old published key is rejected")

        tok = au.create_access_token({"sub": "42"})
        payload = au.decode_token(tok)
        if not payload or payload.get("sub") != "42":
            failures.append(f"a genuine token did not round-trip: {payload!r}")
        else:
            print("  with a real SECRET_KEY, sign and verify still round-trip")
    except Exception as e:
        failures.append(f"could not exercise auth_utils with a real SECRET_KEY: "
                        f"{type(e).__name__}: {e}")
    finally:
        os.environ.pop("SECRET_KEY", None)

    print()
    if failures:
        for f in failures:
            print(f"  FAIL  {f}")
        return 1
    print("  PASS  no fallback key, unset means closed, forged tokens rejected")
    return 0


if __name__ == "__main__":
    sys.exit(main())

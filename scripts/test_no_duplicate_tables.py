"""
Test that no two ORM classes claim the same table.

WHAT WENT WRONG
---------------
email_campaigns.py declared a class named CampaignContact against
__tablename__ = "campaign_contacts" - a table models.py already owns with
a completely different shape (campaign_id/email/contact_data versus
customer_email/referral_code/company). Both inherited the SAME Base
imported from database.py, so both registered on one MetaData:

    sqlalchemy.exc.InvalidRequestError: Table 'campaign_contacts' is
    already defined for this MetaData instance.

It stayed latent only because main.py never imports email_campaigns.
Anything importing both - a script, a router, a future startup task -
died at import time. The referral code path had therefore never run
against a table with its own columns.

There was a second, quieter half to the same bug. run_migrations() walks
Base.metadata.sorted_tables, so it only creates tables whose model is in
the registry. A model declared in a module the app never imports is not
in that registry, which means "referral_contacts" would never have been
created even if the collision were resolved. Fixing it means declaring
the model in models.py, which main.py does import.

WHAT IS ASSERTED
----------------
  1. importing every module that declares an ORM model raises nothing -
     this is the exact failure, reproduced directly
  2. no __tablename__ is claimed by two different classes
  3. models.py, which main.py imports, is the single place tables are
     declared - a model anywhere else is invisible to run_migrations()
  4. referral_contacts is in Base.metadata, so migrations will create it
  5. the referral code path really uses the renamed model

No network, no database, no keys.
"""
import ast
import importlib
import os
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")


def declaring_modules():
    """Every .py in the repo root that declares a __tablename__, tagged with
    whether it uses the app's SHARED Base or a private one.

    The distinction is the whole point. beauty_support_bot.py and
    data_entry_bot.py both declare models outside models.py, but each calls
    declarative_base() to get its own MetaData and runs its own
    create_all - self-contained, no collision, nothing for
    run_migrations() to miss. Only a module that imports Base from
    database.py is sharing the app's registry, and only those have to be
    declared in models.py.

    Flagging the private-Base bots would be a false positive, and a check
    that cries wolf is a check people stop reading.
    """
    out = {}
    for path in sorted(REPO.glob("*.py")):
        try:
            src = path.read_text()
            tree = ast.parse(src)
        except SyntaxError:
            continue

        private_base = any(
            isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "Base" for t in n.targets)
            and isinstance(n.value, ast.Call)
            and getattr(n.value.func, "id", None) == "declarative_base"
            for n in ast.walk(tree))

        shared_base = any(
            isinstance(n, ast.ImportFrom) and n.module == "database"
            and any(a.name == "Base" for a in n.names)
            for n in ast.walk(tree))

        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for stmt in node.body:
                if (isinstance(stmt, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == "__tablename__"
                                for t in stmt.targets)
                        and isinstance(stmt.value, ast.Constant)):
                    out.setdefault(path.name, {"shared": shared_base and not private_base,
                                               "models": []})
                    out[path.name]["models"].append((node.name, stmt.value.value))
    return out


def main():
    failures = []
    print("Duplicate table declarations\n")

    declared = declaring_modules()

    # ── 2. no physical table name has two owners ───────────────────────
    # Checked across ALL modules, private Base or not: two classes writing
    # one table name in one Postgres database is a data hazard even when
    # separate MetaData objects keep it from raising at import.
    by_table = {}
    for module, info in declared.items():
        for cls, table in info["models"]:
            by_table.setdefault(table, []).append(f"{module}:{cls}")

    dupes = {t: o for t, o in by_table.items() if len(o) > 1}
    for table, owners in sorted(dupes.items()):
        failures.append(f'"{table}" is declared by {owners} - two classes on one '
                        f'table. On a shared Base this raises InvalidRequestError '
                        f'as soon as both are imported; on separate Bases it '
                        f'silently points two different schemas at one table.')
    if not dupes:
        print(f"  {len(by_table)} table names, each declared exactly once")

    # ── 3. anything on the SHARED Base lives in models.py ──────────────
    stray = {m: [c for c, _ in i["models"]]
             for m, i in declared.items() if i["shared"] and m != "models.py"}
    if stray:
        failures.append(f"models on the shared Base declared outside models.py: "
                        f"{stray}. run_migrations() walks "
                        f"Base.metadata.sorted_tables, so a model in a module "
                        f"main.py never imports is never created.")
    else:
        private = sorted(m for m, i in declared.items() if not i["shared"])
        print(f"  every shared-Base model is in models.py "
              f"(private-Base modules exempt: {', '.join(private) or 'none'})")

    # ── 1. the real failure, reproduced ────────────────────────────────
    # Importing models THEN email_campaigns is precisely what used to
    # raise. Anything other than a clean import is a regression, so the
    # except is broad on purpose - an uncaught traceback here would kill
    # the script before it printed a verdict.
    try:
        importlib.import_module("models")
        importlib.import_module("email_campaigns")
        print("  models + email_campaigns import together cleanly")
    except Exception as e:
        failures.append(f"importing models and email_campaigns together still fails - "
                        f"{type(e).__name__}: {e}")

    # ── 4 & 5. the renamed table is registered and actually used ───────
    try:
        from database import Base
        import models
        import email_campaigns

        tables = set(Base.metadata.tables)
        if "referral_contacts" not in tables:
            failures.append('"referral_contacts" is not in Base.metadata, so '
                            'run_migrations() will never create it')
        else:
            print(f"  referral_contacts is registered ({len(tables)} tables total)")

        cols = set(models.ReferralContact.__table__.columns.keys())
        needed = {"customer_email", "referral_code", "company", "status"}
        missing = needed - cols
        if missing:
            failures.append(f"ReferralContact is missing columns the referral code "
                            f"writes: {sorted(missing)}")

        if email_campaigns.ReferralContact is not models.ReferralContact:
            failures.append("email_campaigns uses a different ReferralContact than "
                            "models.py - the duplicate declaration is back")
        else:
            print("  email_campaigns uses models.ReferralContact, not a local copy")

        contact_cols = set(models.CampaignContact.__table__.columns.keys())
        if contact_cols & {"customer_email", "referral_code"}:
            failures.append("CampaignContact and ReferralContact have been merged - "
                            "they are different entities (campaign recipients vs "
                            "referral enrollees) and should stay separate")
    except Exception as e:
        failures.append(f"could not inspect the models: {type(e).__name__}: {e}")

    print()
    if failures:
        for f in failures:
            print(f"  FAIL  {f}")
        return 1
    print("  PASS  every table has exactly one owner, declared where migrations "
          "can see it")
    return 0


if __name__ == "__main__":
    sys.exit(main())

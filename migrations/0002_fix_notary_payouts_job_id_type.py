"""
Fix notary_payouts.job_id column type: INTEGER -> VARCHAR.

WHY THIS EXISTS
----------------
jobs.id (see models.Job) is a VARCHAR primary key, but
notary_payouts.job_id was declared Integer in models.py. SQLAlchemy
cannot create a foreign key between mismatched column types, so
Base.metadata.create_all() (database.py) failed on every startup with:

    sqlalchemy.exc.ProgrammingError: (psycopg2.errors.FeatureNotSupported)
    foreign key constraint "notary_payouts_job_id_fkey" cannot be implemented
    DETAIL:  Key columns "job_id" are of incompatible types:
    integer and character varying.

This crashed app startup entirely, blocking all trading/notary
functionality, not just the notary_payouts table.

This migration brings any already-existing notary_payouts table (created
before the model was fixed) in line with the corrected model: drop the
stale FK if present, widen job_id to VARCHAR, then recreate the FK. It is
safe to run repeatedly - every step is guarded/idempotent - and safe to
run against a database where notary_payouts doesn't exist yet at all
(nothing to do; Base.metadata.create_all() will create it correctly from
the fixed model).
"""
import asyncio
from database import engine
from sqlalchemy import text, inspect


async def migrate():
    """Convert notary_payouts.job_id from INTEGER to VARCHAR and make sure
    the FK to jobs.id exists with matching types."""

    async with engine.begin() as conn:
        tables = await conn.run_sync(lambda c: inspect(c).get_table_names())

        if "notary_payouts" not in tables:
            print("notary_payouts table does not exist yet - nothing to migrate "
                  "(create_all will create it with the correct VARCHAR job_id)")
            return True

        columns = await conn.run_sync(
            lambda c: {col["name"]: col["type"] for col in c.get_columns("notary_payouts")}
        )
        job_id_type = str(columns.get("job_id", "")).upper()

        if "VARCHAR" in job_id_type or "CHARACTER VARYING" in job_id_type or "TEXT" in job_id_type:
            print(f"notary_payouts.job_id already {job_id_type} - nothing to do")
            return True

        try:
            if engine.dialect.name == "postgresql":
                # Drop the FK first (if it exists) - can't alter a column's
                # type while a mismatched constraint depends on it, and the
                # constraint may or may not have made it into the database
                # depending on how far create_all() got before failing.
                await conn.execute(text(
                    "ALTER TABLE notary_payouts DROP CONSTRAINT IF EXISTS "
                    "notary_payouts_job_id_fkey"
                ))

                await conn.execute(text(
                    "ALTER TABLE notary_payouts ALTER COLUMN job_id TYPE VARCHAR "
                    "USING job_id::text"
                ))

                await conn.execute(text(
                    "ALTER TABLE notary_payouts ADD CONSTRAINT "
                    "notary_payouts_job_id_fkey FOREIGN KEY (job_id) "
                    "REFERENCES jobs(id)"
                ))
            else:
                # SQLite has no ALTER COLUMN TYPE and is used only for local/
                # test runs, where this drift doesn't occur (tables are
                # created fresh from the already-fixed model).
                print(f"Skipping type conversion on non-Postgres dialect "
                      f"({engine.dialect.name})")
                return True

            print("✅ notary_payouts.job_id converted from INTEGER to VARCHAR "
                  "and FK to jobs.id recreated")
            return True
        except Exception as e:
            print(f"❌ Failed to migrate notary_payouts.job_id: {e}")
            return False


if __name__ == "__main__":
    result = asyncio.run(migrate())
    exit(0 if result else 1)

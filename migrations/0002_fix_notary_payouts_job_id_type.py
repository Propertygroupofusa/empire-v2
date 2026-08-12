"""
Fix notary_payouts column types: job_id and worker_id, INTEGER -> VARCHAR.

WHY THIS EXISTS
----------------
jobs.id (see models.Job) and workers.id (see models.Worker) are VARCHAR
primary keys, but notary_payouts.job_id and notary_payouts.worker_id were
both declared Integer in models.py. SQLAlchemy cannot create a foreign
key between mismatched column types, so Base.metadata.create_all()
(database.py) failed on every startup with, first:

    sqlalchemy.exc.ProgrammingError: (psycopg2.errors.FeatureNotSupported)
    foreign key constraint "notary_payouts_job_id_fkey" cannot be implemented
    DETAIL:  Key columns "job_id" are of incompatible types:
    integer and character varying.

and then, once job_id was fixed, the exact same failure for worker_id:

    sqlalchemy.exc.ProgrammingError: (psycopg2.errors.FeatureNotSupported)
    foreign key constraint "notary_payouts_worker_id_fkey" cannot be implemented
    DETAIL:  Key columns "worker_id" are of incompatible types:
    integer and character varying.

This crashed app startup entirely, blocking all trading/notary
functionality, not just the notary_payouts table.

This migration brings any already-existing notary_payouts table (created
before the model was fixed) in line with the corrected model: drop the
stale FKs if present, widen job_id and worker_id to VARCHAR, then
recreate the FKs. It is safe to run repeatedly - every step is
guarded/idempotent - and safe to run against a database where
notary_payouts doesn't exist yet at all (nothing to do;
Base.metadata.create_all() will create it correctly from the fixed
model).
"""
import asyncio
from database import engine
from sqlalchemy import text, inspect


# (column_name, referenced_table, referenced_column, fk_constraint_name)
_COLUMNS_TO_FIX = [
    ("job_id", "jobs", "id", "notary_payouts_job_id_fkey"),
    ("worker_id", "workers", "id", "notary_payouts_worker_id_fkey"),
]


async def migrate():
    """Convert notary_payouts.job_id and notary_payouts.worker_id from
    INTEGER to VARCHAR and make sure their FKs to jobs.id / workers.id
    exist with matching types."""

    async with engine.begin() as conn:
        tables = await conn.run_sync(lambda c: inspect(c).get_table_names())

        if "notary_payouts" not in tables:
            print("notary_payouts table does not exist yet - nothing to migrate "
                  "(create_all will create it with the correct VARCHAR columns)")
            return True

        if engine.dialect.name != "postgresql":
            # SQLite has no ALTER COLUMN TYPE and is used only for local/
            # test runs, where this drift doesn't occur (tables are
            # created fresh from the already-fixed model).
            print(f"Skipping type conversion on non-Postgres dialect "
                  f"({engine.dialect.name})")
            return True

        columns = await conn.run_sync(
            lambda c: {col["name"]: col["type"] for col in c.get_columns("notary_payouts")}
        )

        overall_success = True

        for column_name, ref_table, ref_column, fk_name in _COLUMNS_TO_FIX:
            column_type = str(columns.get(column_name, "")).upper()

            if "VARCHAR" in column_type or "CHARACTER VARYING" in column_type or "TEXT" in column_type:
                print(f"notary_payouts.{column_name} already {column_type} - nothing to do")
                continue

            try:
                # Drop the FK first (if it exists) - can't alter a column's
                # type while a mismatched constraint depends on it, and the
                # constraint may or may not have made it into the database
                # depending on how far create_all() got before failing.
                await conn.execute(text(
                    f"ALTER TABLE notary_payouts DROP CONSTRAINT IF EXISTS "
                    f"{fk_name}"
                ))

                await conn.execute(text(
                    f"ALTER TABLE notary_payouts ALTER COLUMN {column_name} TYPE VARCHAR "
                    f"USING {column_name}::text"
                ))

                await conn.execute(text(
                    f"ALTER TABLE notary_payouts ADD CONSTRAINT "
                    f"{fk_name} FOREIGN KEY ({column_name}) "
                    f"REFERENCES {ref_table}({ref_column})"
                ))

                print(f"✅ notary_payouts.{column_name} converted from INTEGER to VARCHAR "
                      f"and FK to {ref_table}.{ref_column} recreated")
            except Exception as e:
                print(f"❌ Failed to migrate notary_payouts.{column_name}: {e}")
                overall_success = False

        return overall_success


if __name__ == "__main__":
    result = asyncio.run(migrate())
    exit(0 if result else 1)

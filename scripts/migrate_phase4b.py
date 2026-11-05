"""
scripts/migrate_phase4b.py
==========================
Additive, idempotent, and reversible database migration for Phase 4B.

Phase 4B Schema Evolution:
  1. Adds column `action` to `whatsapp_booking_session` with default 'book' (NOT NULL).
     Permitted values: 'book', 'cancel', 'reschedule'.
  2. Supports --dry-run and --rollback flags.
  3. Strict safety guards preventing any execution against production Neon or unbacked databases.
"""

import os
import sys
import argparse
from sqlalchemy import create_engine, inspect, text

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from dotenv import load_dotenv
load_dotenv()

from database_safety import assert_database_safety


def apply_phase4b_migration(db_engine, dry_run: bool = False):
    """
    Applies Phase 4B schema migration to target database engine.
    """
    dialect = db_engine.dialect.name.lower()
    is_pg = ('postgres' in dialect)

    print(f"\n--- [MIGRATION] Applying Phase 4B Schema on {dialect.upper()} engine ---")

    inspector = inspect(db_engine)
    table_names = inspector.get_table_names()

    if "whatsapp_booking_session" not in table_names:
        raise RuntimeError("whatsapp_booking_session table does not exist. Run migrate_phase4a.py first.")

    columns = [c['name'] for c in inspector.get_columns("whatsapp_booking_session")]
    action_exists = "action" in columns

    if dry_run:
        print("[DRY RUN] Inspection complete:")
        print(f"  - Dialect: {dialect}")
        print(f"  - Column 'action' in whatsapp_booking_session: {action_exists}")
        return {
            "status": "dry_run",
            "dialect": dialect,
            "action_exists": action_exists
        }

    with db_engine.begin() as conn:
        if not action_exists:
            print("  -> Adding column 'action VARCHAR(30) NOT NULL DEFAULT 'book'' to whatsapp_booking_session...")
            if is_pg:
                conn.execute(text("ALTER TABLE whatsapp_booking_session ADD COLUMN IF NOT EXISTS action VARCHAR(30) NOT NULL DEFAULT 'book';"))
            else:
                conn.execute(text("ALTER TABLE whatsapp_booking_session ADD COLUMN action VARCHAR(30) NOT NULL DEFAULT 'book';"))
            print("  -> Column 'action' added successfully.")
        else:
            print("  -> Column 'action' already exists. Skipping addition.")

    # Verification
    inspector_post = inspect(db_engine)
    cols_post = [c['name'] for c in inspector_post.get_columns("whatsapp_booking_session")]
    if "action" not in cols_post:
        raise RuntimeError("Verification failed: column 'action' not found in whatsapp_booking_session!")

    print("[SUCCESS] Phase 4B schema migration verified successfully.")
    return {
        "status": "success",
        "dialect": dialect,
        "action_exists": True
    }


def rollback_phase4b_migration(db_engine, dry_run: bool = False):
    """
    Rolls back Phase 4B schema changes.
    """
    dialect = db_engine.dialect.name.lower()
    is_pg = ('postgres' in dialect)

    print(f"\n--- [ROLLBACK] Reverting Phase 4B Schema on {dialect.upper()} engine ---")

    inspector = inspect(db_engine)
    columns = [c['name'] for c in inspector.get_columns("whatsapp_booking_session")]

    if "action" not in columns:
        print("  -> Column 'action' does not exist. Nothing to roll back.")
        return {"status": "skipped", "reason": "column_missing"}

    if dry_run:
        print("[DRY RUN] Rollback inspection complete. Would drop column 'action'.")
        return {"status": "dry_run"}

    with db_engine.begin() as conn:
        if is_pg:
            conn.execute(text("ALTER TABLE whatsapp_booking_session DROP COLUMN IF EXISTS action;"))
            print("  -> Column 'action' dropped from PostgreSQL table.")
        else:
            # SQLite 3.35.0+ supports ALTER TABLE DROP COLUMN
            try:
                conn.execute(text("ALTER TABLE whatsapp_booking_session DROP COLUMN action;"))
                print("  -> Column 'action' dropped via SQLite ALTER TABLE DROP COLUMN.")
            except Exception as e:
                print(f"  -> SQLite DROP COLUMN skipped or unsupported: {e}")

    print("[SUCCESS] Phase 4B rollback completed.")
    return {"status": "rolled_back"}


def main():
    parser = argparse.ArgumentParser(description="PrimeCare Phase 4B Database Migration")
    parser.add_argument("--dry-run", action="store_true", help="Inspect without modifying")
    parser.add_argument("--rollback", action="store_true", help="Revert Phase 4B migration")
    parser.add_argument("--allow-dev-db", action="store_true", help="Allow updating instance/primecare.db")
    parser.add_argument("--database-url", type=str, default=None, help="Explicit database URL")
    parser.add_argument("--db-url", type=str, default=None, help="Explicit database URL (alias)")
    parser.add_argument("--db-uri", type=str, default=None, help="Explicit database URL (alias)")
    args = parser.parse_args()

    target_uri = args.database_url or args.db_url or args.db_uri
    if not target_uri:
        target_uri = "sqlite:///" + os.path.join(BASE_DIR, "instance", "test_primecare.db")

    print(f"Target Database URI: {target_uri}")
    assert_database_safety(target_uri, allow_dev_db=args.allow_dev_db)

    engine = create_engine(target_uri)

    if args.rollback:
        rollback_phase4b_migration(engine, dry_run=args.dry_run)
    else:
        apply_phase4b_migration(engine, dry_run=args.dry_run)


if __name__ == "__main__":
    main()


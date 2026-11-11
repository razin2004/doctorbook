"""
scripts/migrate_bilingual_preferences.py
========================================
Additive, idempotent, and reversible database migration for WhatsApp User Preferences
(Bilingual Malayalam & English language support).

Schema Evolution:
  1. Creates table whatsapp_user_preference with:
       - id (Primary Key)
       - sender_phone (VARCHAR(30), UNIQUE, NOT NULL, INDEXED)
       - language (VARCHAR(10), NOT NULL, DEFAULT 'en')
       - created_at (TIMESTAMP, NOT NULL, DEFAULT CURRENT_TIMESTAMP)
       - updated_at (TIMESTAMP, NOT NULL, DEFAULT CURRENT_TIMESTAMP)
  2. Creates standard lookup index on sender_phone.
  3. Supports --dry-run and --rollback flags.
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


def apply_bilingual_preferences_migration(db_engine, dry_run: bool = False):
    """
    Applies Bilingual Preferences schema migration to target database engine.
    """
    dialect = db_engine.dialect.name.lower()
    is_pg = ('postgres' in dialect)

    print(f"\n--- [MIGRATION] Applying Bilingual Preferences Schema on {dialect.upper()} engine ---")

    inspector = inspect(db_engine)
    table_names = inspector.get_table_names()

    table_exists = "whatsapp_user_preference" in table_names

    if dry_run:
        print("[DRY RUN] Inspection complete:")
        print(f"  - Dialect: {dialect}")
        print(f"  - Table whatsapp_user_preference exists: {table_exists}")
        return {
            "status": "dry_run",
            "dialect": dialect,
            "table_exists": table_exists
        }

    with db_engine.begin() as conn:
        if is_pg:
            # PostgreSQL Table DDL
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS whatsapp_user_preference (
                    id SERIAL PRIMARY KEY,
                    sender_phone VARCHAR(30) UNIQUE NOT NULL,
                    language VARCHAR(10) NOT NULL DEFAULT 'en',
                    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
            """))
            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS ix_whatsapp_user_pref_phone
                ON whatsapp_user_preference (sender_phone);
            """))
            print("[MIGRATION] PostgreSQL whatsapp_user_preference table and indexes applied successfully.")
        else:
            # SQLite Table DDL
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS whatsapp_user_preference (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender_phone VARCHAR(30) UNIQUE NOT NULL,
                    language VARCHAR(10) NOT NULL DEFAULT 'en',
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
            """))
            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS ix_whatsapp_user_pref_phone
                ON whatsapp_user_preference (sender_phone);
            """))
            print("[MIGRATION] SQLite whatsapp_user_preference table and indexes applied successfully.")

    inspector_post = inspect(db_engine)
    if "whatsapp_user_preference" not in inspector_post.get_table_names():
        raise RuntimeError("Verification failed: table whatsapp_user_preference was not created!")

    print("[MIGRATION] Verified: whatsapp_user_preference table is ready.")
    return {"status": "applied", "table": "whatsapp_user_preference"}


def rollback_bilingual_preferences_migration(db_engine):
    """
    Rolls back bilingual preferences schema migration.
    """
    dialect = db_engine.dialect.name.lower()
    print(f"\n--- [ROLLBACK] Reverting Bilingual Preferences Schema on {dialect.upper()} engine ---")

    with db_engine.begin() as conn:
        conn.execute(text("DROP INDEX IF EXISTS ix_whatsapp_user_pref_phone;"))
        conn.execute(text("DROP TABLE IF EXISTS whatsapp_user_preference;"))

    print("[ROLLBACK] Dropped table whatsapp_user_preference.")
    return {"status": "rolled_back"}


def main():
    parser = argparse.ArgumentParser(description="Apply or Rollback WhatsApp Bilingual Preferences Schema.")
    parser.add_argument("--dry-run", action="store_true", help="Inspect schema without applying changes.")
    parser.add_argument("--rollback", action="store_true", help="Revert the migration.")
    parser.add_argument("--database-url", type=str, default=None, help="Target database connection URL.")
    args = parser.parse_args()

    db_url = args.database_url or os.environ.get("DATABASE_URL")
    if not db_url:
        print("[ERROR] DATABASE_URL must be set or passed via --database-url.")
        sys.exit(1)

    print(f"Connecting to database...")
    engine = create_engine(db_url)

    if args.rollback:
        rollback_bilingual_preferences_migration(engine)
    else:
        apply_bilingual_preferences_migration(engine, dry_run=args.dry_run)


if __name__ == "__main__":
    main()

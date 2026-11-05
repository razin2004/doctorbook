"""
scripts/migrate_phase4a.py
==========================
Additive, idempotent, and reversible database migration for Phase 4A.

Phase 4A Schema Evolution:
  1. Creates table whatsapp_booking_session with foreign keys to whatsapp_conversation, doctor, user, and patient_booking.
  2. Creates standard lookup indexes on conversation_id, status, and (conversation_id, status).
  3. Creates partial unique index uq_whatsapp_active_booking_per_conversation (WHERE status = 'in_progress').
  4. Supports --dry-run and --rollback flags.
  5. Strict safety guards preventing any execution against production Neon or unbacked databases.
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


def apply_phase4a_migration(db_engine, dry_run: bool = False):
    """
    Applies Phase 4A schema migration to target database engine.
    """
    dialect = db_engine.dialect.name.lower()
    is_pg = ('postgres' in dialect)

    print(f"\n--- [MIGRATION] Applying Phase 4A Schema on {dialect.upper()} engine ---")

    inspector = inspect(db_engine)
    table_names = inspector.get_table_names()

    if "whatsapp_conversation" not in table_names:
        raise RuntimeError("whatsapp_conversation table does not exist. Run migrate_phase3c.py first.")

    table_exists = "whatsapp_booking_session" in table_names

    if dry_run:
        print("[DRY RUN] Inspection complete:")
        print(f"  - Dialect: {dialect}")
        print(f"  - Table whatsapp_booking_session exists: {table_exists}")
        return {
            "status": "dry_run",
            "dialect": dialect,
            "table_exists": table_exists
        }

    with db_engine.begin() as conn:
        if is_pg:
            # PostgreSQL Table DDL
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS whatsapp_booking_session (
                    id SERIAL PRIMARY KEY,
                    conversation_id INTEGER NOT NULL REFERENCES whatsapp_conversation(id) ON DELETE RESTRICT,
                    status VARCHAR(30) NOT NULL DEFAULT 'in_progress',
                    step VARCHAR(50) NOT NULL DEFAULT 'SELECT_DOCTOR',
                    doctor_id INTEGER REFERENCES doctor(id) ON DELETE SET NULL,
                    doctor_name VARCHAR(100),
                    specialization VARCHAR(100),
                    appointment_date VARCHAR(20),
                    time_slot VARCHAR(50),
                    patient_name VARCHAR(100),
                    patient_age VARCHAR(10),
                    patient_gender VARCHAR(20),
                    contact_phone VARCHAR(20),
                    patient_user_id INTEGER REFERENCES "user"(id) ON DELETE SET NULL,
                    booking_id INTEGER REFERENCES patient_booking(id) ON DELETE SET NULL,
                    token_number INTEGER,
                    expires_at TIMESTAMP,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
            """))

            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_whatsapp_booking_session_convo ON whatsapp_booking_session (conversation_id);"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_whatsapp_booking_session_status ON whatsapp_booking_session (status);"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_whatsapp_booking_session_convo_status ON whatsapp_booking_session (conversation_id, status);"))
            conn.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_whatsapp_active_booking_per_conversation
                ON whatsapp_booking_session (conversation_id)
                WHERE status = 'in_progress';
            """))
            print("[MIGRATION] PostgreSQL whatsapp_booking_session table and indexes applied successfully.")

        else:
            # SQLite Table DDL
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS whatsapp_booking_session (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id INTEGER NOT NULL,
                    status VARCHAR(30) NOT NULL DEFAULT 'in_progress',
                    step VARCHAR(50) NOT NULL DEFAULT 'SELECT_DOCTOR',
                    doctor_id INTEGER,
                    doctor_name VARCHAR(100),
                    specialization VARCHAR(100),
                    appointment_date VARCHAR(20),
                    time_slot VARCHAR(50),
                    patient_name VARCHAR(100),
                    patient_age VARCHAR(10),
                    patient_gender VARCHAR(20),
                    contact_phone VARCHAR(20),
                    patient_user_id INTEGER,
                    booking_id INTEGER,
                    token_number INTEGER,
                    expires_at DATETIME,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (conversation_id) REFERENCES whatsapp_conversation(id) ON DELETE RESTRICT,
                    FOREIGN KEY (doctor_id) REFERENCES doctor(id) ON DELETE SET NULL,
                    FOREIGN KEY (patient_user_id) REFERENCES user(id) ON DELETE SET NULL,
                    FOREIGN KEY (booking_id) REFERENCES patient_booking(id) ON DELETE SET NULL
                );
            """))

            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_whatsapp_booking_session_convo ON whatsapp_booking_session (conversation_id);"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_whatsapp_booking_session_status ON whatsapp_booking_session (status);"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_whatsapp_booking_session_convo_status ON whatsapp_booking_session (conversation_id, status);"))
            conn.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_whatsapp_active_booking_per_conversation
                ON whatsapp_booking_session (conversation_id)
                WHERE status = 'in_progress';
            """))
            print("[MIGRATION] SQLite whatsapp_booking_session table and indexes applied successfully.")

    return {"status": "applied", "dialect": dialect}


def rollback_phase4a_migration(db_engine):
    """
    Safely rolls back Phase 4A schema migration.
    """
    dialect = db_engine.dialect.name.lower()
    print(f"\n--- [ROLLBACK] Reverting Phase 4A Schema on {dialect.upper()} engine ---")

    with db_engine.begin() as conn:
        conn.execute(text("DROP INDEX IF EXISTS uq_whatsapp_active_booking_per_conversation;"))
        conn.execute(text("DROP INDEX IF EXISTS ix_whatsapp_booking_session_convo_status;"))
        conn.execute(text("DROP INDEX IF EXISTS ix_whatsapp_booking_session_status;"))
        conn.execute(text("DROP INDEX IF EXISTS ix_whatsapp_booking_session_convo;"))
        conn.execute(text("DROP TABLE IF EXISTS whatsapp_booking_session;"))
    print("[ROLLBACK] Phase 4A table and indexes dropped successfully.")


def main():
    parser = argparse.ArgumentParser(description="Phase 4A Database Migration")
    parser.add_argument("--db-url", type=str, default=None, help="Explicit target database URL")
    parser.add_argument("--database-url", type=str, default=None, help="Explicit target database URL (alias)")
    parser.add_argument("--db-uri", type=str, default=None, help="Explicit target database URL (alias)")
    parser.add_argument("--dry-run", action="store_true", help="Simulate migration without modifying data")
    parser.add_argument("--rollback", action="store_true", help="Roll back Phase 4A changes")
    parser.add_argument("--allow-dev-db", action="store_true", help="Allow running against instance/primecare.db")
    args = parser.parse_args()

    target_uri = args.db_url or args.database_url or args.db_uri
    if not target_uri:
        test_db_path = os.path.abspath(os.path.join(BASE_DIR, 'instance', 'test_primecare.db'))
        target_uri = f"sqlite:///{test_db_path}"

    assert_database_safety(target_uri, allow_dev_db=args.allow_dev_db)

    print(f"Target Database: {target_uri}")
    engine = create_engine(target_uri)

    if args.rollback:
        rollback_phase4a_migration(engine)
    else:
        apply_phase4a_migration(engine, dry_run=args.dry_run)


if __name__ == '__main__':
    main()


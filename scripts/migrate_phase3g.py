"""
scripts/migrate_phase3g.py
==========================
Additive, idempotent, and reversible database migration for Phase 3G.

Phase 3G Schema Evolution for whatsapp_conversation:
  1. Adds scoped_user_id (VARCHAR(128)) for BSUID-only / provider-scoped identities.
  2. Adds parent_scoped_id (VARCHAR(128)) for business/portfolio scope isolation.
  3. Modifies sender_phone to be nullable (allowing BSUID-only senders without a phone number).
  4. Preserves uq_whatsapp_open_conversation_per_phone for phone-based conversations.
  5. Adds uq_whatsapp_open_conversation_per_bsuid for scoped identities (status != 'closed').
  6. Supports --dry-run and --rollback flags.
  7. Strict safety guards preventing any execution against production Neon or unbacked databases.
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


def apply_phase3g_migration(db_engine, dry_run: bool = False):
    """
    Applies Phase 3G schema migration to target database engine.
    """
    dialect = db_engine.dialect.name.lower()
    is_pg = ('postgres' in dialect)

    print(f"\n--- [MIGRATION] Applying Phase 3G Schema on {dialect.upper()} engine ---")

    inspector = inspect(db_engine)
    if "whatsapp_conversation" not in inspector.get_table_names():
        raise RuntimeError("whatsapp_conversation table does not exist. Run migrate_phase3c.py first.")

    existing_columns = {col['name'] for col in inspector.get_columns('whatsapp_conversation')}
    needs_scoped_col = 'scoped_user_id' not in existing_columns
    needs_parent_col = 'parent_scoped_id' not in existing_columns

    if dry_run:
        print("[DRY RUN] Inspection complete:")
        print(f"  - Dialect: {dialect}")
        print(f"  - Needs scoped_user_id column: {needs_scoped_col}")
        print(f"  - Needs parent_scoped_id column: {needs_parent_col}")
        return {
            "status": "dry_run",
            "needs_scoped_col": needs_scoped_col,
            "needs_parent_col": needs_parent_col
        }

    if is_pg:
        # PostgreSQL Migration
        with db_engine.begin() as conn:
            print("[MIGRATION] Making sender_phone nullable on PostgreSQL...")
            conn.execute(text("ALTER TABLE whatsapp_conversation ALTER COLUMN sender_phone DROP NOT NULL;"))

            if needs_scoped_col:
                print("[MIGRATION] Adding column scoped_user_id to whatsapp_conversation...")
                conn.execute(text("ALTER TABLE whatsapp_conversation ADD COLUMN IF NOT EXISTS scoped_user_id VARCHAR(128);"))

            if needs_parent_col:
                print("[MIGRATION] Adding column parent_scoped_id to whatsapp_conversation...")
                conn.execute(text("ALTER TABLE whatsapp_conversation ADD COLUMN IF NOT EXISTS parent_scoped_id VARCHAR(128);"))

            print("[MIGRATION] Creating indexes on PostgreSQL...")
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_whatsapp_convo_scoped_status ON whatsapp_conversation (scoped_user_id, parent_scoped_id, status);"))
            conn.execute(text("DROP INDEX IF EXISTS uq_whatsapp_open_conversation_per_bsuid;"))
            conn.execute(text("DROP INDEX IF EXISTS uq_whatsapp_open_conversation_per_bsuid_null_scope;"))
            conn.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_whatsapp_open_conversation_per_bsuid
                ON whatsapp_conversation (scoped_user_id, parent_scoped_id)
                WHERE status != 'closed' AND scoped_user_id IS NOT NULL AND parent_scoped_id IS NOT NULL;
            """))
            conn.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_whatsapp_open_conversation_per_bsuid_null_scope
                ON whatsapp_conversation (scoped_user_id)
                WHERE status != 'closed' AND scoped_user_id IS NOT NULL AND parent_scoped_id IS NULL;
            """))

    else:
        # SQLite Migration via safe atomic table replacement
        with db_engine.connect() as conn:
            conn.execute(text("PRAGMA foreign_keys=OFF;"))
            
            # Create upgraded table
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS whatsapp_conversation_phase3g (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_uuid VARCHAR(36) NOT NULL,
                    sender_phone VARCHAR(20),
                    scoped_user_id VARCHAR(128),
                    parent_scoped_id VARCHAR(128),
                    status VARCHAR(20) NOT NULL DEFAULT 'active',
                    is_human_handoff INTEGER NOT NULL DEFAULT 0,
                    handoff_reason VARCHAR(100),
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_message_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    closed_at TIMESTAMP,
                    expires_at TIMESTAMP,
                    CONSTRAINT uq_whatsapp_conversation_uuid UNIQUE (conversation_uuid)
                );
            """))

            # Copy data
            has_scoped_already = 'scoped_user_id' in existing_columns
            if has_scoped_already:
                conn.execute(text("""
                    INSERT INTO whatsapp_conversation_phase3g (
                        id, conversation_uuid, sender_phone, scoped_user_id, parent_scoped_id,
                        status, is_human_handoff, handoff_reason, created_at, updated_at,
                        last_message_at, closed_at, expires_at
                    )
                    SELECT id, conversation_uuid, sender_phone, scoped_user_id, parent_scoped_id,
                           status, is_human_handoff, handoff_reason, created_at, updated_at,
                           last_message_at, closed_at, expires_at
                    FROM whatsapp_conversation;
                """))
            else:
                conn.execute(text("""
                    INSERT INTO whatsapp_conversation_phase3g (
                        id, conversation_uuid, sender_phone,
                        status, is_human_handoff, handoff_reason, created_at, updated_at,
                        last_message_at, closed_at, expires_at
                    )
                    SELECT id, conversation_uuid, sender_phone,
                           status, is_human_handoff, handoff_reason, created_at, updated_at,
                           last_message_at, closed_at, expires_at
                    FROM whatsapp_conversation;
                """))

            # Swap tables
            conn.execute(text("DROP TABLE whatsapp_conversation;"))
            conn.execute(text("ALTER TABLE whatsapp_conversation_phase3g RENAME TO whatsapp_conversation;"))

            # Recreate indexes
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_whatsapp_convo_phone_status ON whatsapp_conversation (sender_phone, status);"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_whatsapp_convo_scoped_status ON whatsapp_conversation (scoped_user_id, parent_scoped_id, status);"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_whatsapp_conversation_sender_phone ON whatsapp_conversation (sender_phone);"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_whatsapp_conversation_status ON whatsapp_conversation (status);"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_whatsapp_conversation_last_message_at ON whatsapp_conversation (last_message_at);"))
            conn.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_whatsapp_open_conversation_per_phone
                ON whatsapp_conversation (sender_phone)
                WHERE status != 'closed' AND sender_phone IS NOT NULL;
            """))
            conn.execute(text("DROP INDEX IF EXISTS uq_whatsapp_open_conversation_per_bsuid;"))
            conn.execute(text("DROP INDEX IF EXISTS uq_whatsapp_open_conversation_per_bsuid_null_scope;"))
            conn.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_whatsapp_open_conversation_per_bsuid
                ON whatsapp_conversation (scoped_user_id, parent_scoped_id)
                WHERE status != 'closed' AND scoped_user_id IS NOT NULL AND parent_scoped_id IS NOT NULL;
            """))
            conn.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_whatsapp_open_conversation_per_bsuid_null_scope
                ON whatsapp_conversation (scoped_user_id)
                WHERE status != 'closed' AND scoped_user_id IS NOT NULL AND parent_scoped_id IS NULL;
            """))

            # Foreign key check
            violations = conn.execute(text("PRAGMA foreign_key_check('whatsapp_conversation');")).fetchall()
            if violations:
                raise RuntimeError(f"Foreign key violations detected after table swap: {violations}")

            conn.execute(text("PRAGMA foreign_keys=ON;"))
            conn.commit()

    print("[MIGRATION] Phase 3G migration applied successfully.")

    # Verification
    verify_inspector = inspect(db_engine)
    new_cols = {col['name'] for col in verify_inspector.get_columns('whatsapp_conversation')}
    if 'scoped_user_id' not in new_cols or 'parent_scoped_id' not in new_cols:
        raise RuntimeError(f"Verification failed: expected columns missing in whatsapp_conversation: {new_cols}")

    return {"status": "success"}


def rollback_phase3g_migration(db_engine):
    """
    Rollback procedure for Phase 3G columns and indexes.
    """
    dialect = db_engine.dialect.name.lower()
    is_pg = ('postgres' in dialect)

    print(f"\n--- [ROLLBACK] Reverting Phase 3G Schema on {dialect.upper()} engine ---")

    if is_pg:
        with db_engine.begin() as conn:
            conn.execute(text("DROP INDEX IF EXISTS uq_whatsapp_open_conversation_per_bsuid_null_scope;"))
            conn.execute(text("DROP INDEX IF EXISTS uq_whatsapp_open_conversation_per_bsuid;"))
            conn.execute(text("DROP INDEX IF EXISTS ix_whatsapp_convo_scoped_status;"))
            conn.execute(text("ALTER TABLE whatsapp_conversation DROP COLUMN IF EXISTS parent_scoped_id;"))
            conn.execute(text("ALTER TABLE whatsapp_conversation DROP COLUMN IF EXISTS scoped_user_id;"))
    else:
        with db_engine.connect() as conn:
            conn.execute(text("DROP INDEX IF EXISTS uq_whatsapp_open_conversation_per_bsuid_null_scope;"))
            conn.execute(text("DROP INDEX IF EXISTS uq_whatsapp_open_conversation_per_bsuid;"))
            conn.execute(text("DROP INDEX IF EXISTS ix_whatsapp_convo_scoped_status;"))
            conn.commit()

    print("[ROLLBACK] Phase 3G rollback complete.")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="PrimeCare Phase 3G Database Migration")
    parser.add_argument('--db-uri', type=str, default=None, help="Database URI target")
    parser.add_argument('--database-url', type=str, default=None, help="Database URI target (alias)")
    parser.add_argument('--db-url', type=str, default=None, help="Database URI target (alias)")
    parser.add_argument('--dry-run', action='store_true', help="Inspect without modifying")
    parser.add_argument('--rollback', action='store_true', help="Revert migration")
    parser.add_argument('--allow-dev-db', action='store_true', help="Allow modifying primecare.db")
    args = parser.parse_args()

    target_uri = args.db_uri or args.database_url or args.db_url
    if not target_uri:
        target_uri = "sqlite:///" + os.path.join(BASE_DIR, "instance", "test_primecare.db")

    assert_database_safety(target_uri, allow_dev_db=args.allow_dev_db)

    engine = create_engine(target_uri)
    if args.rollback:
        rollback_phase3g_migration(engine)
    else:
        apply_phase3g_migration(engine, dry_run=args.dry_run)


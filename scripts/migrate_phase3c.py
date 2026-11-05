"""
scripts/migrate_phase3c.py
==========================
Additive, idempotent, and reversible database migration for Phase 3C.

Features:
  1. Creates whatsapp_conversation, whatsapp_message, and whatsapp_webhook_event tables.
  2. Creates performance indexes and unique constraints for both SQLite and PostgreSQL.
  3. Preflight and schema verification assertions.
  4. Full rollback support via --rollback flag.
  5. Dry-run inspection option via --dry-run flag.
  6. Strict safety guardrails preventing any execution against production Neon database.
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


def detect_duplicate_open_conversations(db_engine):
    """
    Detects existing duplicate open conversations (status != 'closed').
    Returns a list of tuples: (sender_phone, duplicate_count).
    Does NOT delete or merge any records.
    """
    inspector = inspect(db_engine)
    if "whatsapp_conversation" not in inspector.get_table_names():
        return []
    with db_engine.connect() as conn:
        dup_query = text("""
            SELECT sender_phone, COUNT(*) AS open_count
            FROM whatsapp_conversation
            WHERE status != 'closed'
            GROUP BY sender_phone
            HAVING COUNT(*) > 1;
        """)
        rows = conn.execute(dup_query).fetchall()
        return [(str(r[0]), int(r[1])) for r in rows]


def apply_phase3c_migration(db_engine, dry_run: bool = False):
    """
    Applies additive Phase 3C schema creation to target database engine.
    """
    dialect = db_engine.dialect.name.lower()
    is_pg = ('postgres' in dialect)
    id_col_type = "SERIAL PRIMARY KEY" if is_pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    bool_col_type = "BOOLEAN NOT NULL DEFAULT FALSE" if is_pg else "INTEGER NOT NULL DEFAULT 0"

    print(f"\n--- [MIGRATION] Applying Phase 3C Schema on {dialect.upper()} engine ---")

    inspector = inspect(db_engine)
    existing_tables = set(inspector.get_table_names())

    # Preflight duplicate open conversation check
    duplicates = detect_duplicate_open_conversations(db_engine)
    if duplicates:
        print(f"[PREFLIGHT ERROR] Found {len(duplicates)} sender(s) with duplicate open conversations:")
        for phone, count in duplicates:
            print(f"  - sender_phone: {phone}, open_count: {count}")
        raise RuntimeError(
            f"MIGRATION BLOCKED: Cannot apply open conversation uniqueness. "
            f"Found {len(duplicates)} duplicate open conversation sets. "
            "Please review and safely resolve duplicates before proceeding."
        )
    else:
        print("[PREFLIGHT] Verified 0 duplicate open conversations in 'whatsapp_conversation'.")

    canonical_schemas = {
        "whatsapp_conversation": f"""
            CREATE TABLE whatsapp_conversation (
                id {id_col_type},
                conversation_uuid VARCHAR(36) NOT NULL,
                sender_phone VARCHAR(20) NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'active',
                is_human_handoff {bool_col_type},
                handoff_reason VARCHAR(100),
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_message_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                closed_at TIMESTAMP,
                expires_at TIMESTAMP,
                CONSTRAINT uq_whatsapp_conversation_uuid UNIQUE (conversation_uuid)
            );
        """,
        "whatsapp_message": f"""
            CREATE TABLE whatsapp_message (
                id {id_col_type},
                conversation_id INTEGER NOT NULL REFERENCES whatsapp_conversation(id) ON DELETE RESTRICT,
                wamid VARCHAR(128),
                direction VARCHAR(10) NOT NULL,
                message_type VARCHAR(20) NOT NULL DEFAULT 'text',
                content TEXT NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'received',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                processed_at TIMESTAMP,
                CONSTRAINT uq_whatsapp_message_wamid UNIQUE (wamid)
            );
        """,
        "whatsapp_webhook_event": f"""
            CREATE TABLE whatsapp_webhook_event (
                id {id_col_type},
                event_id VARCHAR(128) NOT NULL,
                event_type VARCHAR(50) NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'received',
                payload_json TEXT,
                conversation_id INTEGER,
                inbound_message_id INTEGER,
                locked_at TIMESTAMP,
                locked_by VARCHAR(64),
                attempt_count INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 3,
                next_retry_at TIMESTAMP,
                error_code VARCHAR(50),
                last_error TEXT,
                received_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                processed_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uq_whatsapp_webhook_event_id UNIQUE (event_id)
            );
        """
    }

    canonical_indexes = {
        "whatsapp_conversation": [
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_whatsapp_conversation_uuid ON whatsapp_conversation (conversation_uuid);",
            "CREATE INDEX IF NOT EXISTS ix_whatsapp_conversation_sender_phone ON whatsapp_conversation (sender_phone);",
            "CREATE INDEX IF NOT EXISTS ix_whatsapp_conversation_status ON whatsapp_conversation (status);",
            "CREATE INDEX IF NOT EXISTS ix_whatsapp_conversation_last_message_at ON whatsapp_conversation (last_message_at);",
            "CREATE INDEX IF NOT EXISTS ix_whatsapp_convo_phone_status ON whatsapp_conversation (sender_phone, status);",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_whatsapp_open_conversation_per_phone ON whatsapp_conversation (sender_phone) WHERE status != 'closed';"
        ],
        "whatsapp_message": [
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_whatsapp_message_wamid ON whatsapp_message (wamid);",
            "CREATE INDEX IF NOT EXISTS ix_whatsapp_message_conversation_id ON whatsapp_message (conversation_id);",
            "CREATE INDEX IF NOT EXISTS ix_whatsapp_message_created_at ON whatsapp_message (created_at);",
            "CREATE INDEX IF NOT EXISTS ix_whatsapp_msg_convo_created ON whatsapp_message (conversation_id, created_at);"
        ],
        "whatsapp_webhook_event": [
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_whatsapp_webhook_event_id ON whatsapp_webhook_event (event_id);",
            "CREATE INDEX IF NOT EXISTS ix_whatsapp_webhook_event_event_type ON whatsapp_webhook_event (event_type);",
            "CREATE INDEX IF NOT EXISTS ix_whatsapp_webhook_event_status ON whatsapp_webhook_event (status);",
            "CREATE INDEX IF NOT EXISTS ix_whatsapp_webhook_queue_retry ON whatsapp_webhook_event (status, next_retry_at);"
        ]
    }

    tables_to_create = []
    tables_verified = []

    for tbl in ["whatsapp_conversation", "whatsapp_message", "whatsapp_webhook_event"]:
        if tbl not in existing_tables:
            tables_to_create.append(tbl)
        else:
            tables_verified.append(tbl)

    if dry_run:
        print("[DRY RUN] Inspection complete:")
        print(f"  - Tables already existing: {tables_verified}")
        print(f"  - Tables to create: {tables_to_create}")
        return {
            "status": "dry_run",
            "tables_to_create": tables_to_create,
            "tables_verified": tables_verified
        }

    # Execute table and index creation in transaction
    with db_engine.begin() as conn:
        for tbl in ["whatsapp_conversation", "whatsapp_message", "whatsapp_webhook_event"]:
            if tbl in tables_to_create:
                print(f"[MIGRATION] Creating table: {tbl}")
                conn.execute(text(canonical_schemas[tbl]))
            else:
                print(f"[MIGRATION] Table already exists: {tbl}")

        # Upgrade pre-existing whatsapp_webhook_event table with queue columns if needed
        if "whatsapp_webhook_event" in existing_tables:
            current_cols = {c['name'] for c in inspector.get_columns('whatsapp_webhook_event')}
            queue_cols = [
                ("payload_json", "TEXT"),
                ("conversation_id", "INTEGER"),
                ("inbound_message_id", "INTEGER"),
                ("locked_at", "TIMESTAMP"),
                ("locked_by", "VARCHAR(64)"),
                ("max_attempts", "INTEGER DEFAULT 3"),
                ("next_retry_at", "TIMESTAMP"),
                ("last_error", "TEXT")
            ]
            for col_name, col_def in queue_cols:
                if col_name not in current_cols:
                    print(f"[MIGRATION] Adding column '{col_name}' to whatsapp_webhook_event")
                    if is_pg:
                        conn.execute(text(f"ALTER TABLE whatsapp_webhook_event ADD COLUMN IF NOT EXISTS {col_name} {col_def};"))
                    else:
                        conn.execute(text(f"ALTER TABLE whatsapp_webhook_event ADD COLUMN {col_name} {col_def};"))

        # Create indexes after tables and columns are guaranteed to exist
        for tbl in ["whatsapp_conversation", "whatsapp_message", "whatsapp_webhook_event"]:
            for idx_sql in canonical_indexes.get(tbl, []):
                conn.execute(text(idx_sql))

    # Post-migration Schema Verification
    verify_inspector = inspect(db_engine)
    new_tables = set(verify_inspector.get_table_names())

    missing = [tbl for tbl in ["whatsapp_conversation", "whatsapp_message", "whatsapp_webhook_event"] if tbl not in new_tables]
    if missing:
        raise RuntimeError(f"Schema verification failed! Tables missing after migration: {missing}")

    # Check foreign keys
    msg_fks = verify_inspector.get_foreign_keys('whatsapp_message')
    has_convo_fk = any(fk.get('referred_table') == 'whatsapp_conversation' for fk in msg_fks)
    if not has_convo_fk:
        raise RuntimeError("Verification failed: whatsapp_message foreign key to whatsapp_conversation is missing.")

    # Check unique constraints
    convo_uq = verify_inspector.get_unique_constraints('whatsapp_conversation')
    convo_uq_cols = [u.get('column_names', []) for u in convo_uq]
    convo_indices = verify_inspector.get_indexes('whatsapp_conversation')
    has_uuid_uq = any('conversation_uuid' in cols for cols in convo_uq_cols) or any(
        idx.get('unique') and 'conversation_uuid' in idx.get('column_names', []) for idx in convo_indices
    )
    if not has_uuid_uq and not is_pg:
        with db_engine.connect() as conn:
            for idx_row in conn.execute(text("PRAGMA index_list('whatsapp_conversation');")).fetchall():
                if idx_row[2] == 1:
                    info_cols = [c[2] for c in conn.execute(text(f"PRAGMA index_info('{idx_row[1]}');")).fetchall()]
                    if 'conversation_uuid' in info_cols:
                        has_uuid_uq = True
                        break

    if not has_uuid_uq:
        raise RuntimeError("Verification failed: unique constraint on conversation_uuid is missing.")

    # Check open conversation partial unique index
    has_open_uq = False
    if is_pg:
        pg_indexes = verify_inspector.get_indexes('whatsapp_conversation')
        has_open_uq = any(idx.get('name') == 'uq_whatsapp_open_conversation_per_phone' for idx in pg_indexes)
    else:
        with db_engine.connect() as conn:
            index_list = conn.execute(text("PRAGMA index_list('whatsapp_conversation');")).fetchall()
            has_open_uq = any(row[1] == 'uq_whatsapp_open_conversation_per_phone' for row in index_list)

    if not has_open_uq:
        raise RuntimeError("Verification failed: unique index uq_whatsapp_open_conversation_per_phone is missing.")

    # SQLite PRAGMA foreign_key_check for Phase 3C tables
    if not is_pg:
        with db_engine.connect() as conn:
            violations = []
            for tbl in ["whatsapp_conversation", "whatsapp_message", "whatsapp_webhook_event"]:
                v = conn.execute(text(f"PRAGMA foreign_key_check('{tbl}');")).fetchall()
                if v:
                    violations.extend(v)
            if violations:
                raise RuntimeError(f"Foreign key check failed for Phase 3C tables: {violations}")

    print("\n--- [MIGRATION SUCCESS] Phase 3C schema applied and verified ---")
    print(f"  - Created: {tables_to_create}")
    print(f"  - Verified: {list(new_tables & {'whatsapp_conversation', 'whatsapp_message', 'whatsapp_webhook_event'})}")

    return {
        "status": "success",
        "created": tables_to_create,
        "verified": ["whatsapp_conversation", "whatsapp_message", "whatsapp_webhook_event"]
    }


def rollback_phase3c_migration(db_engine):
    """
    Safely rolls back Phase 3C tables in reverse foreign key order.
    """
    print(f"\n--- [ROLLBACK] Reversing Phase 3C Schema on {db_engine.dialect.name.upper()} engine ---")
    with db_engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS whatsapp_message;"))
        conn.execute(text("DROP TABLE IF EXISTS whatsapp_webhook_event;"))
        conn.execute(text("DROP TABLE IF EXISTS whatsapp_conversation;"))
    print("[ROLLBACK] Phase 3C tables dropped successfully.")


def main():
    parser = argparse.ArgumentParser(description="Phase 3C WhatsApp Database Migration")
    parser.add_argument("--sqlite", action="store_true", help="Migrate isolated test SQLite database (instance/test_primecare.db)")
    parser.add_argument("--postgres", action="store_true", help="Migrate verified local PostgreSQL database on port 5433")
    parser.add_argument("--db-uri", type=str, default="", help="Explicit database connection URI")
    parser.add_argument("--database-url", type=str, default="", help="Explicit database connection URI (alias)")
    parser.add_argument("--dry-run", action="store_true", help="Inspect and plan without executing DDL")
    parser.add_argument("--rollback", action="store_true", help="Drop Phase 3C tables and indexes")
    parser.add_argument("--allow-dev-db", action="store_true", help="Explicit confirmation if modifying instance/primecare.db")
    parser.add_argument("--confirm-production", action="store_true", help="Explicit confirmation if targeting production databases.")

    args = parser.parse_args()

    target_explicit = args.db_uri or args.database_url
    if args.sqlite:
        db_path = os.path.join(BASE_DIR, 'instance', 'test_primecare.db')
        db_uri = f"sqlite:///{db_path}"
    elif args.postgres:
        db_uri = os.environ.get('POSTGRES_TEST_URL', 'postgresql://postgres:postgres@127.0.0.1:5433/primecare_test')
    elif target_explicit:
        db_uri = target_explicit
    else:
        # Default to isolated SQLite test database
        db_path = os.path.join(BASE_DIR, 'instance', 'test_primecare.db')
        db_uri = f"sqlite:///{db_path}"

    assert_database_safety(
        db_uri,
        allow_dev_db=args.allow_dev_db,
        allow_production_migration=args.confirm_production
    )

    print(f"[TARGET] Database URI: {db_uri.split('@')[-1] if '@' in db_uri else db_uri}")
    engine = create_engine(db_uri)

    if args.rollback:
        rollback_phase3c_migration(engine)
    else:
        apply_phase3c_migration(engine, dry_run=args.dry_run)


if __name__ == '__main__':
    main()

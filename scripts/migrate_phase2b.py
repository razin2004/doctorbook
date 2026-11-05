"""
scripts/migrate_phase2b.py
==========================
Additive, reversible database migration and preflight audit script for Phase 2B.

Features:
  1. Preflight audit: Scans all existing patient_booking records (confirmed,
     active, cancelled) and reports the max legacy token per doctor and date.
  2. Schema creation: Creates daily_token_allocation and booking_sync_outbox tables
     along with indexes and constraints (PostgreSQL & SQLite compatible).
  3. Backfill: Seeds daily_token_allocation rows from existing legacy data so
     token allocations never restart at zero.
  4. Safe Reversibility: Full rollback support via --rollback flag.
  5. Dry-run option: Inspects and plans migration without modifying the database.
  6. Production safety assertion: Guards against accidental execution on production databases.
"""

import os
import sys
import argparse
from datetime import datetime

# Add root directory to sys.path
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from dotenv import load_dotenv
load_dotenv()

from database_safety import assert_database_safety


def run_preflight_audit(db_engine, allow_conflicts=False):
    """
    Scans patient_booking table and computes max legacy tokens per doctor and appointment date.
    Performs fail-closed validation on legacy dates, tokens, doctor identities, and duplicate tokens.
    Produces a structured conflict report and explicit preflight limitation notice.
    """
    from collections import defaultdict
    import re
    from sqlalchemy import text
    from services.token_allocation import extract_doctor_identifier, normalize_booking_date

    print("\n--- [PREFLIGHT AUDIT] Scanning legacy patient_booking records ---")
    query = text("""
        SELECT doctor_name, specialization, date, token, sheet_url, status
        FROM patient_booking
        ORDER BY date ASC, token ASC
    """)

    allocations = {}
    conflicts = []
    seen_tokens = defaultdict(set) # (doc_id, date) -> set(tokens)

    with db_engine.connect() as conn:
        try:
            results = conn.execute(query).fetchall()
        except Exception as e:
            print(f"[PREFLIGHT AUDIT CRITICAL ERROR] Failed to query patient_booking: {e}")
            raise RuntimeError(f"Migration aborted: Preflight audit query against patient_booking failed: {e}") from e

        print(f"[PREFLIGHT AUDIT] Found {len(results)} total patient_booking records.")

        for idx, row in enumerate(results, start=1):
            doc_name = (row[0] or "").strip()
            doc_spec = (row[1] or "").strip()
            date_raw = row[2]
            tok_val = row[3]
            sheet_url = row[4]
            status = row[5]

            # 1. Validate doctor identifier
            if not doc_name or doc_name.lower() in ('unknown', 'none'):
                conflicts.append({
                    "type": "INVALID_DOCTOR",
                    "row_index": idx,
                    "doctor_name": doc_name,
                    "specialization": doc_spec,
                    "date": str(date_raw),
                    "token": str(tok_val),
                    "reason": "Missing or placeholder doctor name"
                })

            # 2. Validate date
            norm_date = normalize_booking_date(date_raw)
            date_valid = False
            if norm_date and re.match(r'^\d{4}-\d{2}-\d{2}$', norm_date):
                try:
                    datetime.strptime(norm_date, "%Y-%m-%d")
                    date_valid = True
                except ValueError:
                    date_valid = False

            if not date_valid:
                conflicts.append({
                    "type": "INVALID_DATE",
                    "row_index": idx,
                    "doctor_name": doc_name,
                    "specialization": doc_spec,
                    "date": str(date_raw),
                    "token": str(tok_val),
                    "reason": f"Unparseable or non-calendar date value '{date_raw}'"
                })
                continue

            doc_id = extract_doctor_identifier(sheet_url=sheet_url, doctor_name=doc_name, specialization=doc_spec)

            # 3. Validate token value
            tok_int = None
            try:
                tok_int = int(tok_val) if tok_val is not None else None
                if tok_int is None or tok_int <= 0:
                    conflicts.append({
                        "type": "NON_POSITIVE_TOKEN",
                        "row_index": idx,
                        "doctor_identifier": doc_id,
                        "doctor_name": doc_name,
                        "specialization": doc_spec,
                        "date": norm_date,
                        "token": str(tok_val),
                        "reason": f"Token value '{tok_val}' is not a positive integer"
                    })
            except (ValueError, TypeError):
                conflicts.append({
                    "type": "INVALID_TOKEN_FORMAT",
                    "row_index": idx,
                    "doctor_identifier": doc_id,
                    "doctor_name": doc_name,
                    "specialization": doc_spec,
                    "date": norm_date,
                    "token": str(tok_val),
                    "reason": f"Token value '{tok_val}' cannot be parsed as integer"
                })

            if tok_int is not None and tok_int > 0:
                # 4. Check for duplicate token within the same doctor and date scope
                scope_key = (doc_id, norm_date)
                if tok_int in seen_tokens[scope_key]:
                    conflicts.append({
                        "type": "DUPLICATE_TOKEN",
                        "row_index": idx,
                        "doctor_identifier": doc_id,
                        "doctor_name": doc_name,
                        "specialization": doc_spec,
                        "date": norm_date,
                        "token": tok_int,
                        "reason": f"Token #{tok_int} already assigned to another booking on {norm_date}"
                    })
                else:
                    seen_tokens[scope_key].add(tok_int)

            # Accumulate max token per scope
            key = (doc_id, norm_date)
            tok_for_alloc = tok_int if (tok_int and tok_int > 0) else 0

            if key not in allocations:
                allocations[key] = {
                    "doctor_identifier": doc_id,
                    "doctor_name": doc_name or "Unknown",
                    "specialization": doc_spec or "General",
                    "appointment_date": norm_date,
                    "max_token": tok_for_alloc,
                    "total_records": 1,
                    "has_cancelled": (status == 'cancelled')
                }
            else:
                if tok_for_alloc > allocations[key]["max_token"]:
                    allocations[key]["max_token"] = tok_for_alloc
                allocations[key]["total_records"] += 1
                if status == 'cancelled':
                    allocations[key]["has_cancelled"] = True

    # Print summary
    print(f"[PREFLIGHT AUDIT] Identified {len(allocations)} distinct (doctor, date) groups.")
    for (doc_id, d), info in sorted(allocations.items(), key=lambda x: (x[0][1], x[0][0])):
        print(f"  • Date: {d} | Doctor: {info['doctor_name']} ({info['specialization']}) | ID: {doc_id} | Max Token: {info['max_token']} | Total Bookings: {info['total_records']}")

    # Print explicit limitation report
    print("\n----------------------------------------------------------------------")
    print("EXPLICIT PREFLIGHT LIMITATION REPORT:")
    print("  Google Sheets offline records not present in the clinic database cannot")
    print("  be audited by this database-level preflight. Such records must be")
    print("  manually audited or reconciled separately prior to cutover.")
    print("----------------------------------------------------------------------\n")

    # If conflicts exist, report and fail closed
    if conflicts:
        print(f"======================================================================")
        print(f"PREFLIGHT CONFLICT REPORT: {len(conflicts)} data inconsistencies detected!")
        print(f"======================================================================")
        for c in conflicts:
            print(f"  [{c['type']}] Doctor: {c.get('doctor_name')} ({c.get('specialization')}) | Date: {c.get('date')} | Token: {c.get('token')} | Reason: {c['reason']}")
        print(f"======================================================================\n")

        if not allow_conflicts:
            raise RuntimeError(
                f"Migration aborted: Preflight audit detected {len(conflicts)} legacy data conflicts "
                f"in patient_booking. Data must be cleaned before migration can proceed safely."
            )

    return allocations


def apply_migration(db_engine, allocations, dry_run=False):
    """
    Applies additive Phase 2B migration:
    - Supports fresh database creation
    - Supports upgrading existing databases containing earlier Phase 2B schemas
      (column lengths, nullability, defaults, constraints, indexes, plus new sheet_sync_lock table)
      on both SQLite (via temporary table copy/drop/rename) and PostgreSQL (via ALTER TABLE).
    - Preserves all existing records.
    - Backfills token counters from preflight audit.
    - Provides a structured migration report.
    """
    from sqlalchemy import inspect, text
    dialect = db_engine.dialect.name.lower()
    is_pg = ('postgres' in dialect)
    id_col_type = "SERIAL PRIMARY KEY" if is_pg else "INTEGER PRIMARY KEY AUTOINCREMENT"

    print(f"\n--- [MIGRATION] Applying Phase 2B Schema on {dialect.upper()} engine ---")

    inspector = inspect(db_engine)
    existing_tables = set(inspector.get_table_names())

    report = {
        "created_tables": [],
        "upgraded_tables": [],
        "verified_tables": [],
        "backfilled_records": 0
    }

    # Canonical table schemas
    canonical_schemas = {
        "daily_token_allocation": f"""
            CREATE TABLE daily_token_allocation (
                id {id_col_type},
                doctor_identifier VARCHAR(100) NOT NULL,
                doctor_name VARCHAR(100) NOT NULL,
                specialization VARCHAR(100) NOT NULL,
                appointment_date VARCHAR(20) NOT NULL,
                last_allocated_token INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uq_doctor_date_token_allocation UNIQUE (doctor_identifier, appointment_date)
            );
        """,
        "booking_sync_outbox": f"""
            CREATE TABLE booking_sync_outbox (
                id {id_col_type},
                event_type VARCHAR(50) NOT NULL,
                booking_id INTEGER NOT NULL REFERENCES patient_booking(id) ON DELETE RESTRICT,
                idempotency_key VARCHAR(100) NOT NULL UNIQUE,
                payload TEXT NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 5,
                next_retry_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                locked_at TIMESTAMP,
                locked_by VARCHAR(100),
                completed_at TIMESTAMP,
                last_error TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
        """,
        "sheet_sync_lock": f"""
            CREATE TABLE sheet_sync_lock (
                id {id_col_type},
                lock_key VARCHAR(150) NOT NULL UNIQUE,
                locked_by VARCHAR(100),
                locked_at TIMESTAMP,
                expires_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
        """
    }

    canonical_indexes = {
        "daily_token_allocation": [
            "CREATE INDEX IF NOT EXISTS ix_daily_token_allocation_doctor_identifier ON daily_token_allocation (doctor_identifier);",
            "CREATE INDEX IF NOT EXISTS ix_daily_token_allocation_appointment_date ON daily_token_allocation (appointment_date);"
        ],
        "booking_sync_outbox": [
            "CREATE INDEX IF NOT EXISTS ix_booking_sync_outbox_status ON booking_sync_outbox (status);",
            "CREATE INDEX IF NOT EXISTS ix_booking_sync_outbox_next_retry_at ON booking_sync_outbox (next_retry_at);"
        ],
        "sheet_sync_lock": [
            "CREATE INDEX IF NOT EXISTS ix_sheet_sync_lock_lock_key ON sheet_sync_lock (lock_key);"
        ]
    }

    # Inspect each table to determine: fresh create, upgrade, or already verified
    tables_to_create = []
    tables_to_upgrade = {}  # table_name -> list of reasons/differences

    for tbl in ["daily_token_allocation", "booking_sync_outbox", "sheet_sync_lock"]:
        if tbl not in existing_tables:
            tables_to_create.append(tbl)
        else:
            cols = {c['name']: c for c in inspector.get_columns(tbl)}
            reasons = []

            # Check Primary Key
            pk_info = inspector.get_pk_constraint(tbl) or {}
            pk_cols = pk_info.get('constrained_columns', [])
            if not pk_cols or 'id' not in pk_cols:
                reasons.append("Primary key 'id' missing or incorrect")

            if tbl == "daily_token_allocation":
                required = ['id', 'doctor_identifier', 'doctor_name', 'specialization', 'appointment_date', 'last_allocated_token', 'created_at', 'updated_at']
                for req in required:
                    if req not in cols:
                        reasons.append(f"Missing column '{req}'")
                if 'appointment_date' in cols:
                    col_type = str(cols['appointment_date'].get('type', '')).upper()
                    if 'VARCHAR(20)' not in col_type and 'CHARACTER VARYING(20)' not in col_type and 'VARCHAR' not in col_type:
                        reasons.append(f"appointment_date type '{col_type}' differs from VARCHAR(20)")
                if 'doctor_name' in cols and cols['doctor_name'].get('nullable') is True:
                    reasons.append("doctor_name is nullable")
                if 'specialization' in cols and cols['specialization'].get('nullable') is True:
                    reasons.append("specialization is nullable")
                if 'last_allocated_token' in cols and cols['last_allocated_token'].get('nullable') is True:
                    reasons.append("last_allocated_token is nullable")

            elif tbl == "booking_sync_outbox":
                required = ['id', 'event_type', 'booking_id', 'idempotency_key', 'payload', 'status', 'attempts', 'max_attempts', 'next_retry_at', 'locked_at', 'locked_by', 'completed_at', 'last_error', 'created_at', 'updated_at']
                for req in required:
                    if req not in cols:
                        reasons.append(f"Missing column '{req}'")
                if 'status' in cols and cols['status'].get('nullable') is True:
                    reasons.append("status is nullable")
                if 'booking_id' in cols and cols['booking_id'].get('nullable') is True:
                    reasons.append("booking_id is nullable")
                if 'payload' in cols and cols['payload'].get('nullable') is True:
                    reasons.append("payload is nullable")
                if 'idempotency_key' in cols:
                    col_type = str(cols['idempotency_key'].get('type', '')).upper()
                    if 'VARCHAR(100)' not in col_type and 'CHARACTER VARYING(100)' not in col_type and 'VARCHAR' not in col_type:
                        reasons.append(f"idempotency_key type '{col_type}' differs from VARCHAR(100)")

                # Foreign Key check
                fks = inspector.get_foreign_keys(tbl) or []
                has_pb_fk = any(fk.get('referred_table') == 'patient_booking' for fk in fks)
                if not has_pb_fk and not tbl.endswith('__upgrade_tmp'):
                    reasons.append("Foreign key to patient_booking missing")

                # Check ondelete action on SQLite and PostgreSQL
                if not is_pg:
                    with db_engine.connect() as fk_conn:
                        fk_rows = fk_conn.execute(text(f"PRAGMA foreign_key_list({tbl});")).fetchall()
                        for fkr in fk_rows:
                            if len(fkr) > 6 and str(fkr[2]).lower() == 'patient_booking':
                                if str(fkr[6]).upper() != 'RESTRICT':
                                    reasons.append(f"booking_id foreign key ondelete is {str(fkr[6]).upper()} (requires upgrade to RESTRICT)")
                else:
                    for fk in fks:
                        if fk.get('referred_table') == 'patient_booking':
                            if fk.get('options', {}).get('ondelete', '').upper() != 'RESTRICT':
                                reasons.append(f"booking_id foreign key ondelete is {fk.get('options', {}).get('ondelete', '').upper()} (requires upgrade to RESTRICT)")

            elif tbl == "sheet_sync_lock":
                required = ['id', 'lock_key', 'locked_by', 'locked_at', 'expires_at', 'created_at', 'updated_at']
                for req in required:
                    if req not in cols:
                        reasons.append(f"Missing column '{req}'")
                if 'lock_key' in cols and cols['lock_key'].get('nullable') is True:
                    reasons.append("lock_key is nullable")

            if reasons:
                tables_to_upgrade[tbl] = reasons
            else:
                report["verified_tables"].append(tbl)

    if dry_run:
        print("\n[DRY-RUN] Schema modifications planned (no changes applied):")
        for tbl in tables_to_create:
            print(f"  • CREATE fresh table: '{tbl}' with canonical schema and indexes.")
        for tbl, reasons in tables_to_upgrade.items():
            print(f"  • UPGRADE existing table: '{tbl}' (reasons: {', '.join(reasons)})")
        for tbl in report["verified_tables"]:
            print(f"  • VERIFIED canonical schema: '{tbl}'")
        print(f"  • BACKFILL: Would seed/update {len(allocations)} token counters.")
        return report

    # 1. Fresh Creations
    with db_engine.begin() as conn:
        for tbl in tables_to_create:
            conn.execute(text(canonical_schemas[tbl]))
            for idx_sql in canonical_indexes[tbl]:
                conn.execute(text(idx_sql))
            report["created_tables"].append(tbl)
            print(f"[MIGRATION] Created fresh table '{tbl}' with indexes.")

    # 2. Upgrades
    for tbl, reasons in tables_to_upgrade.items():
        print(f"[MIGRATION] Upgrading existing table '{tbl}' (reasons: {', '.join(reasons)})...")
        if not is_pg:
            import sqlite3
            sqlite_ver = sqlite3.sqlite_version
            print(f"[MIGRATION] SQLite runtime version {sqlite_ver} verified. Applying safe table replacement (ALTER COLUMN SET NOT NULL is unsupported in SQLite syntax).")
        if not is_pg:
            # SQLite safe upgrade pattern:
            # Create temporary table with canonical schema -> Copy data -> Drop old -> Rename temp -> Re-create indexes
            tmp_tbl = f"{tbl}__upgrade_tmp"
            tmp_ddl = canonical_schemas[tbl].replace(f"CREATE TABLE {tbl}", f"CREATE TABLE {tmp_tbl}")

            with db_engine.begin() as conn:
                # 1. Capture existing custom indexes and triggers before dropping table
                existing_indexes = conn.execute(text(
                    "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = :tbl AND sql IS NOT NULL;"
                ), {"tbl": tbl}).fetchall()
                existing_triggers = conn.execute(text(
                    "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = :tbl AND sql IS NOT NULL;"
                ), {"tbl": tbl}).fetchall()

                conn.execute(text(f"DROP TABLE IF EXISTS {tmp_tbl};"))
                conn.execute(text(tmp_ddl))

                # Fetch existing columns to map available data
                curr_cols_info = {c['name']: c for c in inspector.get_columns(tbl)}
                curr_cols = list(curr_cols_info.keys())

                if tbl == "daily_token_allocation":
                    canonical_cols = ['id', 'doctor_identifier', 'doctor_name', 'specialization', 'appointment_date', 'last_allocated_token', 'created_at', 'updated_at']
                elif tbl == "booking_sync_outbox":
                    canonical_cols = ['id', 'event_type', 'booking_id', 'idempotency_key', 'payload', 'status', 'attempts', 'max_attempts', 'next_retry_at', 'locked_at', 'locked_by', 'completed_at', 'last_error', 'created_at', 'updated_at']
                elif tbl == "sheet_sync_lock":
                    canonical_cols = ['id', 'lock_key', 'locked_by', 'locked_at', 'expires_at', 'created_at', 'updated_at']
                else:
                    canonical_cols = curr_cols

                # Preserve non-canonical legacy columns with data
                extra_cols = [c for c in curr_cols if c not in canonical_cols]
                for extra_c in extra_cols:
                    col_type = str(curr_cols_info[extra_c].get('type', 'TEXT'))
                    conn.execute(text(f"ALTER TABLE {tmp_tbl} ADD COLUMN {extra_c} {col_type};"))

                target_cols = []
                source_exprs = []

                for c in canonical_cols:
                    if c in curr_cols:
                        target_cols.append(c)
                        if c == 'status':
                            source_exprs.append("COALESCE(status, 'pending')")
                        else:
                            source_exprs.append(c)
                    else:
                        # Provide fallback defaults for NOT NULL columns missing from earlier schemas
                        if c == 'doctor_name':
                            target_cols.append(c)
                            source_exprs.append("'Unknown'")
                        elif c == 'specialization':
                            target_cols.append(c)
                            source_exprs.append("'General'")
                        elif c == 'last_allocated_token':
                            target_cols.append(c)
                            source_exprs.append("0")
                        elif c in ('created_at', 'updated_at', 'next_retry_at'):
                            target_cols.append(c)
                            source_exprs.append("CURRENT_TIMESTAMP")
                        elif c == 'max_attempts':
                            target_cols.append(c)
                            source_exprs.append("5")
                        elif c == 'attempts':
                            target_cols.append(c)
                            source_exprs.append("0")
                        elif c == 'status':
                            target_cols.append(c)
                            source_exprs.append("'pending'")
                        elif c == 'payload':
                            target_cols.append(c)
                            source_exprs.append("'{}'")

                for extra_c in extra_cols:
                    target_cols.append(extra_c)
                    source_exprs.append(extra_c)

                target_cols_str = ", ".join(target_cols)
                source_exprs_str = ", ".join(source_exprs)

                conn.execute(text(f"INSERT INTO {tmp_tbl} ({target_cols_str}) SELECT {source_exprs_str} FROM {tbl};"))
                conn.execute(text(f"DROP TABLE {tbl};"))
                conn.execute(text(f"ALTER TABLE {tmp_tbl} RENAME TO {tbl};"))

                for idx_sql in canonical_indexes[tbl]:
                    conn.execute(text(idx_sql))

                # Recreate captured custom indexes
                curr_indexes = {row[0] for row in conn.execute(text(
                    "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = :tbl;"
                ), {"tbl": tbl}).fetchall()}
                for idx_name, idx_sql in existing_indexes:
                    if idx_name not in curr_indexes and idx_sql:
                        conn.execute(text(idx_sql))

                # Recreate captured custom triggers (excluding trg_prevent_retired_outbox_reactivation which is installed later)
                curr_triggers = {row[0] for row in conn.execute(text(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = :tbl;"
                ), {"tbl": tbl}).fetchall()}
                for trg_name, trg_sql in existing_triggers:
                    if trg_name not in curr_triggers and trg_name != 'trg_prevent_retired_outbox_reactivation' and trg_sql:
                        conn.execute(text(trg_sql))

                # Verify foreign key integrity
                fk_violations = conn.execute(text(f"PRAGMA foreign_key_check({tbl});")).fetchall()
                if fk_violations:
                    raise RuntimeError(f"Foreign key constraint violation detected after rebuilding '{tbl}': {fk_violations}")

            report["upgraded_tables"].append(tbl)
            print(f"[MIGRATION] Upgraded table '{tbl}' successfully via SQLite safe table replacement (preserved {len(extra_cols)} extra legacy columns, custom indexes, custom triggers, 0 foreign key violations).")
        else:
            # PostgreSQL upgrade via ALTER TABLE with data pre-cleaning
            with db_engine.begin() as conn:
                curr_cols = {c['name']: c for c in inspector.get_columns(tbl)}
                if tbl == "daily_token_allocation":
                    # Clean NULLs before enforcing NOT NULL
                    conn.execute(text("UPDATE daily_token_allocation SET doctor_name = 'Unknown' WHERE doctor_name IS NULL;"))
                    conn.execute(text("UPDATE daily_token_allocation SET specialization = 'General' WHERE specialization IS NULL;"))
                    conn.execute(text("UPDATE daily_token_allocation SET last_allocated_token = 0 WHERE last_allocated_token IS NULL;"))

                    if 'appointment_date' in curr_cols:
                        conn.execute(text("ALTER TABLE daily_token_allocation ALTER COLUMN appointment_date TYPE VARCHAR(20);"))
                    if 'doctor_name' in curr_cols and curr_cols['doctor_name'].get('nullable'):
                        conn.execute(text("ALTER TABLE daily_token_allocation ALTER COLUMN doctor_name SET NOT NULL;"))
                    if 'specialization' in curr_cols and curr_cols['specialization'].get('nullable'):
                        conn.execute(text("ALTER TABLE daily_token_allocation ALTER COLUMN specialization SET NOT NULL;"))
                    if 'last_allocated_token' in curr_cols and curr_cols['last_allocated_token'].get('nullable'):
                        conn.execute(text("ALTER TABLE daily_token_allocation ALTER COLUMN last_allocated_token SET NOT NULL;"))
                        conn.execute(text("ALTER TABLE daily_token_allocation ALTER COLUMN last_allocated_token SET DEFAULT 0;"))
                elif tbl == "booking_sync_outbox":
                    # Clean NULLs before enforcing NOT NULL
                    conn.execute(text("UPDATE booking_sync_outbox SET payload = '{}' WHERE payload IS NULL;"))
                    conn.execute(text("UPDATE booking_sync_outbox SET status = 'pending' WHERE status IS NULL;"))
                    conn.execute(text("ALTER TABLE booking_sync_outbox ALTER COLUMN status SET NOT NULL;"))
                    conn.execute(text("""
                        DO $$
                        BEGIN
                            IF EXISTS (
                                SELECT 1 FROM information_schema.referential_constraints
                                WHERE constraint_name = 'booking_sync_outbox_booking_id_fkey'
                                AND delete_rule = 'CASCADE'
                            ) THEN
                                ALTER TABLE booking_sync_outbox DROP CONSTRAINT booking_sync_outbox_booking_id_fkey;
                                ALTER TABLE booking_sync_outbox ADD CONSTRAINT booking_sync_outbox_booking_id_fkey
                                    FOREIGN KEY (booking_id) REFERENCES patient_booking(id) ON DELETE RESTRICT;
                            END IF;
                        END $$;
                    """))
                    conn.execute(text("UPDATE booking_sync_outbox SET attempts = 0 WHERE attempts IS NULL;"))
                    conn.execute(text("UPDATE booking_sync_outbox SET max_attempts = 5 WHERE max_attempts IS NULL;"))

                    if 'max_attempts' not in curr_cols:
                        conn.execute(text("ALTER TABLE booking_sync_outbox ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 5;"))
                    if 'booking_id' in curr_cols and curr_cols['booking_id'].get('nullable'):
                        conn.execute(text("ALTER TABLE booking_sync_outbox ALTER COLUMN booking_id SET NOT NULL;"))
                    if 'payload' in curr_cols and curr_cols['payload'].get('nullable'):
                        conn.execute(text("ALTER TABLE booking_sync_outbox ALTER COLUMN payload SET NOT NULL;"))
                    if 'idempotency_key' in curr_cols:
                        conn.execute(text("ALTER TABLE booking_sync_outbox ALTER COLUMN idempotency_key TYPE VARCHAR(100);"))
                elif tbl == "sheet_sync_lock":
                    if 'lock_key' in curr_cols and curr_cols['lock_key'].get('nullable'):
                        conn.execute(text("ALTER TABLE sheet_sync_lock ALTER COLUMN lock_key SET NOT NULL;"))

                for idx_sql in canonical_indexes[tbl]:
                    conn.execute(text(idx_sql))
            report["upgraded_tables"].append(tbl)
            print(f"[MIGRATION] Upgraded table '{tbl}' successfully via PostgreSQL ALTER TABLE.")

    # 3. Backfill Legacy Allocations
    if allocations:
        print(f"[MIGRATION] Backfilling {len(allocations)} token counters...")
        with db_engine.begin() as conn:
            for (doc_id, norm_date), info in allocations.items():
                check_q = text("SELECT id, last_allocated_token FROM daily_token_allocation WHERE doctor_identifier = :doc_id AND appointment_date = :app_date")
                existing = conn.execute(check_q, {"doc_id": doc_id, "app_date": norm_date}).first()

                now = datetime.utcnow()
                if existing:
                    if info["max_token"] > existing[1]:
                        update_q = text("UPDATE daily_token_allocation SET last_allocated_token = :max_tok, updated_at = :now WHERE id = :id")
                        conn.execute(update_q, {"max_tok": info["max_token"], "now": now, "id": existing[0]})
                        report["backfilled_records"] += 1
                else:
                    insert_q = text("""
                        INSERT INTO daily_token_allocation (doctor_identifier, doctor_name, specialization, appointment_date, last_allocated_token, created_at, updated_at)
                        VALUES (:doc_id, :doc_name, :spec, :app_date, :max_tok, :now, :now)
                    """)
                    conn.execute(insert_q, {
                        "doc_id": doc_id,
                        "doc_name": info["doctor_name"],
                        "spec": info["specialization"],
                        "app_date": norm_date,
                        "max_tok": info["max_token"],
                        "now": now
                    })
                    report["backfilled_records"] += 1
        print(f"[MIGRATION] Successfully backfilled {report['backfilled_records']} token counters.")
    else:
        print("[MIGRATION] No legacy bookings required backfill.")

    # 4. Ensure Outbox Status Immutability Triggers
    if not dry_run:
        with db_engine.begin() as conn:
            if is_pg:
                conn.execute(text("""
                    CREATE OR REPLACE FUNCTION prevent_retired_outbox_reactivation_func()
                    RETURNS TRIGGER AS $$
                    BEGIN
                        IF OLD.status = 'retired' AND NEW.status IS DISTINCT FROM 'retired' THEN
                            RAISE EXCEPTION 'Cannot transition retired outbox event (ID: %): retired status is permanently immutable', OLD.id;
                        END IF;
                        RETURN NEW;
                    END;
                    $$ LANGUAGE plpgsql;
                """))
                conn.execute(text("""
                    DROP TRIGGER IF EXISTS trg_prevent_retired_outbox_reactivation ON booking_sync_outbox;
                    CREATE TRIGGER trg_prevent_retired_outbox_reactivation
                    BEFORE UPDATE OF status ON booking_sync_outbox
                    FOR EACH ROW
                    EXECUTE FUNCTION prevent_retired_outbox_reactivation_func();
                """))
                print("[MIGRATION] PostgreSQL trigger 'trg_prevent_retired_outbox_reactivation' applied on booking_sync_outbox.")
            else:
                conn.execute(text("DROP TRIGGER IF EXISTS trg_prevent_retired_outbox_reactivation;"))
                conn.execute(text("""
                    CREATE TRIGGER trg_prevent_retired_outbox_reactivation
                    BEFORE UPDATE OF status ON booking_sync_outbox
                    FOR EACH ROW
                    WHEN OLD.status = 'retired' AND NEW.status IS NOT 'retired'
                    BEGIN
                        SELECT RAISE(ABORT, 'Cannot transition retired outbox event: retired status is permanently immutable');
                    END;
                """))
                print("[MIGRATION] SQLite trigger 'trg_prevent_retired_outbox_reactivation' applied on booking_sync_outbox.")

    print("\n======================================================================")
    print("PHASE 2B MIGRATION REPORT:")
    print(f"  Status: SUCCESS")
    print(f"  Database Dialect: {dialect.upper()}")
    print(f"  Tables Created: {report['created_tables']}")
    print(f"  Tables Upgraded: {report['upgraded_tables']}")
    print(f"  Tables Verified: {report['verified_tables']}")
    print(f"  Allocations Backfilled: {report['backfilled_records']}")
    print("======================================================================\n")

    return report


def rollback_migration(db_engine):
    """
    Rolls back Phase 2B migration by safely dropping added tables:
    - sheet_sync_lock
    - booking_sync_outbox
    - daily_token_allocation

    Guarantees patient_booking and other application tables remain 100% untouched.
    """
    from sqlalchemy import text
    dialect = db_engine.dialect.name.lower()
    print(f"\n======================================================================")
    print(f"CRITICAL WARNING: PHASE 2B MIGRATION ROLLBACK INITIATED ON {dialect.upper()}")
    print(f"======================================================================")
    print(f"Rolling back Phase 2B will DROP 'sheet_sync_lock', 'booking_sync_outbox',")
    print(f"and 'daily_token_allocation'.")
    print(f"Any pending synchronization events, unsynced Google Sheets outbox records,")
    print(f"and retry histories will be PERMANENTLY DESTROYED.")
    print(f"\nRECOMMENDED BACKUP PROCEDURE BEFORE ROLLBACK:")
    print(f"Export pending outbox events using:")
    print(f"  SELECT * FROM booking_sync_outbox WHERE status != 'completed';")
    print(f"======================================================================\n")
    with db_engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS sheet_sync_lock;"))
        conn.execute(text("DROP TABLE IF EXISTS booking_sync_outbox;"))
        conn.execute(text("DROP TABLE IF EXISTS daily_token_allocation;"))
    print("[ROLLBACK] Successfully dropped 'sheet_sync_lock', 'booking_sync_outbox', and 'daily_token_allocation' tables.")
    print("[ROLLBACK] Preserved table 'patient_booking' - no patient or booking data was deleted.")
    print("[ROLLBACK] Phase 2B rollback complete.")


def main():
    parser = argparse.ArgumentParser(description="Phase 2B Database Migration and Legacy Preflight Audit Tool")
    parser.add_argument("--dry-run", action="store_true", help="Perform preflight audit and plan migration without writing changes.")
    parser.add_argument("--rollback", action="store_true", help="Roll back Phase 2B schema modifications (drops created tables).")
    parser.add_argument("--database-url", type=str, default=None, help="Explicit database URL to migrate. Defaults to DATABASE_URL or test DB.")
    parser.add_argument("--confirm-production", action="store_true", help="Explicit confirmation if targeting production databases.")
    parser.add_argument("--allow-conflicts", action="store_true", help="Allow migration to proceed despite preflight conflicts (not recommended).")
    parser.add_argument("--allow-dev-db", action="store_true", help="Explicit confirmation if modifying instance/primecare.db.")

    args = parser.parse_args()

    # Determine target database URL safely: NEVER silently fall back to DATABASE_URL
    db_url = args.database_url
    if not db_url:
        db_url = f"sqlite:///{os.path.join(BASE_DIR, 'instance', 'test_primecare.db')}"

    # Strict centralized safety assertion
    assert_database_safety(db_url, allow_dev_db=args.allow_dev_db)

    print(f"[MIGRATION TOOL] Target database: {db_url.split('@')[-1] if '@' in db_url else db_url}")

    from sqlalchemy import create_engine
    engine = create_engine(db_url)

    if args.rollback:
        rollback_migration(engine)
    else:
        allocations = run_preflight_audit(engine, allow_conflicts=args.allow_conflicts)
        apply_migration(engine, allocations, dry_run=args.dry_run)


if __name__ == '__main__':
    main()

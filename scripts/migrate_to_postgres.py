"""
scripts/migrate_to_postgres.py
==============================
Versioned, Dialect-Safe Database Migration Utility for PrimeCare Clinic.

Supports:
  - PostgreSQL (Local isolated test instance strictly on 127.0.0.1:5433/primecare_test)
  - SQLite (Local development & isolated testing)

Safety Rules:
  - Strictly prevents connecting to Neon production database.
  - Non-destructive: preserves 100% of existing users, bookings, and tokens.
  - Idempotent: can be executed repeatedly without errors or data duplication.
  - Reversible schema verification.
"""

import os
import sys
import datetime
from urllib.parse import urlparse
from sqlalchemy import (
    create_engine, inspect, text, MetaData, Table, Column, Integer,
    String, Boolean, DateTime, Text, Numeric, ForeignKey, UniqueConstraint, Index
)


def assert_safe_database_url(db_url: str):
    """
    Enforces strict safety assertions against production database leakage.
    Allows only SQLite and PostgreSQL URLs.
    Blocks any URL containing 'neon.tech', 'ep-small-dream', or production indicators under any circumstances.
    For PostgreSQL, strictly requires local test instance on 127.0.0.1 or localhost, port 5433, database primecare_test.
    Never logs database passwords.
    """
    if not db_url:
        return
    parsed = urlparse(db_url)
    scheme = parsed.scheme.lower()

    if scheme not in ('sqlite', 'postgresql', 'postgres'):
        raise RuntimeError(
            f"CRITICAL ABORT: Database scheme '{scheme}' is not authorized. "
            "Only SQLite and PostgreSQL URLs are permitted."
        )

    full_url_lower = db_url.lower()
    hostname = (parsed.hostname or "").lower()
    if 'neon.tech' in full_url_lower or 'ep-small-dream' in full_url_lower or 'neon' in hostname:
        raise RuntimeError(
            "CRITICAL ABORT: Migration is attempting to connect to production Neon database! "
            "Production Neon PostgreSQL is strictly off-limits under any circumstances."
        )

    if scheme in ('postgresql', 'postgres'):
        if hostname not in ('127.0.0.1', 'localhost'):
            raise RuntimeError(f"CRITICAL ABORT: PostgreSQL target host '{hostname}' is not authorized. Must be 127.0.0.1 or localhost.")
        if parsed.port != 5433:
            raise RuntimeError(f"CRITICAL ABORT: PostgreSQL target port '{parsed.port}' is not authorized. Must be port 5433.")
        db_name = parsed.path.lstrip('/')
        if db_name != 'primecare_test':
            raise RuntimeError(f"CRITICAL ABORT: PostgreSQL target database '{db_name}' is not authorized. Must be primecare_test.")


def get_target_db_url():
    """
    Retrieves and validates the target database URL with strict production guardrails.
    """
    db_url = os.environ.get('MIGRATION_DATABASE_URL') or os.environ.get('DATABASE_URL')
    if not db_url:
        db_path = os.path.join(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')), 'instance', 'primecare.db')
        db_url = f"sqlite:///{db_path}"

    if db_url.startswith('postgres://'):
        db_url = db_url.replace('postgres://', 'postgresql://', 1)

    assert_safe_database_url(db_url)
    return db_url


def run_migration(db_url=None, dry_run=False):
    """
    Executes the schema migration against the configured database.
    """
    if db_url is None:
        db_url = get_target_db_url()
    else:
        assert_safe_database_url(db_url)

    is_sqlite = db_url.startswith('sqlite')
    dialect_name = 'SQLITE' if is_sqlite else 'POSTGRESQL'

    print(f"\n======================================================================")
    print(f"PRIMECARE CLINIC DATABASE MIGRATION — {dialect_name}")
    print(f"Target: {'[LOCAL SQLITE]' if is_sqlite else '[ISOLATED POSTGRESQL]'}")
    print(f"Mode: {'DRY RUN (No changes applied)' if dry_run else 'APPLY MIGRATION'}")
    print(f"======================================================================\n")

    engine = create_engine(db_url)
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    tables_created = []
    tables_upgraded = []
    tables_verified = []
    columns_added = []

    with engine.connect() as conn:
        # 0. BASE APPLICATION TABLES (USER, DOCTOR_SESSION)
        if 'user' not in existing_tables:
            print("[MIGRATION] Creating table 'user'...")
            if not dry_run:
                if is_sqlite:
                    conn.execute(text("""
                        CREATE TABLE user (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            name VARCHAR(100) NOT NULL,
                            email VARCHAR(120) NOT NULL UNIQUE,
                            password_hash VARCHAR(255) NOT NULL,
                            role VARCHAR(20) NOT NULL DEFAULT 'patient',
                            phone_number VARCHAR(20),
                            phone_verified BOOLEAN DEFAULT 0,
                            phone_verified_at DATETIME,
                            email_verified BOOLEAN DEFAULT 0,
                            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                        );
                    """))
                    conn.execute(text("CREATE INDEX idx_user_email ON user (email);"))
                    conn.execute(text("CREATE INDEX idx_user_phone ON user (phone_number);"))
                else:
                    conn.execute(text("""
                        CREATE TABLE "user" (
                            id SERIAL PRIMARY KEY,
                            name VARCHAR(100) NOT NULL,
                            email VARCHAR(120) NOT NULL UNIQUE,
                            password_hash VARCHAR(255) NOT NULL,
                            role VARCHAR(20) NOT NULL DEFAULT 'patient',
                            phone_number VARCHAR(20),
                            phone_verified BOOLEAN DEFAULT FALSE,
                            phone_verified_at TIMESTAMP WITHOUT TIME ZONE,
                            email_verified BOOLEAN DEFAULT FALSE,
                            created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
                            updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
                        );
                    """))
                    conn.execute(text("CREATE INDEX idx_user_email ON \"user\" (email);"))
                    conn.execute(text("CREATE INDEX idx_user_phone ON \"user\" (phone_number);"))
                conn.commit()
            tables_created.append('user')
        else:
            tables_verified.append('user')

        # DOCTOR_SESSION TABLE & UPGRADES
        if 'doctor_session' not in existing_tables:
            print("[MIGRATION] Creating table 'doctor_session'...")
            if not dry_run:
                if is_sqlite:
                    conn.execute(text("""
                        CREATE TABLE doctor_session (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            doctor_name VARCHAR(100) NOT NULL,
                            specialization VARCHAR(100) NOT NULL,
                            session_date VARCHAR(20),
                            email VARCHAR(120),
                            phone_number VARCHAR(20),
                            phone_updated_by VARCHAR(120),
                            phone_updated_at DATETIME,
                            status VARCHAR(50) DEFAULT 'scheduled',
                            total_tokens INTEGER DEFAULT 0,
                            current_token INTEGER DEFAULT 0,
                            start_time VARCHAR(20),
                            end_time VARCHAR(20),
                            skipped_tokens TEXT DEFAULT '',
                            broadcast_message VARCHAR(500),
                            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                        );
                    """))
                    conn.execute(text("CREATE INDEX idx_session_doc ON doctor_session (doctor_name, specialization);"))
                else:
                    conn.execute(text("""
                        CREATE TABLE doctor_session (
                            id SERIAL PRIMARY KEY,
                            doctor_name VARCHAR(100) NOT NULL,
                            specialization VARCHAR(100) NOT NULL,
                            session_date VARCHAR(20),
                            email VARCHAR(120),
                            phone_number VARCHAR(20),
                            phone_updated_by VARCHAR(120),
                            phone_updated_at TIMESTAMP WITHOUT TIME ZONE,
                            status VARCHAR(50) DEFAULT 'scheduled',
                            total_tokens INTEGER DEFAULT 0,
                            current_token INTEGER DEFAULT 0,
                            start_time VARCHAR(20),
                            end_time VARCHAR(20),
                            skipped_tokens TEXT DEFAULT '',
                            broadcast_message VARCHAR(500),
                            created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
                            updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
                        );
                    """))
                    conn.execute(text("CREATE INDEX idx_session_doc ON doctor_session (doctor_name, specialization);"))
                conn.commit()
            tables_created.append('doctor_session')
        else:
            ds_cols = {c['name']: c for c in inspector.get_columns('doctor_session')}
            ds_upgraded = False

            if 'phone_updated_by' not in ds_cols:
                print("[MIGRATION] Adding column 'phone_updated_by' to table 'doctor_session'...")
                if not dry_run:
                    conn.execute(text("ALTER TABLE doctor_session ADD COLUMN phone_updated_by VARCHAR(120);"))
                    conn.commit()
                columns_added.append('doctor_session.phone_updated_by')
                ds_upgraded = True

            if 'phone_updated_at' not in ds_cols:
                print("[MIGRATION] Adding column 'phone_updated_at' to table 'doctor_session'...")
                if not dry_run:
                    if is_sqlite:
                        conn.execute(text("ALTER TABLE doctor_session ADD COLUMN phone_updated_at DATETIME;"))
                    else:
                        conn.execute(text("ALTER TABLE doctor_session ADD COLUMN phone_updated_at TIMESTAMP WITHOUT TIME ZONE;"))
                    conn.commit()
                columns_added.append('doctor_session.phone_updated_at')
                ds_upgraded = True

            if 'start_time' not in ds_cols:
                print("[MIGRATION] Adding column 'start_time' to table 'doctor_session'...")
                if not dry_run:
                    conn.execute(text("ALTER TABLE doctor_session ADD COLUMN start_time VARCHAR(20);"))
                    conn.commit()
                columns_added.append('doctor_session.start_time')
                ds_upgraded = True

            if 'end_time' not in ds_cols:
                print("[MIGRATION] Adding column 'end_time' to table 'doctor_session'...")
                if not dry_run:
                    conn.execute(text("ALTER TABLE doctor_session ADD COLUMN end_time VARCHAR(20);"))
                    conn.commit()
                columns_added.append('doctor_session.end_time')
                ds_upgraded = True

            if 'skipped_tokens' not in ds_cols:
                print("[MIGRATION] Adding column 'skipped_tokens' to table 'doctor_session'...")
                if not dry_run:
                    conn.execute(text("ALTER TABLE doctor_session ADD COLUMN skipped_tokens TEXT;"))
                    conn.commit()
                columns_added.append('doctor_session.skipped_tokens')
                ds_upgraded = True

            if 'broadcast_message' not in ds_cols:
                print("[MIGRATION] Adding column 'broadcast_message' to table 'doctor_session'...")
                if not dry_run:
                    conn.execute(text("ALTER TABLE doctor_session ADD COLUMN broadcast_message VARCHAR(500);"))
                    conn.commit()
                columns_added.append('doctor_session.broadcast_message')
                ds_upgraded = True

            if ds_upgraded:
                tables_upgraded.append('doctor_session')
            else:
                tables_verified.append('doctor_session')

        # 1. SPECIALIZATION TABLE
        if 'specialization' not in existing_tables:
            print("[MIGRATION] Creating table 'specialization'...")
            if not dry_run:
                if is_sqlite:
                    conn.execute(text("""
                        CREATE TABLE specialization (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            name VARCHAR(100) NOT NULL UNIQUE,
                            description VARCHAR(255),
                            is_active BOOLEAN NOT NULL DEFAULT 1,
                            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                        );
                    """))
                    conn.execute(text("CREATE INDEX idx_spec_name ON specialization (name);"))
                else:
                    conn.execute(text("""
                        CREATE TABLE specialization (
                            id SERIAL PRIMARY KEY,
                            name VARCHAR(100) NOT NULL UNIQUE,
                            description VARCHAR(255),
                            is_active BOOLEAN NOT NULL DEFAULT TRUE,
                            created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
                            updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
                        );
                    """))
                    conn.execute(text("CREATE INDEX idx_spec_name ON specialization (name);"))
                conn.commit()
            tables_created.append('specialization')
        else:
            tables_verified.append('specialization')

        # 2. DOCTOR TABLE & UPGRADES
        if 'doctor' not in existing_tables:
            print("[MIGRATION] Creating table 'doctor'...")
            if not dry_run:
                if is_sqlite:
                    conn.execute(text("""
                        CREATE TABLE doctor (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            name VARCHAR(100) NOT NULL,
                            specialization_id INTEGER REFERENCES specialization(id) ON DELETE SET NULL,
                            specialization VARCHAR(100) NOT NULL,
                            qualification VARCHAR(100),
                            email VARCHAR(120) UNIQUE,
                            phone_number VARCHAR(20),
                            image_url VARCHAR(500),
                            is_active BOOLEAN NOT NULL DEFAULT 1,
                            consultation_fee NUMERIC(10, 2),
                            max_daily_patients INTEGER NOT NULL DEFAULT 25,
                            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                        );
                    """))
                    conn.execute(text("CREATE INDEX idx_doc_name ON doctor (name);"))
                    conn.execute(text("CREATE INDEX idx_doc_spec ON doctor (specialization);"))
                    conn.execute(text("CREATE INDEX idx_doc_active ON doctor (is_active);"))
                else:
                    conn.execute(text("""
                        CREATE TABLE doctor (
                            id SERIAL PRIMARY KEY,
                            name VARCHAR(100) NOT NULL,
                            specialization_id INTEGER REFERENCES specialization(id) ON DELETE SET NULL,
                            specialization VARCHAR(100) NOT NULL,
                            qualification VARCHAR(100),
                            email VARCHAR(120) UNIQUE,
                            phone_number VARCHAR(20),
                            image_url VARCHAR(500),
                            is_active BOOLEAN NOT NULL DEFAULT TRUE,
                            consultation_fee NUMERIC(10, 2),
                            max_daily_patients INTEGER NOT NULL DEFAULT 25,
                            created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
                            updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
                        );
                    """))
                    conn.execute(text("CREATE INDEX idx_doc_name ON doctor (name);"))
                    conn.execute(text("CREATE INDEX idx_doc_spec ON doctor (specialization);"))
                    conn.execute(text("CREATE INDEX idx_doc_active ON doctor (is_active);"))
                conn.commit()
            tables_created.append('doctor')
        else:
            doc_cols = {c['name']: c for c in inspector.get_columns('doctor')}
            if 'max_daily_patients' not in doc_cols:
                print("[MIGRATION] Adding column 'max_daily_patients' to table 'doctor'...")
                if not dry_run:
                    conn.execute(text("ALTER TABLE doctor ADD COLUMN max_daily_patients INTEGER NOT NULL DEFAULT 25;"))
                    conn.commit()
                columns_added.append('doctor.max_daily_patients')
                tables_upgraded.append('doctor')
            else:
                tables_verified.append('doctor')

        # 3. DOCTOR_SCHEDULE TABLE
        if 'doctor_schedule' not in existing_tables:
            print("[MIGRATION] Creating table 'doctor_schedule'...")
            if not dry_run:
                if is_sqlite:
                    conn.execute(text("""
                        CREATE TABLE doctor_schedule (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            doctor_id INTEGER NOT NULL REFERENCES doctor(id) ON DELETE CASCADE,
                            day_of_week VARCHAR(20) NOT NULL,
                            start_time VARCHAR(10) NOT NULL,
                            end_time VARCHAR(10) NOT NULL,
                            is_available BOOLEAN NOT NULL DEFAULT 1,
                            is_override BOOLEAN NOT NULL DEFAULT 0,
                            effective_date VARCHAR(20),
                            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                        );
                    """))
                    conn.execute(text("CREATE INDEX idx_sched_lookup ON doctor_schedule (doctor_id, day_of_week);"))
                else:
                    conn.execute(text("""
                        CREATE TABLE doctor_schedule (
                            id SERIAL PRIMARY KEY,
                            doctor_id INTEGER NOT NULL REFERENCES doctor(id) ON DELETE CASCADE,
                            day_of_week VARCHAR(20) NOT NULL,
                            start_time VARCHAR(10) NOT NULL,
                            end_time VARCHAR(10) NOT NULL,
                            is_available BOOLEAN NOT NULL DEFAULT TRUE,
                            is_override BOOLEAN NOT NULL DEFAULT FALSE,
                            effective_date VARCHAR(20),
                            created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
                            updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
                        );
                    """))
                    conn.execute(text("CREATE INDEX idx_sched_lookup ON doctor_schedule (doctor_id, day_of_week);"))
                conn.commit()
            tables_created.append('doctor_schedule')
        else:
            tables_verified.append('doctor_schedule')

        # 4. DOCTOR_LEAVE TABLE
        if 'doctor_leave' not in existing_tables:
            print("[MIGRATION] Creating table 'doctor_leave'...")
            if not dry_run:
                if is_sqlite:
                    conn.execute(text("""
                        CREATE TABLE doctor_leave (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            doctor_id INTEGER REFERENCES doctor(id) ON DELETE CASCADE,
                            doctor_name VARCHAR(100) NOT NULL,
                            specialization VARCHAR(100),
                            date VARCHAR(20) NOT NULL,
                            end_date VARCHAR(20),
                            reason VARCHAR(255),
                            status VARCHAR(20) NOT NULL DEFAULT 'active',
                            approved_by VARCHAR(120),
                            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                        );
                    """))
                    conn.execute(text("CREATE INDEX idx_leave_doc_date ON doctor_leave (doctor_name, date, status);"))
                else:
                    conn.execute(text("""
                        CREATE TABLE doctor_leave (
                            id SERIAL PRIMARY KEY,
                            doctor_id INTEGER REFERENCES doctor(id) ON DELETE CASCADE,
                            doctor_name VARCHAR(100) NOT NULL,
                            specialization VARCHAR(100),
                            date VARCHAR(20) NOT NULL,
                            end_date VARCHAR(20),
                            reason VARCHAR(255),
                            status VARCHAR(20) NOT NULL DEFAULT 'active',
                            approved_by VARCHAR(120),
                            created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
                            updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
                        );
                    """))
                    conn.execute(text("CREATE INDEX idx_leave_doc_date ON doctor_leave (doctor_name, date, status);"))
                conn.commit()
            tables_created.append('doctor_leave')
        else:
            tables_verified.append('doctor_leave')

        # 5. CLINIC_HOLIDAY TABLE
        if 'clinic_holiday' not in existing_tables:
            print("[MIGRATION] Creating table 'clinic_holiday'...")
            if not dry_run:
                if is_sqlite:
                    conn.execute(text("""
                        CREATE TABLE clinic_holiday (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            date VARCHAR(20) NOT NULL UNIQUE,
                            reason VARCHAR(255),
                            is_recurring BOOLEAN NOT NULL DEFAULT 0,
                            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                        );
                    """))
                    conn.execute(text("CREATE INDEX idx_holiday_date ON clinic_holiday (date);"))
                else:
                    conn.execute(text("""
                        CREATE TABLE clinic_holiday (
                            id SERIAL PRIMARY KEY,
                            date VARCHAR(20) NOT NULL UNIQUE,
                            reason VARCHAR(255),
                            is_recurring BOOLEAN NOT NULL DEFAULT FALSE,
                            created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
                            updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
                        );
                    """))
                    conn.execute(text("CREATE INDEX idx_holiday_date ON clinic_holiday (date);"))
                conn.commit()
            tables_created.append('clinic_holiday')
        else:
            tables_verified.append('clinic_holiday')

        # 6. PATIENT_BOOKING TABLE & UPGRADES
        if 'patient_booking' not in existing_tables:
            print("[MIGRATION] Creating table 'patient_booking'...")
            if not dry_run:
                if is_sqlite:
                    conn.execute(text("""
                        CREATE TABLE patient_booking (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            user_id INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
                            doctor_id INTEGER REFERENCES doctor(id) ON DELETE SET NULL,
                            doctor_name VARCHAR(100) NOT NULL,
                            specialization VARCHAR(100) NOT NULL,
                            date VARCHAR(20) NOT NULL,
                            time VARCHAR(50),
                            token INTEGER NOT NULL,
                            sheet_url VARCHAR(255),
                            patient_name VARCHAR(100) NOT NULL,
                            age VARCHAR(10),
                            gender VARCHAR(20),
                            phone_number VARCHAR(20),
                            status VARCHAR(50) NOT NULL DEFAULT 'confirmed',
                            booking_source VARCHAR(50) DEFAULT 'web',
                            cancelled_by VARCHAR(50),
                            cancellation_reason VARCHAR(255),
                            cancelled_at VARCHAR(50),
                            consultation_start_time DATETIME,
                            consultation_end_time DATETIME,
                            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                        );
                    """))
                    conn.execute(text("CREATE INDEX idx_booking_doctor_date_status ON patient_booking (doctor_name, date, status);"))
                    conn.execute(text("CREATE INDEX idx_booking_user ON patient_booking (user_id);"))
                else:
                    conn.execute(text("""
                        CREATE TABLE patient_booking (
                            id SERIAL PRIMARY KEY,
                            user_id INTEGER NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
                            doctor_id INTEGER REFERENCES doctor(id) ON DELETE SET NULL,
                            doctor_name VARCHAR(100) NOT NULL,
                            specialization VARCHAR(100) NOT NULL,
                            date VARCHAR(20) NOT NULL,
                            time VARCHAR(50),
                            token INTEGER NOT NULL,
                            sheet_url VARCHAR(255),
                            patient_name VARCHAR(100) NOT NULL,
                            age VARCHAR(10),
                            gender VARCHAR(20),
                            phone_number VARCHAR(20),
                            status VARCHAR(50) NOT NULL DEFAULT 'confirmed',
                            booking_source VARCHAR(50) DEFAULT 'web',
                            cancelled_by VARCHAR(50),
                            cancellation_reason VARCHAR(255),
                            cancelled_at VARCHAR(50),
                            consultation_start_time TIMESTAMP WITHOUT TIME ZONE,
                            consultation_end_time TIMESTAMP WITHOUT TIME ZONE,
                            created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
                        );
                    """))
                    conn.execute(text("CREATE INDEX idx_booking_doctor_date_status ON patient_booking (doctor_name, date, status);"))
                    conn.execute(text("CREATE INDEX idx_booking_user ON patient_booking (user_id);"))
                conn.commit()
            tables_created.append('patient_booking')
        else:
            pb_cols = {c['name']: c for c in inspector.get_columns('patient_booking')}
            upgraded = False

            if 'gender' not in pb_cols:
                print("[MIGRATION] Adding column 'gender' to 'patient_booking'...")
                if not dry_run:
                    conn.execute(text("ALTER TABLE patient_booking ADD COLUMN gender VARCHAR(20);"))
                    conn.commit()
                columns_added.append('patient_booking.gender')
                upgraded = True

            if 'doctor_id' not in pb_cols:
                print("[MIGRATION] Adding column 'doctor_id' to 'patient_booking'...")
                if not dry_run:
                    conn.execute(text("ALTER TABLE patient_booking ADD COLUMN doctor_id INTEGER REFERENCES doctor(id);"))
                    conn.commit()
                columns_added.append('patient_booking.doctor_id')
                upgraded = True

            if 'booking_source' not in pb_cols:
                print("[MIGRATION] Adding column 'booking_source' to 'patient_booking'...")
                if not dry_run:
                    conn.execute(text("ALTER TABLE patient_booking ADD COLUMN booking_source VARCHAR(50) DEFAULT 'web';"))
                    conn.commit()
                columns_added.append('patient_booking.booking_source')
                upgraded = True

            pb_indexes = {idx['name'] for idx in inspector.get_indexes('patient_booking')}
            if 'idx_booking_doctor_date_status' not in pb_indexes:
                print("[MIGRATION] Creating index 'idx_booking_doctor_date_status'...")
                if not dry_run:
                    conn.execute(text("CREATE INDEX idx_booking_doctor_date_status ON patient_booking (doctor_name, date, status);"))
                    conn.commit()
                upgraded = True

            if upgraded:
                tables_upgraded.append('patient_booking')
            else:
                tables_verified.append('patient_booking')

        # 7. DAILY_TOKEN_ALLOCATION
        if 'daily_token_allocation' not in existing_tables:
            print("[MIGRATION] Creating table 'daily_token_allocation'...")
            if not dry_run:
                if is_sqlite:
                    conn.execute(text("""
                        CREATE TABLE daily_token_allocation (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            doctor_identifier VARCHAR(100) NOT NULL,
                            doctor_name VARCHAR(100) NOT NULL,
                            specialization VARCHAR(100) NOT NULL,
                            appointment_date VARCHAR(20) NOT NULL,
                            last_allocated_token INTEGER NOT NULL DEFAULT 0,
                            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            CONSTRAINT uq_doctor_date_token_allocation UNIQUE (doctor_identifier, appointment_date)
                        );
                    """))
                    conn.execute(text("CREATE INDEX idx_dta_doc_date ON daily_token_allocation (doctor_identifier, appointment_date);"))
                else:
                    conn.execute(text("""
                        CREATE TABLE daily_token_allocation (
                            id SERIAL PRIMARY KEY,
                            doctor_identifier VARCHAR(100) NOT NULL,
                            doctor_name VARCHAR(100) NOT NULL,
                            specialization VARCHAR(100) NOT NULL,
                            appointment_date VARCHAR(20) NOT NULL,
                            last_allocated_token INTEGER NOT NULL DEFAULT 0,
                            created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
                            updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
                            CONSTRAINT uq_doctor_date_token_allocation UNIQUE (doctor_identifier, appointment_date)
                        );
                    """))
                    conn.execute(text("CREATE INDEX idx_dta_doc_date ON daily_token_allocation (doctor_identifier, appointment_date);"))
                conn.commit()
            tables_created.append('daily_token_allocation')
        else:
            tables_verified.append('daily_token_allocation')

        # 8. OTP TABLE & VARCHAR(128) UPGRADE & SECURITY FIELDS
        if 'otp' not in existing_tables:
            print("[MIGRATION] Creating table 'otp'...")
            if not dry_run:
                if is_sqlite:
                    conn.execute(text("""
                        CREATE TABLE otp (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            email VARCHAR(120) NOT NULL,
                            otp VARCHAR(128) NOT NULL,
                            expiry DATETIME NOT NULL,
                            attempt_count INTEGER NOT NULL DEFAULT 0,
                            max_attempts INTEGER NOT NULL DEFAULT 3,
                            consumed_at DATETIME
                        );
                    """))
                    conn.execute(text("CREATE INDEX idx_otp_email ON otp (email);"))
                else:
                    conn.execute(text("""
                        CREATE TABLE otp (
                            id SERIAL PRIMARY KEY,
                            email VARCHAR(120) NOT NULL,
                            otp VARCHAR(128) NOT NULL,
                            expiry TIMESTAMP WITHOUT TIME ZONE NOT NULL,
                            attempt_count INTEGER NOT NULL DEFAULT 0,
                            max_attempts INTEGER NOT NULL DEFAULT 3,
                            consumed_at TIMESTAMP WITHOUT TIME ZONE
                        );
                    """))
                    conn.execute(text("CREATE INDEX idx_otp_email ON otp (email);"))
                conn.commit()
            tables_created.append('otp')
        else:
            otp_cols = {c['name']: c for c in inspector.get_columns('otp')}
            otp_col = otp_cols.get('otp')
            otp_upgraded = False
            if not is_sqlite and otp_col and getattr(otp_col.get('type'), 'length', 0) and otp_col.get('type').length < 128:
                print("[MIGRATION] Upgrading otp.otp column to VARCHAR(128)...")
                if not dry_run:
                    conn.execute(text("ALTER TABLE otp ALTER COLUMN otp TYPE VARCHAR(128);"))
                    conn.commit()
                columns_added.append('otp.otp_length_128')
                otp_upgraded = True

            if 'attempt_count' not in otp_cols:
                print("[MIGRATION] Adding column 'attempt_count' to table 'otp'...")
                if not dry_run:
                    conn.execute(text("ALTER TABLE otp ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0;"))
                    conn.commit()
                columns_added.append('otp.attempt_count')
                otp_upgraded = True

            if 'max_attempts' not in otp_cols:
                print("[MIGRATION] Adding column 'max_attempts' to table 'otp'...")
                if not dry_run:
                    conn.execute(text("ALTER TABLE otp ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 3;"))
                    conn.commit()
                columns_added.append('otp.max_attempts')
                otp_upgraded = True

            if 'consumed_at' not in otp_cols:
                print("[MIGRATION] Adding column 'consumed_at' to table 'otp'...")
                if not dry_run:
                    if is_sqlite:
                        conn.execute(text("ALTER TABLE otp ADD COLUMN consumed_at DATETIME;"))
                    else:
                        conn.execute(text("ALTER TABLE otp ADD COLUMN consumed_at TIMESTAMP WITHOUT TIME ZONE;"))
                    conn.commit()
                columns_added.append('otp.consumed_at')
                otp_upgraded = True

            if otp_upgraded:
                tables_upgraded.append('otp')
            else:
                tables_verified.append('otp')

        # 9. APP_SETTINGS TABLE
        if 'app_settings' not in existing_tables:
            print("[MIGRATION] Creating table 'app_settings'...")
            if not dry_run:
                if is_sqlite:
                    conn.execute(text("""
                        CREATE TABLE app_settings (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            key VARCHAR(50) UNIQUE,
                            value TEXT
                        );
                    """))
                else:
                    conn.execute(text("""
                        CREATE TABLE app_settings (
                            id SERIAL PRIMARY KEY,
                            key VARCHAR(50) UNIQUE,
                            value TEXT
                        );
                    """))
                conn.commit()
            tables_created.append('app_settings')
        else:
            tables_verified.append('app_settings')

        # 10. ADMIN_OTP_CHALLENGE TABLE
        if 'admin_otp_challenge' not in existing_tables:
            print("[MIGRATION] Creating table 'admin_otp_challenge'...")
            if not dry_run:
                if is_sqlite:
                    conn.execute(text("""
                        CREATE TABLE admin_otp_challenge (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            challenge_id_hash VARCHAR(64) NOT NULL UNIQUE,
                            admin_email VARCHAR(120) NOT NULL,
                            purpose VARCHAR(32) NOT NULL,
                            otp_hash VARCHAR(128) NOT NULL,
                            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            expires_at DATETIME NOT NULL,
                            attempt_count INTEGER NOT NULL DEFAULT 0,
                            max_attempts INTEGER NOT NULL DEFAULT 5,
                            consumed_at DATETIME,
                            ip_address VARCHAR(45)
                        );
                    """))
                    conn.execute(text("CREATE INDEX idx_admin_otp_hash ON admin_otp_challenge (challenge_id_hash);"))
                    conn.execute(text("CREATE INDEX idx_admin_otp_email ON admin_otp_challenge (admin_email);"))
                else:
                    conn.execute(text("""
                        CREATE TABLE admin_otp_challenge (
                            id SERIAL PRIMARY KEY,
                            challenge_id_hash VARCHAR(64) NOT NULL UNIQUE,
                            admin_email VARCHAR(120) NOT NULL,
                            purpose VARCHAR(32) NOT NULL,
                            otp_hash VARCHAR(128) NOT NULL,
                            created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
                            expires_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
                            attempt_count INTEGER NOT NULL DEFAULT 0,
                            max_attempts INTEGER NOT NULL DEFAULT 5,
                            consumed_at TIMESTAMP WITHOUT TIME ZONE,
                            ip_address VARCHAR(45)
                        );
                    """))
                    conn.execute(text("CREATE INDEX idx_admin_otp_hash ON admin_otp_challenge (challenge_id_hash);"))
                    conn.execute(text("CREATE INDEX idx_admin_otp_email ON admin_otp_challenge (admin_email);"))
                conn.commit()
            tables_created.append('admin_otp_challenge')
        else:
            tables_verified.append('admin_otp_challenge')

        # 11. BOOKING_SYNC_OUTBOX TABLE
        if 'booking_sync_outbox' not in existing_tables:
            print("[MIGRATION] Creating table 'booking_sync_outbox'...")
            if not dry_run:
                if is_sqlite:
                    conn.execute(text("""
                        CREATE TABLE booking_sync_outbox (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            event_type VARCHAR(50) NOT NULL,
                            booking_id INTEGER NOT NULL REFERENCES patient_booking(id) ON DELETE RESTRICT,
                            idempotency_key VARCHAR(100) NOT NULL UNIQUE,
                            payload TEXT NOT NULL,
                            status VARCHAR(20) NOT NULL DEFAULT 'pending',
                            attempts INTEGER NOT NULL DEFAULT 0,
                            max_attempts INTEGER NOT NULL DEFAULT 5,
                            next_retry_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            locked_at DATETIME,
                            locked_by VARCHAR(100),
                            completed_at DATETIME,
                            last_error TEXT,
                            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                        );
                    """))
                    conn.execute(text("CREATE INDEX idx_outbox_status ON booking_sync_outbox (status);"))
                    conn.execute(text("CREATE INDEX idx_outbox_key ON booking_sync_outbox (idempotency_key);"))
                else:
                    conn.execute(text("""
                        CREATE TABLE booking_sync_outbox (
                            id SERIAL PRIMARY KEY,
                            event_type VARCHAR(50) NOT NULL,
                            booking_id INTEGER NOT NULL REFERENCES patient_booking(id) ON DELETE RESTRICT,
                            idempotency_key VARCHAR(100) NOT NULL UNIQUE,
                            payload TEXT NOT NULL,
                            status VARCHAR(20) NOT NULL DEFAULT 'pending',
                            attempts INTEGER NOT NULL DEFAULT 0,
                            max_attempts INTEGER NOT NULL DEFAULT 5,
                            next_retry_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
                            locked_at TIMESTAMP WITHOUT TIME ZONE,
                            locked_by VARCHAR(100),
                            completed_at TIMESTAMP WITHOUT TIME ZONE,
                            last_error TEXT,
                            created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
                            updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
                        );
                    """))
                    conn.execute(text("CREATE INDEX idx_outbox_status ON booking_sync_outbox (status);"))
                    conn.execute(text("CREATE INDEX idx_outbox_key ON booking_sync_outbox (idempotency_key);"))
                conn.commit()
            tables_created.append('booking_sync_outbox')
        else:
            if not dry_run:
                if not is_sqlite:
                    conn.execute(text("UPDATE booking_sync_outbox SET status = 'pending' WHERE status IS NULL;"))
                    conn.execute(text("ALTER TABLE booking_sync_outbox ALTER COLUMN status SET NOT NULL;"))
                    conn.execute(text("""
                        DO $$
                        DECLARE
                            r RECORD;
                        BEGIN
                            FOR r IN (
                                SELECT tc.constraint_name
                                FROM information_schema.table_constraints tc
                                JOIN information_schema.key_column_usage kcu
                                  ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
                                JOIN information_schema.referential_constraints rc
                                  ON tc.constraint_name = rc.constraint_name AND tc.table_schema = rc.constraint_schema
                                WHERE tc.table_name = 'booking_sync_outbox'
                                  AND kcu.column_name = 'booking_id'
                                  AND tc.constraint_type = 'FOREIGN KEY'
                                  AND rc.delete_rule != 'RESTRICT'
                            ) LOOP
                                EXECUTE 'ALTER TABLE booking_sync_outbox DROP CONSTRAINT ' || quote_ident(r.constraint_name);
                                EXECUTE 'ALTER TABLE booking_sync_outbox ADD CONSTRAINT booking_sync_outbox_booking_id_fkey FOREIGN KEY (booking_id) REFERENCES patient_booking(id) ON DELETE RESTRICT';
                            END LOOP;
                        END $$;
                    """))
                else:
                    import sqlite3
                    sqlite_ver = sqlite3.sqlite_version
                    print(f"[MIGRATION] SQLite runtime version {sqlite_ver} verified. Checking constraints (ALTER COLUMN SET NOT NULL unsupported in SQLite syntax).")
                    conn.execute(text("UPDATE booking_sync_outbox SET status = 'pending' WHERE status IS NULL;"))
                    
                    # Detect status nullability
                    table_info = conn.execute(text("PRAGMA table_info(booking_sync_outbox);")).fetchall()
                    status_col_info = next((r for r in table_info if str(r[1]).lower() == 'status'), None)
                    status_is_nullable = bool(status_col_info and status_col_info[3] == 0)

                    # Detect FK delete action
                    fk_rows = conn.execute(text("PRAGMA foreign_key_list(booking_sync_outbox);")).fetchall()
                    needs_fk_upgrade = any(len(r) > 6 and str(r[2]).lower() == 'patient_booking' and str(r[6]).upper() != 'RESTRICT' for r in fk_rows)

                    if status_is_nullable or needs_fk_upgrade:
                        reasons = []
                        if status_is_nullable:
                            reasons.append("status is nullable (requires NOT NULL)")
                        if needs_fk_upgrade:
                            reasons.append("booking_id foreign key ondelete is not RESTRICT (requires upgrade to RESTRICT)")
                        print(f"[MIGRATION] Upgrading SQLite booking_sync_outbox ({', '.join(reasons)}) via safe table rebuild...")

                        # 1. Capture existing custom indexes
                        existing_indexes = conn.execute(text(
                            "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'booking_sync_outbox' AND sql IS NOT NULL;"
                        )).fetchall()

                        # 2. Capture existing custom triggers
                        existing_triggers = conn.execute(text(
                            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'booking_sync_outbox' AND sql IS NOT NULL;"
                        )).fetchall()

                        # 3. Detect existing columns to preserve extra columns
                        curr_cols_info = {r[1]: {'type': r[2], 'notnull': r[3]} for r in table_info}
                        curr_cols = list(curr_cols_info.keys())
                        canonical_cols = [
                            'id', 'event_type', 'booking_id', 'idempotency_key', 'payload', 'status',
                            'attempts', 'max_attempts', 'next_retry_at', 'locked_at', 'locked_by',
                            'completed_at', 'last_error', 'created_at', 'updated_at'
                        ]
                        extra_cols = [c for c in curr_cols if c not in canonical_cols]

                        conn.execute(text("DROP TABLE IF EXISTS booking_sync_outbox__upgrade_tmp;"))
                        conn.execute(text("""
                            CREATE TABLE booking_sync_outbox__upgrade_tmp (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                event_type VARCHAR(50) NOT NULL,
                                booking_id INTEGER NOT NULL REFERENCES patient_booking(id) ON DELETE RESTRICT,
                                idempotency_key VARCHAR(100) NOT NULL UNIQUE,
                                payload TEXT NOT NULL,
                                status VARCHAR(20) NOT NULL DEFAULT 'pending',
                                attempts INTEGER NOT NULL DEFAULT 0,
                                max_attempts INTEGER NOT NULL DEFAULT 5,
                                next_retry_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                                locked_at DATETIME,
                                locked_by VARCHAR(100),
                                completed_at DATETIME,
                                last_error TEXT,
                                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                            );
                        """))

                        for extra_c in extra_cols:
                            col_type = str(curr_cols_info[extra_c].get('type', 'TEXT'))
                            conn.execute(text(f"ALTER TABLE booking_sync_outbox__upgrade_tmp ADD COLUMN {extra_c} {col_type};"))

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
                                if c == 'attempts':
                                    target_cols.append(c); source_exprs.append("0")
                                elif c == 'max_attempts':
                                    target_cols.append(c); source_exprs.append("5")
                                elif c in ('created_at', 'updated_at', 'next_retry_at'):
                                    target_cols.append(c); source_exprs.append("CURRENT_TIMESTAMP")
                                elif c == 'status':
                                    target_cols.append(c); source_exprs.append("'pending'")
                                elif c == 'payload':
                                    target_cols.append(c); source_exprs.append("'{}'")

                        for extra_c in extra_cols:
                            target_cols.append(extra_c)
                            source_exprs.append(extra_c)

                        target_cols_str = ", ".join(target_cols)
                        source_exprs_str = ", ".join(source_exprs)

                        conn.execute(text(f"INSERT INTO booking_sync_outbox__upgrade_tmp ({target_cols_str}) SELECT {source_exprs_str} FROM booking_sync_outbox;"))
                        conn.execute(text("DROP TABLE booking_sync_outbox;"))
                        conn.execute(text("ALTER TABLE booking_sync_outbox__upgrade_tmp RENAME TO booking_sync_outbox;"))

                        # Recreate canonical indexes
                        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_outbox_status ON booking_sync_outbox (status);"))
                        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_outbox_key ON booking_sync_outbox (idempotency_key);"))

                        # Recreate captured custom indexes
                        curr_indexes = {row[0] for row in conn.execute(text(
                            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'booking_sync_outbox';"
                        )).fetchall()}
                        for idx_name, idx_sql in existing_indexes:
                            if idx_name not in curr_indexes and idx_sql:
                                conn.execute(text(idx_sql))

                        # Recreate captured custom triggers (excluding trg_prevent_retired_outbox_reactivation which is installed right below)
                        curr_triggers = {row[0] for row in conn.execute(text(
                            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'booking_sync_outbox';"
                        )).fetchall()}
                        for trg_name, trg_sql in existing_triggers:
                            if trg_name not in curr_triggers and trg_name != 'trg_prevent_retired_outbox_reactivation' and trg_sql:
                                conn.execute(text(trg_sql))

                        # Verify foreign key integrity
                        fk_violations = conn.execute(text("PRAGMA foreign_key_check(booking_sync_outbox);")).fetchall()
                        if fk_violations:
                            raise RuntimeError(f"Foreign key constraint violation detected after rebuilding 'booking_sync_outbox': {fk_violations}")
                        print(f"[MIGRATION] SQLite booking_sync_outbox rebuilt successfully (preserved {len(extra_cols)} extra legacy columns, custom indexes, custom triggers, 0 foreign key violations).")
                conn.commit()
            tables_verified.append('booking_sync_outbox')

        # Ensure database trigger on booking_sync_outbox protecting retired status
        if not dry_run:
            if is_sqlite:
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
            else:
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
            conn.commit()

        # 12. SHEET_SYNC_LOCK TABLE
        if 'sheet_sync_lock' not in existing_tables:
            print("[MIGRATION] Creating table 'sheet_sync_lock'...")
            if not dry_run:
                if is_sqlite:
                    conn.execute(text("""
                        CREATE TABLE sheet_sync_lock (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            lock_key VARCHAR(150) NOT NULL UNIQUE,
                            locked_by VARCHAR(100),
                            locked_at DATETIME,
                            expires_at DATETIME,
                            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                        );
                    """))
                    conn.execute(text("CREATE INDEX idx_sheet_lock_key ON sheet_sync_lock (lock_key);"))
                else:
                    conn.execute(text("""
                        CREATE TABLE sheet_sync_lock (
                            id SERIAL PRIMARY KEY,
                            lock_key VARCHAR(150) NOT NULL UNIQUE,
                            locked_by VARCHAR(100),
                            locked_at TIMESTAMP WITHOUT TIME ZONE,
                            expires_at TIMESTAMP WITHOUT TIME ZONE,
                            created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
                            updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
                        );
                    """))
                    conn.execute(text("CREATE INDEX idx_sheet_lock_key ON sheet_sync_lock (lock_key);"))
                conn.commit()
            tables_created.append('sheet_sync_lock')
        else:
            tables_verified.append('sheet_sync_lock')

        # 14. TICKER_MESSAGE TABLE
        if 'ticker_message' not in existing_tables:
            print("[MIGRATION] Creating table 'ticker_message'...")
            if not dry_run:
                if is_sqlite:
                    conn.execute(text("""
                        CREATE TABLE ticker_message (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            content VARCHAR(500) NOT NULL,
                            is_active BOOLEAN NOT NULL DEFAULT 1,
                            start_time DATETIME,
                            end_time DATETIME,
                            color_dot VARCHAR(20) DEFAULT 'yellow',
                            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                        );
                    """))
                else:
                    conn.execute(text("""
                        CREATE TABLE ticker_message (
                            id SERIAL PRIMARY KEY,
                            content VARCHAR(500) NOT NULL,
                            is_active BOOLEAN NOT NULL DEFAULT TRUE,
                            start_time TIMESTAMP WITHOUT TIME ZONE,
                            end_time TIMESTAMP WITHOUT TIME ZONE,
                            color_dot VARCHAR(20) DEFAULT 'yellow',
                            created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
                        );
                    """))
                conn.commit()
            tables_created.append('ticker_message')
        else:
            tables_verified.append('ticker_message')

        # 15. PRESCRIPTION TABLE
        if 'prescription' not in existing_tables:
            print("[MIGRATION] Creating table 'prescription'...")
            if not dry_run:
                if is_sqlite:
                    conn.execute(text("""
                        CREATE TABLE prescription (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            user_id INTEGER NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
                            patient_name VARCHAR(100),
                            consultation_date VARCHAR(20),
                            doctor_name VARCHAR(100),
                            file_path VARCHAR(255),
                            text_content TEXT,
                            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                        );
                    """))
                else:
                    conn.execute(text("""
                        CREATE TABLE prescription (
                            id SERIAL PRIMARY KEY,
                            user_id INTEGER NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
                            patient_name VARCHAR(100),
                            consultation_date VARCHAR(20),
                            doctor_name VARCHAR(100),
                            file_path VARCHAR(255),
                            text_content TEXT,
                            created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
                        );
                    """))
                conn.commit()
            tables_created.append('prescription')
        else:
            tables_verified.append('prescription')

        # 16. PUSH_SUBSCRIPTION TABLE
        if 'push_subscription' not in existing_tables:
            print("[MIGRATION] Creating table 'push_subscription'...")
            if not dry_run:
                if is_sqlite:
                    conn.execute(text("""
                        CREATE TABLE push_subscription (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            user_id INTEGER REFERENCES "user"(id) ON DELETE CASCADE,
                            patient_name VARCHAR(100),
                            doctor_name VARCHAR(100),
                            endpoint VARCHAR(500) NOT NULL UNIQUE,
                            p256dh VARCHAR(255) NOT NULL,
                            auth VARCHAR(255) NOT NULL,
                            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                        );
                    """))
                else:
                    conn.execute(text("""
                        CREATE TABLE push_subscription (
                            id SERIAL PRIMARY KEY,
                            user_id INTEGER REFERENCES "user"(id) ON DELETE CASCADE,
                            patient_name VARCHAR(100),
                            doctor_name VARCHAR(100),
                            endpoint VARCHAR(500) NOT NULL UNIQUE,
                            p256dh VARCHAR(255) NOT NULL,
                            auth VARCHAR(255) NOT NULL,
                            created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
                        );
                    """))
                conn.commit()
            tables_created.append('push_subscription')
        else:
            tables_verified.append('push_subscription')

        # 17. SAVED_PATIENT TABLE
        if 'saved_patient' not in existing_tables:
            print("[MIGRATION] Creating table 'saved_patient'...")
            if not dry_run:
                if is_sqlite:
                    conn.execute(text("""
                        CREATE TABLE saved_patient (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            user_id INTEGER NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
                            name VARCHAR(100) NOT NULL,
                            age VARCHAR(10) NOT NULL,
                            gender VARCHAR(20) NOT NULL,
                            phone_number VARCHAR(20) NOT NULL,
                            relationship VARCHAR(50) DEFAULT 'Self',
                            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                        );
                    """))
                else:
                    conn.execute(text("""
                        CREATE TABLE saved_patient (
                            id SERIAL PRIMARY KEY,
                            user_id INTEGER NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
                            name VARCHAR(100) NOT NULL,
                            age VARCHAR(10) NOT NULL,
                            gender VARCHAR(20) NOT NULL,
                            phone_number VARCHAR(20) NOT NULL,
                            relationship VARCHAR(50) DEFAULT 'Self',
                            created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
                        );
                    """))
                conn.commit()
            tables_created.append('saved_patient')
        else:
            tables_verified.append('saved_patient')

        # 18. DOCTOR_REFERRAL TABLE
        if 'doctor_referral' not in existing_tables:
            print("[MIGRATION] Creating table 'doctor_referral'...")
            if not dry_run:
                if is_sqlite:
                    conn.execute(text("""
                        CREATE TABLE doctor_referral (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            user_id INTEGER NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
                            from_doctor VARCHAR(100),
                            to_specialization VARCHAR(100),
                            notes TEXT,
                            status VARCHAR(50) DEFAULT 'pending',
                            patient_name VARCHAR(100),
                            booking_id INTEGER REFERENCES patient_booking(id) ON DELETE SET NULL,
                            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                        );
                    """))
                else:
                    conn.execute(text("""
                        CREATE TABLE doctor_referral (
                            id SERIAL PRIMARY KEY,
                            user_id INTEGER NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
                            from_doctor VARCHAR(100),
                            to_specialization VARCHAR(100),
                            notes TEXT,
                            status VARCHAR(50) DEFAULT 'pending',
                            patient_name VARCHAR(100),
                            booking_id INTEGER REFERENCES patient_booking(id) ON DELETE SET NULL,
                            created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
                        );
                    """))
                conn.commit()
            tables_created.append('doctor_referral')
        else:
            tables_verified.append('doctor_referral')

        # 19. DOCTOR_INVITATION TABLE
        if 'doctor_invitation' not in existing_tables:
            print("[MIGRATION] Creating table 'doctor_invitation'...")
            if not dry_run:
                if is_sqlite:
                    conn.execute(text("""
                        CREATE TABLE doctor_invitation (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            doctor_id INTEGER NOT NULL REFERENCES doctor_session(id) ON DELETE CASCADE,
                            email VARCHAR(120) NOT NULL,
                            token_hash VARCHAR(64) NOT NULL UNIQUE,
                            expires_at DATETIME NOT NULL,
                            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            is_used BOOLEAN NOT NULL DEFAULT 0,
                            used_at DATETIME
                        );
                    """))
                    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_invitation_email ON doctor_invitation (email);"))
                    conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS idx_invitation_token ON doctor_invitation (token_hash);"))
                else:
                    conn.execute(text("""
                        CREATE TABLE doctor_invitation (
                            id SERIAL PRIMARY KEY,
                            doctor_id INTEGER NOT NULL REFERENCES doctor_session(id) ON DELETE CASCADE,
                            email VARCHAR(120) NOT NULL,
                            token_hash VARCHAR(64) NOT NULL UNIQUE,
                            expires_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
                            created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
                            is_used BOOLEAN NOT NULL DEFAULT FALSE,
                            used_at TIMESTAMP WITHOUT TIME ZONE
                        );
                    """))
                    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_invitation_email ON doctor_invitation (email);"))
                    conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS idx_invitation_token ON doctor_invitation (token_hash);"))
                conn.commit()
            tables_created.append('doctor_invitation')
        else:
            tables_verified.append('doctor_invitation')

    # Post-migration Comprehensive Verification
    inspector_post = inspect(engine)
    final_tables = set(inspector_post.get_table_names())

    # sheet_sync_lock is decommissioned and strictly NOT required as an active application table
    required_tables = {
        'user', 'doctor_session', 'patient_booking', 'specialization',
        'doctor', 'doctor_schedule', 'doctor_leave', 'clinic_holiday',
        'daily_token_allocation', 'otp', 'app_settings', 'admin_otp_challenge',
        'booking_sync_outbox', 'ticker_message', 'prescription', 'push_subscription',
        'saved_patient', 'doctor_referral', 'doctor_invitation'
    }
    missing = required_tables - final_tables
    if missing and not dry_run:
        raise RuntimeError(f"Migration verification failed: Missing required tables: {missing}")

    # Comprehensive verification of columns, primary keys, foreign keys, unique constraints, and indexes
    if not dry_run:
        # Verify required columns on key models
        pb_cols = {c['name'] for c in inspector_post.get_columns('patient_booking')}
        assert {'status', 'consultation_start_time', 'consultation_end_time', 'doctor_id', 'booking_source'}.issubset(pb_cols), f"Missing patient_booking cols: {pb_cols}"
        
        otp_cols = {c['name'] for c in inspector_post.get_columns('otp')}
        assert {'attempt_count', 'max_attempts', 'consumed_at', 'otp'}.issubset(otp_cols), f"Missing otp cols: {otp_cols}"
        
        aoc_cols = {c['name'] for c in inspector_post.get_columns('admin_otp_challenge')}
        assert {'challenge_id_hash', 'otp_hash', 'purpose', 'admin_email', 'attempt_count', 'max_attempts'}.issubset(aoc_cols), f"Missing admin_otp_challenge cols: {aoc_cols}"
        
        outbox_cols = {c['name'] for c in inspector_post.get_columns('booking_sync_outbox')}
        assert {'idempotency_key', 'status', 'attempts', 'completed_at', 'last_error'}.issubset(outbox_cols), f"Missing booking_sync_outbox cols: {outbox_cols}"

        token_cols = {c['name'] for c in inspector_post.get_columns('daily_token_allocation')}
        assert {'doctor_identifier', 'appointment_date', 'last_allocated_token'}.issubset(token_cols), f"Missing daily_token_allocation cols: {token_cols}"

        # Verify Primary Keys exist for all required tables
        for tbl in required_tables:
            pk = inspector_post.get_pk_constraint(tbl)
            assert pk and pk.get('constrained_columns'), f"Table '{tbl}' missing primary key constraint!"

        # Verify Foreign Keys exist where mandated with expected ondelete actions
        if not is_sqlite:
            expected_fks = [
                ('patient_booking', 'user_id', 'user', 'id', 'CASCADE'),
                ('patient_booking', 'doctor_id', 'doctor', 'id', 'SET NULL'),
                ('doctor_schedule', 'doctor_id', 'doctor', 'id', 'CASCADE'),
                ('doctor_leave', 'doctor_id', 'doctor', 'id', 'CASCADE'),
                ('booking_sync_outbox', 'booking_id', 'patient_booking', 'id', 'RESTRICT'),
            ]
            for tbl, col, ref_tbl, ref_col, ondel in expected_fks:
                fks = inspector_post.get_foreign_keys(tbl)
                matching = [
                    fk for fk in fks
                    if col in fk.get('constrained_columns', [])
                    and fk.get('referred_table') == ref_tbl
                    and ref_col in fk.get('referred_columns', [])
                ]
                assert len(matching) > 0, f"Table '{tbl}' missing foreign key on '{col}' -> '{ref_tbl}.{ref_col}'"
                fk_opts = matching[0].get('options', {}) or {}
                actual_ondel = (fk_opts.get('ondelete') or '').upper()
                if actual_ondel:
                    assert actual_ondel == ondel, f"Table '{tbl}' FK on '{col}' ondelete mismatch: expected '{ondel}', got '{actual_ondel}'"
        else:
            # On SQLite, assert booking_sync_outbox RESTRICT foreign key
            outbox_fks = inspector_post.get_foreign_keys('booking_sync_outbox')
            matching = [
                fk for fk in outbox_fks
                if 'booking_id' in fk.get('constrained_columns', [])
                and fk.get('referred_table') == 'patient_booking'
            ]
            assert len(matching) > 0, "booking_sync_outbox missing foreign key to patient_booking"
            fk_opts = matching[0].get('options', {}) or {}
            actual_ondel = (fk_opts.get('ondelete') or '').upper()
            if actual_ondel:
                assert actual_ondel == 'RESTRICT', f"booking_sync_outbox FK ondelete mismatch: expected RESTRICT, got {actual_ondel}"

        # Verify Unique Constraints / Unique Indexes
        expected_uqs = [
            ('user', ['email']),
            ('specialization', ['name']),
            ('clinic_holiday', ['date']),
            ('booking_sync_outbox', ['idempotency_key']),
            ('daily_token_allocation', ['doctor_identifier', 'appointment_date']),
            ('admin_otp_challenge', ['challenge_id_hash']),
            ('sheet_sync_lock', ['lock_key']),
        ]
        for tbl, cols in expected_uqs:
            uqs = inspector_post.get_unique_constraints(tbl)
            idxs = inspector_post.get_indexes(tbl)
            has_uq = any(uq.get('column_names') == cols for uq in uqs)
            has_unique_idx = any(idx.get('column_names') == cols and idx.get('unique') for idx in idxs)
            has_sqlite_auto_uq = False
            if not (has_uq or has_unique_idx) and is_sqlite:
                with engine.connect() as uq_conn:
                    for il in uq_conn.execute(text(f"PRAGMA index_list('{tbl}');")).fetchall():
                        if len(il) > 2 and il[2] == 1:
                            info = uq_conn.execute(text(f"PRAGMA index_info('{il[1]}');")).fetchall()
                            if [r[2] for r in info] == cols:
                                has_sqlite_auto_uq = True
                                break
            assert has_uq or has_unique_idx or has_sqlite_auto_uq, f"Table '{tbl}' missing unique constraint or unique index on {cols}"

        # Verify booking_sync_outbox.status is NOT NULL
        outbox_col_map = {c['name']: c for c in inspector_post.get_columns('booking_sync_outbox')}
        assert 'status' in outbox_col_map, "booking_sync_outbox missing 'status' column"
        assert outbox_col_map['status']['nullable'] is False, "booking_sync_outbox.status must be NOT NULL"

        # Verify composite index on patient_booking (doctor_name, date, status)
        pb_idxs = inspector_post.get_indexes('patient_booking')
        has_comp_idx = any(idx.get('column_names') == ['doctor_name', 'date', 'status'] for idx in pb_idxs)
        assert has_comp_idx, "patient_booking missing composite index on (doctor_name, date, status)"

        # Verify Indexes exist on critical query pathways
        for tbl in ['patient_booking', 'booking_sync_outbox', 'admin_otp_challenge']:
            idxes = inspector_post.get_indexes(tbl)
            assert len(idxes) > 0, f"Table '{tbl}' has no indexes configured!"

        # Verify database trigger on booking_sync_outbox protecting retired status
        if 'booking_sync_outbox' in final_tables:
            with engine.connect() as check_conn:
                if is_sqlite:
                    trg_check = check_conn.execute(text(
                        "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name = 'trg_prevent_retired_outbox_reactivation'"
                    )).fetchall()
                    assert len(trg_check) > 0, "SQLite trigger 'trg_prevent_retired_outbox_reactivation' missing on booking_sync_outbox!"
                else:
                    trg_check = check_conn.execute(text("""
                        SELECT trigger_name FROM information_schema.triggers
                        WHERE event_object_table = 'booking_sync_outbox'
                          AND trigger_name = 'trg_prevent_retired_outbox_reactivation'
                    """)).fetchall()
                    assert len(trg_check) > 0, "PostgreSQL trigger 'trg_prevent_retired_outbox_reactivation' missing on booking_sync_outbox!"
            print("[MIGRATION VERIFICATION] Database trigger 'trg_prevent_retired_outbox_reactivation' verified on booking_sync_outbox.")

        print("[MIGRATION VERIFICATION] All required columns, primary keys, unique constraints, indexes, and triggers verified.")

    print("\n======================================================================")
    print("MIGRATION EXECUTION REPORT:")
    print(f"  Status: {'DRY RUN SUCCESS' if dry_run else 'SUCCESS'}")
    print(f"  Database Dialect: {dialect_name}")
    print(f"  Tables Created: {tables_created}")
    print(f"  Tables Upgraded: {tables_upgraded}")
    print(f"  Tables Verified: {tables_verified}")
    print(f"  Columns Added: {columns_added}")
    print(f"  Final Table Count: {len(final_tables)}")
    print("======================================================================\n")

    return {
        "status": "SUCCESS",
        "dialect": dialect_name,
        "tables_created": tables_created,
        "tables_upgraded": tables_upgraded,
        "tables_verified": tables_verified,
        "columns_added": columns_added,
        "total_tables": len(final_tables)
    }


if __name__ == '__main__':
    dry = '--dry-run' in sys.argv
    target_url = None
    for i, arg in enumerate(sys.argv):
        if arg == '--url' and i + 1 < len(sys.argv):
            target_url = sys.argv[i + 1]
    run_migration(db_url=target_url, dry_run=dry)

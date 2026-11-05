"""
scripts/init_phase1c_baseline_postgres.py
=========================================
Initializes the clean Phase 1C baseline schema and synthetic fixtures
on the disposable Neon PostgreSQL test database.

Strict Safety Rules:
  - Validates target URI using database_safety.assert_database_safety.
  - Connects strictly to POSTGRES_TEST_URL.
  - Never touches production Neon.
  - Does NOT create Phase 2B-4B tables (daily_token_allocation, booking_sync_outbox, whatsapp_*),
    ensuring migrations test genuine incremental schema evolution.
  - Uses 100% synthetic test data (zero real patient PII).
"""

import os
import sys
from sqlalchemy import create_engine, text, inspect
from dotenv import load_dotenv

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

load_dotenv()
from database_safety import assert_database_safety


PHASE_1C_DDL_STATEMENTS = [
    # 1. USER
    """
    CREATE TABLE IF NOT EXISTS "user" (
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
    """,
    "CREATE INDEX IF NOT EXISTS idx_user_email ON \"user\" (email);",
    "CREATE INDEX IF NOT EXISTS idx_user_phone ON \"user\" (phone_number);",

    # 2. SPECIALIZATION
    """
    CREATE TABLE IF NOT EXISTS specialization (
        id SERIAL PRIMARY KEY,
        name VARCHAR(100) NOT NULL UNIQUE,
        description VARCHAR(255),
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_spec_name ON specialization (name);",

    # 3. DOCTOR
    """
    CREATE TABLE IF NOT EXISTS doctor (
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
    """,
    "CREATE INDEX IF NOT EXISTS idx_doc_name ON doctor (name);",
    "CREATE INDEX IF NOT EXISTS idx_doc_spec ON doctor (specialization);",
    "CREATE INDEX IF NOT EXISTS idx_doc_active ON doctor (is_active);",

    # 4. DOCTOR_SCHEDULE
    """
    CREATE TABLE IF NOT EXISTS doctor_schedule (
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
    """,
    "CREATE INDEX IF NOT EXISTS idx_sched_lookup ON doctor_schedule (doctor_id, day_of_week);",

    # 5. DOCTOR_LEAVE
    """
    CREATE TABLE IF NOT EXISTS doctor_leave (
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
    """,
    "CREATE INDEX IF NOT EXISTS idx_leave_doc_date ON doctor_leave (doctor_name, date, status);",

    # 6. CLINIC_HOLIDAY
    """
    CREATE TABLE IF NOT EXISTS clinic_holiday (
        id SERIAL PRIMARY KEY,
        date VARCHAR(20) NOT NULL UNIQUE,
        reason VARCHAR(255),
        is_recurring BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_holiday_date ON clinic_holiday (date);",

    # 7. DOCTOR_SESSION
    """
    CREATE TABLE IF NOT EXISTS doctor_session (
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
        created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
    );
    """,

    # 8. PATIENT_BOOKING (Phase 1C canonical schema)
    """
    CREATE TABLE IF NOT EXISTS patient_booking (
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
    """,
    "CREATE INDEX IF NOT EXISTS idx_booking_doctor_date_status ON patient_booking (doctor_name, date, status);",
    "CREATE INDEX IF NOT EXISTS idx_booking_user ON patient_booking (user_id);",

    # 9. OTP
    """
    CREATE TABLE IF NOT EXISTS otp (
        id SERIAL PRIMARY KEY,
        email VARCHAR(120) NOT NULL,
        otp VARCHAR(128) NOT NULL,
        expiry TIMESTAMP WITHOUT TIME ZONE NOT NULL,
        attempt_count INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 3,
        consumed_at TIMESTAMP WITHOUT TIME ZONE
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_otp_email ON otp (email);",

    # 10. APP_SETTINGS
    """
    CREATE TABLE IF NOT EXISTS app_settings (
        id SERIAL PRIMARY KEY,
        key VARCHAR(50) UNIQUE,
        value TEXT
    );
    """,

    # 11. ADMIN_OTP_CHALLENGE
    """
    CREATE TABLE IF NOT EXISTS admin_otp_challenge (
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
    """,
    "CREATE INDEX IF NOT EXISTS idx_admin_otp_hash ON admin_otp_challenge (challenge_id_hash);",
    "CREATE INDEX IF NOT EXISTS idx_admin_otp_email ON admin_otp_challenge (admin_email);",

    # 12. TICKER_MESSAGE
    """
    CREATE TABLE IF NOT EXISTS ticker_message (
        id SERIAL PRIMARY KEY,
        content VARCHAR(500) NOT NULL,
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        start_time TIMESTAMP WITHOUT TIME ZONE,
        end_time TIMESTAMP WITHOUT TIME ZONE,
        color_dot VARCHAR(20) DEFAULT 'yellow',
        created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
    );
    """,

    # 13. PRESCRIPTION
    """
    CREATE TABLE IF NOT EXISTS prescription (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
        patient_name VARCHAR(100),
        consultation_date VARCHAR(20),
        doctor_name VARCHAR(100),
        file_path VARCHAR(255),
        text_content TEXT,
        created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
    );
    """,

    # 14. PUSH_SUBSCRIPTION
    """
    CREATE TABLE IF NOT EXISTS push_subscription (
        id SERIAL PRIMARY KEY,
        user_id INTEGER REFERENCES "user"(id) ON DELETE CASCADE,
        patient_name VARCHAR(100),
        doctor_name VARCHAR(100),
        endpoint VARCHAR(500) NOT NULL UNIQUE,
        p256dh VARCHAR(255) NOT NULL,
        auth VARCHAR(255) NOT NULL,
        created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
    );
    """,

    # 15. SAVED_PATIENT
    """
    CREATE TABLE IF NOT EXISTS saved_patient (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
        name VARCHAR(100) NOT NULL,
        age VARCHAR(10) NOT NULL,
        gender VARCHAR(20) NOT NULL,
        phone_number VARCHAR(20) NOT NULL,
        relationship VARCHAR(50) DEFAULT 'Self',
        created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
    );
    """,

    # 16. DOCTOR_REFERRAL
    """
    CREATE TABLE IF NOT EXISTS doctor_referral (
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
    """,

    # 17. DOCTOR_INVITATION
    """
    CREATE TABLE IF NOT EXISTS doctor_invitation (
        id SERIAL PRIMARY KEY,
        doctor_id INTEGER NOT NULL REFERENCES doctor_session(id) ON DELETE CASCADE,
        email VARCHAR(120) NOT NULL,
        token_hash VARCHAR(64) NOT NULL UNIQUE,
        expires_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
        created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
        is_used BOOLEAN NOT NULL DEFAULT FALSE,
        used_at TIMESTAMP WITHOUT TIME ZONE
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_invitation_email ON doctor_invitation (email);",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_invitation_token ON doctor_invitation (token_hash);"
]


def init_phase1c_baseline(target_url: str):
    """Initializes Phase 1C baseline schema and seeds synthetic test fixtures."""
    # 1. Enforce strict safety
    assert_database_safety(target_url)

    engine = create_engine(target_url)
    dialect = engine.dialect.name.lower()
    if 'postgres' not in dialect:
        raise RuntimeError(f"Expected PostgreSQL database engine, got '{dialect}'.")

    print(f"\n--- [BASELINE INITIALIZATION] Initializing Phase 1C Schema on PostgreSQL ---")

    # 2. Execute Phase 1C DDL
    with engine.begin() as conn:
        for stmt in PHASE_1C_DDL_STATEMENTS:
            conn.execute(text(stmt))

    # 3. Seed Synthetic Test Records (Zero real patient information)
    print("--- [SEEDING] Inserting synthetic Phase 1C test records ---")
    with engine.begin() as conn:
        # Seed test patient user
        conn.execute(text("""
            INSERT INTO "user" (id, name, email, password_hash, role, phone_number, phone_verified)
            VALUES (1, 'Synthetic Patient', 'synthetic.patient@primecare.com', 'synthetic_pw_hash', 'patient', '+919847055001', TRUE)
            ON CONFLICT (id) DO UPDATE SET phone_number = '+919847055001', phone_verified = TRUE;
        """))

        # Seed test admin user
        conn.execute(text("""
            INSERT INTO "user" (id, name, email, password_hash, role, phone_number, phone_verified)
            VALUES (2, 'Synthetic Admin', 'admin@primecare.com', 'admin_pw_hash', 'admin', '+919847055002', TRUE)
            ON CONFLICT (id) DO UPDATE SET role = 'admin';
        """))

        # Seed test doctor user
        conn.execute(text("""
            INSERT INTO "user" (id, name, email, password_hash, role, phone_number, phone_verified)
            VALUES (3, 'Dr. Sarah Jenkins', 'dr.jenkins@primecare.com', 'doc_pw_hash', 'doctor', '+919847055003', TRUE)
            ON CONFLICT (id) DO UPDATE SET role = 'doctor';
        """))

        # Seed test specialization
        conn.execute(text("""
            INSERT INTO specialization (id, name, description, is_active)
            VALUES (1, 'General Medicine', 'Primary care and general consultations', TRUE)
            ON CONFLICT (id) DO UPDATE SET name = 'General Medicine';
        """))

        # Seed test doctor
        conn.execute(text("""
            INSERT INTO doctor (id, name, specialization_id, specialization, qualification, email, phone_number, is_active, consultation_fee, max_daily_patients)
            VALUES (1, 'Dr. Sarah Jenkins', 1, 'General Medicine', 'MD General Medicine', 'dr.jenkins@primecare.com', '+919847055003', TRUE, 500.00, 25)
            ON CONFLICT (id) DO UPDATE SET max_daily_patients = 25, is_active = TRUE;
        """))

        # Seed doctor schedule
        conn.execute(text("""
            INSERT INTO doctor_schedule (id, doctor_id, day_of_week, start_time, end_time, is_available)
            VALUES (1, 1, 'Monday', '09:00', '13:00', TRUE)
            ON CONFLICT (id) DO NOTHING;
        """))

        # Seed doctor session
        conn.execute(text("""
            INSERT INTO doctor_session (id, doctor_name, specialization, session_date, email, phone_number, status, total_tokens, current_token)
            VALUES (1, 'Dr. Sarah Jenkins', 'General Medicine', '2026-11-20', 'dr.jenkins@primecare.com', '+919847055003', 'scheduled', 2, 0)
            ON CONFLICT (id) DO NOTHING;
        """))

        # Seed synthetic legacy bookings with tokens in patient_booking (to test migrate_phase2b preflight audit & backfill!)
        conn.execute(text("""
            INSERT INTO patient_booking (id, user_id, doctor_id, doctor_name, specialization, date, time, token, status, patient_name, phone_number)
            VALUES (1, 1, 1, 'Dr. Sarah Jenkins', 'General Medicine', '2026-11-20', '09:00', 1, 'confirmed', 'Synthetic Patient A', '+919847055001')
            ON CONFLICT (id) DO NOTHING;
        """))
        conn.execute(text("""
            INSERT INTO patient_booking (id, user_id, doctor_id, doctor_name, specialization, date, time, token, status, patient_name, phone_number)
            VALUES (2, 1, 1, 'Dr. Sarah Jenkins', 'General Medicine', '2026-11-20', '09:15', 2, 'confirmed', 'Synthetic Patient B', '+919847055002')
            ON CONFLICT (id) DO NOTHING;
        """))

        # Set Postgres sequences so subsequent inserts do not collide
        conn.execute(text("SELECT setval(pg_get_serial_sequence('\"user\"', 'id'), coalesce(max(id), 1)) FROM \"user\";"))
        conn.execute(text("SELECT setval(pg_get_serial_sequence('specialization', 'id'), coalesce(max(id), 1)) FROM specialization;"))
        conn.execute(text("SELECT setval(pg_get_serial_sequence('doctor', 'id'), coalesce(max(id), 1)) FROM doctor;"))
        conn.execute(text("SELECT setval(pg_get_serial_sequence('doctor_schedule', 'id'), coalesce(max(id), 1)) FROM doctor_schedule;"))
        conn.execute(text("SELECT setval(pg_get_serial_sequence('doctor_session', 'id'), coalesce(max(id), 1)) FROM doctor_session;"))
        conn.execute(text("SELECT setval(pg_get_serial_sequence('patient_booking', 'id'), coalesce(max(id), 1)) FROM patient_booking;"))

    # 4. Verify Baseline Tables
    inspector = inspect(engine)
    current_tables = sorted(inspector.get_table_names())
    print(f"\n--- [VERIFICATION] Phase 1C Baseline Tables on PostgreSQL ({len(current_tables)} tables) ---")
    for t in current_tables:
        print(f"  • {t}")

    # Assert migration tables DO NOT exist yet
    migration_tables = [
        'daily_token_allocation', 'booking_sync_outbox', 'sheet_sync_lock',
        'whatsapp_conversation', 'whatsapp_message', 'whatsapp_webhook_event',
        'whatsapp_booking_session'
    ]
    for mt in migration_tables:
        if mt in current_tables:
            raise RuntimeError(f"INTEGRITY VIOLATION: Migration table '{mt}' already exists before migration execution!")

    print("[SUCCESS] Phase 1C baseline schema initialized and verified successfully.")
    return current_tables


if __name__ == '__main__':
    target = os.environ.get('POSTGRES_TEST_URL')
    if not target:
        raise RuntimeError("POSTGRES_TEST_URL is not set!")
    init_phase1c_baseline(target)


import os
import sys
from dotenv import load_dotenv

load_dotenv('.env')

db_url = os.environ.get('DATABASE_URL')
if not db_url:
    print("No DATABASE_URL set. Skipping PostgreSQL migration.")
    sys.exit(0)

# Replace postgres:// with postgresql:// if needed
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

import psycopg2

def migrate_postgres():
    print(f"Connecting to PostgreSQL...")
    conn = psycopg2.connect(db_url)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        print("1. Updating 'user' table...")
        cur.execute('ALTER TABLE "user" ALTER COLUMN email DROP NOT NULL;')
        cur.execute('ALTER TABLE "user" ADD COLUMN IF NOT EXISTS phone_number VARCHAR(20) UNIQUE;')
        cur.execute('ALTER TABLE "user" ADD COLUMN IF NOT EXISTS phone_verified BOOLEAN DEFAULT FALSE;')
        cur.execute('ALTER TABLE "user" ADD COLUMN IF NOT EXISTS phone_verified_at TIMESTAMP;')
        cur.execute('ALTER TABLE "user" ADD COLUMN IF NOT EXISTS email_verified BOOLEAN DEFAULT FALSE;')
        cur.execute('CREATE INDEX IF NOT EXISTS ix_user_phone_number ON "user" (phone_number);')

        print("2. Updating 'doctor_session' table...")
        cur.execute('ALTER TABLE doctor_session ADD COLUMN IF NOT EXISTS phone_number VARCHAR(20);')

        print("3. Updating 'patient_booking' table...")
        cur.execute('ALTER TABLE patient_booking ADD COLUMN IF NOT EXISTS phone_number VARCHAR(20);')

        print("4. Updating 'saved_patient' table...")
        cur.execute("ALTER TABLE saved_patient ADD COLUMN IF NOT EXISTS relationship VARCHAR(50) DEFAULT 'Self';")

        print("5. Ensuring 'doctor_invitation' table...")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS doctor_invitation (
                id SERIAL PRIMARY KEY,
                doctor_id INTEGER NOT NULL REFERENCES doctor_session(id),
                email VARCHAR(120) NOT NULL,
                token_hash VARCHAR(64) UNIQUE NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_used BOOLEAN DEFAULT FALSE,
                used_at TIMESTAMP
            );
        """)
        cur.execute('CREATE INDEX IF NOT EXISTS ix_doctor_invitation_token_hash ON doctor_invitation (token_hash);')
        cur.execute('CREATE INDEX IF NOT EXISTS ix_doctor_invitation_email ON doctor_invitation (email);')

        print("6. Initializing 'admin_whatsapp_phone' setting...")
        cur.execute("""
            INSERT INTO app_settings (key, value) 
            VALUES ('admin_whatsapp_phone', '+918592031725') 
            ON CONFLICT (key) DO NOTHING;
        """)

        conn.commit()
        print("[SUCCESS] PostgreSQL database migration completed successfully!")

        # Verify
        cur.execute('SELECT COUNT(*) FROM "user";')
        print(f"Verified 'user' count: {cur.fetchone()[0]}")
        cur.execute('SELECT COUNT(*) FROM doctor_session;')
        print(f"Verified 'doctor_session' count: {cur.fetchone()[0]}")
        cur.execute('SELECT COUNT(*) FROM patient_booking;')
        print(f"Verified 'patient_booking' count: {cur.fetchone()[0]}")

    except Exception as e:
        conn.rollback()
        print(f"[ERROR] Migration failed: {e}")
        raise
    finally:
        cur.close()
        conn.close()

if __name__ == '__main__':
    migrate_postgres()


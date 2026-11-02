import os
import sys
import sqlite3
from dotenv import load_dotenv

load_dotenv()

def migrate_sqlite():
    db_path = 'instance/primecare.db'
    if not os.path.exists(db_path):
        print(f"[SKIP] SQLite db not found at {db_path}")
        return
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(doctor_session)")
    existing_cols = [c[1] for c in cursor.fetchall()]

    if 'phone_updated_by' not in existing_cols:
        cursor.execute("ALTER TABLE doctor_session ADD COLUMN phone_updated_by VARCHAR(120)")
        print("[SQLITE] Added phone_updated_by to doctor_session")
    if 'phone_updated_at' not in existing_cols:
        cursor.execute("ALTER TABLE doctor_session ADD COLUMN phone_updated_at TIMESTAMP")
        print("[SQLITE] Added phone_updated_at to doctor_session")

    conn.commit()
    conn.close()
    print("[SQLITE] Migration complete.")

def migrate_postgres():
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("[SKIP] No DATABASE_URL found for PostgreSQL.")
        return
    try:
        import psycopg2
        conn = psycopg2.connect(db_url)
        cursor = conn.cursor()
        cursor.execute("ALTER TABLE doctor_session ADD COLUMN IF NOT EXISTS phone_updated_by VARCHAR(120);")
        cursor.execute("ALTER TABLE doctor_session ADD COLUMN IF NOT EXISTS phone_updated_at TIMESTAMP;")
        conn.commit()
        conn.close()
        print("[POSTGRES] Migration complete.")
    except Exception as e:
        print(f"[POSTGRES] Migration skipped or failed: {e}")

if __name__ == '__main__':
    migrate_sqlite()
    migrate_postgres()


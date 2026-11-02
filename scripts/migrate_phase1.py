import sqlite3
import os
import sys

def migrate_database(db_file=None):
    if not db_file:
        app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        db_file = os.path.join(app_dir, 'instance', 'primecare.db')
        
    print(f"[MIGRATION] Target database: {db_file}")
    if not os.path.exists(db_file):
        print(f"[MIGRATION] Database file not found: {db_file}")
        return False
        
    con = sqlite3.connect(db_file)
    cur = con.cursor()
    
    # ─── 1. Migrate `user` table ───
    schema = cur.execute("PRAGMA table_info([user])").fetchall()
    cols = {c[1]: c for c in schema}
    
    needs_user_rebuild = ('email' in cols and cols['email'][3] == 1) or ('phone_number' not in cols)
    
    if needs_user_rebuild:
        print("[MIGRATION] Rebuilding `user` table for nullable email & mobile authentication...")
        cur.execute("""
            CREATE TABLE user_new (
                id INTEGER PRIMARY KEY,
                name VARCHAR(100),
                email VARCHAR(120) UNIQUE,
                phone_number VARCHAR(20) UNIQUE,
                password_hash VARCHAR(256) NOT NULL,
                role VARCHAR(50) DEFAULT 'patient',
                phone_verified BOOLEAN DEFAULT 0,
                phone_verified_at DATETIME,
                email_verified BOOLEAN DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Copy existing columns safely
        existing_cols = [c[1] for c in schema]
        has_phone = 'phone_number' in existing_cols
        
        if has_phone:
            cur.execute("""
                INSERT INTO user_new (id, name, email, phone_number, password_hash, role, created_at)
                SELECT id, name, email, phone_number, password_hash, role, created_at FROM [user]
            """)
        else:
            cur.execute("""
                INSERT INTO user_new (id, name, email, password_hash, role, created_at)
                SELECT id, name, email, password_hash, role, created_at FROM [user]
            """)
            
        cur.execute("DROP TABLE [user]")
        cur.execute("ALTER TABLE user_new RENAME TO [user]")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_user_phone_number ON [user] (phone_number)")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_user_email ON [user] (email)")
        print("[MIGRATION] `user` table rebuilt successfully.")
    else:
        # Check if individual columns need adding
        if 'phone_verified' not in cols:
            cur.execute("ALTER TABLE [user] ADD COLUMN phone_verified BOOLEAN DEFAULT 0")
        if 'phone_verified_at' not in cols:
            cur.execute("ALTER TABLE [user] ADD COLUMN phone_verified_at DATETIME")
        if 'email_verified' not in cols:
            cur.execute("ALTER TABLE [user] ADD COLUMN email_verified BOOLEAN DEFAULT 0")

    # ─── 2. Migrate `doctor_session` table ───
    doc_cols = [c[1] for c in cur.execute("PRAGMA table_info([doctor_session])").fetchall()]
    if 'phone_number' not in doc_cols:
        print("[MIGRATION] Adding phone_number column to `doctor_session`...")
        cur.execute("ALTER TABLE [doctor_session] ADD COLUMN phone_number VARCHAR(20)")

    # ─── 3. Migrate `patient_booking` table ───
    book_cols = [c[1] for c in cur.execute("PRAGMA table_info([patient_booking])").fetchall()]
    if 'phone_number' not in book_cols:
        print("[MIGRATION] Adding phone_number column to `patient_booking`...")
        cur.execute("ALTER TABLE [patient_booking] ADD COLUMN phone_number VARCHAR(20)")

    # ─── 4. Migrate `saved_patient` table ───
    saved_cols = [c[1] for c in cur.execute("PRAGMA table_info([saved_patient])").fetchall()]
    if 'relationship' not in saved_cols:
        print("[MIGRATION] Adding relationship column to `saved_patient`...")
        cur.execute("ALTER TABLE [saved_patient] ADD COLUMN relationship VARCHAR(50) DEFAULT 'Family'")

    # ─── 5. Create `doctor_invitation` table ───
    cur.execute("""
        CREATE TABLE IF NOT EXISTS doctor_invitation (
            id INTEGER PRIMARY KEY,
            doctor_name VARCHAR(100) NOT NULL,
            specialization VARCHAR(100) NOT NULL,
            email VARCHAR(120) NOT NULL,
            phone_number VARCHAR(20),
            token_hash VARCHAR(256) NOT NULL,
            expires_at DATETIME NOT NULL,
            is_used BOOLEAN DEFAULT 0,
            used_at DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            created_by VARCHAR(120)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS ix_doctor_invitation_token_hash ON doctor_invitation (token_hash)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_doctor_invitation_email ON doctor_invitation (email)")

    # ─── 6. Initialize default admin WhatsApp number in app_settings if absent ───
    cur.execute("SELECT id, value FROM app_settings WHERE key = 'admin_whatsapp_phone'")
    row = cur.fetchone()
    if not row:
        print("[MIGRATION] Initializing default admin_whatsapp_phone in `app_settings`...")
        cur.execute("INSERT INTO app_settings (key, value) VALUES ('admin_whatsapp_phone', '+918592031725')")

    con.commit()
    con.close()
    print("[MIGRATION] Phase 1 database migration completed successfully.")
    return True

if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else None
    migrate_database(target)


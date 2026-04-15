import sqlite3

def run_migration():
    conn = sqlite3.connect('instance/primecare.db')
    cursor = conn.cursor()
    try:
        cursor.execute("ALTER TABLE user ADD COLUMN role VARCHAR(50) DEFAULT 'patient'")
        print("Added role to user table.")
    except Exception as e:
        print("Role column might already exist:", e)
    
    # Create doctor session table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS doctor_session (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doctor_name VARCHAR(100),
            specialization VARCHAR(100),
            email VARCHAR(120) UNIQUE,
            status VARCHAR(20) DEFAULT 'idle',
            current_token INTEGER DEFAULT 0,
            session_date VARCHAR(20),
            total_tokens INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()
    print("Migration finished.")

if __name__ == '__main__':
    run_migration()

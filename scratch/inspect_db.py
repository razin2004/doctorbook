import sqlite3

def main():
    conn = sqlite3.connect('instance/primecare.db')
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [t[0] for t in cur.fetchall()]
    print("Tables:", tables)
    for table in tables:
        print(f"\n--- Table: {table} ---")
        try:
            cur.execute(f"PRAGMA table_info({table})")
            columns = [c[1] for c in cur.fetchall()]
            print("Columns:", columns)
            cur.execute(f"SELECT * FROM {table} LIMIT 10")
            rows = cur.fetchall()
            for row in rows:
                row_str = str(row)
                if 'welcome' in row_str.lower() or 'control' in row_str.lower() or 'parameters' in row_str.lower() or 'rosters' in row_str.lower():
                    print("MATCHING ROW:", row)
                else:
                    print(row[:3], "...")
        except Exception as e:
            print("Error:", e)

if __name__ == '__main__':
    main()

import os

with open("app.py", "r", encoding="utf-8") as f:
    lines = f.readlines()

for i, line in enumerate(lines, 1):
    if "session['user_id']" in line or "session[\"user_id\"]" in line:
        print(f"Line {i}: {line.strip()}")

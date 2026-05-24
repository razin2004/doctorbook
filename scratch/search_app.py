import re

with open("app.py", "r", encoding="utf-8") as f:
    lines = f.readlines()

print("Searching app.py...")
keywords = ["patient_dashboard", "doctor_dashboard", "get_credentials", "switch_active_role", "clinic_address"]
for i, line in enumerate(lines, 1):
    for kw in keywords:
        if kw in line:
            print(f"Line {i}: {line.strip()}")

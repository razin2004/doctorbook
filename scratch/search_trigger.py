import os

with open("templates/home.html", "r", encoding="utf-8") as f:
    lines = f.readlines()

for i, line in enumerate(lines, 1):
    if "trigger_login" in line or "trigger-login" in line:
        print(f"Line {i}: {line.strip()}")

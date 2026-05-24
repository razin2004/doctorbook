import re

with open('templates/home.html', 'r', encoding='utf-8') as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    clean = re.sub(r'[^\x00-\x7F]+', '', line)
    if 'departments' in line.lower() or '10+' in line or 'admin' in line.lower():
        print(f"Line {i+1}: {clean.strip()}")

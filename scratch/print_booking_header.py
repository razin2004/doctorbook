import re

with open('templates/booking.html', 'r', encoding='utf-8') as f:
    lines = f.readlines()

for i in range(250, 450):
    if i < len(lines):
        clean = re.sub(r'[^\x00-\x7F]+', '', lines[i])
        print(f"Line {i+1}: {clean.strip()}")

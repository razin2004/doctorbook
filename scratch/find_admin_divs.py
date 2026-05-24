import re

with open('templates/booking.html', 'r', encoding='utf-8') as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if 'adminLoginDiv' in line or 'adminLoginFormDiv' in line or 'adminActiveSessionDiv' in line or 'adminOptions' in line or 'adminPanel' in line:
        print(f"Line {i+1}: {line.strip()}")

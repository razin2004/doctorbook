import re

with open('templates/booking.html', 'r', encoding='utf-8') as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    clean = re.sub(r'[^\x00-\x7F]+', '', line)
    if 'by doctor' in line.lower() or 'by department' in line.lower() or 'panel-nav' in line.lower() or 'specialization' in line.lower():
        if len(clean.strip()) < 150:
            print(f"Line {i+1}: {clean.strip()}")

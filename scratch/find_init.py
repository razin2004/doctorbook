import re

with open('templates/booking.html', 'r', encoding='utf-8') as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if 'urlparams' in line.lower() or 'location.search' in line.lower() or 'window.addEventListener' in line or 'DOMContentLoaded' in line or 'onload' in line:
        print(f"Line {i+1}: {line.strip()}")

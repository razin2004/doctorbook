with open('templates/booking.html', 'r', encoding='utf-8') as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if 'function showSection' in line:
        print(f"Line {i+1}: {line.strip()}")

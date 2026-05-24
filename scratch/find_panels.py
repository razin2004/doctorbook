with open('templates/booking.html', 'r', encoding='utf-8') as f:
    lines = f.readlines()

targets = ['tickerPanel', 'holidayPanel', 'addDoctorPanel', 'deleteDoctorPanel', 'printReportTemplate', 'settingsPanel']
for i, line in enumerate(lines):
    for t in targets:
        if t in line and 'id=' in line:
            print(f"Line {i+1}: {line.strip()}")

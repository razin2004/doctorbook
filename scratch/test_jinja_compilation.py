import jinja2
import os

env = jinja2.Environment(loader=jinja2.FileSystemLoader('templates'))

templates_to_test = [
    'booking.html',
    'confirmation.html',
    'doctor_dashboard.html',
    'home.html',
    'patient_dashboard.html',
    'admin_analytics.html'
]

success = True
for name in templates_to_test:
    try:
        env.get_template(name)
        print(f"Jinja Compilation SUCCESS: {name}")
    except Exception as e:
        print(f"Jinja Compilation FAILED: {name} - Error: {e}")
        success = False

if not success:
    exit(1)

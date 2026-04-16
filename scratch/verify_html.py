import sys

checks = [
    'PrimeCare', 'primecare-live', 'primecare-alert',
    'showNotifPermCard', 'initInstallGuide',
    'isStandaloneMode', 'canReceiveNotif',
    'hasNotification', '_addSwipeDismiss', '_slideOut',
    'serviceWorker.register', 'enableNotif'
]

for fname in ['templates/home.html', 'templates/patient_dashboard.html']:
    with open(fname, 'r', encoding='utf-8', errors='ignore') as f:
        text = f.read()
    print('\n' + fname)
    for c in checks:
        status = 'OK     ' if c in text else 'MISSING'
        print(f'  [{status}] {c}')

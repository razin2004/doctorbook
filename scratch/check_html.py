import re

for fname in ['templates/home.html', 'templates/patient_dashboard.html']:
    with open(fname, 'r', encoding='utf-8') as f:
        content = f.read()

    scripts = re.findall(r'<script>(.*?)</script>', content, re.DOTALL)
    print(f"\n=== {fname} ({len(scripts)} script blocks) ===")
    for i, s in enumerate(scripts):
        opens = s.count('{') - s.count('${')  # exclude template literals
        closes = s.count('}')
        balance = opens - closes
        status = "OK" if balance == 0 else f"IMBALANCED by {balance}"
        print(f"  Script {i+1}: {{ = {s.count('{')} }} = {closes}  [{status}]")

    last = scripts[-1] if scripts else ''
    checks = [
        '_addSwipeDismiss', 'showNotifPermCard', 'initInstallGuide',
        'serviceWorker.register', 'PrimeCare . Live', 'primecare-alert',
        'hasNotification', 'updateBanner'
    ]
    for c in checks:
        print(f"  {'[OK]' if c in last else '[MISSING]'} {c}")

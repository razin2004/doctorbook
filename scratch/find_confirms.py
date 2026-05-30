import os
import re

for root, dirs, files in os.walk('templates'):
    for file in files:
        if file.endswith('.html'):
            path = os.path.join(root, file)
            content = open(path, encoding='utf-8').read()
            lines = content.splitlines()
            
            printed_header = False
            for idx, line in enumerate(lines, 1):
                if 'confirm(' in line or 'alert(' in line or 'showConfirmModal' in line or 'universalmodal' in line.lower() or 'universal-modal' in line.lower():
                    if not printed_header:
                        print(f"\n=== {path} ===")
                        printed_header = True
                    print(f"  {idx}: {line.strip()[:140]}")

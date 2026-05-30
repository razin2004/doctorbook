import os
import re

for root, dirs, files in os.walk('templates'):
    for file in files:
        if file.endswith('.html'):
            path = os.path.join(root, file)
            content = open(path, encoding='utf-8').read()
            
            # Find the div matching id="universalModal" and print up to the closing divs
            match = re.search(r'<div[^>]*id=["\']universalModal["\'].*?</div>\s*</div>\s*</div>', content, re.DOTALL)
            if match:
                print(f"\n=== {path} ===")
                print(match.group(0))

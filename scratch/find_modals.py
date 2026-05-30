import os
import re

patterns = {
    "js_confirm": r"confirm\(.*?\)",
    "modal_id_class": r"(?:id|class)=[\"'][^\"']*(?:modal|confirm|popup|dialog)[^\"']*[\"']",
    "alert_or_show_modal": r"showModal|openModal|openUniversalModal|closeUniversalModal"
}

for root, dirs, files in os.walk('templates'):
    for file in files:
        if file.endswith('.html'):
            path = os.path.join(root, file)
            content = open(path, encoding='utf-8').read()
            
            matches = {}
            for name, pat in patterns.items():
                found = re.findall(pat, content, re.IGNORECASE)
                if found:
                    matches[name] = len(found)
            
            if matches:
                print(f"=== {path} ===")
                for name, count in matches.items():
                    print(f"  {name}: {count}")

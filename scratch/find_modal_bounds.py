import os
import re

for root, dirs, files in os.walk('templates'):
    for file in files:
        if file.endswith('.html'):
            path = os.path.join(root, file)
            content = open(path, encoding='utf-8').read()
            
            # Find the index of <div class="modal-overlay" id="universalModal">
            start_idx = content.find('id="universalModal"')
            if start_idx == -1:
                start_idx = content.find("id='universalModal'")
            
            if start_idx != -1:
                # Find the surrounding div block start
                div_start = content.rfind('<div', 0, start_idx)
                # Find the matching closing div of modal-overlay.
                # Since modal-overlay contains modal-content, which has modal-btns, let's count open/close divs.
                open_divs = 0
                pos = div_start
                div_end = -1
                while pos < len(content):
                    if content[pos:pos+4] == '<div':
                        open_divs += 1
                        pos += 4
                    elif content[pos:pos+5] == '</div':
                        open_divs -= 1
                        if open_divs == 0:
                            # found the end of modal-overlay div
                            div_end = content.find('>', pos) + 1
                            break
                        pos += 5
                    else:
                        pos += 1
                
                if div_end != -1:
                    print(f"=== HTML modal in {path} ===")
                    print(f"Start index: {div_start}, End index: {div_end}")
                    print(content[div_start:div_end])

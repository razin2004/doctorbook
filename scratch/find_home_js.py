import re

def print_function(js, func_name):
    idx = js.find(f"function {func_name}")
    if idx == -1:
        idx = js.find(f"const {func_name}")
    if idx == -1:
        idx = js.find(f"async function {func_name}")
    if idx == -1:
        print(f"Could not find function {func_name}")
        return
        
    start_brace = js.find('{', idx)
    if start_brace == -1:
        print(f"Could not find start brace for {func_name}")
        return
        
    count = 1
    pos = start_brace + 1
    while count > 0 and pos < len(js):
        if js[pos] == '{':
            count += 1
        elif js[pos] == '}':
            count -= 1
        pos += 1
    print(f"=== Function {func_name} ===")
    print(js[idx:pos])
    print("=============================")

def main():
    content = open('templates/home.html', encoding='utf-8').read()
    scripts = re.findall(r'<script>(.*?)</script>', content, re.DOTALL)
    js = "\n".join(scripts)
    
    print_function(js, 'openLogoutModal')
    print_function(js, 'confirmLogout')
    
if __name__ == '__main__':
    main()

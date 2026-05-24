import os

def search_dir(directory, keywords):
    for root, dirs, files in os.walk(directory):
        for file in files:
            if file.endswith(".html"):
                filepath = os.path.join(root, file)
                with open(filepath, "r", encoding="utf-8") as f:
                    content = f.read()
                for kw in keywords:
                    if kw in content:
                        print(f"Found {kw} in {file}")

search_dir("templates", ["can_switch"])

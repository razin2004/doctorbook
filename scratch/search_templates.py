import os

def search_file(filepath, keywords):
    print(f"--- Searching {filepath} ---")
    if not os.path.exists(filepath):
        print("File does not exist")
        return
    with open(filepath, "r", encoding="utf-8") as f:
        lines = f.readlines()
    for i, line in enumerate(lines, 1):
        for kw in keywords:
            if kw in line:
                print(f"Line {i}: {line.strip()}")

search_file("templates/home.html", ["function openModal", "openModal("])

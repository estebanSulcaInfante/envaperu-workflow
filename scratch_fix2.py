import os
import re

TEST_DIR = r"c:\Users\esteb\gitprojects\envaperu-workspace-2\backend\tests"

def replace_in_files():
    for root, dirs, files in os.walk(TEST_DIR):
        for file in files:
            if not file.endswith(".py"):
                continue
            filepath = os.path.join(root, file)
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()

            new_content = content
            
            # Replace maquinista="TESTER" -> trabajador_id=1, maquinista_snapshot="Juan P."
            new_content = re.sub(r'maquinista=("[^"]+")',
                                 r'trabajador_id=1, maquinista_snapshot=\1',
                                 new_content)

            if new_content != content:
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(new_content)
                print(f"Updated maquinista in {filepath}")

replace_in_files()

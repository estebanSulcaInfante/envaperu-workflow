import os
import re

TEST_DIR = r"c:\Users\esteb\gitprojects\envaperu-workspace-2\backend\tests"

def replace_in_files():
    # Fix Maquina(..., tipo="...") to Maquina(codigo="AUTO-MQ", ..., tipo_maquina_id=1, tipo="...")
    for root, dirs, files in os.walk(TEST_DIR):
        for file in files:
            if not file.endswith(".py"):
                continue
            filepath = os.path.join(root, file)
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()

            new_content = content
            
            # Replace Maquina(nombre="...", tipo="...") -> Maquina(codigo="...", nombre="...", tipo_maquina_id=1, tipo="...")
            # We can use regex to match Maquina( and insert codigo="TEST-MQ", tipo_maquina_id=1
            new_content = re.sub(r'Maquina\((id=\d+,\s*)?nombre=("[^"]+")',
                                 r'Maquina(\1codigo=\2, nombre=\2, tipo_maquina_id=1',
                                 new_content)

            if new_content != content:
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(new_content)
                print(f"Updated Maquina in {filepath}")

replace_in_files()

import os
import re

TEST_DIR = r"c:\Users\esteb\gitprojects\envaperu-workspace-2\backend\tests"

def run():
    for filename in os.listdir(TEST_DIR):
        if filename.endswith(".py"):
            filepath = os.path.join(TEST_DIR, filename)
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()

            new_content = content
            
            # PiezaColor kwargs
            new_content = re.sub(r'cod_color=[^,)]+,?\s*', '', new_content)
            new_content = re.sub(r'color=[^,)]+,?\s*', '', new_content)
            new_content = re.sub(r'molde_id=["\'][^,)]+["\'],?\s*', '', new_content)
            
            # ProductoTerminado kwargs
            new_content = re.sub(r'familia_color=[^,)]+,?\s*', '', new_content)
            
            # LoteColor / ControlPeso / Dict kwargs
            new_content = new_content.replace('color_id=', 'color_produccion_id=')
            new_content = new_content.replace('"color_id":', '"color_produccion_id":')
            new_content = new_content.replace("'color_id':", "'color_produccion_id':")
            
            # Pieza kwargs
            new_content = new_content.replace('pieza_sku=', 'nombre=')
            
            # test_e2e_flujo_op uses "cod_color" in dicts maybe?
            new_content = new_content.replace('"cod_color":', '"color_produccion_id":')
            new_content = new_content.replace("'cod_color':", "'color_produccion_id':")
            
            # test_simple_molde.py uses `mp.pieza_sku`
            new_content = new_content.replace('mp.pieza_sku', 'mp.nombre')

            if new_content != content:
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(new_content)
                print(f"Fixed kwargs in {filename}")

if __name__ == '__main__':
    run()

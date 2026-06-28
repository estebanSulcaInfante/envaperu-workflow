import os
import re

def refactor_file(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    original = content
    
    # In app.models.producto, Pieza becomes PiezaColor
    # Replaces 'from app.models.producto import ... Pieza ...'
    # We will do regex replacement
    # 1. First, rename Pieza to PiezaColor in all imports from app.models.producto
    # We find "from app.models.producto import " and replace Pieza with PiezaColor in that line.
    lines = content.split('\n')
    new_lines = []
    for line in lines:
        if 'from app.models.producto import' in line or 'from app.models.molde import' in line or 'import' in line:
            if 'from app.models.producto' in line:
                line = re.sub(r'\bPieza\b', 'PiezaColor', line)
            if 'from app.models.molde' in line:
                line = re.sub(r'\bMoldePieza\b', 'Pieza', line)
            
            # For general uses like queries `Pieza.query` -> `PiezaColor.query` if they were meant for the old Pieza.
            # But wait, we shouldn't indiscriminately replace `Pieza` everywhere because `MoldePieza` is now `Pieza`.
        new_lines.append(line)
    
    # We should replace MoldePieza with Pieza in all code
    # And we should replace Pieza with PiezaColor in all code (except where it's the new Pieza)
    # Order matters:
    # 1. Replace Pieza with PiezaColor
    # 2. Replace MoldePieza with Pieza
    # Note: If there are strings "MoldePieza", they become "Pieza".
    
    content = '\n'.join(new_lines)
    
    # Global replace for classes
    content = re.sub(r'\bPieza\b', 'PiezaColor', content)
    content = re.sub(r'\bMoldePiezaColor\b', 'MoldePieza', content) # Revert if we messed up
    content = re.sub(r'\bMoldePieza\b', 'Pieza', content)

    # Some variables like `pieza = PiezaColor(...)` might be confusing, but `PiezaColor` works as the class name.
    
    if content != original:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"Refactored {filepath}")

def main():
    backend_dir = r"C:\Users\esteb\gitprojects\envaperu-workspace-2\backend"
    exclude_dirs = ['venv', 'migrations', '__pycache__', '.git', 'logs']
    exclude_files = ['molde.py', 'producto.py', 'migrate_ts001.py', 'refactor_imports.py']
    
    for root, dirs, files in os.walk(backend_dir):
        dirs[:] = [d for d in dirs if d not in exclude_dirs]
        for f in files:
            if f.endswith('.py') and f not in exclude_files:
                refactor_file(os.path.join(root, f))

if __name__ == '__main__':
    main()

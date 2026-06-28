import os
import re

def fix_imports(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    original = content
    
    # Fix 'from app.models.molde import ... Pieza' to Pieza
    lines = content.split('\n')
    new_lines = []
    for line in lines:
        if 'from app.models.molde' in line and 'Pieza' in line:
            line = re.sub(r'\bPiezaColor\b', 'Pieza', line)
        new_lines.append(line)
        
    content = '\n'.join(new_lines)
    
    if content != original:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"Fixed {filepath}")

def main():
    backend_dir = r"C:\Users\esteb\gitprojects\envaperu-workspace-2\backend"
    exclude_dirs = ['venv', 'migrations', '__pycache__', '.git', 'logs']
    exclude_files = ['molde.py', 'producto.py', 'migrate_ts001.py', 'refactor_imports.py']
    
    for root, dirs, files in os.walk(backend_dir):
        dirs[:] = [d for d in dirs if d not in exclude_dirs]
        for f in files:
            if f.endswith('.py') and f not in exclude_files:
                fix_imports(os.path.join(root, f))

if __name__ == '__main__':
    main()

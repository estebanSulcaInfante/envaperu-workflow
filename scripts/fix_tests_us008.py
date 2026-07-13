import os
import re

TEST_DIR = r"c:\Users\esteb\gitprojects\envaperu-workspace-2\backend\tests"

def fix_test_file(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    # Imports: Replace ColorProducto with ColorProduccion, ColorBase
    content = re.sub(r'\bColorProducto\b', 'ColorProduccion, ColorBase', content)

    # ColorProducto(...) -> crear_color_test(db, ...)
    # Let's write a regex that finds ColorProduccion, ColorBase(nombre="...", ...)
    # Wait, the previous regex changed ColorProducto to "ColorProduccion, ColorBase"
    # So the calls look like `ColorProduccion, ColorBase(nombre="ROJO", codigo=20)`
    
    # We can define a helper in the test file or just replace it with a helper call.
    # Actually, it's easier to just read the file, and do manual replacements on the string.
    
    # First, let's revert the import replacement to do it cleaner
    pass

def run():
    for filename in os.listdir(TEST_DIR):
        if filename.endswith(".py"):
            filepath = os.path.join(TEST_DIR, filename)
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
                
            if "ColorProducto" in content:
                content = content.replace("from app.models.producto import ColorProducto", "from app.models.producto import ColorProduccion, ColorBase, FamiliaColor")
                content = content.replace("from app.models.producto import ProductoTerminado, FamiliaColor, ColorProducto, PiezaColor", "from app.models.producto import ProductoTerminado, FamiliaColor, ColorProduccion, ColorBase, PiezaColor")
                content = content.replace("from app.models.producto import ColorProducto, FamiliaColor", "from app.models.producto import ColorProduccion, ColorBase, FamiliaColor")
                content = content.replace("from app.models.producto import ColorProducto, FamiliaColor, PiezaColor, Linea, Familia", "from app.models.producto import ColorProduccion, ColorBase, FamiliaColor, PiezaColor, Linea, Familia")
                
                # Helper function to inject
                helper = """
def _get_or_create_fam(nombre="SOLIDO", codigo=1):
    from app.models.producto import FamiliaColor
    fam = FamiliaColor.query.filter_by(nombre=nombre).first()
    if not fam:
        from app.extensions import db
        fam = FamiliaColor(nombre=nombre, codigo=codigo)
        db.session.add(fam)
        db.session.flush()
    return fam

def _create_color_prod(nombre, codigo=None, familia_id=None):
    from app.models.producto import ColorBase, ColorProduccion
    from app.extensions import db
    cb = ColorBase.query.filter_by(nombre=nombre).first()
    if not cb:
        cb = ColorBase(nombre=nombre)
        db.session.add(cb)
        db.session.flush()
    fam_id = familia_id if familia_id else _get_or_create_fam().id
    cp = ColorProduccion(color_base_id=cb.id, familia_color_id=fam_id, codigo_legacy=codigo)
    db.session.add(cp)
    db.session.flush()
    return cp
"""
                # Inject helper after imports
                import_end = content.find("\n\n")
                content = content[:import_end] + "\n" + helper + content[import_end:]
                
                # Replace instantiations
                # ColorProducto(nombre="ROJO", codigo=20, familia_id=fam.id) -> _create_color_prod(nombre="ROJO", codigo=20, familia_id=fam.id)
                content = re.sub(r'ColorProducto\s*\(', '_create_color_prod(', content)
                
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(content)
                    
            # Fix talonarios syntax error
            if filename == "test_talonarios.py":
                content = content.replace('"""Tests de la API de Talonarios"""', '"""Tests de la API de Talonarios"""\n')
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(content)

if __name__ == '__main__':
    run()

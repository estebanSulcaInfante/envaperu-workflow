"""
Script de migración para refactorizar ProductoTerminado.

Cambios:
1. FamiliaColor ahora tiene campo 'codigo'
2. ProductoTerminado ya NO usa color_id/cod_color
3. ProductoTerminado AHORA usa:
   - cod_familia_color (int): Código de la familia de color
   - familia_color (str): Nombre de la familia
   - familia_color_id (FK): Relación con FamiliaColor

Para ejecutar:
    python migrate_familia_color.py
"""

import sys
sys.path.insert(0, '.')

from app import create_app
from app.extensions import db
from app.models.producto import FamiliaColor, ProductoTerminado, ColorProducto
from sqlalchemy import text

app = create_app()


def column_exists(table_name: str, column_name: str) -> bool:
    """Verifica si una columna existe en PostgreSQL."""
    result = db.session.execute(text(f"""
        SELECT EXISTS (
            SELECT 1 FROM information_schema.columns 
            WHERE table_name = '{table_name}' 
            AND column_name = '{column_name}'
        )
    """))
    return result.scalar()


def migrate():
    with app.app_context():
        print("=" * 60)
        print("MIGRACIÓN: ProductoTerminado → FamiliaColor")
        print("=" * 60)
        
        # 1. Agregar columna 'codigo' a FamiliaColor si no existe
        print("\n1. Verificando estructura de FamiliaColor...")
        if column_exists('familia_color', 'codigo'):
            print("   ✅ Columna 'codigo' ya existe en familia_color")
        else:
            print("   ⚠️ Agregando columna 'codigo' a familia_color...")
            db.session.execute(text("ALTER TABLE familia_color ADD COLUMN codigo INTEGER"))
            db.session.commit()
            print("   ✅ Columna 'codigo' agregada")
        
        # 2. Verificar/agregar columnas en ProductoTerminado
        print("\n2. Verificando estructura de ProductoTerminado...")
        
        # Check for cod_familia_color
        if column_exists('producto_terminado', 'cod_familia_color'):
            print("   ✅ Columna 'cod_familia_color' ya existe")
        else:
            print("   ⚠️ Agregando columna 'cod_familia_color'...")
            db.session.execute(text("ALTER TABLE producto_terminado ADD COLUMN cod_familia_color INTEGER"))
            db.session.commit()
            print("   ✅ Columna 'cod_familia_color' agregada")
        
        # Check for familia_color_id
        if column_exists('producto_terminado', 'familia_color_id'):
            print("   ✅ Columna 'familia_color_id' ya existe")
        else:
            print("   ⚠️ Agregando columna 'familia_color_id'...")
            db.session.execute(text("""
                ALTER TABLE producto_terminado 
                ADD COLUMN familia_color_id INTEGER 
                REFERENCES familia_color(id)
            """))
            db.session.commit()
            print("   ✅ Columna 'familia_color_id' agregada")
        
        # 3. Migrar datos de cod_color a cod_familia_color (si cod_color existe)
        print("\n3. Migrando datos de cod_color → cod_familia_color...")
        if column_exists('producto_terminado', 'cod_color'):
            db.session.execute(text("""
                UPDATE producto_terminado 
                SET cod_familia_color = cod_color 
                WHERE cod_familia_color IS NULL AND cod_color IS NOT NULL
            """))
            db.session.commit()
            print("   ✅ Datos migrados exitosamente desde cod_color")
        else:
            print("   ℹ️ Columna 'cod_color' no existe, saltando migración de datos")
        
        # 4. Crear FamiliaColor desde familia_color strings existentes
        print("\n4. Normalizando FamiliaColor desde datos existentes...")
        
        # Obtener familias únicas de productos existentes
        existing_familias = db.session.execute(text("""
            SELECT DISTINCT UPPER(familia_color) as nombre, cod_familia_color as codigo
            FROM producto_terminado 
            WHERE familia_color IS NOT NULL
        """)).fetchall()
        
        familias_creadas = 0
        for row in existing_familias:
            nombre = row[0]
            codigo = row[1]
            
            if nombre:
                # Verificar si ya existe
                existe = FamiliaColor.query.filter(
                    db.func.upper(FamiliaColor.nombre) == nombre
                ).first()
                
                if not existe:
                    nueva = FamiliaColor(nombre=nombre, codigo=codigo)
                    db.session.add(nueva)
                    familias_creadas += 1
                elif existe.codigo is None and codigo is not None:
                    # Actualizar código si no tenía
                    existe.codigo = codigo
        
        db.session.commit()
        print(f"   ✅ {familias_creadas} familias creadas")
        
        # 5. Actualizar FK familia_color_id en productos existentes
        print("\n5. Actualizando relaciones FK en ProductoTerminado...")
        productos = ProductoTerminado.query.filter(
            ProductoTerminado.familia_color_id.is_(None),
            ProductoTerminado.familia_color.isnot(None)
        ).all()
        
        actualizados = 0
        for prod in productos:
            if prod.familia_color:
                familia = FamiliaColor.query.filter(
                    db.func.upper(FamiliaColor.nombre) == prod.familia_color.upper()
                ).first()
                if familia:
                    prod.familia_color_id = familia.id
                    actualizados += 1
        
        db.session.commit()
        print(f"   ✅ {actualizados} productos actualizados con FK")
        
        # 6. Resumen final
        print("\n" + "=" * 60)
        print("RESUMEN DE MIGRACIÓN")
        print("=" * 60)
        print(f"FamiliaColor existentes: {FamiliaColor.query.count()}")
        print(f"ProductoTerminado total: {ProductoTerminado.query.count()}")
        with_fk = ProductoTerminado.query.filter(ProductoTerminado.familia_color_id.isnot(None)).count()
        print(f"Productos con familia_color_id: {with_fk}")
        print("\n✅ Migración completada exitosamente!")
        
        # Listar familias
        print("\nFamilias de Color existentes:")
        for f in FamiliaColor.query.all():
            print(f"  - {f.nombre} (codigo={f.codigo})")


if __name__ == '__main__':
    migrate()

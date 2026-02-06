"""
Migración: Agregar campos de revisión progresiva a ProductoTerminado Y Pieza

Campos nuevos en ambas tablas:
- estado_revision: IMPORTADO, EN_REVISION, VERIFICADO
- fecha_importacion: DateTime
- fecha_revision: DateTime
- notas_revision: Text

Uso: python migrate_revision_fields.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import create_app, db
from sqlalchemy import text, inspect

def column_exists(table_name, column_name):
    """Verifica si una columna existe en una tabla (PostgreSQL)."""
    inspector = inspect(db.engine)
    columns = [col['name'] for col in inspector.get_columns(table_name)]
    return column_name in columns

def migrate_table(table_name):
    """Agrega campos de revisión a una tabla."""
    print(f"\n📦 Procesando tabla: {table_name}")
    
    # 1. estado_revision
    if column_exists(table_name, 'estado_revision'):
        print(f"   ✅ Columna 'estado_revision' ya existe")
    else:
        print(f"   ⚠️  Agregando columna 'estado_revision'...")
        db.session.execute(text(
            f"ALTER TABLE {table_name} ADD COLUMN estado_revision VARCHAR(20) DEFAULT 'IMPORTADO'"
        ))
        db.session.commit()
        print(f"   ✅ Columna 'estado_revision' agregada")
    
    # 2. fecha_importacion
    if column_exists(table_name, 'fecha_importacion'):
        print(f"   ✅ Columna 'fecha_importacion' ya existe")
    else:
        print(f"   ⚠️  Agregando columna 'fecha_importacion'...")
        db.session.execute(text(
            f"ALTER TABLE {table_name} ADD COLUMN fecha_importacion TIMESTAMP"
        ))
        db.session.commit()
        print(f"   ✅ Columna 'fecha_importacion' agregada")
    
    # 3. fecha_revision
    if column_exists(table_name, 'fecha_revision'):
        print(f"   ✅ Columna 'fecha_revision' ya existe")
    else:
        print(f"   ⚠️  Agregando columna 'fecha_revision'...")
        db.session.execute(text(
            f"ALTER TABLE {table_name} ADD COLUMN fecha_revision TIMESTAMP"
        ))
        db.session.commit()
        print(f"   ✅ Columna 'fecha_revision' agregada")
    
    # 4. notas_revision
    if column_exists(table_name, 'notas_revision'):
        print(f"   ✅ Columna 'notas_revision' ya existe")
    else:
        print(f"   ⚠️  Agregando columna 'notas_revision'...")
        db.session.execute(text(
            f"ALTER TABLE {table_name} ADD COLUMN notas_revision TEXT"
        ))
        db.session.commit()
        print(f"   ✅ Columna 'notas_revision' agregada")
    
    # 5. Actualizar registros sin estado
    print(f"   📊 Actualizando registros sin estado de revisión...")
    db.session.execute(text(f"""
        UPDATE {table_name} 
        SET estado_revision = 'IMPORTADO' 
        WHERE estado_revision IS NULL
    """))
    db.session.commit()
    print(f"   ✅ Registros actualizados a estado 'IMPORTADO'")

def migrate():
    """Ejecuta la migración de campos de revisión."""
    print("\n" + "="*60)
    print("MIGRACIÓN: Campos de Revisión Progresiva")
    print("="*60)
    
    try:
        # Migrar ProductoTerminado
        migrate_table('producto_terminado')
        
        # Migrar Pieza
        migrate_table('pieza')
        
        # Estadísticas finales
        print("\n" + "="*60)
        print("📈 ESTADÍSTICAS FINALES")
        print("="*60)
        
        for table_name in ['producto_terminado', 'pieza']:
            print(f"\n📊 {table_name}:")
            stats = db.session.execute(text(f"""
                SELECT estado_revision, COUNT(*) as total 
                FROM {table_name} 
                GROUP BY estado_revision
            """)).fetchall()
            
            for estado, total in stats:
                print(f"   • {estado or 'SIN ESTADO'}: {total} registros")
        
        print("\n" + "="*60)
        print("✅ MIGRACIÓN COMPLETADA EXITOSAMENTE")
        print("="*60 + "\n")
        
    except Exception as e:
        db.session.rollback()
        print(f"\n❌ ERROR en la migración: {e}")
        raise

if __name__ == '__main__':
    app = create_app()
    with app.app_context():
        migrate()

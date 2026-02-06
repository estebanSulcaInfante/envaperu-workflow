"""
Script de migración para normalizar la entidad Linea.

Este script:
1. Crea la tabla 'linea' si no existe
2. Pobla con HOGAR (codigo=1) e INDUSTRIAL (codigo=2)
3. Actualiza linea_id en ProductoTerminado basándose en el nombre de linea
4. Actualiza linea_id en Pieza basándose en cod_linea

Uso:
    python migrate_linea.py
"""
import sys
sys.path.insert(0, '.')

from sqlalchemy import text, inspect
from app import create_app
from app.extensions import db
from app.models.producto import Linea, ProductoTerminado, Pieza

def column_exists(table_name, column_name):
    """Check if a column exists in a table."""
    inspector = inspect(db.engine)
    columns = [c['name'] for c in inspector.get_columns(table_name)]
    return column_name in columns

def table_exists(table_name):
    """Check if a table exists."""
    inspector = inspect(db.engine)
    return table_name in inspector.get_table_names()

def migrate():
    app = create_app()
    
    with app.app_context():
        print("=" * 60)
        print("MIGRACIÓN: Normalización de Linea")
        print("=" * 60)
        
        # 1. Crear tabla linea si no existe
        print("\n1. Verificando tabla 'linea'...")
        if not table_exists('linea'):
            print("   ⚠️ Creando tabla 'linea'...")
            Linea.__table__.create(db.engine)
            print("   ✅ Tabla 'linea' creada")
        else:
            print("   ✅ Tabla 'linea' ya existe")
        
        # 2. Agregar columna linea_id a producto_terminado si no existe
        print("\n2. Verificando columna 'linea_id' en producto_terminado...")
        if not column_exists('producto_terminado', 'linea_id'):
            print("   ⚠️ Agregando columna 'linea_id'...")
            db.session.execute(text("""
                ALTER TABLE producto_terminado 
                ADD COLUMN linea_id INTEGER 
                REFERENCES linea(id)
            """))
            db.session.commit()
            print("   ✅ Columna 'linea_id' agregada")
        else:
            print("   ✅ Columna 'linea_id' ya existe")
        
        # 3. Agregar columna linea_id a pieza si no existe
        print("\n3. Verificando columna 'linea_id' en pieza...")
        if not column_exists('pieza', 'linea_id'):
            print("   ⚠️ Agregando columna 'linea_id'...")
            db.session.execute(text("""
                ALTER TABLE pieza 
                ADD COLUMN linea_id INTEGER 
                REFERENCES linea(id)
            """))
            db.session.commit()
            print("   ✅ Columna 'linea_id' agregada")
        else:
            print("   ✅ Columna 'linea_id' ya existe")
        
        # 4. Poblar tabla linea con valores conocidos
        print("\n4. Poblando tabla 'linea'...")
        
        # Definimos las líneas conocidas
        lineas_conocidas = [
            {'codigo': 1, 'nombre': 'HOGAR'},
            {'codigo': 2, 'nombre': 'INDUSTRIAL'},
        ]
        
        for linea_data in lineas_conocidas:
            existente = Linea.query.filter_by(codigo=linea_data['codigo']).first()
            if not existente:
                existente = Linea.query.filter_by(nombre=linea_data['nombre']).first()
            
            if not existente:
                nueva_linea = Linea(**linea_data)
                db.session.add(nueva_linea)
                print(f"   ✅ Creada línea: {linea_data['nombre']} (codigo={linea_data['codigo']})")
            else:
                print(f"   ⏭️ Línea ya existe: {existente.nombre} (codigo={existente.codigo})")
        
        db.session.commit()
        
        # 5. Actualizar linea_id en ProductoTerminado
        print("\n5. Actualizando linea_id en producto_terminado...")
        
        # Mapeo por nombre de linea (normalizado a mayúsculas)
        lineas_map = {l.nombre.upper(): l for l in Linea.query.all()}
        
        productos_actualizados = 0
        for pt in ProductoTerminado.query.filter(ProductoTerminado.linea_id.is_(None)).all():
            if pt.linea:
                linea_nombre = pt.linea.upper()
                if linea_nombre in lineas_map:
                    pt.linea_id = lineas_map[linea_nombre].id
                    productos_actualizados += 1
        
        db.session.commit()
        print(f"   ✅ Actualizados {productos_actualizados} productos")
        
        # 6. Actualizar linea_id en Pieza
        print("\n6. Actualizando linea_id en pieza...")
        
        # Mapeo por código de linea
        lineas_map_codigo = {l.codigo: l for l in Linea.query.all()}
        
        piezas_actualizadas = 0
        for pieza in Pieza.query.filter(Pieza.linea_id.is_(None)).all():
            if pieza.cod_linea and pieza.cod_linea in lineas_map_codigo:
                pieza.linea_id = lineas_map_codigo[pieza.cod_linea].id
                piezas_actualizadas += 1
            elif pieza.linea:
                linea_nombre = pieza.linea.upper()
                if linea_nombre in lineas_map:
                    pieza.linea_id = lineas_map[linea_nombre].id
                    piezas_actualizadas += 1
        
        db.session.commit()
        print(f"   ✅ Actualizadas {piezas_actualizadas} piezas")
        
        # 7. Resumen
        print("\n" + "=" * 60)
        print("RESUMEN DE MIGRACIÓN")
        print("=" * 60)
        print(f"Líneas en tabla: {Linea.query.count()}")
        print(f"Productos con linea_id: {ProductoTerminado.query.filter(ProductoTerminado.linea_id.isnot(None)).count()}")
        print(f"Piezas con linea_id: {Pieza.query.filter(Pieza.linea_id.isnot(None)).count()}")
        print("\n✅ Migración completada exitosamente!")

if __name__ == '__main__':
    migrate()

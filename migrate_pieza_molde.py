"""
Migración: Agregar molde_id a tabla pieza (relación 1:N)

Ejecutar: python migrate_pieza_molde.py
"""
from app import create_app, db
from sqlalchemy import text

app = create_app()

def migrate():
    with app.app_context():
        conn = db.engine.connect()
        
        print("=== Migración: Agregar molde_id a pieza ===")
        
        # 1. Verificar si columna existe
        result = conn.execute(text("""
            SELECT column_name FROM information_schema.columns 
            WHERE table_name = 'pieza' AND column_name = 'molde_id'
        """))
        if result.fetchone():
            print("✅ Columna molde_id ya existe en pieza")
        else:
            print("Agregando columna molde_id a pieza...")
            conn.execute(text("ALTER TABLE pieza ADD COLUMN molde_id VARCHAR(50)"))
            conn.commit()
            print("✅ Columna molde_id agregada")
        
        # 2. Migrar datos desde molde_pieza si existe
        try:
            result = conn.execute(text("""
                SELECT table_name FROM information_schema.tables 
                WHERE table_name = 'molde_pieza'
            """))
            if result.fetchone():
                print("Migrando datos desde molde_pieza...")
                conn.execute(text("""
                    UPDATE pieza SET molde_id = (
                        SELECT molde_id FROM molde_pieza 
                        WHERE molde_pieza.pieza_sku = pieza.sku 
                        LIMIT 1
                    )
                    WHERE molde_id IS NULL
                """))
                conn.commit()
                print("✅ Datos migrados desde molde_pieza")
        except Exception as e:
            print(f"⚠️ No se pudo migrar desde molde_pieza: {e}")
        
        print("=== Migración completada ===")
        conn.close()

if __name__ == '__main__':
    migrate()

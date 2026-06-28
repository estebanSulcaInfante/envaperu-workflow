"""
Migración TS-001: Refactorización de Pieza y PiezaColor

Ejecutar: python migrate_ts001.py
"""
from app import create_app, db
from sqlalchemy import text

app = create_app()

def migrate():
    with app.app_context():
        conn = db.engine.connect()
        
        print("=== Migración TS-001: Renombrar tablas y agregar columnas ===")
        
        # 1. Renombrar tabla pieza -> pieza_color
        try:
            result = conn.execute(text("SELECT table_name FROM information_schema.tables WHERE table_name = 'pieza'"))
            if result.fetchone():
                print("Renombrando tabla 'pieza' a 'pieza_color'...")
                conn.execute(text("ALTER TABLE pieza RENAME TO pieza_color"))
                conn.commit()
                print("OK: Tabla renombrada a pieza_color")
        except Exception as e:
            print(f"ERROR: Error renombrando pieza: {e}")
            conn.rollback()

        # 2. Renombrar tabla molde_pieza -> pieza
        try:
            result = conn.execute(text("SELECT table_name FROM information_schema.tables WHERE table_name = 'molde_pieza'"))
            if result.fetchone():
                print("Renombrando tabla 'molde_pieza' a 'pieza'...")
                conn.execute(text("ALTER TABLE molde_pieza RENAME TO pieza"))
                conn.commit()
                print("OK: Tabla renombrada a pieza")
        except Exception as e:
            print(f"ERROR: Error renombrando molde_pieza: {e}")
            conn.rollback()

        # 3. Renombrar columna molde_pieza_id -> pieza_id en pieza_color
        try:
            result = conn.execute(text("""
                SELECT column_name FROM information_schema.columns 
                WHERE table_name = 'pieza_color' AND column_name = 'molde_pieza_id'
            """))
            if result.fetchone():
                print("Renombrando columna molde_pieza_id a pieza_id en pieza_color...")
                conn.execute(text("ALTER TABLE pieza_color RENAME COLUMN molde_pieza_id TO pieza_id"))
                conn.commit()
                print("OK: Columna renombrada a pieza_id")
        except Exception as e:
            print(f"ERROR: Error renombrando columna molde_pieza_id: {e}")
            conn.rollback()

        # 4. Agregar linea_id y familia_id a pieza
        try:
            result = conn.execute(text("""
                SELECT column_name FROM information_schema.columns 
                WHERE table_name = 'pieza' AND column_name = 'linea_id'
            """))
            if not result.fetchone():
                print("Agregando linea_id y familia_id a pieza...")
                conn.execute(text("ALTER TABLE pieza ADD COLUMN linea_id INTEGER"))
                conn.execute(text("ALTER TABLE pieza ADD COLUMN familia_id INTEGER"))
                
                # Migrar datos desde pieza_color a pieza
                conn.execute(text("""
                    UPDATE pieza 
                    SET linea_id = pc.linea_id, familia_id = pc.familia_id
                    FROM pieza_color pc 
                    WHERE pc.pieza_id = pieza.id
                """))
                conn.commit()
                print("OK: Columnas linea_id y familia_id agregadas y pobladas")
        except Exception as e:
            print(f"ERROR: Error agregando linea_id/familia_id: {e}")
            conn.rollback()

        # 5. Intentar agregar constraints FK si no existen (opcional, SQLAlchemy lo maneja, pero por integridad BD)
        try:
            conn.execute(text("""
                ALTER TABLE pieza 
                ADD CONSTRAINT fk_pieza_linea FOREIGN KEY (linea_id) REFERENCES linea(id);
                
                ALTER TABLE pieza 
                ADD CONSTRAINT fk_pieza_familia FOREIGN KEY (familia_id) REFERENCES familia(id);
            """))
            conn.commit()
            print("OK: Constraints FK agregadas a pieza")
        except Exception as e:
            # Pueden fallar si hay datos inválidos o ya existe
            print(f"ERROR: Nota sobre constraints FK: {e}")
            conn.rollback()

        # 6. Eliminar FK obsoleta pieza_sku de pieza (antes molde_pieza)
        try:
            result = conn.execute(text("""
                SELECT column_name FROM information_schema.columns 
                WHERE table_name = 'pieza' AND column_name = 'pieza_sku'
            """))
            if result.fetchone():
                print("Eliminando columna legacy pieza_sku de pieza...")
                # Eliminar constraint primero
                conn.execute(text("ALTER TABLE pieza DROP CONSTRAINT IF EXISTS fk_moldepieza_pieza_sku"))
                conn.execute(text("ALTER TABLE pieza DROP COLUMN pieza_sku"))
                conn.commit()
                print("OK: Columna pieza_sku eliminada")
        except Exception as e:
            print(f"ERROR: Error eliminando pieza_sku: {e}")
            conn.rollback()

        print("=== Migración completada ===")
        conn.close()

if __name__ == '__main__':
    migrate()

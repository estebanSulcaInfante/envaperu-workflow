"""
Migración: Crear tablas para Molde, MoldePieza, PiezaComponente
y agregar campos tipo a Pieza y molde_id a OrdenProduccion
"""
from app import create_app
from app.extensions import db
from sqlalchemy import text

app = create_app()

def migrar():
    with app.app_context():
        print("🔄 Ejecutando migración: Entidad Molde y relaciones...")
        
        try:
            # 1. Crear tabla molde
            db.session.execute(text("""
                CREATE TABLE IF NOT EXISTS molde (
                    codigo VARCHAR(50) PRIMARY KEY,
                    nombre VARCHAR(100) NOT NULL,
                    peso_tiro_gr FLOAT NOT NULL,
                    tiempo_ciclo_std FLOAT DEFAULT 30.0,
                    activo BOOLEAN DEFAULT TRUE,
                    notas TEXT
                )
            """))
            print("✅ Tabla 'molde' creada o ya existe")
            
            # 2. Crear tabla molde_pieza
            db.session.execute(text("""
                CREATE TABLE IF NOT EXISTS molde_pieza (
                    id SERIAL PRIMARY KEY,
                    molde_id VARCHAR(50) REFERENCES molde(codigo),
                    pieza_sku VARCHAR(50) REFERENCES pieza(sku),
                    cavidades INTEGER NOT NULL DEFAULT 1,
                    peso_unitario_gr FLOAT NOT NULL,
                    UNIQUE(molde_id, pieza_sku)
                )
            """))
            print("✅ Tabla 'molde_pieza' creada o ya existe")
            
            # 3. Agregar campo tipo a pieza
            db.session.execute(text("""
                ALTER TABLE pieza 
                ADD COLUMN IF NOT EXISTS tipo VARCHAR(20) DEFAULT 'SIMPLE'
            """))
            print("✅ Campo 'tipo' agregado a 'pieza'")
            
            # 4. Crear tabla pieza_componente
            db.session.execute(text("""
                CREATE TABLE IF NOT EXISTS pieza_componente (
                    id SERIAL PRIMARY KEY,
                    kit_sku VARCHAR(50) REFERENCES pieza(sku),
                    componente_sku VARCHAR(50) REFERENCES pieza(sku),
                    cantidad INTEGER DEFAULT 1,
                    UNIQUE(kit_sku, componente_sku)
                )
            """))
            print("✅ Tabla 'pieza_componente' creada o ya existe")
            
            # 5. Agregar campo molde_id a orden_produccion
            db.session.execute(text("""
                ALTER TABLE orden_produccion 
                ADD COLUMN IF NOT EXISTS molde_id VARCHAR(50) REFERENCES molde(codigo)
            """))
            print("✅ Campo 'molde_id' agregado a 'orden_produccion'")
            
            db.session.commit()
            print("✅ Migración completada exitosamente")
            
            # Verificar tablas
            result = db.session.execute(text("""
                SELECT table_name FROM information_schema.tables 
                WHERE table_schema = 'public' 
                AND table_name IN ('molde', 'molde_pieza', 'pieza_componente')
            """))
            tablas = [r[0] for r in result.fetchall()]
            print(f"📋 Tablas verificadas: {tablas}")
                
        except Exception as e:
            print(f"❌ Error en migración: {e}")
            db.session.rollback()

if __name__ == "__main__":
    migrar()

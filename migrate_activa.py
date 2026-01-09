"""
Migración: Agregar campo 'activa' a OrdenProduccion
"""
from app import create_app
from app.extensions import db
from sqlalchemy import text

app = create_app()

def migrar():
    with app.app_context():
        print("🔄 Ejecutando migración: agregar columna 'activa' a orden_produccion...")
        
        try:
            # Agregar columna activa si no existe
            db.session.execute(text("""
                ALTER TABLE orden_produccion 
                ADD COLUMN IF NOT EXISTS activa BOOLEAN DEFAULT TRUE
            """))
            db.session.commit()
            print("✅ Columna 'activa' agregada correctamente (o ya existía)")
            
            # Verificar
            result = db.session.execute(text("""
                SELECT column_name FROM information_schema.columns 
                WHERE table_name = 'orden_produccion' AND column_name = 'activa'
            """))
            if result.fetchone():
                print("✅ Verificación exitosa: columna 'activa' existe en la tabla")
            else:
                print("❌ Error: columna no encontrada después de migración")
                
        except Exception as e:
            print(f"❌ Error en migración: {e}")
            db.session.rollback()

if __name__ == "__main__":
    migrar()

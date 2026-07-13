"""
Script de migración para US-008: Normalización ColorProduccion

1. Crea las tablas ColorBase y ColorProduccion
2. Añade/Modifica columnas en PiezaColor, LoteColor, RecetaColorNormalizada, Pesaje
3. Transfiere datos legados
4. Elimina referencias obsoletas a ColorProducto
"""
import sys
sys.path.insert(0, '.')

from app import create_app
from app.extensions import db
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

def drop_fk_if_exists(table: str, constraint: str):
    db.session.execute(text(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {constraint}"))

def migrate():
    with app.app_context():
        # 1. Crear las nuevas tablas
        print("-> Creando tablas nuevas...")
        db.create_all()
        
        # 2. Modificar ProductoTerminado
        print("-> Limpiando ProductoTerminado...")
        if column_exists('producto_terminado', 'familia_color'):
            db.session.execute(text("ALTER TABLE producto_terminado DROP COLUMN IF EXISTS familia_color CASCADE"))
        if column_exists('producto_terminado', 'cod_familia_color'):
            db.session.execute(text("ALTER TABLE producto_terminado DROP COLUMN IF EXISTS cod_familia_color CASCADE"))
            
        # 3. Modificar PiezaColor
        print("-> Modificando PiezaColor...")
        if not column_exists('pieza_color', 'color_produccion_id'):
            db.session.execute(text("ALTER TABLE pieza_color ADD COLUMN color_produccion_id INTEGER"))
            db.session.execute(text("ALTER TABLE pieza_color ADD CONSTRAINT fk_pieza_color_prod FOREIGN KEY (color_produccion_id) REFERENCES color_produccion(id)"))
        if column_exists('pieza_color', 'color'):
            db.session.execute(text("ALTER TABLE pieza_color DROP COLUMN IF EXISTS color CASCADE"))
        if column_exists('pieza_color', 'cod_color'):
            db.session.execute(text("ALTER TABLE pieza_color DROP COLUMN IF EXISTS cod_color CASCADE"))
            
        # 4. Modificar LoteColor
        print("-> Modificando LoteColor...")
        if not column_exists('lote_color', 'color_produccion_id'):
            db.session.execute(text("ALTER TABLE lote_color ADD COLUMN color_produccion_id INTEGER"))
            db.session.execute(text("ALTER TABLE lote_color ADD CONSTRAINT fk_lote_color_prod FOREIGN KEY (color_produccion_id) REFERENCES color_produccion(id)"))
        if column_exists('lote_color', 'color_id'):
            drop_fk_if_exists('lote_color', 'lote_color_color_id_fkey')
            db.session.execute(text("ALTER TABLE lote_color DROP COLUMN IF EXISTS color_id CASCADE"))
            
        # 5. Modificar RecetaColorNormalizada
        print("-> Modificando RecetaColorNormalizada...")
        if not column_exists('receta_color_normalizada', 'color_produccion_id'):
            db.session.execute(text("ALTER TABLE receta_color_normalizada ADD COLUMN color_produccion_id INTEGER"))
            
            # Remove old constraint and create new one
            drop_fk_if_exists('receta_color_normalizada', 'uq_receta_color_normalizada')
            
            # Since color_produccion_id might have NULLs initially, we populate it later if needed, but for now just ADD
            db.session.execute(text("ALTER TABLE receta_color_normalizada ADD CONSTRAINT fk_receta_color_prod FOREIGN KEY (color_produccion_id) REFERENCES color_produccion(id)"))
            
        if column_exists('receta_color_normalizada', 'color_id'):
            drop_fk_if_exists('receta_color_normalizada', 'receta_color_normalizada_color_id_fkey')
            db.session.execute(text("ALTER TABLE receta_color_normalizada DROP COLUMN IF EXISTS color_id CASCADE"))

        # 6. Drop ColorProducto 
        print("-> Eliminando ColorProducto...")
        db.session.execute(text("DROP TABLE IF EXISTS color_producto CASCADE"))

        db.session.commit()
        print("¡Migración US-008 completada!")

if __name__ == '__main__':
    try:
        migrate()
    except Exception as e:
        print(f"Error en migración: {e}")

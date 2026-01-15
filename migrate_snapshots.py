from app import create_app
from app.extensions import db
from sqlalchemy import text

app = create_app()

def run_migration():
    with app.app_context():
        # Mask password in print
        uri = app.config['SQLALCHEMY_DATABASE_URI']
        print(f"Migrating DB connected to: {uri.split('@')[-1] if '@' in uri else uri}")
        
        try:
            # Columns to rename
            # Format: (old_name, new_name)
            renames = [
                ('peso_unitario_gr', 'snapshot_peso_unitario_gr'),
                ('peso_inc_colada', 'snapshot_peso_inc_colada'),
                ('cavidades', 'snapshot_cavidades'),
                ('tiempo_ciclo', 'snapshot_tiempo_ciclo'),
                ('horas_turno', 'snapshot_horas_turno')
            ]
            
            # Using raw SQL for schema change
            for old, new in renames:
                try:
                    print(f"Attempting rename: {old} -> {new}")
                    sql = text(f"ALTER TABLE orden_produccion RENAME COLUMN {old} TO {new}")
                    db.session.execute(sql)
                    print(f"✅ Success: {old} -> {new}")
                except Exception as e:
                    # Capture specific error if column missing (already migrated)
                    err = str(e).lower()
                    if "no such column" in err or "column" in err and "does not exist" in err:
                         print(f"⚠️ Skipped {old} (Might not exist or already renamed). Error: {e}")
                    else:
                         print(f"❌ Error renaming {old}: {e}")
                         raise e

            db.session.commit()
            print("Migration process finished.")
            
        except Exception as e:
            print(f"Migration rolled back due to fatal error: {e}")
            db.session.rollback()

if __name__ == '__main__':
    run_migration()

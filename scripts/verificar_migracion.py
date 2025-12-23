import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import create_app
from app.models.producto import ProductoTerminado, Pieza
from app.extensions import db

def verificar():
    app = create_app()
    with app.app_context():
        total_pt = db.session.query(ProductoTerminado).count()
        total_piezas = db.session.query(Pieza).count()
        
        # Piezas con FK asignada
        piezas_con_fk = db.session.query(Pieza).filter(Pieza.producto_terminado_id != None).count()
        
        print(f"Total Productos Terminados: {total_pt}")
        print(f"Total Piezas: {total_piezas}")
        print(f"Piezas con enlace FK exitoso: {piezas_con_fk} ({piezas_con_fk/total_piezas*100:.1f}%)")
        
        if piezas_con_fk < total_piezas:
            print("\n[ALERTA] Algunas piezas no encontraron su Producto Padre por nombre.")
            print("Ejemplos de piezas sin enlace:")
            sin_fk = db.session.query(Pieza).filter(Pieza.producto_terminado_id == None).limit(5).all()
            for p in sin_fk:
                print(f" - Pieza SKU: {p.sku} (Sin enlace PT)")

        print("\nEjemplo de enlace:")
        ejemplo = db.session.query(Pieza).filter(Pieza.producto_terminado_id != None).first()
        if ejemplo:
            print(f" - Pieza: {ejemplo.sku} ({ejemplo.piezas})")
            print(f"   -> Enlazada a PT SKU: {ejemplo.producto_terminado_id}")
            pt = db.session.get(ProductoTerminado, ejemplo.producto_terminado_id)
            print(f"   -> Nombre PT: {pt.producto}")
            
            # Verificar generación de SKU
            print(f"   -> SKU Generado Python: {ejemplo.generar_sku()}")
            print(f"   -> SKU Excel (DB):      {ejemplo.sku}")
            assert ejemplo.generar_sku() == ejemplo.sku, "SKU Generado no coincide!"

if __name__ == "__main__":
    verificar()

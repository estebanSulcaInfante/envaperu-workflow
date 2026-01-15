from app import create_app, db
from app.models.registro import RegistroDiarioProduccion

app = create_app()

with app.app_context():
    registros = RegistroDiarioProduccion.query.all()
    print(f"Encontrados {len(registros)} registros. Recalculando...")
    
    count = 0
    for r in registros:
        old_kg = r.total_kg_real
        # Debug info
        print(f"DEBUG Reg {r.id}: OrdenID={r.orden_id}, OrdenObj={r.orden}")
        if r.orden:
             print(f"   -> Orden PesoTiro: {r.orden.peso_inc_colada}")
             
        r.actualizar_totales()
        print(f"   -> New Kg: {r.total_kg_real}")
        # Imprimir solo si hubo cambio significativo > 0.001
        if abs((old_kg or 0) - (r.total_kg_real or 0)) > 0.001:
            print(f"Registro {r.id}: {old_kg} kg -> {r.total_kg_real:.2f} kg (Coladas: {r.total_coladas_calculada})")
            count += 1
            
    db.session.commit()
    print(f"Actualizados {count} registros con cambios.")

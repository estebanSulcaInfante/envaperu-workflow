from app import create_app, db
from app.models.orden import OrdenProduccion
from app.models.molde import Molde

app = create_app()

with app.app_context():
    print("--- Moldes Disponibles ---")
    moldes = Molde.query.all()
    target_molde_id = None
    for m in moldes:
        print(f"{m.codigo}: {m.nombre}")
        if "ROMANO" in m.nombre.upper():
            target_molde_id = m.codigo
            
    op = OrdenProduccion.query.get('OP-1322')
    if op:
        print(f"\nUPDATING OP-1322: Current MoldeID={op.molde_id}")
        
        # If we found a likely match and current is null, assign it
        if not op.molde_id and target_molde_id:
            print(f"Assigning Molde {target_molde_id} to OP.")
            op.molde_id = target_molde_id
            op.molde_ref = Molde.query.get(target_molde_id) # Ensure relationship loads
        
        # KEY STEP: Recalculate metrics
        op.actualizar_metricas()
        
        db.session.commit()
        print("Updated metrics.")
        
        # Print results of update
        print(f"Calculo Peso Produccion: {op.calculo_peso_produccion}")
        print(f"Calculo Merma %: {op.calculo_merma_pct}")
        print(f"Calculo Peso Entregar: {op.calculo_peso_real_entregar}")
    else:
        print("OP not found")

from app import create_app
from app.models.molde import Molde

app = create_app()
with app.app_context():
    moldes = Molde.query.all()
    print(f"Total Moldes en Catálogo: {len(moldes)}")
    if not moldes:
        print("El catálogo de moldes está VACÍO.")
    else:
        for m in moldes:
            print(f"[{m.codigo}] {m.nombre} (Activo: {m.activo})")

from app import create_app, db
from app.models.orden import OrdenProduccion
import json

app = create_app()

with app.app_context():
    op = OrdenProduccion.query.get('OP-1322')
    if op:
        # Dump all attrs
        data = op.__dict__
        # Clean internal state
        data.pop('_sa_instance_state', None)
        print("Datos de OP-1322:")
        for k, v in data.items():
            print(f"{k}: {v}")
    else:
        print("OP-1322 no encontrada")

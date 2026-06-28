from dotenv import load_dotenv
load_dotenv()

from app import create_app
from app.extensions import db
from app.models.producto import Linea, Familia

app = create_app()

with app.app_context():
    print("Verificando Lineas...")
    lineas_data = [
        {'codigo': 1, 'nombre': 'HOGAR'},
        {'codigo': 2, 'nombre': 'JUGUETES'},
        {'codigo': 3, 'nombre': 'INDUSTRIAL'},
    ]
    
    for ld in lineas_data:
        existente = Linea.query.filter_by(codigo=ld['codigo']).first()
        if not existente:
            db.session.add(Linea(**ld))
            print(f"Agregado Linea: {ld['nombre']}")
    
    print("Verificando Familias...")
    familias_data = [
        {'codigo': 14, 'nombre': 'PLAYEROS'},
        {'codigo': 15, 'nombre': 'BALDES'},
        {'codigo': 10, 'nombre': 'JARRAS'},
        {'codigo': 11, 'nombre': 'TAZONES'},
        {'codigo': 20, 'nombre': 'TINAS'}
    ]
    
    for fd in familias_data:
        existente = Familia.query.filter_by(codigo=fd['codigo']).first()
        if not existente:
            db.session.add(Familia(**fd))
            print(f"Agregado Familia: {fd['nombre']}")

    db.session.commit()
    print("Semillas de Lineas y Familias plantadas correctamente.")

import sys
import os
import json
from datetime import datetime

# Set up the path to import app
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import create_app
from app.extensions import db
from app.models.kardex import InventarioManga

# Forzar sqlite temporal
os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
os.environ['TESTING'] = 'True'

# Create a minimal app context for testing
app = create_app()

# Use an in-memory database for testing just the kardex route
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
app.config['TESTING'] = True

# Use an in-memory database for testing just the kardex route
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
app.config['TESTING'] = True

with app.app_context():
    # Solo crear las tablas de kardex para que no chille por otras dependencias
    db.create_all()

    client = app.test_client()

    print("===== PRUEBA: ENTRADA (INGRESO-PROD) =====")
    payload_ingreso = {
        "codigo_qr": "1001;MOLDE_TEST;MAQ1;OP100;TURNO1;2026-03-13;1;USER;ROJO;2026-03-13/10:00:00;5.5;SKU123;Pieza Test",
        "tipo_operacion": "INGRESO-PROD",
        "locacion_origen": "ZONA_PRODUCCION",
        "locacion_destino": "ALMACEN_PRINCIPAL",
        "operario_id": "user@test.com"
    }
    res = client.post('/api/kardex/movimientos', json=payload_ingreso)
    print(f"Status: {res.status_code}")
    print(json.dumps(res.get_json(), indent=2))
    
    print("\n===== PRUEBA: ENTRADA DUPLICADA =====")
    res_dup = client.post('/api/kardex/movimientos', json=payload_ingreso)
    print(f"Status: {res_dup.status_code}")
    print(json.dumps(res_dup.get_json(), indent=2))

    print("\n===== PRUEBA: MOVIMIENTO (MOV-INTERNO) =====")
    payload_mov = {
        "codigo_qr": "1001;MOLDE_TEST;MAQ1;OP100;TURNO1;2026-03-13;1;USER;ROJO;2026-03-13/10:00:00;5.5;SKU123;Pieza Test",
        "tipo_operacion": "MOV-INTERNO",
        "locacion_origen": "ALMACEN_PRINCIPAL",
        "locacion_destino": "ZONA_DESPACHO",
        "operario_id": "user@test.com"
    }
    res_mov = client.post('/api/kardex/movimientos', json=payload_mov)
    print(f"Status: {res_mov.status_code}")
    print(json.dumps(res_mov.get_json(), indent=2))

    print("\n===== PRUEBA: SALIDA (SAL-ARMAR) =====")
    payload_salida = {
        "codigo_qr": "1001;MOLDE_TEST;MAQ1;OP100;TURNO1;2026-03-13;1;USER;ROJO;2026-03-13/10:00:00;5.5;SKU123;Pieza Test",
        "tipo_operacion": "SAL-ARMAR",
        "locacion_origen": "ZONA_DESPACHO",
        "locacion_destino": "",
        "operario_id": "user@test.com"
    }
    res_sal = client.post('/api/kardex/movimientos', json=payload_salida)
    print(f"Status: {res_sal.status_code}")
    print(json.dumps(res_sal.get_json(), indent=2))

    print("\n===== PRUEBA: CONSULTAR INVENTARIO =====")
    res_inv = client.get('/api/kardex/inventario')
    print(f"Status: {res_inv.status_code}")
    print(json.dumps(res_inv.get_json(), indent=2))

    print("\n===== PRUEBA: CONSULTAR MANGA (HISTORIAL) =====")
    res_manga = client.get('/api/kardex/manga/1001')
    print(f"Status: {res_manga.status_code}")
    print(json.dumps(res_manga.get_json(), indent=2))

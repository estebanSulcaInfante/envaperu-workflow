import pytest
from app.models.orden import OrdenProduccion
from app.models.lote import LoteColor
from app.models.materiales import MateriaPrima, Colorante
from app.extensions import db

def test_crear_orden_post(client, app): # Removed db argument
    """
    Prueba la creación de una orden completa vía POST.
    """
    with app.app_context():
        # Setup catalog
        mp = MateriaPrima(nombre="TEST MP", tipo="VIRGEN")
        col = Colorante(nombre="TEST COL")
        db.session.add_all([mp, col])
        db.session.commit()
        mp_id = mp.id
        col_id = col.id

    payload = {
        "numero_op": "OP-API-TEST",
        "maquina_id": "M1",
        "tipo_maquina": "T1",
        "producto": "P1",
        "tipo_estrategia": "POR_PESO",
        "meta_total_kg": 100.0,
        "peso_unitario_gr": 10.0,
        "peso_inc_colada": 12.0,
        "cavidades": 2,
        "tiempo_ciclo": 10.0,
        "horas_turno": 8.0,
        
        "lotes": [
            {
                "color_nombre": "ROJO",
                "personas": 3,
                "materiales": [
                    {"nombre": "TEST MP", "tipo": "VIRGEN", "fraccion": 1.0}
                ],
                "pigmentos": [
                    {"nombre": "TEST COL", "gramos": 15.0}
                ]
            }
        ]
    }

    response = client.post('/api/ordenes', json=payload)
    if response.status_code != 201:
        print(f"\nResponse Error: {response.get_json()}")
    
    assert response.status_code == 201
    data = response.get_json()
    assert data['numero_op'] == "OP-API-TEST"
    assert len(data['lotes']) == 1
    
    lote = data['lotes'][0]
    assert lote['Color'] == "ROJO" # Keys updated in previous step
    assert lote['mano_obra']['personas'] == 3
    assert len(lote['materiales']) == 1
    assert lote['materiales'][0]['fraccion'] == 1.0
    
    # Verify DB persistence
    with app.app_context():
        orden_db = OrdenProduccion.query.filter_by(numero_op="OP-API-TEST").first()
        assert orden_db is not None
        assert len(orden_db.lotes) == 1
        lote_db = orden_db.lotes[0]
        assert len(lote_db.materias_primas) == 1
        assert len(lote_db.colorantes) == 1

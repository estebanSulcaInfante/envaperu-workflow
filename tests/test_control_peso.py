import pytest
from app.models.orden import OrdenProduccion, SnapshotComposicionMolde
from app.models.registro import RegistroDiarioProduccion
from app.models.control_peso import ControlPeso
from app.extensions import db
from datetime import datetime, date


def test_control_peso_flow(client, app):
    """
    Verifica el flujo completo de Control de Peso:
    1. Crear Orden (con composición manual)
    2. Crear Registro Diario
    3. Agregar Bultos (Pesaje)
    4. Validar Totales (Endpoint de validacion)
    5. Listar y Eliminar Bultos

    Molde: 2 cav × 50g + 10g colada = 110g tiro
    100 coladas → 100 × (50g×2) / 1000 = 10.0 Kg neto
    """
    # 0. Seed Required Data
    from app.models.maquina import Maquina
    with app.app_context():
        db.session.add(Maquina(id=1, nombre="Inyectora 1", tipo="INYECTORA"))
        db.session.commit()

    op_num = "OP-TEST-PESO-01"

    # 1a. Crear Orden — snapshot_composicion manual
    orden_data = {
        "numero_op":          op_num,
        "producto":           "Producto Test Peso",
        "tipo_estrategia":    "POR_CANTIDAD",
        "meta_total_doc":     100,
        "maquina_id":         1,
        "snapshot_tiempo_ciclo":    20.0,
        "snapshot_horas_turno":     8.0,
        "snapshot_peso_colada_gr":  10.0,   # 10g ramal

        # Composición: 2 cavidades × 50g = 100g neto; tiro = 110g
        "snapshot_composicion": [
            {"cavidades": 2, "peso_unit_gr": 50.0}
        ]
    }

    resp = client.post('/api/ordenes', json=orden_data)
    if resp.status_code != 201:
        print(f"\nERROR CREATING ORDEN: {resp.get_json()}")
    assert resp.status_code == 201

    # 1b. Crear Registro Diario
    registro_data = {
        "maquina_id":    1,
        "fecha":         date.today().isoformat(),
        "turno":         "DIA",
        "hora_inicio":   "08:00",
        "colada_inicial": 100,
        "colada_final":   200,   # 100 coladas
        "tiempo_ciclo":  20.0,
        "detalles":      []
    }

    resp_reg = client.post(f'/api/ordenes/{op_num}/registros', json=registro_data)
    assert resp_reg.status_code == 201
    reg_id = resp_reg.json['id']

    # 2. Agregar Bultos
    resp_b1 = client.post(f'/api/registros/{reg_id}/bultos', json={"peso": 5.5, "color": "ROJO"})
    assert resp_b1.status_code == 201

    resp_b2 = client.post(f'/api/registros/{reg_id}/bultos', json={"peso": 5.5, "color": "ROJO"})
    assert resp_b2.status_code == 201

    # 3. Listar Bultos
    resp_list = client.get(f'/api/registros/{reg_id}/bultos')
    assert resp_list.status_code == 200
    assert len(resp_list.json) == 2
    assert resp_list.json[0]['peso_real_kg'] == 5.5

    # 4. Validar Peso
    # Teórico: 100 coladas × (100g neto + 10g colada) / 1000 = 11.0 Kg
    # Pesado:  5.5 + 5.5 = 11.0 Kg
    resp_val = client.get(f'/api/registros/{reg_id}/validacion-peso')
    assert resp_val.status_code == 200
    data_val = resp_val.json

    assert data_val['total_pesado_kg'] == 11.0
    assert data_val['peso_teorico_kg'] == 11.0
    assert data_val['coincide'] == True

    # 5. Caso de Fallo (Bulto extra)
    resp_b3 = client.post(f'/api/registros/{reg_id}/bultos', json={"peso": 20.0, "color": "EXTRA"})
    resp_val_fail = client.get(f'/api/registros/{reg_id}/validacion-peso')
    assert resp_val_fail.json['coincide'] == False
    assert resp_val_fail.json['total_pesado_kg'] == 31.0

    # 6. Eliminar Bulto y verificar que coincide nuevamente
    b3_id = resp_b3.json['id']
    client.delete(f'/api/bultos/{b3_id}')

    resp_val_back = client.get(f'/api/registros/{reg_id}/validacion-peso')
    assert resp_val_back.json['coincide'] == True

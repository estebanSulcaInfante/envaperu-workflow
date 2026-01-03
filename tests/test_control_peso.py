import pytest
from app.models.orden import OrdenProduccion
from app.models.registro import RegistroDiarioProduccion
from app.models.control_peso import ControlPeso
from app.extensions import db
from datetime import datetime, date

def test_control_peso_flow(client, app):
    """
    Verifica el flujo completo de Control de Peso:
    1. Crear Orden
    2. Crear Registro Diario
    3. Agregar Bultos (Pesaje)
    4. Validar Totales (Endpoint de validacion)
    5. Listar y Eliminar Bultos
    """
    
    # 0. Seed Required Data (Maquinas, etc.)
    from app.models.maquina import Maquina
    db.session.add(Maquina(id=1, nombre="Inyectora 1", tipo="INYECTORA"))
    db.session.commit()
    
    # 1. Setup: Crear Orden y Registro
    op_num = "OP-TEST-PESO-01"
    
    # Payload Orden
    orden_data = {
        "numero_op": op_num,
        "producto": "Producto Test Peso",
        "tipo_estrategia": "POR_CANTIDAD",
        "meta_total_doc": 100,
        "peso_unitario_gr": 50.0, # 50g por pieza
        "peso_inc_colada": 60.0,  # 60g tiro
        "cavidades": 2,
        "maquina_id": 1
    }
    
    resp = client.post('/api/ordenes', json=orden_data)
    if resp.status_code != 201:
        print(f"\nERROR CREATING ORDEN: {resp.get_json()}")
    assert resp.status_code == 201
    
    # Payload Registro
    registro_data = {
        "maquina_id": 1,
        "fecha": date.today().isoformat(),
        "turno": "DIA",
        "hora_inicio": "08:00",
        "colada_inicial": 100,
        "colada_final": 200, # 100 coladas hechas
        "tiempo_ciclo": 20.0,
        "detalles": []
    }
    
    resp_reg = client.post(f'/api/ordenes/{op_num}/registros', json=registro_data)
    assert resp_reg.status_code == 201
    reg_id = resp_reg.json['id']
    
    # CALCULOS ESPERADOS:
    # 100 coladas * 60g tiro = 6000g = 6.0 Kg (Peso Tiro Total)
    # Sin embargo, el registro calcula 'total_kg_real' basado en (Coladas * (PesoNeto * Cav + PesoColada?))
    # En registro.py: total_kg_real = (coladas * peso_tiro_gr) / 1000
    # Peso tiro = (50 * 2) + 0 (colada extra) = 100g (Si colada_gr es 0 en snapshot)
    # Revisemos logica de creacion registro: snapshot_peso_colada_gr=0.0 default.
    # Entonces: 100 * (50*2) / 1000 = 10.0 Kg.
    
    # 2. Agregar Bultos
    # Vamos a simular que pesamos 2 bultos que suman 10kg
    bulto1 = {"peso": 4.5, "color": "ROJO"}
    bulto2 = {"peso": 5.5, "color": "ROJO"}
    
    resp_b1 = client.post(f'/api/registros/{reg_id}/bultos', json=bulto1)
    assert resp_b1.status_code == 201
    
    resp_b2 = client.post(f'/api/registros/{reg_id}/bultos', json=bulto2)
    assert resp_b2.status_code == 201
    
    # 3. Listar Bultos
    resp_list = client.get(f'/api/registros/{reg_id}/bultos')
    assert resp_list.status_code == 200
    assert len(resp_list.json) == 2
    assert resp_list.json[0]['peso_real_kg'] == 4.5
    
    # 4. Validar Peso (Endpoint de comparacion)
    resp_val = client.get(f'/api/registros/{reg_id}/validacion-peso')
    assert resp_val.status_code == 200
    data_val = resp_val.json
    
    # Esperamos:
    # Teorico: 10.0 kg (100 coladas * 100g/tiro / 1000)
    # Pesado: 4.5 + 5.5 = 10.0 kg
    # Diferencia: 0
    # Coincide: True
    
    assert data_val['total_pesado_kg'] == 10.0
    # Nota: Validar el teorico exacto depende de como se guardo el snapshot. 
    # En el POST de registro, snapshot_peso_neto = orden.peso_unitario (50). Cav = 2.
    # Peso tiro = 100g. 100 coladas = 10000g = 10kg.
    assert data_val['peso_teorico_kg'] == 10.0 
    assert data_val['coincide'] == True
    
    # 5. Caso de Fallo (Bulto extra)
    resp_b3 = client.post(f'/api/registros/{reg_id}/bultos', json={"peso": 20.0, "color": "EXTRA"})
    resp_val_fail = client.get(f'/api/registros/{reg_id}/validacion-peso')
    assert resp_val_fail.json['coincide'] == False
    assert resp_val_fail.json['total_pesado_kg'] == 30.0
    
    # 6. Eliminar Bulto
    b3_id = resp_b3.json['id']
    client.delete(f'/api/bultos/{b3_id}')
    
    resp_val_back = client.get(f'/api/registros/{reg_id}/validacion-peso')
    assert resp_val_back.json['coincide'] == True
    

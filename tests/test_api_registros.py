import pytest
from datetime import datetime, timezone
from app.models.orden import OrdenProduccion
from app.models.registro import RegistroDiarioProduccion
from app.models.maquina import Maquina
from app.extensions import db

def test_listar_registros_json(client, app):
    """
    Verifica el endpoint GET /ordenes/<op>/registros
    Debe retornar una lista de objetos con las claves exactas requeridas para la vista Excel.
    """
    with app.app_context():
        # Setup Maquina
        maq = Maquina(nombre="MAQ-API", tipo="HYBRID")
        db.session.add(maq)
        db.session.commit()
        
        # Setup Orden
        orden = OrdenProduccion(
            numero_op="OP-API-REG",
            maquina_id=maq.id,
            tipo_estrategia="POR_PESO",
            peso_unitario_gr=50.0,
            tiempo_ciclo=20.0,
            cavidades=4
        )
        db.session.add(orden)
        db.session.commit()
        
        # Setup Registro
        reg = RegistroDiarioProduccion(
            orden_id=orden.numero_op,
            maquina_id=maq.id,
            fecha=datetime.now(timezone.utc).date(),
            turno="NOCHE",
            maquinista="TESTER",
            coladas=100,
            snapshot_cavidades=4,
            snapshot_peso_unitario_gr=50.0,
            snapshot_ciclo_seg=20.0
        )
        reg.actualizar_metricas()
        db.session.add(reg)
        db.session.commit()
        
        # Call Endpoint
        response = client.get(f'/api/ordenes/{orden.numero_op}/registros')
        assert response.status_code == 200
        
        data = response.get_json()
        assert isinstance(data, list)
        assert len(data) == 1
        
        row = data[0]
        
        # Verify Key Columns
        expected_keys = [
            "Hora de Ingreso", "Tipo Maq", "Maquina", "FECHA", "MES", "AÑO", "SEMANA",
            "Maquinista", "Turno", "Molde", "Pieza-Color", "Nº OP", 
            "Coladas", "Horas Trab.", "Peso Real (Kg)",
            "Peso Aprox. (Kg)", "Peso (kg)", "Peso unitario (Gr)", "Cantidad Real",
            "DOC", "Produccion esperada", 
            "Cavidades", "Kg Virgen", "Kg Segunda",
            "Cavidades SKU", "Ciclo SKU", "Peso Unit SKU"
        ]
        
        for k in expected_keys:
            assert k in row, f"Falta la columna {k} en la respuesta JSON"
            
        # Verify Values
        assert row['Maquina'] == "MAQ-API"
        assert row['Tipo Maq'] == "HYBRID"
        assert row['Nº OP'] == "OP-API-REG"
        assert row['Turno'] == "NOCHE"
        # Check calculation: DOC = Cavs * Coladas = 4 * 100 = 400
        assert row['DOC'] == 400.0

def test_crear_registro_api(client, app):
    """
    Verifica el endpoint POST /ordenes/<op>/registros
    """
    with app.app_context():
        # Setup Maquina
        maq = Maquina(nombre="MAQ-POST", tipo="INYECCION")
        db.session.add(maq)
        db.session.commit()
        
        # Setup Orden
        orden = OrdenProduccion(
            numero_op="OP-POST-REG",
            maquina_id=maq.id,
            tipo_estrategia="POR_PESO",
            peso_unitario_gr=100.0,
            tiempo_ciclo=30.0,
            cavidades=2
        )
        db.session.add(orden)
        db.session.commit()
        
        # Payload
        payload = {
            "maquina_id": maq.id,
            "fecha": "2025-12-23",
            "turno": "TARDE",
            "hora_ingreso": "14:00",
            "maquinista": "OPERARIO POST",
            "molde": "MOLDE-X",
            "pieza_color": "PIEZA-ROJA",
            "coladas": 50,
            "horas_trabajadas": 4.0,
            "peso_real_kg": 9.8
        }
        
        # Call Endpoint
        response = client.post(f'/api/ordenes/{orden.numero_op}/registros', json=payload)
        assert response.status_code == 201
        
        data = response.get_json()
        
        # Verify Response Structure
        assert data['maquinista'] == "OPERARIO POST"
        assert 'calculos' in data
        
        # Verify Calculations
        # DOC = Cavs * Coladas = 2 * 50 = 100
        assert data['calculos']['doc_cantidad'] == 100.0
        
        # Peso Aprox = (P.Unit * Cavs * Coladas) / 1000
        # (100 * 2 * 50) / 1000 = 10.0 kg
        assert data['calculos']['peso_aprox'] == 10.0
        
        # Verify DB Persistence
        reg_db = RegistroDiarioProduccion.query.filter_by(orden_id="OP-POST-REG").first()
        assert reg_db is not None
        assert reg_db.peso_real_kg == 9.8

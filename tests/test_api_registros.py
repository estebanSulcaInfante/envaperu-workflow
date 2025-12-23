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

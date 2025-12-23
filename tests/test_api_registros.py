import pytest
from app import create_app
from app.extensions import db
from app.models.orden import OrdenProduccion
from app.models.registro import RegistroDiarioProduccion, DetalleProduccionHora
from app.models.maquina import Maquina
from datetime import date

@pytest.fixture
def app():
    app = create_app()
    app.config.update({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"
    })
    
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()

@pytest.fixture
def client(app):
    return app.test_client()

def test_listar_registros_json(client, app):
    """
    Verifica que el endpoint GET retorne la estructura jerárquica correcta.
    """
    with app.app_context():
        # Setup Maquina
        maq = Maquina(nombre="MAQ-API-REG", tipo="INYECCION")
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
        
        # Setup Registro (Header)
        reg = RegistroDiarioProduccion(
            orden_id=orden.numero_op,
            maquina_id=maq.id,
            fecha=date(2025, 1, 15),
            turno="NOCHE",
            hora_inicio="19:00",
            colada_inicial=1000,
            colada_final=1100, # 100 calc
            
            # Snapshots
            snapshot_cavidades=orden.cavidades,
            snapshot_peso_neto_gr=orden.peso_unitario_gr,
            snapshot_peso_colada_gr=10.0
        )
        reg.actualizar_totales()
        db.session.add(reg)
        db.session.flush()
        
        # Setup Detalles
        det = DetalleProduccionHora(
            registro_id=reg.id,
            hora="19:00",
            maquinista="TESTER",
            coladas_realizadas=100
        )
        det.calcular_metricas(reg.snapshot_cavidades, (reg.snapshot_peso_neto_gr * reg.snapshot_cavidades))
        db.session.add(det)
        db.session.commit()
        
        # Test GET
        response = client.get(f'/api/ordenes/{orden.numero_op}/registros')
        assert response.status_code == 200
        data = response.get_json()
        assert len(data) == 1
        
        row = data[0]
        assert row['Turno'] == "NOCHE"
        assert row['Total Coladas (Calc)'] == 100
        assert len(row['detalles']) == 1
        assert row['detalles'][0]['coladas'] == 100

def test_crear_registro_api(client, app):
    """
    Verifica el endpoint POST /ordenes/<op>/registros con estructura master-detail
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
            "hora_inicio": "14:00",
            "colada_inicial": 100,
            "colada_final": 200, # 100 coladas total
            "tiempo_ciclo": 30.0,
            "detalles": [
                {"hora": "14:00", "coladas": 50, "maquinista": "TESTER", "color": "ROJO"},
                {"hora": "15:00", "coladas": 50, "maquinista": "TESTER", "color": "ROJO"}
            ]
        }
        
        # Call Endpoint
        response = client.post(f'/api/ordenes/{orden.numero_op}/registros', json=payload)
        assert response.status_code == 201
        
        data = response.get_json()
        
        # Verify Response Structure (Header info)
        assert data['contadores']['total'] == 100
        assert len(data['detalles']) == 2
        
        # Verify Calculations
        # Piezas = Total Coladas * Cavidades = 100 * 2 = 200
        assert data['totales_estimados']['piezas'] == 200
        
        # Verify DB Persistence
        reg_db = RegistroDiarioProduccion.query.filter_by(orden_id="OP-POST-REG").first()
        assert reg_db is not None
        assert reg_db.total_coladas_calculada == 100
        assert len(reg_db.detalles) == 2

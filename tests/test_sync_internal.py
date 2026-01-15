import pytest
from app import create_app, db
from app.models.registro import RegistroDiarioProduccion
from app.models.control_peso import ControlPeso
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

def test_sync_pesajes_logic(client, app):
    with app.app_context():
        # Setup: Maquina
        maq = Maquina(nombre="INY-05", tipo="INYECTOR")
        db.session.add(maq)
        
        # Setup: Orden
        from app.models.orden import OrdenProduccion
        op = OrdenProduccion(
            numero_op='OP-TEST', 
            maquina_id=1, 
            producto="TEST", 
            molde="MOLDE TEST",
            tipo_estrategia='POR_PESO',
            meta_total_kg=100, 
            peso_unitario_gr=1, 
            peso_inc_colada=10
        )
        db.session.add(op)
        
        db.session.commit()
        
        payload = {
            'pesajes': [
                {
                    'local_id': 1,
                    'peso_kg': 10.5,
                    'fecha_hora': '2023-01-01T10:00:00',
                    'nro_op': 'OP-TEST',
                    'turno': 'DIURNO',
                    'fecha_ot': date.today().isoformat(),
                    'maquina': 'INY-05',
                    'color': 'ROJO'
                },
                {
                    'local_id': 2,
                    'peso_kg': 20.0,
                    'fecha_hora': '2023-01-01T11:00:00',
                    'nro_op': 'OP-TEST',
                    'turno': 'DIURNO',
                    'fecha_ot': date.today().isoformat(),
                    'maquina': 'INY-05',
                    'color': 'ROJO'
                }
            ]
        }
        
        # Act
        response = client.post('/api/sync/pesajes', json=payload)
        
        # Debug response if fail
        if response.status_code != 200:
            print(response.get_json())
            
        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] == True, f"Failed: {data.get('message')}"
        assert len(data['synced']) == 2
        
        # Assert DB
        rdp = RegistroDiarioProduccion.query.first()
        assert rdp is not None
        assert rdp.orden_id == 'OP-TEST'
        
        # EXACT CALCULATION Verification
        # 10.5 + 20.0 = 30.5
        assert abs(rdp.total_kg_real - 30.5) < 0.001, \
            f"Registration Total Kg incorrect. Got {rdp.total_kg_real}, Expected 30.5"
            
        print("\n✅ Internal Sync Logic Verified: Totals matched exactly.")

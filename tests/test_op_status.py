"""
Tests for OP Status (activa field) functionality
"""
import pytest
from app import create_app
from app.extensions import db
from app.models.orden import OrdenProduccion
from app.models.maquina import Maquina


@pytest.fixture
def app():
    """Create test app with test database"""
    app = create_app()
    app.config['TESTING'] = True
    
    with app.app_context():
        # Ensure tables exist
        db.create_all()
        yield app
        db.session.rollback()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def setup_test_data(app):
    """Create test data for OP status tests"""
    with app.app_context():
        # Create a machine
        maquina = Maquina.query.first()
        if not maquina:
            maquina = Maquina(nombre="Test Machine", tipo="INYECCION")
            db.session.add(maquina)
            db.session.commit()
        
        # Create test OP
        test_op = OrdenProduccion.query.get("TEST-STATUS-001")
        if not test_op:
            test_op = OrdenProduccion(
                numero_op="TEST-STATUS-001",
                producto="Producto Test Status",
                tipo_estrategia="POR_PESO",
                meta_total_kg=100.0,
                maquina_id=maquina.id,
                activa=True
            )
            db.session.add(test_op)
            db.session.commit()
        
        return {"op": test_op.numero_op, "maquina_id": maquina.id}


class TestOpStatus:
    """Test OP activa status functionality"""
    
    def test_op_created_with_activa_true(self, client, setup_test_data):
        """New OPs should be created with activa=True by default"""
        response = client.get(f"/api/ordenes/{setup_test_data['op']}")
        assert response.status_code == 200
        data = response.get_json()
        assert data['activa'] == True
    
    def test_toggle_estado_close_op(self, client, setup_test_data):
        """Should be able to close an OP"""
        response = client.put(
            f"/api/ordenes/{setup_test_data['op']}/estado",
            json={"activa": False}
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data['activa'] == False
        assert 'cerrada' in data['message'].lower()
    
    def test_toggle_estado_reopen_op(self, client, setup_test_data):
        """Should be able to reopen a closed OP"""
        # First close it
        client.put(
            f"/api/ordenes/{setup_test_data['op']}/estado",
            json={"activa": False}
        )
        # Then reopen
        response = client.put(
            f"/api/ordenes/{setup_test_data['op']}/estado",
            json={"activa": True}
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data['activa'] == True
        assert 'abierta' in data['message'].lower()
    
    def test_cannot_create_registro_for_closed_op(self, client, setup_test_data):
        """Should not be able to create registros for closed OPs"""
        # Close the OP
        client.put(
            f"/api/ordenes/{setup_test_data['op']}/estado",
            json={"activa": False}
        )
        
        # Try to create a registro
        response = client.post(
            f"/api/ordenes/{setup_test_data['op']}/registros",
            json={
                "fecha": "2026-01-08",
                "turno": "DIURNO",
                "maquina_id": setup_test_data['maquina_id']
            }
        )
        
        assert response.status_code == 400
        data = response.get_json()
        assert 'cerrada' in data['error'].lower()
    
    def test_can_create_registro_for_active_op(self, client, setup_test_data):
        """Should be able to create registros for active OPs"""
        # Ensure OP is active
        client.put(
            f"/api/ordenes/{setup_test_data['op']}/estado",
            json={"activa": True}
        )
        
        # Create a registro
        response = client.post(
            f"/api/ordenes/{setup_test_data['op']}/registros",
            json={
                "fecha": "2026-01-08",
                "turno": "NOCTURNO",
                "maquina_id": setup_test_data['maquina_id']
            }
        )
        
        # Should succeed (201) or fail for other reasons, but not because OP is closed
        if response.status_code == 400:
            data = response.get_json()
            assert 'cerrada' not in data.get('error', '').lower()
    
    def test_toggle_estado_missing_field(self, client, setup_test_data):
        """Should return error if activa field is missing"""
        response = client.put(
            f"/api/ordenes/{setup_test_data['op']}/estado",
            json={}
        )
        assert response.status_code == 400
    
    def test_toggle_estado_nonexistent_op(self, client):
        """Should return 404 for non-existent OP"""
        response = client.put(
            "/api/ordenes/NONEXISTENT-OP/estado",
            json={"activa": False}
        )
        assert response.status_code == 404


class TestRegistrosEndpoint:
    """Test the new /registros global endpoint"""
    
    def test_get_all_registros(self, client, app):
        """Should return all registros"""
        response = client.get("/api/registros")
        assert response.status_code == 200
        data = response.get_json()
        assert isinstance(data, list)
    
    def test_get_registros_with_limit(self, client, app):
        """Should respect limit parameter"""
        response = client.get("/api/registros?limit=5")
        assert response.status_code == 200
        data = response.get_json()
        assert len(data) <= 5


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

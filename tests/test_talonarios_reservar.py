"""
Tests para el endpoint /reservar de talonarios (bulk reservation)
"""
import pytest
from app import create_app
from app.extensions import db
from app.models.talonario import Talonario


@pytest.fixture
def app():
    app = create_app()
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    app.config['TESTING'] = True
    
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


class TestReservarEndpoint:
    """Tests del endpoint /reservar para cache offline"""
    
    def test_reservar_100_correlativos(self, client, app):
        """Reservar 100 correlativos de un talonario grande"""
        with app.app_context():
            t = Talonario(desde=30001, hasta=30500, activo=True)
            db.session.add(t)
            db.session.commit()
        
        response = client.post('/api/talonarios/reservar', json={'cantidad': 100})
        
        assert response.status_code == 200
        data = response.get_json()
        assert len(data['correlativos']) == 100
        assert data['correlativos'][0] == 30001
        assert data['correlativos'][99] == 30100
    
    def test_reservar_cruza_talonarios(self, client, app):
        """Reservar correlativos que cruzan dos talonarios"""
        with app.app_context():
            t1 = Talonario(desde=30001, hasta=30010, activo=True)  # 10 disponibles
            t2 = Talonario(desde=30011, hasta=30100, activo=True)  # 90 disponibles
            db.session.add_all([t1, t2])
            db.session.commit()
        
        response = client.post('/api/talonarios/reservar', json={'cantidad': 20})
        
        assert response.status_code == 200
        data = response.get_json()
        assert len(data['correlativos']) == 20
        # Primeros 10 del primer talonario
        assert 30001 in data['correlativos']
        assert 30010 in data['correlativos']
        # Siguientes 10 del segundo talonario
        assert 30011 in data['correlativos']
        assert 30020 in data['correlativos']
    
    def test_reservar_sin_talonarios(self, client):
        """Intentar reservar sin talonarios disponibles"""
        response = client.post('/api/talonarios/reservar', json={'cantidad': 100})
        
        assert response.status_code == 404
        data = response.get_json()
        assert data['cantidad'] == 0
    
    def test_reservar_max_500(self, client, app):
        """Verificar límite máximo de 500"""
        with app.app_context():
            t = Talonario(desde=30001, hasta=31000, activo=True)
            db.session.add(t)
            db.session.commit()
        
        response = client.post('/api/talonarios/reservar', json={'cantidad': 1000})
        
        assert response.status_code == 200
        data = response.get_json()
        assert len(data['correlativos']) == 500  # Máximo permitido

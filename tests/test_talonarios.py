"""
Tests para Talonario RDP
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


class TestTalonarioModel:
    """Tests del modelo Talonario"""
    
    def test_crear_talonario(self, app):
        with app.app_context():
            t = Talonario(desde=30001, hasta=30100)
            db.session.add(t)
            db.session.commit()
            
            assert t.total == 100
            assert t.usados == 0
            assert t.disponibles == 100
            assert t.siguiente == 30001
    
    def test_consumir_correlativo(self, app):
        with app.app_context():
            t = Talonario(desde=30001, hasta=30005)
            db.session.add(t)
            db.session.commit()
            
            # Primer consumo
            c1 = t.consumir()
            assert c1 == 30001
            assert t.ultimo_usado == 30001
            assert t.usados == 1
            assert t.siguiente == 30002
            
            # Segundo consumo
            c2 = t.consumir()
            assert c2 == 30002
            assert t.usados == 2
    
    def test_talonario_agotado(self, app):
        with app.app_context():
            t = Talonario(desde=30001, hasta=30002)
            db.session.add(t)
            db.session.commit()
            
            t.consumir()  # 30001
            t.consumir()  # 30002
            
            assert t.disponibles == 0
            assert t.siguiente is None
            
            # Intentar consumir en talonario agotado
            resultado = t.consumir()
            assert resultado is None
    
    def test_porcentaje_uso(self, app):
        with app.app_context():
            t = Talonario(desde=30001, hasta=30010)
            db.session.add(t)
            db.session.commit()
            
            assert t.porcentaje_uso == 0
            
            t.consumir()  # 1 de 10
            assert t.porcentaje_uso == 10.0
            
            for _ in range(4):  # 5 de 10
                t.consumir()
            assert t.porcentaje_uso == 50.0


class TestTalonarioAPI:
    """Tests de la API de Talonarios"""
    
    def test_crear_talonario_api(self, client):
        response = client.post('/api/talonarios', json={
            'desde': 30001,
            'hasta': 30100,
            'descripcion': 'Lote Enero 2026'
        })
        
        assert response.status_code == 201
        data = response.get_json()
        assert data['desde'] == 30001
        assert data['hasta'] == 30100
        assert data['total'] == 100
    
    def test_listar_talonarios(self, client, app):
        with app.app_context():
            t = Talonario(desde=30001, hasta=30100)
            db.session.add(t)
            db.session.commit()
        
        response = client.get('/api/talonarios')
        assert response.status_code == 200
        data = response.get_json()
        assert len(data) == 1
    
    def test_obtener_siguiente(self, client, app):
        with app.app_context():
            t = Talonario(desde=30001, hasta=30100, activo=True)
            db.session.add(t)
            db.session.commit()
        
        response = client.get('/api/talonarios/siguiente')
        assert response.status_code == 200
        data = response.get_json()
        assert data['siguiente'] == 30001
    
    def test_consumir_correlativo_api(self, client, app):
        with app.app_context():
            t = Talonario(desde=30001, hasta=30100, activo=True)
            db.session.add(t)
            db.session.commit()
        
        # Primer consumo
        response = client.post('/api/talonarios/consumir')
        assert response.status_code == 200
        data = response.get_json()
        assert data['correlativo'] == 30001
        
        # Segundo consumo
        response = client.post('/api/talonarios/consumir')
        data = response.get_json()
        assert data['correlativo'] == 30002
    
    def test_solapamiento_talonarios(self, client):
        # Crear primer talonario
        client.post('/api/talonarios', json={'desde': 30001, 'hasta': 30100})
        
        # Intentar crear talonario solapado
        response = client.post('/api/talonarios', json={'desde': 30050, 'hasta': 30150})
        
        assert response.status_code == 400
        assert 'solapa' in response.get_json()['error']
    
    def test_no_eliminar_talonario_con_uso(self, client, app):
        with app.app_context():
            t = Talonario(desde=30001, hasta=30100, activo=True)
            db.session.add(t)
            db.session.commit()
            
            t.consumir()
            db.session.commit()
            tid = t.id
        
        response = client.delete(f'/api/talonarios/{tid}')
        assert response.status_code == 400

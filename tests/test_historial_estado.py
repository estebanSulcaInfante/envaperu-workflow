"""
Tests para HistorialEstadoOrden - tracking de cambios de estado en órdenes.
"""
import pytest
from app import create_app
from app.extensions import db
from app.models.orden import OrdenProduccion
from app.models.historial_estado import HistorialEstadoOrden, registrar_cambio_estado


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


class TestHistorialEstadoModel:
    """Tests del modelo HistorialEstadoOrden"""
    
    def test_registrar_cierre(self, app):
        """Registrar cierre de orden"""
        with app.app_context():
            orden = OrdenProduccion(
                numero_op='OP-TEST-001',
                tipo_estrategia='POR_PESO',
                activa=True
            )
            db.session.add(orden)
            db.session.commit()
            
            historial = registrar_cambio_estado(
                orden, 
                nuevo_estado=False, 
                usuario='admin', 
                motivo='Producción completada'
            )
            
            assert historial is not None
            assert historial.accion == 'CERRADA'
            assert historial.usuario == 'admin'
            assert historial.motivo == 'Producción completada'
            assert orden.activa == False
    
    def test_registrar_reapertura(self, app):
        """Registrar reapertura de orden cerrada"""
        with app.app_context():
            orden = OrdenProduccion(
                numero_op='OP-TEST-002',
                tipo_estrategia='POR_PESO',
                activa=False
            )
            db.session.add(orden)
            db.session.commit()
            
            historial = registrar_cambio_estado(
                orden, 
                nuevo_estado=True, 
                usuario='supervisor', 
                motivo='Error en cierre previo'
            )
            
            assert historial is not None
            assert historial.accion == 'REABIERTA'
            assert orden.activa == True
    
    def test_sin_cambio_no_registra(self, app):
        """No registrar si estado es el mismo"""
        with app.app_context():
            orden = OrdenProduccion(
                numero_op='OP-TEST-003',
                tipo_estrategia='POR_PESO',
                activa=True
            )
            db.session.add(orden)
            db.session.commit()
            
            historial = registrar_cambio_estado(orden, nuevo_estado=True)
            
            assert historial is None


class TestHistorialAPI:
    """Tests de los endpoints de historial"""
    
    def test_cerrar_orden_con_historial(self, client, app):
        """PUT /ordenes/{op}/estado registra historial"""
        with app.app_context():
            orden = OrdenProduccion(
                numero_op='OP-API-001',
                tipo_estrategia='POR_PESO',
                activa=True
            )
            db.session.add(orden)
            db.session.commit()
        
        response = client.put('/api/ordenes/OP-API-001/estado', json={
            'activa': False,
            'usuario': 'test_user',
            'motivo': 'Termino de turno'
        })
        
        assert response.status_code == 200
        data = response.get_json()
        assert data['activa'] == False
        assert 'historial' in data
        assert data['historial']['accion'] == 'CERRADA'
        assert data['historial']['usuario'] == 'test_user'
    
    def test_obtener_historial(self, client, app):
        """GET /ordenes/{op}/historial retorna lista de cambios"""
        with app.app_context():
            orden = OrdenProduccion(
                numero_op='OP-API-002',
                tipo_estrategia='POR_PESO',
                activa=True
            )
            db.session.add(orden)
            db.session.commit()
            
            # Simular varios cambios
            registrar_cambio_estado(orden, False, 'user1', 'Cierre 1')
            registrar_cambio_estado(orden, True, 'user2', 'Reapertura')
            registrar_cambio_estado(orden, False, 'user1', 'Cierre final')
        
        response = client.get('/api/ordenes/OP-API-002/historial')
        
        assert response.status_code == 200
        data = response.get_json()
        assert len(data['historial']) == 3
        # Más reciente primero
        assert data['historial'][0]['accion'] == 'CERRADA'
        assert data['historial'][1]['accion'] == 'REABIERTA'

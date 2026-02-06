"""
Tests para las funcionalidades de revisión progresiva de productos.
ACTUALIZADO: Usa linea_id y familia_id (FK normalizadas requeridas).
"""
import pytest
from datetime import datetime
from app.models.producto import ProductoTerminado, FamiliaColor, Pieza, Linea, Familia
from app.extensions import db
from tests.conftest import get_or_create_test_dependencies


class TestRevisionProductos:
    """Tests para el sistema de revisión progresiva de productos."""
    
    def test_producto_creado_con_estado_importado(self, app):
        """Un producto nuevo debería tener estado IMPORTADO por defecto."""
        with app.app_context():
            # Setup - get required FKs
            linea_id, familia_id = get_or_create_test_dependencies()
            
            fam = FamiliaColor(nombre="SOLIDO", codigo=1)
            db.session.add(fam)
            db.session.commit()
            
            # Crear producto sin especificar estado
            pt = ProductoTerminado(
                cod_sku_pt="TEST-REV-001",
                producto="Producto Test Revisión",
                linea_id=linea_id,
                familia_id=familia_id,
                familia_color_rel=fam,
                familia_color="SOLIDO",
                cod_familia_color=1
            )
            db.session.add(pt)
            db.session.commit()
            
            # Verificar estado por defecto
            pt_db = ProductoTerminado.query.get("TEST-REV-001")
            assert pt_db.estado_revision == "IMPORTADO"
    
    def test_actualizar_estado_revision(self, app):
        """Debe poder actualizar el estado de revisión de un producto."""
        with app.app_context():
            # Setup - get required FKs
            linea_id, familia_id = get_or_create_test_dependencies()
            
            fam = FamiliaColor(nombre="CARAMELO", codigo=2)
            db.session.add(fam)
            db.session.commit()
            
            pt = ProductoTerminado(
                cod_sku_pt="TEST-REV-002",
                producto="Producto Para Revisar",
                linea_id=linea_id,
                familia_id=familia_id,
                familia_color_rel=fam,
                estado_revision="IMPORTADO"
            )
            db.session.add(pt)
            db.session.commit()
            
            # Actualizar estado
            pt.estado_revision = "EN_REVISION"
            pt.notas_revision = "Verificar precio con contabilidad"
            pt.fecha_revision = datetime.now()
            db.session.commit()
            
            # Verificar cambios
            pt_db = ProductoTerminado.query.get("TEST-REV-002")
            assert pt_db.estado_revision == "EN_REVISION"
            assert pt_db.notas_revision == "Verificar precio con contabilidad"
            assert pt_db.fecha_revision is not None
    
    def test_filtrar_por_estado_revision(self, app):
        """Debe poder filtrar productos por estado de revisión."""
        with app.app_context():
            # Setup - get required FKs
            linea_id, familia_id = get_or_create_test_dependencies()
            
            fam = FamiliaColor(nombre="TRANS", codigo=3)
            db.session.add(fam)
            db.session.commit()
            
            # Crear productos con diferentes estados
            productos = [
                ProductoTerminado(cod_sku_pt="PEND-001", producto="Pendiente 1", 
                                 linea_id=linea_id, familia_id=familia_id,
                                 familia_color_rel=fam, estado_revision="IMPORTADO"),
                ProductoTerminado(cod_sku_pt="PEND-002", producto="Pendiente 2", 
                                 linea_id=linea_id, familia_id=familia_id,
                                 familia_color_rel=fam, estado_revision="IMPORTADO"),
                ProductoTerminado(cod_sku_pt="REV-001", producto="En Revision", 
                                 linea_id=linea_id, familia_id=familia_id,
                                 familia_color_rel=fam, estado_revision="EN_REVISION"),
                ProductoTerminado(cod_sku_pt="VER-001", producto="Verificado", 
                                 linea_id=linea_id, familia_id=familia_id,
                                 familia_color_rel=fam, estado_revision="VERIFICADO"),
            ]
            db.session.add_all(productos)
            db.session.commit()
            
            # Filtrar por estado
            importados = ProductoTerminado.query.filter_by(estado_revision="IMPORTADO").all()
            en_revision = ProductoTerminado.query.filter_by(estado_revision="EN_REVISION").all()
            verificados = ProductoTerminado.query.filter_by(estado_revision="VERIFICADO").all()
            
            assert len(importados) == 2
            assert len(en_revision) == 1
            assert len(verificados) == 1


class TestRevisionAPI:
    """Tests para los endpoints de API de revisión."""
    
    def test_listar_productos_revision_endpoint(self, client, app):
        """Endpoint GET /productos/revision debe listar productos."""
        with app.app_context():
            linea_id, familia_id = get_or_create_test_dependencies()
            
            fam = FamiliaColor(nombre="API-TEST", codigo=99)
            db.session.add(fam)
            db.session.commit()
            
            pt = ProductoTerminado(
                cod_sku_pt="API-REV-001",
                producto="Producto API Test",
                linea_id=linea_id,
                familia_id=familia_id,
                familia_color_rel=fam,
                estado_revision="IMPORTADO"
            )
            db.session.add(pt)
            db.session.commit()
        
        response = client.get('/api/productos/revision')
        assert response.status_code == 200
        
        data = response.get_json()
        assert 'productos' in data
        assert 'pagination' in data
        assert data['pagination']['total'] >= 1
    
    def test_filtrar_por_estado_endpoint(self, client, app):
        """Endpoint debe filtrar por estado de revisión."""
        with app.app_context():
            linea_id, familia_id = get_or_create_test_dependencies()
            
            fam = FamiliaColor(nombre="FILTRO-TEST", codigo=88)
            db.session.add(fam)
            db.session.commit()
            
            db.session.add_all([
                ProductoTerminado(cod_sku_pt="FILT-001", producto="F1", 
                                 linea_id=linea_id, familia_id=familia_id,
                                 familia_color_rel=fam, estado_revision="IMPORTADO"),
                ProductoTerminado(cod_sku_pt="FILT-002", producto="F2", 
                                 linea_id=linea_id, familia_id=familia_id,
                                 familia_color_rel=fam, estado_revision="VERIFICADO"),
            ])
            db.session.commit()
        
        # Filtrar solo importados
        response = client.get('/api/productos/revision?estado=IMPORTADO')
        assert response.status_code == 200
        data = response.get_json()
        
        # Todos los productos retornados deben ser IMPORTADO
        for p in data['productos']:
            assert p['estado_revision'] == 'IMPORTADO'
    
    def test_actualizar_revision_endpoint(self, client, app):
        """Endpoint PUT debe actualizar estado de revisión."""
        with app.app_context():
            linea_id, familia_id = get_or_create_test_dependencies()
            
            fam = FamiliaColor(nombre="UPDATE-TEST", codigo=77)
            db.session.add(fam)
            db.session.commit()
            
            pt = ProductoTerminado(
                cod_sku_pt="UPD-REV-001",
                producto="Para Actualizar",
                linea_id=linea_id,
                familia_id=familia_id,
                familia_color_rel=fam,
                estado_revision="IMPORTADO"
            )
            db.session.add(pt)
            db.session.commit()
        
        response = client.put('/api/productos/UPD-REV-001/revision', json={
            'estado_revision': 'VERIFICADO',
            'notas_revision': 'Revisado correctamente'
        })
        
        assert response.status_code == 200
        data = response.get_json()
        assert data['producto']['estado_revision'] == 'VERIFICADO'
        assert data['producto']['notas_revision'] == 'Revisado correctamente'
    
    def test_estadisticas_revision_endpoint(self, client, app):
        """Endpoint GET debe retornar estadísticas de revisión."""
        with app.app_context():
            linea_id, familia_id = get_or_create_test_dependencies()
            
            fam = FamiliaColor(nombre="STATS-TEST", codigo=66)
            db.session.add(fam)
            db.session.commit()
            
            db.session.add_all([
                ProductoTerminado(cod_sku_pt="STAT-001", producto="S1", 
                                 linea_id=linea_id, familia_id=familia_id,
                                 familia_color_rel=fam, estado_revision="IMPORTADO"),
                ProductoTerminado(cod_sku_pt="STAT-002", producto="S2", 
                                 linea_id=linea_id, familia_id=familia_id,
                                 familia_color_rel=fam, estado_revision="VERIFICADO"),
            ])
            db.session.commit()
        
        response = client.get('/api/productos/revision/estadisticas')
        assert response.status_code == 200
        
        data = response.get_json()
        assert 'total' in data
        assert 'por_estado' in data
        assert 'porcentaje_verificado' in data
        assert data['total'] >= 2
    
    def test_bulk_update_revision_endpoint(self, client, app):
        """Endpoint PUT bulk debe actualizar múltiples productos."""
        with app.app_context():
            linea_id, familia_id = get_or_create_test_dependencies()
            
            fam = FamiliaColor(nombre="BULK-TEST", codigo=55)
            db.session.add(fam)
            db.session.commit()
            
            db.session.add_all([
                ProductoTerminado(cod_sku_pt="BULK-001", producto="B1", 
                                 linea_id=linea_id, familia_id=familia_id,
                                 familia_color_rel=fam, estado_revision="IMPORTADO"),
                ProductoTerminado(cod_sku_pt="BULK-002", producto="B2", 
                                 linea_id=linea_id, familia_id=familia_id,
                                 familia_color_rel=fam, estado_revision="IMPORTADO"),
                ProductoTerminado(cod_sku_pt="BULK-003", producto="B3", 
                                 linea_id=linea_id, familia_id=familia_id,
                                 familia_color_rel=fam, estado_revision="IMPORTADO"),
            ])
            db.session.commit()
        
        response = client.put('/api/productos/revision/bulk', json={
            'skus': ['BULK-001', 'BULK-002'],
            'estado_revision': 'VERIFICADO'
        })
        
        assert response.status_code == 200
        data = response.get_json()
        assert data['actualizados'] == 2
        
        # Verificar en DB
        with app.app_context():
            b1 = ProductoTerminado.query.get('BULK-001')
            b2 = ProductoTerminado.query.get('BULK-002')
            b3 = ProductoTerminado.query.get('BULK-003')
            
            assert b1.estado_revision == 'VERIFICADO'
            assert b2.estado_revision == 'VERIFICADO'
            assert b3.estado_revision == 'IMPORTADO'  # No fue incluido


# ============================================================================
# TESTS PARA PIEZAS
# ============================================================================

class TestRevisionPiezas:
    """Tests para el sistema de revisión progresiva de piezas."""
    
    def test_pieza_creada_con_estado_importado(self, app):
        """Una pieza nueva debería tener estado IMPORTADO por defecto."""
        with app.app_context():
            linea_id, familia_id = get_or_create_test_dependencies()
            
            pieza = Pieza(
                sku="TEST-PZ-001",
                piezas="Pieza Test Revisión",
                linea_id=linea_id,
                familia_id=familia_id
            )
            db.session.add(pieza)
            db.session.commit()
            
            pieza_db = Pieza.query.get("TEST-PZ-001")
            assert pieza_db.estado_revision == "IMPORTADO"
    
    def test_actualizar_estado_revision_pieza(self, app):
        """Debe poder actualizar el estado de revisión de una pieza."""
        with app.app_context():
            linea_id, familia_id = get_or_create_test_dependencies()
            
            pieza = Pieza(
                sku="TEST-PZ-002",
                piezas="Pieza Para Revisar",
                linea_id=linea_id,
                familia_id=familia_id,
                estado_revision="IMPORTADO"
            )
            db.session.add(pieza)
            db.session.commit()
            
            pieza.estado_revision = "VERIFICADO"
            pieza.notas_revision = "Peso verificado OK"
            pieza.fecha_revision = datetime.now()
            db.session.commit()
            
            pieza_db = Pieza.query.get("TEST-PZ-002")
            assert pieza_db.estado_revision == "VERIFICADO"
            assert pieza_db.notas_revision == "Peso verificado OK"


class TestRevisionPiezasAPI:
    """Tests para los endpoints de API de revisión de piezas."""
    
    def test_listar_piezas_revision_endpoint(self, client, app):
        """Endpoint GET /piezas/revision debe listar piezas."""
        with app.app_context():
            linea_id, familia_id = get_or_create_test_dependencies()
            
            pieza = Pieza(
                sku="API-PZ-001",
                piezas="Pieza API Test",
                linea_id=linea_id,
                familia_id=familia_id,
                estado_revision="IMPORTADO"
            )
            db.session.add(pieza)
            db.session.commit()
        
        response = client.get('/api/piezas/revision')
        assert response.status_code == 200
        
        data = response.get_json()
        assert 'piezas' in data
        assert 'pagination' in data
        assert data['pagination']['total'] >= 1
    
    def test_actualizar_revision_pieza_endpoint(self, client, app):
        """Endpoint PUT debe actualizar estado de revisión de pieza."""
        with app.app_context():
            linea_id, familia_id = get_or_create_test_dependencies()
            
            pieza = Pieza(
                sku="UPD-PZ-001",
                piezas="Para Actualizar",
                linea_id=linea_id,
                familia_id=familia_id,
                estado_revision="IMPORTADO"
            )
            db.session.add(pieza)
            db.session.commit()
        
        response = client.put('/api/piezas/UPD-PZ-001/revision', json={
            'estado_revision': 'VERIFICADO',
            'notas_revision': 'Revisado OK'
        })
        
        assert response.status_code == 200
        data = response.get_json()
        assert data['pieza']['estado_revision'] == 'VERIFICADO'
    
    def test_estadisticas_revision_piezas_endpoint(self, client, app):
        """Endpoint GET debe retornar estadísticas de revisión de piezas."""
        with app.app_context():
            linea_id, familia_id = get_or_create_test_dependencies()
            
            db.session.add_all([
                Pieza(sku="STAT-PZ-001", piezas="S1", linea_id=linea_id, 
                     familia_id=familia_id, estado_revision="IMPORTADO"),
                Pieza(sku="STAT-PZ-002", piezas="S2", linea_id=linea_id, 
                     familia_id=familia_id, estado_revision="VERIFICADO"),
            ])
            db.session.commit()
        
        response = client.get('/api/piezas/revision/estadisticas')
        assert response.status_code == 200
        
        data = response.get_json()
        assert 'total' in data
        assert 'por_estado' in data
        assert 'porcentaje_verificado' in data
    
    def test_bulk_update_revision_piezas_endpoint(self, client, app):
        """Endpoint PUT bulk debe actualizar múltiples piezas."""
        with app.app_context():
            linea_id, familia_id = get_or_create_test_dependencies()
            
            db.session.add_all([
                Pieza(sku="BULK-PZ-001", piezas="B1", linea_id=linea_id, 
                     familia_id=familia_id, estado_revision="IMPORTADO"),
                Pieza(sku="BULK-PZ-002", piezas="B2", linea_id=linea_id, 
                     familia_id=familia_id, estado_revision="IMPORTADO"),
                Pieza(sku="BULK-PZ-003", piezas="B3", linea_id=linea_id, 
                     familia_id=familia_id, estado_revision="IMPORTADO"),
            ])
            db.session.commit()
        
        response = client.put('/api/piezas/revision/bulk', json={
            'skus': ['BULK-PZ-001', 'BULK-PZ-002'],
            'estado_revision': 'VERIFICADO'
        })
        
        assert response.status_code == 200
        data = response.get_json()
        assert data['actualizados'] == 2
        
        with app.app_context():
            b1 = Pieza.query.get('BULK-PZ-001')
            b2 = Pieza.query.get('BULK-PZ-002')
            b3 = Pieza.query.get('BULK-PZ-003')
            
            assert b1.estado_revision == 'VERIFICADO'
            assert b2.estado_revision == 'VERIFICADO'
            assert b3.estado_revision == 'IMPORTADO'  # No fue incluido

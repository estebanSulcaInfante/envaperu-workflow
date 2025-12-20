"""
Test para la generación de Excel de Órdenes de Producción.
"""
import pytest
from app.models.orden import OrdenProduccion
from app.models.lote import LoteColor
from app.models.recetas import SeCompone, SeColorea
from app.models.materiales import MateriaPrima, Colorante
from app.services.excel_service import generar_op_excel
from app.extensions import db
from datetime import datetime, timezone


def test_generar_excel_basico(client, app):
    """
    Verifica que el servicio de generación de Excel funcione correctamente.
    """
    with app.app_context():
        # Setup: Crear orden con datos mínimos
        mp = MateriaPrima(nombre="PP Test", tipo="VIRGEN")
        pig = Colorante(nombre="Azul Test")
        db.session.add_all([mp, pig])
        db.session.commit()

        orden = OrdenProduccion(
            numero_op="OP-EXCEL-TEST",
            tipo_estrategia="POR_PESO",
            meta_total_kg=100.0,
            peso_unitario_gr=50.0,
            peso_inc_colada=100.0,
            cavidades=2,
            tiempo_ciclo=30.0,
            horas_turno=24.0,
            producto="Producto Test",
            molde="Molde Test",
            maquina_id="MAQ-01",
            fecha_inicio=datetime.now(timezone.utc)
        )
        db.session.add(orden)
        db.session.commit()

        # Agregar un lote
        lote = LoteColor(
            numero_op=orden.numero_op,
            color_nombre="Rojo",
            personas=1
        )
        db.session.add(lote)
        db.session.flush()

        # Agregar materiales y colorantes
        db.session.add(SeCompone(lote_id=lote.id, materia_prima_id=mp.id, fraccion=1.0))
        db.session.add(SeColorea(lote_id=lote.id, colorante_id=pig.id, gramos=30.0))
        db.session.commit()

        # Generar Excel
        excel_buffer = generar_op_excel(orden)
        
        # Validaciones básicas
        assert excel_buffer is not None
        assert excel_buffer.getbuffer().nbytes > 0
        
        # El buffer debería ser un archivo xlsx válido
        # (los primeros bytes de un archivo xlsx/zip son 'PK')
        excel_buffer.seek(0)
        header = excel_buffer.read(2)
        assert header == b'PK', "El archivo generado no es un Excel válido"


def test_endpoint_excel_descarga(client, app):
    """
    Verifica que el endpoint de descarga de Excel funcione.
    """
    with app.app_context():
        # Usar la orden creada en crear_tablas.py (OP-1322)
        orden = db.session.get(OrdenProduccion, "OP-1322")
        
        if not orden:
            # Si no existe, crear una orden de prueba
            orden = OrdenProduccion(
                numero_op="OP-1322",
                tipo_estrategia="POR_PESO",
                meta_total_kg=100.0,
                peso_unitario_gr=50.0,
                peso_inc_colada=100.0,
                cavidades=2,
                producto="Test",
                molde="Test Molde",
                maquina_id="M1"
            )
            db.session.add(orden)
            db.session.commit()

    # Llamar al endpoint
    response = client.get('/api/ordenes/OP-1322/excel')
    
    # Verificar respuesta
    assert response.status_code == 200
    assert response.content_type == 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    assert len(response.data) > 0


def test_endpoint_excel_orden_no_existe(client, app):
    """
    Verifica que el endpoint retorne 404 para orden inexistente.
    """
    response = client.get('/api/ordenes/OP-NO-EXISTE/excel')
    assert response.status_code == 404

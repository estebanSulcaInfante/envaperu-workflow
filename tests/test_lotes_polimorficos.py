import pytest
from app.models.orden import OrdenProduccion
from app.models.lote import LoteColor
from app.extensions import db

def test_lote_polimorfismo_completo(client, app):
    """
    Verifica que el Lote active la columna correcta (C, D o E)
    y calcule el valor base basándose estrictamente en la Estrategia de la Orden.
    """
    with app.app_context():
        # =================================================================
        # ESCENARIO A: ESTRATEGIA "POR PESO" (Activa Columna D)
        # =================================================================
        orden_peso = OrdenProduccion(
            numero_op="OP-TEST-PESO",
            tipo_estrategia="POR_PESO",
            meta_total_kg=600.0, # Input Global
            peso_unitario_gr=10.0
        )
        db.session.add(orden_peso)
        db.session.commit()

        # Agregamos 3 Lotes (Colores) -> La meta se debe dividir entre 3
        l1 = LoteColor(orden_id=orden_peso.id, color_nombre="Rojo")
        l2 = LoteColor(orden_id=orden_peso.id, color_nombre="Verde")
        l3 = LoteColor(orden_id=orden_peso.id, color_nombre="Azul")
        db.session.add_all([l1, l2, l3])
        db.session.commit()

        # VALIDACIÓN LOTE 1
        data = l1.to_dict()
        
        # 1. Verificar Semáforo de Columnas
        assert data['peso_kg_calc'] is not None  # ACTIVO (Columna D)
        assert data['cant_kg_calc'] is None      # INACTIVO (Columna C)
        assert data['stock_kg_manual'] is None   # INACTIVO (Columna E)

        # 2. Verificar Matemática (600 / 3 = 200)
        assert data['peso_kg_calc'] == 200.0


        # =================================================================
        # ESCENARIO B: ESTRATEGIA "POR CANTIDAD" (Activa Columna C)
        # =================================================================
        orden_cant = OrdenProduccion(
            numero_op="OP-TEST-CANT",
            tipo_estrategia="POR_CANTIDAD",
            meta_total_doc=100.0,  # Input Global: 100 Docenas
            peso_unitario_gr=50.0, # P.Unitario necesario para conversión
        )
        db.session.add(orden_cant)
        db.session.commit()

        # Agregamos 2 Lotes -> Meta repartida entre 2
        l_c1 = LoteColor(orden_id=orden_cant.id, color_nombre="Amarillo")
        l_c2 = LoteColor(orden_id=orden_cant.id, color_nombre="Negro")
        db.session.add_all([l_c1, l_c2])
        db.session.commit()

        # VALIDACIÓN LOTE
        data_c = l_c1.to_dict()

        # 1. Verificar Semáforo
        assert data_c['cant_kg_calc'] is not None # ACTIVO (Columna C)
        assert data_c['peso_kg_calc'] is None     # INACTIVO
        assert data_c['stock_kg_manual'] is None  # INACTIVO

        # 2. Verificar Matemática
        # Fórmula: (100 Doc * 12 * 50gr / 1000) = 60 Kg Totales
        # Reparto: 60 Kg / 2 Colores = 30 Kg por color
        assert data_c['cant_kg_calc'] == 30.0


        # =================================================================
        # ESCENARIO C: ESTRATEGIA "STOCK" (Activa Columna E)
        # =================================================================
        orden_stock = OrdenProduccion(
            numero_op="OP-TEST-STOCK",
            tipo_estrategia="STOCK",
            # Sin metas globales
        )
        db.session.add(orden_stock)
        db.session.commit()

        # Input Manual en el Lote (Stock manual)
        l_s1 = LoteColor(
            orden_id=orden_stock.id, 
            color_nombre="Blanco", 
            stock_kg_manual=25.5 # Usuario escribió esto
        )
        db.session.add(l_s1)
        db.session.commit()

        # VALIDACIÓN LOTE
        data_s = l_s1.to_dict()

        # 1. Verificar Semáforo
        assert data_s['stock_kg_manual'] == 25.5 # ACTIVO (Columna E)
        assert data_s['cant_kg_calc'] is None    # INACTIVO
        assert data_s['peso_kg_calc'] is None    # INACTIVO
        
        # 2. Verificar que el cálculo de coladas use este valor base
        # (Aunque sea manual, el sistema debe reconocerlo como el peso objetivo)
        # Asumiendo Lote.peso_total_objetivo funciona:
        assert l_s1.peso_total_objetivo == 25.5
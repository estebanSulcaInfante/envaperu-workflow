import pytest
from app.models.orden import OrdenProduccion
from app.models.lote import LoteColor
from app.models.producto import ColorProducto
from app.extensions import db

def test_calculos_tabla_auxiliar_por_estrategia(client, app):
    """
    TEST MAESTRO: LÓGICA DE NEGOCIO Y FÓRMULAS DEL EXCEL
    ----------------------------------------------------------------
    Objetivo: Validar que los cálculos de la tabla auxiliar (totales)
    cambien dinámicamente según la estrategia, tal como el IF del Excel.
    """
    
    with app.app_context():
        # =================================================================
        # CASO 1: ESTRATEGIA "POR PESO" (Ejemplo Balde Romano OP1322)
        # =================================================================
        # Lógica Excel: IF(Tipo="Por peso", D10*1000/D7/12, ...)
        # El input principal son KILOS. Las DOCENAS se calculan.
        
        op_peso = OrdenProduccion(
            numero_op="OP-PESO-CALC",
            tipo_estrategia="POR_PESO",
            meta_total_kg=1050.0,  # <-- INPUT USUARIO (D10)
            
            # Datos Técnicos (D7, E7, F7...)
            snapshot_peso_unitario_gr=87.0, # D7
            snapshot_cavidades=2,           # E7
            snapshot_peso_inc_colada=176.0, # F7 (Tiro)
            snapshot_tiempo_ciclo=30.0,
            snapshot_horas_turno=23.0
        )
        db.session.add(op_peso)
        db.session.commit()
        
        # Actualizar métricas calculadas
        op_peso.actualizar_metricas()
        db.session.commit()
        
        # Obtenemos los cálculos
        data_peso = op_peso.to_dict()['resumen_totales']
        
        # --- VALIDACIÓN DE FÓRMULAS (ASSERTS) ---
        
        # 1. % Merma Real
        # Fórm: (PesoTiro - (PesoUnit * Cav)) / PesoTiro
        # Calc: (176 - (87 * 2)) / 176 = 2 / 176 = 0.01136...
        merma_esperada = (176 - (87 * 2)) / 176
        assert data_peso['%Merma'] == pytest.approx(merma_esperada, abs=0.0001)
        
        # 2. % EXTRA (Regla de Negocio)
        # Como 1.13% < 5%, se cobra el 100% de la merma.
        assert data_peso['% EXTRA'] == pytest.approx(merma_esperada, abs=0.0001)

        # 3. EXTRA (Kg)
        # Fórm: MetaKg * %Extra
        # Calc: 1050 * 0.01136... = 11.93
        extra_kg_esperado = 1050.0 * merma_esperada
        assert data_peso['EXTRA'] == pytest.approx(extra_kg_esperado, abs=0.01)
        
        # 4. CANTIDAD DOC (La fórmula condicional H21)
        # Al ser POR PESO -> Convertimos Kg a Docenas
        # Fórm: (Kg * 1000) / P.Unit / 12
        # Calc: 1,050,000 / 87 / 12 = 1005.747...
        docenas_calc = (1050.0 * 1000) / 87.0 / 12
        assert data_peso['Cantidad DOC'] == pytest.approx(docenas_calc, abs=0.01)


        # =================================================================
        # CASO 2: ESTRATEGIA "POR CANTIDAD"
        # =================================================================
        # Lógica Excel: IF(Tipo="Por cantidad", C10, ...)
        # El input principal son DOCENAS. Los Kilos base se calculan.
        
        op_cant = OrdenProduccion(
            numero_op="OP-CANT-CALC",
            tipo_estrategia="POR_CANTIDAD",
            meta_total_doc=100.0,  # <-- INPUT USUARIO (C10)
            
            snapshot_peso_unitario_gr=45.0,
            snapshot_cavidades=4,
            snapshot_peso_inc_colada=200.0, # Tiro más grande => Más merma
            snapshot_tiempo_ciclo=20.0
        )
        db.session.add(op_cant)
        db.session.commit()
        
        # Actualizar métricas calculadas
        op_cant.actualizar_metricas()
        db.session.commit()
        
        data_cant = op_cant.to_dict()['resumen_totales']
        
        # --- VALIDACIÓN DE FÓRMULAS ---

        # 1. Base de Cálculo (Kg Producción)
        # Al ser POR CANTIDAD, necesitamos Kilos para la máquina.
        # Fórm: (Docenas * 12 * P.Unit) / 1000
        # Calc: (100 * 12 * 45) / 1000 = 54.0 Kg
        kg_base_esperado = 54.0
        assert data_cant['Peso(Kg) PRODUCCION'] == kg_base_esperado
        
        # 2. Cantidad DOC (Identidad)
        # Al ser POR CANTIDAD -> Es el mismo valor del input (C10)
        assert data_cant['Cantidad DOC'] == 100.0

        # 3. % Merma
        # (200 - (45*4)) / 200 = (200 - 180) / 200 = 20/200 = 0.10 (10%)
        assert data_cant['%Merma'] == 0.10
        
        # 4. % EXTRA (Regla de Negocio)
        # Como es 10% (está en rango 5-10%), se cobra la MITAD.
        # Esperado: 5% (0.05)
        assert data_cant['% EXTRA'] == 0.05
        
        # 5. EXTRA (Kg)
        # 54 Kg * 5% = 2.7 Kg
        assert data_cant['EXTRA'] == pytest.approx(2.7, abs=0.01)


        # =================================================================
        # CASO 3: ESTRATEGIA "STOCK"
        # =================================================================
        # Lógica Excel: IF(Tipo="Completar Stock", SUM(E13:E18), ...)
        # No hay meta global. El total es la SUMA de los inputs manuales.
        
        op_stock = OrdenProduccion(
            numero_op="OP-STOCK-CALC",
            tipo_estrategia="STOCK",
            
            snapshot_peso_unitario_gr=100.0,
            snapshot_cavidades=1,
            snapshot_peso_inc_colada=110.0
        )
        db.session.add(op_stock)
        db.session.commit()
        
        # Agregamos Lotes (Simulando input manual en columna E)
        # Crear colores primero
        c_a = ColorProducto(nombre="A", codigo=10)
        c_b = ColorProducto(nombre="B", codigo=11)
        db.session.add_all([c_a, c_b])
        db.session.commit()
        
        l1 = LoteColor(numero_op=op_stock.numero_op, color_id=c_a.id, stock_kg_manual=50.0)
        l2 = LoteColor(numero_op=op_stock.numero_op, color_id=c_b.id, stock_kg_manual=50.0)
        db.session.add_all([l1, l2])
        db.session.commit()
        
        # Actualizar métricas calculadas
        op_stock.actualizar_metricas()
        db.session.commit()
        
        data_stock = op_stock.to_dict()['resumen_totales']
        
        # --- VALIDACIÓN DE FÓRMULAS ---
        
        # 1. Base de Cálculo (Suma de Lotes)
        # 50 + 50 = 100 Kg
        kg_stock_total = 100.0
        assert data_stock['Peso(Kg) PRODUCCION'] == kg_stock_total
        
        # 2. Cantidad DOC
        # Al ser STOCK -> Convertimos la Suma de Kg a Docenas
        # Fórm: (100 Kg * 1000) / P.Unit / 12
        # Calc: 100,000 / 100 = 1000 un. / 12 = 83.33 docenas
        docenas_stock = (100.0 * 1000) / 100.0 / 12
        assert data_stock['Cantidad DOC'] == pytest.approx(docenas_stock, abs=0.01)
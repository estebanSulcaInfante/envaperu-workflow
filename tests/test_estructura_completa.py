import pytest
from datetime import datetime, timezone
from app.models.orden import OrdenProduccion
from app.models.lote import LoteColor
from app.models.recetas import SeCompone, SeColorea
from app.models.materiales import MateriaPrima, Colorante
from app.models.producto import ProductoTerminado, FamiliaColor, ColorProducto, Pieza
from app.models.maquina import Maquina
from app.models.registro import RegistroDiarioProduccion
from app.extensions import db

def test_estructura_completa_json(client, app):
    """
    Verifica la estructura completa del JSON (Reporte Normalizado):
    - Cabecera
    - Lotes Polimórficos con Extra
    - Materiales (Lista dinámica con cálculo de peso por fracción)
    - Pigmentos (Lista dinámica con dosis de gramos)
    - Mano de Obra (Cálculo de HH)
    - Tabla Auxiliar
    """
    with app.app_context():
        # 1. SETUP: MATERIALES Y COLORANTES
        mp1 = MateriaPrima(nombre="POLIPROPILENO", tipo="VIRGEN")
        mp2 = MateriaPrima(nombre="MOLIDO", tipo="MOLIDO")
        color = Colorante(nombre="AZUL")
        db.session.add_all([mp1, mp2, color])
        db.session.commit()

        # 1.5 SETUP: MAQUINA
        maquina_test = Maquina(nombre="MAQ-TEST", tipo="TEST 100T")
        db.session.add(maquina_test)
        db.session.commit()

        # 2. SETUP: ORDEN (Por Peso)
        orden = OrdenProduccion(
            numero_op="OP-FULL-STRUCT",
            tipo_estrategia="POR_PESO",
            meta_total_kg=1000.0,
            snapshot_peso_unitario_gr=50.0,
            snapshot_peso_inc_colada=200.0, # 200g tiro
            snapshot_cavidades=4,
            snapshot_tiempo_ciclo=20.0,
            snapshot_horas_turno=24.0,
            fecha_inicio=datetime.now(timezone.utc),
            maquina_id=maquina_test.id  # FK a Maquina
        )
        db.session.add(orden)
        db.session.commit()

        # 3. SETUP: LOTE (1 solo lote -> 1000kg)
        # Necesitamos un ColorProducto real
        c_prod_test = ColorProducto(nombre="AZUL TEST", codigo=99, familia_id=None)
        db.session.add(c_prod_test)
        db.session.commit()

        lote = LoteColor(
            numero_op=orden.numero_op,
            color_id=c_prod_test.id, # Usar ID
            personas=2 # 2 Operarios
        )
        db.session.add(lote)
        db.session.commit()

        # 4. SETUP: RECETA
        # Mezcla: 60% PP, 40% Molido
        r1 = SeCompone(lote_id=lote.id, materia_prima_id=mp1.id, fraccion=0.60)
        r2 = SeCompone(lote_id=lote.id, materia_prima_id=mp2.id, fraccion=0.40)
        # Color: 50g dosis
        c1 = SeColorea(lote_id=lote.id, colorante_id=color.id, gramos=50.0)
        
        db.session.add_all([r1, r2, c1])
        db.session.commit()

        # ---------------------------------------------------------------------
        # NUEVO: LLAMADA EXPLÍCITA AL MOTOR DE CÁLCULO
        # ---------------------------------------------------------------------
        orden.actualizar_metricas()
        db.session.commit() # Guardar cambios calculados

        # 5. VALIDACIÓN DE PERSISTENCIA (Columnas directas)
        # Re-fetch para asegurar que leemos de DB
        orden_db = OrdenProduccion.query.get("OP-FULL-STRUCT")
        
        # A. CÁLCULO DE PESOS
        assert orden_db.calculo_peso_produccion == 1000.0
        assert orden_db.calculo_merma_pct == 0.0
        assert orden_db.calculo_extra_kg == 0.0
        
        # B. LOTE DATA (Columnas)
        lote_db = orden_db.lotes[0]
        assert lote_db.calculo_col_d == 1000.0
        assert lote_db.calculo_peso_base == 1000.0
        
        # C. MATERIALES (Columnas)
        # PP: 60% de 1000 = 600kg
        mats = lote_db.materias_primas
        pp = next(m for m in mats if m.materia.nombre == "POLIPROPILENO")
        assert pp.calculo_peso_kg == 600.0
        
        molido = next(m for m in mats if m.materia.nombre == "MOLIDO")
        assert molido.calculo_peso_kg == 400.0
        
        # ---------------------------------------------------------------------
        # 6. VALIDACIÓN LEGACY (El to_dict debe seguir funcionando igual)
        # ---------------------------------------------------------------------
        data = orden_db.to_dict()
        lote_data = data['lotes'][0]
        resumen = data['resumen_totales']

        assert resumen['Peso(Kg) PRODUCCION'] == 1000.0
        assert lote_data['Peso (Kg)'] == 1000.0
        
        mats_dict = lote_data['materiales']
        pp_data = next(m for m in mats_dict if m['nombre'] == "POLIPROPILENO")
        assert pp_data['peso_kg'] == 600.0
        
        mo = lote_data['mano_obra']
        # HH Validation (mismos valores que antes)
        # 100,000s / 3600 = 27.77h maq -> 1.157 dias -> * 48h (24*2p) = 55.55
        horas_maq = 100000 / 3600
        dias_maq = horas_maq / 24
        hh_esperadas = dias_maq * 24 * 2
        
        
        assert mo['horas_hombre'] == pytest.approx(hh_esperadas, abs=0.01)

        # ---------------------------------------------------------------------
        # 7. VALIDACIÓN SKU Y FAMILIA COLOR (NUEVO REFACTOR)
        # ---------------------------------------------------------------------
        # A. Crear Entidades de Color
        fam = FamiliaColor(nombre="TRANSPARENTE", codigo=50)  # Añadir codigo
        db.session.add(fam)
        db.session.commit()
        
        col_prod = ColorProducto(nombre="ROJO", codigo=50, familia=fam)
        db.session.add(col_prod)
        db.session.commit()
        
        # B. Crear Producto con FamiliaColor (no ColorProducto) y verificar SKU
        from app.models.producto import Linea, Familia
        
        # Get or create linea and familia
        linea_test = Linea.query.filter_by(nombre='INDUSTRIAL').first()
        if not linea_test:
            linea_test = Linea(codigo=1, nombre='INDUSTRIAL')
            db.session.add(linea_test)
            db.session.flush()
        
        familia_test = Familia.query.filter_by(nombre='TEST').first()
        if not familia_test:
            familia_test = Familia(codigo=2, nombre='TEST')
            db.session.add(familia_test)
            db.session.flush()
        
        pt = ProductoTerminado(
            linea_id=linea_test.id,
            familia_id=familia_test.id,
            cod_producto=3,
            familia_color_rel=fam,  # Usando relacion con FamiliaColor
            familia_color="TRANSPARENTE",
            cod_familia_color=50,
            producto="BALDE ROJO TRANS",
            estado_revision="IMPORTADO"  # Nuevo campo obligatorio
        )
        
        # Manually set backref relationships for SKU generation (before session)
        pt.linea_rel = linea_test
        pt.familia_rel = familia_test
        
        # Generar SKU (deberia usar codigo 50)
        sku_gen = pt.generar_sku()
        # Dynamic assertion: 0 + linea.codigo + familia.codigo + 3 + 0 + 50
        expected_sku = f"0{linea_test.codigo}{familia_test.codigo}3050"
        assert sku_gen == expected_sku, f"SKU mismatch. Expected {expected_sku}, got {sku_gen}"
        pt.cod_sku_pt = sku_gen
        db.session.add(pt)
        db.session.commit()
        
        # C. Vincular Orden a Producto y Actualizar
        orden.producto_sku = pt.cod_sku_pt
        orden.actualizar_metricas() # Debe jalar la familia
        db.session.commit()
        
        # D. Verificar Cache
        assert orden.calculo_familia_color == "TRANSPARENTE"
        
        # Verificar en dict final
        final_dict = orden.to_dict()
        assert final_dict['resumen_totales']['Familia Color'] == "TRANSPARENTE"
        # ---------------------------------------------------------------------
        # 8. VALIDAR REGISTRO DIARIO
        # ---------------------------------------------------------------------
        # Crear registro asociado a la orden y maquina
        registro = RegistroDiarioProduccion(
            orden_id=orden.numero_op,
            maquina_id=maquina_test.id,
            fecha=datetime.now(timezone.utc).date(),
            turno="DIA",
            # maquinista="TEST OPERATOR", # Removed
            # molde=orden.molde, # Removed
            # pieza_color="TEST-PIEZA-COLOR", # Removed
            colada_inicial=0,
            colada_final=100, # coladas=100
            # horas_trabajadas=5.0, # Removed
            
            # Snapshots (Copiados de la Orden)
            snapshot_cavidades=orden.snapshot_cavidades, # 4
            tiempo_ciclo_reportado=orden.snapshot_tiempo_ciclo,
            snapshot_peso_neto_gr=orden.snapshot_peso_unitario_gr, # 50.0
            snapshot_peso_colada_gr=0.0,
            snapshot_peso_extra_gr=0.0
        )
        
        registro.actualizar_totales()
        db.session.add(registro)
        db.session.commit()
        
        # Validar Cálculos
        
        # 1. Peso Aprox = (PesoUnit * Cavs * Coladas) / 1000
        # (50 * 4 * 100) / 1000 = 20,000 / 1000 = 20.0 kg
        assert registro.total_kg_real == pytest.approx(20.0)
        
        # 2. Cantidad Real = Coladas * Cavs
        # 100 * 4 = 400
        assert registro.total_piezas_buenas == 400
        
        # 3. Produccion Esperada 
        # No existe campo "calculo_produccion_esperada_kg" en el modelo actual.
        # Lo omitimos si no es parte del modelo actual.
        
        # Validar Relationships
        assert len(orden.registros_diarios) == 1
        # assert orden.registros_diarios[0].maquinista == "TEST OPERATOR" # Removed as field does not exist

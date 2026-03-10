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

        from app.models.orden import SnapshotComposicionMolde

        # 3. SETUP: SNAPSHOT del molde (1 cav × 200g, sin colada)
        db.session.add(SnapshotComposicionMolde(
            orden_id=orden.numero_op, cavidades=1, peso_unit_gr=200.0
        ))
        db.session.flush()

        lote = LoteColor(
            numero_op=orden.numero_op,
            color_id=c_prod_test.id,
            personas=2,
            meta_kg=1000.0,   # <-- meta directa por lote
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

        # 5. VALIDACIÓN DE PERSISTENCIA (nuevo modelo)
        orden_db = OrdenProduccion.query.get("OP-FULL-STRUCT")

        # A. PESO PRODUCCION = suma de meta_kg de lotes
        assert orden_db.calculo_peso_produccion == pytest.approx(1000.0)
        # Merma = 0 (sin colada en el snapshot)
        assert orden_db.calculo_merma_pct == pytest.approx(0.0)

        # B. Lote: coladas = meta_kg * 1000 / peso_neto_golpe = 1000000 / 200 = 5000
        lote_db = orden_db.lotes[0]
        assert lote_db.meta_kg == pytest.approx(1000.0)
        assert lote_db.calculo_coladas == pytest.approx(5000.0)
        assert lote_db.calculo_kg_real == pytest.approx(1000.0)

        # C. MATERIALES: fraccion * meta_kg * (1 + merma) = fraccion * 1000
        mats = lote_db.materias_primas
        pp = next(m for m in mats if m.materia.nombre == "POLIPROPILENO")
        assert pp.calculo_peso_kg == pytest.approx(600.0)   # 60% × 1000

        molido = next(m for m in mats if m.materia.nombre == "MOLIDO")
        assert molido.calculo_peso_kg == pytest.approx(400.0)  # 40% × 1000

        # D. to_dict
        data = orden_db.to_dict()
        lote_data = data['lotes'][0]
        resumen = data['resumen_totales']

        assert resumen['Peso(Kg) PRODUCCION'] == pytest.approx(1000.0)
        assert lote_data['meta_kg'] == pytest.approx(1000.0)

        mats_dict = lote_data['materiales']
        pp_data = next(m for m in mats_dict if m['nombre'] == "POLIPROPILENO")
        assert pp_data['peso_kg'] == pytest.approx(600.0)

        mo = lote_data['mano_obra']
        # HH: tiempo=20s/ciclo, meta=1000kg, neto=200g/golpe → golpes=5000
        # total_s=100000 → horas=27.77 → dias=1.157 → HH=1.157*24*2=55.55
        horas_maq = (1000000 / 200 * 20) / 3600   # = 100000/3600
        dias_maq  = horas_maq / 24
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
        # 8. VALIDAR REGISTRO DIARIO
        registro = RegistroDiarioProduccion(
            orden_id=orden.numero_op,
            maquina_id=maquina_test.id,
            fecha=datetime.now(timezone.utc).date(),
            turno="DIA",
            colada_inicial=0,
            colada_final=100,

            # Snapshots (valores directos del nuevo modelo)
            snapshot_cavidades   = orden_db.calculo_cavidades_totales,  # 1
            tiempo_ciclo_reportado = orden.snapshot_tiempo_ciclo,
            snapshot_peso_neto_gr  = orden_db.calculo_peso_neto_golpe,  # 200g
            snapshot_peso_colada_gr= 0.0,
            snapshot_peso_extra_gr = 0.0,
        )
        registro.actualizar_totales()
        db.session.add(registro)
        db.session.commit()

        # Peso = (200g * 1cav * 100 coladas) / 1000 = 20.0 kg
        assert registro.total_kg_real == pytest.approx(20.0)
        # Piezas = 100 * 1 = 100
        assert registro.total_piezas_buenas == 100

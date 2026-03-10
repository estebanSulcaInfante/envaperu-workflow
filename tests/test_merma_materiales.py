import pytest
from app.models.orden import OrdenProduccion, SnapshotComposicionMolde
from app.models.lote import LoteColor
from app.models.recetas import SeCompone
from app.models.materiales import MateriaPrima
from app.models.producto import ColorProducto
from app.extensions import db


def test_calculo_materiales_con_merma(client, app):
    """
    Verifica que SeCompone.calculo_peso_kg incluya la merma de colada.

    Molde: 1 cav × 100g + 10g colada = 110g tiro
    Merma = 10/110 ≈ 9.09%

    LoteColor: meta_kg = 1000 kg
    Receta: fraccion = 1.0 (100% virgen)

    peso_kg esperado = meta_kg * (1 + merma_pct) = 1000 * (1 + 10/110) = 1090.909 kg
    """
    with app.app_context():
        mp = MateriaPrima(nombre="VIRGEN TEST", tipo="VIRGEN")
        db.session.add(mp)
        db.session.commit()

        orden = OrdenProduccion(
            numero_op="OP-MERMA-TEST",
            snapshot_peso_colada_gr=10.0,
            snapshot_tiempo_ciclo=20.0,
            snapshot_horas_turno=24.0,
        )
        db.session.add(orden)
        db.session.flush()

        # Molde: 1 cav × 100g → neto=100g, tiro=110g
        db.session.add(SnapshotComposicionMolde(
            orden_id=orden.numero_op, cavidades=1, peso_unit_gr=100.0
        ))
        db.session.flush()

        c_test = ColorProducto(nombre="A-MERMA", codigo=1)
        db.session.add(c_test)
        db.session.flush()

        lote = LoteColor(numero_op=orden.numero_op, color_id=c_test.id, meta_kg=1000.0)
        db.session.add(lote)
        db.session.flush()

        receta = SeCompone(lote_id=lote.id, materia_prima_id=mp.id, fraccion=1.0)
        db.session.add(receta)
        db.session.flush()

        orden.actualizar_metricas()
        db.session.commit()

        # Cálculos esperados (nuevo sistema: sin extra_pct)
        # merma = colada / tiro = 10 / 110 = 0.090909...
        merma_pct = 10.0 / 110.0

        # peso material = meta_kg * (1 + merma_pct)
        peso_material_esperado = 1000.0 * (1 + merma_pct)

        print(f"Merma %: {merma_pct:.6f}")
        print(f"Peso Material Calc: {receta.peso_kg:.4f}")
        print(f"Peso Material Esperado: {peso_material_esperado:.4f}")

        assert receta.peso_kg == pytest.approx(peso_material_esperado, abs=0.01)

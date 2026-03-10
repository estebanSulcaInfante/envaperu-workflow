import pytest
from app.models.orden import OrdenProduccion, SnapshotComposicionMolde
from app.models.lote import LoteColor
from app.models.producto import ColorProducto
from app.extensions import db


def _mk_op(numero_op, snap_items, lotes_meta, **kwargs):
    """
    Helper: crea OrdenProduccion + SnapshotComposicionMolde + LoteColor.
    snap_items: [{cavidades, peso_unit_gr}]
    lotes_meta: [{color_id, meta_kg}] (pre-creados)
    kwargs: campos extra de OrdenProduccion
    """
    op = OrdenProduccion(numero_op=numero_op, **kwargs)
    db.session.add(op)
    db.session.flush()

    for item in snap_items:
        db.session.add(SnapshotComposicionMolde(
            orden_id     = numero_op,
            cavidades    = item['cavidades'],
            peso_unit_gr = item['peso_unit_gr'],
        ))
    db.session.flush()

    for l in lotes_meta:
        db.session.add(LoteColor(
            numero_op = numero_op,
            color_id  = l['color_id'],
            meta_kg   = l['meta_kg'],
        ))
    db.session.flush()

    op.actualizar_metricas()
    db.session.commit()
    return op


def test_calculos_metricas_basicas(client, app):
    """
    Verifica los cálculos principales con meta_kg por lote.

    Molde: 2 cav × 87g + 2g colada = 176g tiro
    Merma = 2/176 ≈ 1.136%

    Lote Rojo:  meta_kg = 174.0  → coladas = 174000/174 = 1000.0 exacto
    Lote Azul:  meta_kg = 175.0  → coladas = 175000/174 ≈ 1005.747  (sin redondeo)
    """
    with app.app_context():
        c_rojo = ColorProducto(nombre="ROJO-CALC", codigo=20)
        c_azul = ColorProducto(nombre="AZUL-CALC", codigo=21)
        db.session.add_all([c_rojo, c_azul])
        db.session.flush()

        op = _mk_op(
            "OP-CALC-META",
            snap_items=[{"cavidades": 2, "peso_unit_gr": 87.0}],
            lotes_meta=[
                {"color_id": c_rojo.id, "meta_kg": 174.0},
                {"color_id": c_azul.id, "meta_kg": 175.0},
            ],
            snapshot_peso_colada_gr = 2.0,
            snapshot_tiempo_ciclo   = 30.0,
            snapshot_horas_turno    = 23.0,
        )

        # --- Cabecera del golpe ---
        assert op.calculo_peso_neto_golpe == pytest.approx(174.0)   # 2 × 87
        assert op.calculo_peso_tiro_gr    == pytest.approx(176.0)   # 174 + 2

        # --- Merma (solo colada) ---
        merma_esperada = 2.0 / 176.0
        assert op.calculo_merma_pct == pytest.approx(merma_esperada, abs=0.0001)

        # --- Peso producción = suma de meta_kg ---
        assert op.calculo_peso_produccion == pytest.approx(349.0)   # 174 + 175

        # --- Coladas por lote (sin redondeo) ---
        lote_rojo = op.lotes[0]
        lote_azul = op.lotes[1]

        assert lote_rojo.calculo_coladas == pytest.approx(1000.0, abs=0.001)
        assert lote_rojo.calculo_kg_real == pytest.approx(174.0,  abs=0.001)

        coladas_azul_esperadas = 175000.0 / 174.0
        assert lote_azul.calculo_coladas == pytest.approx(coladas_azul_esperadas, rel=1e-4)
        # kg_real > meta (fracción de golpe de sobrante)
        assert lote_azul.calculo_kg_real >= lote_azul.meta_kg


def test_meta_kg_divisible_exacta(client, app):
    """
    Cuando meta_kg es exactamente divisible por peso_neto_golpe/1000,
    calculo_kg_real == meta_kg (sin sobrante).
    """
    with app.app_context():
        c = ColorProducto(nombre="EXACTO", codigo=30)
        db.session.add(c)
        db.session.flush()

        # 1 cav × 100g + 0 colada → peso_neto = 100g
        # meta_kg = 10.0 → coladas = 10000/100 = 100.0 exacta
        op = _mk_op(
            "OP-EXACTO",
            snap_items=[{"cavidades": 1, "peso_unit_gr": 100.0}],
            lotes_meta =[{"color_id": c.id, "meta_kg": 10.0}],
            snapshot_peso_colada_gr=0.0,
        )
        lote = op.lotes[0]
        assert lote.calculo_coladas == pytest.approx(100.0)
        assert lote.calculo_kg_real == pytest.approx(10.0)


def test_meta_kg_no_divisible(client, app):
    """
    Cuando meta_kg NO es divisible, coladas es float (sin ceil),
    y calculo_kg_real == meta_kg (la expresión es idéntica).
    """
    with app.app_context():
        c = ColorProducto(nombre="NODIV", codigo=31)
        db.session.add(c)
        db.session.flush()

        # 1 cav × 30g → meta_kg = 1.0 → coladas = 1000/30 = 33.333...
        op = _mk_op(
            "OP-NODIV",
            snap_items=[{"cavidades": 1, "peso_unit_gr": 30.0}],
            lotes_meta =[{"color_id": c.id, "meta_kg": 1.0}],
            snapshot_peso_colada_gr=0.0,
        )
        lote = op.lotes[0]
        assert lote.calculo_coladas == pytest.approx(1000.0 / 30.0, rel=1e-5)
        # kg_real recalculado desde coladas × peso → deberia ser igual a meta
        assert lote.calculo_kg_real == pytest.approx(1.0, rel=1e-5)


def test_peso_produccion_es_suma_de_meta_kg(client, app):
    """La OP.calculo_peso_produccion debe ser exactamente la suma de meta_kg de sus lotes."""
    with app.app_context():
        c1 = ColorProducto(nombre="S1", codigo=40)
        c2 = ColorProducto(nombre="S2", codigo=41)
        c3 = ColorProducto(nombre="S3", codigo=42)
        db.session.add_all([c1, c2, c3])
        db.session.flush()

        op = _mk_op(
            "OP-SUMA",
            snap_items=[{"cavidades": 4, "peso_unit_gr": 50.0}],
            lotes_meta=[
                {"color_id": c1.id, "meta_kg": 100.0},
                {"color_id": c2.id, "meta_kg": 250.0},
                {"color_id": c3.id, "meta_kg": 75.5},
            ],
            snapshot_peso_colada_gr=20.0,
        )
        assert op.calculo_peso_produccion == pytest.approx(425.5)
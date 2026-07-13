import pytest
from app.models.orden import OrdenProduccion, SnapshotComposicionMolde
from app.models.lote import LoteColor
from app.models.producto import ColorProduccion, ColorBase, FamiliaColor
from app.extensions import db

def _get_or_create_fam(nombre="SOLIDO", codigo=1):
    from app.models.producto import FamiliaColor
    fam = FamiliaColor.query.filter_by(nombre=nombre).first()
    if not fam:
        from app.extensions import db
        fam = FamiliaColor(nombre=nombre, codigo=codigo)
        db.session.add(fam)
        db.session.flush()
    return fam

def _create_color_prod(nombre, codigo=None, familia_id=None):
    from app.models.producto import ColorBase, ColorProduccion
    from app.extensions import db
    cb = ColorBase.query.filter_by(nombre=nombre).first()
    if not cb:
        cb = ColorBase(nombre=nombre)
        db.session.add(cb)
        db.session.flush()
    fam_id = familia_id if familia_id else _get_or_create_fam().id
    cp = ColorProduccion(color_base_id=cb.id, familia_color_id=fam_id, codigo_legacy=codigo)
    db.session.add(cp)
    db.session.flush()
    return cp



def test_coladas_float_sin_redondeo(client, app):
    """
    Verifica que calculo_coladas sea Float (sin redondeo).
    El sistema reporta el valor exacto; la planta opera en golpes enteros.
    """
    with app.app_context():
        c = _create_color_prod(nombre="FLOAT-TEST", codigo=50)
        db.session.add(c)
        db.session.flush()

        # 1 cav × 87g → meta_kg = 100.0 → coladas = 100000/87 = 1149.425...
        op = OrdenProduccion(numero_op="OP-FLOAT")
        db.session.add(op)
        db.session.flush()

        db.session.add(SnapshotComposicionMolde(
            orden_id=op.numero_op, cavidades=1, peso_unit_gr=87.0
        ))
        db.session.flush()

        lote = LoteColor(numero_op=op.numero_op, color_produccion_id=c.id, meta_kg=100.0)
        db.session.add(lote)
        db.session.flush()

        op.actualizar_metricas()
        db.session.commit()

        coladas_esperadas = 100_000.0 / 87.0
        assert lote.calculo_coladas == pytest.approx(coladas_esperadas, rel=1e-5)
        # No es entero
        assert lote.calculo_coladas != round(lote.calculo_coladas)


def test_meta_kg_directo_por_lote(client, app):
    """
    Cada lote tiene su propia meta_kg independiente.
    La OP suma las metas de todos los lotes.
    """
    with app.app_context():
        c1 = _create_color_prod(nombre="L-META-1", codigo=51)
        c2 = _create_color_prod(nombre="L-META-2", codigo=52)
        db.session.add_all([c1, c2])
        db.session.flush()

        op = OrdenProduccion(
            numero_op="OP-META-LOTE",
            snapshot_peso_colada_gr=5.0,
        )
        db.session.add(op)
        db.session.flush()

        db.session.add(SnapshotComposicionMolde(
            orden_id=op.numero_op, cavidades=2, peso_unit_gr=100.0
        ))
        db.session.flush()

        l1 = LoteColor(numero_op=op.numero_op, color_produccion_id=c1.id, meta_kg=200.0)
        l2 = LoteColor(numero_op=op.numero_op, color_produccion_id=c2.id, meta_kg=350.0)
        db.session.add_all([l1, l2])
        db.session.flush()

        op.actualizar_metricas()
        db.session.commit()

        # Suma correcta
        assert op.calculo_peso_produccion == pytest.approx(550.0)

        # Coladas independientes: peso_neto_golpe = 2×100 = 200g
        assert l1.calculo_coladas == pytest.approx(200_000.0 / 200.0)  # 1000
        assert l2.calculo_coladas == pytest.approx(350_000.0 / 200.0)  # 1750
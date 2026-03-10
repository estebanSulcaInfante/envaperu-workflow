import pytest
from app.models.molde import Molde, MoldePieza
from app.models.producto import Pieza, Linea, Familia
from app.extensions import db


def _mk_pieza(linea_id, familia_id, sku, nombre, cavidad, peso):
    return Pieza(
        sku=sku, cod_pieza=int(sku[-1]), piezas=nombre, tipo="SIMPLE",
        linea_id=linea_id, familia_id=familia_id,
        cavidad=cavidad, peso=peso
    )


def test_simple_molde_flow(client, app):
    """
    Test de molde simple: un molde con una sola pieza (vía MoldePieza).
    Verifica creación, propiedades calculadas y actualización.
    """
    molde_code = "MOL-SIMPLE-TEST"

    # ── Setup: necesitamos Linea y Familia para crear Pieza ──────────────────
    with app.app_context():
        linea  = Linea(codigo=99, nombre="LINEA-TEST")
        familia = Familia(codigo=99, nombre="FAM-TEST")
        db.session.add_all([linea, familia])
        db.session.flush()
        linea_id  = linea.id
        familia_id = familia.id
        db.session.commit()

    # ── 1. CREATE MOLDE (Simple Mode vía API) ────────────────────────────────
    payload = {
        "codigo":         molde_code,
        "nombre":         "Molde Simple Test",
        "peso_tiro_gr":   100.0,
        "tiempo_ciclo_std": 20.0,
        "cavidades":      4,          # Simple: 1 tipo de pieza
        "peso_unitario_gr": 20.0      # Peso por cavidad
    }

    resp = client.post('/api/moldes', json=payload)
    if resp.status_code != 201:
        print(f"CREATE ERROR: {resp.get_json()}")

    assert resp.status_code == 201
    data = resp.get_json()
    assert data['codigo'] == molde_code

    # ── 2. Verify DB ─────────────────────────────────────────────────────────
    with app.app_context():
        molde = db.session.get(Molde, molde_code)
        assert molde is not None
        assert molde.peso_tiro_gr == 100.0

        # Debe haber UNA fila en MoldePieza (modo simple → 1 pieza)
        mps = MoldePieza.query.filter_by(molde_id=molde_code).all()
        assert len(mps) == 1
        mp = mps[0]
        assert mp.cavidades == 4
        assert mp.peso_unitario_gr == 20.0

        # Propiedades calculadas del molde
        assert molde.peso_neto_gr == 80.0          # 4 × 20
        assert abs(molde.merma_pct - 0.2) < 0.001  # (100-80)/100

        # Pieza.molde_id ya NO existe — la pieza ahora se vincula solo por MoldePieza
        pieza = db.session.get(Pieza, mp.pieza_sku)
        assert pieza is not None
        assert not hasattr(pieza, 'molde_id') or pieza.molde_id is None  # campo eliminado

    # ── 3. UPDATE MOLDE ──────────────────────────────────────────────────────
    update_payload = {
        "peso_tiro_gr":    110.0,
        "cavidades":       4,
        "peso_unitario_gr": 25.0      # 4×25=100g neto, merma=(110-100)/110
    }

    resp = client.put(f'/api/moldes/{molde_code}', json=update_payload)
    assert resp.status_code == 200

    with app.app_context():
        molde = db.session.get(Molde, molde_code)
        assert molde.peso_tiro_gr == 110.0

        mp = MoldePieza.query.filter_by(molde_id=molde_code).first()
        assert mp.peso_unitario_gr == 25.0

        assert molde.peso_neto_gr == 100.0         # 4 × 25
        expected_merma = 10 / 110
        assert abs(molde.merma_pct - expected_merma) < 0.001

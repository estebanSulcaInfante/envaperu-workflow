import pytest
from app.models.orden import OrdenProduccion, SnapshotComposicionMolde
from app.models.lote import LoteColor
from app.models.materiales import MateriaPrima, Colorante
from app.models.molde import Molde, MoldePieza
from app.models.producto import Pieza, Linea, Familia
from app.extensions import db


def _setup_molde(app):
    """Helper: crea un molde simple con 1 pieza, 2 cavidades.
    Reutiliza Linea y Familia creadas por conftest (codigo=1).
    """
    with app.app_context():
        # Reuse existing Linea/Familia from conftest — no crear duplicados
        linea = Linea.query.first()
        familia = Familia.query.first()

        molde = Molde(codigo="MOL-TEST", nombre="Molde Test", peso_tiro_gr=22.0, tiempo_ciclo_std=10.0)
        db.session.add(molde)
        db.session.flush()

        pieza = Pieza(sku="PIEZA-TEST", cod_pieza=101, piezas="Pieza Test",
                      tipo="SIMPLE", linea_id=linea.id, familia_id=familia.id,
                      cavidad=2, peso=10.0)
        db.session.add(pieza)
        db.session.flush()

        mp_rel = MoldePieza(molde_id="MOL-TEST", pieza_sku="PIEZA-TEST", cavidades=2, peso_unitario_gr=10.0)
        db.session.add(mp_rel)
        db.session.commit()


def test_crear_orden_manual_snapshot(client, app):
    """
    Crea una OP usando snapshot_composicion manual (sin auto_snapshot_molde).
    Verifica que la composición se persiste correctamente.
    """
    with app.app_context():
        mp = MateriaPrima(nombre="TEST MP", tipo="VIRGEN")
        col = Colorante(nombre="TEST COL")
        db.session.add_all([mp, col])
        from app.models.producto import ColorProducto, FamiliaColor
        fam = FamiliaColor(nombre="STD")
        db.session.add(fam)
        db.session.flush()
        c_prod = ColorProducto(nombre="ROJO", codigo=10, familia_id=fam.id)
        db.session.add(c_prod)
        db.session.commit()

    payload = {
        "numero_op": "OP-MANUAL-SNAP",
        "maquina_id": 1,
        "producto": "Pieza Simple",
        "snapshot_tiempo_ciclo": 10.0,
        "snapshot_horas_turno": 8.0,
        "snapshot_peso_colada_gr": 2.0,

        # Modo manual: composición explícita
        "snapshot_composicion": [
            {"pieza_sku": None, "cavidades": 2, "peso_unit_gr": 10.0}
        ],

        "lotes": [
            {
                "color_nombre": "ROJO",
                "meta_kg": 100.0,
                "personas": 3,
                "materiales": [{"nombre": "TEST MP", "tipo": "VIRGEN", "fraccion": 1.0}],
                "pigmentos": [{"nombre": "TEST COL", "gramos": 15.0}]
            }
        ]
    }

    response = client.post('/api/ordenes', json=payload)
    if response.status_code != 201:
        print(f"\nResponse Error: {response.get_json()}")

    assert response.status_code == 201
    data = response.get_json()
    assert data['numero_op'] == "OP-MANUAL-SNAP"
    assert len(data['lotes']) == 1

    lote = data['lotes'][0]
    assert lote['Color'] == "ROJO"
    assert lote['mano_obra']['personas'] == 3
    assert len(lote['materiales']) == 1
    assert lote['materiales'][0]['fraccion'] == 1.0

    # Verificar snapshot técnico
    snap = data['snapshot_tecnico']
    assert snap['cavidades_totales'] == 2
    assert snap['peso_neto_golpe_gr'] == 20.0   # 2 cav × 10g
    assert snap['peso_tiro_gr'] == 22.0          # 20g + 2g colada
    assert snap['es_multipieza'] == False

    # Verificar persistencia en BD
    with app.app_context():
        orden_db = OrdenProduccion.query.filter_by(numero_op="OP-MANUAL-SNAP").first()
        assert orden_db is not None
        assert len(orden_db.snapshot_composicion) == 1
        assert orden_db.snapshot_composicion[0].cavidades == 2
        assert orden_db.snapshot_composicion[0].peso_unit_gr == 10.0
        assert len(orden_db.lotes) == 1
        lote_db = orden_db.lotes[0]
        assert len(lote_db.materias_primas) == 1
        assert len(lote_db.colorantes) == 1


def test_crear_orden_auto_snapshot(client, app):
    """
    Crea una OP con auto_snapshot_molde:true.
    Verifica que la composición se deriva desde MoldePieza del catálogo.
    """
    _setup_molde(app)

    payload = {
        "numero_op": "OP-AUTO-SNAP",
        "maquina_id": 1,
        "molde_id": "MOL-TEST",
        "snapshot_tiempo_ciclo": 10.0,
        "snapshot_horas_turno": 8.0,
        "snapshot_peso_colada_gr": 2.0,

        "auto_snapshot_molde": True,
        "lotes": []
    }

    response = client.post('/api/ordenes', json=payload)
    if response.status_code != 201:
        print(f"\nResponse Error: {response.get_json()}")

    assert response.status_code == 201
    data = response.get_json()

    snap = data['snapshot_tecnico']
    assert snap['cavidades_totales'] == 2
    assert snap['peso_neto_golpe_gr'] == 20.0    # 2 × 10g
    assert snap['peso_tiro_gr'] == 22.0           # 20 + 2 colada
    assert len(snap['composicion']) == 1
    assert snap['composicion'][0]['pieza_sku'] == "PIEZA-TEST"

    # Verificar que el catálogo ya no afecta el snapshot congelado
    with app.app_context():
        mp_row = MoldePieza.query.filter_by(molde_id="MOL-TEST").first()
        mp_row.peso_unitario_gr = 99.0  # Cambiamos el catálogo
        db.session.commit()

        orden_db = OrdenProduccion.query.filter_by(numero_op="OP-AUTO-SNAP").first()
        # El snapshot sigue en 10g, no en 99g
        assert orden_db.snapshot_composicion[0].peso_unit_gr == 10.0

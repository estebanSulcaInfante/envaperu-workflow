"""Contrato US-007 para el snapshot abstracto de la composicion del molde.

Estas pruebas cubren la migracion estructural. La reconciliacion de una OP
legacy real sigue condicionada a disponer de la primera muestra controlada.
"""

from sqlalchemy import inspect

from app.extensions import db
from app.models.molde import Molde, MoldePieza, Pieza
from app.models.orden import OrdenProduccion, SnapshotComposicionMolde
from app.models.producto import Familia, Linea


def _seed_mold_composition(app):
    with app.app_context():
        linea = Linea.query.first()
        familia = Familia.query.first()
        pieza = Pieza(
            codigo="PZ-SNAPSHOT-001",
            nombre="Cuerpo historico",
            linea_id=linea.id,
            familia_id=familia.id,
            peso_nominal_gr=40.0,
            activo=True,
        )
        molde = Molde(
            codigo="ML-SNAPSHOT-001",
            nombre="Molde snapshot normalizado",
            peso_tiro_gr=90.0,
            tiempo_ciclo_std=15.0,
            activo=True,
        )
        relacion = MoldePieza(
            molde=molde,
            pieza=pieza,
            cavidades=2,
            peso_unitario_gr=40.0,
            activo=True,
        )
        db.session.add_all([pieza, molde, relacion])
        db.session.commit()
        return {
            "pieza_id": pieza.id,
            "molde_id": molde.codigo,
            "pieza_codigo": pieza.codigo,
            "pieza_nombre": pieza.nombre,
        }


def _new_order_payload(catalog):
    return {
        "numero_op": "OP-SNAPSHOT-NORMALIZADO",
        "maquina_id": 1,
        "molde_id": catalog["molde_id"],
        "auto_snapshot_molde": True,
        "snapshot_composicion": [],
        "snapshot_tiempo_ciclo": 15.0,
        "snapshot_horas_turno": 8.0,
        "snapshot_peso_colada_gr": 10.0,
        "lotes": [],
    }


def test_snapshot_schema_references_abstract_piece_and_keeps_legacy_as_text(app):
    """MIG-01: la FK canonica apunta a Pieza, nunca a PiezaColor."""

    with app.app_context():
        columns = {
            column["name"]: column
            for column in inspect(db.engine).get_columns("snapshot_composicion_molde")
        }
        assert "pieza_id" in columns
        assert columns["pieza_id"]["nullable"] is True
        assert "pieza_codigo_snapshot" in columns
        assert "pieza_nombre_snapshot" in columns
        assert "pieza_sku_legacy" in columns
        assert columns["pieza_sku_legacy"]["nullable"] is True
        assert "pieza_sku" not in columns

        foreign_keys = inspect(db.engine).get_foreign_keys(
            "snapshot_composicion_molde"
        )
        assert any(
            fk["constrained_columns"] == ["pieza_id"]
            and fk["referred_table"] == "pieza"
            and fk["referred_columns"] == ["id"]
            for fk in foreign_keys
        )
        assert all(
            "pieza_sku_legacy" not in fk["constrained_columns"]
            for fk in foreign_keys
        )


def test_new_order_writes_canonical_snapshot_and_freezes_piece_text(client, app):
    """MIG-02: una OP nueva no persiste ni depende de un SKU coloreado."""

    catalog = _seed_mold_composition(app)

    response = client.post("/api/ordenes", json=_new_order_payload(catalog))
    assert response.status_code == 201, response.get_json()

    composition = response.get_json()["snapshot_tecnico"]["composicion"]
    assert len(composition) == 1
    created_piece = composition[0]
    assert created_piece["pieza_id"] == catalog["pieza_id"]
    assert created_piece["pieza_codigo_snapshot"] == catalog["pieza_codigo"]
    assert created_piece["pieza_nombre_snapshot"] == catalog["pieza_nombre"]
    assert created_piece["pieza_sku_legacy"] is None
    assert created_piece["cavidades"] == 2
    assert created_piece["peso_unit_gr"] == 40.0
    assert created_piece["peso_subtotal_gr"] == 80.0
    assert "pieza_sku" not in created_piece

    with app.app_context():
        snapshot = SnapshotComposicionMolde.query.one()
        assert snapshot.pieza_id == catalog["pieza_id"]
        assert snapshot.pieza_sku_legacy is None
        assert snapshot.pieza_codigo_snapshot == catalog["pieza_codigo"]
        assert snapshot.pieza_nombre_snapshot == catalog["pieza_nombre"]

        pieza = db.session.get(Pieza, catalog["pieza_id"])
        pieza.codigo = "PZ-SNAPSHOT-RENOMBRADA"
        pieza.nombre = "Nombre vigente cambiado"
        db.session.commit()

    historical = client.get("/api/ordenes/OP-SNAPSHOT-NORMALIZADO")
    assert historical.status_code == 200
    historical_piece = historical.get_json()["snapshot_tecnico"]["composicion"][0]
    assert historical_piece["pieza_id"] == catalog["pieza_id"]
    assert historical_piece["pieza_codigo_snapshot"] == catalog["pieza_codigo"]
    assert historical_piece["pieza_nombre_snapshot"] == catalog["pieza_nombre"]
    assert "pieza_sku" not in historical_piece


def test_unresolved_legacy_evidence_does_not_infer_a_piece(app):
    """La ventana legacy conserva evidencia, pero no inventa genealogia.

    No certifica MIG-03: esa reconciliacion requiere una OP legacy real y su
    checklist de evidencia. Solo fija el comportamiento seguro previo.
    """

    with app.app_context():
        order = OrdenProduccion(numero_op="OP-LEGACY-SIN-CONCILIAR")
        db.session.add(order)
        db.session.flush()
        snapshot = SnapshotComposicionMolde(
            orden_id=order.numero_op,
            pieza_id=None,
            pieza_codigo_snapshot=None,
            pieza_nombre_snapshot=None,
            pieza_sku_legacy="PC-LEGACY-SIN-EVIDENCIA",
            cavidades=1,
            peso_unit_gr=25.0,
        )
        db.session.add(snapshot)
        db.session.commit()

        persisted = SnapshotComposicionMolde.query.one()
        assert persisted.pieza_id is None
        assert persisted.pieza_sku_legacy == "PC-LEGACY-SIN-EVIDENCIA"
        serialized = persisted.to_dict()
        assert serialized["pieza_id"] is None
        assert "pieza_sku" not in serialized

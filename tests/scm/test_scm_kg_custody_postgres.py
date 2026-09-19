"""PostgreSQL locking regressions for KG custody."""

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.schema import DropSchema

from test_scm_migrations_postgres import _isolated_postgres_url, _run_flask_db


pytestmark = pytest.mark.postgres


@pytest.fixture
def pg_custody_app():
    admin, schema, schema_url = _isolated_postgres_url()
    _run_flask_db(schema_url, "upgrade", "head")
    from app import create_app, db
    from app.config import Config

    # tests/conftest imports app.config after forcing SQLite.  The factory
    # reads the Config class at call time, so override the class URI for this
    # isolated schema rather than changing an already initialized app.
    original_uri = Config.SQLALCHEMY_DATABASE_URI
    Config.SQLALCHEMY_DATABASE_URI = schema_url.render_as_string(
        hide_password=False
    )
    app = create_app()
    app.config.update(TESTING=True, KG_CUSTODY_WRITE_ENABLED=True)
    try:
        yield app
    finally:
        with app.app_context():
            db.session.remove()
            db.engine.dispose()
        Config.SQLALCHEMY_DATABASE_URI = original_uri
        with admin.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        admin.dispose()


def _seed_custody(app):
    from app import db
    from app.models.scm_articulos import ScmArticulo
    from app.models.scm_catalogos import ScmCapacidad
    from app.models.scm_inventory import ScmUbicacionInventario
    from app.models.scm_inventory_kg import ScmSaldoInventarioKg, ScmUnidadFisicaKg
    from app.models.scm_inventory_operations import ScmAlmacen, ScmAlmacenTrabajador
    from app.models.scm_articulos import ScmDefinicionWip
    from app.models.trabajador import RolOperativo, Trabajador

    capabilities = [
        "PICKING_PREPARAR", "RETORNO_RECIBIR", "ABASTECIMIENTO_DEVOLVER",
        "UNIDAD_LOGISTICA_FRACCIONAR",
    ]
    role = RolOperativo(codigo=f"PG-KG-{uuid4().hex[:8]}".upper(), nombre="PG KG")
    actor = Trabajador(codigo=f"PG-KG-{uuid4().hex[:8]}".upper(), nombres="PG", apellidos="KG", activo=True)
    db.session.add(role)
    for code in capabilities:
        capacity = ScmCapacidad.query.filter_by(codigo=code).one()
        role.capacidades.append(capacity)
    actor.roles.append(role)
    article_code = f"PG-KG-{uuid4().hex[:8]}".upper()
    article = ScmArticulo(codigo=article_code, nombre="KG race", clase="SUBENSAMBLE_WIP", unidad_inventario="KG")
    warehouse = ScmAlmacen(codigo=f"PG-KG-{uuid4().hex[:8]}".upper(), nombre="PG KG", tipo="PIEZAS_WIP")
    location = ScmUbicacionInventario(codigo=f"PG-KG-{uuid4().hex[:8]}".upper(), nombre="PG KG", almacen=warehouse, tipo="POSICION")
    db.session.add_all([actor, article, warehouse, location])
    db.session.flush()
    db.session.add(ScmDefinicionWip(articulo_id=article.id, descripcion="PG KG"))
    db.session.flush()
    db.session.add(ScmAlmacenTrabajador(
        almacen_id=warehouse.id,
        trabajador_id=actor.id,
        clases_articulo_json=["SUBENSAMBLE_WIP"],
        asignado_por_id=actor.id,
    ))
    db.session.flush()
    saldo = ScmSaldoInventarioKg(articulo_scm_id=article.id, ubicacion_id=location.id, cantidad_fisica_kg=5, version=1)
    db.session.add(saldo)
    db.session.flush()
    units = []
    for index in range(2):
        unit = ScmUnidadFisicaKg(
            public_id=uuid4(), codigo=f"PG-KG-U{index}-{uuid4().hex[:6]}",
            articulo_scm_id=article.id, estado="ACTIVA", estado_logistico="RECIBIDA_ALMACEN",
            estado_calidad="LIBERADA", saldo_id=saldo.id, ubicacion_id=location.id,
            kg_entregado=5, kg_verificados=5, almacen_responsable_id=warehouse.id, version=1,
        )
        db.session.add(unit)
        units.append(unit)
    db.session.flush()
    for unit in units:
        unit.unidad_raiz_id = unit.id
    db.session.commit()
    return actor.id, article.id, warehouse.id, location.id, saldo.id, [unit.id for unit in units]


def test_pg_custody_locks_shared_saldo_and_causal_root(pg_custody_app):
    """Two reservations share one saldo; sibling measurements share one root lock."""
    from app import db
    from app.models.scm_auditoria import ScmOperacion
    from app.models.scm_inventory_kg import (
        ScmEtiquetaUnidadKg, ScmMedicionUnidadKg,
        ScmReservaUnidadKg, ScmRetiroArmadoKg, ScmRetiroArmadoKgItem,
        ScmUnidadFisicaKg,
    )
    from app.services.scm_kg_custody_service import capture_kg_measurement, divide_kg_unit, reserve_kg_unit
    from app.services.scm_service_support import ScmServiceError
    import hashlib, json

    with pg_custody_app.app_context():
        actor_id, article_id, warehouse_id, location_id, saldo_id, unit_ids = _seed_custody(pg_custody_app)

    barrier = Barrier(2)

    def reserve_candidate(unit_id):
        with pg_custody_app.app_context():
            try:
                barrier.wait(timeout=15)
                result = reserve_kg_unit(
                    db.session, actor_id=actor_id, unit_id=unit_id, operation_id=uuid4(),
                    data={"version": 1, "documento_destino_tipo": "OA", "documento_destino_id": "PG-RACE"},
                )
                return "reserved", result
            except ScmServiceError as error:
                db.session.rollback()
                return error.code, None

    with ThreadPoolExecutor(max_workers=2) as executor:
        reservation_results = list(executor.map(reserve_candidate, unit_ids))
    assert sorted(item[0] for item in reservation_results) == ["INVENTORY_CONFLICT", "reserved"]

    with pg_custody_app.app_context():
        # Build one withdrawn root with two measured children, each proposing
        # 3 kg.  The second transaction must observe the first measurement.
        root = ScmUnidadFisicaKg(
            public_id=uuid4(), codigo=f"PG-KG-ROOT-{uuid4().hex[:6]}", articulo_scm_id=article_id,
            estado="ACTIVA", estado_logistico="RETIRADA_ARMADO", estado_calidad="LIBERADA",
            kg_entregado=5, almacen_responsable_id=warehouse_id, version=1,
        )
        db.session.add(root); db.session.flush(); root.unidad_raiz_id = root.id
        operation_id = uuid4()
        db.session.add(ScmOperacion(operation_id=operation_id, endpoint="/pg/fixture/retiro", actor_id=actor_id, request_sha256="a" * 64, estado_http=201, response_json={}))
        db.session.flush()
        retiro = ScmRetiroArmadoKg(
            codigo=f"PG-RET-{uuid4().hex[:8]}", almacen_responsable_id=warehouse_id,
            actor_id=actor_id, estado="ABIERTO", operation_id=operation_id,
        )
        db.session.add(retiro); db.session.flush()
        reservation = ScmReservaUnidadKg(
            unidad_id=root.id, cantidad_snapshot_kg=5, estado="RETIRADA", actor_id=actor_id,
            operation_id=operation_id,
        )
        db.session.add(reservation); db.session.flush()
        db.session.add(ScmRetiroArmadoKgItem(retiro_id=retiro.id, unidad_id=root.id, reserva_id=reservation.id, neto_entregado_kg=5))
        children = []
        for index in range(2):
            child = ScmUnidadFisicaKg(
                public_id=uuid4(), codigo=f"PG-KG-CHILD-{index}-{uuid4().hex[:6]}", articulo_scm_id=article_id,
                unidad_padre_id=root.id, unidad_raiz_id=root.id, intencion="RETORNO", estado="ACTIVA",
                estado_logistico="PENDIENTE_VERIFICACION", estado_calidad="LIBERADA",
                modo_lectura="NET_DIRECTO", almacen_responsable_id=warehouse_id, version=1,
            )
            db.session.add(child); db.session.flush()
            payload = {"v": 1, "label_id": str(uuid4())}
            payload["qr_value"] = json.dumps(payload, separators=(",", ":"))
            db.session.add(ScmEtiquetaUnidadKg(
                public_id=UUID(payload["label_id"]), unidad_id=child.id, estado="IMPRESA",
                payload_json=payload, payload_hash=hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
            ))
            children.append((child.id, child.public_id, payload["label_id"]))
        root.intencion = "RETORNO"
        root.modo_lectura = "NET_DIRECTO"
        root_label_id = uuid4()
        root_payload = {
            "v": 1,
            "label_id": str(root_label_id),
            "qr_value": json.dumps({"v": 1, "label_id": str(root_label_id)}, separators=(",", ":")),
        }
        db.session.add(ScmEtiquetaUnidadKg(
            public_id=root_label_id,
            unidad_id=root.id,
            estado="IMPRESA",
            payload_json=root_payload,
            payload_hash=hashlib.sha256(json.dumps(root_payload, sort_keys=True).encode()).hexdigest(),
        ))
        root_id_value = root.id
        root_public_id_value = root.public_id
        stale_version_value = root.version
        retiro_id_value = retiro.id
        db.session.commit()

    barrier = Barrier(2)

    def capture_with_reading(child):
        child_id, public_id, label_id = child
        reading_id = str(uuid4())
        nonce = hashlib.sha256(json.dumps([str(child_id), 1, str(public_id)], separators=(",", ":"), sort_keys=True).encode()).hexdigest()[:32]
        with pg_custody_app.app_context():
            try:
                barrier.wait(timeout=15)
                result = capture_kg_measurement(
                    db.session, actor_id=actor_id, station_id="PG-SERIAL", unit_id=child_id,
                    operation_id=uuid4(), data={"version": 1, "reading_id": reading_id, "label_id": label_id, "expected_unit_source_nonce": nonce},
                    snapshot={"source": "SERIAL", "reading_id": reading_id, "received_at_utc": "2026-09-18T20:00:00+00:00", "peso_kg": "3.000", "stable": True},
                )
                return "measured", result
            except ScmServiceError as error:
                db.session.rollback()
                return error.code, None

    with ThreadPoolExecutor(max_workers=2) as executor:
        measurement_results = list(executor.map(capture_with_reading, children))
        assert sorted(item[0] for item in measurement_results) == ["KG_RETURN_EXCEEDS_DELIVERY", "measured"]

    # A stale parent context must not capture after a concurrent division has
    # made the parent historical.  The capture call carries the old version;
    # the service refreshes the locked row before evaluating state.
    stale_version = stale_version_value
    division_started = Event()
    start = Barrier(2)
    with pg_custody_app.app_context():
        stale_parent = db.session.get(ScmUnidadFisicaKg, root_id_value)
        assert stale_parent is not None
        assert stale_parent.version == stale_version
        assert stale_parent.estado == "ACTIVA"

    def divide_parent():
        with pg_custody_app.app_context():
            try:
                start.wait(timeout=15)
                result = divide_kg_unit(
                    db.session,
                    actor_id=actor_id,
                    retiro_id=retiro_id_value,
                    operation_id=uuid4(),
                    data={"version": stale_version, "partes": [{"intencion": "RETORNO"}, {"intencion": "PERMANECE"}]},
                )
                division_started.set()
                return "divided", result
            except ScmServiceError as error:
                division_started.set()
                db.session.rollback()
                return error.code, None

    def capture_stale_parent():
        with pg_custody_app.app_context():
            try:
                start.wait(timeout=15)
                division_started.wait(timeout=15)
                stale_reading = str(uuid4())
                result = capture_kg_measurement(
                    db.session,
                    actor_id=actor_id,
                    station_id="PG-SERIAL",
                    unit_id=root_id_value,
                    operation_id=uuid4(),
                    data={
                        "version": stale_version,
                        "reading_id": stale_reading,
                        "label_id": str(root_label_id),
                        "expected_unit_source_nonce": hashlib.sha256(
                            json.dumps([str(root_id_value), stale_version, str(root_public_id_value)], separators=(",", ":"), sort_keys=True).encode()
                        ).hexdigest()[:32],
                    },
                    snapshot={"source": "SERIAL", "reading_id": stale_reading, "received_at_utc": "2026-09-18T20:10:00+00:00", "peso_kg": "1.000", "stable": True},
                )
                return "measured", result
            except ScmServiceError as error:
                db.session.rollback()
                return error.code, None

    with ThreadPoolExecutor(max_workers=2) as executor:
        stale_results = list(executor.map(lambda fn: fn(), (divide_parent, capture_stale_parent)))
    assert stale_results[0][0] == "divided"
    assert stale_results[1][0] == "KG_UNIT_COMPETING_OPERATION"
    assert stale_parent.estado == "ACTIVA"
    with pg_custody_app.app_context():
        refreshed_parent = db.session.get(ScmUnidadFisicaKg, root_id_value)
        assert refreshed_parent.estado == "HISTORICA"

    with pg_custody_app.app_context():
        measurement_id = db.session.scalar(text(
            "SELECT id FROM scm_medicion_unidad_kg LIMIT 1"
        ))
        item_id = db.session.scalar(text(
            "SELECT id FROM scm_retiro_armado_kg_item LIMIT 1"
        ))
        assert ScmMedicionUnidadKg.query.count() == 1
        with pytest.raises(DBAPIError) as measurement_error:
            db.session.execute(text(
                "UPDATE scm_medicion_unidad_kg SET neto_kg = 4.000 WHERE id = :id"
            ), {"id": measurement_id})
            db.session.commit()
        assert "append_only" in str(measurement_error.value.orig).lower()
        db.session.rollback()
        with pytest.raises(DBAPIError) as item_error:
            db.session.execute(text(
                "DELETE FROM scm_retiro_armado_kg_item WHERE id = :id"
            ), {"id": item_id})
            db.session.commit()
        assert "append_only" in str(item_error.value.orig).lower()
        db.session.rollback()

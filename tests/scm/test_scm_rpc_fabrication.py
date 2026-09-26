from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.services.scm_fabrication_order_service import (
    _validate_physical_output_set,
)
from app.services.scm_process_resolution import resolve_order_process
from app.services.scm_service_support import ScmServiceError


def _rpc_catalog(app):
    from app import db
    from app.models.maquina import Maquina, TipoMaquina
    from app.models.molde import Molde, MoldePieza, Pieza
    from app.models.producto import ColorBase, ColorProduccion, FamiliaColor, PiezaColor
    from app.models.scm_articulos import ScmArticulo
    from app.services.scm_article_service import _ensure_piece_article
    from app.models.scm_rutas import ScmCentroTrabajo, ScmOperacionRuta, ScmRutaRevision
    from app.models.trabajador import RolOperativo, Trabajador
    from app.services.scm_configuration import ensure_initial_scm_configuration
    from app.services.scm_route_service import _content_hash

    ensure_initial_scm_configuration()
    actor = Trabajador.query.filter_by(codigo="TRB-01").one()
    role = RolOperativo.query.filter_by(codigo="JEFE_PRODUCCION").one()
    if role not in actor.roles:
        actor.roles.append(role)
    engineering_role = RolOperativo.query.filter_by(codigo="INGENIERIA_SCM").one()
    if engineering_role not in actor.roles:
        actor.roles.append(engineering_role)
    suffix = uuid4().hex[:8].upper()
    bases = [
        ColorBase(nombre=f"RPC BASE A {suffix}"),
        ColorBase(nombre=f"RPC BASE B {suffix}"),
    ]
    family = FamiliaColor(nombre=f"RPC FAMILY {suffix}")
    db.session.add_all([*bases, family])
    db.session.flush()
    colors = [
        ColorProduccion(color_base_id=base.id, familia_color_id=family.id)
        for base in bases
    ]
    mold = Molde(
        codigo=f"ML-RPC-{suffix}",
        nombre="Molde RPC integración",
        peso_tiro_gr=80,
        tiempo_ciclo_std=30,
    )
    pieces = [
        Pieza(codigo=f"P-RPC-{suffix}-1", nombre="RPC pieza 1", peso_nominal_gr=10),
        Pieza(codigo=f"P-RPC-{suffix}-2", nombre="RPC pieza 2", peso_nominal_gr=20),
    ]
    mold.piezas.extend([
        MoldePieza(pieza=pieces[0], cavidades=2, peso_unitario_gr=10),
        MoldePieza(pieza=pieces[1], cavidades=1, peso_unitario_gr=20),
    ])
    db.session.add_all([*colors, mold])
    db.session.flush()
    article_ids = []
    for color in colors:
        per_color = []
        for index, piece in enumerate(pieces, start=1):
            pc = PiezaColor(
                sku=f"PC-RPC-{suffix}-{color.id}-{index}",
                pieza_rel=piece,
                color_produccion_rel=color,
                piezas=piece.nombre,
                peso=piece.peso_nominal_gr,
            )
            db.session.add(pc)
            db.session.flush()
            _ensure_piece_article(db.session, pc)
            article = db.session.scalar(
                select(ScmArticulo).where(ScmArticulo.codigo == pc.sku)
            )
            per_color.append(article.id)
        article_ids.append(per_color)
    machine_type = TipoMaquina(
        codigo=f"RPC-SOP-{suffix}", nombre="RPC soplado", proceso="SOPLADO"
    )
    machine = Maquina(
        codigo=f"MQ-RPC-{suffix}", nombre="Máquina RPC", tipo_maquina=machine_type,
        estado="OPERATIVA", activo=True,
    )
    injection_type = TipoMaquina(
        codigo=f"RPC-INJ-{suffix}", nombre="RPC inyección", proceso="INYECCION"
    )
    injection_machine = Maquina(
        codigo=f"MQ-RPC-INJ-{suffix}", nombre="Máquina RPC inyección",
        tipo_maquina=injection_type, estado="OPERATIVA", activo=True,
    )
    center = ScmCentroTrabajo(
        codigo=f"CTR-RPC-{suffix}", nombre="Centro RPC", tipo="SOPLADO"
    )
    db.session.add_all([machine, injection_machine, center])
    db.session.flush()
    operation_ids = []
    for target_id in [items[0] for items in article_ids]:
        route = ScmRutaRevision(
            articulo_objetivo_id=target_id,
            numero_revision=1,
            estado="APROBADA",
            creada_por_id=actor.id,
            aprobada_por_id=actor.id,
        )
        operation = ScmOperacionRuta(
            clave="SOPLAR",
            secuencia_visible=10,
            nombre="Soplar RPC",
            tipo="SOPLADO",
            executor_kind="OP_OT",
            centro_trabajo_id=center.id,
            articulo_salida_id=target_id,
        )
        route.operaciones.append(operation)
        db.session.add(route)
        db.session.flush()
        route.content_hash = _content_hash(route)
        operation_ids.append(operation.id)
    db.session.commit()
    return {
        "actor_id": actor.id,
        "mold_id": mold.codigo,
        "machine_id": machine.id,
        "injection_machine_id": injection_machine.id,
        "colors": [color.id for color in colors],
        "article_ids": article_ids,
        "operation_ids": operation_ids,
    }


def _rpc_payload(catalog, *, process=None, linked=True, machine_id=None):
    runs = []
    for index, color_id in enumerate(catalog["colors"]):
        run = {
            "color_produccion_id": color_id,
            "ciclos_objetivo": 2,
            "salidas": [
                {"articulo_scm_id": catalog["article_ids"][index][0], "cantidad_por_ciclo": 2, "peso_unitario_g": 10},
                {"articulo_scm_id": catalog["article_ids"][index][1], "cantidad_por_ciclo": 1, "peso_unitario_g": 20},
            ],
        }
        if linked:
            run["operacion_ruta_revision_id"] = catalog["operation_ids"][index]
        runs.append(run)
    payload = {
        "motivo": "RPC integración",
        "molde_id": catalog["mold_id"],
        "maquina_prevista_id": machine_id or catalog["machine_id"],
        "snapshot_tiempo_ciclo_seg": 30,
        "snapshot_horas_turno": 8,
        "snapshot_peso_colada_gr": 40,
        "corridas": runs,
    }
    if process is not None:
        payload["proceso"] = process
    return payload


def _patch_payload(body, *, route_ids, process_marker=None, machine_id=None):
    payload = {
        "version": body["version"],
        "molde_id": body["molde_id"],
        "maquina_prevista_id": machine_id or body["maquina_prevista_id"],
        "snapshot_tiempo_ciclo_seg": body["snapshot_tiempo_ciclo_seg"],
        "snapshot_horas_turno": body["snapshot_horas_turno"],
        "snapshot_peso_colada_gr": body["snapshot_peso_colada_gr"],
        "corridas": [],
    }
    for index, run in enumerate(body["corridas"]):
        payload["corridas"].append({
            "id": run["id"],
            "color_produccion_id": run["color_produccion_id"],
            "ciclos_objetivo": run["ciclos_objetivo"],
            "receta_revision_id": run["receta_revision_id"],
            "operacion_ruta_revision_id": route_ids[index],
            "salidas": [
                {"id": output["id"], "cantidad_por_ciclo": output["cantidad_por_ciclo_snapshot"], "peso_unitario_g": output["peso_unitario_snapshot_g"]}
                for output in run["salidas"]
            ],
        })
    if process_marker is not None:
        payload["proceso"] = process_marker
    return payload


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class _Session:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self, _statement):
        return _Rows(self.rows)


def _mold():
    return SimpleNamespace(
        codigo="ML-RPC-TEST",
    )


def _piece(piece_id, cavities, weight):
    return SimpleNamespace(
        pieza_id=piece_id,
        cavidades=cavities,
        peso_unitario_gr=weight,
        activo=True,
        molde_id="ML-RPC-TEST",
    )


def _article(article_id, piece_id, color_id):
    pc = SimpleNamespace(
        pieza_id=piece_id,
        color_produccion_id=color_id,
    )
    return SimpleNamespace(
        id=article_id,
        pieza_color=SimpleNamespace(pieza_color=pc),
    )


def _pc_run(color_id, suffix=""):
    return [
        (_article(f"PC-{color_id}-1{suffix}", 1, color_id), 2, 10),
        (_article(f"PC-{color_id}-2{suffix}", 2, color_id), 1, 20),
    ]


def test_two_colors_two_pieces_are_validated_independently():
    session = _Session([_piece(1, 2, 10), _piece(2, 1, 20)])
    _validate_physical_output_set(session, _mold(), 10, _pc_run(10))
    _validate_physical_output_set(session, _mold(), 20, _pc_run(20))


@pytest.mark.parametrize(
    "entries,code",
    [
        (_pc_run(10)[:1], "MOLD_OUTPUT_SET_MISMATCH"),
        (_pc_run(10) + [(SimpleNamespace(pieza_color=None), 1, 1)], "MOLD_OUTPUT_SET_MISMATCH"),
        ([( _article("PC-10-1", 1, 10), 2, 10), (_article("PC-10-1b", 1, 10), 2, 10)], "DUPLICATE_OF_PIECE"),
        ([(_article("PC-10-1", 1, 10), 3, 10), _pc_run(10)[1]], "MOLD_OUTPUT_SET_MISMATCH"),
        ([_pc_run(10)[0], (_article("PC-11-2", 2, 11), 1, 20)], "OF_RUN_COLOR_MISMATCH"),
    ],
)
def test_physical_set_rejects_missing_extra_duplicate_weight_and_color(entries, code):
    session = _Session([_piece(1, 2, 10), _piece(2, 1, 20)])
    with pytest.raises(ScmServiceError) as error:
        _validate_physical_output_set(session, _mold(), 10, entries)
    assert error.value.code == code


def test_pt_legacy_path_is_preserved_when_process_is_explicit():
    session = _Session([_piece(1, 2, 10), _piece(2, 1, 20)])
    pt = SimpleNamespace(pieza_color=None)
    _validate_physical_output_set(session, _mold(), 10, [(pt, 2, 10)])


def test_borrador_legacy_process_remains_pending_without_snapshot():
    order = SimpleNamespace(
        estado="BORRADOR",
        origen_demanda="EXCEPCIONAL",
        fabricacion=SimpleNamespace(
            snapshot_proceso=None,
            fuente_proceso=None,
            molde_id="ML-RPC-TEST",
        ),
        operacion_ruta_revision=None,
    )
    assert resolve_order_process(order) == (None, None, "LEGACY_PENDIENTE")


def test_api_creates_two_colors_two_pieces_and_patches_process_route_machine(
    app, client, scm_config
):
    from app import db
    from app.models.scm_production_orders import ScmCorridaFabricacion

    with app.app_context():
        catalog = _rpc_catalog(app)
    response = client.post(
        "/api/scm/v1/ordenes-fabricacion/excepcionales",
        headers={"X-Actor-Id": str(catalog["actor_id"]), "Idempotency-Key": str(uuid4())},
        json=_rpc_payload(catalog, process="INYECCION", linked=False, machine_id=catalog["injection_machine_id"]),
    )
    assert response.status_code == 201, response.get_json()
    body = response.get_json()
    patch = _patch_payload(body, route_ids=catalog["operation_ids"], process_marker="SOPLADO", machine_id=catalog["machine_id"])
    patched = client.patch(
        f"/api/scm/v1/ordenes-fabricacion/{body['id']}",
        headers={"X-Actor-Id": str(catalog["actor_id"]), "Idempotency-Key": str(uuid4())},
        json=patch,
    )
    assert patched.status_code == 200, patched.get_json()
    result = patched.get_json()
    assert result["snapshot_proceso"] == "SOPLADO"
    assert result["fuente_proceso"] == "EXPLICITO"
    assert [run["operacion_ruta_revision_id"] for run in result["corridas"]] == catalog["operation_ids"]
    with app.app_context():
        runs = db.session.scalars(
            select(ScmCorridaFabricacion).order_by(ScmCorridaFabricacion.secuencia)
        ).all()
        assert [run.operacion_ruta_revision_id for run in runs[-2:]] == catalog["operation_ids"]
        from app.models.scm_auditoria import ScmEvento
        event = db.session.scalar(
            select(ScmEvento)
            .where(
                ScmEvento.aggregate_id == body["id"],
                ScmEvento.tipo == "OF_DRAFT_CONFIGURED",
            )
        )
        assert event.before_json["snapshot_proceso"] == "INYECCION"
        assert event.after_json["snapshot_proceso"] == "SOPLADO"


def test_api_route_derived_remove_reference_requires_explicit_and_is_atomic(
    app, client, scm_config
):
    from app import db
    from app.models.scm_production_orders import ScmCorridaFabricacion

    with app.app_context():
        catalog = _rpc_catalog(app)
    response = client.post(
        "/api/scm/v1/ordenes-fabricacion/excepcionales",
        headers={"X-Actor-Id": str(catalog["actor_id"]), "Idempotency-Key": str(uuid4())},
        json=_rpc_payload(catalog, linked=True),
    )
    assert response.status_code == 201, response.get_json()
    body = response.get_json()
    remove_one = _patch_payload(
        body,
        route_ids=[catalog["operation_ids"][0], None],
    )
    rejected = client.patch(
        f"/api/scm/v1/ordenes-fabricacion/{body['id']}",
        headers={"X-Actor-Id": str(catalog["actor_id"]), "Idempotency-Key": str(uuid4())},
        json=remove_one,
    )
    assert rejected.status_code == 422
    assert rejected.get_json()["error"]["code"] == "PROCESS_REQUIRED"
    with app.app_context():
        runs = db.session.scalars(
            select(ScmCorridaFabricacion).order_by(ScmCorridaFabricacion.secuencia)
        ).all()
        assert [run.operacion_ruta_revision_id for run in runs[-2:]] == catalog["operation_ids"]
    remove_one["proceso"] = "SOPLADO"
    accepted = client.patch(
        f"/api/scm/v1/ordenes-fabricacion/{body['id']}",
        headers={"X-Actor-Id": str(catalog["actor_id"]), "Idempotency-Key": str(uuid4())},
        json=remove_one,
    )
    assert accepted.status_code == 200, accepted.get_json()
    result = accepted.get_json()
    assert result["fuente_proceso"] == "EXPLICITO"
    assert result["corridas"][1]["operacion_ruta_revision_id"] is None
    assert result["corridas"][1]["operacion_ruta_hash"] is None


def test_release_accepts_frozen_retired_routes_and_rejects_tampered_hash(
    app, client, scm_config
):
    from app import db
    from app.models.scm_rutas import ScmOperacionRuta
    from app.models.scm_production_orders import ScmOrdenOperacion
    from app.services.scm_route_service import retire_route

    with app.app_context():
        catalog = _rpc_catalog(app)
    response = client.post(
        "/api/scm/v1/ordenes-fabricacion/excepcionales",
        headers={"X-Actor-Id": str(catalog["actor_id"]), "Idempotency-Key": str(uuid4())},
        json=_rpc_payload(catalog, linked=True),
    )
    assert response.status_code == 201, response.get_json()
    body = response.get_json()
    with app.app_context():
        operation = db.session.get(ScmOperacionRuta, catalog["operation_ids"][0])
        retire_route(
            db.session,
            actor_id=catalog["actor_id"],
            route_id=operation.ruta_id,
            operation_id=uuid4(),
            data={"version": 1},
        )
    released = client.post(
        f"/api/scm/v1/ordenes-fabricacion/{body['id']}/liberar",
        headers={"X-Actor-Id": str(catalog["actor_id"]), "Idempotency-Key": str(uuid4())},
        json={"version": body["version"]},
    )
    assert released.status_code == 200, released.get_json()
    assert released.get_json()["estado"] == "LIBERADA"
    with app.app_context():
        from app.models.scm_auditoria import ScmEvento
        event = db.session.scalar(
            select(ScmEvento)
            .where(
                ScmEvento.aggregate_id == body["id"],
                ScmEvento.tipo == "OF_RELEASED",
            )
        )
        assert event.before_json["estado"] == "BORRADOR"
        assert event.after_json["estado"] == "LIBERADA"

    # A persisted frozen hash is part of the release contract.
    with app.app_context():
        order = db.session.get(ScmOrdenOperacion, UUID(body["id"]))
        assert order is not None
        order.estado = "BORRADOR"
        order.version += 1
        order.fabricacion.corridas[0].operacion_ruta_hash = "0" * 64
        db.session.commit()
    tampered = client.post(
        f"/api/scm/v1/ordenes-fabricacion/{body['id']}/liberar",
        headers={"X-Actor-Id": str(catalog["actor_id"]), "Idempotency-Key": str(uuid4())},
        json={"version": body["version"] + 2},
    )
    assert tampered.status_code == 409
    assert tampered.get_json()["error"]["code"] == "ROUTE_SNAPSHOT_MISMATCH"


def test_planned_patch_rejects_process_and_per_run_route_references(
    app, client, scm_config
):
    from app import db
    from app.models.scm_production_orders import ScmOrdenOperacion

    with app.app_context():
        catalog = _rpc_catalog(app)
    response = client.post(
        "/api/scm/v1/ordenes-fabricacion/excepcionales",
        headers={"X-Actor-Id": str(catalog["actor_id"]), "Idempotency-Key": str(uuid4())},
        json=_rpc_payload(
            catalog,
            process="INYECCION",
            linked=False,
            machine_id=catalog["injection_machine_id"],
        ),
    )
    assert response.status_code == 201, response.get_json()
    body = response.get_json()
    with app.app_context():
        order = db.session.get(ScmOrdenOperacion, UUID(body["id"]))
        order.origen_demanda = "ORDEN_PRODUCCION"
        db.session.commit()

    process_patch = _patch_payload(
        body,
        route_ids=[None, None],
        process_marker="SOPLADO",
        machine_id=catalog["machine_id"],
    )
    rejected_process = client.patch(
        f"/api/scm/v1/ordenes-fabricacion/{body['id']}",
        headers={"X-Actor-Id": str(catalog["actor_id"]), "Idempotency-Key": str(uuid4())},
        json=process_patch,
    )
    assert rejected_process.status_code == 422
    assert rejected_process.get_json()["error"]["code"] == "PROCESS_NOT_ALLOWED"

    route_patch = _patch_payload(
        body,
        route_ids=catalog["operation_ids"],
        machine_id=catalog["injection_machine_id"],
    )
    rejected_route = client.patch(
        f"/api/scm/v1/ordenes-fabricacion/{body['id']}",
        headers={"X-Actor-Id": str(catalog["actor_id"]), "Idempotency-Key": str(uuid4())},
        json=route_patch,
    )
    assert rejected_route.status_code == 422
    assert rejected_route.get_json()["error"]["code"] == "ROUTE_TARGET_OUT_OF_SCOPE"


def test_patch_omitted_route_reference_rechecks_frozen_hash(
    app, client, scm_config
):
    from app import db
    from app.models.scm_production_orders import ScmCorridaFabricacion, ScmOrdenOperacion

    with app.app_context():
        catalog = _rpc_catalog(app)
    response = client.post(
        "/api/scm/v1/ordenes-fabricacion/excepcionales",
        headers={"X-Actor-Id": str(catalog["actor_id"]), "Idempotency-Key": str(uuid4())},
        json=_rpc_payload(catalog, linked=True),
    )
    assert response.status_code == 201, response.get_json()
    body = response.get_json()
    with app.app_context():
        order = db.session.get(ScmOrdenOperacion, UUID(body["id"]))
        order.fabricacion.corridas[0].operacion_ruta_hash = "0" * 64
        db.session.commit()
    patch = _patch_payload(body, route_ids=[None, None])
    patch["corridas"][0].pop("operacion_ruta_revision_id")
    patch["corridas"][1].pop("operacion_ruta_revision_id")
    rejected = client.patch(
        f"/api/scm/v1/ordenes-fabricacion/{body['id']}",
        headers={"X-Actor-Id": str(catalog["actor_id"]), "Idempotency-Key": str(uuid4())},
        json=patch,
    )
    assert rejected.status_code == 409
    assert rejected.get_json()["error"]["code"] == "ROUTE_SNAPSHOT_MISMATCH"


def test_normal_patch_rejects_duplicate_run_ids_atomically(
    app, client, scm_config
):
    from app import db
    from app.models.scm_production_orders import ScmOrdenOperacion

    with app.app_context():
        catalog = _rpc_catalog(app)
    response = client.post(
        "/api/scm/v1/ordenes-fabricacion/excepcionales",
        headers={"X-Actor-Id": str(catalog["actor_id"]), "Idempotency-Key": str(uuid4())},
        json=_rpc_payload(
            catalog,
            process="INYECCION",
            linked=False,
            machine_id=catalog["injection_machine_id"],
        ),
    )
    assert response.status_code == 201, response.get_json()
    body = response.get_json()
    duplicate = _patch_payload(body, route_ids=[None, None])
    duplicate["corridas"].append(dict(duplicate["corridas"][0]))
    rejected = client.patch(
        f"/api/scm/v1/ordenes-fabricacion/{body['id']}",
        headers={"X-Actor-Id": str(catalog["actor_id"]), "Idempotency-Key": str(uuid4())},
        json=duplicate,
    )
    assert rejected.status_code == 422
    assert rejected.get_json()["error"]["code"] == "OF_CORRIDA_MISMATCH"
    with app.app_context():
        persisted = db.session.get(ScmOrdenOperacion, UUID(body["id"]))
        assert persisted.version == body["version"]
        assert len(persisted.fabricacion.corridas) == 2
        assert all(run.receta_revision_id is None for run in persisted.fabricacion.corridas)


def test_exceptional_post_rejects_pt_route_reference(
    app, client, scm_config
):
    from app import db
    from app.models.scm_articulos import ScmArticulo
    from app.models.scm_rutas import ScmCentroTrabajo, ScmOperacionRuta, ScmRutaRevision
    from app.services.scm_route_service import _content_hash

    with app.app_context():
        catalog = _rpc_catalog(app)
        article = db.session.scalar(
            select(ScmArticulo).where(ScmArticulo.clase == "PRODUCTO_TERMINADO")
        )
        if article is None:
            article = ScmArticulo(
                codigo=f"PT-RPC-{uuid4().hex[:8].upper()}",
                nombre="PT RPC route target",
                clase="PRODUCTO_TERMINADO",
            )
            db.session.add(article)
            db.session.flush()
        center = db.session.scalar(select(ScmCentroTrabajo))
        route = ScmRutaRevision(
            articulo_objetivo_id=article.id,
            numero_revision=1,
            estado="APROBADA",
            creada_por_id=catalog["actor_id"],
            aprobada_por_id=catalog["actor_id"],
        )
        route.operaciones.append(ScmOperacionRuta(
            clave="PT-ROUTE",
            secuencia_visible=10,
            nombre="Ruta PT",
            tipo="SOPLADO",
            executor_kind="OP_OT",
            centro_trabajo_id=center.id,
            articulo_salida_id=article.id,
        ))
        db.session.add(route)
        db.session.flush()
        route.content_hash = _content_hash(route)
        pt_operation_id = route.operaciones[0].id
        db.session.commit()
    payload = _rpc_payload(
        catalog,
        process="SOPLADO",
        linked=False,
        machine_id=catalog["machine_id"],
    )
    payload["corridas"][0]["operacion_ruta_revision_id"] = pt_operation_id
    rejected = client.post(
        "/api/scm/v1/ordenes-fabricacion/excepcionales",
        headers={"X-Actor-Id": str(catalog["actor_id"]), "Idempotency-Key": str(uuid4())},
        json=payload,
    )
    assert rejected.status_code == 422, rejected.get_json()
    assert rejected.get_json()["error"]["code"] == "ROUTE_TARGET_OUT_OF_SCOPE"

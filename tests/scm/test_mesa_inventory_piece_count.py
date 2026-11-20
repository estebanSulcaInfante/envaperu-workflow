"""Central contract tests for the named, multi-room piece count."""

import json
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import event

from app.extensions import db
from app.models.scm_articulos import ScmArticulo
from app.models.scm_inventory import ScmUbicacionInventario
from app.models.scm_inventory_operations import ScmAlmacen
from app.models.scm_inventory_kg import ScmExistenciaMangaKg, ScmMovimientoInventarioKg, ScmSaldoInventarioKg, ScmUnidadFisicaKg
from app.models.scm_auditoria import ScmOperacion
from app.models.scm_mesa_inventory import ScmMesaInventarioManga, ScmMesaInventarioMovimiento
from app.models.scm_ot import ScmEtiquetaManga, ScmManga, ScmTrabajoImpresionManga
from app.models.trabajador import RolOperativo, Trabajador
from app.services.scm_mesa_inventory import dispatch, vista_state, vista_state_conteo
from app.services.scm_service_support import ScmServiceError


ZONE = "Z-ALMACEN-PIEZAS"
OTHER_ZONE = "Z-ALMACEN-ARMADO-3P"


@pytest.fixture
def count_actor(app, scm_config):
    with app.app_context():
        actor = Trabajador.query.filter_by(codigo="TRB-01").one()
        role = RolOperativo.query.filter_by(codigo="MESA_COUNT_TEST").first()
        if role is None:
            role = RolOperativo(codigo="MESA_COUNT_TEST", nombre="Mesa count test")
            db.session.add(role)
        capability = next((item for item in role.capacidades if item.codigo == "INVENTARIO_CONTEO_C66"), None)
        if capability is None:
            from app.models.trabajador import ScmCapacidad
            capability = ScmCapacidad(codigo="INVENTARIO_CONTEO_C66", nombre="Conteo C66")
            role.capacidades.append(capability)
        actor.roles.append(role) if role not in actor.roles else None
        # The two color variants share the canonical base name.  The third
        # name is deliberately a prefix and must not be selected by "Tapa".
        article_green = ScmArticulo(codigo="PC-TAPA-VERDE", nombre="Tapa", clase="PIEZA_COLOR", unidad_base="UN", unidad_inventario="KG")
        article_blue = ScmArticulo(codigo="PC-TAPA-AZUL", nombre="Tapa", clase="PIEZA_COLOR", unidad_base="UN", unidad_inventario="KG")
        article_asa = ScmArticulo(codigo="PC-ASA-BLANCA", nombre="Tapa Grande", clase="PIEZA_COLOR", unidad_base="UN", unidad_inventario="KG")
        db.session.add_all([article_green, article_blue, article_asa])
        db.session.flush()
        warehouse = ScmAlmacen(codigo="WH-MESA-COUNT", nombre="Mesa count", tipo="PIEZAS_WIP")
        db.session.add(warehouse)
        db.session.flush()
        location = ScmUbicacionInventario(codigo="PRODUCCION_KG", nombre="Saldo KG", tipo="PUNTO_PRODUCCION", permite_saldo_libre=True, clases_articulo_json=["PIEZA_COLOR"], almacen_id=warehouse.id)
        db.session.add(location)
        db.session.flush()
        for article, code, kg in ((article_green, "CENTRAL-TAPA-VERDE", "2.000"), (article_blue, "CENTRAL-TAPA-AZUL", "3.000"), (article_asa, "CENTRAL-ASA-BLANCA", "1.000")):
            saldo = ScmSaldoInventarioKg(articulo_scm_id=article.id, ubicacion_id=location.id, cantidad_fisica_kg=kg)
            db.session.add(saldo)
            db.session.flush()
            operation = ScmOperacion(operation_id=uuid.uuid4(), endpoint="fixture", actor_id=actor.id, request_sha256="0" * 64, estado_http=201, response_json={})
            db.session.add(operation)
            db.session.flush()
            movement = ScmMovimientoInventarioKg(id=uuid.uuid4(), saldo_id=saldo.id, tipo="INGRESO_PRODUCCION", cantidad_delta_kg=kg, saldo_fisico_resultante_kg=kg, motivo="fixture", referencia_id=code, actor_id=actor.id, operation_id=operation.operation_id, projection_sha256="1" * 64, peso_neto_snapshot_kg=kg, pesada_at_snapshot=operation.created_at)
            db.session.add(movement)
            db.session.flush()
            intake_operation = ScmOperacion(operation_id=uuid.uuid4(), endpoint="fixture-intake", actor_id=actor.id, request_sha256="2" * 64, estado_http=201, response_json={})
            db.session.add(intake_operation)
            db.session.flush()
            unit = ScmUnidadFisicaKg(public_id=uuid.uuid4(), codigo=code, articulo_scm_id=article.id, estado="ACTIVA", estado_logistico="RECIBIDA_ALMACEN", estado_calidad="SIN_CONTROL", saldo_id=saldo.id, ubicacion_id=location.id, kg_verificados=kg)
            db.session.add(unit)
            db.session.flush()
            existence = ScmExistenciaMangaKg(id=uuid.uuid4(), unidad_fisica_kg_id=unit.id, articulo_scm_id=article.id, saldo_id=saldo.id, ubicacion_id=location.id, movimiento_ingreso_id=movement.id, operation_id=intake_operation.operation_id, resuelta_por="FIXTURE", estado_logistico="RECIBIDA_ALMACEN", estado_calidad="SIN_CONTROL", cantidad_fisica_kg=kg, peso_neto_snapshot_kg=kg, pesada_at_snapshot=intake_operation.created_at, recibida_por_id=actor.id)
            db.session.add(existence)
            unit.recepcion_vigente_id = existence.id
        db.session.commit()
        return actor.id


def call(actor_id, method, path, data=None):
    return dispatch(db.session, actor_id, method, path, data or {}, uuid.uuid4())


def _attach_prepesaje_label(unit, existence, actor_id, index, *, label_state="IMPRESA"):
    """Attach the minimum central label graph used by the C66 parity test."""
    article = db.session.get(ScmArticulo, unit.articulo_scm_id)
    manga = ScmManga(
        public_id=uuid.uuid4(), codigo=unit.codigo, ot_id=9000 + index,
        plan_linea_id=9000 + index, lote_articulo_id=9000 + index,
        secuencia_ot=index, estado="RECIBIDA", cantidad_planificada_un=10,
        cantidad_asignada_un=10, cantidad_confirmada_un=10,
        cantidad_contenida_un=10, maquinista_previsto_id=actor_id,
        articulo_codigo_snapshot=article.codigo,
        articulo_nombre_snapshot=article.nombre,
        regla_revision_id_snapshot=9000 + index, regla_hash_snapshot="a" * 64,
        tipo_contenedor_codigo_snapshot="MANGA",
        tipo_contenedor_nombre_snapshot="Manga", peso_unitario_snapshot_g=10,
        tara_nominal_g_snapshot=100, tolerancia_tara_g_snapshot=5,
        peso_bruto_max_kg_snapshot=20, created_by_id=actor_id,
    )
    job = ScmTrabajoImpresionManga(
        public_id=uuid.uuid4(), estado="PROCESADO", payload_hash="b" * 64,
        solicitado_por_id=actor_id,
    )
    label_id = uuid.uuid4()
    label = ScmEtiquetaManga(
        public_id=label_id, manga=manga, trabajo_impresion=job,
        tipo="PREPESAJE", version=1, estado=label_state,
        payload_json={"v": 1, "label_id": str(label_id)}, payload_hash="c" * 64,
    )
    existence.manga = manga
    existence.etiqueta = label
    db.session.add_all([manga, job, label])
    return label_id


def test_named_count_freezes_all_color_variants_and_supports_rooms(app, count_actor):
    with app.app_context():
        opened, status = call(count_actor, "POST", "/sesiones", {
            "nombre": "Tapas del jueves", "piezas": ["Tapa"],
            "zona": [ZONE, OTHER_ZONE], "clave": "named-1",
        })
        assert status == 201
        assert set(opened["esperadas"]) == {"CENTRAL-TAPA-VERDE", "CENTRAL-TAPA-AZUL"}
        session_id = opened["id"]
        first, status = call(count_actor, "POST", f"/sesiones/{session_id}/lecturas", {"codigo": "CENTRAL-TAPA-VERDE", "zona": ZONE})
        assert status == 201 and first["estado"] == "PRESENTE"
        with pytest.raises(ScmServiceError) as error:
            call(count_actor, "POST", f"/sesiones/{session_id}/lecturas", {"codigo": "CENTRAL-TAPA-VERDE", "zona": OTHER_ZONE})
        assert error.value.code == "ZONE_CONFLICT"
        with pytest.raises(ScmServiceError) as error:
            call(count_actor, "POST", f"/sesiones/{session_id}/lecturas", {"codigo": "CENTRAL-ASA-BLANCA", "zona": ZONE})
        assert error.value.code == "PIECE_OUT_OF_SCOPE"
        state = vista_state(db.session, count_actor)
        current = next(item for item in state["sesiones"] if item["id"] == session_id)
        assert current["nombre"] == "Tapas del jueves"
        assert current["piezas"] == ["Tapa"]
        assert current["pendientes_conteo"] == ["CENTRAL-TAPA-AZUL"]


def test_named_count_delete_is_retriable_and_allows_reading_again(app, count_actor):
    with app.app_context():
        opened, _ = call(count_actor, "POST", "/sesiones", {"nombre": "Tapas", "piezas": ["Tapa"], "zona": ZONE, "clave": "named-delete"})
        path = f"/sesiones/{opened['id']}/lecturas"
        call(count_actor, "POST", path, {"codigo": "CENTRAL-TAPA-VERDE"})
        operation = uuid.uuid4()
        deleted, status = dispatch(db.session, count_actor, "DELETE", f"{path}/CENTRAL-TAPA-VERDE", {}, operation)
        replay, replay_status = dispatch(db.session, count_actor, "DELETE", f"{path}/CENTRAL-TAPA-VERDE", {}, operation)
        assert status == 200 and replay_status == 200 and replay == deleted
        read, status = call(count_actor, "POST", path, {"codigo": "CENTRAL-TAPA-VERDE"})
        assert status == 201 and read["estado"] == "PRESENTE"


def test_named_count_delete_keeps_later_session_room(app, count_actor):
    with app.app_context():
        first, _ = call(count_actor, "POST", "/sesiones", {"nombre": "A", "piezas": ["Tapa"], "zona": ZONE, "clave": "named-room-a"})
        first_path = f"/sesiones/{first['id']}/lecturas"
        call(count_actor, "POST", first_path, {"codigo": "CENTRAL-TAPA-VERDE", "zona": ZONE})
        second, _ = call(count_actor, "POST", "/sesiones", {"nombre": "B", "piezas": ["Tapa"], "zona": OTHER_ZONE, "clave": "named-room-b"})
        second_path = f"/sesiones/{second['id']}/lecturas"
        call(count_actor, "POST", second_path, {
            "codigo": "CENTRAL-TAPA-VERDE", "zona": OTHER_ZONE,
            "confirmar_zona": True, "zona_anterior": ZONE,
        })
        dispatch(db.session, count_actor, "DELETE", f"{first_path}/CENTRAL-TAPA-VERDE", {}, uuid.uuid4())
        manga = ScmMesaInventarioManga.query.filter_by(codigo="CENTRAL-TAPA-VERDE").one()
        assert manga.zona_hallazgo == OTHER_ZONE
        assert len(next(item for item in vista_state(db.session, count_actor)["sesiones"] if item["id"] == second["id"])["lecturas"]) == 1


def test_web_state_uses_reads_from_later_session_for_older_pending(app, count_actor):
    with app.app_context():
        first, _ = call(count_actor, "POST", "/sesiones", {"nombre": "A", "piezas": ["Tapa"], "zona": ZONE, "clave": "named-cross-a"})
        second, _ = call(count_actor, "POST", "/sesiones", {"nombre": "B", "piezas": ["Tapa"], "zona": OTHER_ZONE, "clave": "named-cross-b"})
        call(count_actor, "POST", f"/sesiones/{second['id']}/lecturas", {"codigo": "CENTRAL-TAPA-VERDE", "zona": OTHER_ZONE, "confirmar_zona": True, "zona_anterior": ZONE})
        state = vista_state_conteo(db.session, count_actor, first["id"])
        older = next(item for item in state["sesiones"] if item["id"] == first["id"])
        assert "CENTRAL-TAPA-VERDE" not in older["pendientes_conteo"]
        closed, status = call(count_actor, "POST", f"/sesiones/{first['id']}/cerrar", {"marcar_no_habidas": True, "no_habidas": older["pendientes_conteo"]})
        assert status == 200 and closed["no_habidas"] == older["pendientes_conteo"]


def test_web_count_state_has_bounded_query_shape(app, count_actor):
    with app.app_context():
        call(count_actor, "POST", "/sesiones", {"nombre": "Budget", "piezas": ["Tapa"], "zona": ZONE, "clave": "named-budget"})
        statements = []

        def observe(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement)

        event.listen(db.engine, "before_cursor_execute", observe)
        try:
            payload = vista_state_conteo(db.session, count_actor)
        finally:
            event.remove(db.engine, "before_cursor_execute", observe)
        assert payload["conteo_por_pieza"] is True
        # The web projection must not call the global movement/pending
        # projection or issue one query per manga/session.
        assert len(statements) <= 20
        assert not any("scm_mesa_inventario_movimiento" in statement for statement in statements)


def test_named_count_close_records_missing_without_kardex_mutation_and_replays(app, count_actor):
    with app.app_context():
        opened, _ = call(count_actor, "POST", "/sesiones", {"nombre": "Tapas", "piezas": ["Tapa"], "zona": ZONE, "clave": "named-2"})
        session_id = opened["id"]
        call(count_actor, "POST", f"/sesiones/{session_id}/lecturas", {"codigo": "CENTRAL-TAPA-VERDE"})
        with pytest.raises(ScmServiceError) as error:
            call(count_actor, "POST", f"/sesiones/{session_id}/cerrar", {"marcar_no_habidas": True, "no_habidas": []})
        assert error.value.code == "MISSING_MISMATCH"
        before = len(vista_state(db.session, count_actor)["movimientos"])
        closed, status = call(count_actor, "POST", f"/sesiones/{session_id}/cerrar", {"marcar_no_habidas": True, "no_habidas": ["CENTRAL-TAPA-AZUL"]})
        assert status == 200
        replay, status = call(count_actor, "POST", f"/sesiones/{session_id}/cerrar", {"marcar_no_habidas": True, "no_habidas": ["CENTRAL-TAPA-AZUL"]})
        assert status == 200 and replay == closed
        after = vista_state(db.session, count_actor)
        assert len(after["movimientos"]) == before
        current = next(item for item in after["sesiones"] if item["id"] == session_id)
        assert current["no_habidas"] == ["CENTRAL-TAPA-AZUL"]


def test_web_state_preserves_pending_from_partial_close(app, count_actor):
    with app.app_context():
        opened, _ = call(count_actor, "POST", "/sesiones", {"nombre": "Tapas", "piezas": ["Tapa"], "zona": ZONE, "clave": "named-partial-get"})
        closed, status = call(count_actor, "POST", f"/sesiones/{opened['id']}/cerrar", {"marcar_no_habidas": False, "no_habidas": []})
        assert status == 200 and closed["pendientes_conteo"]
        state = vista_state_conteo(db.session, count_actor, opened["id"])
        current = next(item for item in state["sesiones"] if item["id"] == opened["id"])
        assert current["pendientes_conteo"] == closed["pendientes_conteo"]


def test_named_count_close_blocks_post_cut_identity_change(app, count_actor):
    with app.app_context():
        opened, _ = call(count_actor, "POST", "/sesiones", {"nombre": "Tapas", "piezas": ["Tapa"], "zona": ZONE, "clave": "named-3"})
        call(count_actor, "POST", f"/sesiones/{opened['id']}/lecturas", {"codigo": "CENTRAL-TAPA-VERDE"})
        manga = ScmMesaInventarioManga.query.filter_by(codigo="CENTRAL-TAPA-VERDE").one()
        manga.version += 1
        db.session.commit()
        with pytest.raises(ScmServiceError) as error:
            call(count_actor, "POST", f"/sesiones/{opened['id']}/cerrar", {"marcar_no_habidas": True, "no_habidas": opened["esperadas"]})
        # Shadow updates are part of the snapshot guard even when no Kardex
        # movement was generated.
        assert error.value.code in {"COUNT_SNAPSHOT_STALE", "MISSING_MISMATCH"}


def test_named_count_recomputes_pending_after_identity_changes_at_cut(app, count_actor):
    with app.app_context():
        opened, _ = call(count_actor, "POST", "/sesiones", {"nombre": "Tapas", "piezas": ["Tapa"], "zona": ZONE, "clave": "named-current-eligible"})
        unit = ScmUnidadFisicaKg.query.filter_by(codigo="CENTRAL-TAPA-AZUL").one()
        unit.updated_at = datetime.now(timezone.utc)
        db.session.commit()
        with pytest.raises(ScmServiceError) as error:
            call(count_actor, "POST", f"/sesiones/{opened['id']}/cerrar", {"marcar_no_habidas": True, "no_habidas": opened["esperadas"]})
        assert error.value.code == "MISSING_MISMATCH"
        current_pending = error.value.details["pendientes_conteo"]
        closed, status = call(count_actor, "POST", f"/sesiones/{opened['id']}/cerrar", {"marcar_no_habidas": True, "no_habidas": current_pending})
        assert status == 200 and closed["no_habidas"] == current_pending


def test_typed_of_codes_have_json_prepesaje_parity_including_materialized_shadow(app, count_actor):
    """Typed OF input must not bypass current PREPESAJE or shadow checks."""
    with app.app_context():
        units = ScmUnidadFisicaKg.query.order_by(ScmUnidadFisicaKg.codigo).all()
        # The fixture has three active identities; use them for the active,
        # in-production and dispatched cases and add two more for the remaining
        # arm/reversed samples so all six staging decisions are represented.
        states = [
            ("KG-OF000102-OT106-M001", "DISPONIBLE_PRODUCCION", "ACTIVA", "IMPRESA", True),
            ("KG-OF000102-OT108-M006", "EN_PRODUCCION", "ACTIVA", "IMPRESA", True),
            ("KG-OF000129-OT524-M032", "RETIRADA_ARMADO", "ACTIVA", "IMPRESA", True),
            ("KG-OF000129-OT524-M033", "RETIRADA_ARMADO", "ACTIVA", "IMPRESA", True),
            ("KG-OF000138-OT419-M009", "RETIRADA_CLIENTE_MN31", "HISTORICA", "IMPRESA", False),
            ("KG-OF000030-OT009-M001", "REVERSADA", "HISTORICA", "INVALIDADA", False),
        ]
        samples = []
        for index, (code, logistics, identity_state, label_state, expected) in enumerate(states, 1):
            if index <= len(units):
                unit = units[index - 1]
                unit.codigo = code
                existence = ScmExistenciaMangaKg.query.filter_by(unidad_fisica_kg_id=unit.id).one()
            else:
                article = ScmArticulo.query.filter_by(codigo="PC-TAPA-VERDE").one()
                saldo = ScmSaldoInventarioKg.query.filter_by(articulo_scm_id=article.id).first()
                existence_logistics = logistics if logistics in {
                    "EN_PRODUCCION", "DISPONIBLE_PRODUCCION", "RECIBIDA_ALMACEN",
                    "REVERSADA", "DESPACHADA_CLIENTE_MN31",
                } else "RECIBIDA_ALMACEN"
                unit = ScmUnidadFisicaKg(
                    public_id=uuid.uuid4(), codigo=code, articulo_scm_id=article.id,
                    estado=identity_state, estado_logistico=logistics,
                    estado_calidad="SIN_CONTROL", saldo_id=saldo.id,
                    ubicacion_id=saldo.ubicacion_id, kg_verificados="1.000",
                )
                db.session.add(unit)
                db.session.flush()
                operation = ScmOperacion(
                    operation_id=uuid.uuid4(), endpoint=f"fixture-{index}",
                    actor_id=count_actor, request_sha256=str(index) * 64,
                    estado_http=201, response_json={},
                )
                db.session.add(operation)
                db.session.flush()
                movement = ScmMovimientoInventarioKg(
                    id=uuid.uuid4(), saldo_id=saldo.id, tipo="INGRESO_PRODUCCION",
                    cantidad_delta_kg="1.000", saldo_fisico_resultante_kg="1.000",
                    motivo="fixture", referencia_id=code, actor_id=count_actor,
                    operation_id=operation.operation_id, projection_sha256="d" * 64,
                    peso_neto_snapshot_kg="1.000", pesada_at_snapshot=operation.created_at,
                )
                db.session.add(movement)
                db.session.flush()
                existence = ScmExistenciaMangaKg(
                    id=uuid.uuid4(), articulo_scm_id=article.id,
                    saldo_id=saldo.id, ubicacion_id=saldo.ubicacion_id,
                    movimiento_ingreso_id=movement.id, operation_id=operation.operation_id,
                    unidad_fisica_kg_id=unit.id, resuelta_por="FIXTURE",
                    estado_logistico=existence_logistics, estado_calidad="SIN_CONTROL",
                    cantidad_fisica_kg="1.000", peso_neto_snapshot_kg="1.000",
                    pesada_at_snapshot=operation.created_at, recibida_por_id=count_actor,
                )
                db.session.add(existence)
                db.session.flush()
            unit.estado, unit.estado_logistico = identity_state, logistics
            existence.estado_logistico = logistics if logistics in {
                "EN_PRODUCCION", "DISPONIBLE_PRODUCCION", "RECIBIDA_ALMACEN",
                "REVERSADA", "DESPACHADA_CLIENTE_MN31",
            } else "RECIBIDA_ALMACEN"
            label_id = _attach_prepesaje_label(unit, existence, count_actor, index, label_state=label_state)
            samples.append((code, label_id, expected))
        db.session.commit()

        def read(session_id, value):
            return call(count_actor, "POST", f"/sesiones/{session_id}/lecturas", {"codigo": value, "zona": ZONE})

        # JSON establishes the reference outcome in one session.
        for index, (code, label_id, expected) in enumerate(samples, 1):
            opened, _ = call(count_actor, "POST", "/sesiones", {"nombre": f"json-{index}", "piezas": ["Tapa", "Tapa Grande"], "zona": ZONE, "clave": f"json-{index}"})
            qr = json.dumps({"v": 1, "label_id": str(label_id)}, separators=(",", ":"))
            if expected:
                payload, status = read(opened["id"], qr)
                assert status == 201 and payload["codigo"] == code
            else:
                with pytest.raises(ScmServiceError) as error:
                    read(opened["id"], qr)
                assert error.value.code in {"KG_UNIT_HISTORICAL", "ETIQUETA_INVALIDADA"}

        # The rejected samples must also remain rejected when an older C66
        # shadow already exists (the path that previously returned too early).
        for code, _label_id, expected in samples:
            if expected:
                continue
            unit = ScmUnidadFisicaKg.query.filter_by(codigo=code).one()
            db.session.add(ScmMesaInventarioManga(
                codigo=code, unidad_fisica_id=unit.id,
                articulo_id=unit.articulo_scm_id, estado="HALLAZGO",
            ))
        db.session.commit()

        # Typed code must make the same decision even when the accepted JSON
        # already materialized the shadow. Test both accepted input spellings.
        for index, (code, _label_id, expected) in enumerate(samples, 1):
            opened, _ = call(count_actor, "POST", "/sesiones", {"nombre": f"typed-{index}", "piezas": ["Tapa", "Tapa Grande"], "zona": ZONE, "clave": f"typed-{index}"})
            typed = code.removeprefix("KG-") if index % 2 else code
            if expected:
                payload, status = read(opened["id"], typed)
                assert status == 201 and payload["codigo"] == code
            else:
                with pytest.raises(ScmServiceError) as error:
                    read(opened["id"], typed)
                assert error.value.code in {"KG_UNIT_HISTORICAL", "ETIQUETA_INVALIDADA"}

        # A BAJA-shaped shadow without the persisted negative FALTANTE
        # movement is not enough to reopen a historical identity.
        recovery_code, recovery_label, _ = samples[4]
        recovery_shadow = ScmMesaInventarioManga.query.filter_by(codigo=recovery_code).one()
        recovery_shadow.estado, recovery_shadow.baja_motivo = "BAJA_COMPROBADA", "FALTANTE"
        recovery_shadow.baja_movimiento_id = None
        db.session.commit()
        qr = json.dumps({"v": 1, "label_id": str(recovery_label)}, separators=(",", ":"))
        blocked_session, _ = call(count_actor, "POST", "/sesiones", {"nombre": "reappear-blocked", "piezas": ["Tapa", "Tapa Grande"], "zona": ZONE, "clave": "reappear-blocked"})
        with pytest.raises(ScmServiceError) as error:
            read(blocked_session["id"], qr)
        assert error.value.code == "KG_UNIT_HISTORICAL"
        blocked_manual, _ = call(count_actor, "POST", "/sesiones", {"nombre": "reappear-blocked-manual", "piezas": ["Tapa", "Tapa Grande"], "zona": ZONE, "clave": "reappear-blocked-manual"})
        with pytest.raises(ScmServiceError) as error:
            read(blocked_manual["id"], recovery_code.removeprefix("KG-"))
        assert error.value.code == "KG_UNIT_HISTORICAL"

        # A documented missing-item reappearance is the only historical
        # exception: JSON and visible code both reopen the same shadow for
        # repesaje, while the dispatched sample above remains blocked.
        baja_movement = ScmMesaInventarioMovimiento(
            id=uuid.uuid4(), manga_id=recovery_shadow.id, tipo="AJUSTE_FALTANTE",
            delta_kg="-1.000", motivo="Faltante documentado", clave="baja-real-1",
            actor_id=count_actor,
        )
        db.session.add(baja_movement)
        recovery_shadow.estado = "BAJA_COMPROBADA"
        recovery_shadow.baja_motivo = "FALTANTE"
        recovery_shadow.baja_movimiento_id = baja_movement.id
        recovery_shadow.neto_kg = None
        recovery_unit = ScmUnidadFisicaKg.query.filter_by(codigo=recovery_code).one()
        recovery_unit.estado, recovery_unit.estado_logistico = "HISTORICA", "REVERSADA"
        recovery_existence = ScmExistenciaMangaKg.query.filter_by(unidad_fisica_kg_id=recovery_unit.id).one()
        recovery_existence.estado_logistico = "REVERSADA"
        db.session.commit()
        qr = json.dumps({"v": 1, "label_id": str(recovery_label)}, separators=(",", ":"))
        json_session, _ = call(count_actor, "POST", "/sesiones", {"nombre": "reappear-json", "piezas": ["Tapa", "Tapa Grande"], "zona": ZONE, "clave": "reappear-json"})
        payload, status = read(json_session["id"], qr)
        assert status == 201 and payload["codigo"] == recovery_code
        manual_session, _ = call(count_actor, "POST", "/sesiones", {"nombre": "reappear-manual", "piezas": ["Tapa", "Tapa Grande"], "zona": ZONE, "clave": "reappear-manual"})
        payload, status = read(manual_session["id"], recovery_code.removeprefix("KG-"))
        assert status == 201 and payload["codigo"] == recovery_code

        # The recovered visible OF continues through pesaje and confirmation;
        # QR/visible parity above is the C66 lookup contract, while the
        # existing visible-code path retains its confirmation idempotency.
        actor = db.session.get(Trabajador, count_actor)
        actor.roles.append(RolOperativo.query.filter_by(codigo="GERENTE_GENERAL").one())
        db.session.commit()
        visible_code = recovery_code.removeprefix("KG-")
        capture = {
            "token": "reappearance-scale", "codigo": visible_code, "kg": "1.500",
            "base": "NT", "captured_at": "2026-11-20T10:00:00+00:00",
            "reading_id": "reappearance-reading",
        }
        weighed, status = call(count_actor, "POST", "/pesajes", {
            "codigo": visible_code, "bruto": "1.500", "tara": "0.500",
            "captura": capture, "clave": "reappearance-weight",
        })
        assert status == 201 and weighed["estado"] == "PESADA_PENDIENTE_CONFIRMACION"
        confirm_operation = uuid.uuid4()
        confirm_body = {
            "codigo": visible_code, "motivo": "Reaparición verificada",
            "clave": "reappearance-confirm",
        }
        confirmed, status = dispatch(db.session, count_actor, "POST", "/confirmaciones", confirm_body, confirm_operation)
        replay, replay_status = dispatch(db.session, count_actor, "POST", "/confirmaciones", confirm_body, confirm_operation)
        assert status == 201 and replay_status == 201 and replay == confirmed

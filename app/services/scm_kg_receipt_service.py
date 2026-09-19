"""KG-specific receipt transaction, kept outside the legacy UN flow."""

from decimal import Decimal
from uuid import UUID
import hashlib
import json
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.scm_auditoria import ScmEvento
from app.models.scm_inventory import ScmUbicacionInventario
from app.models.scm_articulos import ScmArticulo
from app.models.scm_inventory_kg import (
    ScmExistenciaMangaKg,
    ScmMovimientoInventarioKg,
    ScmSaldoInventarioKg,
    ScmUnidadFisicaKg,
    ScmEtiquetaUnidadKg,
)
from app.models.scm_ot import ScmCorreccionPesajeManga, ScmManga, ScmPesajeManga
from app.models.scm_warehouse import ScmSesionRecepcionManga
from app.services.scm_kg_service import (
    assert_kg_article,
    assert_kg_write_enabled,
    validate_expected_source,
)
from app.services.scm_service_support import ScmServiceError, actor_snapshot, required_text
from app.services.scm_weighing_service import _effective_projection


QUANTUM = Decimal("0.001")


def _locked_or_create_balance(session, *, article_id, location_id, atributo_proceso=None):
    """Return the unique article/location balance, recovering a first-row race."""
    balance = session.scalar(
        select(ScmSaldoInventarioKg).where(
            ScmSaldoInventarioKg.articulo_scm_id == article_id,
            ScmSaldoInventarioKg.ubicacion_id == location_id,
        ).with_for_update()
    )
    if balance is not None:
        return balance
    candidate = ScmSaldoInventarioKg(
        articulo_scm_id=article_id,
        ubicacion_id=location_id,
        atributo_proceso=atributo_proceso or "PROCESO",
    )
    try:
        with session.begin_nested():
            session.add(candidate)
            session.flush()
        return candidate
    except IntegrityError:
        balance = session.scalar(
            select(ScmSaldoInventarioKg).where(
                ScmSaldoInventarioKg.articulo_scm_id == article_id,
                ScmSaldoInventarioKg.ubicacion_id == location_id,
            ).with_for_update()
        )
        if balance is None:
            raise
        return balance


def _kg_identity_for_receipt(session, *, existence, article, location, quantity):
    """Create the canonical root without creating a second stock fact."""
    public_id = uuid4()
    label_public_id = uuid4()
    payload = {"v": 1, "label_id": str(label_public_id)}
    payload["qr_value"] = json.dumps(payload, separators=(",", ":"))
    unit = ScmUnidadFisicaKg(
        public_id=public_id,
        codigo=f"KG-{existence.manga.codigo if existence.manga else existence.id}",
        articulo_scm_id=article.id,
        existencia_manga_kg_id=existence.id,
        unidad_raiz_id=None,
        estado="ACTIVA",
        estado_logistico="PENDIENTE_CALIDAD",
        estado_calidad="PENDIENTE",
        saldo_id=existence.saldo_id,
        ubicacion_id=location.id,
        recepcion_vigente_id=existence.id,
        kg_entregado=quantity,
        # KG001 already captured this root directly.  Preserve that source
        # fact for inventory display; return/division children stay null until
        # the station records a new measurement.
        kg_verificados=quantity,
        kg_verificados_at=existence.pesada_at_snapshot,
        almacen_responsable_id=location.almacen_id,
        # The return station's reading mode/tare is governed separately; do
        # not infer NET_DIRECTO from the historical KG001 weighing.
        modo_lectura=None,
    )
    session.add(unit)
    session.flush()
    unit.unidad_raiz_id = unit.id
    label = ScmEtiquetaUnidadKg(
        public_id=label_public_id,
        unidad_id=unit.id,
        payload_json=payload,
        payload_hash=hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
        estado="GENERADA",
    )
    session.add(label)
    label.unidad_id = unit.id
    existence.unidad_fisica_kg_id = unit.id
    session.flush()
    return unit, label


def _assert_kg_location_scope(session, *, actor_id, location, article_class):
    from app.services.scm_warehouse_scope_service import assert_location_scope
    try:
        return assert_location_scope(
            session, actor_id=actor_id, location=location,
            article_class=article_class,
        )
    except ScmServiceError as error:
        if error.code == "LOCATION_NOT_FOUND":
            raise ScmServiceError(
                "INVENTORY_SCOPE_FORBIDDEN",
                "La ubicación no pertenece al alcance del almacén del actor.",
                status_code=403,
            ) from error
        raise


def receive_manga_kg(session, *, actor, operation_id, data, manga, label, resolution):
    """Write exactly one KG receipt after reauthorizing scope and source."""
    assert_kg_write_enabled()
    article = session.scalar(
        select(ScmArticulo)
        .where(ScmArticulo.id == manga.lote_articulo.articulo.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    assert_kg_article(article)
    location_code = required_text(data.get("ubicacion_codigo"), field="ubicacion_codigo", max_length=40).upper()
    location = session.scalar(select(ScmUbicacionInventario).where(ScmUbicacionInventario.codigo == location_code).with_for_update())
    if location is None or not location.activo:
        raise ScmServiceError("UBICACION_INCOMPATIBLE", "La ubicación no existe o está inactiva.", status_code=422)
    _assert_kg_location_scope(
        session, actor_id=actor.id, location=location,
        article_class=article.clase,
    )
    allowed_classes = set(location.clases_articulo_json or [])
    if allowed_classes and article.clase not in allowed_classes:
        raise ScmServiceError("UBICACION_INCOMPATIBLE", "La ubicación no admite esta clase de artículo.", status_code=422)

    expected = data.get("expected_weighing_source")
    # A control-only administrative close has no final weighing token.  It is
    # accepted only after the locked production existence proves that this is
    # the explicit DISPONIBLE_PRODUCCION closure path below.
    if expected is not None and not isinstance(expected, dict):
        raise ScmServiceError("EXPECTED_WEIGHING_SOURCE_INVALID", "expected_weighing_source debe ser un objeto.", status_code=422)
    command = {
        "label_id": str(label.public_id), "manga_id": str(manga.public_id),
        "sesion_id": str(data.get("sesion_id")) if data.get("sesion_id") else None,
        "ubicacion_codigo": location_code, "resuelta_por": resolution,
        "expected_weighing_source": {key: expected.get(key) for key in ("pesaje_public_id", "correccion_aplicada_public_id", "projection_sha256")},
        "presencia_confirmada": True, "bolsa_cerrada": True, "coincidencia_etiquetas": True,
    }
    existing_for_operation = session.scalar(
        select(ScmExistenciaMangaKg).where(
            ScmExistenciaMangaKg.operation_id == operation_id,
        )
    )
    if existing_for_operation is not None:
        _assert_kg_location_scope(
            session,
            actor_id=actor.id,
            location=existing_for_operation.ubicacion,
            article_class=article.clase,
        )
    from app.services.scm_warehouse_service import _candidate_payload, _reserve_operation, _complete, _uuid_value
    endpoint = f"POST /recepcion-mangas/{manga.public_id}/confirmar"
    operation, replay = _reserve_operation(session, operation_id, endpoint, actor, command)
    if replay is not None:
        return replay
    try:
        manga = session.scalar(
            select(ScmManga)
            .where(ScmManga.id == manga.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        article = session.scalar(
            select(ScmArticulo)
            .where(ScmArticulo.id == manga.lote_articulo.articulo.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        assert_kg_article(article)
        existing = session.scalar(select(ScmExistenciaMangaKg).where(ScmExistenciaMangaKg.manga_id == manga.id))
        weighing = session.scalar(
            select(ScmPesajeManga)
            .where(
                ScmPesajeManga.manga_id == manga.id,
                ScmPesajeManga.estado == "VIGENTE",
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        control_closure = (
            weighing is None
            and existing is not None
            and existing.estado_logistico == "DISPONIBLE_PRODUCCION"
            and existing.pesaje_public_id is None
        )
        if weighing is None and not control_closure:
            raise ScmServiceError("PESAJE_FINAL_REQUERIDO", "La manga todavía no posee un pesaje final confirmado.", status_code=409)
        if control_closure:
            if expected is not None:
                raise ScmServiceError("EXPECTED_WEIGHING_SOURCE_NOT_APPLICABLE", "El cierre administrativo se identifica por su control fuente.", status_code=409)
            token = {
                "pesaje_public_id": None,
                "correccion_aplicada_public_id": None,
                "projection_sha256": existing.projection_sha256,
                "peso_neto_snapshot_kg": format(Decimal(existing.cantidad_fisica_kg), "f"),
            }
        else:
            if not isinstance(expected, dict):
                raise ScmServiceError("EXPECTED_WEIGHING_SOURCE_REQUIRED", "Se requiere expected_weighing_source para recibir un artículo KG.", status_code=409)
            applied_correction = session.scalar(
                select(ScmCorreccionPesajeManga)
                .where(
                    ScmCorreccionPesajeManga.pesaje_id == weighing.id,
                    ScmCorreccionPesajeManga.estado == "APLICADA",
                )
                .order_by(ScmCorreccionPesajeManga.id.desc())
                .with_for_update()
            )
            projection = (
                dict(applied_correction.result_projection_json)
                if applied_correction is not None
                else _effective_projection(weighing)
            )
            token = validate_expected_source(session, weighing, projection, expected)
        source_public_id = weighing.public_id if weighing is not None else None
        source_at = weighing.pesada_at if weighing is not None else existing.pesada_at_snapshot
        candidate = _candidate_payload(session, manga, label, resolution)
        receipt_session = None
        if data.get("sesion_id"):
            receipt_session = session.get(ScmSesionRecepcionManga, _uuid_value(data["sesion_id"], field="sesion_id"))
            if receipt_session is None or receipt_session.estado != "ABIERTA":
                raise ScmServiceError("SESION_RECEPCION_INVALIDA", "La sesión no existe o ya fue cerrada.", status_code=409)
            if receipt_session.actor_id != actor.id:
                raise ScmServiceError("SESION_RECEPCION_OTRO_ACTOR", "La sesión pertenece a otro almacenero.", status_code=409)
        if existing is not None:
            _assert_kg_location_scope(
                session, actor_id=actor.id, location=existing.ubicacion,
                article_class=article.clase,
            )
            if existing.estado_logistico not in {"EN_PRODUCCION", "DISPONIBLE_PRODUCCION"}:
                raise ScmServiceError(
                    "MANGA_YA_RECIBIDA", "La manga ya fue aceptada por Almacén.",
                    status_code=409, details={"existencia": existing.to_dict()},
                )
            quantity = Decimal(existing.cantidad_fisica_kg).quantize(QUANTUM)
            if quantity != Decimal(token["peso_neto_snapshot_kg"]).quantize(QUANTUM):
                raise ScmServiceError(
                    "PESAJE_VERSION_CONFLICT",
                    "El saldo de Producción no coincide con el NET final vigente.",
                    status_code=409,
                )
            source_location = existing.ubicacion
            if source_location.id == location.id:
                raise ScmServiceError(
                    "MANGA_YA_RECIBIDA", "La manga ya está en la ubicación indicada.",
                    status_code=409, details={"existencia": existing.to_dict()},
                )
            source_balance = session.scalar(
                select(ScmSaldoInventarioKg).where(ScmSaldoInventarioKg.id == existing.saldo_id).with_for_update()
            )
            if source_balance is None or Decimal(source_balance.cantidad_fisica_kg) < quantity:
                raise ScmServiceError("INVENTORY_CONFLICT", "El saldo de Producción no cubre el traslado.", status_code=409)
            target_balance = _locked_or_create_balance(
                session,
                article_id=article.id,
                location_id=location.id,
                atributo_proceso=existing.atributo_proceso,
            )
            source_balance.cantidad_fisica_kg = Decimal(source_balance.cantidad_fisica_kg) - quantity
            source_balance.version += 1
            target_balance.cantidad_fisica_kg = Decimal(target_balance.cantidad_fisica_kg) + quantity
            target_balance.version += 1
            correction_id = UUID(token["correccion_aplicada_public_id"]) if token["correccion_aplicada_public_id"] else None
            transfer_hash = token["projection_sha256"]
            source_movement = ScmMovimientoInventarioKg(
                saldo_id=source_balance.id, tipo="TRASLADO_SALIDA", cantidad_delta_kg=-quantity,
                saldo_fisico_resultante_kg=source_balance.cantidad_fisica_kg,
                motivo=f"Traslado de Producción a Almacén de manga {manga.codigo}",
                referencia_tipo="MANGA", referencia_id=str(manga.public_id), actor_id=actor.id,
                operation_id=operation.operation_id, pesaje_public_id=source_public_id,
                correccion_aplicada_public_id=correction_id, projection_sha256=transfer_hash,
                peso_neto_snapshot_kg=quantity, pesada_at_snapshot=source_at,
                fuente_tipo="KG_RECEPCION", atributo_proceso=existing.atributo_proceso,
            )
            # Each movement keeps the existing one-operation uniqueness.  The
            # paired inbound movement therefore receives an audited child
            # operation instead of an orphan UUID.
            transfer_operation, transfer_replay = _reserve_operation(
                session, uuid4(),
                f"{endpoint}/traslado-entrada",
                actor,
                {"operation_id": str(operation.operation_id), "manga_id": str(manga.public_id)},
            )
            if transfer_replay is not None:
                raise ScmServiceError("IDEMPOTENCY_OPERATION_INCOMPLETE", "El traslado interno ya tiene una operación incompatible.", status_code=409)
            target_movement = ScmMovimientoInventarioKg(
                saldo_id=target_balance.id, tipo="TRASLADO_ENTRADA", cantidad_delta_kg=quantity,
                saldo_fisico_resultante_kg=target_balance.cantidad_fisica_kg,
                motivo=f"Recepción del traslado de manga {manga.codigo}",
                referencia_tipo="MANGA", referencia_id=str(manga.public_id), actor_id=actor.id,
                operation_id=transfer_operation.operation_id, pesaje_public_id=source_public_id,
                correccion_aplicada_public_id=correction_id, projection_sha256=transfer_hash,
                peso_neto_snapshot_kg=quantity, pesada_at_snapshot=source_at,
                fuente_tipo="KG_RECEPCION", atributo_proceso=existing.atributo_proceso,
            )
            session.add_all([source_movement, target_movement])
            existing.sesion_id = receipt_session.id if data.get("sesion_id") else existing.sesion_id
            existing.etiqueta_resuelta_id = label.id
            existing.saldo_id = target_balance.id
            existing.ubicacion_id = location.id
            existing.estado_logistico = "RECIBIDA_ALMACEN"
            existing.estado_calidad = "SIN_CONTROL"
            existing.pesaje_public_id = source_public_id
            existing.correccion_aplicada_public_id = correction_id
            existing.projection_sha256 = transfer_hash
            existing.recibida_por_id = actor.id
            existing.version += 1
            unit = existing.unidad_fisica_kg
            if unit is not None:
                unit.saldo_id = target_balance.id
                unit.ubicacion_id = location.id
                unit.estado_logistico = "RECIBIDA_ALMACEN"
                unit.estado_calidad = "SIN_CONTROL"
                unit.version += 1
            manga.estado = "RECIBIDA"
            manga.version += 1
            session.flush()
            _complete(transfer_operation, {
                "movement_id": str(target_movement.id),
                "parent_operation_id": str(operation.operation_id),
            }, 201)
            response = {
                "existencia": existing.to_dict(),
                "unidad": unit.to_dict() if unit else None,
                "movimiento_id": str(target_movement.id),
                "movimiento_salida_id": str(source_movement.id),
                "operation_id": str(operation.operation_id),
                "idempotent_replay": False,
                "traslado_sin_doble_ingreso": True,
            }
            _complete(operation, response, 200)
            session.add(ScmEvento(
                aggregate_type="MANGA", aggregate_id=str(manga.public_id),
                tipo="MANGA_KG_PRODUCTION_TRANSFERRED_TO_WAREHOUSE",
                actor_id=actor.id, actor_snapshot=actor_snapshot(actor),
                before_json=candidate, after_json=response, operation_id=operation.operation_id,
            ))
            session.commit()
            return response
        quantity = Decimal(token["peso_neto_snapshot_kg"]).quantize(QUANTUM)
        balance = _locked_or_create_balance(
            session, article_id=article.id, location_id=location.id,
        )
        resulting = Decimal(balance.cantidad_fisica_kg) + quantity
        balance.cantidad_fisica_kg = resulting
        balance.cantidad_no_disponible_kg = Decimal(balance.cantidad_no_disponible_kg) + quantity
        balance.version += 1
        correction_id = UUID(token["correccion_aplicada_public_id"]) if token["correccion_aplicada_public_id"] else None
        movement = ScmMovimientoInventarioKg(
            saldo=balance, tipo="INGRESO_PRODUCCION", cantidad_delta_kg=quantity,
            saldo_fisico_resultante_kg=resulting, motivo=f"Recepción física KG de manga {manga.codigo}",
            referencia_tipo="MANGA", referencia_id=str(manga.public_id), actor_id=actor.id,
            operation_id=operation.operation_id, pesaje_public_id=source_public_id,
            correccion_aplicada_public_id=correction_id, projection_sha256=token["projection_sha256"],
            peso_neto_snapshot_kg=quantity, pesada_at_snapshot=source_at,
        )
        session.add(movement)
        session.flush()
        existence = ScmExistenciaMangaKg(
            manga_id=manga.id, sesion_id=receipt_session.id if receipt_session else None,
            etiqueta_resuelta_id=label.id, articulo_scm_id=article.id, saldo_id=balance.id,
            ubicacion_id=location.id, movimiento_ingreso_id=movement.id, operation_id=operation.operation_id,
            resuelta_por=resolution, cantidad_fisica_kg=quantity, peso_neto_snapshot_kg=quantity,
            pesaje_public_id=source_public_id, correccion_aplicada_public_id=correction_id,
            projection_sha256=token["projection_sha256"], pesada_at_snapshot=source_at,
            recibida_por_id=actor.id, estado_calidad="PENDIENTE",
        )
        session.add(existence)
        manga.estado = "RECIBIDA"
        manga.version += 1
        session.flush()
        unit, identity_label = _kg_identity_for_receipt(
            session, existence=existence, article=article, location=location,
            quantity=quantity,
        )
        response = {"existencia": existence.to_dict(), "unidad": unit.to_dict(), "etiqueta": {"id": str(identity_label.id), "public_id": str(identity_label.id), "qr_value": identity_label.payload_json.get("qr_value"), "payload_hash": identity_label.payload_hash, "estado": identity_label.estado}, "movimiento_id": str(movement.id), "operation_id": str(operation.operation_id), "idempotent_replay": False}
        _complete(operation, response, 201)
        session.add(ScmEvento(
            aggregate_type="MANGA", aggregate_id=str(manga.public_id), tipo="MANGA_RECEIVED_IN_WAREHOUSE_KG",
            actor_id=actor.id, actor_snapshot=actor_snapshot(actor), before_json=candidate,
            after_json=response, operation_id=operation.operation_id,
        ))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise

"""US-010H: solicitud, reserva, picking y traslado de mangas a Armado."""

import copy
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_CEILING

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models.registro import RegistroDiarioProduccion
from app.models.scm_auditoria import ScmEvento
from app.models.scm_internal_supply import (
    ScmAsignacionAbastecimiento,
    ScmAsignacionPoolArmado,
    ScmPoolOrigenArmado,
    ScmSolicitudAbastecimiento,
    ScmSolicitudAbastecimientoLinea,
    request_payload,
    utc_now,
)
from app.models.scm_inventory import (
    ScmMovimientoInventario,
    ScmSaldoInventario,
    ScmUbicacionInventario,
)
from app.models.scm_inline_wip import ScmReservaWipSalida
from app.models.scm_ot import ScmEtiquetaManga, ScmManga, ScmTrabajoOt
from app.models.scm_production_orders import ScmOrdenOperacion
from app.models.scm_rutas import ScmCentroTrabajo, ScmOperacionRuta
from app.models.scm_warehouse import ScmExistenciaManga
from app.models.trabajador import Trabajador
from app.services.catalog_code_generator import generar_codigo_catalogo
from app.services.scm_ot_service import (
    _batch_ot_serialization_context,
    _complete_operation,
    _reserve_operation,
    _serialize_ot,
)
from app.services.scm_service_support import (
    ScmServiceError,
    actor_snapshot,
    expected_version,
    load_actor,
    reject_unknown_fields,
    required_text,
)


QUANTUM = Decimal("0.001")
ACTIVE_ASSIGNMENT_STATES = (
    "RESERVADA",
    "EN_PICKING",
    "EN_TRANSITO_PRODUCCION",
    "EN_STAGING_ARMADO",
    "ABIERTA_EN_CONSUMO",
    "PENDIENTE_RETORNO",
    "EN_TRANSITO_ALMACEN",
)


def _quantity(value, field="cantidad"):
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ScmServiceError(
            "INVALID_QUANTITY",
            f"{field} debe ser una cantidad positiva.",
            status_code=422,
        ) from error
    if not parsed.is_finite() or parsed <= 0:
        raise ScmServiceError(
            "INVALID_QUANTITY",
            f"{field} debe ser una cantidad positiva.",
            status_code=422,
        )
    return parsed.quantize(QUANTUM)


def _date(value):
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as error:
        raise ScmServiceError(
            "INVALID_DATE", "fecha_operativa debe usar YYYY-MM-DD.", status_code=422
        ) from error


def _event(aggregate_type, aggregate_id, event_type, actor, operation, after):
    return ScmEvento(
        aggregate_type=aggregate_type,
        aggregate_id=str(aggregate_id),
        tipo=event_type,
        actor_id=actor.id,
        actor_snapshot=actor_snapshot(actor),
        after_json=copy.deepcopy(after),
        operation_id=operation.operation_id,
    )


def _load_assembly_order(session, order_id, *, lock=False):
    statement = select(ScmOrdenOperacion).where(
        ScmOrdenOperacion.id == order_id,
        ScmOrdenOperacion.tipo == "ENSAMBLE",
    )
    if lock:
        statement = statement.with_for_update()
    order = session.scalar(statement)
    if order is None:
        raise ScmServiceError("OA_NOT_FOUND", "La OA no existe.", status_code=404)
    return order


def _load_ot(session, public_id, *, lock=False):
    try:
        public_id = uuid.UUID(str(public_id))
    except (TypeError, ValueError, AttributeError) as error:
        raise ScmServiceError(
            "OT_ENSAMBLE_ID_INVALID",
            "El identificador de la OT de Armado no es válido.",
            status_code=400,
        ) from error
    statement = select(RegistroDiarioProduccion).where(
        RegistroDiarioProduccion.public_id == public_id,
        RegistroDiarioProduccion.tipo_ot == "ENSAMBLE",
    )
    if lock:
        statement = statement.with_for_update()
    item = session.scalar(statement)
    if item is None:
        raise ScmServiceError(
            "OT_ENSAMBLE_NOT_FOUND", "La OT de Armado no existe.", status_code=404
        )
    return item


def _load_request(session, request_id, *, lock=False):
    statement = select(ScmSolicitudAbastecimiento).where(
        ScmSolicitudAbastecimiento.id == request_id
    )
    if lock:
        statement = statement.with_for_update()
    item = session.scalar(statement)
    if item is None:
        raise ScmServiceError(
            "SUPPLY_REQUEST_NOT_FOUND",
            "La solicitud de abastecimiento no existe.",
            status_code=404,
        )
    return item


def _check_version(item, raw):
    version = expected_version(raw)
    if item.version != version:
        raise ScmServiceError(
            "VERSION_CONFLICT",
            "La solicitud fue modificada por otro usuario.",
            status_code=409,
        )


def create_assembly_ot(session, *, actor_id, order_id, operation_id, data):
    reject_unknown_fields(data, allowed={
        "fecha_operativa", "turno", "centro_trabajo_id", "responsable_id",
        "cantidad_objetivo", "modo_ejecucion",
        "ot_fabricacion_contexto_id",
        "trabajo_color_contexto_id",
    })
    actor = load_actor(session, actor_id, capability="OT_CREAR")
    command = {
        "order_id": str(order_id),
        "fecha_operativa": str(data.get("fecha_operativa")),
        "turno": str(data.get("turno") or "").strip().upper(),
        "centro_trabajo_id": data.get("centro_trabajo_id"),
        "responsable_id": data.get("responsable_id"),
        "cantidad_objetivo": format(
            _quantity(data.get("cantidad_objetivo"), "cantidad_objetivo"), "f"
        ),
        "modo_ejecucion": str(
            data.get("modo_ejecucion") or "MESA"
        ).strip().upper(),
        "ot_fabricacion_contexto_id": (
            str(data.get("ot_fabricacion_contexto_id")).strip()
            if data.get("ot_fabricacion_contexto_id") else None
        ),
        "trabajo_color_contexto_id": (
            str(data.get("trabajo_color_contexto_id")).strip()
            if data.get("trabajo_color_contexto_id") else None
        ),
    }
    operation, replay = _reserve_operation(
        session, operation_id, f"POST /ordenes-armado/{order_id}/ots", actor, command
    )
    if replay is not None:
        return replay
    try:
        order = _load_assembly_order(session, order_id, lock=True)
        if order.estado not in ("LIBERADA", "EN_EJECUCION"):
            raise ScmServiceError(
                "OA_NOT_RELEASED",
                "La OA debe estar liberada para crear una OT diaria.",
                status_code=409,
            )
        if len(order.salidas) != 1:
            raise ScmServiceError(
                "OA_OUTPUT_INVALID", "La OA debe tener una sola salida.", status_code=409
            )
        route_operation = session.get(ScmOperacionRuta, order.operacion_ruta_revision_id)
        mode = command["modo_ejecucion"]
        if mode not in ("MESA", "CONCURRENTE"):
            raise ScmServiceError(
                "ASSEMBLY_EXECUTION_MODE_INVALID",
                "Selecciona una modalidad de ejecución válida.",
                status_code=422,
            )
        fabrication_context = None
        color_work_context = None
        if mode == "CONCURRENTE":
            if route_operation is None or not route_operation.permite_concurrente:
                raise ScmServiceError(
                    "ASSEMBLY_CONCURRENT_NOT_ALLOWED",
                    "La operación de ruta no permite ejecución concurrente.",
                    status_code=422,
                )
            if not command["trabajo_color_contexto_id"]:
                raise ScmServiceError(
                    "ASSEMBLY_COLOR_WORK_CONTEXT_REQUIRED",
                    "Selecciona el Trabajo de color exacto para el prearmado.",
                    status_code=422,
                )
            if command["trabajo_color_contexto_id"]:
                try:
                    work_id = uuid.UUID(command["trabajo_color_contexto_id"])
                except (TypeError, ValueError, AttributeError) as error:
                    raise ScmServiceError(
                        "ASSEMBLY_COLOR_WORK_CONTEXT_INVALID",
                        "El trabajo de color seleccionado no es valido.",
                        status_code=422,
                    ) from error
                color_work_context = session.scalar(
                    select(ScmTrabajoOt).where(
                        ScmTrabajoOt.id == work_id,
                        ScmTrabajoOt.tipo == "COLOR",
                        ScmTrabajoOt.estado.in_((
                            "PLANIFICADO", "EN_EJECUCION", "PAUSADO"
                        )),
                    )
                )
                if color_work_context is None:
                    raise ScmServiceError(
                        "ASSEMBLY_COLOR_WORK_CONTEXT_INVALID",
                        "Selecciona un trabajo de color activo.",
                        status_code=422,
                    )
                if (
                    color_work_context.orden_operacion is None
                    or color_work_context.orden_operacion.tipo
                    != "FABRICACION"
                ):
                    raise ScmServiceError(
                        "ASSEMBLY_COLOR_WORK_NOT_FABRICATION",
                        "El Trabajo de color debe pertenecer a una OF.",
                        status_code=422,
                    )
                if (
                    command["ot_fabricacion_contexto_id"]
                    and command["ot_fabricacion_contexto_id"]
                    != str(color_work_context.orden_trabajo.public_id)
                ):
                    raise ScmServiceError(
                        "TRABAJO_NO_PERTENECE_A_OT",
                        "El trabajo no pertenece a la OT de contexto.",
                        status_code=409,
                    )
                command["ot_fabricacion_contexto_id"] = str(
                    color_work_context.orden_trabajo.public_id
                )
            try:
                context_id = uuid.UUID(command["ot_fabricacion_contexto_id"])
            except (TypeError, ValueError, AttributeError) as error:
                raise ScmServiceError(
                    "ASSEMBLY_FABRICATION_CONTEXT_INVALID",
                    "La OT de fabricación seleccionada no es válida.",
                    status_code=422,
                ) from error
            fabrication_context = session.scalar(
                select(RegistroDiarioProduccion).where(
                    RegistroDiarioProduccion.public_id == context_id,
                    RegistroDiarioProduccion.codigo_ot_sintetico.is_(False),
                    RegistroDiarioProduccion.tipo_ot == "FABRICACION",
                    RegistroDiarioProduccion.estado.in_(("PLANIFICADA", "EN_EJECUCION")),
                )
            )
            if fabrication_context is None:
                raise ScmServiceError(
                    "ASSEMBLY_FABRICATION_CONTEXT_INVALID",
                    "Selecciona una OT de fabricación activa.",
                    status_code=422,
                )
            if color_work_context is None:
                candidates = [
                    work for work in fabrication_context.trabajos_ot
                    if work.tipo == "COLOR"
                    and work.estado in (
                        "PLANIFICADO", "EN_EJECUCION", "PAUSADO"
                    )
                ]
                if len(candidates) == 1:
                    color_work_context = candidates[0]
                elif len(candidates) > 1:
                    raise ScmServiceError(
                        "ASSEMBLY_COLOR_WORK_CONTEXT_REQUIRED",
                        "La OT contiene varios trabajos de color; selecciona el que abastece el prearmado.",
                        status_code=422,
                    )
        elif (
            command["ot_fabricacion_contexto_id"]
            or command["trabajo_color_contexto_id"]
        ):
            raise ScmServiceError(
                "ASSEMBLY_FABRICATION_CONTEXT_INVALID",
                "La modalidad en mesa no utiliza una OT de fabricación de contexto.",
                status_code=422,
            )
        operational_date = _date(command["fecha_operativa"])
        if fabrication_context is not None and fabrication_context.fecha != operational_date:
            raise ScmServiceError(
                "ASSEMBLY_FABRICATION_CONTEXT_DATE_MISMATCH",
                "La OT de fabricación debe corresponder a la misma fecha operativa.",
                status_code=422,
            )
        center_id = command["centro_trabajo_id"] or (
            route_operation.centro_trabajo_id if route_operation else None
        )
        center = session.get(ScmCentroTrabajo, center_id)
        if center is None or not center.activo or center.tipo not in (
            "PREARMADO", "ENSAMBLE", "ACABADO", "EMPAQUE"
        ):
            raise ScmServiceError(
                "ASSEMBLY_WORK_CENTER_INVALID",
                "Selecciona una mesa o centro de Armado activo.",
                status_code=422,
            )
        responsible = session.get(Trabajador, command["responsable_id"])
        if responsible is None or not responsible.activo:
            raise ScmServiceError(
                "ASSEMBLY_RESPONSIBLE_INVALID",
                "Selecciona un responsable de Armado activo.",
                status_code=422,
            )
        target = Decimal(command["cantidad_objetivo"])
        daily_orders = session.scalars(
            select(RegistroDiarioProduccion)
            .where(
                RegistroDiarioProduccion.orden_operacion_id == order.id,
                RegistroDiarioProduccion.tipo_ot == "ENSAMBLE",
                RegistroDiarioProduccion.estado != "ANULADA",
            )
            .with_for_update()
        ).all()
        assigned = sum(
            (Decimal(item.cantidad_objetivo or 0) for item in daily_orders),
            Decimal("0"),
        )
        if assigned + target > Decimal(order.salidas[0].cantidad_objetivo):
            raise ScmServiceError(
                "ASSEMBLY_OT_QUOTA_EXCEEDED",
                "La cuota excede el saldo pendiente de la OA.",
                status_code=409,
            )
        item = RegistroDiarioProduccion(
            codigo_ot=generar_codigo_catalogo("ORDEN_TRABAJO", session=session),
            codigo_ot_sintetico=False,
            estado="PLANIFICADA",
            tipo_ot="ENSAMBLE",
            modo_ejecucion_ensamble=mode,
            ot_fabricacion_contexto_id=(
                fabrication_context.public_id if fabrication_context else None
            ),
            trabajo_color_contexto_id=(
                color_work_context.id if color_work_context else None
            ),
            orden_operacion_id=order.id,
            maquina_id=None,
            centro_trabajo_id=center.id,
            responsable_id=responsible.id,
            cantidad_objetivo=target,
            cantidad_confirmada=0,
            fecha=operational_date,
            turno=required_text(command["turno"], field="turno", max_length=20),
            created_by_id=actor.id,
        )
        session.add(item)
        session.flush()
        response = {"ot": item.to_dict()}
        _complete_operation(operation, response, 201)
        session.add(_event(
            "ORDEN_TRABAJO", item.public_id, "ASSEMBLY_OT_CREATED",
            actor, operation, response,
        ))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def list_assembly_ots(session, *, actor_id, order_id):
    load_actor(session, actor_id, capability="OT_VER")
    order = _load_assembly_order(session, order_id)
    items = session.scalars(
        select(RegistroDiarioProduccion)
        .options(
            selectinload(RegistroDiarioProduccion.detalles),
            selectinload(
                RegistroDiarioProduccion.ot_fabricacion_contexto
            ),
        )
        .where(
            RegistroDiarioProduccion.orden_operacion_id == order.id,
            RegistroDiarioProduccion.tipo_ot == "ENSAMBLE",
        )
        .order_by(RegistroDiarioProduccion.fecha.desc(), RegistroDiarioProduccion.id.desc())
    ).all()
    mangas_by_ot = {item.id: [] for item in items}
    if mangas_by_ot:
        for manga in session.scalars(
            select(ScmManga)
            .where(ScmManga.ot_id.in_(mangas_by_ot))
            .order_by(ScmManga.ot_id, ScmManga.secuencia_ot)
        ).all():
            mangas_by_ot[manga.ot_id].append(manga)
    serialization_context = _batch_ot_serialization_context(session, items)
    return {
        "items": [
            _serialize_ot(
                item,
                mangas=mangas_by_ot[item.id],
                assembly_context=serialization_context[
                    "assembly_by_ot"
                ][item.id],
                color_context=serialization_context[
                    "color_context_by_ot"
                ].get(item.id),
                output_articles_by_work=serialization_context[
                    "output_articles_by_work"
                ],
            )
            for item in items
        ]
    }


def create_supply_request(session, *, actor_id, ot_id, operation_id):
    actor = load_actor(session, actor_id, capability="ABASTECIMIENTO_SOLICITAR")
    command = {"ot_id": str(ot_id)}
    operation, replay = _reserve_operation(
        session, operation_id, f"POST /ots/{ot_id}/abastecimiento", actor, command
    )
    if replay is not None:
        return replay
    try:
        ot = _load_ot(session, ot_id, lock=True)
        if ot.estado not in ("PLANIFICADA", "EN_EJECUCION"):
            raise ScmServiceError(
                "WORK_ORDER_NOT_READY",
                "La OT debe estar planificada o en ejecución.",
                status_code=409,
            )
        existing = session.scalar(select(ScmSolicitudAbastecimiento).where(
            ScmSolicitudAbastecimiento.orden_trabajo_id == ot.id
        ))
        if existing is not None:
            raise ScmServiceError(
                "SUPPLY_REQUEST_ALREADY_EXISTS",
                "La OT ya posee una solicitud de abastecimiento.",
                status_code=409,
                details={"solicitud_id": str(existing.id)},
            )
        order = _load_assembly_order(session, ot.orden_operacion_id, lock=True)
        route_operation = session.get(ScmOperacionRuta, order.operacion_ruta_revision_id)
        if route_operation is None or route_operation.estructura_revision is None:
            raise ScmServiceError(
                "OA_BOM_SNAPSHOT_MISSING",
                "La OA no conserva una BOM resoluble.",
                status_code=409,
            )
        request = ScmSolicitudAbastecimiento(
            codigo=generar_codigo_catalogo("SOLICITUD_ABASTECIMIENTO", session=session),
            orden_ensamble_id=order.id,
            orden_trabajo_id=ot.id,
            solicitado_por_id=actor.id,
        )
        session.add(request)
        target = Decimal(ot.cantidad_objetivo)
        inline_component_ids = set()
        if ot.modo_ejecucion_ensamble == "CONCURRENTE":
            reservations = session.scalars(
                select(ScmReservaWipSalida)
                .join(ScmManga, ScmManga.id == ScmReservaWipSalida.manga_id)
                .where(
                    ScmManga.ot_id == ot.id,
                    ScmReservaWipSalida.estado.in_((
                        "CREDITO_EN_LINEA_PENDIENTE",
                        "APLICADA",
                    )),
                )
                .order_by(ScmReservaWipSalida.id)
                .with_for_update(of=ScmReservaWipSalida)
            ).all()
            inline_component_ids = {
                item.articulo_componente_id for item in reservations
            }
            if len(inline_component_ids) != 1:
                raise ScmServiceError(
                    "INLINE_RESERVATION_REQUIRED",
                    "Asigna las mangas y su reserva en línea antes de solicitar abastecimiento.",
                    status_code=409,
                )
        for component in route_operation.estructura_revision.componentes:
            if component.articulo_componente_id in inline_component_ids:
                continue
            per_output = Decimal(component.cantidad)
            waste = Decimal(component.merma_tecnica_pct or 0)
            required = target * per_output
            if waste:
                required /= Decimal("1") - waste / Decimal("100")
            required = required.to_integral_value(rounding=ROUND_CEILING)
            session.add(ScmSolicitudAbastecimientoLinea(
                solicitud=request,
                articulo_scm_id=component.articulo_componente_id,
                cantidad_requerida=required,
                cantidad_por_salida=per_output,
                merma_tecnica_pct=waste,
            ))
        session.flush()
        response = {"solicitud": request_payload(request)}
        _complete_operation(operation, response, 201)
        session.add(_event(
            "SOLICITUD_ABASTECIMIENTO", request.id, "SUPPLY_REQUEST_CREATED",
            actor, operation, response,
        ))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def list_supply_requests(session, *, actor_id, state=None):
    load_actor(session, actor_id, capability="ABASTECIMIENTO_VER")
    statement = select(ScmSolicitudAbastecimiento).order_by(
        ScmSolicitudAbastecimiento.created_at.desc()
    )
    if state:
        statement = statement.where(
            ScmSolicitudAbastecimiento.estado == str(state).strip().upper()
        )
    return {"items": [request_payload(item) for item in session.scalars(statement).all()]}


def get_supply_request(session, *, actor_id, request_id):
    load_actor(session, actor_id, capability="ABASTECIMIENTO_VER")
    return request_payload(_load_request(session, request_id))


def _resolve_existence(session, data):
    if data.get("label_id"):
        try:
            public_id = uuid.UUID(str(data["label_id"]))
        except (TypeError, ValueError, AttributeError) as error:
            raise ScmServiceError(
                "QR_INVALID", "El QR no contiene un identificador válido.", status_code=400
            ) from error
        label = session.scalar(select(ScmEtiquetaManga).where(
            ScmEtiquetaManga.public_id == public_id
        ))
        if label is None or label.estado == "INVALIDADA":
            raise ScmServiceError(
                "LABEL_NOT_FOUND", "La etiqueta no existe o fue invalidada.", status_code=404
            )
        manga_id = label.manga_id
    else:
        code = required_text(data.get("manga_codigo"), field="manga_codigo", max_length=80).upper()
        manga = session.scalar(select(ScmManga).where(ScmManga.codigo == code))
        if manga is None:
            raise ScmServiceError(
                "MANGA_NOT_FOUND", "No existe una manga con ese código.", status_code=404
            )
        manga_id = manga.id
    existence = session.scalar(
        select(ScmExistenciaManga)
        .where(ScmExistenciaManga.manga_id == manga_id)
        .with_for_update()
    )
    if existence is None:
        raise ScmServiceError(
            "MANGA_NOT_IN_WAREHOUSE",
            "La manga todavía no tiene existencia en Almacén.",
            status_code=409,
        )
    return existence


def assign_supply_manga(session, *, actor_id, request_id, operation_id, data):
    reject_unknown_fields(data, allowed={"version", "linea_id", "label_id", "manga_codigo"})
    actor = load_actor(session, actor_id, capability="PICKING_PREPARAR")
    command = {"request_id": str(request_id), **data}
    operation, replay = _reserve_operation(
        session, operation_id, f"POST /abastecimiento/{request_id}/mangas", actor, command
    )
    if replay is not None:
        return replay
    try:
        request = _load_request(session, request_id, lock=True)
        _check_version(request, data.get("version"))
        if request.estado not in ("SOLICITADA", "EN_PREPARACION"):
            raise ScmServiceError(
                "SUPPLY_REQUEST_NOT_PREPARABLE",
                "La solicitud ya no admite nuevas reservas.",
                status_code=409,
            )
        try:
            line_id = uuid.UUID(str(data.get("linea_id")))
        except (TypeError, ValueError, AttributeError) as error:
            raise ScmServiceError(
                "SUPPLY_LINE_INVALID", "Selecciona una línea válida.", status_code=400
            ) from error
        line = session.scalar(
            select(ScmSolicitudAbastecimientoLinea)
            .where(
                ScmSolicitudAbastecimientoLinea.id == line_id,
                ScmSolicitudAbastecimientoLinea.solicitud_id == request.id,
            )
            .with_for_update()
        )
        if line is None:
            raise ScmServiceError(
                "SUPPLY_LINE_NOT_FOUND", "La línea no pertenece a la solicitud.", status_code=404
            )
        existence = _resolve_existence(session, data)
        if existence.articulo_scm_id != line.articulo_scm_id:
            raise ScmServiceError(
                "SUPPLY_ARTICLE_MISMATCH",
                "La manga escaneada no contiene el componente solicitado.",
                status_code=409,
            )
        if existence.estado_calidad != "LIBERADA":
            raise ScmServiceError(
                "COMPONENT_QUALITY_NOT_RELEASED",
                "La manga debe estar liberada por Calidad.",
                status_code=409,
            )
        if existence.estado_logistico != "RECIBIDA_ALMACEN":
            raise ScmServiceError(
                "MANGA_NOT_AVAILABLE",
                "La manga no está disponible físicamente en Almacén.",
                status_code=409,
            )
        active = session.scalar(
            select(ScmAsignacionAbastecimiento).where(
                ScmAsignacionAbastecimiento.existencia_manga_id == existence.id,
                ScmAsignacionAbastecimiento.estado.in_(ACTIVE_ASSIGNMENT_STATES),
            )
        )
        if active is not None:
            raise ScmServiceError(
                "MANGA_ALREADY_RESERVED",
                "La manga ya está reservada para otra solicitud.",
                status_code=409,
            )
        quantity = Decimal(existence.cantidad_libre)
        if quantity <= 0 or quantity != Decimal(existence.cantidad_fisica):
            raise ScmServiceError(
                "MANGA_NOT_FULLY_AVAILABLE",
                "El piloto solo reserva mangas completas y libres.",
                status_code=409,
            )
        balance = session.scalar(
            select(ScmSaldoInventario)
            .where(ScmSaldoInventario.id == existence.saldo_id)
            .with_for_update()
        )
        if Decimal(balance.cantidad_fisica) - Decimal(balance.cantidad_reservada) - Decimal(balance.cantidad_no_disponible) < quantity:
            raise ScmServiceError(
                "INVENTORY_NOT_AVAILABLE",
                "El saldo agregado no cubre la manga escaneada.",
                status_code=409,
            )
        assignment = ScmAsignacionAbastecimiento(
            linea=line,
            existencia=existence,
            cantidad_asignada=quantity,
            asignada_por_id=actor.id,
        )
        session.add(assignment)
        existence.cantidad_reservada = quantity
        existence.estado_logistico = "RESERVADA"
        existence.version += 1
        balance.cantidad_reservada = Decimal(balance.cantidad_reservada) + quantity
        balance.version += 1
        request.estado = "EN_PREPARACION"
        request.version += 1
        session.flush()
        response = {"solicitud": request_payload(request)}
        _complete_operation(operation, response, 201)
        session.add(_event(
            "SOLICITUD_ABASTECIMIENTO", request.id, "SUPPLY_MANGA_RESERVED",
            actor, operation, response,
        ))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def assign_non_exact_supply_source(
    session, *, actor_id, request_id, operation_id, data
):
    """Registra una apertura excepcional sin inventar una genealogia exacta."""
    reject_unknown_fields(data, allowed={
        "version", "linea_id", "modo", "cantidad", "ubicacion_codigo",
        "motivo", "candidato_existencia_ids", "candidato_codigos",
    })
    mode = str(data.get("modo") or "").strip().upper()
    if mode not in ("CONJUNTO_CANDIDATOS", "LEGACY_SIN_ORIGEN"):
        raise ScmServiceError(
            "NON_EXACT_SOURCE_MODE_INVALID",
            "Selecciona conjunto de candidatos o apertura legacy.", status_code=422,
        )
    capability = (
        "GENEALOGIA_CANDIDATA_CONFIRMAR"
        if mode == "CONJUNTO_CANDIDATOS"
        else "GENEALOGIA_LEGACY_APERTURA"
    )
    actor = load_actor(session, actor_id, capability=capability)
    quantity = _quantity(data.get("cantidad"))
    reason = required_text(data.get("motivo"), field="motivo", max_length=500)
    location_code = required_text(
        data.get("ubicacion_codigo"), field="ubicacion_codigo", max_length=40
    ).upper()
    candidate_ids = sorted({str(value) for value in (data.get("candidato_existencia_ids") or [])})
    candidate_codes = sorted({
        str(value).strip().upper() for value in (data.get("candidato_codigos") or [])
        if str(value).strip()
    })
    command = {
        "request_id": str(request_id), "version": data.get("version"),
        "linea_id": str(data.get("linea_id") or ""), "modo": mode,
        "cantidad": format(quantity, "f"), "ubicacion_codigo": location_code,
        "motivo": reason, "candidato_existencia_ids": candidate_ids,
        "candidato_codigos": candidate_codes,
    }
    endpoint = f"/abastecimiento/{request_id}/fuentes-no-exactas"
    operation, replay = _reserve_operation(session, operation_id, endpoint, actor, command)
    if replay is not None:
        return replay
    try:
        request = _load_request(session, request_id, lock=True)
        _check_version(request, data.get("version"))
        if request.estado not in ("SOLICITADA", "EN_PREPARACION"):
            raise ScmServiceError(
                "SUPPLY_REQUEST_NOT_PREPARABLE",
                "La fuente solo puede abrirse durante la preparacion.", status_code=409,
            )
        try:
            line_id = uuid.UUID(command["linea_id"])
        except (TypeError, ValueError) as error:
            raise ScmServiceError("SUPPLY_LINE_INVALID", "La linea no es valida.", status_code=422) from error
        line = next((item for item in request.lineas if item.id == line_id), None)
        if line is None:
            raise ScmServiceError("SUPPLY_LINE_NOT_FOUND", "La linea no pertenece a la solicitud.", status_code=404)
        pending = Decimal(line.cantidad_requerida) - Decimal(line.cantidad_asignada)
        if pending <= 0 or quantity > pending:
            raise ScmServiceError(
                "NON_EXACT_SOURCE_EXCEEDS_GAP",
                "La cantidad excepcional no puede superar el faltante de la linea.", status_code=409,
                details={"faltante": format(max(pending, Decimal('0')), 'f')},
            )
        location = session.scalar(
            select(ScmUbicacionInventario).where(
                ScmUbicacionInventario.codigo == location_code,
                ScmUbicacionInventario.activo.is_(True),
            ).with_for_update()
        )
        if location is None:
            raise ScmServiceError("UBICACION_NOT_FOUND", "La ubicacion no existe.", status_code=404)
        balance = _balance(session, line.articulo_scm_id, location.id)
        candidates = []
        if mode == "CONJUNTO_CANDIDATOS":
            if len(candidate_ids) + len(candidate_codes) < 2:
                raise ScmServiceError(
                    "CANDIDATE_SET_TOO_SMALL",
                    "El conjunto debe identificar al menos dos mangas candidatas.", status_code=422,
                )
            try:
                parsed_ids = [uuid.UUID(value) for value in candidate_ids]
            except ValueError as error:
                raise ScmServiceError("CANDIDATE_ID_INVALID", "Una manga candidata no es valida.", status_code=422) from error
            candidate_statement = select(ScmExistenciaManga).join(ScmManga)
            selectors = []
            if parsed_ids:
                selectors.append(ScmExistenciaManga.id.in_(parsed_ids))
            if candidate_codes:
                selectors.append(ScmManga.codigo.in_(candidate_codes))
            from sqlalchemy import or_
            candidates = session.scalars(
                candidate_statement.where(or_(*selectors)).with_for_update()
            ).unique().all()
            if len(candidates) != len(set(parsed_ids)) + len(set(candidate_codes)) or any(
                item.articulo_scm_id != line.articulo_scm_id
                or item.ubicacion_id != location.id
                or item.estado_calidad != "LIBERADA"
                or item.estado_logistico != "RECIBIDA_ALMACEN"
                for item in candidates
            ):
                raise ScmServiceError(
                    "CANDIDATE_SET_INCOMPATIBLE",
                    "Todas las candidatas deben ser libres, liberadas, del mismo articulo y ubicacion.",
                    status_code=409,
                )
            candidate_total = sum((Decimal(item.cantidad_libre) for item in candidates), Decimal("0"))
            if candidate_total < quantity:
                raise ScmServiceError(
                    "CANDIDATE_SET_INSUFFICIENT", "El conjunto candidato no cubre la cantidad.",
                    status_code=409,
                )
            available = candidate_total
            for item in candidates:
                item.estado_logistico = "AGRUPADA_CANDIDATOS"
                item.version += 1
        else:
            balance.cantidad_fisica = Decimal(balance.cantidad_fisica) + quantity
            balance.version += 1
            session.add(ScmMovimientoInventario(
                saldo=balance, tipo="SALDO_INICIAL", cantidad_delta=quantity,
                saldo_fisico_resultante=balance.cantidad_fisica,
                motivo=f"Apertura legacy autorizada: {reason}",
                referencia_tipo="APERTURA_LEGACY_ARMADO", referencia_id=str(request.id),
                actor_id=actor.id, operation_id=uuid.uuid5(operation.operation_id, "LEGACY:OPEN"),
            ))
        if not candidates:
            available = quantity
        pool = ScmPoolOrigenArmado(
            articulo_scm_id=line.articulo_scm_id, saldo_id=balance.id, modo=mode,
            cantidad_inicial=available, cantidad_disponible=available,
            motivo=reason, creado_por_id=actor.id, operation_id=operation.operation_id,
            candidatos=candidates,
        )
        session.add(pool)
        session.flush()
        if Decimal(balance.cantidad_fisica) - Decimal(balance.cantidad_reservada) < quantity:
            raise ScmServiceError(
                "INVENTORY_NOT_AVAILABLE", "El saldo agregado no cubre la apertura.", status_code=409
            )
        balance.cantidad_reservada = Decimal(balance.cantidad_reservada) + quantity
        balance.version += 1
        assignment = ScmAsignacionPoolArmado(
            linea_id=line.id, pool_id=pool.id, saldo_id=balance.id,
            cantidad_asignada=quantity, asignada_por_id=actor.id,
        )
        pool.cantidad_disponible = available - quantity
        session.add(assignment)
        request.estado = "EN_PREPARACION"
        request.version += 1
        session.flush()
        response = {"solicitud": request_payload(request), "fuente_no_exacta_id": str(pool.id)}
        _complete_operation(operation, response)
        session.add(_event("POOL_ORIGEN_ARMADO", pool.id, "NON_EXACT_SOURCE_OPENED", actor, operation, response))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def mark_supply_ready(session, *, actor_id, request_id, operation_id, data):
    reject_unknown_fields(data, allowed={"version"})
    actor = load_actor(session, actor_id, capability="PICKING_PREPARAR")
    command = {"request_id": str(request_id), "version": data.get("version")}
    operation, replay = _reserve_operation(
        session, operation_id, f"POST /abastecimiento/{request_id}/lista", actor, command
    )
    if replay is not None:
        return replay
    try:
        request = _load_request(session, request_id, lock=True)
        _check_version(request, data.get("version"))
        if request.estado != "EN_PREPARACION":
            raise ScmServiceError(
                "SUPPLY_REQUEST_NOT_IN_PREPARATION",
                "La solicitud debe estar en preparación.",
                status_code=409,
            )
        uncovered = [line for line in request.lineas if Decimal(line.cantidad_asignada) < Decimal(line.cantidad_requerida)]
        if uncovered:
            raise ScmServiceError(
                "SUPPLY_REQUEST_INCOMPLETE",
                "Faltan mangas para cubrir uno o más componentes.",
                status_code=409,
                details={"lineas": [str(line.id) for line in uncovered]},
            )
        for line in request.lineas:
            for assignment in line.asignaciones:
                if assignment.estado == "RESERVADA":
                    assignment.estado = "EN_PICKING"
                    assignment.existencia.estado_logistico = "EN_PICKING"
                    assignment.existencia.version += 1
            for assignment in line.asignaciones_pool:
                if assignment.estado == "RESERVADA":
                    assignment.estado = "EN_PICKING"
        request.estado = "LISTA"
        request.preparada_por_id = actor.id
        request.preparada_at = utc_now()
        request.version += 1
        session.flush()
        response = {"solicitud": request_payload(request)}
        _complete_operation(operation, response)
        session.add(_event("SOLICITUD_ABASTECIMIENTO", request.id, "SUPPLY_READY", actor, operation, response))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def _location(session, code, name, classes):
    item = session.scalar(
        select(ScmUbicacionInventario)
        .where(ScmUbicacionInventario.codigo == code)
        .with_for_update()
    )
    if item is None:
        item = ScmUbicacionInventario(
            codigo=code, nombre=name, clases_articulo_json=list(classes)
        )
        session.add(item)
        session.flush()
    return item


def _balance(session, article_id, location_id):
    item = session.scalar(
        select(ScmSaldoInventario)
        .where(
            ScmSaldoInventario.articulo_scm_id == article_id,
            ScmSaldoInventario.ubicacion_id == location_id,
        )
        .with_for_update()
    )
    if item is None:
        item = ScmSaldoInventario(
            articulo_scm_id=article_id, ubicacion_id=location_id
        )
        session.add(item)
        session.flush()
    return item


def _move(session, assignment, destination, *, operation_id, prefix, actor, reference):
    existence = assignment.existencia
    quantity = Decimal(assignment.saldo)
    if quantity <= 0:
        raise ScmServiceError(
            "SUPPLY_ASSIGNMENT_EMPTY", "La manga ya no tiene saldo trasladable.", status_code=409
        )
    origin = session.scalar(
        select(ScmSaldoInventario)
        .where(ScmSaldoInventario.id == existence.saldo_id)
        .with_for_update()
    )
    target = _balance(session, existence.articulo_scm_id, destination.id)
    if Decimal(origin.cantidad_fisica) < quantity or Decimal(origin.cantidad_reservada) < quantity:
        raise ScmServiceError(
            "INVENTORY_TRANSFER_MISMATCH",
            "El saldo de origen no coincide con la manga reservada.",
            status_code=409,
        )
    origin.cantidad_fisica = Decimal(origin.cantidad_fisica) - quantity
    origin.cantidad_reservada = Decimal(origin.cantidad_reservada) - quantity
    origin.version += 1
    target.cantidad_fisica = Decimal(target.cantidad_fisica) + quantity
    target.cantidad_reservada = Decimal(target.cantidad_reservada) + quantity
    target.version += 1
    session.add_all([
        ScmMovimientoInventario(
            saldo=origin, tipo=f"{prefix}_SALIDA", cantidad_delta=-quantity,
            saldo_fisico_resultante=origin.cantidad_fisica,
            motivo=reference, referencia_tipo="SOLICITUD_ABASTECIMIENTO",
            referencia_id=str(assignment.linea.solicitud_id), actor_id=actor.id,
            operation_id=uuid.uuid5(operation_id, f"{assignment.id}:OUT:{prefix}"),
        ),
        ScmMovimientoInventario(
            saldo=target, tipo=f"{prefix}_ENTRADA", cantidad_delta=quantity,
            saldo_fisico_resultante=target.cantidad_fisica,
            motivo=reference, referencia_tipo="SOLICITUD_ABASTECIMIENTO",
            referencia_id=str(assignment.linea.solicitud_id), actor_id=actor.id,
            operation_id=uuid.uuid5(operation_id, f"{assignment.id}:IN:{prefix}"),
        ),
    ])
    existence.saldo = target
    existence.ubicacion = destination
    existence.version += 1


def _move_pool(session, assignment, destination, *, operation_id, prefix, actor, reference):
    quantity = Decimal(assignment.saldo_cantidad)
    if quantity <= 0:
        raise ScmServiceError(
            "SUPPLY_ASSIGNMENT_EMPTY", "La fuente agrupada ya no tiene saldo trasladable.", status_code=409
        )
    origin = session.scalar(
        select(ScmSaldoInventario)
        .where(ScmSaldoInventario.id == assignment.saldo_id)
        .with_for_update()
    )
    target = _balance(session, assignment.linea.articulo_scm_id, destination.id)
    if Decimal(origin.cantidad_fisica) < quantity or Decimal(origin.cantidad_reservada) < quantity:
        raise ScmServiceError(
            "INVENTORY_TRANSFER_MISMATCH",
            "El saldo agregado de origen no coincide con la asignacion.", status_code=409,
        )
    origin.cantidad_fisica = Decimal(origin.cantidad_fisica) - quantity
    origin.cantidad_reservada = Decimal(origin.cantidad_reservada) - quantity
    origin.version += 1
    target.cantidad_fisica = Decimal(target.cantidad_fisica) + quantity
    target.cantidad_reservada = Decimal(target.cantidad_reservada) + quantity
    target.version += 1
    session.add_all([
        ScmMovimientoInventario(
            saldo=origin, tipo=f"{prefix}_SALIDA", cantidad_delta=-quantity,
            saldo_fisico_resultante=origin.cantidad_fisica, motivo=reference,
            referencia_tipo="POOL_ORIGEN_ARMADO", referencia_id=str(assignment.pool_id),
            actor_id=actor.id,
            operation_id=uuid.uuid5(operation_id, f"{assignment.id}:POOL:OUT:{prefix}"),
        ),
        ScmMovimientoInventario(
            saldo=target, tipo=f"{prefix}_ENTRADA", cantidad_delta=quantity,
            saldo_fisico_resultante=target.cantidad_fisica, motivo=reference,
            referencia_tipo="POOL_ORIGEN_ARMADO", referencia_id=str(assignment.pool_id),
            actor_id=actor.id,
            operation_id=uuid.uuid5(operation_id, f"{assignment.id}:POOL:IN:{prefix}"),
        ),
    ])
    assignment.saldo = target


def dispatch_supply(session, *, actor_id, request_id, operation_id, data):
    reject_unknown_fields(data, allowed={"version"})
    actor = load_actor(session, actor_id, capability="PICKING_DESPACHAR")
    command = {"request_id": str(request_id), "version": data.get("version")}
    operation, replay = _reserve_operation(
        session, operation_id, f"POST /abastecimiento/{request_id}/despachar", actor, command
    )
    if replay is not None:
        return replay
    try:
        request = _load_request(session, request_id, lock=True)
        _check_version(request, data.get("version"))
        if request.estado != "LISTA":
            raise ScmServiceError(
                "SUPPLY_REQUEST_NOT_READY", "La solicitud todavía no está lista.", status_code=409
            )
        destination = _location(
            session, "TRANSITO_PRODUCCION", "Tránsito hacia Producción",
            ["PIEZA_COLOR", "SUBENSAMBLE_WIP"],
        )
        for line in request.lineas:
            for assignment in line.asignaciones:
                if assignment.estado != "EN_PICKING":
                    raise ScmServiceError(
                        "SUPPLY_ASSIGNMENT_NOT_PICKED",
                        "Todas las mangas deben estar preparadas.", status_code=409,
                    )
                _move(
                    session, assignment, destination, operation_id=operation.operation_id,
                    prefix="TRASLADO", actor=actor,
                    reference=f"Despacho {request.codigo} hacia Armado",
                )
                assignment.estado = "EN_TRANSITO_PRODUCCION"
                assignment.existencia.estado_logistico = "EN_TRANSITO_PRODUCCION"
            for assignment in line.asignaciones_pool:
                if assignment.estado != "EN_PICKING":
                    raise ScmServiceError(
                        "SUPPLY_ASSIGNMENT_NOT_PICKED",
                        "Todas las fuentes agrupadas deben estar preparadas.", status_code=409,
                    )
                _move_pool(
                    session, assignment, destination, operation_id=operation.operation_id,
                    prefix="TRASLADO", actor=actor,
                    reference=f"Despacho agrupado {request.codigo} hacia Armado",
                )
                assignment.estado = "EN_TRANSITO_PRODUCCION"
        request.estado = "DESPACHADA"
        request.despachada_por_id = actor.id
        request.despachada_at = utc_now()
        request.version += 1
        session.flush()
        response = {"solicitud": request_payload(request)}
        _complete_operation(operation, response)
        session.add(_event("SOLICITUD_ABASTECIMIENTO", request.id, "SUPPLY_DISPATCHED", actor, operation, response))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def receive_supply(session, *, actor_id, request_id, operation_id, data):
    reject_unknown_fields(data, allowed={"version"})
    actor = load_actor(session, actor_id, capability="ABASTECIMIENTO_RECIBIR")
    command = {"request_id": str(request_id), "version": data.get("version")}
    operation, replay = _reserve_operation(
        session, operation_id, f"POST /abastecimiento/{request_id}/recibir", actor, command
    )
    if replay is not None:
        return replay
    try:
        request = _load_request(session, request_id, lock=True)
        _check_version(request, data.get("version"))
        if request.estado != "DESPACHADA":
            raise ScmServiceError(
                "SUPPLY_REQUEST_NOT_DISPATCHED",
                "Almacén todavía no registró el despacho.", status_code=409,
            )
        destination = _location(
            session, "MESA_ARMADO", "Mesa de Armado",
            ["PIEZA_COLOR", "SUBENSAMBLE_WIP"],
        )
        for line in request.lineas:
            for assignment in line.asignaciones:
                if assignment.estado != "EN_TRANSITO_PRODUCCION":
                    raise ScmServiceError(
                        "SUPPLY_ASSIGNMENT_NOT_IN_TRANSIT",
                        "La manga no está en tránsito hacia Armado.", status_code=409,
                    )
                _move(
                    session, assignment, destination, operation_id=operation.operation_id,
                    prefix="TRASLADO", actor=actor,
                    reference=f"Recepción {request.codigo} en Mesa de Armado",
                )
                assignment.estado = "EN_STAGING_ARMADO"
                assignment.existencia.estado_logistico = "EN_STAGING_ARMADO"
            for assignment in line.asignaciones_pool:
                if assignment.estado != "EN_TRANSITO_PRODUCCION":
                    raise ScmServiceError(
                        "SUPPLY_ASSIGNMENT_NOT_IN_TRANSIT",
                        "La fuente agrupada no esta en transito hacia Armado.", status_code=409,
                    )
                _move_pool(
                    session, assignment, destination, operation_id=operation.operation_id,
                    prefix="TRASLADO", actor=actor,
                    reference=f"Recepcion agrupada {request.codigo} en Mesa de Armado",
                )
                assignment.estado = "EN_STAGING_ARMADO"
        request.estado = "RECIBIDA"
        request.recibida_por_id = actor.id
        request.recibida_at = utc_now()
        request.version += 1
        session.flush()
        response = {"solicitud": request_payload(request)}
        _complete_operation(operation, response)
        session.add(_event("SOLICITUD_ABASTECIMIENTO", request.id, "SUPPLY_RECEIVED", actor, operation, response))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def request_supply_return(session, *, actor_id, assignment_id, operation_id):
    actor = load_actor(session, actor_id, capability="ABASTECIMIENTO_DEVOLVER")
    command = {"assignment_id": str(assignment_id)}
    operation, replay = _reserve_operation(
        session, operation_id, f"POST /abastecimiento/asignaciones/{assignment_id}/retorno", actor, command
    )
    if replay is not None:
        return replay
    try:
        assignment = session.scalar(
            select(ScmAsignacionAbastecimiento)
            .where(ScmAsignacionAbastecimiento.id == assignment_id)
            .with_for_update()
        )
        if assignment is None:
            raise ScmServiceError(
                "SUPPLY_ASSIGNMENT_NOT_FOUND", "La asignación no existe.", status_code=404
            )
        if assignment.estado not in ("EN_STAGING_ARMADO", "ABIERTA_EN_CONSUMO"):
            raise ScmServiceError(
                "SUPPLY_RETURN_NOT_ALLOWED",
                "Solo puede devolverse una manga que está en Mesa de Armado.", status_code=409,
            )
        if Decimal(assignment.saldo) <= 0:
            raise ScmServiceError(
                "SUPPLY_ASSIGNMENT_EMPTY", "La manga no tiene remanente.", status_code=409
            )
        assignment.estado = "PENDIENTE_RETORNO"
        assignment.existencia.estado_logistico = "PENDIENTE_RETORNO"
        assignment.existencia.version += 1
        session.flush()
        response = {"solicitud": request_payload(assignment.linea.solicitud)}
        _complete_operation(operation, response)
        session.add(_event("ASIGNACION_ABASTECIMIENTO", assignment.id, "SUPPLY_RETURN_REQUESTED", actor, operation, response))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def dispatch_supply_return(session, *, actor_id, assignment_id, operation_id):
    actor = load_actor(session, actor_id, capability="ABASTECIMIENTO_DEVOLVER")
    command = {"assignment_id": str(assignment_id)}
    operation, replay = _reserve_operation(
        session, operation_id, f"POST /abastecimiento/asignaciones/{assignment_id}/despachar-retorno", actor, command
    )
    if replay is not None:
        return replay
    try:
        assignment = session.scalar(
            select(ScmAsignacionAbastecimiento)
            .where(ScmAsignacionAbastecimiento.id == assignment_id)
            .with_for_update()
        )
        if assignment is None or assignment.estado != "PENDIENTE_RETORNO":
            raise ScmServiceError(
                "SUPPLY_RETURN_NOT_READY", "El retorno todavía no está preparado.", status_code=409
            )
        destination = _location(
            session, "TRANSITO_ALMACEN", "Tránsito hacia Almacén",
            ["PIEZA_COLOR", "SUBENSAMBLE_WIP"],
        )
        _move(
            session, assignment, destination, operation_id=operation.operation_id,
            prefix="RETORNO", actor=actor, reference="Retorno de remanente a Almacén",
        )
        assignment.estado = "EN_TRANSITO_ALMACEN"
        assignment.existencia.estado_logistico = "EN_TRANSITO_ALMACEN"
        session.flush()
        response = {"solicitud": request_payload(assignment.linea.solicitud)}
        _complete_operation(operation, response)
        session.add(_event("ASIGNACION_ABASTECIMIENTO", assignment.id, "SUPPLY_RETURN_DISPATCHED", actor, operation, response))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def receive_supply_return(session, *, actor_id, assignment_id, operation_id, data):
    reject_unknown_fields(data, allowed={"ubicacion_codigo"})
    actor = load_actor(session, actor_id, capability="RETORNO_RECIBIR")
    location_code = required_text(
        data.get("ubicacion_codigo"), field="ubicacion_codigo", max_length=40
    ).upper()
    command = {"assignment_id": str(assignment_id), "ubicacion_codigo": location_code}
    operation, replay = _reserve_operation(
        session, operation_id, f"POST /abastecimiento/asignaciones/{assignment_id}/recibir-retorno", actor, command
    )
    if replay is not None:
        return replay
    try:
        assignment = session.scalar(
            select(ScmAsignacionAbastecimiento)
            .where(ScmAsignacionAbastecimiento.id == assignment_id)
            .with_for_update()
        )
        if assignment is None or assignment.estado != "EN_TRANSITO_ALMACEN":
            raise ScmServiceError(
                "SUPPLY_RETURN_NOT_IN_TRANSIT", "El retorno no está en tránsito.", status_code=409
            )
        location = session.scalar(
            select(ScmUbicacionInventario).where(
                ScmUbicacionInventario.codigo == location_code,
                ScmUbicacionInventario.activo.is_(True),
            )
        )
        if location is None or (
            location.clases_articulo_json
            and assignment.linea.articulo.clase not in location.clases_articulo_json
        ):
            raise ScmServiceError(
                "UBICACION_INCOMPATIBLE",
                "Selecciona una ubicación de Almacén compatible.", status_code=422,
            )
        _move(
            session, assignment, location, operation_id=operation.operation_id,
            prefix="RETORNO", actor=actor, reference="Recepción de remanente en Almacén",
        )
        remainder = Decimal(assignment.saldo)
        balance = assignment.existencia.saldo
        balance.cantidad_reservada = Decimal(balance.cantidad_reservada) - remainder
        balance.version += 1
        assignment.existencia.cantidad_reservada = Decimal(
            assignment.existencia.cantidad_reservada
        ) - remainder
        assignment.existencia.estado_logistico = "RECIBIDA_ALMACEN"
        assignment.existencia.version += 1
        assignment.cantidad_retornada = Decimal(assignment.cantidad_retornada) + remainder
        assignment.estado = "RETORNADA"
        request = assignment.linea.solicitud
        if all(
            item.estado in ("RETORNADA", "CONSUMIDA", "CANCELADA")
            for line in request.lineas
            for item in (*line.asignaciones, *line.asignaciones_pool)
        ):
            request.estado = "CERRADA"
            request.version += 1
        session.flush()
        response = {"solicitud": request_payload(request)}
        _complete_operation(operation, response)
        session.add(_event("ASIGNACION_ABASTECIMIENTO", assignment.id, "SUPPLY_RETURN_RECEIVED", actor, operation, response))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def request_pool_supply_return(session, *, actor_id, assignment_id, operation_id):
    actor = load_actor(session, actor_id, capability="ABASTECIMIENTO_DEVOLVER")
    command = {"assignment_id": str(assignment_id)}
    endpoint = f"/abastecimiento/asignaciones-pool/{assignment_id}/retorno"
    operation, replay = _reserve_operation(session, operation_id, endpoint, actor, command)
    if replay is not None:
        return replay
    try:
        assignment = session.scalar(select(ScmAsignacionPoolArmado).where(
            ScmAsignacionPoolArmado.id == assignment_id
        ).with_for_update())
        if assignment is None:
            raise ScmServiceError("SUPPLY_ASSIGNMENT_NOT_FOUND", "La fuente agrupada no existe.", status_code=404)
        if assignment.estado not in ("EN_STAGING_ARMADO", "ABIERTA_EN_CONSUMO") or Decimal(assignment.saldo_cantidad) <= 0:
            raise ScmServiceError("SUPPLY_RETURN_NOT_ALLOWED", "La fuente no tiene remanente retornable en Mesa.", status_code=409)
        assignment.estado = "PENDIENTE_RETORNO"
        session.flush()
        response = {"solicitud": request_payload(assignment.linea.solicitud)}
        _complete_operation(operation, response)
        session.add(_event("ASIGNACION_POOL_ARMADO", assignment.id, "SUPPLY_RETURN_REQUESTED", actor, operation, response))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def dispatch_pool_supply_return(session, *, actor_id, assignment_id, operation_id):
    actor = load_actor(session, actor_id, capability="ABASTECIMIENTO_DEVOLVER")
    command = {"assignment_id": str(assignment_id)}
    endpoint = f"/abastecimiento/asignaciones-pool/{assignment_id}/despachar-retorno"
    operation, replay = _reserve_operation(session, operation_id, endpoint, actor, command)
    if replay is not None:
        return replay
    try:
        assignment = session.scalar(select(ScmAsignacionPoolArmado).where(
            ScmAsignacionPoolArmado.id == assignment_id
        ).with_for_update())
        if assignment is None or assignment.estado != "PENDIENTE_RETORNO":
            raise ScmServiceError("SUPPLY_RETURN_NOT_READY", "El retorno agrupado no esta preparado.", status_code=409)
        destination = _location(session, "TRANSITO_ALMACEN", "Transito hacia Almacen", ["PIEZA_COLOR", "SUBENSAMBLE_WIP"])
        _move_pool(session, assignment, destination, operation_id=operation.operation_id,
                   prefix="RETORNO", actor=actor, reference="Retorno agrupado a Almacen")
        assignment.estado = "EN_TRANSITO_ALMACEN"
        session.flush()
        response = {"solicitud": request_payload(assignment.linea.solicitud)}
        _complete_operation(operation, response)
        session.add(_event("ASIGNACION_POOL_ARMADO", assignment.id, "SUPPLY_RETURN_DISPATCHED", actor, operation, response))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def receive_pool_supply_return(session, *, actor_id, assignment_id, operation_id, data):
    reject_unknown_fields(data, allowed={"ubicacion_codigo"})
    actor = load_actor(session, actor_id, capability="RETORNO_RECIBIR")
    location_code = required_text(data.get("ubicacion_codigo"), field="ubicacion_codigo", max_length=40).upper()
    command = {"assignment_id": str(assignment_id), "ubicacion_codigo": location_code}
    endpoint = f"/abastecimiento/asignaciones-pool/{assignment_id}/recibir-retorno"
    operation, replay = _reserve_operation(session, operation_id, endpoint, actor, command)
    if replay is not None:
        return replay
    try:
        assignment = session.scalar(select(ScmAsignacionPoolArmado).where(
            ScmAsignacionPoolArmado.id == assignment_id
        ).with_for_update())
        if assignment is None or assignment.estado != "EN_TRANSITO_ALMACEN":
            raise ScmServiceError("SUPPLY_RETURN_NOT_IN_TRANSIT", "El retorno agrupado no esta en transito.", status_code=409)
        location = session.scalar(select(ScmUbicacionInventario).where(
            ScmUbicacionInventario.codigo == location_code,
            ScmUbicacionInventario.activo.is_(True),
        ))
        if location is None:
            raise ScmServiceError("UBICACION_INCOMPATIBLE", "Selecciona una ubicacion activa.", status_code=422)
        _move_pool(session, assignment, location, operation_id=operation.operation_id,
                   prefix="RETORNO", actor=actor, reference="Recepcion de remanente agrupado")
        remainder = Decimal(assignment.saldo_cantidad)
        balance = assignment.saldo
        balance.cantidad_reservada = Decimal(balance.cantidad_reservada) - remainder
        balance.version += 1
        assignment.cantidad_retornada = Decimal(assignment.cantidad_retornada) + remainder
        assignment.pool.cantidad_disponible = Decimal(assignment.pool.cantidad_disponible) + remainder
        assignment.estado = "RETORNADA"
        request = assignment.linea.solicitud
        if all(
            item.estado in ("RETORNADA", "CONSUMIDA", "CANCELADA")
            for line in request.lineas
            for item in (*line.asignaciones, *line.asignaciones_pool)
        ):
            request.estado = "CERRADA"
            request.version += 1
        session.flush()
        response = {"solicitud": request_payload(request)}
        _complete_operation(operation, response)
        session.add(_event("ASIGNACION_POOL_ARMADO", assignment.id, "SUPPLY_RETURN_RECEIVED", actor, operation, response))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise

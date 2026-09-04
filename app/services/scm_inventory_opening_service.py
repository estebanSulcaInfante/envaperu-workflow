"""Lotes controlados para el corte inicial del Kardex."""

import copy
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import select

from app.models.scm_articulos import ScmArticulo
from app.models.scm_auditoria import ScmEvento
from app.models.scm_catalogos import ScmMaterial
from app.models.scm_inventory import (
    ScmLoteAperturaInventario,
    ScmLoteAperturaLinea,
    ScmMovimientoInventario,
    ScmMovimientoMaterialInventario,
    ScmSaldoInventario,
    ScmSaldoMaterialInventario,
    ScmUnidadLogisticaInventario,
    ScmUbicacionInventario,
)
from app.services.scm_inventory_service import (
    _positive_quantity,
    _reserve_operation,
)
from app.services.scm_service_support import (
    ScmServiceError,
    actor_snapshot,
    load_actor,
    reject_unknown_fields,
    required_text,
)


def _today(value):
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as error:
        raise ScmServiceError(
            "INVALID_OPENING_DATE", "fecha_corte debe usar YYYY-MM-DD.",
            status_code=422,
        ) from error


def _opening_payload(item):
    return {
        "id": str(item.id),
        "codigo": item.codigo,
        "fecha_corte": item.fecha_corte.isoformat(),
        "motivo": item.motivo,
        "metodo": item.metodo,
        "estado": item.estado,
        "version": item.version,
        "creado_por_id": item.creado_por_id,
        "enviado_por_id": item.enviado_por_id,
        "resuelto_por_id": item.resuelto_por_id,
        "creado_por": actor_snapshot(item.creado_por),
        "enviado_por": actor_snapshot(item.enviado_por) if item.enviado_por else None,
        "resuelto_por": actor_snapshot(item.resuelto_por) if item.resuelto_por else None,
        "motivo_resolucion": item.motivo_resolucion,
        "creado_at": item.creado_at.isoformat() if item.creado_at else None,
        "enviado_at": item.enviado_at.isoformat() if item.enviado_at else None,
        "resuelto_at": item.resuelto_at.isoformat() if item.resuelto_at else None,
        "total_lineas": len(item.lineas),
        "total_unidades_logisticas": len(item.unidades_logisticas),
        "unidades_logisticas": [{
            "id": str(unit.id),
            "codigo": unit.codigo,
            "qr_value": unit.qr_value,
            "item_tipo": "MATERIAL" if unit.material_scm_id else "ARTICULO",
            "material_scm_id": unit.material_scm_id,
            "articulo_scm_id": unit.articulo_scm_id,
            "articulo_codigo": unit.material.codigo if unit.material_scm_id else unit.articulo.codigo,
            "articulo_nombre": unit.material.nombre if unit.material_scm_id else unit.articulo.nombre,
            "ubicacion_codigo": unit.ubicacion.codigo,
            "peso_bruto_kg": format(Decimal(unit.peso_bruto_kg), "f"),
            "tara_kg": format(Decimal(unit.tara_kg), "f"),
            "peso_neto_kg": format(Decimal(unit.peso_neto_kg), "f"),
            "cantidad_disponible_kg": format(Decimal(unit.cantidad_disponible_kg), "f"),
            "estado_calidad": unit.estado_calidad,
            "estado": unit.estado,
            "station_id": unit.station_id,
            "capturado_por_id": unit.capturado_por_id,
            "created_at": unit.created_at.isoformat() if unit.created_at else None,
        } for unit in item.unidades_logisticas],
        "lineas": [{
            "id": line.id,
            "articulo_scm_id": line.articulo_scm_id,
            "material_scm_id": line.material_scm_id,
            "item_tipo": "MATERIAL" if line.material_scm_id else "ARTICULO",
            "articulo_codigo": (
                line.material.codigo if line.material_scm_id else line.articulo.codigo
            ),
            "articulo_nombre": (
                line.material.nombre if line.material_scm_id else line.articulo.nombre
            ),
            "unidad": (
                line.material.unidad_base if line.material_scm_id
                else line.articulo.unidad_base
            ),
            "ubicacion_codigo": line.ubicacion_codigo,
            "ubicacion_nombre": line.ubicacion_nombre,
            "cantidad": format(Decimal(line.cantidad), "f"),
            "estado_calidad": line.estado_calidad,
            "observacion": line.observacion,
            "movimiento_id": str(
                line.movimiento_material_id or line.movimiento_id
            ) if (line.movimiento_material_id or line.movimiento_id) else None,
        } for line in item.lineas],
    }


def _normalized_lines(session, raw_lines):
    if not isinstance(raw_lines, list) or not raw_lines:
        raise ScmServiceError(
            "OPENING_LINES_REQUIRED", "El lote requiere al menos una linea.",
            status_code=422,
        )
    normalized = []
    seen = set()
    for raw in raw_lines:
        if not isinstance(raw, dict):
            raise ScmServiceError(
                "INVALID_OPENING_LINE", "Cada linea debe ser un objeto.",
                status_code=422,
            )
        reject_unknown_fields(raw, allowed={
            "articulo_scm_id", "material_scm_id", "cantidad", "ubicacion_codigo",
            "ubicacion_nombre", "estado_calidad", "observacion",
        })
        article_id = raw.get("articulo_scm_id")
        material_id = raw.get("material_scm_id")
        if bool(article_id) == bool(material_id):
            raise ScmServiceError(
                "OPENING_ITEM_REQUIRED",
                "Cada linea debe indicar articulo_scm_id o material_scm_id, no ambos.",
                status_code=422,
            )
        item = (
            session.get(ScmArticulo, article_id)
            if article_id else session.get(ScmMaterial, material_id)
        )
        if item is None or not item.activo:
            raise ScmServiceError(
                "ARTICLE_NOT_FOUND", "Una linea usa un articulo inexistente o inactivo.",
                status_code=422,
            )
        location_code = required_text(
            raw.get("ubicacion_codigo"), field="ubicacion_codigo", max_length=40,
        ).upper()
        key = ("ARTICULO" if article_id else "MATERIAL", item.id, location_code)
        if key in seen:
            raise ScmServiceError(
                "DUPLICATE_OPENING_SOURCE",
                "El articulo y ubicacion no pueden repetirse en el mismo lote.",
                status_code=422,
            )
        seen.add(key)
        quality = str(raw.get("estado_calidad") or "LIBERADO").strip().upper()
        if quality not in {"LIBERADO", "PENDIENTE"}:
            raise ScmServiceError(
                "INVALID_OPENING_QUALITY",
                "estado_calidad debe ser LIBERADO o PENDIENTE.",
                status_code=422,
            )
        normalized.append({
            "articulo": item if article_id else None,
            "material": item if material_id else None,
            "cantidad": _positive_quantity(raw.get("cantidad")),
            "ubicacion_codigo": location_code,
            "ubicacion_nombre": required_text(
                raw.get("ubicacion_nombre") or location_code,
                field="ubicacion_nombre", max_length=120,
            ),
            "estado_calidad": quality,
            "observacion": (
                str(raw.get("observacion")).strip()[:500]
                if raw.get("observacion") else None
            ),
        })
    return normalized


def list_inventory_openings(session, *, actor_id):
    load_actor(session, actor_id, capability="INVENTARIO_VER")
    items = session.scalars(
        select(ScmLoteAperturaInventario)
        .order_by(ScmLoteAperturaInventario.creado_at.desc())
    ).unique().all()
    return {"items": [_opening_payload(item) for item in items]}


def get_inventory_opening(session, *, actor_id, opening_id):
    load_actor(session, actor_id, capability="INVENTARIO_VER")
    item = session.get(ScmLoteAperturaInventario, opening_id)
    if item is None:
        raise ScmServiceError("OPENING_NOT_FOUND", "Lote de apertura no encontrado.", status_code=404)
    return _opening_payload(item)


def create_inventory_opening(session, *, actor_id, operation_id, data):
    actor = load_actor(session, actor_id, capability="INVENTARIO_APERTURA_PREPARAR")
    reject_unknown_fields(data, allowed={"fecha_corte", "motivo", "metodo", "lineas"})
    method = str(data.get("metodo") or "CONTEO_FISICO_QR").strip().upper()
    if method not in {"TABULAR_CONTINGENCIA", "CONTEO_FISICO_QR"}:
        raise ScmServiceError(
            "INVALID_OPENING_METHOD",
            "metodo debe ser TABULAR_CONTINGENCIA o CONTEO_FISICO_QR.",
            status_code=422,
        )
    if (
        method == "TABULAR_CONTINGENCIA"
        and "INVENTARIO_APERTURA_CONTINGENCIA" not in actor.capacidades_efectivas
    ):
        raise ScmServiceError(
            "INVENTORY_OPENING_CONTINGENCY_FORBIDDEN",
            "La carga tabular excepcional requiere autorización de Gerencia General.",
            status_code=403,
        )
    lines = (
        _normalized_lines(session, data.get("lineas"))
        if method == "TABULAR_CONTINGENCIA" else []
    )
    command = {
        "fecha_corte": _today(data.get("fecha_corte")).isoformat(),
        "motivo": required_text(data.get("motivo"), field="motivo", max_length=500),
        "metodo": method,
        "lineas": [{
            "articulo_scm_id": line["articulo"].id if line["articulo"] else None,
            "material_scm_id": line["material"].id if line["material"] else None,
            "cantidad": format(line["cantidad"], "f"),
            "ubicacion_codigo": line["ubicacion_codigo"],
            "ubicacion_nombre": line["ubicacion_nombre"],
            "estado_calidad": line["estado_calidad"],
            "observacion": line["observacion"],
        } for line in lines],
    }
    operation, replay = _reserve_operation(
        session, operation_id, "POST /inventario/aperturas", actor, command,
    )
    if replay is not None:
        return replay
    try:
        item = ScmLoteAperturaInventario(
            codigo=f"AI-{command['fecha_corte'].replace('-', '')}-{str(operation_id)[:8].upper()}",
            fecha_corte=date.fromisoformat(command["fecha_corte"]),
            motivo=command["motivo"], creado_por_id=actor.id,
            metodo=command["metodo"],
            create_operation_id=operation_id,
        )
        session.add(item)
        session.flush()
        for line in lines:
            item.lineas.append(ScmLoteAperturaLinea(
                articulo_scm_id=line["articulo"].id if line["articulo"] else None,
                material_scm_id=line["material"].id if line["material"] else None,
                cantidad=line["cantidad"],
                ubicacion_codigo=line["ubicacion_codigo"],
                ubicacion_nombre=line["ubicacion_nombre"],
                estado_calidad=line["estado_calidad"],
                observacion=line["observacion"],
            ))
        session.flush()
        response = _opening_payload(item)
        operation.response_json = copy.deepcopy(response)
        operation.estado_http = 201
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def update_inventory_opening(session, *, actor_id, opening_id, operation_id, data):
    actor = load_actor(session, actor_id, capability="INVENTARIO_APERTURA_PREPARAR")
    reject_unknown_fields(data, allowed={"version", "fecha_corte", "motivo", "lineas"})
    command = copy.deepcopy(data)
    operation, replay = _reserve_operation(
        session, operation_id, f"PUT /inventario/aperturas/{opening_id}", actor, command,
    )
    if replay is not None:
        return replay
    try:
        item = session.scalar(select(ScmLoteAperturaInventario).where(
            ScmLoteAperturaInventario.id == opening_id,
        ).with_for_update())
        if item is None:
            raise ScmServiceError("OPENING_NOT_FOUND", "Lote de apertura no encontrado.", status_code=404)
        if item.estado != "BORRADOR":
            raise ScmServiceError("OPENING_NOT_EDITABLE", "Solo un borrador puede editarse.", status_code=409)
        if item.creado_por_id != actor.id:
            raise ScmServiceError("OPENING_CREATOR_REQUIRED", "Solo el creador edita el borrador.", status_code=403)
        if (
            item.metodo == "TABULAR_CONTINGENCIA"
            and "INVENTARIO_APERTURA_CONTINGENCIA" not in actor.capacidades_efectivas
        ):
            raise ScmServiceError(
                "INVENTORY_OPENING_CONTINGENCY_FORBIDDEN",
                "La carga tabular excepcional requiere autorización de Gerencia General.",
                status_code=403,
            )
        if int(data.get("version") or 0) != item.version:
            raise ScmServiceError("VERSION_CONFLICT", "El lote cambio; actualiza antes de guardar.", status_code=409)
        if item.metodo == "CONTEO_FISICO_QR" and item.unidades_logisticas:
            raise ScmServiceError(
                "PHYSICAL_OPENING_CAPTURED_UNITS_NOT_EDITABLE",
                "Una jornada con pesajes no se edita como tabla; anula y repite la captura.",
                status_code=409,
            )
        lines = _normalized_lines(session, data.get("lineas"))
        item.fecha_corte = _today(data.get("fecha_corte"))
        item.motivo = required_text(data.get("motivo"), field="motivo", max_length=500)
        item.lineas.clear()
        session.flush()
        for line in lines:
            item.lineas.append(ScmLoteAperturaLinea(
                articulo_scm_id=line["articulo"].id if line["articulo"] else None,
                material_scm_id=line["material"].id if line["material"] else None,
                cantidad=line["cantidad"],
                ubicacion_codigo=line["ubicacion_codigo"],
                ubicacion_nombre=line["ubicacion_nombre"],
                estado_calidad=line["estado_calidad"], observacion=line["observacion"],
            ))
        item.version += 1
        session.flush()
        response = _opening_payload(item)
        operation.response_json = copy.deepcopy(response)
        operation.estado_http = 200
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def submit_inventory_opening(session, *, actor_id, opening_id, operation_id, version):
    actor = load_actor(session, actor_id, capability="INVENTARIO_APERTURA_PREPARAR")
    command = {"version": int(version or 0)}
    operation, replay = _reserve_operation(
        session, operation_id, f"POST /inventario/aperturas/{opening_id}/enviar", actor, command,
    )
    if replay is not None:
        return replay
    try:
        item = session.scalar(select(ScmLoteAperturaInventario).where(
            ScmLoteAperturaInventario.id == opening_id,
        ).with_for_update())
        if item is None:
            raise ScmServiceError("OPENING_NOT_FOUND", "Lote de apertura no encontrado.", status_code=404)
        if item.estado != "BORRADOR" or item.version != command["version"]:
            raise ScmServiceError("OPENING_STATE_CONFLICT", "El lote ya no es el borrador esperado.", status_code=409)
        if item.creado_por_id != actor.id:
            raise ScmServiceError("OPENING_CREATOR_REQUIRED", "Solo el creador puede enviarlo.", status_code=403)
        if not item.lineas:
            raise ScmServiceError("OPENING_LINES_REQUIRED", "El lote no tiene lineas.", status_code=422)
        if item.metodo == "CONTEO_FISICO_QR":
            if not item.unidades_logisticas:
                raise ScmServiceError(
                    "PHYSICAL_OPENING_UNITS_REQUIRED",
                    "La apertura física requiere al menos una bolsa pesada e identificada.",
                    status_code=422,
                )
            captured_total = sum(
                (Decimal(unit.peso_neto_kg) for unit in item.unidades_logisticas
                 if unit.estado == "REGISTRADA"), Decimal("0")
            )
            line_total = sum((Decimal(line.cantidad) for line in item.lineas), Decimal("0"))
            if captured_total != line_total:
                raise ScmServiceError(
                    "PHYSICAL_OPENING_EVIDENCE_MISMATCH",
                    "Las líneas no coinciden con los pesajes físicos registrados.",
                    status_code=409,
                )
        item.estado = "PENDIENTE_APROBACION"
        item.enviado_por_id = actor.id
        item.enviado_at = datetime.now(timezone.utc)
        item.version += 1
        response = _opening_payload(item)
        operation.response_json = copy.deepcopy(response)
        operation.estado_http = 200
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def resolve_inventory_opening(session, *, actor_id, opening_id, operation_id, data):
    actor = load_actor(session, actor_id, capability="INVENTARIO_APERTURA_APROBAR")
    reject_unknown_fields(data, allowed={"version", "decision", "motivo_resolucion"})
    decision = str(data.get("decision") or "").strip().upper()
    if decision not in {"APROBAR", "RECHAZAR"}:
        raise ScmServiceError("INVALID_OPENING_DECISION", "decision debe ser APROBAR o RECHAZAR.", status_code=422)
    reason = required_text(
        data.get("motivo_resolucion"), field="motivo_resolucion", max_length=500,
    )
    command = {"version": int(data.get("version") or 0), "decision": decision, "motivo_resolucion": reason}
    operation, replay = _reserve_operation(
        session, operation_id, f"POST /inventario/aperturas/{opening_id}/resolver", actor, command,
    )
    if replay is not None:
        return replay
    try:
        item = session.scalar(select(ScmLoteAperturaInventario).where(
            ScmLoteAperturaInventario.id == opening_id,
        ).with_for_update())
        if item is None:
            raise ScmServiceError("OPENING_NOT_FOUND", "Lote de apertura no encontrado.", status_code=404)
        if item.estado != "PENDIENTE_APROBACION" or item.version != command["version"]:
            raise ScmServiceError("OPENING_STATE_CONFLICT", "El lote ya no esta pendiente en esa version.", status_code=409)
        if item.creado_por_id == actor.id:
            raise ScmServiceError("OPENING_SELF_APPROVAL_FORBIDDEN", "El creador no puede resolver su lote.", status_code=409)
        now = datetime.now(timezone.utc)
        item.resuelto_por_id = actor.id
        item.resuelto_at = now
        item.motivo_resolucion = reason
        item.approval_operation_id = operation_id
        if decision == "RECHAZAR":
            item.estado = "RECHAZADO"
        else:
            for line in item.lineas:
                is_material = line.material_scm_id is not None
                target = line.material if is_material else line.articulo
                source_filter = (
                    ScmLoteAperturaLinea.material_scm_id == line.material_scm_id
                    if is_material else
                    ScmLoteAperturaLinea.articulo_scm_id == line.articulo_scm_id
                )
                previous = session.scalar(
                    select(ScmLoteAperturaLinea.id)
                    .join(ScmLoteAperturaInventario)
                    .where(
                        ScmLoteAperturaInventario.estado == "APLICADO",
                        source_filter,
                        ScmLoteAperturaLinea.ubicacion_codigo == line.ubicacion_codigo,
                    ).limit(1)
                )
                if previous is not None:
                    raise ScmServiceError(
                        "OPENING_ALREADY_APPLIED",
                        f"{target.codigo} ya tiene una apertura aplicada en {line.ubicacion_codigo}.", status_code=409,
                    )
                location = session.scalar(select(ScmUbicacionInventario).where(
                    ScmUbicacionInventario.codigo == line.ubicacion_codigo,
                ).with_for_update())
                if location is None:
                    location = ScmUbicacionInventario(
                        codigo=line.ubicacion_codigo, nombre=line.ubicacion_nombre,
                    )
                    session.add(location)
                    session.flush()
                balance_class = (
                    ScmSaldoMaterialInventario if is_material else ScmSaldoInventario
                )
                identity_column = (
                    ScmSaldoMaterialInventario.material_id if is_material
                    else ScmSaldoInventario.articulo_scm_id
                )
                identity_value = line.material_scm_id if is_material else line.articulo_scm_id
                balance = session.scalar(select(balance_class).where(
                    identity_column == identity_value,
                    balance_class.ubicacion_id == location.id,
                ).with_for_update())
                if balance is None:
                    balance = (
                        ScmSaldoMaterialInventario(
                            material_id=identity_value, ubicacion_id=location.id,
                        ) if is_material else ScmSaldoInventario(
                            articulo_scm_id=identity_value, ubicacion_id=location.id,
                        )
                    )
                    session.add(balance)
                    session.flush()
                physical = (
                    balance.cantidad_fisica_kg if is_material else balance.cantidad_fisica
                )
                if Decimal(physical) != 0:
                    raise ScmServiceError(
                        "OPENING_BALANCE_NOT_ZERO",
                        f"{target.codigo} ya tiene existencia en {line.ubicacion_codigo}.", status_code=409,
                    )
                quantity = Decimal(line.cantidad)
                if is_material:
                    balance.cantidad_fisica_kg = quantity
                    if line.estado_calidad == "PENDIENTE":
                        balance.cantidad_no_disponible_kg = quantity
                else:
                    balance.cantidad_fisica = quantity
                    if line.estado_calidad == "PENDIENTE":
                        balance.cantidad_no_disponible = quantity
                balance.version += 1
                movement_kwargs = dict(
                    saldo=balance, tipo="SALDO_INICIAL",
                    motivo=f"{item.codigo}: {item.motivo}",
                    referencia_tipo="LOTE_APERTURA", referencia_id=str(item.id),
                    actor_id=actor.id,
                    operation_id=uuid5(NAMESPACE_URL, f"{operation_id}:{line.id}"),
                )
                movement = (
                    ScmMovimientoMaterialInventario(
                        cantidad_delta_kg=quantity,
                        saldo_fisico_resultante_kg=quantity,
                        **movement_kwargs,
                    ) if is_material else ScmMovimientoInventario(
                        cantidad_delta=quantity,
                        saldo_fisico_resultante=quantity,
                        **movement_kwargs,
                    )
                )
                session.add(movement)
                session.flush()
                if is_material:
                    line.movimiento_material_id = movement.id
                else:
                    line.movimiento_id = movement.id
            item.estado = "APLICADO"
            for unit in item.unidades_logisticas:
                unit.estado = (
                    "DISPONIBLE" if unit.estado_calidad == "LIBERADO" else "BLOQUEADA"
                )
        item.version += 1
        response = _opening_payload(item)
        operation.response_json = copy.deepcopy(response)
        operation.estado_http = 200
        session.add(ScmEvento(
            aggregate_type="LOTE_APERTURA_INVENTARIO", aggregate_id=str(item.id),
            tipo=f"OPENING_{item.estado}", actor_id=actor.id,
            actor_snapshot=actor_snapshot(actor), after_json=response,
            operation_id=operation_id,
        ))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def capture_physical_opening_unit(
    session, *, station_id, operation_id, data,
):
    """Registra una bolsa física; la cantidad de la línea siempre se deriva del NET."""
    reject_unknown_fields(data, allowed={
        "opening_id", "material_scm_id", "articulo_scm_id", "ubicacion_codigo",
        "peso_bruto_kg", "tara_kg", "estado_calidad", "pesado_por_id",
        "reading_stable",
    })
    try:
        try:
            opening_id = UUID(str(data.get("opening_id")))
        except (TypeError, ValueError, AttributeError) as error:
            raise ScmServiceError(
                "INVALID_OPENING_ID", "opening_id debe ser un UUID válido.", status_code=422,
            ) from error
        opening = session.scalar(
            select(ScmLoteAperturaInventario)
            .where(ScmLoteAperturaInventario.id == opening_id)
            .with_for_update(of=ScmLoteAperturaInventario)
        )
        if opening is None:
            raise ScmServiceError("OPENING_NOT_FOUND", "Lote de apertura no encontrado.", status_code=404)
        if opening.metodo != "CONTEO_FISICO_QR" or opening.estado != "BORRADOR":
            raise ScmServiceError(
                "PHYSICAL_OPENING_NOT_CAPTURABLE",
                "Solo una apertura física en borrador admite pesajes.", status_code=409,
            )
        if data.get("reading_stable") is not True:
            raise ScmServiceError(
                "SCALE_READING_UNSTABLE", "La captura requiere una lectura estable.", status_code=422,
            )
        actor = load_actor(session, data.get("pesado_por_id"))
        article_id = data.get("articulo_scm_id")
        material_id = data.get("material_scm_id")
        if bool(article_id) == bool(material_id):
            raise ScmServiceError(
                "OPENING_ITEM_REQUIRED",
                "Indica material_scm_id o articulo_scm_id, no ambos.", status_code=422,
            )
        if article_id:
            raise ScmServiceError(
                "PHYSICAL_ARTICLE_COUNT_NOT_IMPLEMENTED",
                "El corte actual pesa materiales en KG; piezas y PT requieren conteo físico por UN.",
                status_code=422,
            )
        material = session.get(ScmMaterial, material_id)
        if material is None or not material.activo or material.unidad_base != "KG":
            raise ScmServiceError(
                "MATERIAL_NOT_FOUND", "El material KG no existe o está inactivo.", status_code=422,
            )
        location_code = required_text(
            data.get("ubicacion_codigo"), field="ubicacion_codigo", max_length=40,
        ).upper()
        location = session.scalar(
            select(ScmUbicacionInventario)
            .where(ScmUbicacionInventario.codigo == location_code)
            .with_for_update(of=ScmUbicacionInventario)
        )
        if location is None or not location.activo or location.almacen_id is None:
            raise ScmServiceError(
                "CANONICAL_LOCATION_REQUIRED",
                "La captura física exige una ubicación canónica de almacén.", status_code=422,
            )
        try:
            gross = Decimal(str(data.get("peso_bruto_kg"))).quantize(Decimal("0.001"))
            tare = Decimal(str(data.get("tara_kg"))).quantize(Decimal("0.001"))
        except Exception as error:
            raise ScmServiceError("INVALID_WEIGHT", "Los pesos deben ser numéricos.", status_code=422) from error
        net = gross - tare
        if gross <= 0 or tare < 0 or net <= 0:
            raise ScmServiceError("INVALID_WEIGHT", "El peso NET debe ser mayor que cero.", status_code=422)
        quality = str(data.get("estado_calidad") or "PENDIENTE").strip().upper()
        if quality not in {"LIBERADO", "PENDIENTE"}:
            raise ScmServiceError("INVALID_OPENING_QUALITY", "Calidad inválida.", status_code=422)
        prior = session.scalar(
            select(ScmUnidadLogisticaInventario).where(
                ScmUnidadLogisticaInventario.capture_operation_id == operation_id
            )
        )
        if prior is not None:
            return _opening_payload(opening)
        line = next((value for value in opening.lineas if (
            value.material_scm_id == material.id and value.ubicacion_codigo == location_code
            and value.estado_calidad == quality
        )), None)
        if line is None:
            line = ScmLoteAperturaLinea(
                lote=opening, material_scm_id=material.id,
                ubicacion_codigo=location.codigo, ubicacion_nombre=location.nombre,
                cantidad=net, estado_calidad=quality,
                observacion="Cantidad derivada de unidades logísticas pesadas.",
            )
            session.add(line)
            session.flush()
        else:
            line.cantidad = Decimal(line.cantidad) + net
        unit_id = uuid4()
        unit = ScmUnidadLogisticaInventario(
            id=unit_id, codigo=f"UL-{str(unit_id)[:8].upper()}",
            qr_value=f"SCM:UL:{unit_id}", lote_apertura=opening,
            apertura_linea=line, material_scm_id=material.id,
            ubicacion_id=location.id, peso_bruto_kg=gross, tara_kg=tare,
            peso_neto_kg=net, cantidad_disponible_kg=net,
            estado_calidad=quality, station_id=station_id,
            capturado_por_id=actor.id, capture_operation_id=operation_id,
            reading_stable=True,
        )
        session.add(unit)
        opening.version += 1
        session.commit()
        return _opening_payload(opening)
    except Exception:
        session.rollback()
        raise


def resolve_physical_opening_target(session, *, data):
    """Valida el contexto de apertura sin registrar peso ni movimiento."""
    try:
        opening_id = UUID(str(data.get("opening_id")))
    except (TypeError, ValueError, AttributeError) as error:
        raise ScmServiceError(
            "INVALID_OPENING_ID", "El contexto no contiene una apertura válida.", status_code=422,
        ) from error
    opening = session.get(ScmLoteAperturaInventario, opening_id)
    if opening is None:
        raise ScmServiceError("OPENING_NOT_FOUND", "Lote de apertura no encontrado.", status_code=404)
    if opening.metodo != "CONTEO_FISICO_QR" or opening.estado != "BORRADOR":
        raise ScmServiceError(
            "PHYSICAL_OPENING_NOT_CAPTURABLE",
            "Esta jornada ya no admite pesajes físicos.", status_code=409,
        )
    material = session.get(ScmMaterial, data.get("material_scm_id"))
    if material is None or not material.activo or material.unidad_base != "KG":
        raise ScmServiceError(
            "MATERIAL_NOT_FOUND", "El material KG no existe o está inactivo.", status_code=422,
        )
    location_code = required_text(
        data.get("ubicacion_codigo"), field="ubicacion_codigo", max_length=40,
    ).upper()
    location = session.scalar(select(ScmUbicacionInventario).where(
        ScmUbicacionInventario.codigo == location_code
    ))
    if location is None or not location.activo or location.almacen_id is None:
        raise ScmServiceError(
            "CANONICAL_LOCATION_REQUIRED",
            "La ubicación no es una ubicación activa de almacén.", status_code=422,
        )
    quality = str(data.get("estado_calidad") or "PENDIENTE").strip().upper()
    if quality not in {"LIBERADO", "PENDIENTE"}:
        raise ScmServiceError("INVALID_OPENING_QUALITY", "Calidad inválida.", status_code=422)
    captured_units = [unit for unit in opening.unidades_logisticas if (
        unit.material_scm_id == material.id
        and unit.ubicacion_id == location.id
        and unit.estado_calidad == quality
    )]
    return {
        "opening_id": str(opening.id),
        "opening_codigo": opening.codigo,
        "opening_estado": opening.estado,
        "opening_version": opening.version,
        "total_unidades_logisticas": len(captured_units),
        "unidades_logisticas": [{
            "id": str(unit.id),
            "codigo": unit.codigo,
            "qr_value": unit.qr_value,
            "articulo_codigo": material.codigo,
            "articulo_nombre": material.nombre,
            "ubicacion_codigo": location.codigo,
            "peso_bruto_kg": format(Decimal(unit.peso_bruto_kg), "f"),
            "tara_kg": format(Decimal(unit.tara_kg), "f"),
            "peso_neto_kg": format(Decimal(unit.peso_neto_kg), "f"),
            "estado_calidad": unit.estado_calidad,
            "estado": unit.estado,
            "created_at": unit.created_at.isoformat() if unit.created_at else None,
        } for unit in captured_units],
        "material_scm_id": material.id,
        "material_codigo": material.codigo,
        "material_nombre": material.nombre,
        "unidad_medida": material.unidad_base,
        "ubicacion_codigo": location.codigo,
        "ubicacion_nombre": location.nombre,
        "estado_calidad": quality,
    }

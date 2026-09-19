"""KG production evidence and document closure.

This module deliberately keeps physical kg facts separate from the legacy UN
projections.  It does not create inventory movements, consume BOM sources, or
turn an estimate into a measured result.
"""

from decimal import Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.models.registro import RegistroDiarioProduccion
from app.models.scm_ot import (
    ScmAtribucionProduccionKg,
    ScmCierreProductivoKg,
    ScmControlPesoManga,
    ScmManga,
    ScmPesajeManga,
    ScmTramoMangaTrabajo,
    ScmTrabajoOt,
    utc_now,
)
from app.models.scm_production_orders import ScmOrdenOperacion
from app.services.scm_ot_service import (
    _complete_operation,
    _event,
    _reserve_operation,
    _serialize_manga,
)
from app.services.scm_service_support import (
    ScmServiceError,
    load_actor,
    reject_unknown_fields,
)


KG_QUANTUM = Decimal("0.001")
REFERENCE_KG_QUANTUM = Decimal("0.000001")


def automatic_kg_intake_enabled():
    """Return the explicit W1 pilot switch without enabling it implicitly."""
    try:
        from flask import current_app
        return bool(current_app.config.get("KG_AUTOMATIC_INTAKE_ENABLED", False))
    except RuntimeError:
        return False


def _production_location(session, *, article_class=None):
    from flask import current_app
    from app.models.scm_inventory import ScmUbicacionInventario
    code = str(current_app.config.get("KG_PRODUCTION_LOCATION_CODE") or "").strip().upper()
    if not code:
        raise ScmServiceError(
            "KG_PRODUCTION_LOCATION_REQUIRED",
            "El modo automático requiere una ubicación de Producción explícita.",
            status_code=409,
        )
    location = session.scalar(
        select(ScmUbicacionInventario)
        .where(ScmUbicacionInventario.codigo == code)
        .with_for_update()
    )
    allowed_classes = set(location.clases_articulo_json or []) if location is not None else set()
    if (
        location is None
        or not location.activo
        or location.tipo != "PUNTO_PRODUCCION"
        or not location.permite_saldo_libre
        or (allowed_classes and article_class not in allowed_classes)
    ):
        raise ScmServiceError(
            "KG_PRODUCTION_LOCATION_INVALID",
            "La ubicación de Producción debe ser un punto activo, admitir saldo libre y la clase KG.",
            status_code=409,
            details={"ubicacion_codigo": code},
        )
    return location


def sync_kg_production_inventory(
    session,
    *,
    actor_id,
    manga,
    net_kg,
    operation_id,
    source_type,
    source_id=None,
    source_at=None,
    correction_id=None,
    final=False,
):
    """Project one cumulative measured net into the KG ledger.

    Controls and final weighing call this with the cumulative NET.  The ledger
    movement is only the delta, so replaying a station command cannot duplicate
    mass.  Receipt later changes the location of this same existence.
    """
    if not automatic_kg_intake_enabled():
        return None
    from hashlib import sha256
    from app.models.scm_articulos import ScmArticulo
    from app.models.scm_inventory import ScmUbicacionInventario
    from app.models.scm_inventory_kg import (
        ScmExistenciaMangaKg,
        ScmMovimientoInventarioKg,
        ScmSaldoInventarioKg,
    )
    article = session.scalar(
        select(ScmArticulo).where(ScmArticulo.id == manga.lote_articulo.articulo.id).with_for_update()
    )
    _kg_article(manga)
    location = _production_location(session, article_class=article.clase)
    total = _kg(net_kg, "net_kg")
    source_token = f"{source_type}:{source_id or manga.id}:{total}:{correction_id or ''}"
    projection_hash = sha256(source_token.encode()).hexdigest()
    balance = session.scalar(
        select(ScmSaldoInventarioKg)
        .where(
            ScmSaldoInventarioKg.articulo_scm_id == article.id,
            ScmSaldoInventarioKg.ubicacion_id == location.id,
        )
        .with_for_update()
    )
    if balance is None:
        candidate_balance = ScmSaldoInventarioKg(
            articulo_scm_id=article.id,
            ubicacion_id=location.id,
            cantidad_fisica_kg=Decimal("0"),
            cantidad_reservada_kg=Decimal("0"),
            cantidad_no_disponible_kg=Decimal("0"),
            atributo_proceso="PROCESO",
        )
        try:
            with session.begin_nested():
                session.add(candidate_balance)
                session.flush()
            balance = candidate_balance
        except IntegrityError:
            # A concurrent first event may have won the article/location
            # unique key.  Re-read its locked row and continue the same
            # transaction instead of creating a second balance.
            balance = session.scalar(
                select(ScmSaldoInventarioKg)
                .where(
                    ScmSaldoInventarioKg.articulo_scm_id == article.id,
                    ScmSaldoInventarioKg.ubicacion_id == location.id,
                )
                .with_for_update()
            )
            if balance is None:
                raise
    existence = session.scalar(
        select(ScmExistenciaMangaKg)
        .where(
            ScmExistenciaMangaKg.manga_id == manga.id,
            ScmExistenciaMangaKg.estado_logistico != "REVERSADA",
        )
        .with_for_update()
    )
    previous = Decimal(existence.cantidad_fisica_kg) if existence is not None else Decimal("0")
    delta = (total - previous).quantize(KG_QUANTUM)
    if delta < 0 and not correction_id:
        raise ScmServiceError(
            "KG_PRODUCTION_NET_DECREASE",
            "El NET acumulado de Producción no puede disminuir sin una corrección autorizada.",
            status_code=409,
        )
    if delta:
        resulting = (Decimal(balance.cantidad_fisica_kg) + delta).quantize(KG_QUANTUM)
        if resulting < 0 or Decimal(balance.cantidad_reservada_kg) + Decimal(balance.cantidad_no_disponible_kg) > resulting:
            raise ScmServiceError(
                "KG_PRODUCTION_CORRECTION_CONFLICT",
                "La corrección dejaría un saldo KG negativo o comprometido.",
                status_code=409,
            )
        balance.cantidad_fisica_kg = resulting
        balance.version += 1
        movement = ScmMovimientoInventarioKg(
            saldo=balance,
            tipo="INGRESO_PRODUCCION" if delta > 0 else "AJUSTE_NEGATIVO",
            cantidad_delta_kg=delta,
            saldo_fisico_resultante_kg=resulting,
            motivo=f"Ingreso automático desde {source_type} de manga {manga.codigo}",
            referencia_tipo="PESAJE_MANGA" if source_type == "PESAJE_FINAL" else "CONTROL_PESO_MANGA",
            referencia_id=str(source_id or manga.public_id),
            actor_id=actor_id,
            operation_id=operation_id,
            pesaje_public_id=source_id if source_type == "PESAJE_FINAL" else None,
            correccion_aplicada_public_id=correction_id,
            projection_sha256=projection_hash,
            peso_neto_snapshot_kg=total,
            pesada_at_snapshot=source_at or utc_now(),
            fuente_tipo="KG_AUTO_PRODUCCION",
            atributo_proceso="TERMINADA" if final else "PROCESO",
        )
        session.add(movement)
        session.flush()
    else:
        movement = None
    if existence is None:
        existence = ScmExistenciaMangaKg(
            manga_id=manga.id,
            articulo_scm_id=article.id,
            saldo_id=balance.id,
            ubicacion_id=location.id,
            movimiento_ingreso_id=movement.id if movement else None,
            operation_id=operation_id,
            resuelta_por="KG_AUTO_PRODUCCION",
            estado_logistico="DISPONIBLE_PRODUCCION" if final else "EN_PRODUCCION",
            estado_calidad="SIN_CONTROL",
            atributo_proceso="TERMINADA" if final else "PROCESO",
            cantidad_fisica_kg=total,
            peso_neto_snapshot_kg=total,
            pesaje_public_id=source_id if source_type == "PESAJE_FINAL" else None,
            correccion_aplicada_public_id=correction_id,
            projection_sha256=projection_hash,
            pesada_at_snapshot=source_at or utc_now(),
            recibida_por_id=actor_id,
            origen_tipo="PRODUCCION",
        )
        session.add(existence)
        session.flush()
        from app.services.scm_kg_receipt_service import _kg_identity_for_receipt
        unit, label = _kg_identity_for_receipt(
            session, existence=existence, article=article,
            location=location, quantity=total,
        )
        unit.estado_logistico = "DISPONIBLE_PRODUCCION" if final else "EN_PRODUCCION"
        unit.estado_calidad = "SIN_CONTROL"
        unit.atributo_proceso = "TERMINADA" if final else "PROCESO"
        unit.kg_entregado = total
        unit.kg_verificados = total
        existence.unidad_fisica_kg_id = unit.id
    else:
        existence.cantidad_fisica_kg = total
        existence.peso_neto_snapshot_kg = total
        existence.pesaje_public_id = source_id if source_type == "PESAJE_FINAL" else existence.pesaje_public_id
        existence.correccion_aplicada_public_id = correction_id
        existence.projection_sha256 = projection_hash
        existence.pesada_at_snapshot = source_at or existence.pesada_at_snapshot or utc_now()
        existence.estado_logistico = "DISPONIBLE_PRODUCCION" if final else "EN_PRODUCCION"
        existence.estado_calidad = "SIN_CONTROL"
        existence.atributo_proceso = "TERMINADA" if final else "PROCESO"
        existence.version += 1
        unit = existence.unidad_fisica_kg
        label = None
        if unit is not None:
            unit.estado_logistico = "DISPONIBLE_PRODUCCION" if final else "EN_PRODUCCION"
            unit.estado_calidad = "SIN_CONTROL"
            unit.atributo_proceso = "TERMINADA" if final else "PROCESO"
            unit.kg_entregado = total
            unit.kg_verificados = total
            unit.kg_verificados_at = source_at or utc_now()
            unit.version += 1
    if final:
        balance.atributo_proceso = "TERMINADA"
    elif balance.atributo_proceso == "TERMINADA":
        balance.atributo_proceso = "MIXTA"
    session.flush()
    return {
        "existencia": existence,
        "unidad": unit,
        "movimiento": movement,
        "delta_kg": format(delta, "f"),
        "ubicacion_codigo": location.codigo,
        "atributo_proceso": "TERMINADA" if final else "PROCESO",
    }


def close_kg_from_last_control(
    session, *, actor_id, manga_id, operation_id, data=None
):
    """Close a KG manga from its last control without fabricating a weighing.

    This is an explicit administrative closure: the last stable control is the
    authoritative measured NET and the command records its source and reason.
    It is only valid while the manga remains in EN_LLENADO and never accepts a
    lower/equal replacement control.
    """
    command = dict(data or {})
    reject_unknown_fields(command, allowed={"motivo", "version"})
    actor = load_actor(session, actor_id, capability="MANGA_FINALIZAR_PARCIAL")
    reason = str(command.get("motivo") or "").strip()
    if not reason:
        raise ScmServiceError("REASON_REQUIRED", "El cierre desde control requiere motivo.", status_code=422)
    raw_version = command.get("version")
    valid_integer_version = (
        isinstance(raw_version, int) and not isinstance(raw_version, bool)
    ) or (
        isinstance(raw_version, str)
        and raw_version.strip().lstrip("-").isdigit()
    )
    if not valid_integer_version:
        raise ScmServiceError(
            "INVALID_VERSION", "version es obligatorio y debe ser entero.", status_code=422
        )
    requested_version = int(raw_version)
    command["version"] = requested_version
    manga = session.scalar(select(ScmManga).where(ScmManga.public_id == manga_id).with_for_update())
    if manga is None:
        raise ScmServiceError("MANGA_NOT_FOUND", "La manga no existe.", status_code=404)
    operation, replay = _reserve_operation(
        session, operation_id, f"/kg/mangas/{manga_id}/cierre-desde-control", actor,
        {"manga_id": str(manga_id), "motivo": reason, "version": requested_version},
    )
    if replay is not None:
        return replay
    _kg_article(manga)
    if manga.estado != "EN_LLENADO":
        raise ScmServiceError("MANGA_CLOSE_FROM_CONTROL_NOT_ALLOWED", "La manga no está en llenado.", status_code=409)
    latest = session.scalar(
        select(ScmControlPesoManga)
        .where(ScmControlPesoManga.manga_id == manga.id)
        .order_by(ScmControlPesoManga.pesado_at.desc(), ScmControlPesoManga.id.desc())
        .with_for_update()
    )
    if latest is None or latest.unidad_evidencia != "KG":
        raise ScmServiceError("KG_CONTROL_REQUIRED", "No existe un control KG vigente para cerrar.", status_code=409)
    if requested_version != manga.version:
        raise ScmServiceError("VERSION_CONFLICT", "La manga cambió desde la última lectura.", status_code=409)
    segments = session.scalars(
        select(ScmTramoMangaTrabajo).where(ScmTramoMangaTrabajo.manga_id == manga.id).order_by(ScmTramoMangaTrabajo.secuencia).with_for_update()
    ).all()
    active = next((item for item in reversed(segments) if item.estado == "ACTIVO"), None)
    if active is None:
        raise ScmServiceError("KG_CONTROL_SEGMENT_REQUIRED", "No existe tramo activo para cerrar.", status_code=409)
    final_net = Decimal(latest.peso_neto_kg).quantize(KG_QUANTUM)
    start = Decimal(active.cantidad_inicio_kg or 0).quantize(KG_QUANTUM)
    if final_net <= start:
        raise ScmServiceError("CONTROL_WEIGHT_NOT_MONOTONIC", "El último control no supera el inicio del tramo.", status_code=409)
    active.cantidad_fin_kg = final_net
    active.cantidad_atribuida_kg = (final_net - start).quantize(KG_QUANTUM)
    active.calidad_evidencia_kg = "MEDIDA_DIRECTA_CIERRE_CONTROL"
    active.estado = "CERRADO"
    active.cerrada_at = latest.pesado_at
    active.motivo_cierre = reason
    manga.estado = "PENDIENTE_RECEPCION_ALMACEN"
    manga.version += 1
    inventory = sync_kg_production_inventory(
        session, actor_id=actor.id, manga=manga, net_kg=final_net,
        operation_id=operation.operation_id, source_type="CONTROL_CIERRE",
        source_id=latest.public_id, source_at=latest.pesado_at, final=True,
    )
    response = {
        "manga": _serialize_manga(manga),
        "control_fuente": latest.to_dict(),
        "cierre": {"tipo": "DESDE_ULTIMO_CONTROL", "motivo": reason, "simula_pesaje": False},
        "inventario_creado": bool(inventory),
        "inventario_kg": {"delta_kg": inventory["delta_kg"], "ubicacion_codigo": inventory["ubicacion_codigo"], "atributo_proceso": inventory["atributo_proceso"]} if inventory else None,
        "operation_id": str(operation.operation_id),
    }
    session.add(_event("MANGA", manga.id, "KG_MANGA_CLOSED_FROM_LAST_CONTROL", actor, operation, response))
    _complete_operation(operation, response)
    session.commit()
    return response


def _kg_reference_snapshot(session, article, *, preferred_structure=None, visited=None):
    """Return a deterministic reference weight plus its revision trace."""
    from app.services.scm_assembly_execution_service import _master_weight_g
    direct = _master_weight_g(article)
    if direct is not None:
        return direct, {
            "tipo": "MAESTRO_DIRECTO",
            "articulo_id": article.id,
            "articulo_version": article.version,
        }
    visited = set(visited or ())
    if article.id in visited:
        return None, None
    visited.add(article.id)
    from app.models.scm_estructuras import ScmEstructuraRevision
    structure = preferred_structure
    if structure is None or structure.articulo_resultado_id != article.id:
        structure = session.scalar(
            select(ScmEstructuraRevision)
            .where(
                ScmEstructuraRevision.articulo_resultado_id == article.id,
                ScmEstructuraRevision.estado == "APROBADA",
            )
            .order_by(
                ScmEstructuraRevision.numero_revision.desc(),
                ScmEstructuraRevision.id.desc(),
            )
        )
    if structure is None or not structure.content_hash:
        return None, None
    total = Decimal("0")
    trace_components = []
    for component in sorted(
        structure.componentes,
        key=lambda item: (item.secuencia, item.articulo_componente_id),
    ):
        weight, trace = _kg_reference_snapshot(
            session,
            component.articulo_componente,
            visited=visited,
        )
        if weight is None or trace is None:
            return None, None
        total += weight * Decimal(component.cantidad)
        trace_components.append({
            "articulo_id": component.articulo_componente_id,
            "cantidad": str(component.cantidad),
            "referencia": trace,
        })
    if total <= 0:
        return None, None
    return total.quantize(Decimal("0.0001")), {
        "tipo": "SUBBOM_APROBADA",
        "revision_id": structure.id,
        "revision_numero": structure.numero_revision,
        "content_hash": structure.content_hash,
        "componentes": trace_components,
    }


def _kg(value, field):
    try:
        amount = Decimal(str(value)).quantize(KG_QUANTUM)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ScmServiceError(
            "INVALID_KG", f"{field} debe expresarse en kg.", status_code=422
        ) from error
    if not amount.is_finite() or amount < 0:
        raise ScmServiceError(
            "INVALID_KG", f"{field} no puede ser negativo.", status_code=422
        )
    return amount


def _kg_article(manga):
    article = (
        manga.lote_articulo.articulo
        if manga.lote_articulo is not None else None
    )
    if article is None or article.unidad_inventario != "KG":
        raise ScmServiceError(
            "KG_ARTICLE_REQUIRED",
            "La evidencia de producción KG requiere un artículo opt-in KG.",
            status_code=409,
        )
    return article


def _resolve_frozen_bom_basis(session, manga, command=None):
    """Resolve the approved OA structure; never trust a station BOM payload.

    Fresh versus previous is derived from the frozen concurrent OA source.
    If the source cannot be resolved, measured NET remains pending rather than
    accepting proportions or component IDs supplied by the weighing client.
    """
    # A color manga owns ``trabajo``.  An OA output manga intentionally does
    # not: its frozen BOM is reached through the assembly OT's operation.
    work = manga.trabajo
    order = (
        work.orden_operacion
        if work is not None
        else getattr(manga.ot, "orden_operacion", None)
    )
    route = order.operacion_ruta_revision if order is not None else None
    structure = route.estructura_revision if route is not None else None
    if structure is None or structure.estado != "APROBADA" or not structure.content_hash:
        return None
    from app.services.scm_assembly_execution_service import (
        _inline_source_for_ot,
    )

    fresh_component_id = None
    source_work = None
    try:
        inline = _inline_source_for_ot(
            session, ot=manga.ot, structure=structure, lock=False
        )
        if inline is not None:
            source_work, source_component, source_output = inline
            fresh_component_id = source_component.articulo_componente_id
    except ScmServiceError:
        # Without a uniquely resolvable concurrent source, retain measured
        # evidence and leave BOM attribution pending.
        fresh_component_id = None
    if fresh_component_id is None:
        return None

    components = []
    # The approved revision is the authority, but the reference weights are
    # read exactly once at weighing time and persisted in the evidence base.
    # Stable ordering keeps the hash/audit payload deterministic.
    for component in sorted(
        structure.componentes,
        key=lambda item: (item.secuencia, item.articulo_componente_id),
    ):
        weight_g, weight_trace = _kg_reference_snapshot(
            session,
            component.articulo_componente,
        )
        if weight_g is None or weight_trace is None:
            return None
        is_fresh = component.articulo_componente_id == fresh_component_id
        components.append({
            "articulo_id": component.articulo_componente_id,
            "articulo_version": component.articulo_componente.version,
            "cantidad": str(component.cantidad),
            "peso_referencia_kg": format(Decimal(weight_g) / 1000, "f"),
            "origen": "FRESCO" if is_fresh else "STOCK_PREVIO",
            "referencia": weight_trace,
        })
    return {
        "revision": str(structure.id),
        "revision_numero": structure.numero_revision,
        "content_hash": structure.content_hash,
        "referencias_capturadas_at": utc_now().isoformat(),
        "referencia_peso_fuente": "MAESTRO_VIGENTE_AL_PESAJE",
        # An OA output manga has no TrabajoColor owner of its own.  Preserve
        # the inline source identity in the frozen evidence so the later OF
        # / OT closure can project this estimate exactly once onto its owner.
        "source_trabajo_ot_id": str(source_work.id) if source_work is not None else None,
        "source_orden_operacion_id": (
            str(source_work.orden_operacion_id)
            if source_work is not None and source_work.orden_operacion_id is not None
            else None
        ),
        "source_orden_trabajo_id": (
            source_work.orden_trabajo_id if source_work is not None else None
        ),
        "componentes": components,
    }


def preview_kg_attribution(*, net_kg, bom_basis=None):
    """Calculate a mass-proportion estimate without creating stock facts.

    ``bom_basis`` is an already frozen, caller-provided snapshot.  Each item
    must provide quantity, reference weight in kg, and an explicit source
    (``FRESH`` or ``PREVIOUS``).  Missing or zero bases stay pending instead
    of being converted into a fabricated result.
    """
    net = _kg(net_kg, "net_kg")
    if not bom_basis:
        return {
            "estado": "PENDIENTE_BOM",
            "kg_medido": format(net, "f"),
            "kg_fabricacion_estimado": None,
            "kg_previo_estimado": None,
            "base": None,
        }
    components = bom_basis.get("componentes", bom_basis.get("components")) if isinstance(bom_basis, dict) else None
    if not isinstance(components, list) or not components:
        raise ScmServiceError(
            "KG_BOM_ATTRIBUTION_PENDING",
            "La base BOM debe conservar componentes congelados.",
            status_code=409,
        )
    previous_mass = Decimal("0")
    total_mass = Decimal("0")
    normalized = []
    for index, item in enumerate(components):
        if not isinstance(item, dict):
            raise ScmServiceError("KG_BOM_ATTRIBUTION_PENDING", "La base BOM contiene un componente inválido.", status_code=409)
        quantity_raw = item.get("cantidad", item.get("quantity"))
        weight_raw = item.get("peso_referencia_kg", item.get("reference_weight_kg"))
        source = str(item.get("origen", item.get("source", ""))).upper()
        if source in {"STOCK_PREVIO", "COMPONENTE_PREVIO", "PREVIOUS", "PREVIO"}:
            source = "PREVIOUS"
        elif source in {"FRESCO", "FABRICACION_NUEVA", "FRESH", "FABRICADO"}:
            source = "FRESH"
        else:
            raise ScmServiceError(
                "KG_BOM_ATTRIBUTION_PENDING",
                f"El componente BOM {index + 1} no identifica si es fresco o previo.",
                status_code=409,
            )
        try:
            quantity = Decimal(str(quantity_raw))
            reference_weight = Decimal(str(weight_raw))
        except (InvalidOperation, TypeError, ValueError) as error:
            raise ScmServiceError(
                "KG_BOM_ATTRIBUTION_PENDING",
                f"El componente BOM {index + 1} no tiene peso de referencia válido.",
                status_code=409,
            ) from error
        mass = (quantity * reference_weight).quantize(REFERENCE_KG_QUANTUM)
        if not quantity.is_finite() or not reference_weight.is_finite() or quantity <= 0 or reference_weight <= 0 or mass <= 0:
            raise ScmServiceError(
                "KG_BOM_ATTRIBUTION_PENDING",
                f"El componente BOM {index + 1} tiene cantidad o peso no positivo.",
                status_code=409,
            )
        total_mass += mass
        if source == "PREVIOUS":
            previous_mass += mass
        normalized.append({
            **item,
            "origen": source,
            "cantidad": format(quantity, "f"),
            "peso_referencia_kg": format(reference_weight, "f"),
            "masa_referencia_kg": format(mass, "f"),
        })
    if total_mass <= 0 or previous_mass > total_mass:
        raise ScmServiceError(
            "KG_BOM_ATTRIBUTION_PENDING",
            "La base BOM no permite calcular una proporción de masa válida.",
            status_code=409,
        )
    previous = (net * previous_mass / total_mass).quantize(KG_QUANTUM)
    fresh = (net - previous).quantize(KG_QUANTUM)
    base = {
        "revision": bom_basis.get("revision", bom_basis.get("bom_revision")) if isinstance(bom_basis, dict) else None,
        "componentes": normalized,
        "metodo": "MASA_REFERENCIA_Q_PESO",
    }
    if isinstance(bom_basis, dict):
        for key in (
            "source_trabajo_ot_id",
            "source_orden_operacion_id",
            "source_orden_trabajo_id",
            "referencias_capturadas_at",
            "referencia_peso_fuente",
        ):
            if bom_basis.get(key) is not None:
                base[key] = bom_basis[key]
    return {
        "estado": "ESTIMADA_BOM",
        "kg_medido": format(net, "f"),
        "kg_fabricacion_estimado": format(fresh, "f"),
        "kg_previo_estimado": format(previous, "f"),
        "base": base,
    }


def _persist_bom_estimate(session, *, weighing, manga, actor_id, operation_id, attribution):
    """Attach estimates to an existing measured fact exactly once."""
    if attribution["estado"] != "ESTIMADA_BOM":
        return
    existing_types = set(session.scalars(
        select(ScmAtribucionProduccionKg.tipo).where(
            ScmAtribucionProduccionKg.pesaje_id == weighing.id,
            ScmAtribucionProduccionKg.tipo != "NETO_MEDIDO",
        )
    ).all())
    # A later replay may observe a changed master weight.  Once estimate rows
    # exist, restore the projection from those append-only facts and never
    # recalculate the historical split from mutable masters.
    if existing_types:
        existing_rows = session.scalars(
            select(ScmAtribucionProduccionKg).where(
                ScmAtribucionProduccionKg.pesaje_id == weighing.id,
                ScmAtribucionProduccionKg.tipo != "NETO_MEDIDO",
            )
        ).all()
        fabrication_row = next(
            (row for row in existing_rows if row.tipo == "FABRICACION_ESTIMADA"),
            None,
        )
        previous_row = next(
            (row for row in existing_rows if row.tipo == "COMPONENTE_PREVIO_ESTIMADO"),
            None,
        )
        if fabrication_row is not None:
            weighing.kg_fabricacion_estimado = Decimal(fabrication_row.cantidad_kg)
            weighing.atribucion_kg_base_json = {
                "neto_medido_kg": format(Decimal(weighing.peso_fisico_neto_kg), "f"),
                "base": fabrication_row.base_json,
            }
        if previous_row is not None:
            weighing.kg_previo_estimado = Decimal(previous_row.cantidad_kg)
        weighing.atribucion_kg_estado = "ESTIMADA_BOM"
        return
    weighing.atribucion_kg_estado = attribution["estado"]
    weighing.kg_fabricacion_estimado = Decimal(attribution["kg_fabricacion_estimado"])
    weighing.kg_previo_estimado = Decimal(attribution["kg_previo_estimado"])
    weighing.atribucion_kg_base_json = {
        "neto_medido_kg": format(Decimal(weighing.peso_fisico_neto_kg), "f"),
        "base": attribution["base"],
    }
    rows = []
    current_segment = manga.tramos_trabajo[-1] if manga.tramos_trabajo else None
    source_work_id = attribution["base"].get("source_trabajo_ot_id")
    if source_work_id:
        try:
            source_work_id = UUID(str(source_work_id))
        except (TypeError, ValueError, AttributeError):
            source_work_id = None
    current_work_id = (
        current_segment.trabajo_ot_id
        if current_segment is not None else manga.trabajo_ot_id
    )
    fabrication_work_id = source_work_id or current_work_id
    if "FABRICACION_ESTIMADA" not in existing_types:
        rows.append(ScmAtribucionProduccionKg(
            manga_id=manga.id,
            pesaje_id=weighing.id,
            tramo_id=(manga.tramos_trabajo[-1].id if manga.tramos_trabajo else None),
            trabajo_ot_id=fabrication_work_id,
            tipo="FABRICACION_ESTIMADA",
            cantidad_kg=Decimal(attribution["kg_fabricacion_estimado"]),
            calidad="ESTIMADA_BOM",
            base_json=attribution["base"],
            actor_id=actor_id,
            operation_id=operation_id,
        ))
    if Decimal(attribution["kg_previo_estimado"]) > 0 and "COMPONENTE_PREVIO_ESTIMADO" not in existing_types:
        rows.append(ScmAtribucionProduccionKg(
            manga_id=manga.id,
            pesaje_id=weighing.id,
            tramo_id=(manga.tramos_trabajo[-1].id if manga.tramos_trabajo else None),
            # Previous material remains an OA/output-side estimate.  It is
            # not fabricated by the source OT and must not be projected onto
            # the source axis during OF/OT closure.
            trabajo_ot_id=current_work_id,
            tipo="COMPONENTE_PREVIO_ESTIMADO",
            cantidad_kg=Decimal(attribution["kg_previo_estimado"]),
            calidad="ESTIMADA_BOM",
            base_json=attribution["base"],
            actor_id=actor_id,
            operation_id=operation_id,
        ))
    session.add_all(rows)


def record_kg_production_evidence(
    session, *, actor_id, weighing_id, operation_id, data=None
):
    """Persist one idempotent NET evidence row without inventory side effects."""
    actor = load_actor(session, actor_id)
    command = dict(data or {})
    operation, replay = _reserve_operation(
        session,
        operation_id,
        f"/kg/pesajes/{weighing_id}/evidencia-produccion",
        actor,
        {"weighing_id": str(weighing_id), **command},
    )
    if replay is not None:
        return replay
    try:
        try:
            weighing_key = UUID(str(weighing_id))
        except (TypeError, ValueError, AttributeError) as error:
            raise ScmServiceError(
                "INVALID_UUID", "weighing_id debe ser UUID.", status_code=422
            ) from error
        weighing = session.scalar(
            select(ScmPesajeManga)
            .where(ScmPesajeManga.public_id == weighing_key)
            .with_for_update()
        )
        if weighing is None:
            raise ScmServiceError(
                "WEIGHING_NOT_FOUND", "El pesaje no existe.", status_code=404
            )
        manga = weighing.manga
        _kg_article(manga)
        if weighing.estado != "VIGENTE":
            raise ScmServiceError(
                "WEIGHING_NOT_ACTIVE", "El pesaje no está vigente.", status_code=409
            )
        existing = session.scalar(
            select(ScmAtribucionProduccionKg)
            .where(
                ScmAtribucionProduccionKg.pesaje_id == weighing.id,
                ScmAtribucionProduccionKg.tipo == "NETO_MEDIDO",
            )
            .with_for_update()
        )
        if existing is not None:
            attribution = preview_kg_attribution(
                net_kg=weighing.peso_fisico_neto_kg,
                bom_basis=_resolve_frozen_bom_basis(session, manga, command),
            )
            _persist_bom_estimate(
                session,
                weighing=weighing,
                manga=manga,
                actor_id=actor.id,
                operation_id=operation.operation_id,
                attribution=attribution,
            )
            session.flush()
            response = {
                "evidencia": existing.to_dict(),
                "kg_medido": format(weighing.peso_fisico_neto_kg, "f"),
                "kg_fabricacion_estimado": format(weighing.kg_fabricacion_estimado, "f") if weighing.kg_fabricacion_estimado is not None else None,
                "kg_previo_estimado": format(weighing.kg_previo_estimado, "f") if weighing.kg_previo_estimado is not None else None,
                "atribucion_estado": weighing.atribucion_kg_estado,
                "inventario_creado": False,
                "un_confirmadas": False,
                "idempotent_replay": True,
            }
            _complete_operation(operation, response)
            session.commit()
            return response
        net = Decimal(weighing.peso_fisico_neto_kg).quantize(KG_QUANTUM)
        bom_basis = _resolve_frozen_bom_basis(session, manga, command)
        attribution = preview_kg_attribution(net_kg=net, bom_basis=bom_basis)
        current_segment = manga.tramos_trabajo[-1] if manga.tramos_trabajo else None
        evidence = ScmAtribucionProduccionKg(
            manga_id=manga.id,
            pesaje_id=weighing.id,
            tramo_id=(
                manga.tramos_trabajo[-1].id if manga.tramos_trabajo else None
            ),
            # A manga always belongs to one originating work.  Do not infer
            # an OT by list order: continuity may have several tramos and
            # the production fact must retain the manga's canonical owner.
            trabajo_ot_id=(
                current_segment.trabajo_ot_id
                if current_segment is not None else manga.trabajo_ot_id
            ),
            tipo="NETO_MEDIDO",
            cantidad_kg=net,
            calidad="MEDIDA_DIRECTA",
            base_json={
                "fuente": "PESAJE_FINAL",
                "pesaje_public_id": str(weighing.public_id),
                "pesada_at": weighing.pesada_at.isoformat(),
                "bom": attribution["base"],
                "atribucion_estado": attribution["estado"],
            },
            actor_id=actor.id,
            operation_id=operation.operation_id,
        )
        session.add(evidence)
        weighing.atribucion_kg_estado = attribution["estado"]
        weighing.kg_fabricacion_estimado = (
            Decimal(attribution["kg_fabricacion_estimado"])
            if attribution["kg_fabricacion_estimado"] is not None else None
        )
        weighing.kg_previo_estimado = (
            Decimal(attribution["kg_previo_estimado"])
            if attribution["kg_previo_estimado"] is not None else None
        )
        weighing.atribucion_kg_base_json = {
            "neto_medido_kg": format(net, "f"),
            "base": attribution["base"],
        }
        _persist_bom_estimate(
            session,
            weighing=weighing,
            manga=manga,
            actor_id=actor.id,
            operation_id=operation.operation_id,
            attribution=attribution,
        )
        session.flush()
        response = {
            "evidencia": evidence.to_dict(),
            "kg_medido": format(net, "f"),
            "kg_fabricacion_estimado": attribution["kg_fabricacion_estimado"],
            "kg_previo_estimado": attribution["kg_previo_estimado"],
            "atribucion_estado": attribution["estado"],
            "inventario_creado": False,
            "un_confirmadas": False,
            "idempotent_replay": False,
        }
        session.add(_event(
            "PESAJE_MANGA", weighing.id,
            "KG_PRODUCTION_EVIDENCE_RECORDED", actor, operation, response,
        ))
        _complete_operation(operation, response)
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def _document_aggregate(session, *, documento_tipo, documento_id):
    if documento_tipo == "OT":
        try:
            key = UUID(str(documento_id))
        except (TypeError, ValueError, AttributeError) as error:
            raise ScmServiceError("INVALID_UUID", "documento_id no es UUID.", status_code=422) from error
        ot = session.scalar(
            select(RegistroDiarioProduccion)
            .where(RegistroDiarioProduccion.public_id == key)
            .with_for_update()
        )
        if ot is None or ot.tipo_ot not in {"FABRICACION", "ENSAMBLE"}:
            raise ScmServiceError("DOCUMENT_NOT_FOUND", "La OT no existe.", status_code=404)
        works = session.scalars(
            select(ScmTrabajoOt)
            .where(ScmTrabajoOt.orden_trabajo_id == ot.id)
            .with_for_update()
        ).all()
        return ot, works
    try:
        key = UUID(str(documento_id))
    except (TypeError, ValueError, AttributeError) as error:
        raise ScmServiceError("INVALID_UUID", "documento_id no es UUID.", status_code=422) from error
    order = session.scalar(
        select(ScmOrdenOperacion)
        .where(ScmOrdenOperacion.id == key)
        .with_for_update()
    )
    if order is None or ((documento_tipo == "OF" and order.tipo != "FABRICACION") or
                         (documento_tipo == "OA" and order.tipo != "ENSAMBLE")):
        raise ScmServiceError("DOCUMENT_NOT_FOUND", "La orden no existe o no corresponde al tipo.", status_code=404)
    works = session.scalars(
        select(ScmTrabajoOt)
        .where(ScmTrabajoOt.orden_operacion_id == order.id)
        .with_for_update()
    ).all()
    return order, works


def close_productive_document_kg(
    session, *, actor_id, documento_tipo, documento_id, operation_id, data
):
    """Close OT/OF/OA after kg evidence, preserving logistics and UN facts."""
    actor = load_actor(session, actor_id)
    tipo = str(documento_tipo or "").upper()
    if tipo not in {"OT", "OF", "OA"}:
        raise ScmServiceError("INVALID_DOCUMENT_TYPE", "Tipo de documento inválido.", status_code=422)
    command = dict(data or {})
    operation, replay = _reserve_operation(
        session, operation_id, f"/kg/{tipo}/{documento_id}/cierre", actor,
        {"documento_tipo": tipo, "documento_id": str(documento_id), **command},
    )
    if replay is not None:
        return replay
    try:
        aggregate, works = _document_aggregate(
            session, documento_tipo=tipo, documento_id=documento_id
        )
        if command.get("version") is not None:
            try:
                requested_version = int(command["version"])
            except (TypeError, ValueError) as error:
                raise ScmServiceError(
                    "INVALID_VERSION", "version debe ser entero.", status_code=422
                ) from error
            if aggregate.version != requested_version:
                raise ScmServiceError(
                    "VERSION_CONFLICT",
                    "El documento fue modificado por otra operación.",
                    status_code=409,
                )
        if aggregate.estado in {"CERRADA", "ANULADA"}:
            raise ScmServiceError("DOCUMENT_ALREADY_CLOSED", "El documento ya está cerrado.", status_code=409)
        def _inline_source_pending(work):
            return (
                not work.mangas
                and bool(getattr(work, "saldos_wip_salida", ()))
                and work.trabajo_color is not None
                and work.estado in {"EN_EJECUCION", "PAUSADO"}
            )
        ot_segments = []
        if tipo == "OT":
            work_ids = [item.id for item in works]
            if work_ids:
                ot_segments = session.scalars(
                    select(ScmTramoMangaTrabajo)
                    .where(ScmTramoMangaTrabajo.trabajo_ot_id.in_(work_ids))
                    .with_for_update()
                ).all()
            segments_by_work = {}
            for segment in ot_segments:
                segments_by_work.setdefault(segment.trabajo_ot_id, []).append(segment)
            pending_works = []
            for item in works:
                owned = segments_by_work.get(item.id, [])
                if item.estado in {"COMPLETADO", "ANULADO"}:
                    continue
                if owned and all(seg.estado in {"CERRADO", "ANULADO"} for seg in owned):
                    continue
                # Concurrent OA WIP may consume a source TrabajoColor whose
                # source OT has no physical manga at all.  Its active WIP
                # saldo is the explicit cycle marker; allow the KG closure
                # to proceed and let persisted BOM rows (when available)
                # provide the source estimate.  A plain legacy work without
                # this inline marker remains blocked as before.
                inline_source_pending = not owned and _inline_source_pending(item)
                if inline_source_pending:
                    continue
                pending_works.append(item)
            mangas = [segment.manga for segment in ot_segments if segment.manga is not None]
            known_ids = {item.id for item in mangas}
            mangas.extend(
                manga for work in works for manga in (work.mangas or ())
                if manga.id not in known_ids
            )
        else:
            pending_works = [
                item for item in works
                if item.estado not in {"COMPLETADO", "ANULADO"}
                and not _inline_source_pending(item)
            ]
            mangas = [manga for work in works for manga in (work.mangas or ())]
        if tipo in {"OF", "OA"}:
            # Output mangas of an OF/OA may intentionally have no
            # ``trabajo_ot_id`` (especially concurrent OA WIP).  Resolve them
            # through the daily OT's operation instead of dropping their kg
            # evidence from the document closure summary.
            aggregate_mangas = session.scalars(
                select(ScmManga)
                .join(
                    RegistroDiarioProduccion,
                    ScmManga.ot_id == RegistroDiarioProduccion.id,
                )
                .where(
                    RegistroDiarioProduccion.orden_operacion_id == aggregate.id
                )
                .with_for_update()
            ).all()
            mangas.extend(
                manga for manga in aggregate_mangas
                if manga.id not in {item.id for item in mangas}
            )
        mangas = list({manga.id: manga for manga in mangas}.values())
        units = set()
        for manga in mangas:
            article = manga.lote_articulo.articulo if manga.lote_articulo is not None else None
            if article is not None and article.unidad_inventario:
                units.add(str(article.unidad_inventario).upper())
        if tipo in {"OF", "OA"}:
            for output in getattr(aggregate, "salidas", ()):
                article = getattr(output, "articulo", None)
                if article is not None and article.unidad_inventario:
                    units.add(str(article.unidad_inventario).upper())
        if "KG" in units and "UN" in units:
            raise ScmServiceError(
                "KG_DOCUMENT_MIXED_UNITS",
                "El documento mezcla mangas KG y UN; cierre cada eje por separado.",
                status_code=409,
            )
        pending_mangas = []
        for manga in mangas:
            if manga.estado in {
                "PESADA", "ETIQUETADA_FINAL", "PENDIENTE_RECEPCION_ALMACEN",
                "RECIBIDA", "ANULADA",
            }:
                continue
            if tipo == "OT":
                owned = [segment for segment in ot_segments if segment.manga_id == manga.id]
                if owned and all(segment.estado in {"CERRADO", "ANULADO"} for segment in owned):
                    continue
            pending_mangas.append(manga)
        if pending_works or pending_mangas:
            raise ScmServiceError(
                "KG_DOCUMENT_HAS_PENDING_WORK",
                "El documento conserva trabajos o mangas sin resolver.",
                status_code=409,
                details={
                    "trabajos": [str(item.id) for item in pending_works],
                    "mangas": [str(item.public_id) for item in pending_mangas],
                },
            )
        weighed_ids = [manga.id for manga in mangas]
        measured = Decimal("0")
        if tipo == "OT" and ot_segments:
            measured = Decimal(session.scalar(
                select(func.coalesce(func.sum(ScmTramoMangaTrabajo.cantidad_atribuida_kg), 0))
                .where(
                    ScmTramoMangaTrabajo.id.in_([segment.id for segment in ot_segments]),
                    ScmTramoMangaTrabajo.estado != "ANULADO",
                )
            )).quantize(KG_QUANTUM)
        if weighed_ids and (tipo != "OT" or not ot_segments):
            measured = Decimal(session.scalar(
                select(func.coalesce(func.sum(ScmPesajeManga.peso_fisico_neto_kg), 0))
                .where(
                    ScmPesajeManga.manga_id.in_(weighed_ids),
                    ScmPesajeManga.estado == "VIGENTE",
                )
            )).quantize(KG_QUANTUM)
        planned = command.get("kg_objetivo")
        planned_kg = _kg(planned, "kg_objetivo") if planned is not None else None
        close_type = str(command.get("tipo_cierre") or "NORMAL").upper()
        if close_type not in {"NORMAL", "PARCIAL"}:
            raise ScmServiceError("INVALID_CLOSE_TYPE", "tipo_cierre debe ser NORMAL o PARCIAL.", status_code=422)
        reason = str(command.get("motivo") or "").strip() or None
        if close_type == "PARCIAL" and not reason:
            raise ScmServiceError("KG_PARTIAL_CLOSE_REASON_REQUIRED", "El cierre parcial requiere motivo.", status_code=422)
        deviation = (measured - planned_kg).quantize(KG_QUANTUM) if planned_kg is not None else None
        weighings = session.scalars(
            select(ScmPesajeManga).where(
                ScmPesajeManga.manga_id.in_(weighed_ids),
                ScmPesajeManga.estado == "VIGENTE",
            )
        ).all() if weighed_ids else []
        # An OA output manga is deliberately not owned by the source
        # TrabajoColor.  Its frozen BOM rows still carry that source owner;
        # include those rows when closing the source OT/OF, while excluding
        # rows already represented by this document's own weighings.  This
        # projects one estimate onto the source axis without manufacturing a
        # second NET fact or double-counting the OA closure.
        weighing_ids = {item.id for item in weighings}
        owner_work_ids = [item.id for item in works]
        source_estimate_rows = []
        if owner_work_ids:
            source_estimate_rows = session.scalars(
                select(ScmAtribucionProduccionKg)
                .where(
                    ScmAtribucionProduccionKg.trabajo_ot_id.in_(owner_work_ids),
                    ScmAtribucionProduccionKg.tipo.in_(
                        ("FABRICACION_ESTIMADA", "COMPONENTE_PREVIO_ESTIMADO")
                    ),
                    ~ScmAtribucionProduccionKg.pesaje_id.in_(weighing_ids)
                    if weighing_ids
                    else True,
                )
            ).all()
        fabrication_estimated = sum(
            (Decimal(item.kg_fabricacion_estimado or 0) for item in weighings),
            Decimal("0"),
        ).quantize(KG_QUANTUM)
        previous_estimated = sum(
            (Decimal(item.kg_previo_estimado or 0) for item in weighings),
            Decimal("0"),
        ).quantize(KG_QUANTUM)
        fabrication_estimated += sum(
            (
                Decimal(row.cantidad_kg)
                for row in source_estimate_rows
                if row.tipo == "FABRICACION_ESTIMADA"
            ),
            Decimal("0"),
        ).quantize(KG_QUANTUM)
        previous_estimated += sum(
            (
                Decimal(row.cantidad_kg)
                for row in source_estimate_rows
                if row.tipo == "COMPONENTE_PREVIO_ESTIMADO"
            ),
            Decimal("0"),
        ).quantize(KG_QUANTUM)
        closure_pending = list(command.get("pendientes") or [])
        # OT continuity owns physical deltas, while the BOM split belongs to
        # the final document (OF/OA).  Do not attach a complete final-net
        # estimate (for example 10/2 over NET 9) to a 4 kg OT segment.
        if tipo == "OT" and weighed_ids:
            if fabrication_estimated > 0 or previous_estimated > 0:
                closure_pending.append({
                    "codigo": "KG_BOM_ESTIMATE_CONSOLIDATED_AT_OF_OA",
                    "pesajes": [str(item.public_id) for item in weighings],
                })
            fabrication_estimated = Decimal("0")
            previous_estimated = Decimal("0")
        pending_bom = [
            str(item.public_id) for item in weighings
            if item.atribucion_kg_estado != "ESTIMADA_BOM"
        ]
        if pending_bom:
            closure_pending.append({
                "codigo": "KG_BOM_ATTRIBUTION_PENDING",
                "pesajes": pending_bom,
            })
        closure = ScmCierreProductivoKg(
            documento_tipo=tipo,
            documento_id=str(documento_id),
            ot_id=aggregate.id if tipo == "OT" else None,
            tipo_cierre=close_type,
            kg_medido=measured,
            desviacion_plan_kg=deviation,
            motivo=reason,
            kg_fabricacion_estimado=fabrication_estimated if fabrication_estimated > 0 else None,
            kg_previo_estimado=previous_estimated if previous_estimated > 0 else None,
            pendientes_json=closure_pending,
            evidencia_json={"mangas": [str(manga.public_id) for manga in mangas]},
            actor_id=actor.id,
            operation_id=operation.operation_id,
        )
        session.add(closure)
        aggregate.estado = "CERRADA"
        if tipo == "OT":
            aggregate.cerrada_at = utc_now()
            aggregate.version += 1
        else:
            aggregate.closed_by_id = actor.id
            aggregate.closed_at = utc_now()
            aggregate.version += 1
        session.flush()
        response = {
            "documento_tipo": tipo,
            "documento_id": str(documento_id),
            "codigo": getattr(aggregate, "codigo", None)
            or getattr(aggregate, "codigo_ot", None),
            "estado": aggregate.estado,
            "cierre": closure.to_dict(),
            "kg_medido": format(measured, "f"),
            "desviacion_plan_kg": format(deviation, "f") if deviation is not None else None,
            "inventario_creado": False,
            "un_confirmadas": False,
            "pendientes_conservados": closure.pendientes_json,
            "idempotent_replay": False,
        }
        session.add(_event("DOCUMENTO_PRODUCTIVO", closure.id, "KG_DOCUMENT_CLOSED", actor, operation, response))
        _complete_operation(operation, response)
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise

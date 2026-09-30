"""Governed recovery of simple KG weighings that missed inventory intake.

KG009 intentionally starts with a read-only manifest.  Applying a manifest is
append-only and idempotent: the source weighing and its UN history remain
untouched, while the KG projection uses a parent operation and one stable child
operation per source.  Ambiguous UN projections, controls and corrections are
hard conflicts; they are not guessed or silently rewritten.
"""

from decimal import Decimal
import hashlib
import json
from time import monotonic
from uuid import UUID, uuid5

from sqlalchemy import or_, select, text
from sqlalchemy.orm import noload, selectinload

from app.models.scm_articulos import KG_ARTICLE_CLASSES, ScmArticulo
from app.models.scm_auditoria import ScmEvento
from app.models.scm_inventory import (
    ScmSaldoInventario,
    ScmUbicacionInventario,
    ScmUnidadLogisticaInventario,
)
from app.models.scm_inventory_kg import (
    ScmExistenciaMangaKg,
    ScmMovimientoInventarioKg,
    ScmSaldoInventarioKg,
)
from app.models.scm_ot import (
    ScmAnulacionPesajeManga,
    ScmControlPesoManga,
    ScmCorreccionAsignacionManga,
    ScmCorreccionPesajeManga,
    ScmManga,
    ScmPesajeManga,
    ScmReaperturaManga,
    ScmAtribucionProduccionKg,
    ScmLoteArticulo,
    ScmTramoMangaTrabajo,
    ScmTrabajoOt,
)
from app.models.scm_warehouse import ScmExistenciaManga
from app.services.scm_kg_production_service import (
    _production_location,
    sync_kg_production_inventory,
)
from app.services.scm_kg_service import activate_article_for_kg
from app.services.scm_ot_service import (
    _complete_operation,
    _event,
    _reserve_operation,
)
from app.services.scm_service_support import (
    ScmServiceError,
    acquire_kg_productive_write_lock,
    load_actor,
    required_text,
)


RECOVERY_CAPABILITY = "ALMACEN_CONFIG_ADMINISTRAR"
RECOVERY_ENDPOINT = "/kg/recovery"
RECOVERY_NAMESPACE = UUID("9a13a2f6-6cb8-5a0f-9c38-8bd6b3c7f009")
RECOVERY_LOCK_TIMEOUT_MS = 5000
RECOVERY_STATEMENT_TIMEOUT_MS = 30000
RECOVERY_BATCH_TIMEOUT_SECONDS = RECOVERY_STATEMENT_TIMEOUT_MS / 1000


def _uuid(value, *, field):
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise ScmServiceError(
            "INVALID_UUID", f"{field} debe ser un UUID valido.", status_code=422
        ) from error


def _article_ids(values):
    ids = sorted(set(values or ()))
    if (
        not ids
        or len(ids) > 100
        or any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in ids)
    ):
        raise ScmServiceError(
            "INVALID_ARTICLES",
            "Seleccione entre 1 y 100 articulos explicitos.",
            status_code=422,
        )
    return ids


def _legacy_blockers(session, article_id):
    balances = session.scalars(
        select(ScmSaldoInventario).options(noload("*")).where(
            ScmSaldoInventario.articulo_scm_id == article_id
        ).with_for_update()
    ).all()
    if any(
        Decimal(value or 0) != 0
        for row in balances
        for value in (row.cantidad_fisica, row.cantidad_reservada, row.cantidad_no_disponible)
    ):
        return ["LEGACY_UN_BALANCE_NONZERO"]
    if session.scalar(
        select(ScmExistenciaManga.id).options(noload("*")).where(
            ScmExistenciaManga.articulo_scm_id == article_id,
            ScmExistenciaManga.estado_logistico != "REVERSADA",
        )
    ) is not None:
        return ["LEGACY_UN_EXISTENCE_ACTIVE"]
    if session.scalar(
        select(ScmUnidadLogisticaInventario.id).options(noload("*")).where(
            ScmUnidadLogisticaInventario.articulo_scm_id == article_id
        )
    ) is not None:
        return ["LEGACY_LOGISTIC_UNIT"]
    return []


def _source_ids(values):
    ids = sorted(set(values or ()))
    if not ids or len(ids) > 200:
        raise ScmServiceError(
            "SOURCE_SELECTION_REQUIRED",
            "Declare entre 1 y 200 pesajes fuente explícitos.",
            status_code=422,
        )
    return [_uuid(value, field="source_pesaje_id") for value in ids]


def _recovery_deadline():
    return monotonic() + RECOVERY_BATCH_TIMEOUT_SECONDS


def _check_recovery_deadline(deadline):
    if monotonic() > deadline:
        raise ScmServiceError(
            "KG_RECOVERY_DEADLINE_EXCEEDED",
            "La recuperación excedió su ventana; reanude con un lote menor.",
            status_code=409,
        )


def _prepare_recovery_transaction(session):
    """Bound waits before taking the global transaction-scoped KG lock."""
    if session.get_bind().dialect.name == "postgresql":
        session.execute(
            text("SET LOCAL lock_timeout = :timeout"),
            {"timeout": f"{RECOVERY_LOCK_TIMEOUT_MS}ms"},
        )
        session.execute(
            text("SET LOCAL statement_timeout = :timeout"),
            {"timeout": f"{RECOVERY_STATEMENT_TIMEOUT_MS}ms"},
        )


def _derived_un_projection(manga, weighing, un_confirmed, un_contained):
    """Recognize the normal final-flow UN mirror, never an explicit count.

    The weighing snapshot is immutable evidence.  Only a non-partial final
    with no returned plan quantity and equal confirmed/contained values is
    eligible for neutralization; malformed or absent snapshots remain a
    conflict rather than being guessed.
    """
    if not un_contained or un_contained != un_confirmed:
        return False
    if weighing.fuente_cantidad != "PLAN_CONFIRMADO_POR_PESAJE":
        return False
    snapshot = weighing.snapshots_json
    if not isinstance(snapshot, dict):
        return False
    if snapshot.get("cierre_parcial") is not False:
        return False
    try:
        assigned = Decimal(str(snapshot["cantidad_asignada_original_un"]))
        returned = Decimal(str(snapshot["cantidad_devuelta_plan_un"]))
    except (KeyError, TypeError, ValueError):
        return False
    return assigned >= un_confirmed and returned == 0


def _kg_projection_hash(public_id, net_kg, correction_id=None):
    total = Decimal(net_kg).quantize(Decimal("0.001"))
    source_token = f"PESAJE_FINAL:{public_id}:{total}:{correction_id or ''}"
    return hashlib.sha256(source_token.encode()).hexdigest()


def _kg_projection_complete(
    active_kg,
    movement,
    attribution,
    net_kg,
    public_id,
    *,
    article_id=None,
    manga_id=None,
    weighing_id=None,
    owner_work_id=None,
    movement_saldo=None,
):
    """Return true only when the complete KG projection is source-coherent.

    A pre-existing row is a valid no-op only when the existence, movement and
    measured attribution all describe the same final weighing.  Matching NET
    values alone are insufficient: crossed article/manga/source rows must be
    rejected as a conflict instead of repaired by recovery.
    """
    if active_kg is None or movement is None or attribution is None:
        return False
    net = Decimal(net_kg)
    correction_id = getattr(movement, "correccion_aplicada_public_id", None)
    expected_hash = _kg_projection_hash(public_id, net, correction_id)
    movement_id = getattr(movement, "id", None)
    movement_saldo = movement_saldo or getattr(movement, "saldo", None)
    attribution_base = getattr(attribution, "base_json", None)
    if not isinstance(attribution_base, dict):
        return False
    return all((
        movement_id is not None,
        active_kg.movimiento_ingreso_id == movement_id,
        active_kg.operation_id == movement.operation_id,
        active_kg.pesaje_public_id == public_id,
        active_kg.manga_id == manga_id if manga_id is not None else True,
        active_kg.articulo_scm_id == article_id if article_id is not None else True,
        active_kg.saldo_id == movement.saldo_id,
        movement_saldo is not None,
        movement_saldo.articulo_scm_id == article_id if article_id is not None else True,
        movement_saldo.ubicacion_id == active_kg.ubicacion_id,
        movement.pesaje_public_id == public_id,
        movement.referencia_tipo == "PESAJE_MANGA",
        movement.referencia_id == str(public_id),
        movement.tipo == "INGRESO_PRODUCCION",
        movement.fuente_tipo == "KG_AUTO_PRODUCCION",
        active_kg.origen_tipo == "PRODUCCION",
        active_kg.pesaje_public_id == public_id,
        active_kg.projection_sha256 == expected_hash,
        movement.projection_sha256 == expected_hash,
        Decimal(active_kg.cantidad_fisica_kg) == net,
        Decimal(active_kg.peso_neto_snapshot_kg) == net,
        Decimal(movement.cantidad_delta_kg) == net,
        Decimal(movement.peso_neto_snapshot_kg) == net,
        attribution.manga_id == manga_id if manga_id is not None else True,
        attribution.pesaje_id == weighing_id if weighing_id is not None else True,
        attribution.trabajo_ot_id == owner_work_id,
        attribution.tipo == "NETO_MEDIDO",
        attribution.calidad == "MEDIDA_DIRECTA",
        Decimal(attribution.cantidad_kg) == net,
        attribution_base.get("pesaje_public_id") == str(public_id),
    ))


def _source_un_quantity_conflict(
    manga_quantity,
    source_quantity,
    kg_complete,
    work_quantity=None,
    contained_quantity=None,
):
    """Require explicit neutralization before accepting UN/source divergence."""
    manga = Decimal(manga_quantity or 0)
    source = Decimal(source_quantity or 0)
    if manga == source:
        return False
    if not kg_complete:
        return True
    # With no optional context the helper remains useful for the simple
    # zero/None neutralized state.  The manifest always supplies work and
    # contained quantities, so a stale 5-vs-12 projection cannot pass.
    return not (
        manga == 0
        and Decimal(contained_quantity or 0) == 0
        and (
            work_quantity is None
            or Decimal(work_quantity or 0) == 0
        )
    )


def _source_snapshot_hash(source_snapshot):
    return hashlib.sha256(
        json.dumps(
            source_snapshot,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()


def _lock_work_reconciliation(session, work_id):
    """Lock a work and all its manga before comparing UN contributions."""
    work = session.scalar(
        select(ScmTrabajoOt).options(noload("*"))
        .where(ScmTrabajoOt.id == work_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if work is None:
        return None, [], None
    mangas = session.scalars(
        select(ScmManga).options(noload("*"))
        .where(ScmManga.trabajo_ot_id == work.id)
        .order_by(ScmManga.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    observed = sum(
        (Decimal(manga.cantidad_confirmada_un or 0) for manga in mangas),
        Decimal("0"),
    )
    return work, mangas, observed


def _source_manifest(session, article_ids, source_pesaje_ids, *, context=None):
    """Build the source manifest with one locked batch per relation/check.

    Recovery is intentionally conservative, but it need not issue one ORM
    query per source.  Every row used for a decision is locked/refreshed in a
    deterministic batch before the manifest is evaluated.  ``context`` keeps
    those exact instances for apply, avoiding a second stale select.
    """
    rows = session.scalars(
        select(ScmPesajeManga).options(noload("*"))
        .join(ScmManga, ScmManga.id == ScmPesajeManga.manga_id)
        .join(ScmLoteArticulo, ScmLoteArticulo.id == ScmManga.lote_articulo_id)
        .where(
            ScmPesajeManga.estado == "VIGENTE",
            ScmLoteArticulo.articulo_id.in_(article_ids),
            ScmPesajeManga.public_id.in_(source_pesaje_ids),
        )
        .order_by(ScmPesajeManga.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    found_ids = {row.public_id for row in rows}
    manga_ids = sorted({row.manga_id for row in rows})
    manga_rows = session.scalars(
        select(ScmManga).options(noload("*"))
        .where(ScmManga.id.in_(manga_ids))
        .order_by(ScmManga.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all() if manga_ids else []
    mangas = {row.id: row for row in manga_rows}
    lote_ids = sorted({row.lote_articulo_id for row in manga_rows})
    lotes = {
        row.id: row for row in session.scalars(
            select(ScmLoteArticulo).options(noload("*"))
            .where(ScmLoteArticulo.id.in_(lote_ids))
            .order_by(ScmLoteArticulo.id)
            .execution_options(populate_existing=True)
        ).all()
    } if lote_ids else {}
    article_map = {}
    if context:
        article_map.update(context.get("article_objects", {}))
    missing_article_ids = sorted({lote.articulo_id for lote in lotes.values()} - set(article_map))
    if missing_article_ids:
        article_map.update({
            row.id: row for row in session.scalars(
                select(ScmArticulo).options(noload("*"))
                .where(ScmArticulo.id.in_(missing_article_ids))
                .order_by(ScmArticulo.id)
                .execution_options(populate_existing=True)
            ).all()
        })
    work_ids = sorted({manga.trabajo_ot_id for manga in manga_rows if manga.trabajo_ot_id is not None})
    works = {
        row.id: row for row in session.scalars(
            select(ScmTrabajoOt).options(noload("*"))
            .where(ScmTrabajoOt.id.in_(work_ids))
            .order_by(ScmTrabajoOt.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    } if work_ids else {}
    all_work_mangas = session.scalars(
        select(ScmManga).options(noload("*"))
        .where(ScmManga.trabajo_ot_id.in_(work_ids))
        .order_by(ScmManga.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all() if work_ids else []
    observed_by_work = {}
    for manga in all_work_mangas:
        observed_by_work[manga.trabajo_ot_id] = (
            observed_by_work.get(manga.trabajo_ot_id, Decimal("0"))
            + Decimal(manga.cantidad_confirmada_un or 0)
        )
    segments_by_manga = {}
    if manga_ids:
        for segment in session.scalars(
            select(ScmTramoMangaTrabajo).options(noload("*"))
            .where(ScmTramoMangaTrabajo.manga_id.in_(manga_ids))
            .order_by(ScmTramoMangaTrabajo.manga_id, ScmTramoMangaTrabajo.secuencia)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all():
            segments_by_manga.setdefault(segment.manga_id, []).append(segment)

    manga_id_set = set(manga_ids)
    weighing_id_set = {row.id for row in rows}
    control_ids = set(session.scalars(
        select(ScmControlPesoManga.manga_id).options(noload("*"))
        .where(ScmControlPesoManga.manga_id.in_(manga_ids))
    ).all()) if manga_ids else set()
    correction_ids = set(session.scalars(
        select(ScmCorreccionPesajeManga.pesaje_id).options(noload("*"))
        .where(ScmCorreccionPesajeManga.pesaje_id.in_(weighing_id_set))
    ).all()) if weighing_id_set else set()
    annulment_manga_ids = set(session.scalars(
        select(ScmPesajeManga.manga_id).options(noload("*"))
        .join(ScmAnulacionPesajeManga, ScmAnulacionPesajeManga.pesaje_id == ScmPesajeManga.id)
        .where(ScmPesajeManga.manga_id.in_(manga_ids))
    ).all()) if weighing_id_set else set()
    reopening_rows = session.execute(
        select(ScmReaperturaManga.manga_id, ScmReaperturaManga.pesaje_id).options(noload("*"))
        .where(or_(
            ScmReaperturaManga.manga_id.in_(manga_ids),
            ScmReaperturaManga.pesaje_id.in_(weighing_id_set),
        ))
    ).all() if manga_ids or weighing_id_set else []
    reopening_manga_ids = {row[0] for row in reopening_rows}
    reopening_weighing_ids = {row[1] for row in reopening_rows}
    assignment_ids = set(session.scalars(
        select(ScmCorreccionAsignacionManga.manga_id).options(noload("*"))
        .where(ScmCorreccionAsignacionManga.manga_id.in_(manga_ids))
    ).all()) if manga_ids else set()
    legacy_logistics_ids = set(session.scalars(
        select(ScmExistenciaManga.manga_id).options(noload("*"))
        .where(ScmExistenciaManga.manga_id.in_(manga_ids))
    ).all()) if manga_ids else set()
    active_kg_rows = session.scalars(
        select(ScmExistenciaMangaKg).options(
            noload("*"),
            selectinload(ScmExistenciaMangaKg.unidad_fisica_kg),
        )
        .where(ScmExistenciaMangaKg.manga_id.in_(manga_ids))
        .order_by(ScmExistenciaMangaKg.manga_id, ScmExistenciaMangaKg.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all() if manga_ids else []
    existences = {row.manga_id: row for row in active_kg_rows}
    active_kg = {
        row.manga_id: row for row in active_kg_rows
        if row.estado_logistico != "REVERSADA"
    }
    kg_history_ids = set(existences)
    movement_rows = session.scalars(
        select(ScmMovimientoInventarioKg).options(noload("*"))
        .where(ScmMovimientoInventarioKg.pesaje_public_id.in_(source_pesaje_ids))
        .order_by(ScmMovimientoInventarioKg.pesaje_public_id, ScmMovimientoInventarioKg.id.desc())
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all() if source_pesaje_ids else []
    movements = {}
    for movement in movement_rows:
        movements.setdefault(movement.pesaje_public_id, movement)
    movement_saldo_ids = sorted({movement.saldo_id for movement in movements.values()})
    movement_saldos = {
        row.id: row for row in session.scalars(
            select(ScmSaldoInventarioKg).options(noload("*"))
            .where(ScmSaldoInventarioKg.id.in_(movement_saldo_ids))
            .order_by(ScmSaldoInventarioKg.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    } if movement_saldo_ids else {}
    attribution_rows = session.scalars(
        select(ScmAtribucionProduccionKg).options(noload("*"))
        .where(
            ScmAtribucionProduccionKg.pesaje_id.in_(weighing_id_set),
            ScmAtribucionProduccionKg.tipo == "NETO_MEDIDO",
        )
        .order_by(ScmAtribucionProduccionKg.pesaje_id, ScmAtribucionProduccionKg.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all() if weighing_id_set else []
    attributions = {}
    for attribution in attribution_rows:
        attributions.setdefault(attribution.pesaje_id, attribution)
    if context is not None:
        context.update({
            "weighings": {row.public_id: row for row in rows},
            "mangas": mangas,
            "segments": segments_by_manga,
            "works": works,
            "articles": article_map,
            "article_objects": article_map,
            "active_kg": active_kg,
            "existences": existences,
            "movements": movements,
            "movement_saldos": movement_saldos,
            "attributions": attributions,
        })

    manifest = []
    for weighing in rows:
        manga = mangas.get(weighing.manga_id)
        lote = lotes.get(manga.lote_articulo_id) if manga is not None else None
        article = article_map.get(lote.articulo_id) if lote is not None else None
        reasons = []
        segments = segments_by_manga.get(manga.id, []) if manga is not None else []
        work = works.get(manga.trabajo_ot_id) if manga is not None else None
        observed_work_un = observed_by_work.get(manga.trabajo_ot_id) if manga is not None else None
        if manga is None or lote is None or article is None:
            reasons.append("SOURCE_NOT_FOUND_OR_NOT_SELECTED")
            continue
        if manga.estado not in {"PESADA", "ETIQUETADA_FINAL", "PENDIENTE_RECEPCION_ALMACEN"}:
            reasons.append("MANGA_LOGISTICS_NOT_SIMPLE")
        if manga.id in control_ids:
            reasons.append("CONTROL_PRESENT")
        if weighing.id in correction_ids:
            reasons.append("WEIGHING_CORRECTION_PRESENT")
        if manga.id in annulment_manga_ids:
            reasons.append("WEIGHING_ANNULMENT_PRESENT")
        if weighing.id in reopening_weighing_ids or manga.id in reopening_manga_ids:
            reasons.append("REOPENING_PRESENT")
        if manga.id in assignment_ids:
            reasons.append("ASSIGNMENT_CORRECTION_PRESENT")
        if manga.id in legacy_logistics_ids:
            reasons.append("LEGACY_LOGISTICS_PRESENT")
        active = active_kg.get(manga.id)
        if active is None and manga.id in kg_history_ids:
            reasons.append("KG_LOGISTICS_HISTORY_PRESENT")
        movement = movements.get(weighing.public_id)
        un_confirmed = Decimal(manga.cantidad_confirmada_un or 0)
        un_contained = Decimal(manga.cantidad_contenida_un or 0)
        if work is not None and observed_work_un != Decimal(work.cantidad_confirmada_un or 0):
            reasons.append("UN_WORK_RECONCILIATION_REQUIRED")
        derived_un_projection = _derived_un_projection(manga, weighing, un_confirmed, un_contained)
        if un_contained != 0 and not derived_un_projection:
            reasons.append("UN_EXPLICIT_COUNT_REQUIRES_REVIEW")
        if un_confirmed != 0 and weighing.fuente_cantidad != "PLAN_CONFIRMADO_POR_PESAJE":
            reasons.append("UN_PROJECTION_RECONCILIATION_REQUIRED")
        if un_confirmed != 0 and len(segments) > 1:
            reasons.append("UN_PROJECTION_SEGMENT_AMBIGUOUS")
        if un_confirmed != 0 and work is None:
            reasons.append("UN_PROJECTION_WORK_MISSING")
        if un_confirmed != 0 and len(segments) == 1:
            segment = segments[0]
            if Decimal(segment.cantidad_inicio_un or 0) != 0 or Decimal(segment.cantidad_fin_un or 0) != un_confirmed:
                reasons.append("UN_PROJECTION_SEGMENT_MISMATCH")
            if segment.trabajo_ot_id != manga.trabajo_ot_id:
                reasons.append("UN_PROJECTION_OWNERSHIP_MISMATCH")
            if any(Decimal(value or 0) != 0 for value in (segment.cantidad_inicio_kg, segment.cantidad_fin_kg, segment.cantidad_atribuida_kg)):
                reasons.append("KG_SEGMENT_ALREADY_POPULATED")
        if un_confirmed != 0 and segments and Decimal(segments[-1].cantidad_atribuida_un or 0) not in {Decimal("0"), un_confirmed}:
            reasons.append("UN_PROJECTION_DELTA_MISMATCH")
        attribution = attributions.get(weighing.id)
        net_kg = Decimal(weighing.peso_fisico_neto_kg)
        kg_projection_complete = _kg_projection_complete(
            active, movement, attribution, net_kg, weighing.public_id,
            article_id=article.id, manga_id=manga.id, weighing_id=weighing.id,
            owner_work_id=manga.trabajo_ot_id,
            movement_saldo=movement_saldos.get(movement.saldo_id) if movement else None,
        )
        if _source_un_quantity_conflict(
            manga.cantidad_confirmada_un, weighing.cantidad_confirmada, kg_projection_complete,
            work_quantity=(work.cantidad_confirmada_un if work is not None else None),
            contained_quantity=manga.cantidad_contenida_un,
        ):
            reasons.append("UN_SOURCE_MANGA_QUANTITY_MISMATCH")
        already_applied = kg_projection_complete
        if (active is not None or movement is not None or attribution is not None) and not kg_projection_complete:
            reasons.append("KG_PROJECTION_PARTIAL")
        source_snapshot = {
            "pesaje_public_id": str(weighing.public_id),
            "operation_id": str(weighing.operation_id),
            "capture_id": str(weighing.capture_id),
            "neto_kg": format(net_kg, "f"),
            "cantidad_confirmada": format(Decimal(weighing.cantidad_confirmada or 0), "f"),
            "fuente_cantidad": weighing.fuente_cantidad,
            "pesada_at": weighing.pesada_at.isoformat(),
            "estado": weighing.estado,
            "snapshots": weighing.snapshots_json,
            "article": {"id": article.id, "version": article.version, "clase": article.clase, "unidad_inventario": article.unidad_inventario},
            "manga": {"id": manga.id, "version": manga.version, "estado": manga.estado, "cantidad_confirmada_un": format(Decimal(manga.cantidad_confirmada_un or 0), "f"), "cantidad_contenida_un": format(Decimal(manga.cantidad_contenida_un or 0), "f"), "trabajo_ot_id": str(manga.trabajo_ot_id) if manga.trabajo_ot_id else None},
            "trabajo": ({"id": str(work.id), "version": work.version, "cantidad_confirmada_un": format(Decimal(work.cantidad_confirmada_un or 0), "f")} if work is not None else None),
            "tramos": [{
                "id": str(segment.id), "version": getattr(segment, "version", None), "secuencia": segment.secuencia,
                "estado": segment.estado, "cantidad_inicio_un": format(Decimal(segment.cantidad_inicio_un or 0), "f"),
                "cantidad_fin_un": format(Decimal(segment.cantidad_fin_un or 0), "f") if segment.cantidad_fin_un is not None else None,
                "cantidad_atribuida_un": format(Decimal(segment.cantidad_atribuida_un or 0), "f"),
                "cantidad_inicio_kg": format(Decimal(segment.cantidad_inicio_kg or 0), "f") if segment.cantidad_inicio_kg is not None else None,
                "cantidad_fin_kg": format(Decimal(segment.cantidad_fin_kg or 0), "f") if segment.cantidad_fin_kg is not None else None,
                "cantidad_atribuida_kg": format(Decimal(segment.cantidad_atribuida_kg or 0), "f") if segment.cantidad_atribuida_kg is not None else None,
            } for segment in segments],
        }
        manifest.append({
            "pesaje_public_id": str(weighing.public_id), "pesaje_id": weighing.id,
            "manga_id": manga.id, "manga_codigo": manga.codigo, "article_id": article.id,
            "article_codigo": article.codigo, "article_unit_before": article.unidad_inventario,
            "estado_fuente": weighing.estado, "estado_manga": manga.estado,
            "neto_kg": format(net_kg, "f"), "pesada_at": weighing.pesada_at.isoformat(),
            "source_operation_id": str(weighing.operation_id), "source_snapshot_hash": _source_snapshot_hash(source_snapshot),
            "source_snapshot": source_snapshot, "atribucion_kg_present": attribution is not None,
            "status": "ALREADY_APPLIED" if already_applied else ("PARTIAL_CONFLICT" if "KG_PROJECTION_PARTIAL" in reasons else "CANDIDATE"),
            "proposed_effect": "ALREADY_PROJECTED" if already_applied else "INGRESO_PRODUCCION_KG",
            "un_projection": {"cantidad_confirmada_un": format(Decimal(manga.cantidad_confirmada_un or 0), "f"), "cantidad_contenida_un": format(un_contained, "f"), "segment_count": len(segments), "reconciliation": "NEUTRALIZE_MUTABLE_PROJECTION" if un_confirmed and not any(reasons) else "NONE_OBSERVED", "preserve_immutable_weighing": True},
            "conflicts": sorted(set(reasons)),
        })
    for source_id in source_pesaje_ids:
        if source_id not in found_ids:
            manifest.append({
                "pesaje_public_id": str(source_id),
                "pesaje_id": None,
                "manga_id": None,
                "manga_codigo": None,
                "article_id": None,
                "article_codigo": None,
                "article_unit_before": None,
                "estado_fuente": None,
                "estado_manga": None,
                "neto_kg": None,
                "pesada_at": None,
                "source_operation_id": None,
                "source_snapshot_hash": None,
                "source_snapshot": None,
                "atribucion_kg_present": False,
                "status": "EXCLUDED",
                "proposed_effect": "NONE",
                "un_projection": None,
                "conflicts": ["SOURCE_NOT_FOUND_OR_NOT_SELECTED"],
            })
    return manifest


def _current_source_ids(session, article_ids):
    """Return every current final for the selected articles under the lock."""
    return set(session.scalars(
        select(ScmPesajeManga.public_id).options(noload("*"))
        .join(ScmManga, ScmManga.id == ScmPesajeManga.manga_id)
        .join(ScmLoteArticulo, ScmLoteArticulo.id == ScmManga.lote_articulo_id)
        .where(
            ScmPesajeManga.estado == "VIGENTE",
            ScmLoteArticulo.articulo_id.in_(article_ids),
        )
    ).all())


def _neutralize_un_projection(
    session, *, manga, weighing, actor, operation, net_kg,
    segments=None, work=None,
    defer_flush=False,
):
    """Move the mutable UN projection to the KG evidence axis.

    The immutable weighing, capture, QR and operational timestamps are never
    edited. Only derived manga/segment/work counters are adjusted, with a
    before/after event so reports can explain the transition.
    """
    amount = Decimal(manga.cantidad_confirmada_un or 0)
    if not amount:
        return None
    if weighing.fuente_cantidad != "PLAN_CONFIRMADO_POR_PESAJE":
        raise ScmServiceError(
            "UN_PROJECTION_RECONCILIATION_REQUIRED",
            "La fuente UN no tiene una regla de neutralización aprobada.",
            status_code=409,
        )
    if segments is None:
        segments = session.scalars(
            select(ScmTramoMangaTrabajo).options(noload("*"))
            .where(ScmTramoMangaTrabajo.manga_id == manga.id)
            .order_by(ScmTramoMangaTrabajo.secuencia)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    if len(segments) > 1:
        raise ScmServiceError(
            "UN_PROJECTION_SEGMENT_AMBIGUOUS",
            "La proyección UN requiere una atribución por tramo explícita.",
            status_code=409,
        )
    segment = segments[0] if segments else None
    if work is None and manga.trabajo_ot_id is not None:
        work = session.scalar(
            select(ScmTrabajoOt).options(noload("*"))
            .where(ScmTrabajoOt.id == manga.trabajo_ot_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    if work is None:
        raise ScmServiceError(
            "UN_PROJECTION_WORK_MISSING",
            "No se puede atribuir la neutralización al trabajo fuente.",
            status_code=409,
        )
    before = {
        "manga_confirmada_un": format(amount, "f"),
        "manga_contenida_un": format(Decimal(manga.cantidad_contenida_un or 0), "f"),
        "trabajo_confirmada_un": format(Decimal(work.cantidad_confirmada_un or 0), "f") if work else None,
        "segment_atribuida_un": format(Decimal(segment.cantidad_atribuida_un or 0), "f") if segment else None,
    }
    if work is not None:
        current = Decimal(work.cantidad_confirmada_un or 0)
        if current < amount:
            raise ScmServiceError(
                "UN_PROJECTION_NEGATIVE",
                "La neutralización excede la contribución UN del trabajo.",
                status_code=409,
            )
        work.cantidad_confirmada_un = current - amount
        work.version += 1
    manga.cantidad_confirmada_un = None
    manga.cantidad_contenida_un = None
    manga.version += 1
    if segment is not None:
        segment.cantidad_atribuida_un = Decimal("0")
        segment.cantidad_fin_un = None
        segment.cantidad_inicio_kg = Decimal("0")
        segment.cantidad_fin_kg = net_kg
        segment.cantidad_atribuida_kg = net_kg
        segment.calidad_evidencia_kg = "MEDIDA_DIRECTA"
    after = {
        "manga_confirmada_un": None,
        "manga_contenida_un": None,
        "trabajo_confirmada_un": format(Decimal(work.cantidad_confirmada_un or 0), "f") if work else None,
        "segment_atribuida_un": format(Decimal(segment.cantidad_atribuida_un or 0), "f") if segment else None,
        "segment_fin_kg": format(net_kg, "f") if segment else None,
        "preserved_weighing_public_id": str(weighing.public_id),
    }
    if not defer_flush:
        session.flush()
    session.add(_event(
        "MANGA", manga.id, "KG009_UN_PROJECTION_NEUTRALIZED", actor, operation,
        {"before": before, "after": after},
    ))
    return {"before": before, "after": after}


def _legacy_blockers_batch(session, article_ids):
    """Lock and inspect legacy UN blockers in bounded batches."""
    blockers = {article_id: [] for article_id in article_ids}
    balances = session.scalars(
        select(ScmSaldoInventario).options(noload("*")).where(
            ScmSaldoInventario.articulo_scm_id.in_(article_ids)
        ).order_by(ScmSaldoInventario.articulo_scm_id, ScmSaldoInventario.id).with_for_update()
    ).all() if article_ids else []
    for row in balances:
        if any(
            Decimal(value or 0) != 0
            for value in (row.cantidad_fisica, row.cantidad_reservada, row.cantidad_no_disponible)
        ):
            blockers[row.articulo_scm_id].append("LEGACY_UN_BALANCE_NONZERO")
    active_existence = session.scalars(
        select(ScmExistenciaManga.articulo_scm_id).options(noload("*")).where(
            ScmExistenciaManga.articulo_scm_id.in_(article_ids),
            ScmExistenciaManga.estado_logistico != "REVERSADA",
        )
    ).all() if article_ids else []
    for article_id in active_existence:
        blockers[article_id].append("LEGACY_UN_EXISTENCE_ACTIVE")
    logistic_units = session.scalars(
        select(ScmUnidadLogisticaInventario.articulo_scm_id).options(noload("*")).where(
            ScmUnidadLogisticaInventario.articulo_scm_id.in_(article_ids)
        )
    ).all() if article_ids else []
    for article_id in logistic_units:
        blockers[article_id].append("LEGACY_LOGISTIC_UNIT")
    return blockers


def _article_manifest(session, article_ids, *, context=None):
    result = []
    articles = session.scalars(
        select(ScmArticulo).options(noload("*"))
        .where(ScmArticulo.id.in_(article_ids))
        .order_by(ScmArticulo.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    by_id = {article.id: article for article in articles}
    if context is not None:
        context["article_objects"] = by_id
    legacy_blockers = _legacy_blockers_batch(
        session,
        [article.id for article in articles if article.unidad_inventario == "UN"],
    )
    for article_id in article_ids:
        article = by_id.get(article_id)
        conflicts = []
        if article is None:
            conflicts.append("ARTICLE_NOT_FOUND")
        else:
            if article.clase not in KG_ARTICLE_CLASSES:
                conflicts.append("KG_CLASS_NOT_ALLOWED")
            if not article.activo:
                conflicts.append("ARTICLE_INACTIVE")
            if article.unidad_inventario == "UN":
                conflicts.extend(legacy_blockers.get(article.id, []))
        result.append({
            "article_id": article_id,
            "codigo": article.codigo if article else None,
            "clase": article.clase if article else None,
            "activo": article.activo if article else None,
            "unidad_before": article.unidad_inventario if article else None,
            "unidad_after": "KG" if article and article.clase in KG_ARTICLE_CLASSES else None,
            "conflicts": sorted(set(conflicts)),
        })
    return result


def preview_kg_recovery(session, *, actor_id, article_ids, reason, source_pesaje_ids):
    """Return a deterministic, non-mutating manifest for explicit articles."""
    actor = load_actor(session, actor_id, capability=RECOVERY_CAPABILITY)
    ids = _article_ids(article_ids)
    source_ids = _source_ids(source_pesaje_ids)
    reason = required_text(reason, field="motivo", max_length=500)
    deadline = _recovery_deadline()
    _prepare_recovery_transaction(session)
    acquire_kg_productive_write_lock(session)
    try:
        _check_recovery_deadline(deadline)
        articles = _article_manifest(session, ids)
        sources = _source_manifest(session, ids, source_ids)
        _check_recovery_deadline(deadline)
        omitted_sources = sorted(
            str(value) for value in _current_source_ids(session, ids) - set(source_ids)
        )
        response = {
            "mode": "DRY_RUN",
            "actor_id": actor.id,
            "motivo": reason,
            "articles": articles,
            "sources": sources,
            "omitted_current_sources": omitted_sources,
            "eligible_sources": [item for item in sources if not item["conflicts"]],
            "release_constraint": "no_habilitar_en_planta",
            "apply_allowed": (
                not omitted_sources
                and not any(item["conflicts"] for item in articles + sources)
            ),
            "projection_policy": "PRESERVE_UN_FACTS; RECONCILIATION_REQUIRED_ON_CONFLICT",
        }
        session.rollback()
        return response
    except Exception:
        session.rollback()
        raise


def apply_kg_recovery(
    session,
    *,
    actor_id,
    article_ids,
    reason,
    operation_id,
    source_pesaje_ids,
    source_snapshot_hashes=None,
):
    """Apply only a clean manifest, preserving all original UN facts."""
    actor = load_actor(session, actor_id, capability=RECOVERY_CAPABILITY)
    ids = _article_ids(article_ids)
    source_ids = _source_ids(source_pesaje_ids)
    reason = required_text(reason, field="motivo", max_length=500)
    expected_hashes = {
        str(key): str(value)
        for key, value in (source_snapshot_hashes or {}).items()
    }
    expected_source_keys = {str(value) for value in source_ids}
    parent_id = _uuid(operation_id, field="operation_id")
    deadline = _recovery_deadline()
    _prepare_recovery_transaction(session)
    command = {
        "article_ids": ids,
        "source_pesaje_ids": [str(value) for value in source_ids],
        "source_snapshot_hashes": {
            str(key): str(value) for key, value in expected_hashes.items()
        },
        "motivo": reason,
    }
    try:
        # Reserve the top-level idempotency operation first.  This is the
        # common prefix with weighing/pilot and prevents a same-UUID
        # advisory-vs-operation deadlock across endpoints.
        parent, replay = _reserve_operation(
            session, parent_id, RECOVERY_ENDPOINT, actor, command
        )
        if replay is not None:
            # The advisory lock is transaction-scoped.  A replay has no
            # writes to commit, so explicitly end the transaction before
            # returning the copied response to release the persistent lock.
            session.rollback()
            return replay
        # The advisory lock remains before all article, manga, segment and
        # source row locks.  Only the reservation prefix is intentionally
        # outside this critical section.
        acquire_kg_productive_write_lock(session)
        if set(expected_hashes) != expected_source_keys:
            raise ScmServiceError(
                "SOURCE_SNAPSHOT_HASHES_REQUIRED",
                "La aplicación requiere el hash de cada fuente seleccionada.",
                status_code=422,
                details={"missing": sorted(expected_source_keys - set(expected_hashes)),
                         "unexpected": sorted(set(expected_hashes) - expected_source_keys)},
            )
        _check_recovery_deadline(deadline)
        recovery_context = {}
        articles = _article_manifest(session, ids, context=recovery_context)
        sources = _source_manifest(
            session, ids, source_ids, context=recovery_context
        )
        _check_recovery_deadline(deadline)
        omitted_sources = sorted(
            str(value) for value in _current_source_ids(session, ids) - set(source_ids)
        )
        if omitted_sources:
            raise ScmServiceError(
                "KG_RECOVERY_SELECTION_STALE",
                "Existen finales vigentes del artículo fuera de la selección explícita.",
                status_code=409,
                details={"omitted_current_sources": omitted_sources},
            )
        for source in sources:
            expected = expected_hashes.get(source["pesaje_public_id"])
            if expected != source.get("source_snapshot_hash"):
                source["conflicts"].append("SOURCE_SNAPSHOT_CHANGED")
                source["conflicts"] = sorted(set(source["conflicts"]))
        conflicts = [
            {"scope": "article", **item} for item in articles if item["conflicts"]
        ] + [
            {"scope": "source", **item} for item in sources if item["conflicts"]
        ]
        if conflicts:
            raise ScmServiceError(
                "KG_RECOVERY_CONFLICT",
                "El manifiesto contiene fuentes o dependencias no reconciliables.",
                status_code=409,
                details={"conflicts": conflicts},
            )
        for article_id in ids:
            article = recovery_context["article_objects"].get(article_id)
            if article.unidad_inventario != "KG":
                activate_article_for_kg(session, article_id=article_id)
        applied = []
        location_cache = {}
        balance_cache = {}
        for source in sources:
            _check_recovery_deadline(deadline)
            if source["status"] == "ALREADY_APPLIED":
                applied.append({
                    "pesaje_public_id": source["pesaje_public_id"],
                    "status": "ALREADY_APPLIED",
                    "delta_kg": "0.000",
                })
                continue
            weighing = recovery_context["weighings"].get(
                UUID(source["pesaje_public_id"])
            )
            manga = recovery_context["mangas"].get(weighing.manga_id)
            segments = recovery_context["segments"].get(manga.id, [])
            child_id = uuid5(RECOVERY_NAMESPACE, f"{parent_id}:{source['pesaje_public_id']}")
            child, child_replay = _reserve_operation(
                session,
                child_id,
                f"{RECOVERY_ENDPOINT}/{source['pesaje_public_id']}",
                actor,
                {
                    "parent_operation_id": str(parent_id),
                    "pesaje_public_id": source["pesaje_public_id"],
                    "source_snapshot_hash": source["source_snapshot_hash"],
                },
            )
            if child_replay is not None:
                applied.append(child_replay)
                continue
            reconciliation = _neutralize_un_projection(
                session,
                manga=manga,
                weighing=weighing,
                actor=actor,
                operation=child,
                net_kg=Decimal(weighing.peso_fisico_neto_kg),
                segments=segments,
                work=recovery_context["works"].get(manga.trabajo_ot_id),
                defer_flush=True,
            )
            attribution = recovery_context["attributions"].get(weighing.id)
            if attribution is None:
                if len(segments) > 1:
                    raise ScmServiceError(
                        "KG_RECOVERY_SEGMENT_AMBIGUOUS",
                        "La fuente tiene múltiples tramos sin atribución KG canónica.",
                        status_code=409,
                        details={"pesaje_public_id": source["pesaje_public_id"]},
                    )
                attribution = ScmAtribucionProduccionKg(
                    manga_id=manga.id,
                    pesaje_id=weighing.id,
                    tramo_id=segments[-1].id if segments else None,
                    trabajo_ot_id=manga.trabajo_ot_id,
                    tipo="NETO_MEDIDO",
                    cantidad_kg=Decimal(weighing.peso_fisico_neto_kg),
                    calidad="MEDIDA_DIRECTA",
                    base_json={
                        "fuente": "KG009_RECOVERY",
                        "pesaje_public_id": source["pesaje_public_id"],
                        "source_snapshot_hash": source["source_snapshot_hash"],
                        "motivo": reason,
                        # Match the canonical production-evidence path: this
                        # recovery restores measured NET only and leaves BOM
                        # attribution pending for a separate reconciliation.
                        "bom": "PENDIENTE_BOM",
                        "parent_operation_id": str(parent_id),
                    },
                    actor_id=actor.id,
                    operation_id=child_id,
                )
                session.add(attribution)
                weighing.atribucion_kg_estado = "PENDIENTE_BOM"
                weighing.atribucion_kg_base_json = attribution.base_json
            article_obj = recovery_context["articles"].get(source["article_id"])
            location = location_cache.get(article_obj.clase)
            if location is None:
                location = _production_location(
                    session, article_class=article_obj.clase
                )
                location_cache[article_obj.clase] = location
            inventory = sync_kg_production_inventory(
                session,
                actor_id=actor.id,
                manga=manga,
                net_kg=weighing.peso_fisico_neto_kg,
                operation_id=child_id,
                source_type="PESAJE_FINAL",
                source_id=weighing.public_id,
                source_at=weighing.pesada_at,
                final=True,
                force=True,
                article=article_obj,
                location=location,
                balance_cache=balance_cache,
                existence_cache=recovery_context["existences"],
                defer_flush=True,
            )
            item = {
                "pesaje_public_id": source["pesaje_public_id"],
                "manga_codigo": manga.codigo,
                "neto_kg": source["neto_kg"],
                "delta_kg": inventory["delta_kg"],
                "operation_id": str(child_id),
                "parent_operation_id": str(parent_id),
                "source_snapshot_hash": source["source_snapshot_hash"],
                "atribucion_preservada": True,
                "un_projection_reconciliation": reconciliation,
            }
            _complete_operation(child, item, 201)
            session.add(_event("PESAJE_MANGA", weighing.id, "KG009_RECOVERY_APPLIED", actor, child, item))
            applied.append(item)
        response = {
            "mode": "APPLIED",
            "operation_id": str(parent_id),
            "items": applied,
            "articles": articles,
            "source_policy": "SIMPLE_VIGENTE_FINAL_ONLY",
            "release_constraint": "no_habilitar_en_planta",
        }
        _complete_operation(parent, response, 200)
        session.add(_event("KG_RECOVERY", parent_id, "KG009_RECOVERY_APPLIED", actor, parent, response))
        # Materialize deferred writes before the final deadline decision so an
        # expired unit of work is rolled back in full.
        session.flush()
        _check_recovery_deadline(deadline)
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def recover_kg_pesajes(
    session,
    *,
    actor_id,
    article_ids,
    reason,
    source_pesaje_ids,
    operation_id=None,
    apply=False,
    source_snapshot_hashes=None,
):
    """Unified command entry point used by the CLI and integration tests."""
    if apply:
        if operation_id is None:
            raise ScmServiceError("OPERATION_ID_REQUIRED", "La aplicacion requiere operation_id UUID.", status_code=422)
        return apply_kg_recovery(
            session,
            actor_id=actor_id,
            article_ids=article_ids,
            reason=reason,
            operation_id=operation_id,
            source_pesaje_ids=source_pesaje_ids,
            source_snapshot_hashes=source_snapshot_hashes,
        )
    return preview_kg_recovery(
        session,
        actor_id=actor_id,
        article_ids=article_ids,
        reason=reason,
        source_pesaje_ids=source_pesaje_ids,
    )


__all__ = [
    "apply_kg_recovery",
    "preview_kg_recovery",
    "recover_kg_pesajes",
]

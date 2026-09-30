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
from app.services.scm_kg_production_service import sync_kg_production_inventory
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
        select(ScmSaldoInventario).where(
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
        select(ScmExistenciaManga.id).where(
            ScmExistenciaManga.articulo_scm_id == article_id,
            ScmExistenciaManga.estado_logistico != "REVERSADA",
        )
    ) is not None:
        return ["LEGACY_UN_EXISTENCE_ACTIVE"]
    if session.scalar(
        select(ScmUnidadLogisticaInventario.id).where(
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
    movement_saldo = getattr(movement, "saldo", None)
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
        select(ScmTrabajoOt)
        .where(ScmTrabajoOt.id == work_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if work is None:
        return None, [], None
    mangas = session.scalars(
        select(ScmManga)
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


def _source_manifest(session, article_ids, source_pesaje_ids):
    rows = session.scalars(
        select(ScmPesajeManga)
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
    manifest = []
    found_ids = set()
    work_reconciliation = {}
    for weighing in rows:
        found_ids.add(weighing.public_id)
        manga = session.scalar(
            select(ScmManga)
            .where(ScmManga.id == weighing.manga_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        lote = session.scalar(
            select(ScmLoteArticulo)
            .where(ScmLoteArticulo.id == manga.lote_articulo_id)
            .execution_options(populate_existing=True)
        )
        article = session.scalar(
            select(ScmArticulo)
            .where(ScmArticulo.id == lote.articulo_id)
            .execution_options(populate_existing=True)
        )
        reasons = []
        segments = session.scalars(
            select(ScmTramoMangaTrabajo)
            .where(ScmTramoMangaTrabajo.manga_id == manga.id)
            .order_by(ScmTramoMangaTrabajo.secuencia)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
        work = None
        observed_work_un = None
        if manga.trabajo_ot_id is not None:
            cached = work_reconciliation.get(manga.trabajo_ot_id)
            if cached is None:
                work, _work_mangas, observed_work_un = _lock_work_reconciliation(
                    session, manga.trabajo_ot_id
                )
                work_reconciliation[manga.trabajo_ot_id] = (
                    work, observed_work_un
                )
            else:
                work, observed_work_un = cached
        if manga.estado not in {
            "PESADA", "ETIQUETADA_FINAL", "PENDIENTE_RECEPCION_ALMACEN"
        }:
            reasons.append("MANGA_LOGISTICS_NOT_SIMPLE")
        if session.scalar(select(ScmControlPesoManga.id).where(ScmControlPesoManga.manga_id == manga.id)):
            reasons.append("CONTROL_PRESENT")
        if session.scalar(select(ScmCorreccionPesajeManga.id).where(ScmCorreccionPesajeManga.pesaje_id == weighing.id)):
            reasons.append("WEIGHING_CORRECTION_PRESENT")
        if session.scalar(select(ScmAnulacionPesajeManga.id).join(
            ScmPesajeManga,
            ScmPesajeManga.id == ScmAnulacionPesajeManga.pesaje_id,
        ).where(
            ScmPesajeManga.manga_id == manga.id,
        )):
            reasons.append("WEIGHING_ANNULMENT_PRESENT")
        if session.scalar(select(ScmReaperturaManga.id).where(
            or_(
                ScmReaperturaManga.pesaje_id == weighing.id,
                ScmReaperturaManga.manga_id == manga.id,
            )
        )):
            reasons.append("REOPENING_PRESENT")
        if session.scalar(select(ScmCorreccionAsignacionManga.id).where(ScmCorreccionAsignacionManga.manga_id == manga.id)):
            reasons.append("ASSIGNMENT_CORRECTION_PRESENT")
        if session.scalar(select(ScmExistenciaManga.id).where(
            ScmExistenciaManga.manga_id == manga.id,
        )) is not None:
            reasons.append("LEGACY_LOGISTICS_PRESENT")
        active_kg = session.scalar(select(ScmExistenciaMangaKg).where(
            ScmExistenciaMangaKg.manga_id == manga.id,
            ScmExistenciaMangaKg.estado_logistico != "REVERSADA",
        ).with_for_update().execution_options(populate_existing=True))
        if active_kg is None and session.scalar(select(ScmExistenciaMangaKg.id).where(
            ScmExistenciaMangaKg.manga_id == manga.id,
        )) is not None:
            reasons.append("KG_LOGISTICS_HISTORY_PRESENT")
        movement = session.scalar(select(ScmMovimientoInventarioKg).where(
            ScmMovimientoInventarioKg.pesaje_public_id == weighing.public_id
        ).order_by(ScmMovimientoInventarioKg.id.desc()).with_for_update().execution_options(
            populate_existing=True
        ))
        un_confirmed = Decimal(manga.cantidad_confirmada_un or 0)
        un_contained = Decimal(manga.cantidad_contenida_un or 0)
        if work is not None and observed_work_un != Decimal(work.cantidad_confirmada_un or 0):
            reasons.append("UN_WORK_RECONCILIATION_REQUIRED")
        # PLAN_CONFIRMADO_POR_PESAJE is the known mutable UN projection that
        # can be neutralized for a simple one-segment piece. Explicit counts,
        # corrections and WIP/ambiguous paths stay out of this increment.
        derived_un_projection = _derived_un_projection(
            manga, weighing, un_confirmed, un_contained
        )
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
            if (
                Decimal(segment.cantidad_inicio_un or 0) != 0
                or Decimal(segment.cantidad_fin_un or 0) != un_confirmed
            ):
                reasons.append("UN_PROJECTION_SEGMENT_MISMATCH")
            if segment.trabajo_ot_id != manga.trabajo_ot_id:
                reasons.append("UN_PROJECTION_OWNERSHIP_MISMATCH")
            if any(
                Decimal(value or 0) != 0
                for value in (
                    segment.cantidad_inicio_kg,
                    segment.cantidad_fin_kg,
                    segment.cantidad_atribuida_kg,
                )
            ):
                reasons.append("KG_SEGMENT_ALREADY_POPULATED")
        if un_confirmed != 0 and segments and Decimal(segments[-1].cantidad_atribuida_un or 0) not in {Decimal("0"), un_confirmed}:
            reasons.append("UN_PROJECTION_DELTA_MISMATCH")
        attribution = session.scalar(select(ScmAtribucionProduccionKg).where(
            ScmAtribucionProduccionKg.pesaje_id == weighing.id,
            ScmAtribucionProduccionKg.tipo == "NETO_MEDIDO",
        ).with_for_update().execution_options(populate_existing=True))
        # A canonical NETO_MEDIDO is already the authoritative KG evidence,
        # even when an older projection did not leave a visible movement.  A
        # recovery must prove this no-op instead of creating a second
        # attribution under a new operation.
        net_kg = Decimal(weighing.peso_fisico_neto_kg)
        kg_projection_complete = _kg_projection_complete(
            active_kg,
            movement,
            attribution,
            net_kg,
            weighing.public_id,
            article_id=article.id,
            manga_id=manga.id,
            weighing_id=weighing.id,
            owner_work_id=manga.trabajo_ot_id,
        )
        if _source_un_quantity_conflict(
            manga.cantidad_confirmada_un,
            weighing.cantidad_confirmada,
            kg_projection_complete,
            work_quantity=(work.cantidad_confirmada_un if work is not None else None),
            contained_quantity=manga.cantidad_contenida_un,
        ):
            reasons.append("UN_SOURCE_MANGA_QUANTITY_MISMATCH")
        already_applied = kg_projection_complete
        if (active_kg is not None or movement is not None or attribution is not None) and not kg_projection_complete:
            reasons.append("KG_PROJECTION_PARTIAL")
        source_snapshot = {
            "pesaje_public_id": str(weighing.public_id),
            "operation_id": str(weighing.operation_id),
            "capture_id": str(weighing.capture_id),
            "neto_kg": format(Decimal(weighing.peso_fisico_neto_kg), "f"),
            "cantidad_confirmada": format(Decimal(weighing.cantidad_confirmada or 0), "f"),
            "fuente_cantidad": weighing.fuente_cantidad,
            "pesada_at": weighing.pesada_at.isoformat(),
            "estado": weighing.estado,
            "snapshots": weighing.snapshots_json,
            "article": {
                "id": article.id,
                "version": article.version,
                "clase": article.clase,
                "unidad_inventario": article.unidad_inventario,
            },
            "manga": {
                "id": manga.id,
                "version": manga.version,
                "estado": manga.estado,
                "cantidad_confirmada_un": format(Decimal(manga.cantidad_confirmada_un or 0), "f"),
                "cantidad_contenida_un": format(Decimal(manga.cantidad_contenida_un or 0), "f"),
                "trabajo_ot_id": str(manga.trabajo_ot_id) if manga.trabajo_ot_id else None,
            },
            "trabajo": ({
                "id": str(work.id),
                "version": work.version,
                "cantidad_confirmada_un": format(Decimal(work.cantidad_confirmada_un or 0), "f"),
            } if work is not None else None),
            "tramos": [
                {
                    "id": str(segment.id),
                    "version": getattr(segment, "version", None),
                    "secuencia": segment.secuencia,
                    "estado": segment.estado,
                    "cantidad_inicio_un": format(Decimal(segment.cantidad_inicio_un or 0), "f"),
                    "cantidad_fin_un": format(Decimal(segment.cantidad_fin_un or 0), "f") if segment.cantidad_fin_un is not None else None,
                    "cantidad_atribuida_un": format(Decimal(segment.cantidad_atribuida_un or 0), "f"),
                    "cantidad_inicio_kg": format(Decimal(segment.cantidad_inicio_kg or 0), "f") if segment.cantidad_inicio_kg is not None else None,
                    "cantidad_fin_kg": format(Decimal(segment.cantidad_fin_kg or 0), "f") if segment.cantidad_fin_kg is not None else None,
                    "cantidad_atribuida_kg": format(Decimal(segment.cantidad_atribuida_kg or 0), "f") if segment.cantidad_atribuida_kg is not None else None,
                }
                for segment in segments
            ],
        }
        source_hash = _source_snapshot_hash(source_snapshot)
        manifest.append({
            "pesaje_public_id": str(weighing.public_id),
            "pesaje_id": weighing.id,
            "manga_id": manga.id,
            "manga_codigo": manga.codigo,
            "article_id": article.id,
            "article_codigo": article.codigo,
            "article_unit_before": article.unidad_inventario,
            "estado_fuente": weighing.estado,
            "estado_manga": manga.estado,
            "neto_kg": format(Decimal(weighing.peso_fisico_neto_kg), "f"),
            "pesada_at": weighing.pesada_at.isoformat(),
            "source_operation_id": str(weighing.operation_id),
            "source_snapshot_hash": source_hash,
            "source_snapshot": source_snapshot,
            "atribucion_kg_present": attribution is not None,
            "status": "ALREADY_APPLIED" if already_applied else (
                "PARTIAL_CONFLICT" if "KG_PROJECTION_PARTIAL" in reasons else "CANDIDATE"
            ),
            "proposed_effect": "ALREADY_PROJECTED" if already_applied else "INGRESO_PRODUCCION_KG",
            "un_projection": {
                "cantidad_confirmada_un": format(Decimal(manga.cantidad_confirmada_un or 0), "f"),
                "cantidad_contenida_un": format(un_contained, "f"),
                "segment_count": len(segments),
                "reconciliation": "NEUTRALIZE_MUTABLE_PROJECTION" if un_confirmed and not any(reasons) else "NONE_OBSERVED",
                "preserve_immutable_weighing": True,
            },
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
        select(ScmPesajeManga.public_id)
        .join(ScmManga, ScmManga.id == ScmPesajeManga.manga_id)
        .join(ScmLoteArticulo, ScmLoteArticulo.id == ScmManga.lote_articulo_id)
        .where(
            ScmPesajeManga.estado == "VIGENTE",
            ScmLoteArticulo.articulo_id.in_(article_ids),
        )
    ).all())


def _neutralize_un_projection(session, *, manga, weighing, actor, operation, net_kg):
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
    segments = session.scalars(
        select(ScmTramoMangaTrabajo)
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
    work = session.scalar(
        select(ScmTrabajoOt)
        .where(ScmTrabajoOt.id == manga.trabajo_ot_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ) if manga.trabajo_ot_id is not None else None
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
    session.flush()
    session.add(_event(
        "MANGA", manga.id, "KG009_UN_PROJECTION_NEUTRALIZED", actor, operation,
        {"before": before, "after": after},
    ))
    return {"before": before, "after": after}


def _article_manifest(session, article_ids):
    result = []
    articles = session.scalars(
        select(ScmArticulo)
        .where(ScmArticulo.id.in_(article_ids))
        .order_by(ScmArticulo.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    by_id = {article.id: article for article in articles}
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
                conflicts.extend(_legacy_blockers(session, article.id))
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
        articles = _article_manifest(session, ids)
        sources = _source_manifest(session, ids, source_ids)
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
            article = session.scalar(
                select(ScmArticulo)
                .where(ScmArticulo.id == article_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if article.unidad_inventario != "KG":
                activate_article_for_kg(session, article_id=article_id)
        applied = []
        for source in sources:
            _check_recovery_deadline(deadline)
            if source["status"] == "ALREADY_APPLIED":
                applied.append({
                    "pesaje_public_id": source["pesaje_public_id"],
                    "status": "ALREADY_APPLIED",
                    "delta_kg": "0.000",
                })
                continue
            weighing = session.scalar(select(ScmPesajeManga).where(
                ScmPesajeManga.public_id == UUID(source["pesaje_public_id"])
            ).with_for_update().execution_options(populate_existing=True))
            manga = session.scalar(
                select(ScmManga)
                .where(ScmManga.id == weighing.manga_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            segments = session.scalars(
                select(ScmTramoMangaTrabajo)
                .where(ScmTramoMangaTrabajo.manga_id == manga.id)
                .order_by(ScmTramoMangaTrabajo.secuencia)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).all()
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
            )
            attribution = session.scalar(select(ScmAtribucionProduccionKg).where(
                ScmAtribucionProduccionKg.pesaje_id == weighing.id,
                ScmAtribucionProduccionKg.tipo == "NETO_MEDIDO",
            ))
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

"""KG production evidence and document closure.

This module deliberately keeps physical kg facts separate from the legacy UN
projections.  It does not create inventory movements, consume BOM sources, or
turn an estimate into a measured result.
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import noload, selectinload

from app.models.registro import RegistroDiarioProduccion
from app.models.scm_inline_wip import ScmSaldoWipSalida
from app.models.scm_material_execution import ScmRequerimientoMaterial
from app.models.scm_prepared_material import ScmRequerimientoMaterialPreparado
from app.models.scm_ot import (
    ScmAtribucionProduccionKg,
    ScmCierreProductivoKg,
    ScmControlPesoManga,
    ScmCorreccionAsignacionManga,
    ScmLoteArticulo,
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
    acquire_kg_productive_write_lock,
    load_actor,
    reject_unknown_fields,
)
from app.services.scm_manga_assignment_projection import (
    effective_work,
    effective_work_for_segment,
)


KG_QUANTUM = Decimal("0.001")
REFERENCE_KG_QUANTUM = Decimal("0.000001")


def _effective_attribution_work(row):
    manga = getattr(row, "manga", None)
    segment = getattr(row, "tramo", None)
    if manga is not None and segment is not None:
        return effective_work_for_segment(manga, segment)
    return effective_work(manga) if manga is not None else None


def automatic_kg_intake_enabled():
    """Return the explicit W1 pilot switch without enabling it implicitly."""
    try:
        from flask import current_app
        return bool(current_app.config.get("KG_AUTOMATIC_INTAKE_ENABLED", False))
    except RuntimeError:
        return False


def _complete_active_control_recovery(session, manga, current_operation_id, cutoff, source_at):
    """Recognize only the exact audited control-history bundle for this manga.

    Recovery is a projection of an existing control, never a rewrite of that
    control. The current manga must satisfy the full live control/final and
    first-segment continuity checks. The companion manga is checked only
    against its immutable source control and original recovery movement,
    existence references, and complete child/parent operation audit bundle;
    later legitimate weighing, finalization, custody, or consumption does not
    change that historical proof. Partial or drifted bundles stay blocked.
    """
    import hashlib
    import json
    from datetime import datetime, timezone
    from uuid import UUID, uuid5
    from sqlalchemy import inspect as sa_inspect
    from app.models.scm_auditoria import ScmEvento, ScmOperacion
    from app.models.scm_inventory_kg import ScmExistenciaMangaKg, ScmMovimientoInventarioKg, ScmSaldoInventarioKg
    from app.services.scm_ot_service import _operation_hash
    namespace = UUID("e898117f-cac3-4cbd-aead-d43c9e8e3f18")
    expected_manifest_sha256 = "9971fc19e26d1490fc313620f423612750cda734906dc0afb53eee7a073bcc21"
    def utc(value):
        if not isinstance(value, datetime):
            return None
        return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value).astimezone(timezone.utc)

    def row_hash(row, excluded=()):
        values = {}
        for attribute in sa_inspect(type(row)).column_attrs:
            key = attribute.key
            if key in excluded:
                continue
            value = getattr(row, key)
            if isinstance(value, datetime):
                value = value.isoformat()
                if "." in value:
                    head, tail = value.split(".", 1)
                    fraction, suffix = tail[:6], tail[6:]
                    value = head + "." + fraction.rstrip("0") + suffix if fraction.rstrip("0") else head + suffix
            elif isinstance(value, Decimal):
                value = float(value)
            elif hasattr(value, "hex") and type(value).__name__ == "UUID":
                value = str(value)
            values[key] = value
        return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()).hexdigest()

    controls = session.scalars(
        select(ScmControlPesoManga)
        .where(ScmControlPesoManga.manga_id == manga.id)
        .order_by(ScmControlPesoManga.created_at, ScmControlPesoManga.id)
    ).all()
    segments = session.scalars(
        select(ScmTramoMangaTrabajo)
        .where(ScmTramoMangaTrabajo.manga_id == manga.id)
        .order_by(ScmTramoMangaTrabajo.secuencia)
    ).all()
    if not controls or len(segments) != 1:
        return False
    control, segment = controls[0], segments[0]
    permitted_sources = {
        "970a4edc-f6b8-4f65-bc07-47dcf63274c8": (831, 258, Decimal("5.700"), "90497e8553631128bda56554ec1a9755a8cd4e0e87779621c1bc98f6a0ddd15e", "52884b9e96fd1541d5198fde5c6db329cc5b936f857f2498c73f8f4343aa0c99", "86d855aa5c0b535e93dc863250d01609c9d4c4d3ea6eab2cab4436d49b939d07", "859218c7481ee3d02d866e6e7ac7e50cc3dff4596cc0287295abd92f8b6038b7", "00f92bebed99cc94772893761c8be0b9116bbcd4b19a7e04d7d5c7ee7be2d101", "e4513155-7349-44b8-89bd-4fcb8cda4e88", "be72e1d4-e5b5-48b5-b606-2a5920156484"),
        "0f39985e-c3ad-4c93-9223-4a07d8a208dd": (1642, 224, Decimal("4.700"), "8be0d1fc467091a63e1e648f5cd55a05a07fee1da1f2c3f30029696f8742394d", "abb3ac7a835e413120dd8cc6db4829ebb461956026f8c9aa698062abd464ab78", "572d16daec82f0ba86319a4a1403f1d6cd030cdc8191a3be0402f40fc561c62e", "98404eb1c3c63d56288f130dc822f6a202993a6d5dcc58be5690df2a88006809", "d6a3c527d152e3565da927af1c10ef14ee51bca300067b9e06561c4ac95cb975", "0a34bda6-bdef-4977-8412-066ebecc857b", "4ef9641e-1631-4f69-bf00-c7df0a485471"),
    }
    def peer_recovery_bundle_valid(peer_source_id):
        """Verify the immutable recovery audit bundle for both sources."""
        expected_source_ids = [
            "0f39985e-c3ad-4c93-9223-4a07d8a208dd",
            "970a4edc-f6b8-4f65-bc07-47dcf63274c8",
        ]
        if peer_source_id not in expected_source_ids:
            return False
        expected_child_ids = [
            str(uuid5(namespace, "active-control-source:" + source_id))
            for source_id in expected_source_ids
        ]
        parent_id = uuid5(namespace, "active-control-manifest:" + expected_manifest_sha256)
        parent_request = {
            "manifest_sha256": expected_manifest_sha256,
            "source_control_public_ids": expected_source_ids,
            "source_count": 2,
            "delta_kg": "10.400",
            "actor_id": 1,
            "location_id": 9,
        }
        recovery_reason = (
            "Recuperacion auditada de control activo; NET historico sin unidad "
            "fisica, QR ni atribucion NETO_MEDIDO."
        )
        expected_sources = []
        for source_id in expected_source_ids:
            permitted = permitted_sources[source_id]
            peer = session.get(ScmManga, permitted[0])
            if peer is None or str(peer.public_id) != permitted[8]:
                return False
            control_rows = session.scalars(select(ScmControlPesoManga).where(
                ScmControlPesoManga.manga_id == peer.id,
                ScmControlPesoManga.public_id == UUID(source_id),
            )).all()
            if len(control_rows) != 1:
                return False
            baseline = control_rows[0]
            net = Decimal(baseline.peso_neto_kg).quantize(KG_QUANTUM)
            if (
                str(baseline.public_id) != source_id
                or baseline.manga_id != permitted[0]
                or net != permitted[2]
                or row_hash(baseline) != permitted[3]
                or baseline.tipo != "AVANCE_KG"
                or baseline.unidad_evidencia != "UN"
                or baseline.conteo_acumulado_un is not None
                or utc(baseline.pesado_at) is None
            ):
                return False

            child_id = uuid5(namespace, "active-control-source:" + source_id)
            existence_rows = session.scalars(select(ScmExistenciaMangaKg).where(
                ScmExistenciaMangaKg.manga_id == peer.id
            )).all()
            movement_rows = session.scalars(select(ScmMovimientoInventarioKg).where(
                ScmMovimientoInventarioKg.operation_id == child_id
            )).all()
            source_movements = session.scalars(select(ScmMovimientoInventarioKg).where(
                ScmMovimientoInventarioKg.referencia_tipo == "CONTROL_PESO_MANGA",
                ScmMovimientoInventarioKg.referencia_id == source_id,
            )).all()
            child_events = session.scalars(select(ScmEvento).where(
                ScmEvento.operation_id == child_id
            )).all()
            if (
                len(existence_rows) != 1
                or len(movement_rows) != 1
                or len(source_movements) != 1
                or len(child_events) != 1
            ):
                return False
            existence = existence_rows[0]
            recovery_move = movement_rows[0]
            child_event = child_events[0]
            if source_movements[0].id != recovery_move.id:
                return False

            # Pin the movement to its original, unique article/location ledger
            # scope. The existence may have moved; its current saldo is irrelevant.
            original_saldos = session.scalars(select(ScmSaldoInventarioKg).where(
                ScmSaldoInventarioKg.articulo_scm_id == permitted[1],
                ScmSaldoInventarioKg.ubicacion_id == 9,
            )).all()
            if len(original_saldos) != 1:
                return False
            original_saldo = original_saldos[0]
            # Reconstruct only the immutable ledger prefix through this
            # recovery movement. Later postings and the live saldo snapshot
            # may legitimately change after the peer bundle was recorded.
            original_chain = session.scalars(select(ScmMovimientoInventarioKg).where(
                ScmMovimientoInventarioKg.saldo_id == original_saldo.id
            ).order_by(ScmMovimientoInventarioKg.created_at, ScmMovimientoInventarioKg.id)).all()
            running_original = Decimal("0.000")
            recovery_result = None
            for ledger_row in original_chain:
                if ledger_row.saldo_fisico_resultante_kg is None:
                    return False
                running_original += Decimal(ledger_row.cantidad_delta_kg).quantize(KG_QUANTUM)
                running_original = running_original.quantize(KG_QUANTUM)
                recorded_result = Decimal(ledger_row.saldo_fisico_resultante_kg).quantize(KG_QUANTUM)
                if running_original < 0 or recorded_result != running_original:
                    return False
                if ledger_row.id == recovery_move.id:
                    recovery_result = recorded_result
                    break
            if recovery_result is None:
                return False

            recovery_hash = hashlib.sha256(
                f"CONTROL_PESO:{source_id}:{net}:".encode()
            ).hexdigest()
            if (
                existence.resuelta_por != "KG_HISTORY_RECOVERY"
                or existence.manga_id != peer.id
                or existence.articulo_scm_id != permitted[1]
                or existence.operation_id != child_id
                or existence.movimiento_ingreso_id != recovery_move.id
                or recovery_move.saldo_id != original_saldo.id
                or recovery_move.operation_id != child_id
                or recovery_move.tipo != "INGRESO_PRODUCCION"
                or recovery_move.actor_id != 1
                or recovery_move.motivo != recovery_reason
                or recovery_move.referencia_tipo != "CONTROL_PESO_MANGA"
                or recovery_move.referencia_id != source_id
                or recovery_move.fuente_tipo != "KG_HISTORY_RECOVERY"
                or recovery_move.atributo_proceso != "PROCESO"
                or recovery_move.pesaje_public_id is not None
                or recovery_move.correccion_aplicada_public_id is not None
                or recovery_move.projection_sha256 != recovery_hash
                or Decimal(recovery_move.cantidad_delta_kg).quantize(KG_QUANTUM) != net
                or Decimal(recovery_move.peso_neto_snapshot_kg).quantize(KG_QUANTUM) != net
                or recovery_move.pesada_at_snapshot != baseline.pesado_at
                or utc(recovery_move.created_at) is None
                or utc(recovery_move.created_at) < cutoff
                or recovery_move.actor_id != 1
                or recovery_move.fuente_tipo != "KG_HISTORY_RECOVERY"
                or recovery_result != Decimal(recovery_move.saldo_fisico_resultante_kg).quantize(KG_QUANTUM)
            ):
                return False

            expected_after = {
                "manifest_sha256": expected_manifest_sha256,
                "manifest_parent_operation_id": str(parent_id),
                "source_control_public_id": source_id,
                "source_type": "CONTROL_PESO_MANGA",
                "manga_public_id": permitted[8],
                "net_kg": format(net, "f"),
                "source_at": baseline.pesado_at.isoformat(),
                "existence_id": str(existence.id),
                "movement_id": str(recovery_move.id),
                "child_operation_id": str(child_id),
                "unidad_fisica_creada": False,
                "qr_creado": False,
                "physical_units_created": 0,
                "qrs_created": 0,
                "current_physical_count_performed": False,
                "accounting_availability_from_measured_net": True,
                "delta_kg": format(net, "f"),
            }
            child_request = {
                "manifest_sha256": expected_manifest_sha256,
                "source_control_public_id": source_id,
            }
            child = session.get(ScmOperacion, child_id)
            child_response = {**expected_after, "status": "APPLIED"}
            if (
                child is None
                or child.operation_id != child_id
                or child.endpoint != "/kg/history-controls/active/v1/" + source_id
                or child.actor_id != 1
                or child.estado_http != 201
                or child.request_sha256 != _operation_hash(child.endpoint, 1, child_request)
                or child.response_json != child_response
                or (utc(child.created_at) is None or utc(child.created_at) < cutoff)
                or child_event.operation_id != child_id
                or child_event.aggregate_type != "MANGA"
                or child_event.aggregate_id != str(peer.id)
                or child_event.tipo != "KG_ACTIVE_CONTROL_HISTORY_RECOVERED"
                or child_event.actor_id != 1
                or child_event.motivo != recovery_reason
                or (utc(child_event.occurred_at) is None or utc(child_event.occurred_at) < cutoff)
                or child_event.after_json != expected_after
            ):
                return False

            # Check the source facts captured in the child event. Manga and
            # segment live state can evolve after recovery; their audit snapshot
            # remains tied to the manifested identities and initial states.
            before = child_event.before_json or {}
            control_before = before.get("control") or {}
            if (
                control_before != {
                    "public_id": source_id,
                    "peso_neto_kg": format(net, "f"),
                    "unidad_evidencia": "UN",
                    "conteo_acumulado_un": None,
                    "pesado_at": baseline.pesado_at.isoformat(),
                    "created_at": baseline.created_at.isoformat(),
                }
                or before.get("manga") != {
                    "public_id": permitted[8],
                    "estado": "EN_LLENADO",
                }
                or (before.get("segment") or {}).get("id") != permitted[9]
                or (before.get("segment") or {}).get("estado") != "ACTIVO"
                or before.get("existing_kg_existence") is not False
                or before.get("existing_kg_movement") is not False
            ):
                return False
            expected_sources.append(expected_after)

        parent = session.get(ScmOperacion, parent_id)
        parent_events = session.scalars(select(ScmEvento).where(
            ScmEvento.operation_id == parent_id
        )).all()
        expected_parent_after = {
            "manifest_sha256": expected_manifest_sha256,
            "manifest": parent_request,
            "sources": expected_sources,
            "source_count": 2,
            "delta_kg": "10.400",
            "physical_units_created": 0,
            "qrs_created": 0,
            "current_physical_count_performed": False,
            "accounting_availability_from_measured_net": True,
            "status": "APPLIED",
        }
        if (
            parent is None
            or parent.operation_id != parent_id
            or parent.endpoint != "/kg/history-controls/active/manifest/v1"
            or parent.actor_id != 1
            or parent.estado_http != 201
            or parent.request_sha256 != _operation_hash(parent.endpoint, 1, parent_request)
            or parent.response_json != expected_parent_after
            or (utc(parent.created_at) is None or utc(parent.created_at) < cutoff)
            or len(parent_events) != 1
        ):
            return False
        parent_event = parent_events[0]
        return bool(
            parent_event.operation_id == parent_id
            and parent_event.tipo == "KG_ACTIVE_CONTROL_HISTORY_MANIFEST_APPLIED"
            and parent_event.aggregate_type == "KG_RECOVERY_BATCH"
            and parent_event.aggregate_id == str(parent_id)
            and parent_event.actor_id == 1
            and parent_event.motivo == recovery_reason
            and utc(parent_event.occurred_at) is not None
            and utc(parent_event.occurred_at) >= cutoff
            and parent_event.before_json == {
                "source_count": 0,
                "delta_kg": "0.000",
                "manifest_sha256": expected_manifest_sha256,
            }
            and parent_event.after_json == expected_parent_after
        )

    source_id = str(control.public_id)
    permitted_source = permitted_sources.get(source_id)
    if (
        permitted_source is None
        or permitted_source[0] != manga.id
        or permitted_source[1] != manga.lote_articulo.articulo_id
        or Decimal(control.peso_neto_kg).quantize(KG_QUANTUM) != permitted_source[2]
        or row_hash(control) != permitted_source[3]
        or row_hash(manga, {"estado", "version"}) != permitted_source[4]
        or row_hash(manga.lote_articulo.articulo, {"unidad_inventario", "version", "updated_at"}) != permitted_source[6]
        or control.tipo != "AVANCE_KG"
        or control.unidad_evidencia != "UN"
        or control.conteo_acumulado_un is not None
        or utc(control.pesado_at) is None
        or any(
            live.tipo != "AVANCE_KG"
            or live.unidad_evidencia != "KG"
            or live.conteo_acumulado_un is not None
            or utc(live.pesado_at) is None or utc(live.pesado_at) < cutoff
            or utc(live.created_at) is None or utc(live.created_at) < cutoff
            for live in controls[1:]
        )
        or manga.anulada_at is not None
        or segment.secuencia != 1
        or segment.cantidad_inicio_un is None
        or Decimal(segment.cantidad_inicio_un) != 0
        or Decimal(segment.cantidad_atribuida_un or 0) != 0
        or len(manga.reaperturas) != 0
        or manga.correccion_asignacion is not None
    ):
        return False
    segment_mutable = {"estado", "cerrada_at", "motivo_cierre", "cantidad_fin_kg", "cantidad_atribuida_kg"}
    segment_final_mutable = segment_mutable | {"calidad_evidencia_kg"}
    current_finals = session.scalars(select(ScmPesajeManga).where(
        ScmPesajeManga.manga_id == manga.id,
        ScmPesajeManga.estado == "VIGENTE",
    )).all()
    if not current_finals:
        segment_identity_ok = (
            segment.calidad_evidencia_kg is None
            and row_hash(segment, segment_mutable) == permitted_source[5]
        )
    else:
        segment_identity_ok = (
            segment.calidad_evidencia_kg == "MEDIDA_DIRECTA"
            and row_hash(segment, segment_final_mutable) == permitted_source[7]
        )
    if not segment_identity_ok:
        return False
    if not current_finals:
        current_controls = [c for c in controls[1:] if c.operation_id == current_operation_id]
        source_shape_ok = (
            manga.estado == "EN_LLENADO"
            and segment.estado == "ACTIVO"
            and segment.cantidad_fin_un is None
            and Decimal(segment.cantidad_inicio_kg or 0) == 0
            and Decimal(segment.cantidad_atribuida_kg or 0) == 0
            and len(current_controls) == 1
            and utc(current_controls[0].pesado_at) == utc(source_at)
            and utc(current_controls[0].created_at) is not None
            and utc(current_controls[0].created_at) >= cutoff
            and not session.scalar(select(ScmMovimientoInventarioKg.id).where(
                ScmMovimientoInventarioKg.operation_id == current_operation_id
            ))
        )
    else:
        # The normal final weighing inserts its source and closes the segment
        # before calling this gate in the same transaction. Admit only that
        # current operation's sole post-cutover final, from the exact 0 -> NET
        # segment; an earlier final, reopen, or closed baseline is ineligible.
        final = current_finals[0] if len(current_finals) == 1 else None
        final_net = Decimal(final.peso_fisico_neto_kg).quantize(KG_QUANTUM) if final else None
        source_shape_ok = bool(
            final is not None
            and final.operation_id == current_operation_id
            and final.pesada_at is not None
            and final.pesada_at >= cutoff
            and final.created_at is not None
            and utc(final.created_at) is not None
            and utc(final.created_at) >= cutoff
            and utc(final.pesada_at) == utc(source_at)
            and manga.estado == "PESADA"
            and segment.estado == "CERRADO"
            and segment.cantidad_fin_un is None
            and Decimal(segment.cantidad_inicio_kg or 0) == 0
            and Decimal(segment.cantidad_fin_kg or 0) == final_net
            and Decimal(segment.cantidad_atribuida_kg or 0) == final_net
            and segment.cerrada_at == final.pesada_at
        )
    if not source_shape_ok:
        return False
    existence_rows = session.scalars(
        select(ScmExistenciaMangaKg)
        .where(ScmExistenciaMangaKg.manga_id == manga.id)
    ).all()
    if len(existence_rows) != 1:
        return False
    existence = existence_rows[0]
    if (
        existence.resuelta_por != "KG_HISTORY_RECOVERY"
        or existence.manga_id != manga.id
        or existence.articulo_scm_id != manga.lote_articulo.articulo_id
        or existence.recibida_por_id != 1
        or (existence.unidad_fisica_kg_id is not None and len(controls) == 1)
        or existence.etiqueta_resuelta_id is not None
        or existence.sesion_id is not None
        or existence.pesaje_public_id is not None
        or existence.correccion_aplicada_public_id is not None
        or existence.estado_logistico != "EN_PRODUCCION"
        or existence.estado_calidad != "SIN_CONTROL"
        or existence.atributo_proceso != "PROCESO"
        or existence.origen_tipo != "PRODUCCION"
        or Decimal(existence.cantidad_reservada_kg or 0) != 0
    ):
        return False
    movement = session.get(ScmMovimientoInventarioKg, existence.movimiento_ingreso_id)
    if movement is None:
        return False
    saldo = session.get(ScmSaldoInventarioKg, movement.saldo_id)
    if saldo is None or saldo.id != existence.saldo_id or saldo.articulo_scm_id != existence.articulo_scm_id or saldo.ubicacion_id != existence.ubicacion_id:
        return False
    movement_chain = session.scalars(select(ScmMovimientoInventarioKg).where(
        ScmMovimientoInventarioKg.saldo_id == saldo.id
    ).order_by(ScmMovimientoInventarioKg.created_at, ScmMovimientoInventarioKg.id)).all()
    running = Decimal("0.000")
    found_recovery_movement = False
    for chain_row in movement_chain:
        running += Decimal(chain_row.cantidad_delta_kg).quantize(KG_QUANTUM)
        if running < 0 or Decimal(chain_row.saldo_fisico_resultante_kg).quantize(KG_QUANTUM) != running:
            return False
        if chain_row.id == movement.id:
            found_recovery_movement = True
    if not found_recovery_movement or running != Decimal(saldo.cantidad_fisica_kg).quantize(KG_QUANTUM):
        return False
    live_attributes = session.execute(select(ScmExistenciaMangaKg.atributo_proceso).where(
        ScmExistenciaMangaKg.saldo_id == saldo.id,
        ScmExistenciaMangaKg.estado_logistico != "REVERSADA",
    )).scalars().all()
    has_process = any(value in {"PROCESO", "MIXTA"} for value in live_attributes)
    has_finished = any(value in {"TERMINADA", "MIXTA"} for value in live_attributes)
    expected_attribute = "MIXTA" if has_process and has_finished else "TERMINADA" if has_finished else "PROCESO"
    if saldo.atributo_proceso != expected_attribute:
        return False
    net = Decimal(control.peso_neto_kg).quantize(KG_QUANTUM)
    projection_hash = hashlib.sha256(
        f"CONTROL_PESO:{source_id}:{net}:".encode()
    ).hexdigest()
    child_id = uuid5(namespace, "active-control-source:" + source_id)
    if (
        existence.operation_id != child_id
        or movement.operation_id != child_id
        or movement.saldo_id != existence.saldo_id
        or movement.tipo != "INGRESO_PRODUCCION"
        or movement.actor_id != 1
        or movement.referencia_tipo != "CONTROL_PESO_MANGA"
        or movement.referencia_id != source_id
        or movement.pesaje_public_id is not None
        or movement.correccion_aplicada_public_id is not None
        or movement.fuente_tipo != "KG_HISTORY_RECOVERY"
        or movement.atributo_proceso != "PROCESO"
        or Decimal(movement.cantidad_delta_kg).quantize(KG_QUANTUM) != net
        or Decimal(movement.peso_neto_snapshot_kg).quantize(KG_QUANTUM) != net
        or Decimal(movement.peso_neto_snapshot_kg).quantize(KG_QUANTUM) != net
        or movement.pesada_at_snapshot != control.pesado_at
        or movement.projection_sha256 != projection_hash
    ):
        return False
    previous_live = [c for c in controls[1:] if c.operation_id != current_operation_id]
    chain_net = net
    for live_control in previous_live:
        live_net = Decimal(live_control.peso_neto_kg).quantize(KG_QUANTUM)
        live_op = session.get(ScmOperacion, live_control.operation_id)
        rows = session.scalars(select(ScmMovimientoInventarioKg).where(
            ScmMovimientoInventarioKg.operation_id == live_control.operation_id
        )).all()
        if (
            live_op is None or utc(live_op.created_at) is None or utc(live_op.created_at) < cutoff
            or len(rows) != 1 or live_net <= chain_net
        ):
            return False
        live_move = rows[0]
        live_hash = hashlib.sha256(
            f"CONTROL_PESO:{live_control.public_id}:{live_net}:".encode()
        ).hexdigest()
        if (
            live_move.saldo_id != movement.saldo_id
            or live_move.tipo != "INGRESO_PRODUCCION"
            or live_move.referencia_tipo != "CONTROL_PESO_MANGA"
            or live_move.referencia_id != str(live_control.public_id)
            or live_move.fuente_tipo != "KG_AUTO_PRODUCCION"
            or live_move.pesada_at_snapshot != live_control.pesado_at
            or live_move.projection_sha256 != live_hash
            or Decimal(live_move.peso_neto_snapshot_kg).quantize(KG_QUANTUM) != live_net
            or Decimal(live_move.cantidad_delta_kg).quantize(KG_QUANTUM) != live_net - chain_net
        ):
            return False
        chain_net = live_net
    expected_current = previous_live[-1] if previous_live else control
    expected_current_net = Decimal(expected_current.peso_neto_kg).quantize(KG_QUANTUM)
    expected_current_hash = hashlib.sha256(
        f"CONTROL_PESO:{expected_current.public_id}:{expected_current_net}:".encode()
    ).hexdigest()
    expected_current_at = expected_current.pesado_at
    expected_identity = bool(previous_live)
    if (
        Decimal(existence.peso_neto_snapshot_kg).quantize(KG_QUANTUM) != expected_current_net
        or Decimal(existence.cantidad_fisica_kg).quantize(KG_QUANTUM) != expected_current_net
        or existence.pesada_at_snapshot != expected_current_at
        or existence.projection_sha256 != expected_current_hash
        or bool(existence.unidad_fisica_kg_id) != expected_identity
    ):
        return False
    child = session.get(ScmOperacion, child_id)
    events = session.scalars(
        select(ScmEvento).where(ScmEvento.operation_id == child_id)
    ).all()
    if child is None or len(events) != 1:
        return False
    event = events[0]
    after = event.after_json or {}
    parent_raw = after.get("manifest_parent_operation_id")
    if not parent_raw:
        return False
    try:
        from uuid import UUID
        parent_id = UUID(str(parent_raw))
    except (TypeError, ValueError):
        return False
    parent = session.get(ScmOperacion, parent_id)
    parent_events = session.scalars(
        select(ScmEvento).where(ScmEvento.operation_id == parent_id)
    ).all()
    parent_manifest = after.get("manifest_sha256")
    parent_after = parent_events[0].after_json if len(parent_events) == 1 else None
    parent_source_rows = (parent_after or {}).get("sources") or []
    parent_source = next((row for row in parent_source_rows
                          if row.get("source_control_public_id") == source_id), None)
    expected_parent_id = uuid5(namespace, "active-control-manifest:" + expected_manifest_sha256)
    expected_source_ids = ["0f39985e-c3ad-4c93-9223-4a07d8a208dd", "970a4edc-f6b8-4f65-bc07-47dcf63274c8"]
    expected_child_ids = [str(uuid5(namespace, "active-control-source:" + value)) for value in expected_source_ids]
    parent_request = {
        "manifest_sha256": expected_manifest_sha256,
        "source_control_public_ids": expected_source_ids,
        "source_count": 2,
        "delta_kg": "10.400",
        "actor_id": 1,
        "location_id": 9,
    }
    child_request = {"manifest_sha256": expected_manifest_sha256, "source_control_public_id": source_id}
    expected_bundle = {
        "source_control_public_id": source_id,
        "manga_public_id": str(manga.public_id),
        "net_kg": format(net, "f"),
        "source_at": control.pesado_at.isoformat(),
        "existence_id": str(existence.id),
        "movement_id": str(movement.id),
        "child_operation_id": str(child_id),
        "manifest_parent_operation_id": str(parent_id),
    }
    if (
        child.endpoint != "/kg/history-controls/active/v1/" + source_id
        or child.actor_id != 1
        or child.estado_http != 201
        or child.request_sha256 != _operation_hash(child.endpoint, 1, child_request)
        or child.response_json != {**after, "status": "APPLIED"}
        or event.aggregate_type != "MANGA"
        or event.aggregate_id != str(manga.id)
        or event.tipo != "KG_ACTIVE_CONTROL_HISTORY_RECOVERED"
        or event.actor_id != 1
        or event.after_json is None
        or any(after.get(key) != value for key, value in expected_bundle.items())
        or parent_manifest != expected_manifest_sha256
        or parent is None
        or parent.operation_id != expected_parent_id
        or parent.actor_id != 1
        or parent.endpoint != "/kg/history-controls/active/manifest/v1"
        or parent.estado_http != 201
        or parent.request_sha256 != _operation_hash(parent.endpoint, 1, parent_request)
        or len(parent_events) != 1
        or parent_events[0].tipo != "KG_ACTIVE_CONTROL_HISTORY_MANIFEST_APPLIED"
        or parent_events[0].aggregate_type != "KG_RECOVERY_BATCH"
        or parent_events[0].aggregate_id != str(expected_parent_id)
        or parent_events[0].actor_id != 1
        or parent_after is None
        or parent.response_json != parent_events[0].after_json
        or parent_after.get("manifest_sha256") != parent_manifest
        or parent_after.get("source_count") != 2
        or parent_after.get("status") != "APPLIED"
        or parent_after.get("manifest") != parent_request
        or [row.get("source_control_public_id") for row in parent_source_rows] != expected_source_ids
        or len({row.get("source_control_public_id") for row in parent_source_rows}) != 2
        or [row.get("child_operation_id") for row in parent_source_rows] != expected_child_ids
        or len({row.get("child_operation_id") for row in parent_source_rows}) != 2
        or parent_source is None
        or parent_source.get("existence_id") != str(existence.id)
        or parent_source.get("movement_id") != str(movement.id)
        or parent_source.get("child_operation_id") != str(child_id)
        or after.get("source_type") != "CONTROL_PESO_MANGA"
        or after.get("unidad_fisica_creada") is not False
        or after.get("qr_creado") is not False
        or after.get("current_physical_count_performed") is not False
        or after.get("accounting_availability_from_measured_net") is not True
    ):
        return False
    peer_source_id = (
        "970a4edc-f6b8-4f65-bc07-47dcf63274c8"
        if source_id == "0f39985e-c3ad-4c93-9223-4a07d8a208dd"
        else "0f39985e-c3ad-4c93-9223-4a07d8a208dd"
    )
    if not peer_recovery_bundle_valid(peer_source_id):
        return False
    # Never waive guards for a historical source capture, an old operation,
    # a live UN projection, or a different reason. Only the measured-history
    # blocker may be suppressed after the full recovery bundle is proved.
    return True


def _assert_automatic_intake_cutoff(session, manga, source_at, operation_id):
    """Cut on measured facts, allowing only a verified active-control recovery.

    Called under the productive advisory lock and manga row lock. Original
    source/server times survive corrections, annulments and reopenings. A clean
    precreated manga may start after T0; historical mass cannot cross the cut.
    Completed operation replays return before reaching this guard.
    """
    from flask import current_app
    from app.models.scm_auditoria import ScmOperacion
    from app.models.scm_warehouse import ScmExistenciaManga
    from app.models.scm_inventory_kg import ScmExistenciaMangaKg
    raw = current_app.config.get("KG_AUTOMATIC_INTAKE_CUTOFF_AT", "")
    try:
        cutoff = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if cutoff.tzinfo is None or cutoff.utcoffset() is None:
            raise ValueError("timezone required")
    except (TypeError, ValueError):
        raise ScmServiceError(
            "KG_INTAKE_CUTOFF_REQUIRED",
            "El ingreso automatico requiere una fecha de corte explicita con zona horaria.",
            status_code=409,
        )

    def utc(value):
        if not isinstance(value, datetime):
            return None
        return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value).astimezone(timezone.utc)

    cutoff = cutoff.astimezone(timezone.utc)
    captured = utc(source_at)
    operation_at = utc(session.scalar(select(ScmOperacion.created_at).where(
        ScmOperacion.operation_id == operation_id)))
    reason = None
    if captured is None or captured < cutoff:
        reason = "CAPTURE_BEFORE_CUTOFF"
    elif operation_at is None or operation_at < cutoff:
        reason = "OPERATION_BEFORE_CUTOFF"
    weighings = session.execute(select(ScmPesajeManga.pesada_at,
        ScmPesajeManga.created_at, ScmPesajeManga.cantidad_confirmada).where(
        ScmPesajeManga.manga_id == manga.id)).all()
    controls = session.execute(select(ScmControlPesoManga.pesado_at,
        ScmControlPesoManga.created_at, ScmControlPesoManga.conteo_acumulado_un).where(
        ScmControlPesoManga.manga_id == manga.id)).all()
    historical_measurement = any(
        utc(value) is None or utc(value) < cutoff
        for row in [*weighings, *controls] for value in row[:2]
    )
    if historical_measurement and _complete_active_control_recovery(session, manga, operation_id, cutoff, source_at):
        historical_measurement = False
    if historical_measurement and reason is None:
        reason = "HISTORICAL_MEASUREMENT"
    un_values = [manga.cantidad_confirmada_un, manga.cantidad_contenida_un]
    un_values.extend(row[2] for row in [*weighings, *controls])
    for row in session.execute(select(ScmTramoMangaTrabajo.cantidad_inicio_un,
            ScmTramoMangaTrabajo.cantidad_fin_un, ScmTramoMangaTrabajo.cantidad_atribuida_un).where(
            ScmTramoMangaTrabajo.manga_id == manga.id)):
        un_values.extend(row)
    if any(Decimal(value or 0) != 0 for value in un_values) or session.scalar(
        select(ScmExistenciaManga.id).where(ScmExistenciaManga.manga_id == manga.id,
            ScmExistenciaManga.estado_logistico != "REVERSADA").limit(1)) is not None:
        if reason is None:
            reason = "LIVE_UN_PROJECTION"
    kg_rows = session.execute(select(ScmExistenciaMangaKg.recibida_at,
            ScmExistenciaMangaKg.resuelta_por).where(ScmExistenciaMangaKg.manga_id == manga.id)).all()
    historical_kg_existence = any(
        origin == "KG_HISTORY_RECOVERY" or utc(received_at) is None or utc(received_at) < cutoff
        for received_at, origin in kg_rows
    )
    if historical_kg_existence and not _complete_active_control_recovery(session, manga, operation_id, cutoff, source_at):
        if reason is None:
            reason = "HISTORICAL_KG_EXISTENCE"
    if reason:
        raise ScmServiceError(
            "KG_INTAKE_CUTOVER_REVIEW_REQUIRED",
            "La captura o historia de la manga requiere conciliacion separada del corte. La operacion no fue aplicada; conserve el UUID y contacte al responsable.",
            status_code=409,
            details={"manga_id": str(manga.public_id), "cutoff_at": cutoff.isoformat(),
                     "source_at": captured.isoformat() if captured else None, "reason": reason},
        )
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
    force=False,
    article=None,
    location=None,
    balance_cache=None,
    existence_cache=None,
    defer_flush=False,
):
    """Project one cumulative measured net into the KG ledger.

    Controls and final weighing call this with the cumulative NET.  The ledger
    movement is only the delta, so replaying a station command cannot duplicate
    mass.  Receipt later changes the location of this same existence.
    """
    if not force and not automatic_kg_intake_enabled():
        from app.models.scm_inventory_kg import ScmExistenciaMangaKg
        existing_stock = session.scalar(select(ScmExistenciaMangaKg.id).where(
            ScmExistenciaMangaKg.manga_id == manga.id,
            ScmExistenciaMangaKg.estado_logistico != "REVERSADA",
        ))
        if existing_stock is not None:
            raise ScmServiceError(
                "KG_INTAKE_DISABLED_WITH_EXISTENCE",
                "El ingreso KG esta deshabilitado. No se puede cambiar el peso de una manga con existencia sin actualizar su inventario.",
                status_code=409,
            )
        return None
    if not force:
        _assert_automatic_intake_cutoff(session, manga, source_at, operation_id)
    from hashlib import sha256
    from app.models.scm_articulos import ScmArticulo
    from app.models.scm_inventory import ScmUbicacionInventario
    from app.models.scm_inventory_kg import (
        ScmExistenciaMangaKg,
        ScmMovimientoInventarioKg,
        ScmSaldoInventarioKg,
    )
    if article is None:
        article = session.scalar(
            select(ScmArticulo).options(noload("*")).where(
                ScmArticulo.id == manga.lote_articulo.articulo.id
            ).with_for_update().execution_options(populate_existing=True)
        )
    _kg_article(manga, article=article)
    if location is None:
        location = _production_location(session, article_class=article.clase)
    total = _kg(net_kg, "net_kg")
    source_token = f"{source_type}:{source_id or manga.id}:{total}:{correction_id or ''}"
    projection_hash = sha256(source_token.encode()).hexdigest()
    balance_key = (article.id, location.id)
    balance = balance_cache.get(balance_key) if balance_cache is not None else None
    if balance is None:
        balance = session.scalar(
            select(ScmSaldoInventarioKg).options(noload("*")).where(
                ScmSaldoInventarioKg.articulo_scm_id == article.id,
                ScmSaldoInventarioKg.ubicacion_id == location.id,
            ).with_for_update().execution_options(populate_existing=True)
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
            if balance_cache is not None:
                balance_cache[balance_key] = balance
        except IntegrityError:
            # A concurrent first event may have won the article/location
            # unique key.  Re-read its locked row and continue the same
            # transaction instead of creating a second balance.
            balance = session.scalar(
                select(ScmSaldoInventarioKg).options(noload("*"))
                .where(
                    ScmSaldoInventarioKg.articulo_scm_id == article.id,
                    ScmSaldoInventarioKg.ubicacion_id == location.id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if balance is None:
                raise
            if balance_cache is not None:
                balance_cache[balance_key] = balance
    elif balance_cache is not None:
        balance_cache[balance_key] = balance
    existence = (
        existence_cache.get(manga.id)
        if existence_cache is not None
        else session.scalar(
            select(ScmExistenciaMangaKg).options(
                noload("*"),
                selectinload(ScmExistenciaMangaKg.unidad_fisica_kg),
            )
            .where(
                ScmExistenciaMangaKg.manga_id == manga.id,
                ScmExistenciaMangaKg.estado_logistico != "REVERSADA",
            )
            .with_for_update()
        )
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
        movement.id = uuid4()
        session.add(movement)
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
        existence.id = uuid4()
        session.add(existence)
        if existence_cache is not None:
            existence_cache[manga.id] = existence
        from app.services.scm_kg_receipt_service import _kg_identity_for_receipt
        unit, label = _kg_identity_for_receipt(
            session, existence=existence, article=article,
            location=location, quantity=total, manga=manga,
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
        elif existence.resuelta_por == "KG_HISTORY_RECOVERY" and delta >= 0:
            # A new, real weighing/control after recovery establishes the
            # physical identity even when final NET equals the recovered NET.
            # Idempotent replays return before the inventory projection.
            from app.services.scm_kg_receipt_service import _kg_identity_for_receipt
            unit, label = _kg_identity_for_receipt(
                session, existence=existence, article=article,
                location=location, quantity=total, manga=manga,
            )
            unit.estado_logistico = "DISPONIBLE_PRODUCCION" if final else "EN_PRODUCCION"
            unit.estado_calidad = "SIN_CONTROL"
            unit.atributo_proceso = "TERMINADA" if final else "PROCESO"
            unit.kg_entregado = total
            unit.kg_verificados = total
            existence.unidad_fisica_kg_id = unit.id
    if existence.resuelta_por == "KG_HISTORY_RECOVERY":
        # Reconcile this shared balance only when it contains this recovered
        # history. Other capture flows preserve their established semantics.
        attributes = session.execute(select(ScmExistenciaMangaKg.atributo_proceso).where(
            ScmExistenciaMangaKg.articulo_scm_id == article.id,
            ScmExistenciaMangaKg.ubicacion_id == location.id,
            ScmExistenciaMangaKg.estado_logistico != "REVERSADA",
            ScmExistenciaMangaKg.id != existence.id,
        )).scalars().all()
        attributes.append(existence.atributo_proceso)
        has_process = any(value in {"PROCESO", "MIXTA"} for value in attributes)
        has_finished = any(value in {"TERMINADA", "MIXTA"} for value in attributes)
        balance.atributo_proceso = (
            "MIXTA" if has_process and has_finished
            else "TERMINADA" if has_finished
            else "PROCESO"
        )
    elif final:
        balance.atributo_proceso = "TERMINADA"
    elif balance.atributo_proceso == "TERMINADA":
        balance.atributo_proceso = "MIXTA"
    if not defer_flush:
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
    acquire_kg_productive_write_lock(session)
    manga_probe = session.scalar(select(ScmManga).where(ScmManga.public_id == manga_id))
    if manga_probe is None:
        raise ScmServiceError("MANGA_NOT_FOUND", "La manga no existe.", status_code=404)
    session.scalars(
        select(ScmTrabajoOt)
        .where(ScmTrabajoOt.id == manga_probe.trabajo_ot_id)
        .with_for_update()
    ).all()
    session.scalars(
        select(ScmTramoMangaTrabajo)
        .where(ScmTramoMangaTrabajo.manga_id == manga_probe.id)
        .order_by(ScmTramoMangaTrabajo.secuencia)
        .with_for_update()
    ).all()
    manga = session.scalar(
        select(ScmManga).where(ScmManga.id == manga_probe.id).with_for_update()
    )
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
    try:
        inventory = sync_kg_production_inventory(
            session, actor_id=actor.id, manga=manga, net_kg=final_net,
            operation_id=operation.operation_id, source_type="CONTROL_CIERRE",
            source_id=latest.public_id, source_at=latest.pesado_at, final=True,
        )
    except Exception:
        session.rollback()
        raise
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


def _kg_article(manga, *, article=None):
    article = article or (
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
    work = effective_work(manga)
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
    current_work = effective_work_for_segment(manga, current_segment)
    current_work_id = current_work.id if current_work is not None else manga.trabajo_ot_id
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
    acquire_kg_productive_write_lock(session)
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
                effective_work_for_segment(manga, current_segment).id
                if effective_work_for_segment(manga, current_segment) is not None
                else manga.trabajo_ot_id
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


def _cancelled_output_has_no_activity(session, output):
    """Ignore only an unused cancelled run when selecting closure units."""
    run = output.corrida_fabricacion
    if run is None or run.estado != "ANULADA":
        return False
    # Legacy links may carry execution evidence outside the canonical graph.
    if run.lote_color_legacy_id is not None or output.lote_salida_legacy_id is not None:
        return False
    if run.trabajos_color or run.corrida_premezclas or output.asignaciones:
        return False
    if output.cantidad_real is not None or output.cantidad_rechazada is not None:
        return False
    # These roots can own reservations, emissions and consumption without a work.
    for model in (ScmRequerimientoMaterial, ScmRequerimientoMaterialPreparado):
        if session.scalar(
            select(model.id).where(model.corrida_fabricacion_id == run.id).limit(1)
        ) is not None:
            return False
    if session.scalar(
        select(ScmSaldoWipSalida.id).where(
            ScmSaldoWipSalida.orden_operacion_salida_id == output.id
        ).limit(1)
    ) is not None:
        return False
    lot = session.scalar(
        select(ScmLoteArticulo).where(
            ScmLoteArticulo.orden_operacion_salida_id == output.id
        )
    )
    if lot is None:
        return True
    if (
        Decimal(lot.cantidad_acreditada or 0) != 0
        or lot.event_time is not None
        or lot.estado_calidad != "PLANIFICADO"
    ):
        return False
    # Annulled mangas still count as historical evidence.
    return session.scalar(
        select(ScmManga.id).where(ScmManga.lote_articulo_id == lot.id).limit(1)
    ) is None


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
    acquire_kg_productive_write_lock(session)
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
        # A correction keeps the source segment and manga immutable.  Make
        # the destination OT see the same physical manga through the overlay;
        # the source OT is filtered below so its closure cannot count it twice.
        work_ids = {item.id for item in works}
        if work_ids:
            corrected_mangas = session.scalars(
                select(ScmManga)
                .join(ScmCorreccionAsignacionManga, ScmCorreccionAsignacionManga.manga_id == ScmManga.id)
                .where(ScmCorreccionAsignacionManga.destino_trabajo_ot_id.in_(work_ids))
                .with_for_update()
            ).all()
            mangas.extend(
                manga for manga in corrected_mangas
                if manga.id not in {item.id for item in mangas}
            )
            # The destination OT does not own the immutable source segment
            # through its FK.  Project each physical segment explicitly so
            # the anchored segment can close B while a later segment closes C.
            known_segment_ids = {segment.id for segment in ot_segments}
            for manga in corrected_mangas:
                for segment in manga.tramos_trabajo:
                    owner = effective_work_for_segment(manga, segment)
                    if owner is not None and owner.id in work_ids and segment.id not in known_segment_ids:
                        ot_segments.append(segment)
                        known_segment_ids.add(segment.id)
        mangas = list({manga.id: manga for manga in mangas}.values())
        if tipo == "OT" and work_ids:
            # The source segment remains the historical ledger row, but its
            # effective owner is the destination after correction.
            ot_segments = [
                segment for segment in ot_segments
                if segment.manga is None
                or getattr(segment.manga, "correccion_asignacion", None) is None
                or (effective_work_for_segment(segment.manga, segment) is not None
                    and effective_work_for_segment(segment.manga, segment).id in work_ids)
            ]
            mangas = [
                manga for manga in mangas
                if getattr(manga, "correccion_asignacion", None) is None
                or (
                    bool(manga.tramos_trabajo)
                    and any(
                        effective_work_for_segment(manga, segment) is not None
                        and effective_work_for_segment(manga, segment).id in work_ids
                        for segment in manga.tramos_trabajo
                    )
                )
                or (
                    not manga.tramos_trabajo
                    and effective_work(manga) is not None
                    and effective_work(manga).id in work_ids
                )
            ]
            resolved_effective_work_ids = {
                effective_work_for_segment(manga, segment).id
                for manga in mangas
                for segment in manga.tramos_trabajo
                if effective_work_for_segment(manga, segment) is not None
                and (
                    segment.estado in {"CERRADO", "ANULADO"}
                    or manga.estado in {
                        "PESADA", "ETIQUETADA_FINAL", "PENDIENTE_RECEPCION_ALMACEN",
                        "RECIBIDA", "ANULADA",
                    }
                )
            }
            resolved_effective_work_ids.update(
                effective_work(manga).id
                for manga in mangas
                if not manga.tramos_trabajo and effective_work(manga) is not None
            )
            # A corrected manga has no destination segment by design.  Its
            # terminal weighing resolves the destination work for OT closure.
            pending_works = [
                work for work in pending_works
                if work.id not in resolved_effective_work_ids
            ]
        units = set()
        for manga in mangas:
            article = manga.lote_articulo.articulo if manga.lote_articulo is not None else None
            if article is not None and article.unidad_inventario:
                units.add(str(article.unidad_inventario).upper())
        if tipo in {"OF", "OA"}:
            for output in getattr(aggregate, "salidas", ()):
                if tipo == "OF" and _cancelled_output_has_no_activity(session, output):
                    continue
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
        weighings = session.scalars(
            select(ScmPesajeManga).where(
                ScmPesajeManga.manga_id.in_(weighed_ids),
                ScmPesajeManga.estado == "VIGENTE",
            )
        ).all() if weighed_ids else []
        weighing_by_manga_id = {item.manga_id: item for item in weighings}
        measured = Decimal("0")
        ot_segments_by_manga = {}
        if tipo == "OT":
            for segment in ot_segments:
                if segment.estado != "ANULADO":
                    ot_segments_by_manga.setdefault(segment.manga_id, []).append(segment)
        for manga in mangas:
            if manga.estado == "ANULADA":
                # Annulment preserves the weighing/segment facts but removes
                # this manga from the productive KG projection entirely.
                continue
            physical_segments = (
                ot_segments_by_manga.get(manga.id, [])
                if tipo == "OT" else [
                    segment for segment in manga.tramos_trabajo
                    if segment.estado != "ANULADO"
                ]
            )
            if physical_segments:
                measured += sum(
                    (Decimal(segment.cantidad_atribuida_kg or 0)
                     for segment in physical_segments),
                    Decimal("0"),
                )
                continue
            final_weighing = weighing_by_manga_id.get(manga.id)
            if final_weighing is not None:
                measured += Decimal(final_weighing.peso_fisico_neto_kg)
        measured = measured.quantize(KG_QUANTUM)
        planned = command.get("kg_objetivo")
        planned_kg = _kg(planned, "kg_objetivo") if planned is not None else None
        close_type = str(command.get("tipo_cierre") or "NORMAL").upper()
        if close_type not in {"NORMAL", "PARCIAL"}:
            raise ScmServiceError("INVALID_CLOSE_TYPE", "tipo_cierre debe ser NORMAL o PARCIAL.", status_code=422)
        reason = str(command.get("motivo") or "").strip() or None
        if close_type == "PARCIAL" and not reason:
            raise ScmServiceError("KG_PARTIAL_CLOSE_REASON_REQUIRED", "El cierre parcial requiere motivo.", status_code=422)
        deviation = (measured - planned_kg).quantize(KG_QUANTUM) if planned_kg is not None else None
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
            if tipo == "OT" and owner_work_ids:
                source_estimate_rows = [
                    row for row in source_estimate_rows
                    if row.manga is None
                    or _effective_attribution_work(row) is None
                    or _effective_attribution_work(row).id in owner_work_ids
                ]
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

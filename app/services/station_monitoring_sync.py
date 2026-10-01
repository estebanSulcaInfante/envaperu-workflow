"""Read-only, station-scoped snapshot reconciliation; never trusts historical ACKs."""
import json
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import func

from app.extensions import db
from app.models.estacion_pesaje import EstacionAvanceProduccion, EstacionReporteAvanceRecepcion
from app.models.legacy_pesaje import (
    EstacionCierreOpLegacy, EstacionDeltaPesajeLegacy,
    EstacionImportacionPesajeLegacy, EstacionPesajeLegacy,
)
from app.services.legacy_continuity import LegacyContinuityError
from app.services.monitoring_sync_fingerprint import closure_fingerprint, progress_fingerprint


@lru_cache(maxsize=1)
def _validator():
    schema = json.loads((Path(__file__).resolve().parents[2] / 'contracts' /
                        'station-monitoring-sync-v1/contract.schema.json').read_text())
    return Draft202012Validator(schema['$defs']['request'], format_checker=FormatChecker())


def _received(model, id_column, operation_id, station_id):
    if operation_id is None:
        return False
    return db.session.query(id_column).filter(
        id_column == operation_id, model.station_id == station_id,
    ).first() is not None


def monitoring_sync_check(station_id, payload):
    if next(_validator().iter_errors(payload), None) is not None:
        raise LegacyContinuityError('PAYLOAD_REJECTED', 'Solicitud sync-check invalida')
    start = date.fromisoformat(payload['window_start_date'])
    end = date.fromisoformat(payload['window_end_date'])
    if not 0 <= (end - start).days <= 30:
        raise LegacyContinuityError('PAYLOAD_REJECTED', 'La ventana debe contener entre 1 y 31 dias')
    model = EstacionAvanceProduccion
    fields = ('operational_date', 'op', 'ot', 'mold', 'color', 'machine_code', 'shift',
              'bags', 'weight_kg', 'first_capture_at_utc', 'last_capture_at_utc')
    rows = db.session.query(*(getattr(model, key) for key in fields)).filter(
        model.station_id == station_id, model.operational_date >= start, model.operational_date <= end,
    ).limit(5001).all()
    # Existing progress-v1 permits at most 5000 groups. Never hash a truncated
    # state: null requires a full snapshot, which can repair oversized state.
    progress_hash = (progress_fingerprint({**payload, 'rows': [dict(row._mapping) for row in rows]})
                     if len(rows) <= 5000 else None)
    legacy_state = None
    closures_hash = None
    if payload['include_legacy']:
        # Scope and project the import lookup: no manifest bodies or other stations.
        imports = EstacionImportacionPesajeLegacy
        current = db.session.query(imports.import_id).filter(
            imports.station_id == station_id, imports.status == 'COMPLETE',
        ).order_by(func.coalesce(imports.completed_at_utc, imports.started_at_utc).desc()).first()
        if current is None:
            raise LegacyContinuityError('INITIAL_IMPORT_REQUIRED', 'Se requiere importacion historica completa', 409)
        captures = EstacionPesajeLegacy
        watermark = db.session.query(func.max(captures.legacy_pesaje_id)).filter(
            captures.station_id == station_id,
        ).scalar() or 0
        closures = EstacionCierreOpLegacy
        current_closures = db.session.query(closures.op_raw, closures.mold_raw,
            closures.reason_raw, closures.closed_at_utc).filter(
            closures.station_id == station_id, closures.import_id == current.import_id,
        ).limit(501).all()
        # SQLite returns naive UTC for timezone-aware DB columns.
        closures_hash = closure_fingerprint([{'op': row.op_raw, 'mold': row.mold_raw,
            'reason': row.reason_raw, 'closed_at_local': row.closed_at_utc.replace(tzinfo=timezone.utc)
            if row.closed_at_utc.tzinfo is None else row.closed_at_utc} for row in current_closures]) if len(current_closures) <= 500 else None
        legacy_state = {'station_id': station_id, 'initial_import_id': current.import_id,
                        'high_watermark': watermark, 'contract_version': 'station-legacy-continuity-v1'}
    return {
        'contract_version': 'station-monitoring-sync-v1', 'station_id': station_id,
        'window_start_date': start.isoformat(), 'window_end_date': end.isoformat(),
        'checked_at_utc': datetime.now(timezone.utc).isoformat(),
        'progress_fingerprint': progress_hash, 'closures_fingerprint': closures_hash,
        'legacy_state': legacy_state,
        'pending_report_received': _received(EstacionReporteAvanceRecepcion,
            EstacionReporteAvanceRecepcion.report_id, payload.get('pending_report_id'), station_id),
        'pending_batch_received': _received(EstacionDeltaPesajeLegacy,
            EstacionDeltaPesajeLegacy.batch_id, payload.get('pending_batch_id'), station_id),
    }

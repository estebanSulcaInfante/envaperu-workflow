from flask import Blueprint, g, jsonify, request

from app.extensions import db
from app.services.scm_auth import request_actor_id
from app.services.scm_postpesaje_reprint_service import (
    acknowledge_reprint_job,
    claim_reprint_job,
    confirm_reprint,
    get_reprint_request,
    list_station_reprint_jobs,
    preview_reprint,
)
from app.services.scm_service_support import ScmServiceError
from app.services.station_auth import require_station_auth


postpesaje_reprint_bp = Blueprint("postpesaje_reprint", __name__)
postpesaje_reprint_station_bp = Blueprint("postpesaje_reprint_station", __name__)


def _error(exc):
    db.session.rollback()
    return jsonify({"error": exc.to_dict()}), exc.status_code


def _actor_id():
    try:
        return request_actor_id()
    except ValueError as exc:
        raise ScmServiceError("ACTOR_HEADER_REQUIRED", str(exc), status_code=400) from exc


@postpesaje_reprint_bp.post("/reimpresiones-postpesaje/preview")
def postpesaje_reprint_preview():
    try:
        return jsonify(preview_reprint(db.session, actor_id=_actor_id(), data=request.get_json(silent=True) or {}))
    except ScmServiceError as exc:
        return _error(exc)


@postpesaje_reprint_bp.post("/reimpresiones-postpesaje/confirmar")
def postpesaje_reprint_confirm():
    try:
        return jsonify(confirm_reprint(db.session, actor_id=_actor_id(), data=request.get_json(silent=True) or {})), 201
    except ScmServiceError as exc:
        return _error(exc)


@postpesaje_reprint_bp.get("/reimpresiones-postpesaje/solicitudes/<uuid:request_id>")
def postpesaje_reprint_request(request_id):
    try:
        return jsonify(get_reprint_request(db.session, actor_id=_actor_id(), request_id=request_id))
    except ScmServiceError as exc:
        return _error(exc)


def _station_matches(station_id):
    authenticated = getattr(g, "authenticated_station", None)
    if authenticated is None or authenticated.station_id != str(station_id):
        return jsonify({"code": "STATION_MISMATCH", "message": "La credencial no pertenece a la estacion solicitada."}), 403
    return None


@postpesaje_reprint_station_bp.get("/stations/<station_id>/post-reprint-jobs")
@require_station_auth
def station_postpesaje_reprint_jobs(station_id):
    mismatch = _station_matches(station_id)
    if mismatch:
        return mismatch
    query_station = request.args.get("station_id", station_id)
    if str(query_station) != str(station_id):
        return jsonify({"code": "STATION_MISMATCH", "message": "station_id no coincide con la ruta."}), 400
    try:
        return jsonify(list_station_reprint_jobs(db.session, station_id=station_id, limit=request.args.get("limit", 20)))
    except ScmServiceError as exc:
        return _error(exc)


@postpesaje_reprint_station_bp.post("/stations/<station_id>/post-reprint-jobs/<uuid:job_id>/claim")
@require_station_auth
def station_postpesaje_reprint_job_claim(station_id, job_id):
    mismatch = _station_matches(station_id)
    if mismatch:
        return mismatch
    try:
        body = request.get_json(silent=True) or {}
        from uuid import UUID
        try:
            attempt_id = UUID(str(body.get("attempt_id")))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ScmServiceError("INVALID_UUID", "attempt_id debe ser UUID valido.", status_code=422) from exc
        return jsonify(claim_reprint_job(db.session, station_id=station_id, job_id=job_id, attempt_id=attempt_id))
    except ScmServiceError as exc:
        return _error(exc)


@postpesaje_reprint_station_bp.post("/stations/<station_id>/post-reprint-jobs/<uuid:job_id>/ack")
@require_station_auth
def station_postpesaje_reprint_job_ack(station_id, job_id):
    mismatch = _station_matches(station_id)
    if mismatch:
        return mismatch
    try:
        return jsonify(acknowledge_reprint_job(db.session, station_id=station_id, job_id=job_id, data=request.get_json(silent=True) or {}))
    except ScmServiceError as exc:
        return _error(exc)

"""Additive HTTP contract for paged packaging read projections."""
from flask import Blueprint, jsonify, request

from app.extensions import db
from app.services.scm_auth import request_actor_id
from app.services.scm_packaging_reads import list_packaging_assignments
from app.services.scm_service_support import ScmServiceError

scm_packaging_reads_bp = Blueprint('scm_packaging_reads', __name__)


@scm_packaging_reads_bp.errorhandler(ScmServiceError)
def handle_error(error):
    return jsonify({'error': error.to_dict()}), error.status_code


@scm_packaging_reads_bp.get('/empaque/asignaciones')
def assignments():
    try:
        actor_id = request_actor_id()
    except ValueError as error:
        raise ScmServiceError('ACTOR_HEADER_REQUIRED', 'X-Actor-Id debe identificar un trabajador válido.', status_code=400) from error
    return jsonify(list_packaging_assignments(db.session, actor_id=actor_id,
        query=request.args.get('q'), limit=request.args.get('limite', 25), cursor=request.args.get('cursor')))
from flask import Blueprint, current_app, g, request, jsonify
from sqlalchemy import update

from app.extensions import db
from app.models.trabajador import Trabajador, RolOperativo, trabajador_rol
from app.models.scm_auditoria import ScmEvento
from app.services.catalog_code_generator import generar_codigo_catalogo
from app.services.scm_auth import request_actor_id
from app.services.scm_service_support import ScmServiceError, actor_snapshot
from app.services.scm_workspace_role_service import (
    create_role,
    list_capabilities,
    list_roles,
    load_workspace_admin,
    set_primary_role,
    update_role,
)

rutas_trabajadores = Blueprint('rutas_trabajadores', __name__)


@rutas_trabajadores.errorhandler(ScmServiceError)
def handle_workspace_role_error(error):
    return jsonify({
        'error': error.to_dict(),
        # Compatibilidad temporal con consumidores de catálogo antiguos.
        'codigo': error.code,
    }), error.status_code


def _json_body():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ScmServiceError(
            'JSON_OBJECT_REQUIRED',
            'Se requiere un objeto JSON.',
            status_code=400,
        )
    return payload


def _actor_id():
    try:
        return request_actor_id()
    except ValueError as error:
        raise ScmServiceError(
            'ACTOR_HEADER_REQUIRED',
            'X-Actor-Id debe identificar un trabajador valido.',
            status_code=400,
        ) from error


def _admin_actor():
    return load_workspace_admin(
        db.session,
        actor_id=_actor_id(),
    )


def _resolve_roles(role_ids):
    if (
        not isinstance(role_ids, list)
        or any(
            not isinstance(role_id, int)
            or isinstance(role_id, bool)
            or role_id <= 0
            for role_id in role_ids
        )
        or len(role_ids) != len(set(role_ids))
    ):
        raise ScmServiceError(
            'INVALID_ROLE_ASSIGNMENTS',
            'roles_ids debe ser una lista de identificadores unicos.',
            status_code=400,
        )
    roles = RolOperativo.query.filter(RolOperativo.id.in_(role_ids)).all()
    missing = sorted(set(role_ids) - {role.id for role in roles})
    if missing:
        raise ScmServiceError(
            'INVALID_ROLE_ASSIGNMENTS',
            'Uno o mas roles asignados no existen.',
            status_code=400,
            details={'ids': missing},
        )
    return roles


def _autoassign_unambiguous_primary(worker, actor):
    active_roles = [role for role in worker.roles if role.activo]
    if len(active_roles) != 1:
        return False
    role = active_roles[0]
    db.session.execute(
        update(trabajador_rol)
        .where(
            trabajador_rol.c.trabajador_id == worker.id,
            trabajador_rol.c.rol_operativo_id == role.id,
        )
        .values(es_principal=True)
    )
    db.session.flush()
    db.session.expire(worker, ['rol_principal'])
    db.session.add(ScmEvento(
        aggregate_type='TRABAJADOR_ROL_PRINCIPAL',
        aggregate_id=str(worker.id),
        tipo='ROL_PRINCIPAL_AUTOASIGNADO',
        actor_id=actor.id,
        actor_snapshot=actor_snapshot(actor),
        before_json={'rol_operativo_id': None},
        after_json={'rol_operativo_id': role.id},
    ))
    return True

# ==========================================
# TRABAJADORES
# ==========================================

@rutas_trabajadores.route('/api/catalogo/trabajadores', methods=['GET'])
def get_trabajadores():
    query = Trabajador.query
    
    # Filtros
    q = request.args.get('q')
    rol_codigo = request.args.get('rol')
    activo = request.args.get('activo')

    if q:
        q_term = f"%{q}%"
        query = query.filter(db.or_(
            Trabajador.nombres.ilike(q_term),
            Trabajador.apellidos.ilike(q_term),
            Trabajador.codigo.ilike(q_term)
        ))
    
    if rol_codigo:
        query = query.join(Trabajador.roles).filter(RolOperativo.codigo == rol_codigo)

    if activo is not None:
        is_active = str(activo).lower() == 'true'
        query = query.filter(Trabajador.activo == is_active)

    trabajadores = query.order_by(Trabajador.apellidos).all()
    actor = getattr(g, 'scm_actor', None)
    can_view_authorization = (
        current_app.config.get('SCM_AUTH_MODE') != 'supabase'
        or (actor is not None and actor.tiene_capacidad('AUTORIZACION_SCM_ADMINISTRAR'))
    )
    if can_view_authorization:
        payload = [t.to_dict() for t in trabajadores]
    else:
        payload = [{
            'id': t.id,
            'codigo': t.codigo,
            'nombres': t.nombres,
            'apellidos': t.apellidos,
            'nombre_corto': t.nombre_corto,
            'nombre_completo': t.nombre_completo,
            'activo': t.activo,
        } for t in trabajadores]
    return jsonify(payload), 200

@rutas_trabajadores.route('/api/catalogo/trabajadores', methods=['POST'])
def create_trabajador():
    try:
        actor = _admin_actor()
        data = _json_body()
        # Generar código auto-secuencial si no existe
        if str(data.get('codigo') or '').strip():
            return jsonify({
                'error': 'El código se asigna automáticamente.',
                'codigo': 'CODIGO_MANUAL_NO_PERMITIDO',
            }), 400
        codigo = generar_codigo_catalogo('TRABAJADOR')

        nuevo_trabajador = Trabajador(
            codigo=codigo,
            nombres=data.get('nombres', '').strip(),
            apellidos=data.get('apellidos', '').strip(),
            nombre_corto=data.get('nombre_corto'),
            activo=data.get('activo', True),
            observaciones=data.get('observaciones')
        )
        
        roles_ids = data.get('roles_ids', [])
        nuevo_trabajador.roles = _resolve_roles(roles_ids)

        db.session.add(nuevo_trabajador)
        db.session.flush()
        _autoassign_unambiguous_primary(nuevo_trabajador, actor)
        db.session.commit()
        return jsonify(nuevo_trabajador.to_dict()), 201

    except ScmServiceError:
        db.session.rollback()
        raise
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@rutas_trabajadores.route('/api/catalogo/trabajadores/<int:id>', methods=['PUT'])
def update_trabajador(id):
    try:
        actor = _admin_actor()
        trabajador = db.session.get(Trabajador, id)
        if not trabajador:
            return jsonify({'error': 'Trabajador no encontrado'}), 404
        data = _json_body()
        if 'codigo' in data and data['codigo'] != trabajador.codigo:
             return jsonify({
                 'error': 'El código es inmutable.',
                 'codigo': 'CODIGO_INMUTABLE',
             }), 400

        trabajador.nombres = data.get('nombres', trabajador.nombres).strip()
        trabajador.apellidos = data.get('apellidos', trabajador.apellidos).strip()
        trabajador.nombre_corto = data.get('nombre_corto', trabajador.nombre_corto)
        trabajador.activo = data.get('activo', trabajador.activo)
        trabajador.observaciones = data.get('observaciones', trabajador.observaciones)

        previous_primary = trabajador.rol_principal
        if 'roles_ids' in data:
            trabajador.roles = _resolve_roles(data['roles_ids'])

        db.session.flush()
        db.session.expire(trabajador, ['rol_principal'])
        if previous_primary is None:
            _autoassign_unambiguous_primary(trabajador, actor)
        if previous_primary is not None and (
            'roles_ids' in data
            and previous_primary.id not in set(data['roles_ids'])
        ):
            db.session.add(ScmEvento(
                aggregate_type='TRABAJADOR_ROL_PRINCIPAL',
                aggregate_id=str(trabajador.id),
                tipo='ROL_PRINCIPAL_LIMPIADO',
                actor_id=actor.id,
                actor_snapshot=actor_snapshot(actor),
                before_json={'rol_operativo_id': previous_primary.id},
                after_json={'rol_operativo_id': None},
            ))

        db.session.commit()
        return jsonify(trabajador.to_dict()), 200

    except ScmServiceError:
        db.session.rollback()
        raise
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@rutas_trabajadores.route('/api/catalogo/trabajadores/<int:id>/estado', methods=['PATCH'])
def toggle_estado_trabajador(id):
    try:
        _admin_actor()
        trabajador = db.session.get(Trabajador, id)
        if not trabajador:
            return jsonify({'error': 'Trabajador no encontrado'}), 404
        data = _json_body()
        if 'activo' in data:
            trabajador.activo = data['activo']
        db.session.commit()
        return jsonify(trabajador.to_dict()), 200
    except ScmServiceError:
        db.session.rollback()
        raise
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

# ==========================================
# ROLES OPERATIVOS
# ==========================================

@rutas_trabajadores.route('/api/catalogo/roles-operativos', methods=['GET'])
def get_roles():
    return jsonify(list_roles(db.session, actor_id=_actor_id())), 200

@rutas_trabajadores.route('/api/catalogo/roles-operativos', methods=['POST'])
def create_rol():
    return jsonify(create_role(
        db.session,
        actor_id=_actor_id(),
        data=_json_body(),
    )), 201


@rutas_trabajadores.route(
    '/api/catalogo/roles-operativos/<int:id>', methods=['PUT']
)
def update_rol(id):
    return jsonify(update_role(
        db.session,
        actor_id=_actor_id(),
        role_id=id,
        data=_json_body(),
    )), 200


@rutas_trabajadores.route('/api/catalogo/capacidades', methods=['GET'])
def get_capacidades():
    return jsonify(list_capabilities(
        db.session,
        actor_id=_actor_id(),
    )), 200


@rutas_trabajadores.route(
    '/api/catalogo/trabajadores/<int:id>/rol-principal',
    methods=['PATCH'],
)
def update_rol_principal(id):
    data = _json_body()
    if set(data) != {'rol_operativo_id'}:
        raise ScmServiceError(
            'INVALID_PRIMARY_ROLE_REQUEST',
            'La solicitud solo admite rol_operativo_id.',
            status_code=400,
        )
    role_id = data.get('rol_operativo_id')
    if not isinstance(role_id, int) or isinstance(role_id, bool) or role_id <= 0:
        raise ScmServiceError(
            'INVALID_PRIMARY_ROLE_ID',
            'rol_operativo_id debe ser un entero positivo.',
            status_code=400,
        )
    return jsonify(set_primary_role(
        db.session,
        actor_id=_actor_id(),
        worker_id=id,
        role_id=role_id,
    )), 200

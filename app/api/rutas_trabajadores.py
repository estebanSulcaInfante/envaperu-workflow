from flask import Blueprint, request, jsonify
from app.extensions import db
from app.models.trabajador import Trabajador, RolOperativo
from app.services.catalog_code_generator import generar_codigo_catalogo

rutas_trabajadores = Blueprint('rutas_trabajadores', __name__)

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
    return jsonify([t.to_dict() for t in trabajadores]), 200

@rutas_trabajadores.route('/api/catalogo/trabajadores', methods=['POST'])
def create_trabajador():
    data = request.json
    try:
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
        if roles_ids:
            roles = RolOperativo.query.filter(RolOperativo.id.in_(roles_ids)).all()
            nuevo_trabajador.roles = roles

        db.session.add(nuevo_trabajador)
        db.session.commit()
        return jsonify(nuevo_trabajador.to_dict()), 201

    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@rutas_trabajadores.route('/api/catalogo/trabajadores/<int:id>', methods=['PUT'])
def update_trabajador(id):
    trabajador = db.session.get(Trabajador, id)
    if not trabajador:
        return jsonify({'error': 'Trabajador no encontrado'}), 404
        
    data = request.json
    try:
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

        if 'roles_ids' in data:
            roles = RolOperativo.query.filter(RolOperativo.id.in_(data['roles_ids'])).all()
            trabajador.roles = roles

        db.session.commit()
        return jsonify(trabajador.to_dict()), 200

    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@rutas_trabajadores.route('/api/catalogo/trabajadores/<int:id>/estado', methods=['PATCH'])
def toggle_estado_trabajador(id):
    trabajador = db.session.get(Trabajador, id)
    if not trabajador:
        return jsonify({'error': 'Trabajador no encontrado'}), 404
        
    try:
        data = request.json
        if 'activo' in data:
            trabajador.activo = data['activo']
        db.session.commit()
        return jsonify(trabajador.to_dict()), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

# ==========================================
# ROLES OPERATIVOS
# ==========================================

@rutas_trabajadores.route('/api/catalogo/roles-operativos', methods=['GET'])
def get_roles():
    roles = RolOperativo.query.order_by(RolOperativo.nombre).all()
    return jsonify([r.to_dict() for r in roles]), 200

@rutas_trabajadores.route('/api/catalogo/roles-operativos', methods=['POST'])
def create_rol():
    data = request.json
    try:
        nuevo_rol = RolOperativo(
            codigo=data['codigo'].upper(),
            nombre=data['nombre'],
            activo=data.get('activo', True)
        )
        db.session.add(nuevo_rol)
        db.session.commit()
        return jsonify(nuevo_rol.to_dict()), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

from flask import Blueprint, request, jsonify
from app.extensions import db
from app.models.maquina import Maquina, TipoMaquina
from app.services.catalog_code_generator import generar_codigo_catalogo

rutas_maquinas = Blueprint('rutas_maquinas', __name__)

# ==========================================
# MAQUINAS
# ==========================================

@rutas_maquinas.route('/api/catalogo/maquinas', methods=['GET'])
def get_maquinas():
    query = Maquina.query
    
    q = request.args.get('q')
    estado = request.args.get('estado')
    activo = request.args.get('activo')
    tipo_maquina_id = request.args.get('tipo_maquina_id')

    if q:
        q_term = f"%{q}%"
        query = query.filter(db.or_(
            Maquina.nombre.ilike(q_term),
            Maquina.codigo.ilike(q_term)
        ))
    
    if estado:
        query = query.filter(Maquina.estado == estado)

    if activo is not None:
        is_active = str(activo).lower() == 'true'
        query = query.filter(Maquina.activo == is_active)
        
    if tipo_maquina_id:
        query = query.filter(Maquina.tipo_maquina_id == tipo_maquina_id)

    maquinas = query.order_by(Maquina.codigo).all()
    return jsonify([m.to_dict() for m in maquinas]), 200

@rutas_maquinas.route('/api/catalogo/maquinas', methods=['POST'])
def create_maquina():
    data = request.json
    try:
        # Generar código si no existe
        if str(data.get('codigo') or '').strip():
            return jsonify({
                'error': 'El código se asigna automáticamente.',
                'codigo': 'CODIGO_MANUAL_NO_PERMITIDO',
            }), 400
        codigo = generar_codigo_catalogo('MAQUINA')

        nueva_maquina = Maquina(
            codigo=codigo,
            nombre=data['nombre'].strip(),
            tipo_maquina_id=data['tipo_maquina_id'],
            estado=data.get('estado', 'OPERATIVA'),
            activo=data.get('activo', True),
            numero_serie=data.get('numero_serie'),
            observaciones=data.get('observaciones')
        )
        
        db.session.add(nueva_maquina)
        db.session.commit()
        return jsonify(nueva_maquina.to_dict()), 201

    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@rutas_maquinas.route('/api/catalogo/maquinas/<int:id>', methods=['PUT'])
def update_maquina(id):
    maquina = db.session.get(Maquina, id)
    if not maquina:
        return jsonify({'error': 'Máquina no encontrada'}), 404
        
    data = request.json
    try:
        if 'codigo' in data and data['codigo'] != maquina.codigo:
             return jsonify({
                 'error': 'El código es inmutable.',
                 'codigo': 'CODIGO_INMUTABLE',
             }), 400

        maquina.nombre = data.get('nombre', maquina.nombre).strip()
        maquina.tipo_maquina_id = data.get('tipo_maquina_id', maquina.tipo_maquina_id)
        maquina.estado = data.get('estado', maquina.estado)
        maquina.activo = data.get('activo', maquina.activo)
        maquina.numero_serie = data.get('numero_serie', maquina.numero_serie)
        maquina.observaciones = data.get('observaciones', maquina.observaciones)

        db.session.commit()
        return jsonify(maquina.to_dict()), 200

    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@rutas_maquinas.route('/api/catalogo/maquinas/<int:id>/estado', methods=['PATCH'])
def change_estado_maquina(id):
    maquina = db.session.get(Maquina, id)
    if not maquina:
        return jsonify({'error': 'Máquina no encontrada'}), 404
        
    data = request.json
    try:
        # Validación de negocio TS-009: No puede pasar a MANTENIMIENTO o BAJA si tiene OP activa.
        nuevo_estado = data.get('estado')
        if nuevo_estado in ['MANTENIMIENTO', 'BAJA', 'FUERA_SERVICIO']:
            ops_activas = [op for op in maquina.ordenes if op.activa]
            if ops_activas:
                # Retornamos 409 Conflict o advertencia
                return jsonify({
                    'error': f'La máquina tiene {len(ops_activas)} OPs activas (Ej: {ops_activas[0].numero_op}). Cancele o reasigne primero.'
                }), 409
                
        if 'estado' in data:
            maquina.estado = nuevo_estado
        if 'activo' in data:
            maquina.activo = data['activo']
            
        db.session.commit()
        return jsonify(maquina.to_dict()), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400


# ==========================================
# TIPOS DE MAQUINA
# ==========================================

@rutas_maquinas.route('/api/catalogo/tipos-maquina', methods=['GET'])
def get_tipos_maquina():
    query = TipoMaquina.query
    if request.args.get('include_inactive', 'false').lower() != 'true':
        query = query.filter(TipoMaquina.activo.is_(True))
    tipos = query.order_by(TipoMaquina.nombre).all()
    return jsonify([t.to_dict() for t in tipos]), 200

@rutas_maquinas.route('/api/catalogo/tipos-maquina', methods=['POST'])
def create_tipo_maquina():
    data = request.json or {}
    try:
        if str(data.get('codigo') or '').strip():
            return jsonify({
                'error': 'El código se asigna automáticamente.',
                'codigo': 'CODIGO_MANUAL_NO_PERMITIDO',
            }), 400
        nombre = str(data.get('nombre') or '').strip()
        if not nombre:
            return jsonify({'error': 'El nombre es obligatorio.'}), 400
        nuevo_tipo = TipoMaquina(
            codigo=generar_codigo_catalogo('TIPO_MAQUINA'),
            nombre=nombre,
            proceso=data.get('proceso', 'INYECCION'),
            fabricante=data.get('fabricante'),
            modelo=data.get('modelo'),
            capacidad_toneladas=data.get('capacidad_toneladas'),
            activo=data.get('activo', True)
        )
        db.session.add(nuevo_tipo)
        db.session.commit()
        return jsonify(nuevo_tipo.to_dict()), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400


@rutas_maquinas.route('/api/catalogo/tipos-maquina/<int:id>', methods=['PUT'])
def update_tipo_maquina(id):
    item = db.session.get(TipoMaquina, id)
    if item is None:
        return jsonify({'error': 'Tipo de máquina no encontrado'}), 404
    data = request.json or {}
    try:
        if int(data.get('version', 0)) != item.version:
            return jsonify({
                'error': 'El tipo de máquina cambió; recargue antes de guardar.',
                'codigo': 'VERSION_CONFLICT',
                'actual': item.to_dict(),
            }), 409
        if 'codigo' in data and data['codigo'] != item.codigo:
            return jsonify({
                'error': 'El código es inmutable.',
                'codigo': 'CODIGO_INMUTABLE',
            }), 400
        if 'nombre' in data:
            nombre = str(data['nombre'] or '').strip()
            if not nombre:
                return jsonify({'error': 'El nombre es obligatorio.'}), 400
            item.nombre = nombre
        for field in ('proceso', 'fabricante', 'modelo', 'capacidad_toneladas', 'activo'):
            if field in data:
                setattr(item, field, data[field])
        item.version += 1
        db.session.commit()
        return jsonify(item.to_dict()), 200
    except (TypeError, ValueError):
        db.session.rollback()
        return jsonify({'error': 'version debe ser un entero.'}), 400
    except Exception as exc:
        db.session.rollback()
        return jsonify({'error': str(exc)}), 400


@rutas_maquinas.route('/api/catalogo/tipos-maquina/<int:id>', methods=['DELETE'])
def deactivate_tipo_maquina(id):
    item = db.session.get(TipoMaquina, id)
    if item is None:
        return jsonify({'error': 'Tipo de máquina no encontrado'}), 404
    try:
        version = int(request.args.get('version'))
    except (TypeError, ValueError):
        return jsonify({'error': 'version es requerida.'}), 400
    if version != item.version:
        return jsonify({
            'error': 'El tipo de máquina cambió; recargue antes de guardar.',
            'codigo': 'VERSION_CONFLICT',
            'actual': item.to_dict(),
        }), 409
    active_machines = Maquina.query.filter_by(
        tipo_maquina_id=item.id,
        activo=True,
    ).count()
    if active_machines:
        return jsonify({
            'error': 'No se puede inactivar un tipo usado por máquinas activas.',
            'codigo': 'TIPO_MAQUINA_EN_USO',
            'uso': {'maquinas_activas': active_machines},
        }), 409
    if item.activo:
        item.activo = False
        item.version += 1
        db.session.commit()
    return jsonify(item.to_dict()), 200

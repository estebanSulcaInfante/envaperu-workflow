"""
API para gestión de Talonarios RDP
"""
from flask import Blueprint, request, jsonify
from app.extensions import db
from app.models.talonario import Talonario

talonarios_bp = Blueprint('talonarios', __name__, url_prefix='/api/talonarios')


@talonarios_bp.route('', methods=['GET'])
def listar_talonarios():
    """Lista todos los talonarios"""
    solo_activos = request.args.get('activos', 'true').lower() == 'true'
    
    query = Talonario.query.order_by(Talonario.desde.desc())
    if solo_activos:
        query = query.filter_by(activo=True)
    
    talonarios = query.all()
    return jsonify([t.to_dict() for t in talonarios])


@talonarios_bp.route('/<int:id>', methods=['GET'])
def obtener_talonario(id):
    """Obtiene un talonario por ID"""
    talonario = db.session.get(Talonario, id)
    if not talonario:
        return jsonify({'error': 'Talonario no encontrado'}), 404
    return jsonify(talonario.to_dict())


@talonarios_bp.route('', methods=['POST'])
def crear_talonario():
    """Crea un nuevo talonario"""
    data = request.get_json()
    
    if not data or 'desde' not in data or 'hasta' not in data:
        return jsonify({'error': 'Se requiere desde y hasta'}), 400
    
    desde = int(data['desde'])
    hasta = int(data['hasta'])
    
    if desde >= hasta:
        return jsonify({'error': 'desde debe ser menor que hasta'}), 400
    
    # Verificar solapamiento con talonarios existentes
    existente = Talonario.query.filter(
        db.or_(
            db.and_(Talonario.desde <= desde, Talonario.hasta >= desde),
            db.and_(Talonario.desde <= hasta, Talonario.hasta >= hasta),
            db.and_(Talonario.desde >= desde, Talonario.hasta <= hasta)
        )
    ).first()
    
    if existente:
        return jsonify({
            'error': f'Rango se solapa con talonario existente ({existente.desde}-{existente.hasta})'
        }), 400
    
    talonario = Talonario(
        desde=desde,
        hasta=hasta,
        descripcion=data.get('descripcion', ''),
        activo=True
    )
    
    db.session.add(talonario)
    db.session.commit()
    
    return jsonify(talonario.to_dict()), 201


@talonarios_bp.route('/<int:id>', methods=['DELETE'])
def eliminar_talonario(id):
    """Elimina un talonario (solo si no tiene uso)"""
    talonario = db.session.get(Talonario, id)
    if not talonario:
        return jsonify({'error': 'Talonario no encontrado'}), 404
    
    if talonario.usados > 0:
        return jsonify({'error': 'No se puede eliminar un talonario con correlativos usados'}), 400
    
    db.session.delete(talonario)
    db.session.commit()
    
    return jsonify({'message': 'Talonario eliminado'}), 200


@talonarios_bp.route('/siguiente', methods=['GET'])
def obtener_siguiente():
    """
    Obtiene el siguiente correlativo disponible.
    Busca en el talonario activo más antiguo con correlativos disponibles.
    """
    talonario = Talonario.query.filter(
        Talonario.activo == True,
        db.or_(
            Talonario.ultimo_usado.is_(None),
            Talonario.ultimo_usado < Talonario.hasta
        )
    ).order_by(Talonario.desde).first()
    
    if not talonario:
        return jsonify({
            'error': 'No hay correlativos disponibles',
            'siguiente': None,
            'talonario': None
        }), 404
    
    return jsonify({
        'siguiente': talonario.siguiente,
        'talonario': {
            'id': talonario.id,
            'desde': talonario.desde,
            'hasta': talonario.hasta,
            'disponibles': talonario.disponibles
        }
    })


@talonarios_bp.route('/consumir', methods=['POST'])
def consumir_correlativo():
    """
    Consume el siguiente correlativo disponible y lo retorna.
    Usado cuando se crea un RDP.
    """
    talonario = Talonario.query.filter(
        Talonario.activo == True,
        db.or_(
            Talonario.ultimo_usado.is_(None),
            Talonario.ultimo_usado < Talonario.hasta
        )
    ).order_by(Talonario.desde).first()
    
    if not talonario:
        return jsonify({
            'error': 'No hay correlativos disponibles',
            'correlativo': None
        }), 404
    
    correlativo = talonario.consumir()
    db.session.commit()
    
    return jsonify({
        'correlativo': correlativo,
        'talonario_id': talonario.id,
        'disponibles_restantes': talonario.disponibles
    })


@talonarios_bp.route('/reservar', methods=['POST'])
def reservar_correlativos():
    """
    Reserva múltiples correlativos para cache offline de un dispositivo.
    
    Request:
    {
        "cantidad": 100
    }
    
    Response:
    {
        "correlativos": [30001, 30002, ...],
        "cantidad": 100,
        "disponibles_restantes": 400
    }
    """
    data = request.get_json() or {}
    cantidad = min(int(data.get('cantidad', 100)), 500)  # Max 500 por request
    
    correlativos = []
    
    while len(correlativos) < cantidad:
        talonario = Talonario.query.filter(
            Talonario.activo == True,
            db.or_(
                Talonario.ultimo_usado.is_(None),
                Talonario.ultimo_usado < Talonario.hasta
            )
        ).order_by(Talonario.desde).first()
        
        if not talonario:
            break  # No hay más disponibles
        
        # Cuántos quedan en este talonario
        disponibles_talonario = talonario.disponibles
        a_consumir = min(disponibles_talonario, cantidad - len(correlativos))
        
        for _ in range(a_consumir):
            corr = talonario.consumir()
            if corr:
                correlativos.append(corr)
    
    db.session.commit()
    
    if not correlativos:
        return jsonify({
            'error': 'No hay correlativos disponibles',
            'correlativos': [],
            'cantidad': 0
        }), 404
    
    return jsonify({
        'correlativos': correlativos,
        'cantidad': len(correlativos),
        'disponibles_restantes': Talonario.query.filter_by(activo=True).with_entities(
            db.func.sum(Talonario.hasta - db.func.coalesce(Talonario.ultimo_usado, Talonario.desde - 1))
        ).scalar() or 0
    })

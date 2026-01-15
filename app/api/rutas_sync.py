from flask import Blueprint, jsonify, request
from app.extensions import db
from app.models.registro import RegistroDiarioProduccion
from app.models.control_peso import ControlPeso
from app.models.maquina import Maquina
from datetime import datetime

sync_bp = Blueprint('sync', __name__)

@sync_bp.route('/sync/pesajes', methods=['POST'])
def sync_pesajes():
    """
    Recibe pesajes desde el Scale Module y los inserta como ControlPeso.
    Si el RDP no existe, lo crea.
    """
    data = request.get_json()
    if not data or 'pesajes' not in data:
        return jsonify({'error': 'Invalid payload'}), 400
    
    synced_ids = []
    errors = []
    
    # Pre-cargar maquinas para resolucion rapida
    try:
        maquinas = {m.nombre.upper(): m.id for m in Maquina.query.all()}
    except Exception as e:
        return jsonify({'error': f'Error cargando maquinas: {str(e)}'}), 500
    
    # Track impacted RDPs to recalculate once at end (optimization) or loop?
    # Doing inside loop is safer for now.
    
    for p in data['pesajes']:
        try:
            # 1. Resolver Maquina
            maq_nombre = p.get('maquina', '').upper()
            maq_id = maquinas.get(maq_nombre)
            
            # Fallback simple: si maquinas esta vacio o no encuentra,
            # intenta buscar por query directo o error.
            if not maq_id:
                m_obj = Maquina.query.filter_by(nombre=maq_nombre).first()
                if m_obj: 
                    maq_id = m_obj.id
                    maquinas[maq_nombre] = maq_id
            
            if not maq_id:
                errors.append({'local_id': p['local_id'], 'error': f"Maquina {maq_nombre} no encontrada en Central"})
                continue
                
            # 2. Buscar/Crear RDP
            # Scale envia fecha_ot en ISO string (YYYY-MM-DD)
            fecha_str = p.get('fecha_ot')
            fecha_ot = datetime.fromisoformat(fecha_str).date() if fecha_str else datetime.now().date()
            turno = p.get('turno', 'DIURNO')
            orden_id = p.get('nro_op')
            
            # Buscar existente
            rdp = RegistroDiarioProduccion.query.filter_by(
                orden_id=orden_id,
                maquina_id=maq_id,
                fecha=fecha_ot,
                turno=turno
            ).first()
            
            if not rdp:
                # Crear RDP on-the-fly
                rdp = RegistroDiarioProduccion(
                    orden_id=orden_id,
                    maquina_id=maq_id,
                    fecha=fecha_ot,
                    turno=turno,
                    hora_inicio="00:00", # Placeholder
                    colada_inicial=0,
                    colada_final=0
                )
                db.session.add(rdp)
                db.session.flush() # Para obtener ID
            
            # 3. Insertar ControlPeso
            # Chequear duplicidad simple? (Opcional, Scale deberia manejarlo)
            # Pesaje ID local no se guarda en Central por ahora en modelo ControlPeso
            # Guardamos la data
            
            nuevo_peso = ControlPeso(
                registro_id=rdp.id,
                peso_real_kg=float(p.get('peso_kg', 0)),
                color_nombre=p.get('color'),
                # Parsear fecha_hora con info de timezone si viene
                hora_registro=datetime.fromisoformat(p['fecha_hora']) if p.get('fecha_hora') else None
            )
            db.session.add(nuevo_peso)
            # Flush para persistir ID y FK
            db.session.flush()
            
            # Invalidar cache de la relacion para que se recargue con el nuevo item
            db.session.expire(rdp, ['controles_peso'])
            
            # 4. Actualizar Totales del RDP
            rdp.actualizar_totales()
            
            synced_ids.append({'local_id': p['local_id']})
            
        except Exception as e:
            errors.append({'local_id': p.get('local_id'), 'error': str(e)})
            
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500
        
    return jsonify({
        'success': True,
        'message': f"Procesados {len(synced_ids)} pesajes",
        'synced': synced_ids,
        'errors': errors
    })

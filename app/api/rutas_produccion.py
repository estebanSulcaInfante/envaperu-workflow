from flask import Blueprint, jsonify, request
# from app.api import produccion_bp  <-- Removed


from app.extensions import db
from app.models.orden import OrdenProduccion
from app.models.lote import LoteColor
from app.models.recetas import SeCompone, SeColorea
from datetime import datetime, timezone

# Definimos el "Blueprint" (un grupo de rutas)
produccion_bp = Blueprint('produccion', __name__)

@produccion_bp.route('/ordenes', methods=['POST'])
def crear_orden():
    """
    Crea una nueva Orden de Producción completa:
    Cabecera -> Lotes -> Recetas (Materiales y Pigmentos)
    """
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Payload JSON requerido'}), 400

    try:
        # 1. Cabecera de Orden
        nueva_orden = OrdenProduccion(
            numero_op=data.get('numero_op'),
            maquina_id=data.get('maquina_id'),
            tipo_maquina=data.get('tipo_maquina'),
            producto=data.get('producto'),
            molde=data.get('molde'),
            # cliente=data.get('cliente'), Removed
            tipo_estrategia=data.get('tipo_estrategia'),
            meta_total_kg=data.get('meta_total_kg'),
            meta_total_doc=data.get('meta_total_doc'),
            peso_unitario_gr=data.get('peso_unitario_gr'),
            peso_inc_colada=data.get('peso_inc_colada'),
            cavidades=data.get('cavidades'),
            tiempo_ciclo=data.get('tiempo_ciclo'),
            horas_turno=data.get('horas_turno'),
            fecha_inicio=datetime.fromisoformat(data.get('fecha_inicio')) if data.get('fecha_inicio') else datetime.now(timezone.utc)
        )
        db.session.add(nueva_orden)
        db.session.flush() # Para obtener nueva_orden.id

        # 2. Lotes
        lotes_data = data.get('lotes', [])
        for l_data in lotes_data:
            nuevo_lote = LoteColor(
                orden_id=nueva_orden.id,
                color_nombre=l_data.get('color_nombre'),
                personas=l_data.get('personas', 1),
                stock_kg_manual=l_data.get('stock_kg_manual')
            )
            db.session.add(nuevo_lote)
            db.session.flush() # ID para recetas

            # 3a. Materiales (SeCompone)
            materiales = l_data.get('materiales', [])
            for m_data in materiales:
                receta_mat = SeCompone(
                    lote_id=nuevo_lote.id,
                    materia_prima_id=m_data.get('materia_prima_id'),
                    fraccion=m_data.get('fraccion', 0.0)
                )
                db.session.add(receta_mat)

            # 3b. Pigmentos (SeColorea)
            pigmentos = l_data.get('pigmentos', [])
            for p_data in pigmentos:
                receta_pig = SeColorea(
                    lote_id=nuevo_lote.id,
                    colorante_id=p_data.get('colorante_id'),
                    gramos=p_data.get('gramos', 0.0)
                )
                db.session.add(receta_pig)

        db.session.commit()
        return jsonify(nueva_orden.to_dict()), 201

    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@produccion_bp.route('/ordenes', methods=['GET'])
def obtener_ordenes():
    """
    Vista Principal: Devuelve todas las órdenes con sus lotes y cálculos.
    Equivale a abrir tu Excel de 'Control de Producción'.
    """
    # 1. Consultar BD (Select * from ordenes)
    lista_ordenes = OrdenProduccion.query.order_by(OrdenProduccion.id.desc()).all()
    
    # 2. Convertir a JSON usando los métodos que acabamos de crear
    respuesta = [orden.to_dict() for orden in lista_ordenes]
    
    # 3. Responder
    return jsonify(respuesta), 200
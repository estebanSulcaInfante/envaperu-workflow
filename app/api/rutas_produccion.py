from flask import Blueprint, jsonify, request
# from app.api import produccion_bp  <-- Removed


from app.extensions import db
from app.models.orden import OrdenProduccion
from app.models.lote import LoteColor
from app.models.recetas import SeCompone, SeColorea
from app.models.materiales import MateriaPrima, Colorante
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
                numero_op=nueva_orden.numero_op,
                color_nombre=l_data.get('color_nombre'),
                personas=l_data.get('personas', 1),
                stock_kg_manual=l_data.get('stock_kg_manual')
            )
            db.session.add(nuevo_lote)
            db.session.flush() # ID para recetas

            # 3a. Materiales (SeCompone) - buscar o crear por nombre
            materiales = l_data.get('materiales', [])
            for m_data in materiales:
                nombre_material = m_data.get('nombre')
                tipo_material = m_data.get('tipo', 'VIRGEN')
                
                # Buscar materia prima existente o crear nueva
                materia = MateriaPrima.query.filter_by(nombre=nombre_material).first()
                if not materia:
                    materia = MateriaPrima(nombre=nombre_material, tipo=tipo_material)
                    db.session.add(materia)
                    db.session.flush()
                
                receta_mat = SeCompone(
                    lote_id=nuevo_lote.id,
                    materia_prima_id=materia.id,
                    fraccion=m_data.get('fraccion', 0.0)
                )
                db.session.add(receta_mat)

            # 3b. Pigmentos (SeColorea) - buscar o crear por nombre
            pigmentos = l_data.get('pigmentos', [])
            for p_data in pigmentos:
                nombre_pigmento = p_data.get('nombre')
                
                # Buscar colorante existente o crear nuevo
                colorante = Colorante.query.filter_by(nombre=nombre_pigmento).first()
                if not colorante:
                    colorante = Colorante(nombre=nombre_pigmento)
                    db.session.add(colorante)
                    db.session.flush()
                
                receta_pig = SeColorea(
                    lote_id=nuevo_lote.id,
                    colorante_id=colorante.id,
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
    lista_ordenes = OrdenProduccion.query.order_by(OrdenProduccion.fecha_creacion.desc()).all()
    
    # 2. Convertir a JSON usando los métodos que acabamos de crear
    respuesta = [orden.to_dict() for orden in lista_ordenes]
    
    # 3. Responder
    return jsonify(respuesta), 200


@produccion_bp.route('/ordenes/<numero_op>/excel', methods=['GET'])
def descargar_excel(numero_op):
    """
    Genera y descarga el Excel de una Orden de Producción específica.
    Usa la pestaña 'IMPRIMIR OP' de la plantilla.
    """
    from flask import send_file
    from app.services.excel_service import generar_op_excel
    
    # Buscar la orden
    orden = OrdenProduccion.query.get(numero_op)
    if not orden:
        return jsonify({'error': f'Orden {numero_op} no encontrada'}), 404
    
    try:
        # Generar Excel
        excel_buffer = generar_op_excel(orden)
        
        # Retornar como descarga
        filename = f"{orden.numero_op}.xlsx"
        return send_file(
            excel_buffer,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=filename
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@produccion_bp.route('/ordenes/<numero_op>/qr', methods=['GET'])
def obtener_qr_imagen(numero_op):
    """
    Genera y retorna el QR como imagen PNG.
    Query params:
        - size: tamaño en px (default 200)
    """
    from flask import send_file
    from app.services.qr_service import generar_qr_imagen
    
    orden = OrdenProduccion.query.get(numero_op)
    if not orden:
        return jsonify({'error': f'Orden {numero_op} no encontrada'}), 404
    
    size = request.args.get('size', 200, type=int)
    
    try:
        qr_buffer = generar_qr_imagen(orden, size)
        return send_file(
            qr_buffer,
            mimetype='image/png',
            as_attachment=False,
            download_name=f"QR-{orden.numero_op}.png"
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@produccion_bp.route('/ordenes/<numero_op>/qr-data', methods=['GET'])
def obtener_qr_data(numero_op):
    """
    Retorna el QR como base64 y la URL del form (útil para frontend).
    """
    from app.services.qr_service import generar_qr_base64, generar_url_form
    
    orden = OrdenProduccion.query.get(numero_op)
    if not orden:
        return jsonify({'error': f'Orden {numero_op} no encontrada'}), 404
    
    size = request.args.get('size', 200, type=int)
    
    try:
        return jsonify({
            'numero_op': orden.numero_op,
            'qr_base64': generar_qr_base64(orden, size),
            'form_url': generar_url_form(orden)
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500
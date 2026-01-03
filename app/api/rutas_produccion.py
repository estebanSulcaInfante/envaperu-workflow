from flask import Blueprint, jsonify, request
# from app.api import produccion_bp  <-- Removed


from app.extensions import db
from app.models.orden import OrdenProduccion
from app.models.lote import LoteColor
from app.models.recetas import SeCompone, SeColorea
from app.models.materiales import MateriaPrima, Colorante
from app.models.registro import RegistroDiarioProduccion, DetalleProduccionHora
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
            # tipo_maquina is derived from relation, not stored directly
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


@produccion_bp.route('/ordenes/<numero_op>', methods=['GET'])
def obtener_orden(numero_op):
    """
    Retorna los detalles de una orden específica.
    """
    orden = db.session.get(OrdenProduccion, numero_op)
    if not orden:
        return jsonify({'error': 'Orden no encontrada'}), 404
    return jsonify(orden.to_dict()), 200


@produccion_bp.route('/ordenes/<numero_op>/excel', methods=['GET'])
def descargar_excel(numero_op):
    """
    Genera y descarga el Excel de una Orden de Producción específica.
    Usa la pestaña 'IMPRIMIR OP' de la plantilla.
    """
    from flask import send_file
    from app.services.excel_service import generar_op_excel
    
    # Buscar la orden
    orden = db.session.get(OrdenProduccion, numero_op)
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
    
    orden = db.session.get(OrdenProduccion, numero_op)
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
    
    orden = db.session.get(OrdenProduccion, numero_op)
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

@produccion_bp.route('/ordenes/<numero_op>/registros', methods=['GET'])
def listar_registros(numero_op):
    """
    Retorna la lista de Registros Diarios, simulando la vista del Excel de Producción.
    Incluye todos los cálculos y datos "repetidos" de la orden para completar la vista.
    """
    orden = db.session.get(OrdenProduccion, numero_op)
    if not orden:
        return jsonify({'error': 'Orden no encontrada'}), 404
        
    resultados = []
    
    # Iterar sobre registros (asumiendo que están ordenados por fecha/turno o ID)
    registros = RegistroDiarioProduccion.query.filter_by(orden_id=numero_op).all()
    
    for r in registros:
        # Calcular fecha desglosada
        mes = r.fecha.month if r.fecha else None
        ano = r.fecha.year if r.fecha else None
        semana = r.fecha.isocalendar()[1] if r.fecha else None
        
        # Recuperar datos que vienen de la máquina (aunque ahora está en registro, el user pide Tipo Maq)
        tipo_maquina = r.maquina.tipo if r.maquina else None
        nombre_maquina = r.maquina.nombre if r.maquina else None
        
        # Snapshots vs Live Data (usamos snapshots del registro para consistencia histórica)
        cav_reg = r.snapshot_cavidades
        ciclo_reg = r.tiempo_ciclo_reportado
        peso_unit_reg = r.snapshot_peso_neto_gr # Asumiendo peso neto es el unitario
        
        # Construir fila plana tipo Excel
        # Construir fila plana tipo Excel (AHORA RESUMIDA PORQUE ES HEADER)
        fila = {
            "ID Registro": r.id,
            "FECHA": r.fecha.isoformat() if r.fecha else None,
            "Turno": r.turno,
            "Maquina": nombre_maquina,
            "Hora Inicio": r.hora_inicio,
            "Colada Ini": r.colada_inicial,
            "Colada Fin": r.colada_final,
            "Total Coladas (Calc)": r.total_coladas_calculada,
            "Total Piezas (Est)": r.total_piezas_buenas,
            "Total Kg (Est)": r.total_kg_real,
            
            # Detalles anidados para el frontend
            "detalles": [d.to_dict() for d in r.detalles]
        }
        resultados.append(fila)
        
    return jsonify(resultados), 200

@produccion_bp.route('/ordenes/<numero_op>/registros', methods=['POST'])
def crear_registro(numero_op):
    """
    Crea un nuevo Registro Diario de Producción (CABECERA) y detalles iniciales.
    Payload:
    {
       "fecha": "YYYY-MM-DD",
       "turno": "DIA",
       "hora_inicio": "07:00",
       "colada_inicial": 1000,
       "colada_final": 1500,
       "tiempo_ciclo": 30.0,
       ...
       "detalles": [
          {"hora": "07:00", "coladas": 50, "maquinista": "...", "color": "ROJO"},
          ...
       ]
    }
    """
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Payload JSON requerido'}), 400
        
    orden = db.session.get(OrdenProduccion, numero_op)
    if not orden:
        return jsonify({'error': 'Orden no encontrada'}), 404
        
    try:
        # Validar minimos
        if 'maquina_id' not in data or 'fecha' not in data:
             return jsonify({'error': 'Faltan campos obligatorios (maquina_id, fecha)'}), 400

        # Crear Cabecera
        cabecera = RegistroDiarioProduccion(
            orden_id=orden.numero_op,
            maquina_id=data.get('maquina_id'),
            fecha=datetime.fromisoformat(data.get('fecha')).date(),
            turno=data.get('turno'),
            hora_inicio=data.get('hora_inicio'),
            colada_inicial=data.get('colada_inicial', 0),
            colada_final=data.get('colada_final', 0),
            tiempo_ciclo_reportado=data.get('tiempo_ciclo', 0.0),
            tiempo_enfriamiento=data.get('tiempo_enfriamiento', 0.0),
            cantidad_por_hora_meta=data.get('meta_hora', 0),
            
            # Snapshots
            snapshot_cavidades=orden.cavidades,
            snapshot_peso_neto_gr=orden.peso_unitario_gr, # Asumiendo peso unit es pieza
            snapshot_peso_colada_gr=0.0, # TO-DO: Agregar a Orden si hace falta
            snapshot_peso_extra_gr=0.0
        )
        
        # Calcular totales cabecera
        cabecera.actualizar_totales()
        db.session.add(cabecera)
        db.session.flush() # Para tener ID
        
        # Procesar Detalles
        detalles_data = data.get('detalles', [])
        peso_tiro = (cabecera.snapshot_peso_neto_gr * cabecera.snapshot_cavidades)
        
        for d in detalles_data:
            detalle = DetalleProduccionHora(
                registro_id=cabecera.id,
                hora=d.get('hora'),
                maquinista=d.get('maquinista'),
                color=d.get('color'),
                observacion=d.get('observacion'),
                coladas_realizadas=d.get('coladas', 0)
            )
            # Calcular metricas del detalle
            detalle.calcular_metricas(cabecera.snapshot_cavidades, peso_tiro)
            db.session.add(detalle)
            
        db.session.commit()
        return jsonify(cabecera.to_dict()), 201
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


# ==================== OCR ENDPOINTS ====================

@produccion_bp.route('/ocr/scan-registro', methods=['POST'])
def scan_registro_ocr():
    """
    Escanea una imagen de un registro de producción y extrae los datos.
    
    Acepta:
    - multipart/form-data con campo 'file' (imagen)
    - JSON con campo 'image' (base64)
    """
    from app.services.ocr_service import extract_data_from_image, extract_from_base64
    import os
    
    api_key = os.environ.get('GEMINI_API_KEY')
    if not api_key:
        return jsonify({'error': 'GEMINI_API_KEY not configured on server'}), 500
    
    try:
        # Check if it's a file upload or base64
        if request.files and 'file' in request.files:
            file = request.files['file']
            if file.filename == '':
                return jsonify({'error': 'No file selected'}), 400
            image_bytes = file.read()
            result = extract_data_from_image(image_bytes, api_key)
        elif request.json and 'image' in request.json:
            base64_image = request.json['image']
            result = extract_from_base64(base64_image, api_key)
        else:
            return jsonify({'error': 'No image provided. Send file or base64 image.'}), 400
        
        return jsonify(result), 200 if result.get('success') else 500
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ==================== CONTROL DE PESO ENDPOINTS ====================

@produccion_bp.route('/registros/<int:registro_id>/bultos', methods=['GET'])
def listar_bultos(registro_id):
    """
    Lista los bultos pesados asociados a un Registro de Producción.
    """
    from app.models.control_peso import ControlPeso
    
    bultos = ControlPeso.query.filter_by(registro_id=registro_id).order_by(ControlPeso.hora_registro.asc()).all()
    results = [b.to_dict() for b in bultos]
    
    return jsonify(results), 200

@produccion_bp.route('/registros/<int:registro_id>/bultos', methods=['POST'])
def agregar_bulto(registro_id):
    """
    Registra el peso de un bulto.
    Payload:
    {
        "peso": 15.4,     # Kg
        "color": "ROJO",  # String o ID
        "color_id": 5     # Opcional si se usa ID
    }
    """
    from app.models.control_peso import ControlPeso
    
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Payload JSON requerido'}), 400
        
    registro = db.session.get(RegistroDiarioProduccion, registro_id)
    if not registro:
        return jsonify({'error': 'Registro no encontrado'}), 404
        
    try:
        nuevo_bulto = ControlPeso(
            registro_id=registro_id,
            peso_real_kg=data.get('peso'),
            color_nombre=data.get('color'),
            color_id=data.get('color_id'),
            hora_registro=datetime.now(timezone.utc)
        )
        db.session.add(nuevo_bulto)
        db.session.commit()
        
        return jsonify(nuevo_bulto.to_dict()), 201
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@produccion_bp.route('/bultos/<int:bulto_id>', methods=['DELETE'])
def eliminar_bulto(bulto_id):
    """
    Elimina un registro de peso (bulto).
    """
    from app.models.control_peso import ControlPeso
    
    bulto = db.session.get(ControlPeso, bulto_id)
    if not bulto:
        return jsonify({'error': 'Bulto no encontrado'}), 404
        
    try:
        db.session.delete(bulto)
        db.session.commit()
        return jsonify({'message': 'Bulto eliminado'}), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500
@produccion_bp.route('/registros/<int:registro_id>/validacion-peso', methods=['GET'])
def validar_peso_registro(registro_id):
    """
    Compara el peso total reportado en la cabecera vs la suma de bultos pesados.
    """
    from app.models.control_peso import ControlPeso
    
    registro = db.session.get(RegistroDiarioProduccion, registro_id)
    if not registro:
        return jsonify({'error': 'Registro no encontrado'}), 404
        
    # Sumar pesos de bultos
    bultos = ControlPeso.query.filter_by(registro_id=registro_id).all()
    total_pesado_kg = sum(b.peso_real_kg for b in bultos)
    
    # Peso reportado por maquinista (teórico o manual si existiera campo manual total)
    # Usamos total_kg_real que es el calculado en base a coladas x peso_tiro
    peso_teorico_kg = registro.total_kg_real
    
    diferencia = total_pesado_kg - peso_teorico_kg
    
    return jsonify({
        'registro_id': registro.id,
        'total_pesado_kg': round(total_pesado_kg, 2),
        'peso_teorico_kg': round(peso_teorico_kg, 2),
        'diferencia_kg': round(diferencia, 2),
        'coincide': abs(diferencia) < 5.0 # Margen de tolerancia ejemplo 5kg
    }), 200

from flask import Blueprint, jsonify, request
from app.extensions import db
from app.models.orden import OrdenProduccion, SnapshotComposicionMolde
from app.models.lote import LoteColor
from app.models.recetas import SeCompone, SeColorea
from app.models.materiales import MateriaPrima, Colorante
from app.models.registro import RegistroDiarioProduccion, DetalleProduccionHora
from app.models.producto import Pieza
from app.models.producto import ProductoTerminado, ProductoPieza, ColorProducto
from app.models.molde import Molde, MoldePieza
from datetime import datetime, timezone
from app.models.receta_color import RecetaColorNormalizada

# Definimos el "Blueprint" (un grupo de rutas)
produccion_bp = Blueprint('produccion', __name__)


# ---------------------------------------------------------------------------
# HELPER: Aprender de la OP recién creada (poblamiento dinámico del catálogo)
# ---------------------------------------------------------------------------

def _aprender_de_op(orden):
    """
    Side-effect post-commit de crear_orden:
      A) Si la OP usó snapshot manual + molde_id existente → crear Molde/MoldePieza
         en catálogo SOLO SI NO EXISTEN. Nunca sobreescribe.
      B) Por cada lote con pigmentos → upsert en RecetaColorNormalizada
         usando promedio ponderado de gr/kg.
    Debe llamarse dentro de una transacción activa.
    """

    # ---- A. Upsert de Molde desde snapshot manual -------------------------
    molde_id = orden.molde_id
    if molde_id:
        molde_existente = db.session.get(Molde, molde_id)
        if not molde_existente:
            # Calcular peso_tiro_gr desde los snaps (neto + colada)
            peso_tiro = orden.calculo_peso_tiro_gr or 0.0
            tiempo_ciclo = orden.snapshot_tiempo_ciclo or 30.0
            nuevo_molde = Molde(
                codigo=molde_id,
                nombre=orden.molde or molde_id,
                peso_tiro_gr=peso_tiro,
                tiempo_ciclo_std=tiempo_ciclo,
                activo=True,
                notas='Auto-creado desde OP ' + orden.numero_op
            )
            db.session.add(nuevo_molde)
            db.session.flush()

        # Crear MoldePieza solo para piezas que no existan aún
        for snap in orden.snapshot_composicion:
            if not snap.pieza_sku:
                continue
            existe = MoldePieza.query.filter_by(
                molde_id=molde_id,
                pieza_sku=snap.pieza_sku
            ).first()
            if not existe:
                db.session.add(MoldePieza(
                    molde_id=molde_id,
                    pieza_sku=snap.pieza_sku,
                    cavidades=snap.cavidades,
                    peso_unitario_gr=snap.peso_unit_gr,
                ))

    # ---- B. Upsert de RecetaColorNormalizada --------------------------------
    for lote in orden.lotes:
        meta_kg = lote.meta_kg or 0.0
        if meta_kg <= 0 or not lote.color_id:
            continue

        for colorea in lote.colorantes:
            gramos = colorea.gramos or 0.0
            if gramos <= 0:
                continue

            gr_por_kg = gramos / meta_kg

            # Receta específica (con producto)
            RecetaColorNormalizada.upsert(
                session=db.session,
                color_id=lote.color_id,
                colorante_id=colorea.colorante_id,
                producto_sku=lote.producto_sku_output,
                gr_por_kg_nuevo=gr_por_kg,
            )

            # Receta genérica (sin producto) — siempre actualizar
            if lote.producto_sku_output:
                RecetaColorNormalizada.upsert(
                    session=db.session,
                    color_id=lote.color_id,
                    colorante_id=colorea.colorante_id,
                    producto_sku=None,
                    gr_por_kg_nuevo=gr_por_kg,
                )


@produccion_bp.route('/ordenes', methods=['POST'])
def crear_orden():
    """
    Crea una nueva Orden de Producción completa.
    Soporta dos modos para el snapshot de composición del molde:
      A) auto_snapshot_molde: true  → deriva desde MoldePieza del catálogo
      B) snapshot_composicion: [...]  → lista manual de piezas
    """
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Payload JSON requerido'}), 400

    if not data.get('numero_op'):
        return jsonify({'error': 'Número de OP requerido'}), 400
    if not data.get('maquina_id'):
        return jsonify({'error': 'Máquina requerida'}), 400

    # Debe haber composición (auto o manual)
    auto_snapshot = data.get('auto_snapshot_molde', False)
    composicion_manual = data.get('snapshot_composicion', [])
    if not auto_snapshot and not composicion_manual:
        return jsonify({'error': 'Se requiere auto_snapshot_molde:true o snapshot_composicion[]'}), 400

    try:
        # ----------------------------------------------------------------
        # 1. Cabecera de la Orden
        # ----------------------------------------------------------------
        nueva_orden = OrdenProduccion(
            numero_op               = data.get('numero_op'),
            maquina_id              = data.get('maquina_id'),
            producto                = data.get('producto'),
            producto_sku            = data.get('producto_sku'),
            molde                   = data.get('molde'),
            molde_id                = data.get('molde_id'),
            snapshot_tiempo_ciclo   = data.get('snapshot_tiempo_ciclo', 0.0),
            snapshot_horas_turno    = data.get('snapshot_horas_turno', 24.0),
            snapshot_peso_colada_gr = data.get('snapshot_peso_colada_gr', 0.0),
            tipo_cambio             = data.get('tipo_cambio'),
            fecha_inicio            = (
                datetime.fromisoformat(data['fecha_inicio'])
                if data.get('fecha_inicio') else datetime.now(timezone.utc)
            ),
        )
        db.session.add(nueva_orden)
        db.session.flush()  # necesitamos numero_op para FKs

        # ----------------------------------------------------------------
        # 2. Snapshot de composición del molde
        # ----------------------------------------------------------------
        if auto_snapshot:
            molde_id = data.get('molde_id')
            if not molde_id:
                return jsonify({'error': 'molde_id requerido para auto_snapshot_molde'}), 400

            mp_rows = MoldePieza.query.filter_by(molde_id=molde_id).all()
            if not mp_rows:
                return jsonify({'error': f'Molde {molde_id} no tiene piezas en catálogo (MoldePieza)'}), 400

            for mp in mp_rows:
                snap = SnapshotComposicionMolde(
                    orden_id     = nueva_orden.numero_op,
                    pieza_sku    = mp.pieza_sku,
                    cavidades    = mp.cavidades,
                    peso_unit_gr = mp.peso_unitario_gr,
                )
                db.session.add(snap)
        else:
            # Manual
            if not composicion_manual:
                return jsonify({'error': 'snapshot_composicion no puede estar vacío'}), 400
            for item in composicion_manual:
                snap = SnapshotComposicionMolde(
                    orden_id     = nueva_orden.numero_op,
                    pieza_sku    = item.get('pieza_sku'),
                    cavidades    = item.get('cavidades', 1),
                    peso_unit_gr = item.get('peso_unit_gr', 0.0),
                )
                db.session.add(snap)

        db.session.flush()  # necesitamos los snaps cargados para actualizar_metricas

        # ----------------------------------------------------------------
        # 3. Lotes de Color
        # ----------------------------------------------------------------
        for l_data in data.get('lotes', []):
            # Resolver color
            color_id = l_data.get('color_id')
            c_nombre = l_data.get('color_nombre')
            if not color_id and c_nombre:
                color_obj = ColorProducto.query.filter(ColorProducto.nombre.ilike(c_nombre)).first()
                if color_obj:
                    color_id = color_obj.id
                else:
                    try:
                        max_codigo = db.session.query(db.func.max(ColorProducto.codigo)).scalar() or 0
                        nuevo_color = ColorProducto(nombre=c_nombre.upper(), codigo=max_codigo + 1)
                        db.session.add(nuevo_color)
                        db.session.flush()
                        color_id = nuevo_color.id
                    except Exception as e:
                        print(f"Error creando color on-the-fly: {e}")
                        continue

            # SKU output (usa el de la orden o auto-discover)
            computed_sku = nueva_orden.producto_sku
            if not computed_sku and nueva_orden.molde_id and color_id:
                color_sel = ColorProducto.query.get(color_id)
                familia_color_id = color_sel.familia_id if color_sel else None
                if familia_color_id:
                    piezas_molde = [
                        s.pieza_sku for s in nueva_orden.snapshot_composicion if s.pieza_sku
                    ]
                    if piezas_molde:
                        match_prod = (
                            db.session.query(ProductoTerminado)
                            .join(ProductoPieza, ProductoPieza.producto_terminado_id == ProductoTerminado.cod_sku_pt)
                            .filter(
                                ProductoPieza.pieza_sku.in_(piezas_molde),
                                ProductoTerminado.familia_color_id == familia_color_id,
                            )
                            .first()
                        )
                        if match_prod:
                            computed_sku = match_prod.cod_sku_pt

            nuevo_lote = LoteColor(
                numero_op           = nueva_orden.numero_op,
                color_id            = color_id,
                producto_sku_output  = computed_sku,
                personas            = l_data.get('personas', 1),
                meta_kg             = l_data.get('meta_kg', 0.0),
            )
            db.session.add(nuevo_lote)
            db.session.flush()

            for m_data in l_data.get('materiales', []):
                materia = MateriaPrima.query.filter_by(nombre=m_data.get('nombre')).first()
                if not materia:
                    materia = MateriaPrima(nombre=m_data.get('nombre'), tipo=m_data.get('tipo', 'VIRGEN'))
                    db.session.add(materia)
                    db.session.flush()
                db.session.add(SeCompone(
                    lote_id=nuevo_lote.id,
                    materia_prima_id=materia.id,
                    fraccion=m_data.get('fraccion', 0.0)
                ))

            for p_data in l_data.get('pigmentos', []):
                colorante = Colorante.query.filter_by(nombre=p_data.get('nombre')).first()
                if not colorante:
                    colorante = Colorante(nombre=p_data.get('nombre'))
                    db.session.add(colorante)
                    db.session.flush()
                db.session.add(SeColorea(
                    lote_id=nuevo_lote.id,
                    colorante_id=colorante.id,
                    gramos=p_data.get('gramos', 0.0)
                ))

        # ----------------------------------------------------------------
        # 4. Calcular todo en cascada y guardar
        # ----------------------------------------------------------------
        nueva_orden.actualizar_metricas()
        db.session.commit()

        # ----------------------------------------------------------------
        # 5. SIDE-EFFECTS: Poblamiento dinámico del catálogo (best-effort)
        # ----------------------------------------------------------------
        try:
            _aprender_de_op(nueva_orden)
            db.session.commit()
        except Exception as e_learn:
            db.session.rollback()
            print(f"[WARN] Side-effects post-OP fallaron (OP guardada igual): {e_learn}")

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


@produccion_bp.route('/ordenes/<numero_op>/estado', methods=['PUT'])
def toggle_estado_orden(numero_op):
    """
    Cambia el estado de una Orden (activa/cerrada).
    Registra el cambio en el historial.
    
    Payload: { 
        "activa": true/false,
        "usuario": "opcional",
        "motivo": "opcional"
    }
    """
    from app.models.historial_estado import registrar_cambio_estado
    
    orden = db.session.get(OrdenProduccion, numero_op)
    if not orden:
        return jsonify({'error': 'Orden no encontrada'}), 404
    
    data = request.get_json()
    if data is None or 'activa' not in data:
        return jsonify({'error': 'Campo activa requerido'}), 400
    
    try:
        nuevo_estado = bool(data['activa'])
        usuario = data.get('usuario')
        motivo = data.get('motivo')
        
        historial = registrar_cambio_estado(orden, nuevo_estado, usuario, motivo)
        
        if not historial:
            return jsonify({
                'message': 'Sin cambios (mismo estado)',
                'activa': orden.activa
            }), 200
        
        return jsonify({
            'message': f"Orden {'abierta' if orden.activa else 'cerrada'} correctamente",
            'activa': orden.activa,
            'historial': historial.to_dict()
        }), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@produccion_bp.route('/ordenes/<numero_op>/historial', methods=['GET'])
def obtener_historial_orden(numero_op):
    """
    Retorna el historial de cambios de estado de una orden.
    Ordenado del más reciente al más antiguo.
    """
    from app.models.historial_estado import HistorialEstadoOrden
    
    orden = db.session.get(OrdenProduccion, numero_op)
    if not orden:
        return jsonify({'error': 'Orden no encontrada'}), 404
    
    historial = HistorialEstadoOrden.query.filter_by(
        numero_op=numero_op
    ).order_by(HistorialEstadoOrden.fecha.desc()).all()
    
    return jsonify({
        'numero_op': numero_op,
        'activa': orden.activa,
        'historial': [h.to_dict() for h in historial]
    })


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


@produccion_bp.route('/registros', methods=['GET'])
def obtener_todos_registros():
    """
    Obtiene todos los registros diarios de producción (para dashboard y vista global).
    Soporta filtros: ?fecha=YYYY-MM-DD&orden_id=OP-XXX&limit=N
    """
    from datetime import date
    
    query = RegistroDiarioProduccion.query
    
    # Filtros opcionales
    fecha_str = request.args.get('fecha')
    if fecha_str:
        try:
            fecha_filter = datetime.fromisoformat(fecha_str).date()
            query = query.filter(RegistroDiarioProduccion.fecha == fecha_filter)
        except:
            pass
    
    orden_id = request.args.get('orden_id')
    if orden_id:
        query = query.filter(RegistroDiarioProduccion.orden_id == orden_id)
    
    limit = request.args.get('limit', type=int)
    
    query = query.order_by(RegistroDiarioProduccion.fecha.desc(), RegistroDiarioProduccion.id.desc())
    
    if limit:
        registros = query.limit(limit).all()
    else:
        registros = query.all()
    
    resultados = []
    for r in registros:
        fila = {
            "id": r.id,
            "orden_id": r.orden_id,
            "fecha": r.fecha.isoformat() if r.fecha else None,
            "turno": r.turno,
            "maquina_id": r.maquina_id,
            "total_coladas": r.total_coladas_calculada,
            "total_kg": r.total_kg_real,
            "total_piezas": r.total_piezas_buenas,
            "orden_activa": r.orden.activa if r.orden else True
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
        # Validar que la orden esté activa
        if not orden.activa:
            return jsonify({'error': 'No se pueden crear registros para una Orden cerrada'}), 400
            
        # Validar minimos
        if 'maquina_id' not in data or 'fecha' not in data:
             return jsonify({'error': 'Faltan campos obligatorios (maquina_id, fecha)'}), 400

        # Crear Cabecera
        cabecera = RegistroDiarioProduccion(
            orden_id              = orden.numero_op,
            maquina_id            = data.get('maquina_id'),
            fecha                 = datetime.fromisoformat(data.get('fecha')).date(),
            turno                 = data.get('turno'),
            hora_inicio           = data.get('hora_inicio'),
            colada_inicial        = data.get('colada_inicial', 0),
            colada_final          = data.get('colada_final', 0),
            tiempo_ciclo_reportado = data.get('tiempo_ciclo', 0.0),
            tiempo_enfriamiento   = data.get('tiempo_enfriamiento', 0.0),
            cantidad_por_hora_meta = data.get('meta_hora', 0),

            # Snapshots desde los valores cacheados de la Orden
            snapshot_cavidades      = orden.calculo_cavidades_totales,
            snapshot_peso_neto_gr   = orden.calculo_peso_neto_golpe,
            snapshot_peso_colada_gr = orden.snapshot_peso_colada_gr,
            snapshot_peso_extra_gr  = 0.0,
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



@produccion_bp.route('/ordenes/<numero_op>/metricas', methods=['PUT'])
def actualizar_metricas_orden(numero_op):
    """
    Permite editar metricas tecnicas de una orden ACTIVA.
    Caso de uso: Molde Dañado (reduccion de cavidades), ajuste de ciclo real.
    """
    orden = db.session.get(OrdenProduccion, numero_op)
    if not orden:
        return jsonify({'error': 'Orden no encontrada'}), 404
        
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Payload requerido'}), 400
        
    try:
        # Solo permitimos editar ciertos campos tecnicos
        if 'snapshot_tiempo_ciclo' in data:
            orden.snapshot_tiempo_ciclo = data['snapshot_tiempo_ciclo']

        if 'snapshot_horas_turno' in data:
            orden.snapshot_horas_turno = data['snapshot_horas_turno']

        if 'snapshot_peso_colada_gr' in data:
            orden.snapshot_peso_colada_gr = data['snapshot_peso_colada_gr']

        # Recalcular todo en cascada
        orden.actualizar_metricas()
        db.session.commit()

        return jsonify(orden.to_dict()), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

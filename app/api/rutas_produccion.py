from flask import Blueprint, jsonify, request
from app.extensions import db
from app.models.orden import OrdenProduccion, SnapshotComposicionMolde
from app.models.lote import LoteColor, LoteSalidaPiezaColor
from app.models.recetas import SeCompone, SeColorea
from app.models.materiales import MateriaPrima, Colorante
from app.models.scm_catalogos import ScmMaterial
from app.models.registro import RegistroDiarioProduccion, DetalleProduccionHora
from app.models.producto import ProductoTerminado, ProductoPieza, ColorProduccion, ColorBase, PiezaColor
from app.models.receta_color import RecetaColorMaestra
from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy.exc import IntegrityError
from app.models.receta_color import RecetaColorNormalizada
from app.models.maquina import Maquina, TipoMaquina
from app.services.scm_material_service import (
    create_colorante_with_scm,
    create_materia_prima_with_scm,
    ensure_colorante_identity,
    ensure_materia_prima_identity,
)
from app.services.order_integrity_service import (
    OrderIntegrityError,
    validate_order_creation,
)

# Definimos el "Blueprint" (un grupo de rutas)
produccion_bp = Blueprint('produccion', __name__)


def _validated_recipe_snapshot(raw_recipe, *, color_id, producto_sku):
    if raw_recipe in (None, {}):
        return None
    if not isinstance(raw_recipe, dict):
        raise ValueError('receta_aplicada debe ser un objeto')
    try:
        recipe_id = int(raw_recipe.get('id'))
        revision = int(raw_recipe.get('revision'))
    except (TypeError, ValueError) as exc:
        raise ValueError('receta_aplicada requiere id y revision válidos') from exc
    recipe = db.session.get(RecetaColorMaestra, recipe_id)
    if recipe is None:
        raise ValueError('La receta aplicada ya no existe')
    if recipe.estado != 'APROBADA':
        raise ValueError('La receta aplicada ya no está aprobada')
    if recipe.revision != revision:
        raise ValueError('La revisión de receta aplicada ya no coincide')
    if recipe.color_produccion_id != color_id:
        raise ValueError('La receta aplicada no corresponde al color del lote')
    if recipe.producto_sku is not None and recipe.producto_sku != producto_sku:
        raise ValueError('La receta aplicada no corresponde al producto de la OP')
    return recipe


# ---------------------------------------------------------------------------
# HELPER: Aprender de la OP recién creada (poblamiento dinámico del catálogo)
# ---------------------------------------------------------------------------

def _aprender_de_op(orden):
    """
    Side-effect post-commit de crear_orden:
      Por cada lote con pigmentos → upsert en RecetaColorNormalizada
         usando promedio ponderado de gr/kg.

    Los snapshots ya no crean Molde, Pieza ni MoldePieza. Esas identidades se
    validan antes de la OP; una importación legacy no reconciliada conserva
    evidencia en ``pieza_sku_legacy`` hasta que exista una decisión humana.
    Debe llamarse dentro de una transacción activa.
    """

    # ---- Upsert de RecetaColorNormalizada ----------------------------------
    for lote in orden.lotes:
        meta_kg = lote.meta_kg or 0.0
        if meta_kg <= 0 or not lote.color_produccion_id:
            continue

        for colorea in lote.colorantes:
            gramos = colorea.gramos or 0.0
            if gramos <= 0:
                continue

            gr_por_kg = gramos / meta_kg

            # Receta específica (con producto)
            RecetaColorNormalizada.upsert(
                session=db.session,
                color_produccion_id=lote.color_produccion_id,
                colorante_id=colorea.colorante_id,
                producto_sku=lote.producto_sku_output,
                gr_por_kg_nuevo=gr_por_kg,
            )

            # Receta genérica (sin producto) — siempre actualizar
            if lote.producto_sku_output:
                RecetaColorNormalizada.upsert(
                    session=db.session,
                    color_produccion_id=lote.color_produccion_id,
                    colorante_id=colorea.colorante_id,
                    producto_sku=None,
                    gr_por_kg_nuevo=gr_por_kg,
                )


@produccion_bp.route('/ordenes', methods=['POST'])
def crear_orden():
    """
    Crea una nueva Orden de Producción completa.
    Soporta dos modos para el snapshot de composición del molde:
      A) auto_snapshot_molde: true  → deriva desde Pieza del catálogo
      B) snapshot_composicion: [...]  → lista manual de piezas
    """
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Payload JSON requerido'}), 400

    try:
        # Resuelve y valida el catálogo antes de insertar la cabecera. Las
        # variantes PiezaColor faltantes se crean dentro de esta transacción.
        integrity = validate_order_creation(data, session=db.session)
        maquina = integrity.maquina
        molde = integrity.molde
        producto = integrity.producto
        
        # ----------------------------------------------------------------
        # 1. Cabecera de la Orden
        # ----------------------------------------------------------------
        nueva_orden = OrdenProduccion(
            numero_op               = str(data.get('numero_op')).strip(),
            maquina_id              = maquina.id,
            maquina_codigo_snapshot = maquina.codigo if maquina else None,
            maquina_nombre_snapshot = maquina.nombre if maquina else None,
            producto                = producto.producto if producto else data.get('producto'),
            producto_sku            = producto.cod_sku_pt if producto else None,
            molde                   = molde.nombre if molde else data.get('molde'),
            molde_id                = molde.codigo if molde else None,
            snapshot_tiempo_ciclo   = integrity.snapshot_tiempo_ciclo,
            snapshot_horas_turno    = integrity.snapshot_horas_turno,
            snapshot_peso_colada_gr = integrity.snapshot_peso_colada_gr,
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
        snapshot_records = {}
        for item in integrity.snapshot_rows:
            snapshot = SnapshotComposicionMolde(
                orden_id     = nueva_orden.numero_op,
                pieza_id     = item.get('pieza_id'),
                pieza_codigo_snapshot = item.get('pieza_codigo_snapshot'),
                pieza_nombre_snapshot = item.get('pieza_nombre_snapshot'),
                pieza_sku_legacy = item.get('pieza_sku_legacy'),
                cavidades    = item['cavidades'],
                peso_unit_gr = item['peso_unit_gr'],
            )
            db.session.add(snapshot)
            snapshot_records[item.get('pieza_id')] = snapshot

        db.session.flush()  # necesitamos los snaps cargados para actualizar_metricas

        # ----------------------------------------------------------------
        # 3. Lotes de Color
        # ----------------------------------------------------------------
        for index, l_data in enumerate(data.get('lotes', [])):
            color_produccion_id = integrity.lot_color_ids[index]
            recipe = _validated_recipe_snapshot(
                l_data.get('receta_aplicada'),
                color_id=color_produccion_id,
                producto_sku=nueva_orden.producto_sku,
            )

            # Una OP directa sin producto queda como reposición de PiezaColor.
            # Nunca se infiere un PT por una coincidencia parcial y first().
            computed_sku = nueva_orden.producto_sku

            nuevo_lote = LoteColor(
                numero_op           = nueva_orden.numero_op,
                color_produccion_id = color_produccion_id,
                producto_sku_output  = computed_sku,
                receta_color_maestra_id = recipe.id if recipe else None,
                receta_revision_snapshot = recipe.revision if recipe else None,
                receta_nombre_snapshot = recipe.nombre_variante if recipe else None,
                receta_base_virgen_kg_snapshot = recipe.base_virgen_kg if recipe else None,
                personas            = l_data.get('personas', 1),
                meta_kg             = l_data.get('meta_kg', 0.0),
            )
            db.session.add(nuevo_lote)
            db.session.flush()

            for m_data in l_data.get('materiales', []):
                requested_material_id = m_data.get('material_id')
                material_identity = (
                    db.session.get(ScmMaterial, requested_material_id)
                    if requested_material_id
                    else None
                )
                if requested_material_id and material_identity is None:
                    raise ValueError('La materia prima seleccionada ya no existe')
                if material_identity is not None and not material_identity.activo:
                    raise ValueError('La materia prima seleccionada está inactiva')
                if material_identity is not None and material_identity.clase != 'MATERIA_PRIMA':
                    raise ValueError('El material seleccionado no es una materia prima')
                materia = (
                    material_identity.materia_prima
                    if material_identity is not None
                    else MateriaPrima.query.filter_by(nombre=m_data.get('nombre')).first()
                )
                if not materia:
                    materia = create_materia_prima_with_scm(
                        session=db.session,
                        nombre=m_data.get('nombre'),
                        tipo=m_data.get('tipo', 'VIRGEN'),
                        categoria_codigo='LEGACY_POR_CONFIGURAR',
                    )
                ensure_materia_prima_identity(
                    session=db.session,
                    materia_prima=materia,
                    categoria_codigo='LEGACY_POR_CONFIGURAR',
                )
                db.session.flush()
                db.session.add(SeCompone(
                    lote_id=nuevo_lote.id,
                    materia_prima_id=materia.id,
                    fraccion=m_data.get('fraccion', 0.0)
                ))

            for p_data in l_data.get('pigmentos', []):
                requested_material_id = p_data.get('material_id')
                material_identity = (
                    db.session.get(ScmMaterial, requested_material_id)
                    if requested_material_id
                    else None
                )
                if requested_material_id and material_identity is None:
                    raise ValueError('El colorante o aditivo seleccionado ya no existe')
                if material_identity is not None and not material_identity.activo:
                    raise ValueError('El colorante o aditivo seleccionado está inactivo')
                if material_identity is not None and material_identity.clase != 'COLORANTE':
                    raise ValueError('El material seleccionado no es un colorante o aditivo')
                colorante = (
                    material_identity.colorante
                    if material_identity is not None
                    else Colorante.query.filter_by(nombre=p_data.get('nombre')).first()
                )
                if not colorante:
                    colorante = create_colorante_with_scm(
                        session=db.session,
                        nombre=p_data.get('nombre'),
                    )
                ensure_colorante_identity(
                    session=db.session,
                    colorante=colorante,
                )
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
        for lote in nueva_orden.lotes:
            if lote.color_produccion_id is None:
                continue
            for pieza_id, snapshot in snapshot_records.items():
                if pieza_id is None:
                    continue
                variant = PiezaColor.query.filter_by(
                    pieza_id=pieza_id,
                    color_produccion_id=lote.color_produccion_id,
                ).one()
                quantity = Decimal(str(lote.calculo_coladas or 0)) * Decimal(
                    snapshot.cavidades
                )
                net_kg = quantity * Decimal(str(snapshot.peso_unit_gr)) / Decimal('1000')
                db.session.add(LoteSalidaPiezaColor(
                    lote_color_id=lote.id,
                    snapshot_pieza_id=snapshot.id,
                    pieza_id=pieza_id,
                    pieza_color_sku=variant.sku,
                    cavidades_snapshot=snapshot.cavidades,
                    peso_unitario_snapshot_gr=snapshot.peso_unit_gr,
                    cantidad_objetivo=quantity,
                    kg_objetivo_neto=net_kg,
                    cantidad_buena_real=0,
                    cantidad_rechazada_real=0,
                    kg_bueno_real=0,
                ))
        db.session.flush()
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

        response = nueva_orden.to_dict()
        generated = [
            item for item in integrity.pending_variants if item.get('pieza_sku')
        ]
        if generated:
            response['catalogo_autocreado'] = {'piezas_color': generated}
        return jsonify(response), 201

    except OrderIntegrityError as exc:
        db.session.rollback()
        payload = {'error': exc.message, 'codigo': exc.code}
        if exc.details:
            payload['details'] = exc.details
        return jsonify(payload), exc.status
    except ValueError as exc:
        db.session.rollback()
        return jsonify({'error': str(exc), 'codigo': 'PAYLOAD_INVALIDO'}), 400
    except IntegrityError:
        db.session.rollback()
        return jsonify({
            'error': 'El catálogo cambió mientras se creaba la OP; vuelva a validar y reintente',
            'codigo': 'INTEGRIDAD_CATALOGO_CONFLICTO',
        }), 409
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
    ).order_by(
        HistorialEstadoOrden.fecha.desc(),
        HistorialEstadoOrden.id.desc(),
    ).all()
    
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

        from app.models.maquina import Maquina
        maquina = db.session.get(Maquina, data.get('maquina_id'))

        # Crear Cabecera
        cabecera = RegistroDiarioProduccion(
            orden_id              = orden.numero_op,
            maquina_id            = maquina.id if maquina else data.get('maquina_id'),
            maquina_codigo_snapshot = maquina.codigo if maquina else None,
            maquina_nombre_snapshot = maquina.nombre if maquina else None,
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
        peso_tiro = cabecera.snapshot_peso_neto_gr + (cabecera.snapshot_peso_colada_gr or 0.0) + (cabecera.snapshot_peso_extra_gr or 0.0)
        
        for d in detalles_data:
            # Soportar legacy (maquinista) o nuevo (trabajador_id)
            trabajador_id = d.get('trabajador_id')
            maquinista_snap = d.get('maquinista_snapshot') or d.get('maquinista')

            detalle = DetalleProduccionHora(
                registro_id=cabecera.id,
                hora=d.get('hora'),
                trabajador_id=trabajador_id,
                maquinista_snapshot=maquinista_snap,
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

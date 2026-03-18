from flask import Blueprint, jsonify, request
from app.extensions import db
from app.models.kardex import InventarioManga, MovimientoKardex
from datetime import datetime, timezone

kardex_bp = Blueprint('kardex', __name__)


def _parsear_pesaje_id_del_qr(codigo_qr: str) -> int | None:
    """
    Extrae el pesaje_id (primer campo) del string QR del sticker.
    Formato: ID,MOLDE,MAQUINA,NRO_OP,TURNO,FECHA_OT,NRO_OT,OPERADOR,COLOR,FECHA_HORA,PESO_KG
    """
    try:
        parts = codigo_qr.replace(';', ',').split(',')
        return int(parts[0])
    except (ValueError, IndexError):
        return None


def _parsear_datos_qr(codigo_qr: str) -> dict:
    """
    Extrae datos descriptivos del QR para cachear en InventarioManga.
    Formato: ID,MOLDE,MAQUINA,NRO_OP,TURNO,FECHA_OT,NRO_OT,OPERADOR,COLOR,FECHA_HORA,PESO_KG,PIEZA_NOMBRE,EXTRA1,EXTRA2,EXTRA3
    """
    parts = codigo_qr.replace(';', ',').split(',')
    return {
        'pesaje_id': int(parts[0]) if len(parts) > 0 and parts[0].isdigit() else None,
        'molde': parts[1] if len(parts) > 1 else None,
        'nro_op': parts[3] if len(parts) > 3 else None,
        'color': parts[8] if len(parts) > 8 else None,
        'peso_kg': float(parts[10]) if len(parts) > 10 and parts[10] else None,
        'pieza_nombre': parts[11] if len(parts) > 11 and parts[11] else None,
        'extra1': parts[12] if len(parts) > 12 and parts[12] else None,
        'extra2': parts[13] if len(parts) > 13 and parts[13] else None,
        'extra3': parts[14] if len(parts) > 14 and parts[14] else None,
    }


def _clasificar_operacion(tipo: str) -> str:
    """
    Mapea los tipo_operacion granulares de la app Android a la lógica base.
    INGRESO-PROD, INGRESO-DEV, etc. → ENTRADA
    SAL-ARMAR, SAL-VENTA, SAL-MERMA, etc. → SALIDA
    MOV-INTERNO, etc. → MOVIMIENTO
    """
    tipo_upper = tipo.upper()
    if tipo_upper.startswith('INGRESO') or tipo_upper in ['ENTRADA', 'DEVOLUCION_NO_ARMADO']:
        return 'ENTRADA'
    elif tipo_upper.startswith('SAL') or tipo_upper in ['SALIDA', 'ARMAR_PAQUETES', 'DONACIONES', 'MERMA_MOLINO', 'TRANSFORMACIONES']:
        return 'SALIDA'
    elif tipo_upper.startswith('MOV') or tipo_upper in ['MOVIMIENTO', 'MOVIMIENTOS']:
        return 'MOVIMIENTO'
    return tipo_upper


# ============================================================
# ENDPOINT UNIFICADO DE MOVIMIENTOS
# ============================================================

@kardex_bp.route('/kardex/movimientos', methods=['POST'])
def registrar_movimiento():
    """
    Endpoint unificado para ENTRADA, SALIDA y MOVIMIENTO de mangas.
    
    Payload:
    {
        "codigo_qr": "123;MOLDE;MAQ;OP1354;DIURNO;2026-01-03;0001;Admin;ROJO;2026-01-03/14:30:00;5.2",
        "tipo_operacion": "INGRESO-PROD" | "SAL-ARMAR" | "MOV-INTERNO" | ...,
        "locacion_origen": "ZONA_PRODUCCION",
        "locacion_destino": "ALMACEN_PRINCIPAL",
        "operario_id": "user@gmail.com",
        "metadatos": "{}",
        "timestamp": "2026-03-13T13:00:00Z"
    }
    """
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Payload JSON requerido'}), 400

    codigo_qr = data.get('codigo_qr', '')
    tipo_operacion_raw = data.get('tipo_operacion', '')
    locacion_origen = data.get('locacion_origen', '')
    locacion_destino = data.get('locacion_destino', '')
    operario_id = data.get('operario_id', '')
    metadatos = data.get('metadatos', '')
    timestamp_str = data.get('timestamp')
    
    import json
    metadatos_dict = {}
    if metadatos:
        try:
            metadatos_dict = json.loads(metadatos)
        except Exception:
            pass

    if not codigo_qr or not tipo_operacion_raw:
        return jsonify({'error': 'codigo_qr y tipo_operacion son requeridos'}), 400

    # Parsear pesaje_id del QR
    datos_qr = _parsear_datos_qr(codigo_qr)
    pesaje_id = datos_qr.get('pesaje_id')
    if not pesaje_id:
        return jsonify({'error': 'No se pudo extraer pesaje_id del QR. ¿El sticker incluye el ID?'}), 400

    # Parsear timestamp
    try:
        ts = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00')) if timestamp_str else datetime.now(timezone.utc)
    except (ValueError, AttributeError):
        ts = datetime.now(timezone.utc)

    # Clasificar operación
    tipo_base = _clasificar_operacion(tipo_operacion_raw)

    try:
        if tipo_base == 'ENTRADA':
            if tipo_operacion_raw == 'INGRESO-PROD':
                # ── 1A. INGRESO-PROD: El bulto no debe existir ya en inventario ──
                existente = InventarioManga.query.filter_by(pesaje_id=pesaje_id).first()
                if existente:
                    return jsonify({
                        'error': f'Este bulto (pesaje #{pesaje_id}) ya fue registrado en inventario',
                        'estado_actual': existente.estado,
                        'locacion': existente.locacion_actual
                    }), 409
    
                manga = InventarioManga(
                    pesaje_id=pesaje_id,
                    nro_op=datos_qr.get('nro_op') or metadatos_dict.get('nro_op'),
                    molde=datos_qr.get('molde') or metadatos_dict.get('molde'),
                    color=datos_qr.get('color') or metadatos_dict.get('color'),
                    peso_kg=datos_qr.get('peso_kg') or data.get('peso_kg') or metadatos_dict.get('peso_kg', 0.0),
                    pieza_nombre=datos_qr.get('pieza_nombre') or data.get('pieza_nombre'),
                    extra1=datos_qr.get('extra1') or data.get('extra1'),
                    extra2=datos_qr.get('extra2') or data.get('extra2'),
                    extra3=datos_qr.get('extra3') or data.get('extra3'),
                    locacion_actual=locacion_destino or locacion_origen,
                    estado='EN_INVENTARIO',
                    fecha_ingreso=ts,
                )
                db.session.add(manga)
                db.session.flush()
                
            elif tipo_operacion_raw in ['INGRESO-DEV', 'DEVOLUCION_NO_ARMADO']:
                # ── 1B. INGRESO-DEV (Devolución): El bulto debe existir ──
                manga = InventarioManga.query.filter_by(pesaje_id=pesaje_id).first()
                if not manga:
                    return jsonify({'error': f'Manga con pesaje #{pesaje_id} no encontrada para devolución'}), 404
                
                manga.estado = 'EN_INVENTARIO'
                manga.locacion_actual = locacion_destino or locacion_origen
                manga.fecha_despacho = None # Limpiar en caso tuviera fecha de salida previa

            mov = MovimientoKardex(
                inventario_manga_id=manga.id,
                tipo_operacion=tipo_operacion_raw,
                locacion_origen=locacion_origen,
                locacion_destino=locacion_destino,
                operario_id=operario_id,
                metadatos=metadatos,
                timestamp=ts,
            )
            db.session.add(mov)
            db.session.commit()

            return jsonify({
                'success': True,
                'mensaje': f'Manga #{pesaje_id} ingresada a inventario en {manga.locacion_actual}',
                'manga': manga.to_dict()
            }), 201

        elif tipo_base == 'SALIDA':
            # ── MÓDULO 2: SALIDAS ──
            manga = InventarioManga.query.filter_by(pesaje_id=pesaje_id).first()
            if not manga:
                return jsonify({'error': f'Manga con pesaje #{pesaje_id} no encontrada en inventario'}), 404

            if manga.estado in ['DESPACHADO', 'CONSUMIDO', 'MERMA', 'INACTIVO']:
                return jsonify({
                    'error': f'Este bulto ya no está disponible (Estado: {manga.estado})',
                    'estado_actual': manga.estado
                }), 409

            if tipo_operacion_raw in ['SAL-ARMAR', 'ARMAR_PAQUETES']:
                # 2A. Salida a Armado
                manga.estado = 'CONSUMIDO' # o EN_ARMADO según convención
                manga.locacion_actual = locacion_destino or 'ZONA_ARMADO'
            
            elif tipo_operacion_raw == 'SAL-DESPACHO':
                # 2B. Salida por Despacho al Cliente
                manga.estado = 'DESPACHADO'
                manga.fecha_despacho = ts
                manga.locacion_actual = locacion_destino or 'CLIENTE_FINAL'
                
            elif tipo_operacion_raw in ['SAL-MERMA', 'MERMA_MOLINO']:
                # 2C. Salida por Merma / Falla o Destrucción en Molino
                manga.estado = 'MERMA'
                manga.locacion_actual = locacion_destino or 'MOLINO_DESTRUCCION'
                
            elif tipo_operacion_raw == 'DONACIONES':
                manga.estado = 'DESPACHADO'
                manga.fecha_despacho = ts
                manga.locacion_actual = locacion_destino or 'DONACION_EXTERNA'
                
            elif tipo_operacion_raw == 'TRANSFORMACIONES':
                manga.estado = 'CONSUMIDO'
                manga.locacion_actual = locacion_destino or 'ALMACEN_PARTES'
                
            else:
                # Fallback genérico para otras salidas
                manga.estado = 'DESPACHADO'
                manga.locacion_actual = locacion_destino or 'AFUERA'

            mov = MovimientoKardex(
                inventario_manga_id=manga.id,
                tipo_operacion=tipo_operacion_raw,
                locacion_origen=locacion_origen,
                locacion_destino=locacion_destino,
                operario_id=operario_id,
                metadatos=metadatos,
                timestamp=ts,
            )
            db.session.add(mov)
            db.session.commit()

            return jsonify({
                'success': True,
                'mensaje': f'Manga #{pesaje_id} despachada desde {locacion_origen}',
                'manga': manga.to_dict()
            }), 200

        elif tipo_base == 'MOVIMIENTO':
            # ── MÓDULO 3: MOVIMIENTOS INTERNOS (Flujo de 2 pasos vía TRANSITO) ──
            manga = InventarioManga.query.filter_by(pesaje_id=pesaje_id).first()
            if not manga:
                return jsonify({'error': f'Manga con pesaje #{pesaje_id} no encontrada en inventario'}), 404

            if manga.estado not in ['EN_INVENTARIO', 'TRANSITO']:
                return jsonify({
                    'error': f'No se puede mover: estado actual es {manga.estado}',
                    'estado_actual': manga.estado
                }), 409

            # Lógica de 2 pasos
            if locacion_destino == 'TRANSITO':
                # Paso 1: Iniciar Movimiento (Sale del origen)
                manga.estado = 'TRANSITO'
                manga.locacion_actual = 'TRANSITO'
            elif locacion_origen == 'TRANSITO':
                # Paso 2: Finalizar Movimiento (Llega al destino)
                if manga.estado != 'TRANSITO':
                    return jsonify({'error': 'El bulto debe estar en TRANSITO para ser recibido en destino.'}), 409
                manga.estado = 'EN_INVENTARIO'
                manga.locacion_actual = locacion_destino
            else:
                # Fallback: Movimiento atómico directo (origen -> destino en un solo escaneo)
                if locacion_origen == locacion_destino:
                    return jsonify({'error': 'Locación de origen y destino no pueden ser iguales'}), 400
                manga.locacion_actual = locacion_destino

            mov = MovimientoKardex(
                inventario_manga_id=manga.id,
                tipo_operacion=tipo_operacion_raw,
                locacion_origen=locacion_origen,
                locacion_destino=locacion_destino,
                operario_id=operario_id,
                metadatos=metadatos,
                timestamp=ts,
            )
            db.session.add(mov)
            db.session.commit()

            return jsonify({
                'success': True,
                'mensaje': f'Manga #{pesaje_id} movida de {locacion_origen} a {locacion_destino}',
                'manga': manga.to_dict()
            }), 200

        else:
            return jsonify({'error': f'tipo_operacion "{tipo_operacion_raw}" no reconocido'}), 400

    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


# ============================================================
# CONSULTAS
# ============================================================

@kardex_bp.route('/kardex/manga/<int:pesaje_id>', methods=['GET'])
def consultar_manga(pesaje_id):
    """Consulta el estado actual de un bulto por su pesaje_id."""
    manga = InventarioManga.query.filter_by(pesaje_id=pesaje_id).first()
    if not manga:
        return jsonify({'error': f'Manga con pesaje #{pesaje_id} no encontrada'}), 404

    movimientos = MovimientoKardex.query.filter_by(
        inventario_manga_id=manga.id
    ).order_by(MovimientoKardex.timestamp.desc()).all()

    return jsonify({
        'manga': manga.to_dict(),
        'movimientos': [m.to_dict() for m in movimientos]
    }), 200


@kardex_bp.route('/kardex/inventario', methods=['GET'])
def listar_inventario():
    """
    Lista el inventario actual con filtros opcionales.
    
    Query params:
        - locacion: filtrar por locación actual
        - nro_op: filtrar por OP
        - color: filtrar por color
        - estado: filtrar por estado (default: EN_INVENTARIO)
    """
    query = InventarioManga.query

    estado = request.args.get('estado', 'EN_INVENTARIO')
    if estado:
        query = query.filter_by(estado=estado)

    locacion = request.args.get('locacion')
    if locacion:
        query = query.filter_by(locacion_actual=locacion)

    nro_op = request.args.get('nro_op')
    if nro_op:
        query = query.filter_by(nro_op=nro_op)

    color = request.args.get('color')
    if color:
        query = query.filter(InventarioManga.color.ilike(f'%{color}%'))

    mangas = query.order_by(InventarioManga.fecha_ingreso.desc()).all()

    # Resumen de stock
    total_mangas = len(mangas)
    total_kg = sum(m.peso_kg or 0 for m in mangas)

    # Agrupación por locación
    por_locacion = {}
    for m in mangas:
        loc = m.locacion_actual or 'SIN_LOCACION'
        if loc not in por_locacion:
            por_locacion[loc] = {'cantidad': 0, 'peso_kg': 0}
        por_locacion[loc]['cantidad'] += 1
        por_locacion[loc]['peso_kg'] += m.peso_kg or 0

    return jsonify({
        'total_mangas': total_mangas,
        'total_kg': round(total_kg, 2),
        'por_locacion': por_locacion,
        'mangas': [m.to_dict() for m in mangas]
    }), 200

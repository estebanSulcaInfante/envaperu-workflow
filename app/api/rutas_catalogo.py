"""
Rutas API para el Catálogo de Productos y Piezas (SKU).
Incluye endpoints de listado y búsqueda.
"""
import math

from flask import Blueprint, Response, jsonify, request
from app.extensions import db
from app.models.producto import ProductoTerminado, PiezaColor, ProductoPieza, ColorProduccion, ColorBase, Linea, Familia, FamiliaColor, LineaFamilia
from app.models.scm_commercial import ScmPresentacionComercial
from app.services.catalog_classification_service import (
    ClassificationError,
    classification_usage,
    ensure_linea_familia,
    validate_linea_familia,
)
from app.services.catalog_code_generator import (
    generar_codigo_catalogo,
    generar_numero_catalogo,
)
from app.services.color_recipe_service import (
    ColorRecipeError,
    create_recipe,
    deactivate_recipe,
    find_default_recipe,
    list_recipe_ingredients,
    list_recipes,
    normalize_hex,
    serialize_recipe,
    update_recipe,
)
from app.services.order_integrity_service import (
    OrderIntegrityError,
    validate_order_header_prerequisites,
    validate_order_prerequisites,
)
from app.services.catalog_image_storage import (
    CatalogImageStorageError,
    get_catalog_image_storage,
    has_catalog_image,
)
from sqlalchemy import or_

catalogo_bp = Blueprint('catalogo', __name__)

IMAGE_MIME_TYPES = {'image/jpeg', 'image/png', 'image/webp'}
MAX_CATALOG_IMAGE_BYTES = 2 * 1024 * 1024


def _read_catalog_image():
    image = request.files.get('imagen')
    if image is None or not image.filename:
        return None, (jsonify({'error': 'Selecciona una imagen.'}), 400)
    mime = str(image.mimetype or '').lower()
    if mime not in IMAGE_MIME_TYPES:
        return None, (jsonify({
            'error': 'Formato no permitido. Usa JPG, PNG o WebP.',
            'codigo': 'IMAGEN_FORMATO_INVALIDO',
        }), 415)
    content = image.stream.read(MAX_CATALOG_IMAGE_BYTES + 1)
    if not content:
        return None, (jsonify({'error': 'La imagen esta vacia.'}), 400)
    if len(content) > MAX_CATALOG_IMAGE_BYTES:
        return None, (jsonify({
            'error': 'La imagen supera el limite de 2 MB.',
            'codigo': 'IMAGEN_DEMASIADO_GRANDE',
        }), 413)
    signatures = {
        'image/png': content.startswith(b'\x89PNG\r\n\x1a\n'),
        'image/jpeg': content.startswith(b'\xff\xd8\xff'),
        'image/webp': content.startswith(b'RIFF') and content[8:12] == b'WEBP',
    }
    if not signatures[mime]:
        return None, (jsonify({
            'error': 'El contenido no coincide con el formato declarado.',
            'codigo': 'IMAGEN_CONTENIDO_INVALIDO',
        }), 415)
    return (mime, content), None


def _image_response(entity):
    if entity is None or not has_catalog_image(entity):
        return jsonify({'error': 'Imagen no encontrada'}), 404
    try:
        image = get_catalog_image_storage().load(entity)
    except CatalogImageStorageError as exc:
        return jsonify({
            'error': str(exc),
            'codigo': 'IMAGEN_STORAGE_NO_DISPONIBLE',
        }), 503
    if image is None:
        return jsonify({'error': 'Imagen no encontrada'}), 404
    return Response(
        image.content,
        mimetype=image.mime_type,
        headers={'Cache-Control': 'private, max-age=300'},
    )


def _next_available_numeric_code(model, key):
    """Reserva correlativos hasta hallar uno libre en un catálogo entero legacy."""

    while True:
        candidate = generar_numero_catalogo(key)
        if model.query.filter(model.codigo == candidate).first() is None:
            return candidate


@catalogo_bp.errorhandler(ColorRecipeError)
def _handle_color_recipe_error(error):
    payload = {'error': error.message, 'codigo': error.code}
    if error.details:
        payload['details'] = error.details
    return jsonify(payload), error.status


def _manual_identifier_error(field):
    """Los identificadores normales los asigna el backend, no el cliente."""
    return jsonify({
        'error': f'{field} es automático y no admite asignación manual',
        'codigo': 'CODIGO_MANUAL_NO_PERMITIDO',
        'campo': field,
    }), 400


def _immutable_identifier_error(field, current_value):
    """Respuesta uniforme cuando se intenta cambiar una identidad persistida."""
    return jsonify({
        'error': f'{field} es inmutable',
        'codigo': 'CODIGO_INMUTABLE',
        'campo': field,
        'valor_actual': current_value,
    }), 400


def _classification_error_response(exc):
    return jsonify({
        'error': str(exc),
        'codigo': exc.code,
    }), exc.status


def _normalize_product_numeric_fields(data):
    """Normaliza referencias numéricas y devuelve un error HTTP explicativo."""
    specs = {
        'peso_g': {'integer': False, 'minimum': 0},
        'precio_estimado': {'integer': False, 'minimum': 0},
        'precio_sin_igv': {'integer': False, 'minimum': 0},
        'doc_x_paq': {'integer': True, 'minimum': 1},
        'doc_x_bulto': {'integer': True, 'minimum': 1},
    }
    for field, spec in specs.items():
        if field not in data:
            continue
        raw_value = data[field]
        if raw_value in ('', None):
            data[field] = None
            continue
        try:
            if isinstance(raw_value, bool):
                raise ValueError
            value = float(raw_value)
        except (TypeError, ValueError):
            return jsonify({
                'error': f'{field} debe ser numérico',
                'codigo': 'VALOR_INVALIDO',
                'campo': field,
            }), 400
        if (
            not math.isfinite(value)
            or value < spec['minimum']
            or (spec['integer'] and not value.is_integer())
        ):
            qualifier = (
                f'un entero mayor o igual a {spec["minimum"]}'
                if spec['integer']
                else f'un número mayor o igual a {spec["minimum"]}'
            )
            return jsonify({
                'error': f'{field} debe ser {qualifier}',
                'codigo': 'VALOR_INVALIDO',
                'campo': field,
            }), 400
        data[field] = int(value) if spec['integer'] else value
    return None


def _manual_new_piece_identifier_field(payload):
    """Devuelve el identificador manual pedido para una Pieza anidada nueva."""
    if payload.get('pieza_id') is not None:
        return None
    if str(payload.get('codigo') or '').strip():
        return 'codigo'
    if str(payload.get('sku_override') or '').strip():
        return 'sku_override'
    return None

@catalogo_bp.route('/productos', methods=['GET'])
def listar_productos():
    """
    Lista Productos Terminados con búsqueda opcional.
    Query params:
        - q: término de búsqueda (busca en múltiples campos)
        - limit: máximo de resultados (default 50)
    """
    q = request.args.get('q', '').strip()
    limit = request.args.get('limit', 50, type=int)
    
    query = ProductoTerminado.query
    
    if q:
        search = f"%{q}%"
        query = query.filter(
            or_(
                ProductoTerminado.producto.ilike(search),
                ProductoTerminado.cod_sku_pt.ilike(search),
                ProductoTerminado.nombre_gs1.ilike(search),
                ProductoTerminado.marca.ilike(search),
                ProductoTerminado.codigo_barra.ilike(search),
                Linea.nombre.ilike(search),
                Familia.nombre.ilike(search),
            )
        ).outerjoin(Linea, ProductoTerminado.linea_id == Linea.id).outerjoin(
            Familia,
            ProductoTerminado.familia_id == Familia.id,
        )
    
    productos = query.limit(limit).all()
    
    return jsonify([{
        'cod_sku_pt': p.cod_sku_pt,
        'producto': p.producto,
        # Usar datos normalizados de Linea (campos legacy eliminados)
        'linea': p.linea_rel.nombre if p.linea_rel else None,
        'cod_linea': p.linea_rel.codigo if p.linea_rel else None,
        'linea_id': p.linea_id,
        # Usar datos normalizados de Familia (campos legacy eliminados)
        'familia': p.familia_rel.nombre if p.familia_rel else None,
        'cod_familia': p.familia_rel.codigo if p.familia_rel else None,
        'familia_id': p.familia_id,
        'um': p.um,
        'doc_x_paq': p.doc_x_paq,
        'doc_x_bulto': p.doc_x_bulto,
        'peso_g': p.peso_g,
        'imagen_url': f'/api/productos/{p.cod_sku_pt}/imagen' if has_catalog_image(p) else None,
        'precio_estimado': p.precio_estimado,
        'precio_sin_igv': p.precio_sin_igv,
        'indicador_x_kg': p.indicador_x_kg,
        'status': p.status,
        'codigo_barra': p.codigo_barra,
        'marca': p.marca,
        'nombre_gs1': p.nombre_gs1,
        'obs': p.obs,
        'estado_revision': p.estado_revision,
        'fecha_importacion': p.fecha_importacion.isoformat() if p.fecha_importacion else None,
        'fecha_revision': p.fecha_revision.isoformat() if p.fecha_revision else None,
        'notas_revision': p.notas_revision,
        'num_piezas': len(p.composicion_piezas)
    } for p in productos])


@catalogo_bp.route('/piezas', methods=['GET'])
def listar_piezas_abstractas():
    """
    Lista Piezas (formas abstractas sin color) con búsqueda opcional.
    """
    from app.models.molde import Molde, MoldePieza, Pieza
    q = request.args.get('q', '').strip()
    limit = request.args.get('limit', 50, type=int)
    
    query = Pieza.query
    
    if q:
        search = f"%{q}%"
        query = (
            query
            .outerjoin(MoldePieza)
            .outerjoin(Molde)
            .outerjoin(PiezaColor, PiezaColor.pieza_id == Pieza.id)
            .outerjoin(
                ColorProduccion,
                ColorProduccion.id == PiezaColor.color_produccion_id,
            )
            .outerjoin(ColorBase, ColorBase.id == ColorProduccion.color_base_id)
            .filter(
            or_(
                Pieza.nombre.ilike(search),
                Pieza.codigo.ilike(search),
                Molde.codigo.ilike(search),
                Molde.nombre.ilike(search),
                PiezaColor.sku.ilike(search),
                PiezaColor.piezas.ilike(search),
                ColorBase.nombre.ilike(search),
            )
            )
            .distinct()
        )
    
    piezas = query.order_by(Pieza.nombre, Pieza.codigo).limit(limit).all()
    
    return jsonify([
        p.to_dict(include_variantes=True, include_moldes=True)
        for p in piezas
    ])


@catalogo_bp.route('/piezas', methods=['POST'])
def crear_pieza_maestra():
    """Crea una Pieza global; las cavidades se configuran al asociarla a un molde."""
    data = request.get_json() or {}
    if str(data.get('codigo') or '').strip():
        return _manual_identifier_error('codigo')
    nombre = str(data.get('nombre') or '').strip()
    try:
        peso_nominal = float(data.get('peso_nominal_gr'))
    except (TypeError, ValueError):
        return jsonify({'error': 'peso_nominal_gr debe ser numérico'}), 400
    if not nombre or peso_nominal <= 0:
        return jsonify({
            'error': 'nombre y peso_nominal_gr positivo son obligatorios'
        }), 400
    try:
        linea, familia, _ = validate_linea_familia(
            linea_id=data.get('linea_id'),
            familia_id=data.get('familia_id'),
            allow_unclassified=True,
        )
    except ClassificationError as exc:
        return _classification_error_response(exc)

    try:
        codigo = generar_codigo_catalogo('PIEZA')
        pieza = Pieza(
            codigo=codigo,
            nombre=nombre,
            linea_id=linea.id if linea else None,
            familia_id=familia.id if familia else None,
            peso_nominal_gr=peso_nominal,
            activo=bool(data.get('activo', True)),
        )
        db.session.add(pieza)
        db.session.commit()
        return jsonify(pieza.to_dict(include_moldes=True)), 201
    except Exception as exc:
        db.session.rollback()
        return jsonify({'error': str(exc)}), 400


@catalogo_bp.route('/piezas/<int:pieza_id>', methods=['GET'])
def obtener_pieza_maestra(pieza_id):
    pieza = db.session.get(Pieza, pieza_id)
    if not pieza:
        return jsonify({'error': 'Pieza no encontrada'}), 404
    return jsonify(pieza.to_dict(include_variantes=True, include_moldes=True))


@catalogo_bp.route('/piezas/<int:pieza_id>', methods=['PUT'])
def actualizar_pieza_maestra(pieza_id):
    pieza = db.session.get(Pieza, pieza_id)
    if not pieza:
        return jsonify({'error': 'Pieza no encontrada'}), 404
    data = request.get_json() or {}
    if data.get('version') is None:
        return jsonify({
            'error': 'version es obligatoria para actualizar la pieza',
            'codigo': 'VERSION_REQUIRED',
        }), 400
    try:
        expected_version = int(data['version'])
    except (TypeError, ValueError):
        return jsonify({'error': 'version debe ser un entero positivo'}), 400
    if expected_version <= 0:
        return jsonify({'error': 'version debe ser un entero positivo'}), 400
    if expected_version != pieza.version:
        return jsonify({
            'error': 'La pieza cambió desde que fue cargada',
            'codigo': 'VERSION_CONFLICT',
            'actual': pieza.to_dict(include_moldes=True),
        }), 409

    codigo_solicitado = str(data.get('codigo') or '').strip()
    if codigo_solicitado and codigo_solicitado != pieza.codigo:
        return _immutable_identifier_error('codigo', pieza.codigo)
    nombre = str(data.get('nombre', pieza.nombre)).strip()
    try:
        peso_nominal = float(data.get('peso_nominal_gr', pieza.peso_nominal_gr))
    except (TypeError, ValueError):
        return jsonify({'error': 'peso_nominal_gr debe ser numérico'}), 400
    if not nombre or peso_nominal <= 0:
        return jsonify({'error': 'Nombre y peso nominal positivo son obligatorios'}), 400
    try:
        linea, familia, _ = validate_linea_familia(
            linea_id=data.get('linea_id', pieza.linea_id),
            familia_id=data.get('familia_id', pieza.familia_id),
            allow_unclassified=True,
        )
    except ClassificationError as exc:
        return _classification_error_response(exc)

    nuevo_activo = bool(data.get('activo', pieza.activo))
    if not nuevo_activo and any(item.activo for item in pieza.molde_piezas):
        return jsonify({
            'error': 'Desvincule la pieza de todos los moldes antes de inactivarla'
        }), 409
    pieza.nombre = nombre
    pieza.peso_nominal_gr = peso_nominal
    pieza.linea_id = linea.id if linea else None
    pieza.familia_id = familia.id if familia else None
    for variante in pieza.variantes:
        variante.linea_id = pieza.linea_id
        variante.familia_id = pieza.familia_id
    pieza.activo = nuevo_activo
    pieza.version += 1
    try:
        db.session.commit()
        return jsonify(pieza.to_dict(include_moldes=True)), 200
    except Exception as exc:
        db.session.rollback()
        return jsonify({'error': str(exc)}), 400


@catalogo_bp.route('/piezas/<int:pieza_id>', methods=['DELETE'])
def inactivar_pieza_maestra(pieza_id):
    pieza = db.session.get(Pieza, pieza_id)
    if not pieza:
        return jsonify({'error': 'Pieza no encontrada'}), 404
    if any(item.activo for item in pieza.molde_piezas):
        return jsonify({
            'error': 'Desvincule la pieza de todos los moldes antes de inactivarla'
        }), 409
    pieza.activo = False
    pieza.version += 1
    db.session.commit()
    return jsonify(pieza.to_dict(include_moldes=True)), 200


@catalogo_bp.route('/piezas-color', methods=['GET'])
def listar_piezas_color():
    """
    Lista Piezas Físicas (SKUs con color) con búsqueda opcional.
    Query params:
        - q: término de búsqueda (busca en múltiples campos)
        - producto_id: filtrar por producto terminado (SKU PT)
        - limit: máximo de resultados (default 50)
    """
    q = request.args.get('q', '').strip()
    producto_id = request.args.get('producto_id', '').strip()
    limit = request.args.get('limit', 50, type=int)
    
    query = PiezaColor.query
    
    # Filtrar por producto via tabla intermedia
    if producto_id:
        query = query.join(ProductoPieza).filter(ProductoPieza.producto_terminado_id == producto_id)
    
    if q:
        search = f"%{q}%"
        query = query.filter(
            or_(
                PiezaColor.sku.ilike(search),
                PiezaColor.piezas.ilike(search),
                PiezaColor.mp.ilike(search),
                PiezaColor.tipo_extruccion.ilike(search)
            )
        )
    
    piezas = query.limit(limit).all()
    
    return jsonify([{
        'sku': p.sku,
        'piezas': p.piezas,
        'linea': p.linea_rel.nombre if p.linea_rel else None,
        'cod_linea': p.linea_rel.codigo if p.linea_rel else None,
        'linea_id': p.linea_id,
        'familia': p.familia_rel.nombre if p.familia_rel else None,
        'cod_familia': p.familia_rel.codigo if p.familia_rel else None,
        'familia_id': p.familia_id,
        'cod_pieza': p.cod_pieza,
        'color': p.color_produccion_rel.nombre if p.color_produccion_rel else None,
        'color_hex': p.color_produccion_rel.hex_referencia if p.color_produccion_rel else None,
        'color_produccion_id': p.color_produccion_id,
        'pieza_id': p.pieza_id,
        'imagen_url': f'/api/piezas-color/{p.sku}/imagen' if has_catalog_image(p) else None,
        'cavidad': p.cavidad,
        'peso': p.peso,
        'tipo_extruccion': p.tipo_extruccion,
        'cod_extru': p.cod_extru,
        'mp': p.mp,
        'cod_mp': p.cod_mp,
        'estado_revision': p.estado_revision,
        'fecha_importacion': p.fecha_importacion.isoformat() if p.fecha_importacion else None,
        'fecha_revision': p.fecha_revision.isoformat() if p.fecha_revision else None,
        'notas_revision': p.notas_revision,
        'num_productos': len(p.en_productos),
        'productos': [ep.producto_terminado.producto for ep in p.en_productos[:5]]
    } for p in piezas])


@catalogo_bp.route('/productos/<cod_sku_pt>', methods=['GET'])
def obtener_producto(cod_sku_pt):
    """
    Obtiene la identidad maestra y referencias logísticas de un producto.

    La composición canónica se consulta en Ingeniería SCM mediante su
    estructura/BOM revisionada; ``producto_pieza`` solo permanece como
    compatibilidad de lectura durante la migración.
    """
    producto = db.session.get(ProductoTerminado, cod_sku_pt)
    if not producto:
        return jsonify({'error': 'Producto no encontrado'}), 404
    
    return jsonify({
        'cod_sku_pt': producto.cod_sku_pt,
        'producto': producto.producto,
        'familia': producto.familia_rel.nombre if producto.familia_rel else None,
        'familia_id': producto.familia_id,
        'linea': producto.linea_rel.nombre if producto.linea_rel else None,
        'linea_id': producto.linea_id,
        'doc_x_paq': producto.doc_x_paq,
        'doc_x_bulto': producto.doc_x_bulto,
        'peso_g': producto.peso_g,
        'imagen_url': f'/api/productos/{producto.cod_sku_pt}/imagen' if has_catalog_image(producto) else None,
        'precio_estimado': producto.precio_estimado,
        'precio_sin_igv': producto.precio_sin_igv,
        'status': producto.status,
        'codigo_barra': producto.codigo_barra,
        'marca': producto.marca,
        'um': producto.um,
        'piezas': [{
            'sku': cp.pieza.sku,
            'nombre': cp.pieza.piezas,
            'color': cp.pieza.color_produccion_rel.nombre if cp.pieza.color_produccion_rel else None,
            'peso': cp.pieza.peso,
            'cantidad': cp.cantidad
        } for cp in producto.composicion_piezas]
    })


@catalogo_bp.route('/productos', methods=['POST'])
def crear_producto():
    """Crea la identidad maestra de un ProductoTerminado."""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Payload JSON requerido'}), 400

    if str(data.get('cod_sku_pt') or '').strip():
        return _manual_identifier_error('cod_sku_pt')
    if not str(data.get('producto') or '').strip():
        return jsonify({'error': 'producto es obligatorio'}), 400
    numeric_error = _normalize_product_numeric_fields(data)
    if numeric_error:
        return numeric_error
    try:
        linea, familia, _ = validate_linea_familia(
            linea_id=data.get('linea_id'),
            familia_id=data.get('familia_id'),
        )
    except ClassificationError as exc:
        return _classification_error_response(exc)
    
    try:
        producto = ProductoTerminado(
            cod_sku_pt=generar_codigo_catalogo('PRODUCTO_TERMINADO'),
            producto=str(data['producto']).strip(),
            linea_id=linea.id,
            familia_id=familia.id,
            peso_g=data.get('peso_g'),
            precio_estimado=data.get('precio_estimado'),
            precio_sin_igv=data.get('precio_sin_igv'),
            doc_x_paq=data.get('doc_x_paq'),
            doc_x_bulto=data.get('doc_x_bulto'),
            status=data.get('status', 'Activo'),
            codigo_barra=data.get('codigo_barra'),
            marca=data.get('marca'),
            um=data.get('um', 'Unidad')
        )
        db.session.add(producto)
        db.session.flush()
        db.session.add(ScmPresentacionComercial(
            codigo=generar_codigo_catalogo('PRESENTACION_COMERCIAL'),
            producto_terminado_id=producto.cod_sku_pt,
            nombre='Unidad',
            unidades_base=1,
            codigo_barra=data.get('codigo_barra'),
            predeterminada=True,
        ))

        # Adaptador transitorio para consumidores legacy. El frontend nuevo no
        # envía esta colección; las BOM nuevas viven en Ingeniería SCM.
        for pieza_data in data.get('piezas', []):
            db.session.add(ProductoPieza(
                producto_terminado_id=producto.cod_sku_pt,
                pieza_sku=pieza_data['pieza_sku'],
                cantidad=pieza_data.get('cantidad', 1),
            ))

        db.session.commit()
        return jsonify({'cod_sku_pt': producto.cod_sku_pt, 'producto': producto.producto}), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400


@catalogo_bp.route('/productos/<cod_sku_pt>', methods=['PUT'])
def actualizar_producto(cod_sku_pt):
    """Actualiza la identidad maestra y referencias de un ProductoTerminado."""
    producto = db.session.get(ProductoTerminado, cod_sku_pt)
    if not producto:
        return jsonify({'error': 'Producto no encontrado'}), 404
    
    data = request.get_json() or {}
    numeric_error = _normalize_product_numeric_fields(data)
    if numeric_error:
        return numeric_error
    codigo_solicitado = str(data.get('cod_sku_pt') or '').strip()
    if codigo_solicitado and codigo_solicitado != producto.cod_sku_pt:
        return _immutable_identifier_error('cod_sku_pt', producto.cod_sku_pt)
    nombre = str(data.get('producto', producto.producto) or '').strip()
    if not nombre:
        return jsonify({'error': 'producto es obligatorio'}), 400
    try:
        linea, familia, _ = validate_linea_familia(
            linea_id=data.get('linea_id', producto.linea_id),
            familia_id=data.get('familia_id', producto.familia_id),
        )
    except ClassificationError as exc:
        return _classification_error_response(exc)
    
    producto.producto = nombre
    producto.linea_id = linea.id
    producto.familia_id = familia.id
    producto.peso_g = data.get('peso_g', producto.peso_g)
    producto.precio_estimado = data.get('precio_estimado', producto.precio_estimado)
    producto.precio_sin_igv = data.get('precio_sin_igv', producto.precio_sin_igv)
    producto.doc_x_paq = data.get('doc_x_paq', producto.doc_x_paq)
    producto.doc_x_bulto = data.get('doc_x_bulto', producto.doc_x_bulto)
    producto.status = data.get('status', producto.status)
    producto.codigo_barra = data.get('codigo_barra', producto.codigo_barra)
    producto.marca = data.get('marca', producto.marca)
    producto.um = data.get('um', producto.um)

    # Compatibilidad de escritura hasta que /api/ordenes deje de depender de
    # producto_pieza. No se expone en el CRUD normalizado del maestro.
    if 'piezas' in data:
        ProductoPieza.query.filter_by(
            producto_terminado_id=cod_sku_pt,
        ).delete()
        for pieza_data in data['piezas']:
            db.session.add(ProductoPieza(
                producto_terminado_id=cod_sku_pt,
                pieza_sku=pieza_data['pieza_sku'],
                cantidad=pieza_data.get('cantidad', 1),
            ))

    db.session.commit()
    return jsonify({'cod_sku_pt': producto.cod_sku_pt, 'producto': producto.producto}), 200


@catalogo_bp.route('/productos/<cod_sku_pt>', methods=['DELETE'])
def eliminar_producto(cod_sku_pt):
    """Bloquea el borrado físico; los maestros se desactivan por PUT."""
    producto = db.session.get(ProductoTerminado, cod_sku_pt)
    if not producto:
        return jsonify({'error': 'Producto no encontrado'}), 404
    return jsonify({
        'error': 'La eliminación directa está bloqueada. Desactiva el producto.',
        'codigo': 'ELIMINACION_DIRECTA_BLOQUEADA',
    }), 409


@catalogo_bp.route('/maquinas', methods=['GET'])
def listar_maquinas():
    """
    Lista todas las máquinas disponibles.
    """
    from app.models.maquina import Maquina
    maquinas = Maquina.query.all()
    return jsonify([m.to_dict() for m in maquinas])


# ============================================================
# MOLDES CRUD
# ============================================================
from app.models.molde import Molde, MoldePieza, Pieza


def _ensure_default_line_family():
    """Resuelve los catálogos obligatorios para variantes legacy/autogeneradas."""
    linea = Linea.query.filter_by(nombre='GENERAL').first()
    if not linea:
        linea = Linea(
            codigo=_next_available_numeric_code(Linea, 'LINEA'),
            nombre='GENERAL',
        )
        db.session.add(linea)
        db.session.flush()
    elif not linea.activo:
        linea.activo = True
        linea.version += 1

    familia = Familia.query.filter_by(nombre='COMPONENTES').first()
    if not familia:
        familia = Familia(
            codigo=_next_available_numeric_code(Familia, 'FAMILIA'),
            nombre='COMPONENTES',
        )
        db.session.add(familia)
        db.session.flush()
    elif not familia.activo:
        familia.activo = True
        familia.version += 1
    ensure_linea_familia(
        linea_id=linea.id,
        familia_id=familia.id,
    )
    return linea, familia


def _pieza_color_to_dict(pieza):
    """Serializa el SKU coloreado sin usar su cavidad legacy como configuración."""
    relaciones = []
    if pieza.pieza_rel:
        relaciones = [
            item.to_summary_dict()
            for item in pieza.pieza_rel.molde_piezas
            if item.activo
        ]
    return {
        'sku': pieza.sku,
        'nombre': pieza.piezas,
        'piezas': pieza.piezas,
        'peso': pieza.peso,
        'cavidad': pieza.cavidad,
        'cavidad_legacy': pieza.cavidad,
        'linea_id': pieza.linea_id,
        'familia_id': pieza.familia_id,
        'color_produccion_id': pieza.color_produccion_id,
        'color': pieza.color_produccion_rel.nombre if pieza.color_produccion_rel else None,
        'color_hex': pieza.color_produccion_rel.hex_referencia if pieza.color_produccion_rel else None,
        'pieza_id': pieza.pieza_id,
        'pieza_codigo': pieza.pieza_rel.codigo if pieza.pieza_rel else None,
        'imagen_url': f'/api/piezas-color/{pieza.sku}/imagen' if has_catalog_image(pieza) else None,
        'estado_revision': pieza.estado_revision,
        'moldes': relaciones,
    }


def _resolve_pieza_color_classification(data, current=None):
    """Impide que una variante coloreada se separe de su maestro ``Pieza``."""
    if 'pieza_id' in data:
        requested_piece_id = data.get('pieza_id')
    else:
        requested_piece_id = current.pieza_id if current else None
    if requested_piece_id == '':
        requested_piece_id = None

    if requested_piece_id is not None:
        try:
            requested_piece_id = int(requested_piece_id)
        except (TypeError, ValueError):
            raise ClassificationError(
                'pieza_id debe ser un entero positivo',
                'PIEZA_INVALIDA',
            )
        if requested_piece_id <= 0:
            raise ClassificationError(
                'pieza_id debe ser un entero positivo',
                'PIEZA_INVALIDA',
            )
        pieza_maestra = db.session.get(Pieza, requested_piece_id)
        if not pieza_maestra or not pieza_maestra.activo:
            raise ClassificationError(
                'La pieza maestra no existe o estÃ¡ inactiva',
                'PIEZA_NO_ENCONTRADA',
                404,
            )
        supplied_linea = data.get('linea_id')
        supplied_familia = data.get('familia_id')
        if pieza_maestra.linea_id is None or pieza_maestra.familia_id is None:
            raise ClassificationError(
                'La pieza maestra debe tener Línea y Familia antes de crear variantes',
                'PIEZA_SIN_CLASIFICACION',
                409,
            )
        else:
            linea, familia, _ = validate_linea_familia(
                linea_id=pieza_maestra.linea_id,
                familia_id=pieza_maestra.familia_id,
            )

        if supplied_linea not in (None, '') or supplied_familia not in (None, ''):
            try:
                matches_master = (
                    int(supplied_linea) == linea.id
                    and int(supplied_familia) == familia.id
                )
            except (TypeError, ValueError):
                matches_master = False
            if not matches_master:
                raise ClassificationError(
                    'La clasificaciÃ³n de la variante debe ser la de su pieza maestra',
                    'CLASIFICACION_PIEZA_DIVERGENTE',
                    409,
                )
        return pieza_maestra, linea, familia

    linea, familia, _ = validate_linea_familia(
        linea_id=data.get('linea_id', current.linea_id if current else None),
        familia_id=data.get('familia_id', current.familia_id if current else None),
    )
    return None, linea, familia


def _piece_and_composition_from_payload(molde, data):
    """Resuelve/crea Pieza global y devuelve una composición aún no persistida."""
    pieza_id = data.get('pieza_id')
    if pieza_id is not None:
        try:
            pieza_id = int(pieza_id)
        except (TypeError, ValueError):
            raise ValueError('pieza_id debe ser un entero')
        pieza = db.session.get(Pieza, pieza_id)
        if not pieza or not pieza.activo:
            raise ValueError('La pieza global no existe o está inactiva')
    else:
        nombre = str(data.get('nombre') or '').strip()
        peso_nominal = data.get('peso_nominal_gr', data.get('peso_unitario_gr'))
        if str(data.get('codigo') or '').strip():
            raise ValueError('codigo es automático y no admite asignación manual')
        if str(data.get('sku_override') or '').strip():
            raise ValueError('sku_override ya no admite asignación manual')
        if not nombre or peso_nominal is None:
            raise ValueError('nombre y peso_nominal_gr son obligatorios al crear una pieza')
        linea, familia, _ = validate_linea_familia(
            linea_id=data.get('linea_id'),
            familia_id=data.get('familia_id'),
            allow_unclassified=True,
        )
        pieza = Pieza(
            codigo=generar_codigo_catalogo('PIEZA'),
            nombre=nombre,
            linea_id=linea.id if linea else None,
            familia_id=familia.id if familia else None,
            peso_nominal_gr=float(peso_nominal),
            activo=True,
        )
        if pieza.peso_nominal_gr <= 0:
            raise ValueError('peso_nominal_gr debe ser positivo')
        db.session.add(pieza)
        db.session.flush()

    try:
        cavidades = int(data['cavidades'])
        peso_operativo = float(
            data.get('peso_unitario_gr', pieza.peso_nominal_gr)
        )
    except (KeyError, TypeError, ValueError):
        raise ValueError('cavidades y peso_unitario_gr deben ser numéricos')
    if cavidades <= 0 or peso_operativo <= 0:
        raise ValueError('cavidades y peso_unitario_gr deben ser positivos')

    existing = MoldePieza.query.filter_by(
        molde_id=molde.codigo,
        pieza_id=pieza.id,
    ).first()
    if existing:
        if existing.activo:
            raise ValueError('La pieza ya está asociada a este molde')
        existing.activo = True
        existing.cavidades = cavidades
        existing.peso_unitario_gr = peso_operativo
        existing.version += 1
        return pieza, existing

    return pieza, MoldePieza(
        molde=molde,
        pieza=pieza,
        cavidades=cavidades,
        peso_unitario_gr=peso_operativo,
        activo=True,
    )

@catalogo_bp.route('/moldes/exportar', methods=['GET'])
def exportar_moldes():
    """Exporta todos los moldes con sus piezas para sincronización offline"""
    moldes = Molde.query.filter_by(activo=True).all()
    
    result = []
    for m in moldes:
        piezas = []
        for mp in m.piezas:
            if not mp.activo:
                continue
            variantes = sorted(mp.variantes, key=lambda item: item.sku)
            variante = variantes[0] if variantes else None
            piezas.append({
                'pieza_id': mp.pieza_id,
                'pieza_codigo': mp.pieza.codigo,
                'sku': variante.sku if variante else None,
                'nombre': mp.nombre,
                'cavidades': mp.cavidades,
                'peso_unitario_gr': mp.peso_unitario_gr
            })
        
        result.append({
            'codigo': m.codigo,
            'nombre': m.nombre,
            'peso_tiro_gr': m.peso_tiro_gr,
            'tiempo_ciclo_std': m.tiempo_ciclo_std,
            'piezas': piezas
        })
    
    return jsonify(result), 200


@catalogo_bp.route('/moldes', methods=['GET'])
def obtener_moldes():
    """Obtiene todos los moldes"""
    moldes = Molde.query.order_by(Molde.nombre).all()
    return jsonify([m.to_dict() for m in moldes]), 200


@catalogo_bp.route('/moldes/<codigo>', methods=['GET'])
def obtener_molde(codigo):
    """Obtiene un molde específico"""
    molde = db.session.get(Molde, codigo)
    if not molde:
        return jsonify({'error': 'Molde no encontrado'}), 404
    return jsonify(molde.to_dict(include_variantes=True)), 200


@catalogo_bp.route('/moldes', methods=['POST'])
def crear_molde():
    """Crea un nuevo molde"""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Payload JSON requerido'}), 400
    if str(data.get('codigo') or '').strip():
        return _manual_identifier_error('codigo')
    if not str(data.get('nombre') or '').strip():
        return jsonify({'error': 'nombre es obligatorio'}), 400
    for pieza_data in data.get('piezas', []):
        manual_field = _manual_new_piece_identifier_field(pieza_data)
        if manual_field:
            return _manual_identifier_error(f'piezas.{manual_field}')
    
    try:
        molde = Molde(
            codigo=generar_codigo_catalogo('MOLDE'),
            nombre=str(data['nombre']).strip(),
            peso_tiro_gr=data['peso_tiro_gr'],
            tiempo_ciclo_std=data.get('tiempo_ciclo_std', 30.0),
            activo=data.get('activo', True),
            notas=data.get('notas')
        )
        db.session.add(molde)
        
        # Agregar piezas si se proveen
        if 'piezas' in data and len(data['piezas']) > 0:
            for pieza_data in data.get('piezas', []):
                _, mp = _piece_and_composition_from_payload(molde, pieza_data)
                db.session.add(mp)
        
        # --- SIMPLE MODE: Crear pieza automática si no se especificaron piezas ---
        elif data.get('cavidades') and data.get('peso_unitario_gr'):
            linea_default, familia_default = _ensure_default_line_family()
            pieza_global = Pieza(
                codigo=generar_codigo_catalogo('PIEZA'),
                nombre=f"{molde.nombre} (Std)",
                linea_id=linea_default.id,
                familia_id=familia_default.id,
                peso_nominal_gr=float(data.get('peso_unitario_gr')),
                activo=True,
            )
            db.session.add(pieza_global)
            db.session.flush()
            pieza_color = PiezaColor(
                sku=generar_codigo_catalogo('PIEZA_COLOR'),
                piezas=f"{molde.nombre} (Std)",
                linea_id=linea_default.id,
                familia_id=familia_default.id,
                peso=float(data.get('peso_unitario_gr')),
                cavidad=None,
                pieza_id=pieza_global.id,
            )
            db.session.add(pieza_color)
            
            # Crear relación Molde-PiezaColor
            mp = MoldePieza(
                molde=molde,
                pieza=pieza_global,
                cavidades=int(data.get('cavidades')),
                peso_unitario_gr=float(data.get('peso_unitario_gr')),
                activo=True,
            )
            db.session.add(mp)
        
        db.session.commit()
        return jsonify(molde.to_dict()), 201
    except ClassificationError as exc:
        db.session.rollback()
        return _classification_error_response(exc)
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400


@catalogo_bp.route('/productos/<cod_sku_pt>/imagen', methods=['GET', 'PUT', 'DELETE'])
def imagen_producto(cod_sku_pt):
    producto = db.session.get(ProductoTerminado, cod_sku_pt)
    if not producto:
        return jsonify({'error': 'Producto no encontrado'}), 404
    if request.method == 'GET':
        return _image_response(producto)
    if request.method == 'DELETE':
        try:
            get_catalog_image_storage().delete(producto)
            db.session.commit()
        except CatalogImageStorageError as exc:
            db.session.rollback()
            return jsonify({
                'error': str(exc),
                'codigo': 'IMAGEN_STORAGE_NO_DISPONIBLE',
            }), 503
        return jsonify({'cod_sku_pt': producto.cod_sku_pt, 'imagen_url': None})
    parsed, error = _read_catalog_image()
    if error:
        return error
    try:
        mime_type, content = parsed
        get_catalog_image_storage().store(
            producto,
            category='producto-terminado',
            identity=producto.cod_sku_pt,
            mime_type=mime_type,
            content=content,
        )
        db.session.commit()
    except CatalogImageStorageError as exc:
        db.session.rollback()
        return jsonify({
            'error': str(exc),
            'codigo': 'IMAGEN_STORAGE_NO_DISPONIBLE',
        }), 503
    return jsonify({
        'cod_sku_pt': producto.cod_sku_pt,
        'imagen_url': f'/api/productos/{producto.cod_sku_pt}/imagen',
    })


@catalogo_bp.route('/moldes/<codigo>', methods=['PUT'])
def actualizar_molde(codigo):
    """Actualiza un molde existente"""
    molde = db.session.get(Molde, codigo)
    if not molde:
        return jsonify({'error': 'Molde no encontrado'}), 404
    
    data = request.get_json() or {}
    codigo_solicitado = str(data.get('codigo') or '').strip()
    if codigo_solicitado and codigo_solicitado != molde.codigo:
        return _immutable_identifier_error('codigo', molde.codigo)
    for pieza_data in data.get('piezas', []):
        manual_field = _manual_new_piece_identifier_field(pieza_data)
        if manual_field:
            return _manual_identifier_error(f'piezas.{manual_field}')
    
    try:
        molde.nombre = data.get('nombre', molde.nombre)
        molde.peso_tiro_gr = data.get('peso_tiro_gr', molde.peso_tiro_gr)
        molde.tiempo_ciclo_std = data.get('tiempo_ciclo_std', molde.tiempo_ciclo_std)
        molde.activo = data.get('activo', molde.activo)
        molde.notas = data.get('notas', molde.notas)

        # Actualizar piezas si se proveen explicitamente.
        if 'piezas' in data:
            for relacion in molde.piezas:
                relacion.activo = False
                relacion.version += 1
            for pieza_data in data['piezas']:
                _, mp = _piece_and_composition_from_payload(molde, pieza_data)
                db.session.add(mp)

        # --- SIMPLE MODE: Actualizar primera pieza existente ---
        elif 'cavidades' in data and 'peso_unitario_gr' in data:
            piezas_molde = [item for item in molde.piezas if item.activo]

            if piezas_molde:
                mp = piezas_molde[0]
                mp.cavidades = int(data['cavidades'])
                mp.peso_unitario_gr = float(data['peso_unitario_gr'])
                mp.version += 1
            else:
                linea_default, familia_default = _ensure_default_line_family()
                pieza_global = Pieza(
                    codigo=generar_codigo_catalogo('PIEZA'),
                    nombre=f"{molde.nombre} (Std)",
                    linea_id=linea_default.id,
                    familia_id=familia_default.id,
                    peso_nominal_gr=float(data['peso_unitario_gr']),
                    activo=True,
                )
                db.session.add(pieza_global)
                db.session.flush()
                pieza_color = PiezaColor(
                    sku=generar_codigo_catalogo('PIEZA_COLOR'),
                    piezas=f"{molde.nombre} (Std)",
                    peso=float(data['peso_unitario_gr']),
                    cavidad=None,
                    linea_id=linea_default.id,
                    familia_id=familia_default.id,
                    pieza_id=pieza_global.id,
                )
                db.session.add(pieza_color)

                mp = MoldePieza(
                    molde=molde,
                    pieza=pieza_global,
                    cavidades=int(data['cavidades']),
                    peso_unitario_gr=float(data['peso_unitario_gr']),
                    activo=True,
                )
                db.session.add(mp)

        db.session.commit()
        return jsonify(molde.to_dict()), 200
    except ClassificationError as exc:
        db.session.rollback()
        return _classification_error_response(exc)
    except Exception as exc:
        db.session.rollback()
        return jsonify({'error': str(exc)}), 400


@catalogo_bp.route('/moldes/<codigo>', methods=['DELETE'])
def eliminar_molde(codigo):
    """Elimina un molde"""
    molde = db.session.get(Molde, codigo)
    if not molde:
        return jsonify({'error': 'Molde no encontrado'}), 404
    
    db.session.delete(molde)
    db.session.commit()
    return jsonify({'message': f'Molde {codigo} eliminado'}), 200


@catalogo_bp.route('/moldes/<codigo>/formas', methods=['POST'])
def crear_forma_molde(codigo):
    """Asocia una Pieza global al molde y configura su composición física."""
    molde = db.session.get(Molde, codigo)
    if not molde:
        return jsonify({'error': 'Molde no encontrado'}), 404

    data = request.get_json() or {}
    manual_field = _manual_new_piece_identifier_field(data)
    if manual_field:
        return _manual_identifier_error(manual_field)
    try:
        _, composicion = _piece_and_composition_from_payload(molde, data)
        db.session.add(composicion)
        db.session.commit()
        return jsonify(composicion.to_dict(include_variantes=True)), 201
    except ClassificationError as exc:
        db.session.rollback()
        return _classification_error_response(exc)
    except ValueError as exc:
        db.session.rollback()
        status = 409 if 'ya está asociada' in str(exc) else 400
        return jsonify({'error': str(exc)}), status
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400


@catalogo_bp.route('/formas/<int:forma_id>', methods=['PUT'])
def actualizar_forma_molde(forma_id):
    """Actualiza cavidades/peso de una asociación, nunca el maestro Pieza."""
    composicion = db.session.get(MoldePieza, forma_id)
    if not composicion or not composicion.activo:
        return jsonify({'error': 'Composición de molde no encontrada'}), 404
    data = request.get_json() or {}
    if data.get('version') is None:
        return jsonify({
            'error': 'version es obligatoria para actualizar la composición',
            'codigo': 'VERSION_REQUIRED',
        }), 400
    try:
        expected_version = int(data['version'])
    except (TypeError, ValueError):
        return jsonify({'error': 'version debe ser un entero positivo'}), 400
    if expected_version <= 0:
        return jsonify({'error': 'version debe ser un entero positivo'}), 400
    if expected_version != composicion.version:
        return jsonify({
            'error': 'La composición cambió desde que fue cargada',
            'codigo': 'VERSION_CONFLICT',
            'actual': composicion.to_dict(include_variantes=True),
        }), 409
    try:
        cavidades = int(data.get('cavidades', composicion.cavidades))
        peso = float(data.get('peso_unitario_gr', composicion.peso_unitario_gr))
        if cavidades <= 0 or peso <= 0:
            raise ValueError('cavidades y peso_unitario_gr deben ser positivos')
        composicion.cavidades = cavidades
        composicion.peso_unitario_gr = peso
        composicion.version += 1
        db.session.commit()
        return jsonify(composicion.to_dict(include_variantes=True)), 200
    except (TypeError, ValueError) as exc:
        db.session.rollback()
        return jsonify({'error': str(exc)}), 400


def _ensure_mold_color_variants(molde, color):
    """Crea o reutiliza el mismo color para todas las salidas activas."""
    composiciones = MoldePieza.query.filter_by(
        molde_id=molde.codigo,
        activo=True,
    ).order_by(MoldePieza.id).all()
    if not composiciones:
        raise ValueError('El molde no tiene piezas activas')
    if color.activo is False:
        raise ValueError('El color de producción está inactivo')

    creadas = []
    reutilizadas = []
    por_pieza = {}
    for composicion in composiciones:
        variante = PiezaColor.query.filter_by(
            pieza_id=composicion.pieza_id,
            color_produccion_id=color.id,
        ).one_or_none()
        if variante is None:
            pieza_maestra, linea, familia = _resolve_pieza_color_classification({
                'pieza_id': composicion.pieza_id,
            })
            variante = PiezaColor(
                sku=generar_codigo_catalogo('PIEZA_COLOR'),
                piezas=f"{pieza_maestra.nombre} {color.nombre}",
                peso=pieza_maestra.peso_nominal_gr,
                cavidad=None,
                linea_id=linea.id,
                familia_id=familia.id,
                color_produccion_id=color.id,
                pieza_id=pieza_maestra.id,
                estado_revision='EN_REVISION',
            )
            db.session.add(variante)
            db.session.flush()
            creadas.append(_pieza_color_to_dict(variante))
        else:
            reutilizadas.append(_pieza_color_to_dict(variante))
        por_pieza[composicion.pieza_id] = variante

    return {
        'molde': molde.to_dict(include_variantes=False),
        'color': _color_to_dict(color),
        'variantes_creadas': creadas,
        'variantes_reutilizadas': reutilizadas,
        'variantes': [
            _pieza_color_to_dict(por_pieza[item.pieza_id])
            for item in composiciones
        ],
    }


@catalogo_bp.route('/moldes/<codigo>/colores', methods=['POST'])
def habilitar_color_molde(codigo):
    """Habilita un color por golpe completo, nunca por salida aislada."""
    molde = db.session.get(Molde, codigo)
    if not molde or not molde.activo:
        return jsonify({'error': 'Molde activo no encontrado'}), 404
    data = request.get_json() or {}
    if data.get('color_id') is None:
        return jsonify({'error': 'Debe proveer un color_id'}), 400
    color = db.session.get(ColorProduccion, data['color_id'])
    if not color:
        return jsonify({'error': 'Color no encontrado'}), 404
    try:
        payload = _ensure_mold_color_variants(molde, color)
        db.session.commit()
        status = 201 if payload['variantes_creadas'] else 200
        return jsonify(payload), status
    except ClassificationError as exc:
        db.session.rollback()
        return _classification_error_response(exc)
    except ValueError as exc:
        db.session.rollback()
        return jsonify({'error': str(exc), 'codigo': 'COLOR_MOLDE_INVALIDO'}), 409
    except Exception as exc:
        db.session.rollback()
        return jsonify({'error': str(exc)}), 400


@catalogo_bp.route('/formas/<int:forma_id>/colores', methods=['POST'])
def crear_color_forma(forma_id):
    """Compatibilidad: habilita el color en todo el molde de la forma."""
    composicion = db.session.get(MoldePieza, forma_id)
    if not composicion or not composicion.activo:
        return jsonify({'error': 'Composición de molde no encontrada'}), 404
    data = request.get_json() or {}
    if str(data.get('sku') or '').strip():
        return _manual_identifier_error('sku')
    if data.get('color_id') is None:
        return jsonify({'error': 'Debe proveer un color_id'}), 400
    color = db.session.get(ColorProduccion, data['color_id'])
    if not color:
        return jsonify({'error': 'Color no encontrado'}), 404
    try:
        payload_grupo = _ensure_mold_color_variants(composicion.molde, color)
        variante = next(
            item for item in payload_grupo['variantes']
            if item['pieza_id'] == composicion.pieza_id
        )
        creados = {item['sku'] for item in payload_grupo['variantes_creadas']}
        payload = {
            **variante,
            'existed': variante['sku'] not in creados,
            'molde_id': composicion.molde_id,
            'variantes_creadas': payload_grupo['variantes_creadas'],
            'variantes_reutilizadas': payload_grupo['variantes_reutilizadas'],
        }
        db.session.commit()
        return jsonify(payload), 201 if creados else 200
    except ClassificationError as exc:
        db.session.rollback()
        return _classification_error_response(exc)
    except ValueError as exc:
        db.session.rollback()
        return jsonify({'error': str(exc), 'codigo': 'COLOR_MOLDE_INVALIDO'}), 409
    except Exception as exc:
        db.session.rollback()
        return jsonify({'error': str(exc)}), 400


@catalogo_bp.route('/formas/<int:forma_id>', methods=['DELETE'])
def eliminar_forma(forma_id):
    """Desvincula lógicamente la Pieza del molde; conserva maestro y SKUs."""
    composicion = db.session.get(MoldePieza, forma_id)
    if not composicion or not composicion.activo:
        return jsonify({'error': 'Composición de molde no encontrada'}), 404
    composicion.activo = False
    composicion.version += 1
    db.session.commit()
    return jsonify({
        'message': f'Pieza desvinculada del molde {composicion.molde_id}',
        'composicion': composicion.to_dict(),
    }), 200


# ============================================================
# VARIANTES PIEZA-COLOR
# ============================================================

@catalogo_bp.route('/piezas-color/<sku>', methods=['GET'])
def obtener_pieza_color(sku):
    """Obtiene una variante de pieza y color."""
    pieza = db.session.get(PiezaColor, sku)
    if not pieza:
        return jsonify({'error': 'PiezaColor no encontrada'}), 404
    
    payload = _pieza_color_to_dict(pieza)
    payload['linea'] = pieza.linea_rel.nombre if pieza.linea_rel else None
    payload['familia'] = pieza.familia_rel.nombre if pieza.familia_rel else None
    return jsonify(payload), 200


@catalogo_bp.route('/piezas-color/<sku>/imagen', methods=['GET', 'PUT', 'DELETE'])
def imagen_pieza_color(sku):
    pieza = db.session.get(PiezaColor, sku)
    if not pieza:
        return jsonify({'error': 'PiezaColor no encontrada'}), 404
    if request.method == 'GET':
        return _image_response(pieza)
    if request.method == 'DELETE':
        try:
            get_catalog_image_storage().delete(pieza)
            db.session.commit()
        except CatalogImageStorageError as exc:
            db.session.rollback()
            return jsonify({
                'error': str(exc),
                'codigo': 'IMAGEN_STORAGE_NO_DISPONIBLE',
            }), 503
        return jsonify(_pieza_color_to_dict(pieza))
    parsed, error = _read_catalog_image()
    if error:
        return error
    try:
        mime_type, content = parsed
        get_catalog_image_storage().store(
            pieza,
            category='pieza-color',
            identity=pieza.sku,
            mime_type=mime_type,
            content=content,
        )
        db.session.commit()
    except CatalogImageStorageError as exc:
        db.session.rollback()
        return jsonify({
            'error': str(exc),
            'codigo': 'IMAGEN_STORAGE_NO_DISPONIBLE',
        }), 503
    return jsonify(_pieza_color_to_dict(pieza))


@catalogo_bp.route('/piezas-color', methods=['POST'])
def crear_pieza_color():
    """Crea un SKU coloreado vinculado opcionalmente al maestro Pieza."""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Payload JSON requerido'}), 400
    if str(data.get('sku') or '').strip():
        return _manual_identifier_error('sku')
    if not str(data.get('nombre') or '').strip():
        return jsonify({'error': 'nombre es obligatorio'}), 400
    if str(data.get('tipo') or 'SIMPLE').strip().upper() in {
        'KIT',
        'COMPONENTE',
    } or data.get('componentes'):
        return jsonify({
            'error': (
                'KIT y COMPONENTE ya no son tipos de PiezaColor. '
                'Modele la composición mediante Artículo WIP y BOM.'
            ),
            'codigo': 'LEGACY_KIT_NOT_SUPPORTED',
        }), 422
    
    try:
        pieza_maestra, linea, familia = _resolve_pieza_color_classification(data)
        pieza = PiezaColor(
            sku=generar_codigo_catalogo('PIEZA_COLOR'),
            piezas=str(data['nombre']).strip(),
            peso=data.get('peso'),
            cavidad=data.get('cavidad'),  # legado, no gobierna el molde
            color_produccion_id=data.get('color_produccion_id'),
            cod_pieza=data.get('cod_pieza'),
            linea_id=linea.id,
            familia_id=familia.id,
            pieza_id=pieza_maestra.id if pieza_maestra else None,
            cod_extru=data.get('cod_extru'),
            tipo_extruccion=data.get('tipo_extruccion'),
            cod_mp=data.get('cod_mp'),
            mp=data.get('mp')
        )
        db.session.add(pieza)
        
        db.session.commit()
        return jsonify({'sku': pieza.sku, 'nombre': pieza.piezas}), 201
    except ClassificationError as exc:
        db.session.rollback()
        return _classification_error_response(exc)
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400


@catalogo_bp.route('/piezas-color/<sku>', methods=['PUT'])
def actualizar_pieza_color(sku):
    """Actualiza un SKU coloreado; las cavidades se editan en MoldePieza."""
    pieza = db.session.get(PiezaColor, sku)
    if not pieza:
        return jsonify({'error': 'PiezaColor no encontrada'}), 404
    
    data = request.get_json() or {}
    sku_solicitado = str(data.get('sku') or '').strip()
    if sku_solicitado and sku_solicitado != pieza.sku:
        return _immutable_identifier_error('sku', pieza.sku)
    if (
        str(data.get('tipo') or '').strip().upper()
        in {'KIT', 'COMPONENTE'}
        or 'componentes' in data
    ):
        return jsonify({
            'error': (
                'La composición legacy de PiezaColor fue retirada. '
                'Use Artículo WIP y BOM para nuevas composiciones.'
            ),
            'codigo': 'LEGACY_KIT_NOT_SUPPORTED',
        }), 422
    
    try:
        pieza_maestra, linea, familia = _resolve_pieza_color_classification(
            data,
            current=pieza,
        )
        pieza.piezas = data.get('nombre', pieza.piezas)
        pieza.peso = data.get('peso', pieza.peso)
        pieza.color_produccion_id = data.get(
            'color_produccion_id',
            pieza.color_produccion_id,
        )
        pieza.cod_pieza = data.get('cod_pieza', pieza.cod_pieza)
        pieza.linea_id = linea.id
        pieza.familia_id = familia.id
        pieza.pieza_id = pieza_maestra.id if pieza_maestra else None
        pieza.cod_extru = data.get('cod_extru', pieza.cod_extru)
        pieza.tipo_extruccion = data.get(
            'tipo_extruccion',
            pieza.tipo_extruccion,
        )
        pieza.cod_mp = data.get('cod_mp', pieza.cod_mp)
        pieza.mp = data.get('mp', pieza.mp)

        db.session.commit()
        return jsonify({'sku': pieza.sku, 'nombre': pieza.piezas}), 200
    except ClassificationError as exc:
        db.session.rollback()
        return _classification_error_response(exc)
    except Exception as exc:
        db.session.rollback()
        return jsonify({'error': str(exc)}), 400


@catalogo_bp.route('/piezas-color/<sku>', methods=['DELETE'])
def eliminar_pieza_color(sku):
    """Elimina un SKU coloreado si sus referencias permiten hacerlo."""
    pieza = db.session.get(PiezaColor, sku)
    if not pieza:
        return jsonify({'error': 'PiezaColor no encontrada'}), 404
    
    if ProductoPieza.query.filter_by(pieza_sku=sku).first():
        return jsonify({'error': 'No se puede eliminar: el SKU integra un producto terminado'}), 409
    try:
        db.session.delete(pieza)
        db.session.commit()
        return jsonify({'message': f'PiezaColor {sku} eliminada'}), 200
    except Exception as exc:
        db.session.rollback()
        return jsonify({'error': str(exc)}), 409


# ============================================================
# PIEZAS PRODUCIBLES (para selector de OP)
# ============================================================

@catalogo_bp.route('/piezas-producibles', methods=['GET'])
def obtener_piezas_producibles():
    """Retorna un registro por SKU y composición activa capaz de producirlo."""
    piezas = (
        PiezaColor.query
        .join(Pieza, Pieza.id == PiezaColor.pieza_id)
        .join(MoldePieza, MoldePieza.pieza_id == Pieza.id)
        .filter(Pieza.activo.is_(True), MoldePieza.activo.is_(True))
        .order_by(PiezaColor.piezas)
        .all()
    )

    result = []
    for p in piezas:
        for composicion in p.pieza_rel.molde_piezas:
            if not composicion.activo or not composicion.molde.activo:
                continue
            result.append({
                'sku': p.sku,
                'nombre': p.piezas,
                'pieza_id': p.pieza_id,
                'molde_pieza_id': composicion.id,
                'molde': composicion.molde.to_dict(include_variantes=False),
                'cavidades': composicion.cavidades,
                'peso_unitario_gr': composicion.peso_unitario_gr,
            })
    
    return jsonify(result), 200


# ============================================================
# COLORES
# ============================================================


def _color_to_dict(color, *, existed=None):
    payload = {
        'id': color.id,
        'nombre': str(color),
        'codigo': color.codigo_legacy or color.id,
        'codigo_legacy': color.codigo_legacy,
        'color_base_id': color.color_base_id,
        'color_base_nombre': color.color_base_rel.nombre if color.color_base_rel else None,
        'familia_color_id': color.familia_color_id,
        'familia_color_nombre': (
            color.familia_color_rel.nombre if color.familia_color_rel else None
        ),
        'hex_referencia': color.hex_referencia,
        'activo': color.activo,
        'version': color.version,
    }
    if existed is not None:
        payload['existed'] = existed
    return payload


@catalogo_bp.route('/colores', methods=['GET'])
def listar_colores():
    """Lista todos los colores de producción disponibles"""
    include_inactive = _query_flag('include_inactive', default=False)
    query = ColorProduccion.query
    if not include_inactive:
        query = query.filter(ColorProduccion.activo.is_(True))
    colores = query.all()
    return jsonify([_color_to_dict(c) for c in sorted(colores, key=lambda x: str(x))])

@catalogo_bp.route('/familias-color', methods=['GET'])
def listar_familias_color():
    query = FamiliaColor.query
    if not _query_flag('include_inactive', default=False):
        query = query.filter(FamiliaColor.activo.is_(True))
    familias_color = query.order_by(FamiliaColor.nombre).all()
    return jsonify([_familia_color_to_dict(item) for item in familias_color])


def _familia_color_to_dict(item):
    return {
        'id': item.id,
        'nombre': item.nombre,
        'codigo': item.codigo,
        'codigo_display': (
            f'FC-{item.codigo:06d}' if item.codigo is not None else None
        ),
        'activo': item.activo,
        'version': item.version,
    }


def _familia_color_payload(data, *, current_id=None):
    nombre = str(data.get('nombre', '')).strip().upper()
    if not nombre:
        raise ValueError('Nombre de familia requerido')
    raw_codigo = data.get('codigo')
    try:
        codigo = int(raw_codigo) if raw_codigo not in (None, '') else None
    except (TypeError, ValueError) as exc:
        raise ValueError('El código debe ser un número entero') from exc

    duplicate_name = FamiliaColor.query.filter(
        db.func.upper(FamiliaColor.nombre) == nombre,
        FamiliaColor.id != current_id,
    ).first()
    if duplicate_name is not None:
        raise ValueError('Ya existe una familia de color con ese nombre')
    if codigo is not None:
        duplicate_code = FamiliaColor.query.filter(
            FamiliaColor.codigo == codigo,
            FamiliaColor.id != current_id,
        ).first()
        if duplicate_code is not None:
            raise ValueError('Ya existe una familia de color con ese código')
    return nombre, codigo


@catalogo_bp.route('/familias-color', methods=['POST'])
def crear_familia_color():
    data = request.get_json() or {}
    if data.get('codigo') in (None, ''):
        data['codigo'] = _next_available_numeric_code(
            FamiliaColor, 'FAMILIA_COLOR'
        )
    try:
        nombre, codigo = _familia_color_payload(data)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    item = FamiliaColor(nombre=nombre, codigo=codigo, activo=True)
    db.session.add(item)
    db.session.commit()
    return jsonify(_familia_color_to_dict(item)), 201


@catalogo_bp.route('/familias-color/<int:familia_id>', methods=['PUT'])
def actualizar_familia_color(familia_id):
    item = db.session.get(FamiliaColor, familia_id)
    if item is None:
        return jsonify({'error': 'Familia de color no encontrada'}), 404
    data = request.get_json() or {}
    try:
        expected_version = int(data.get('version'))
    except (TypeError, ValueError):
        return jsonify({'error': 'version es requerida', 'codigo': 'VERSION_REQUERIDA'}), 400
    if expected_version != item.version:
        return jsonify({'error': 'La familia cambió; recargue antes de guardar', 'codigo': 'FAMILIA_COLOR_VERSION_CONFLICTO'}), 409
    try:
        nombre, codigo = _familia_color_payload({
            'nombre': data.get('nombre', item.nombre),
            'codigo': data.get('codigo', item.codigo),
        }, current_id=item.id)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    if codigo != item.codigo:
        return jsonify({
            'error': 'El código es inmutable.',
            'codigo': 'CODIGO_INMUTABLE',
        }), 400
    item.nombre = nombre
    item.codigo = codigo
    if 'activo' in data:
        item.activo = bool(data['activo'])
    item.version += 1
    db.session.commit()
    return jsonify(_familia_color_to_dict(item)), 200


@catalogo_bp.route('/familias-color/<int:familia_id>', methods=['DELETE'])
def inactivar_familia_color(familia_id):
    item = db.session.get(FamiliaColor, familia_id)
    if item is None:
        return jsonify({'error': 'Familia de color no encontrada'}), 404
    try:
        expected_version = int(request.args.get('version'))
    except (TypeError, ValueError):
        return jsonify({'error': 'version es requerida', 'codigo': 'VERSION_REQUERIDA'}), 400
    if expected_version != item.version:
        return jsonify({'error': 'La familia cambió; recargue antes de guardar', 'codigo': 'FAMILIA_COLOR_VERSION_CONFLICTO'}), 409
    item.activo = False
    item.version += 1
    db.session.commit()
    return jsonify(_familia_color_to_dict(item)), 200


@catalogo_bp.route('/formas', methods=['GET'])
def listar_formas():
    """Lista composiciones activas de molde; ``id`` identifica MoldePieza."""
    formas = (
        MoldePieza.query
        .join(Pieza)
        .join(Molde)
        .filter(MoldePieza.activo.is_(True))
        .order_by(MoldePieza.molde_id, Pieza.nombre)
        .all()
    )
    return jsonify([
        {
            **item.to_dict(include_variantes=False),
            'molde_codigo': item.molde.codigo,
            'molde_nombre': item.molde.nombre,
        }
        for item in formas
    ])

@catalogo_bp.route('/colores', methods=['POST'])
def crear_color():
    """Crea un nuevo color base (para create-on-the-fly) - NOTA: El front debe enviar ahora ColorProduccion, este endpoint podría quedar obsoleto o requerir familia_color_id"""
    data = request.get_json()
    nombre = data.get('nombre', '').strip().upper()
    familia_color_id = data.get('familia_color_id')
    hex_referencia = normalize_hex(data.get('hex_referencia'))
    
    if not nombre:
        return jsonify({'error': 'Nombre de color base requerido'}), 400
        
    if not familia_color_id:
        # Fallback para mantener compatibilidad con frontends antiguos (OrdenForm.jsx)
        fam_default = FamiliaColor.query.filter(db.func.upper(FamiliaColor.nombre) == 'SOLIDO').first()
        if not fam_default:
            fam_default = FamiliaColor(nombre='SOLIDO', codigo=1)
            db.session.add(fam_default)
            db.session.flush()
        familia_color_id = fam_default.id
    
    # 1. Buscar o crear ColorBase
    base_existente = ColorBase.query.filter(
        db.func.upper(ColorBase.nombre) == nombre
    ).first()
    
    if not base_existente:
        base_existente = ColorBase(nombre=nombre)
        db.session.add(base_existente)
        db.session.flush()
        
    # 2. Buscar o crear ColorProduccion
    prod_existente = ColorProduccion.query.filter_by(
        color_base_id=base_existente.id,
        familia_color_id=familia_color_id
    ).first()
    
    if prod_existente:
        return jsonify(_color_to_dict(prod_existente, existed=True)), 200
        
    # Crear nuevo ColorProduccion
    nuevo = ColorProduccion(
        color_base_id=base_existente.id,
        familia_color_id=familia_color_id,
        hex_referencia=hex_referencia,
        activo=bool(data.get('activo', True)),
    )
    db.session.add(nuevo)
    db.session.commit()
    
    return jsonify(_color_to_dict(nuevo, existed=False)), 201


@catalogo_bp.route('/colores/<int:color_id>', methods=['PUT'])
def actualizar_color(color_id):
    data = request.get_json() or {}
    color = db.session.get(ColorProduccion, color_id)
    if color is None:
        return jsonify({'error': 'Color de producción no encontrado'}), 404
    try:
        expected_version = int(data.get('version'))
    except (TypeError, ValueError):
        return jsonify({'error': 'version es requerida', 'codigo': 'VERSION_REQUERIDA'}), 400
    if expected_version != color.version:
        return jsonify({
            'error': 'El color cambió; recargue antes de guardar',
            'codigo': 'COLOR_VERSION_CONFLICTO',
        }), 409

    nombre = str(data.get('nombre', color.color_base_rel.nombre)).strip().upper()
    if not nombre:
        return jsonify({'error': 'Nombre de color base requerido'}), 400
    try:
        familia_id = int(data.get('familia_color_id', color.familia_color_id))
    except (TypeError, ValueError):
        return jsonify({'error': 'familia_color_id inválido'}), 400
    familia = db.session.get(FamiliaColor, familia_id)
    if familia is None:
        return jsonify({'error': 'Familia de color no encontrada'}), 404

    base = ColorBase.query.filter(db.func.upper(ColorBase.nombre) == nombre).first()
    if base is None:
        base = ColorBase(nombre=nombre)
        db.session.add(base)
        db.session.flush()
    duplicate = ColorProduccion.query.filter(
        ColorProduccion.color_base_id == base.id,
        ColorProduccion.familia_color_id == familia_id,
        ColorProduccion.id != color.id,
    ).first()
    if duplicate is not None:
        return jsonify({
            'error': 'Ya existe esa combinación de color base y acabado',
            'codigo': 'COLOR_PRODUCCION_DUPLICADO',
        }), 409

    color.color_base_id = base.id
    color.familia_color_id = familia_id
    color.hex_referencia = normalize_hex(data.get('hex_referencia', color.hex_referencia))
    if 'activo' in data:
        color.activo = bool(data['activo'])
    color.version += 1
    db.session.commit()
    return jsonify(_color_to_dict(color)), 200


@catalogo_bp.route('/colores/<int:color_id>', methods=['DELETE'])
def inactivar_color(color_id):
    color = db.session.get(ColorProduccion, color_id)
    if color is None:
        return jsonify({'error': 'Color de producción no encontrado'}), 404
    try:
        expected_version = int(request.args.get('version'))
    except (TypeError, ValueError):
        return jsonify({'error': 'version es requerida', 'codigo': 'VERSION_REQUERIDA'}), 400
    if expected_version != color.version:
        return jsonify({
            'error': 'El color cambió; recargue antes de inactivarlo',
            'codigo': 'COLOR_VERSION_CONFLICTO',
        }), 409
    color.activo = False
    color.version += 1
    db.session.commit()
    return jsonify(_color_to_dict(color)), 200


# ============================================================
# CONFIGURACIÓN RÁPIDA DE PRODUCTO (CASCADA)
# ============================================================

@catalogo_bp.route('/configurar-producto', methods=['POST'])
def configurar_producto_cascada():
    """
    Crea Molde + Formas (Pieza) + Piezas coloreadas opcionales.

    Las PIEZAS son maestras globales. MoldePieza define cavidades y peso
    operativo para este molde. Los SKUs coloreados apuntan a Pieza, no a
    una composición concreta.

    Payload:
    {
        "molde": { "nombre", "peso_tiro_gr", "tiempo_ciclo_std", "usar_existente" },
        "piezas": [ { "nombre", "cavidades", "peso_unitario_gr" } ],
        "kit": null,                 // legacy; cualquier objeto se rechaza
        "color_ids": [1, 2, 3],       // opcional: colores de inyección
        "linea": "JUGUETES", "cod_linea": 2,
        "familia": "PLAYEROS", "cod_familia": 14
    }
    """
    from app.models.producto import ProductoTerminado, ProductoPieza
    from app.models.molde import Molde, MoldePieza, Pieza

    data = request.get_json()
    if not data:
        return jsonify({'error': 'Payload JSON requerido'}), 400

    molde_solicitado = data.get('molde') or {}
    if (
        not molde_solicitado.get('usar_existente')
        and str(molde_solicitado.get('codigo') or '').strip()
    ):
        return _manual_identifier_error('molde.codigo')
    formas_solicitadas = data.get('formas', data.get('piezas', []))
    if not isinstance(formas_solicitadas, list) or not all(
        isinstance(forma, dict) for forma in formas_solicitadas
    ):
        return jsonify({'error': 'formas debe ser una lista de objetos'}), 400
    if not molde_solicitado.get('usar_existente') and not formas_solicitadas:
        return jsonify({
            'error': 'Un molde nuevo requiere al menos una pieza en su composición',
            'codigo': 'COMPOSICION_REQUERIDA',
        }), 400
    nombres_solicitados = [
        str(forma.get('nombre') or '').strip()
        for forma in formas_solicitadas
    ]
    nombres_no_vacios = [nombre for nombre in nombres_solicitados if nombre]
    if len(nombres_no_vacios) != len(set(nombres_no_vacios)):
        return jsonify({'error': 'Los nombres de pieza no pueden repetirse'}), 400
    for forma_solicitada in formas_solicitadas:
        manual_field = _manual_new_piece_identifier_field(forma_solicitada)
        if manual_field:
            return _manual_identifier_error(f'pieza.{manual_field}')

    color_ids_raw = data.get('color_ids', [])
    if color_ids_raw is None:
        color_ids_raw = []
    if not isinstance(color_ids_raw, list):
        return jsonify({'error': 'color_ids debe ser una lista'}), 400
    color_ids = []
    for raw_color_id in color_ids_raw:
        try:
            color_id = int(raw_color_id)
        except (TypeError, ValueError):
            return jsonify({'error': 'Cada color_id debe ser un entero positivo'}), 400
        if isinstance(raw_color_id, bool) or color_id <= 0 or str(raw_color_id).strip() != str(color_id):
            return jsonify({'error': 'Cada color_id debe ser un entero positivo'}), 400
        if color_id not in color_ids:
            color_ids.append(color_id)

    colores = ColorProduccion.query.filter(
        ColorProduccion.id.in_(color_ids)
    ).all() if color_ids else []
    colores_por_id = {color.id: color for color in colores}
    color_ids_faltantes = [color_id for color_id in color_ids if color_id not in colores_por_id]
    if color_ids_faltantes:
        return jsonify({
            'error': 'Uno o más colores de producción no existen',
            'codigo': 'COLOR_NO_ENCONTRADO',
            'color_ids': color_ids_faltantes,
        }), 400
    colores = [colores_por_id[color_id] for color_id in color_ids]

    kit_solicitado = data.get('kit')
    if kit_solicitado:
        return jsonify({
            'error': (
                'La configuración guiada ya no crea kits PiezaColor. '
                'Cree un Artículo WIP y defina su BOM.'
            ),
            'codigo': 'LEGACY_KIT_NOT_SUPPORTED',
        }), 422
    pt_solicitado = data.get('producto_terminado') or {}
    if (
        pt_solicitado
        and not pt_solicitado.get('usar_existente')
        and str(pt_solicitado.get('cod_sku_pt') or '').strip()
    ):
        return _manual_identifier_error('producto_terminado.cod_sku_pt')
    if (
        pt_solicitado.get('usar_existente')
        and not str(pt_solicitado.get('cod_sku_pt') or '').strip()
    ):
        return jsonify({
            'error': 'Código de producto requerido para usar uno existente'
        }), 400
    producto_existente_solicitado = None
    if pt_solicitado.get('usar_existente'):
        codigo_producto_existente = str(
            pt_solicitado['cod_sku_pt']
        ).strip()
        producto_existente_solicitado = db.session.get(
            ProductoTerminado,
            codigo_producto_existente,
        )
        if not producto_existente_solicitado:
            return jsonify({
                'error': (
                    f'Producto Terminado {codigo_producto_existente} no encontrado'
                )
            }), 404

    resultado = {
        'molde_creado': None,
        'molde_reutilizado': None,
        'formas_creadas': [],
        'formas_reutilizadas': [],
        'asociaciones_creadas': [],
        'asociaciones_reutilizadas': [],
        'piezas_maestras_creadas': [],
        'piezas_maestras_reutilizadas': [],
        'composicion_molde': [],
        'piezas_creadas': [],
        'variantes_creadas': [],
        'variantes_reutilizadas': [],
        'kit_creado': None,
        'producto_terminado': None,
        'errores': []
    }

    try:
        # --- Resolver Linea y Familia ---
        linea_id = data.get('linea_id')
        familia_id = data.get('familia_id')
        linea_obj = None
        familia_obj = None

        if linea_id:
            linea_obj = db.session.get(Linea, linea_id)
        elif data.get('cod_linea'):
            linea_obj = Linea.query.filter_by(codigo=data.get('cod_linea')).first()
        elif data.get('linea'):
            linea_obj = Linea.query.filter(Linea.nombre.ilike(data.get('linea'))).first()

        if familia_id:
            familia_obj = db.session.get(Familia, familia_id)
        elif data.get('cod_familia'):
            familia_obj = Familia.query.filter_by(codigo=data.get('cod_familia')).first()
        elif data.get('familia'):
            familia_obj = Familia.query.filter(Familia.nombre.ilike(data.get('familia'))).first()

        if not linea_obj or not familia_obj:
            return jsonify({'error': 'Linea y Familia son requeridas'}), 400
        linea_obj, familia_obj, _ = validate_linea_familia(
            linea_id=linea_obj.id,
            familia_id=familia_obj.id,
        )
        if producto_existente_solicitado and (
            producto_existente_solicitado.linea_id != linea_obj.id
            or producto_existente_solicitado.familia_id != familia_obj.id
        ):
            raise ClassificationError(
                'El producto existente pertenece a otra combinaciÃ³n de lÃ­nea y familia',
                'CLASIFICACION_PRODUCTO_DIVERGENTE',
                409,
            )

        # ── 1. CREAR O OBTENER MOLDE ──
        molde_data = data.get('molde', {})

        if molde_data.get('usar_existente'):
            molde_codigo = str(molde_data.get('codigo') or '').strip()
            if not molde_codigo:
                return jsonify({'error': 'Código de molde requerido para usar uno existente'}), 400
            molde = db.session.get(Molde, molde_codigo)
            if not molde:
                return jsonify({'error': f'Molde {molde_codigo} no encontrado'}), 404
            if not molde.activo:
                return jsonify({
                    'error': f'Molde {molde_codigo} está inactivo',
                    'codigo': 'MOLDE_INACTIVO',
                }), 409
            resultado['molde_reutilizado'] = molde.codigo
        else:
            molde_codigo = generar_codigo_catalogo('MOLDE')
            molde = Molde(
                codigo=molde_codigo,
                nombre=molde_data.get('nombre') or molde_codigo,
                peso_tiro_gr=molde_data.get('peso_tiro_gr', 0),
                tiempo_ciclo_std=molde_data.get('tiempo_ciclo_std', 30.0),
                activo=True
            )
            db.session.add(molde)
            db.session.flush()
            resultado['molde_creado'] = molde.codigo

        # ── 2. ASOCIAR PIEZAS GLOBALES AL MOLDE ──
        formas_creadas = []
        asociaciones_creadas_ids = set()
        piezas_maestras_creadas_ids = set()
        formas_payload = formas_solicitadas

        for idx, pieza_data in enumerate(formas_payload):
            payload_forma = dict(pieza_data)
            payload_forma.setdefault('nombre', f'Forma {idx + 1}')
            payload_forma.setdefault('linea_id', linea_obj.id)
            payload_forma.setdefault('familia_id', familia_obj.id)

            existente = None
            if payload_forma.get('pieza_id') is not None:
                existente = MoldePieza.query.filter_by(
                    molde_id=molde.codigo,
                    pieza_id=int(payload_forma['pieza_id']),
                    activo=True,
                ).first()
            if existente:
                resultado['errores'].append(
                    f'Pieza {existente.pieza.codigo} ya asociada al molde; usando existente'
                )
                formas_creadas.append(existente)
                resultado['formas_reutilizadas'].append(existente.pieza.nombre)
                continue

            pieza_global, composicion = _piece_and_composition_from_payload(
                molde,
                payload_forma,
            )
            db.session.add(composicion)
            db.session.flush()
            formas_creadas.append(composicion)
            asociaciones_creadas_ids.add(composicion.id)
            if payload_forma.get('pieza_id') is None:
                piezas_maestras_creadas_ids.add(pieza_global.id)
            resultado['formas_creadas'].append(pieza_global.nombre)

        # Al reutilizar un molde, los colores y BOM se generan sobre TODA
        # su composición vigente. El payload solo expresa asociaciones nuevas o
        # explícitamente seleccionadas; nunca recorta las piezas ya existentes.
        if molde_data.get('usar_existente'):
            formas_creadas = MoldePieza.query.filter_by(
                molde_id=molde.codigo,
                activo=True,
            ).order_by(MoldePieza.id).all()

        if not formas_creadas:
            raise ValueError('El molde debe tener al menos una pieza activa')

        resultado['composicion_molde'] = [
            {
                'molde_pieza_id': forma.id,
                'pieza_id': forma.pieza_id,
                'pieza_codigo': forma.pieza.codigo,
                'pieza_nombre': forma.pieza.nombre,
                'cavidades': forma.cavidades,
                'peso_unitario_gr': forma.peso_unitario_gr,
            }
            for forma in formas_creadas
        ]
        resultado['asociaciones_creadas'] = [
            item for item in resultado['composicion_molde']
            if item['molde_pieza_id'] in asociaciones_creadas_ids
        ]
        resultado['asociaciones_reutilizadas'] = [
            item for item in resultado['composicion_molde']
            if item['molde_pieza_id'] not in asociaciones_creadas_ids
        ]
        resultado['piezas_maestras_creadas'] = [
            {
                'id': forma.pieza.id,
                'codigo': forma.pieza.codigo,
                'nombre': forma.pieza.nombre,
            }
            for forma in formas_creadas
            if forma.pieza_id in piezas_maestras_creadas_ids
        ]
        resultado['piezas_maestras_reutilizadas'] = [
            {
                'id': forma.pieza.id,
                'codigo': forma.pieza.codigo,
                'nombre': forma.pieza.nombre,
            }
            for forma in formas_creadas
            if forma.pieza_id not in piezas_maestras_creadas_ids
        ]

        # ── 3. CREAR PIEZAS COLOREADAS (opcional) ──
        if colores:
            for color in colores:
                for forma in formas_creadas:
                    nombre_coloreado = f"{forma.nombre} {color.nombre}"

                    pieza_existente = PiezaColor.query.filter_by(
                        pieza_id=forma.pieza_id,
                        color_produccion_id=color.id,
                    ).first()
                    if pieza_existente:
                        resultado['errores'].append(
                            f'PiezaColor {pieza_existente.sku} ya existe'
                        )
                        resultado['variantes_reutilizadas'].append(pieza_existente.sku)
                        continue

                    sku_pieza = generar_codigo_catalogo('PIEZA_COLOR')
                    _, forma_linea, forma_familia = (
                        _resolve_pieza_color_classification({
                            'pieza_id': forma.pieza_id,
                        })
                    )
                    pieza = PiezaColor(
                        sku=sku_pieza,
                        piezas=nombre_coloreado,
                        peso=forma.peso_unitario_gr,
                        cavidad=None,
                        linea_id=forma_linea.id,
                        familia_id=forma_familia.id,
                        color_produccion_id=color.id,
                        pieza_id=forma.pieza_id,
                    )
                    db.session.add(pieza)
                    resultado['piezas_creadas'].append(sku_pieza)
                    resultado['variantes_creadas'].append(sku_pieza)

        db.session.flush()

        # ── 5. CREAR O VINCULAR PRODUCTO TERMINADO (BOM Comercial) ──
        pt_data = data.get('producto_terminado')
        if pt_data:
            usar_existente = pt_data.get('usar_existente', False)
            
            producto = None
            if usar_existente:
                producto = producto_existente_solicitado
            else:
                cod_sku_pt = generar_codigo_catalogo('PRODUCTO_TERMINADO')
                producto = ProductoTerminado(
                    cod_sku_pt=cod_sku_pt,
                    producto=pt_data.get('producto', f'Producto derivado de {molde.nombre}'),
                    um=pt_data.get('um', 'Docena'),
                    doc_x_paq=pt_data.get('doc_x_paq', 1.0),
                    doc_x_bulto=pt_data.get('doc_x_bulto', 10.0),
                    peso_g=pt_data.get('peso_g', 0.0),
                    linea_id=linea_obj.id,
                    familia_id=familia_obj.id,
                    # Campos legacy/vacios requeridos por DB
                    precio_estimado=0.0,
                    precio_sin_igv=0.0
                )
                db.session.add(producto)
                db.session.flush()
                resultado['producto_terminado'] = cod_sku_pt

            if producto:
                # Actualizar el BOM (ProductoPieza) eliminando las anteriores si las hubiera
                ProductoPieza.query.filter_by(producto_terminado_id=producto.cod_sku_pt).delete()
                
                # Compatibilidad legacy: este asistente todavía mantiene el BOM
                # comercial simple con una PiezaColor genérica por forma. Las
                # composiciones multinivel nuevas se administran en SCM Estructuras.
                for forma in formas_creadas:
                    pieza_std = PiezaColor.query.filter_by(
                        pieza_id=forma.pieza_id,
                        color_produccion_id=None,
                    ).first()
                    if not pieza_std:
                        sku_std = generar_codigo_catalogo('PIEZA_COLOR')
                        _, forma_linea, forma_familia = (
                            _resolve_pieza_color_classification({
                                'pieza_id': forma.pieza_id,
                            })
                        )
                        pieza_std = PiezaColor(
                            sku=sku_std,
                            piezas=f"{forma.nombre} (Genérico)",
                            peso=forma.peso_unitario_gr,
                            cavidad=None,
                            linea_id=forma_linea.id,
                            familia_id=forma_familia.id,
                            pieza_id=forma.pieza_id
                            # Sin color
                        )
                        db.session.add(pieza_std)
                        db.session.flush()
                        resultado['piezas_creadas'].append(sku_std)
                        resultado['variantes_creadas'].append(sku_std)
                    elif pieza_std.sku not in resultado['variantes_reutilizadas']:
                        resultado['variantes_reutilizadas'].append(pieza_std.sku)
                    
                    # Agregar al BOM del Producto
                    db.session.add(ProductoPieza(
                        producto_terminado_id=producto.cod_sku_pt,
                        pieza_sku=pieza_std.sku,
                        cantidad=forma.cavidades
                    ))

        db.session.commit()

        return jsonify({
            'success': True,
            'resultado': resultado
        }), 201

    except ClassificationError as exc:
        db.session.rollback()
        return _classification_error_response(exc)
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e), 'resultado': resultado}), 400




# ============================================================
# PRE-VALIDACIÓN PARA CREACIÓN DE OP
# ============================================================

@catalogo_bp.route('/validar-orden-prereq', methods=['GET'])
def validar_orden_prereq():
    """
    Valida pre-requisitos para crear una orden de producción.
    Verifica molde, piezas asociadas y disponibilidad de SKUs para colores.
    
    Query params:
        - molde_id: código del molde
        - color_ids: lista de IDs de colores separados por coma (opcional)
    """
    molde_id = request.args.get('molde_id', '').strip()
    producto_sku = request.args.get('producto_sku', '').strip() or None
    maquina_id = request.args.get('maquina_id', '').strip() or None
    numero_op = request.args.get('numero_op', '').strip() or None
    color_ids_str = request.args.get('color_ids', '')

    result = {
        'valid': True,
        'warnings': [],
        'errors': [],
        'issues': [],
        'molde': None,
        'maquina': None,
        'numero_op': numero_op,
        'producto_sku': producto_sku,
        'colores_info': [],
        'variantes_por_crear': [],
    }

    try:
        maquina = validate_order_header_prerequisites(
            numero_op=numero_op,
            maquina_id=maquina_id,
            session=db.session,
            require_values=False,
        )
        if maquina is not None:
            result['maquina'] = {
                'id': maquina.id,
                'codigo': maquina.codigo,
                'nombre': maquina.nombre,
                'estado': maquina.estado,
                'activo': maquina.activo,
            }
        if not molde_id:
            return jsonify(result), 200

        try:
            color_ids = [
                int(value.strip())
                for value in color_ids_str.split(',')
                if value.strip()
            ]
        except ValueError:
            raise OrderIntegrityError(
                'color_ids debe contener enteros separados por coma',
                'COLOR_IDS_INVALIDOS',
                400,
            )

        context = validate_order_prerequisites(
            molde_id=molde_id,
            color_ids=color_ids,
            producto_sku=producto_sku,
            session=db.session,
        )
        molde = context.molde
        result['molde'] = {
            'codigo': molde.codigo,
            'nombre': molde.nombre,
            'tiene_piezas': bool(context.snapshot_rows),
            'piezas_count': len(context.snapshot_rows),
            'tiempo_ciclo_std': molde.tiempo_ciclo_std,
            'peso_tiro_gr': molde.peso_tiro_gr,
            'composicion': list(context.snapshot_rows),
        }
        result['variantes_por_crear'] = list(context.pending_variants)
        if context.pending_variants:
            result['warnings'].append(
                f"Se crearán {len(context.pending_variants)} variantes PiezaColor al guardar la OP"
            )

        for color_id in color_ids:
            color_prod = db.session.get(ColorProduccion, color_id)
            pending = [
                item for item in context.pending_variants
                if item['color_id'] == color_id
            ]
            existing = []
            for row in context.snapshot_rows:
                if row.get('pieza_id') is None:
                    continue
                variant = PiezaColor.query.filter_by(
                    pieza_id=row['pieza_id'],
                    color_produccion_id=color_id,
                ).one_or_none()
                if variant:
                    existing.append(variant.sku)
            result['colores_info'].append({
                'color_id': color_id,
                'color_nombre': str(color_prod),
                # Alias legacy: ya no se infiere un PT con first().
                'sku_encontrado': producto_sku,
                'sku_exists': bool(producto_sku),
                'pieza_color_skus': sorted(existing),
                'variantes_por_crear': pending,
            })

    except OrderIntegrityError as exc:
        db.session.rollback()
        result['valid'] = False
        result['errors'].append(exc.message)
        issue = {
            'codigo': exc.code,
            'mensaje': exc.message,
            'status': exc.status,
        }
        if exc.details:
            issue['details'] = exc.details
        result['issues'].append(issue)

    return jsonify(result), 200


# ============================================================
# IMPORTACIÓN MASIVA DESDE EXCEL/CSV
# ============================================================

@catalogo_bp.route('/importar/productos', methods=['POST'])
def importar_productos():
    """
    Importa productos terminados desde Excel o CSV.
    
    Query params:
        - mode: 'validate' o 'execute'
        - crear_colores: 'true' o 'false' (solo en mode=execute)
    
    Formatos soportados: .xlsx, .xls, .csv
    
    Errores retornados:
        - 400: Archivo no enviado o formato inválido
        - 422: Errores de validación en los datos
        - 500: Error interno
    """
    from app.services.import_service import ImportService
    
    mode = request.args.get('mode', 'validate')
    
    # Validar que se envió archivo
    if 'file' not in request.files:
        return jsonify({
            'error': 'No se envió archivo',
            'codigo': 'FILE_MISSING',
            'detalle': 'Debe enviar un archivo en el campo "file" del formulario'
        }), 400
    
    file = request.files['file']
    
    # Validar nombre de archivo
    if not file.filename:
        return jsonify({
            'error': 'Nombre de archivo vacío',
            'codigo': 'FILENAME_EMPTY'
        }), 400
    
    service = ImportService()
    file_bytes = file.read()
    
    # Parsear archivo (soporta Excel y CSV)
    df, parse_result = service.parsear_archivo(file_bytes, file.filename, tipo='productos')
    
    if df is None:
        return jsonify({
            'error': 'No se pudo leer el archivo',
            'codigo': 'PARSE_ERROR',
            'validacion': parse_result.to_dict()
        }), 400
    
    if mode == 'validate':
        # Solo validar, no importar
        resultado = service.validar_productos(df)
        # Combinar errores del parseo con validación
        resultado.errores = parse_result.errores + resultado.errores
        resultado.warnings = parse_result.warnings + resultado.warnings
        resultado.formato_archivo = parse_result.formato_archivo
        return jsonify(resultado.to_dict()), 200
    
    elif mode == 'execute':
        # Validar primero
        validacion = service.validar_productos(df)
        if not validacion.es_valido:
            return jsonify({
                'error': 'El archivo tiene errores que impiden la importación',
                'codigo': 'VALIDATION_FAILED',
                'validacion': validacion.to_dict()
            }), 422
        
        # Ejecutar importación
        try:
            crear_familias = request.args.get('crear_familias', 'true').lower() == 'true'
            resultado = service.ejecutar_import_productos(df, crear_familias=crear_familias)
            return jsonify(resultado), 200
        except Exception as e:
            import traceback
            traceback.print_exc()
            return jsonify({
                'error': 'Error interno durante la importación',
                'mensaje': str(e),
                'traceback': traceback.format_exc()
            }), 500
    
    return jsonify({
        'error': 'Modo inválido',
        'codigo': 'INVALID_MODE',
        'detalle': 'Use mode=validate o mode=execute'
    }), 400


@catalogo_bp.route('/importar/piezas', methods=['POST'])
def importar_piezas():
    """
    Importa piezas desde Excel o CSV.
    
    Query params:
        - mode: 'validate' o 'execute'
        - crear_colores: 'true' o 'false' (solo en mode=execute)
    
    Formatos soportados: .xlsx, .xls, .csv
    """
    from app.services.import_service import ImportService
    
    mode = request.args.get('mode', 'validate')
    
    if 'file' not in request.files:
        return jsonify({
            'error': 'No se envió archivo',
            'codigo': 'FILE_MISSING'
        }), 400
    
    file = request.files['file']
    
    if not file.filename:
        return jsonify({
            'error': 'Nombre de archivo vacío',
            'codigo': 'FILENAME_EMPTY'
        }), 400
    
    service = ImportService()
    file_bytes = file.read()
    
    df, parse_result = service.parsear_archivo(file_bytes, file.filename, tipo='piezas')
    
    if df is None:
        return jsonify({
            'error': 'No se pudo leer el archivo',
            'codigo': 'PARSE_ERROR',
            'validacion': parse_result.to_dict()
        }), 400
    
    if mode == 'validate':
        resultado = service.validar_piezas(df)
        resultado.errores = parse_result.errores + resultado.errores
        resultado.warnings = parse_result.warnings + resultado.warnings
        resultado.formato_archivo = parse_result.formato_archivo
        return jsonify(resultado.to_dict()), 200
    
    elif mode == 'execute':
        validacion = service.validar_piezas(df)
        if not validacion.es_valido:
            return jsonify({
                'error': 'El archivo tiene errores que impiden la importación',
                'codigo': 'VALIDATION_FAILED',
                'validacion': validacion.to_dict()
            }), 422
        
        crear_colores = request.args.get('crear_colores', 'true').lower() == 'true'
        resultado = service.ejecutar_import_piezas(df, crear_colores=crear_colores)
        return jsonify(resultado), 200
    
    return jsonify({
        'error': 'Modo inválido',
        'codigo': 'INVALID_MODE'
    }), 400


@catalogo_bp.route('/importar/colores-detectados', methods=['POST'])
def detectar_colores():
    """
    Analiza un archivo Excel/CSV y detecta colores/familias únicos para revisión.
    
    Para PRODUCTOS: Detecta FamiliaColor (SOLIDO, CARAMELO, TRANSPARENTE, etc.)
    Para PIEZAS: Detecta ColorBase (Rojo, Azul, Verde, etc.)
    """
    from app.services.import_service import ImportService
    from app.models.producto import FamiliaColor, ColorBase
    
    if 'file' not in request.files:
        return jsonify({'error': 'No se envió archivo'}), 400
    
    file = request.files['file']
    tipo = request.args.get('tipo', 'productos')
    
    service = ImportService()
    df, parse_result = service.parsear_archivo(file.read(), file.filename, tipo=tipo)
    
    if df is None:
        return jsonify({
            'error': 'No se pudo leer el archivo',
            'validacion': parse_result.to_dict()
        }), 400
    
    resultado = []
    
    if tipo == 'productos':
        # Para Productos: Verificar contra FamiliaColor (por NOMBRE, no código)
        familias_existentes = {f.nombre.upper(): f for f in FamiliaColor.query.all()}
        familias_archivo = {}
        
        for _, row in df.iterrows():
            nombre = service._obtener_valor_str(row, 'Familia Color')
            cod = service._obtener_valor_int(row, 'Cod Color')
            
            if nombre:
                nombre_upper = nombre.upper()
                if nombre_upper not in familias_archivo:
                    familias_archivo[nombre_upper] = cod or 0
        
        # Comparar con existentes
        for nombre, codigo in sorted(familias_archivo.items()):
            existe = nombre in familias_existentes
            
            resultado.append({
                'codigo': codigo,
                'nombre_archivo': nombre,
                'existe': existe,
                'nombre_db': nombre if existe else None,  # Es el mismo nombre si existe
                'conflicto': False  # No hay conflictos de nombre para familias
            })
        
        label = 'familias'
        
    else:
        # Para Piezas: Verificar contra ColorBase (por CÓDIGO u NOMBRE)
        colores_existentes = {c.id: c.nombre for c in ColorBase.query.all()}
        colores_archivo = {}
        
        for _, row in df.iterrows():
            cod = service._obtener_valor_int(row, 'Cod Color')
            nombre = service._obtener_valor_str(row, 'Color')
            
            if cod is not None and cod not in colores_archivo:
                colores_archivo[cod] = (nombre or f"COLOR_{cod}").upper()
        
        # Comparar con existentes
        for cod, nombre in sorted(colores_archivo.items()):
            existe = cod in colores_existentes
            nombre_existente = colores_existentes.get(cod)
            
            resultado.append({
                'codigo': cod,
                'nombre_archivo': nombre,
                'existe': existe,
                'nombre_db': nombre_existente,
                'conflicto': existe and nombre_existente and nombre_existente.upper() != nombre.upper()
            })
        
        label = 'colores'
    
    return jsonify({
        'total_colores': len(resultado),
        'nuevos': sum(1 for c in resultado if not c['existe']),
        'existentes': sum(1 for c in resultado if c['existe']),
        'conflictos': sum(1 for c in resultado if c.get('conflicto')),
        'formato_archivo': parse_result.formato_archivo,
        'tipo_detectado': label,  # 'familias' o 'colores'
        'colores': resultado
    }), 200


@catalogo_bp.route('/importar/formatos-soportados', methods=['GET'])
def obtener_formatos_soportados():
    """
    Retorna información sobre los formatos de archivo soportados.
    """
    return jsonify({
        'formatos': {
            'excel': {
                'extensiones': ['.xlsx', '.xls'],
                'descripcion': 'Microsoft Excel'
            },
            'csv': {
                'extensiones': ['.csv'],
                'descripcion': 'Comma Separated Values',
                'encodings_soportados': ['UTF-8', 'Latin-1', 'Windows-1252'],
                'delimitadores_soportados': [',', ';', '\\t (tab)', '|']
            }
        },
        'columnas_productos': {
            'requeridas': ['COD SKU PT', 'Producto'],
            'opcionales': ['Cod Linea', 'Linea', 'Cod Familia', 'Familia', 'Cod Color', 'Familia Color', 'PESO g.', '...']
        },
        'columnas_piezas': {
            'requeridas': ['SKU', 'PIEZAS'],
            'opcionales': ['Cod Linea', 'Cod PiezaColor', 'Cavidad', 'Peso', 'Cod Color', 'Color', '...']
        }
    }), 200


# ============================================================================
# ENDPOINTS DE REVISIÓN PROGRESIVA
# ============================================================================

@catalogo_bp.route('/productos/revision', methods=['GET'])
def listar_productos_revision():
    """
    Lista productos con filtros de revisión.
    
    Query params:
        - estado: IMPORTADO, EN_REVISION, VERIFICADO (opcional, default: todos)
        - q: término de búsqueda
        - linea: filtrar por línea
        - familia: filtrar por familia
        - page: página (default 1)
        - per_page: items por página (default 20)
    """
    from datetime import datetime
    
    estado = request.args.get('estado', '').strip().upper()
    q = request.args.get('q', '').strip()
    linea = request.args.get('linea', '').strip()
    familia = request.args.get('familia', '').strip()
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    
    query = ProductoTerminado.query
    
    # Filtro por estado de revisión
    if estado in ['IMPORTADO', 'EN_REVISION', 'VERIFICADO']:
        query = query.filter(ProductoTerminado.estado_revision == estado)
    
    # Filtro de búsqueda
    if q:
        search = f"%{q}%"
        query = query.filter(
            or_(
                ProductoTerminado.producto.ilike(search),
                ProductoTerminado.cod_sku_pt.ilike(search)
            )
        )
    
    # Nota: filtros por linea/familia eliminados - campos legacy ya no existen
    # Si se necesita filtrar, usar join con Linea/Familia tables
    
    # Ordenar por fecha de importación (más recientes primero)
    query = query.order_by(ProductoTerminado.fecha_importacion.desc().nullsfirst())
    
    # Paginación
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    
    productos = [{
        'cod_sku_pt': p.cod_sku_pt,
        'producto': p.producto,
        'familia': p.familia_rel.nombre if p.familia_rel else None,
        'linea': p.linea_rel.nombre if p.linea_rel else None,
        'peso_g': p.peso_g,
        'precio_estimado': p.precio_estimado,
        'estado_revision': p.estado_revision or 'IMPORTADO',
        'fecha_importacion': p.fecha_importacion.isoformat() if p.fecha_importacion else None,
        'fecha_revision': p.fecha_revision.isoformat() if p.fecha_revision else None,
        'notas_revision': p.notas_revision
    } for p in pagination.items]
    
    return jsonify({
        'productos': productos,
        'pagination': {
            'page': pagination.page,
            'per_page': pagination.per_page,
            'total': pagination.total,
            'pages': pagination.pages,
            'has_next': pagination.has_next,
            'has_prev': pagination.has_prev
        }
    })


@catalogo_bp.route('/productos/<cod_sku_pt>/revision', methods=['PUT'])
def actualizar_revision_producto(cod_sku_pt):
    """
    Actualiza el estado de revisión de un producto.
    
    Body JSON:
        - estado_revision: IMPORTADO, EN_REVISION, VERIFICADO
        - notas_revision: texto opcional con notas
    """
    from datetime import datetime
    
    producto = ProductoTerminado.query.get(cod_sku_pt)
    if not producto:
        return jsonify({'error': f'Producto {cod_sku_pt} no encontrado'}), 404
    
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Se requiere body JSON'}), 400
    
    nuevo_estado = data.get('estado_revision', '').upper()
    if nuevo_estado and nuevo_estado in ['IMPORTADO', 'EN_REVISION', 'VERIFICADO']:
        producto.estado_revision = nuevo_estado
        producto.fecha_revision = datetime.now()
    
    if 'notas_revision' in data:
        producto.notas_revision = data['notas_revision']
    
    db.session.commit()
    
    return jsonify({
        'message': 'Revisión actualizada',
        'producto': {
            'cod_sku_pt': producto.cod_sku_pt,
            'producto': producto.producto,
            'estado_revision': producto.estado_revision,
            'fecha_revision': producto.fecha_revision.isoformat() if producto.fecha_revision else None,
            'notas_revision': producto.notas_revision
        }
    })


@catalogo_bp.route('/productos/revision/bulk', methods=['PUT'])
def actualizar_revision_bulk():
    """
    Actualiza el estado de revisión de múltiples productos.
    
    Body JSON:
        - skus: lista de cod_sku_pt
        - estado_revision: nuevo estado
        - notas_revision: notas opcionales
    """
    from datetime import datetime
    
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Se requiere body JSON'}), 400
    
    skus = data.get('skus', [])
    nuevo_estado = data.get('estado_revision', '').upper()
    notas = data.get('notas_revision')
    
    if not skus:
        return jsonify({'error': 'Se requiere lista de SKUs'}), 400
    
    if nuevo_estado not in ['IMPORTADO', 'EN_REVISION', 'VERIFICADO']:
        return jsonify({'error': 'Estado no válido'}), 400
    
    # Actualizar en bulk
    productos = ProductoTerminado.query.filter(
        ProductoTerminado.cod_sku_pt.in_(skus)
    ).all()
    
    actualizados = 0
    for p in productos:
        p.estado_revision = nuevo_estado
        p.fecha_revision = datetime.now()
        if notas is not None:
            p.notas_revision = notas
        actualizados += 1
    
    db.session.commit()
    
    return jsonify({
        'message': f'{actualizados} productos actualizados',
        'actualizados': actualizados,
        'solicitados': len(skus)
    })


@catalogo_bp.route('/productos/revision/estadisticas', methods=['GET'])
def estadisticas_revision():
    """
    Retorna estadísticas de revisión de productos.
    """
    from sqlalchemy import func
    
    # Contar por estado
    stats_query = db.session.query(
        ProductoTerminado.estado_revision,
        func.count(ProductoTerminado.cod_sku_pt)
    ).group_by(ProductoTerminado.estado_revision).all()
    
    stats = {
        'IMPORTADO': 0,
        'EN_REVISION': 0,
        'VERIFICADO': 0
    }
    
    total = 0
    for estado, count in stats_query:
        key = estado if estado else 'IMPORTADO'
        stats[key] = count
        total += count
    
    # Por línea (top 5 con más pendientes)
    por_linea = db.session.query(
        Linea.nombre,
        func.count(ProductoTerminado.cod_sku_pt)
    ).join(Linea, ProductoTerminado.linea_id == Linea.id).filter(
        or_(
            ProductoTerminado.estado_revision == 'IMPORTADO',
            ProductoTerminado.estado_revision.is_(None)
        )
    ).group_by(Linea.nombre).order_by(
        func.count(ProductoTerminado.cod_sku_pt).desc()
    ).limit(5).all()
    
    return jsonify({
        'total': total,
        'por_estado': stats,
        'porcentaje_verificado': round((stats['VERIFICADO'] / total * 100) if total > 0 else 0, 1),
        'pendientes': stats['IMPORTADO'] + stats['EN_REVISION'],
        'por_linea_pendiente': [{'linea': linea or 'Sin Línea', 'cantidad': cant} for linea, cant in por_linea]
    })


# ============================================================================
# ENDPOINTS DE REVISIÓN PROGRESIVA - PIEZAS
# ============================================================================

@catalogo_bp.route('/piezas/revision', methods=['GET'])
@catalogo_bp.route('/piezas-color/revision', methods=['GET'])
def listar_piezas_revision():
    """
    Lista piezas con filtros de revisión.
    
    Query params:
        - estado: IMPORTADO, EN_REVISION, VERIFICADO
        - q: término de búsqueda
        - linea: filtrar por línea
        - page: página (default 1)
        - per_page: items por página (default 20)
    """
    estado = request.args.get('estado', '').strip().upper()
    q = request.args.get('q', '').strip()
    linea = request.args.get('linea', '').strip()
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    
    query = PiezaColor.query
    
    if estado in ['IMPORTADO', 'EN_REVISION', 'VERIFICADO']:
        query = query.filter(PiezaColor.estado_revision == estado)
    
    if q:
        search = f"%{q}%"
        query = query.filter(
            or_(
                PiezaColor.piezas.ilike(search),
                PiezaColor.sku.ilike(search)
            )
        )
    
    # Nota: filtro por linea eliminado - PiezaColor.linea ya no existe
    # Si se necesita filtrar por linea, usar join con Linea table
    
    query = query.order_by(PiezaColor.fecha_importacion.desc().nullsfirst())
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    
    piezas = [{
        'sku': p.sku,
        'piezas': p.piezas,
        'familia': p.familia_rel.nombre if p.familia_rel else None,
        'linea': p.linea_rel.nombre if p.linea_rel else None,
        'color': p.color_produccion_rel.nombre if p.color_produccion_rel else None,
        'peso': p.peso,
        'cavidad': p.cavidad,
        'estado_revision': p.estado_revision or 'IMPORTADO',
        'fecha_importacion': p.fecha_importacion.isoformat() if p.fecha_importacion else None,
        'fecha_revision': p.fecha_revision.isoformat() if p.fecha_revision else None,
        'notas_revision': p.notas_revision
    } for p in pagination.items]
    
    return jsonify({
        'piezas': piezas,
        'pagination': {
            'page': pagination.page,
            'per_page': pagination.per_page,
            'total': pagination.total,
            'pages': pagination.pages,
            'has_next': pagination.has_next,
            'has_prev': pagination.has_prev
        }
    })


@catalogo_bp.route('/piezas/<sku>/revision', methods=['PUT'])
@catalogo_bp.route('/piezas-color/<sku>/revision', methods=['PUT'])
def actualizar_revision_pieza(sku):
    """Actualiza el estado de revisión de una pieza."""
    from datetime import datetime
    
    pieza = PiezaColor.query.get(sku)
    if not pieza:
        return jsonify({'error': f'PiezaColor {sku} no encontrada'}), 404
    
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Se requiere body JSON'}), 400
    
    nuevo_estado = data.get('estado_revision', '').upper()
    if nuevo_estado and nuevo_estado in ['IMPORTADO', 'EN_REVISION', 'VERIFICADO']:
        pieza.estado_revision = nuevo_estado
        pieza.fecha_revision = datetime.now()
    
    if 'notas_revision' in data:
        pieza.notas_revision = data['notas_revision']
    
    db.session.commit()
    
    return jsonify({
        'message': 'Revisión actualizada',
        'pieza': {
            'sku': pieza.sku,
            'piezas': pieza.piezas,
            'estado_revision': pieza.estado_revision,
            'fecha_revision': pieza.fecha_revision.isoformat() if pieza.fecha_revision else None,
            'notas_revision': pieza.notas_revision
        }
    })


@catalogo_bp.route('/piezas/revision/bulk', methods=['PUT'])
@catalogo_bp.route('/piezas-color/revision/bulk', methods=['PUT'])
def actualizar_revision_piezas_bulk():
    """Actualiza el estado de revisión de múltiples piezas."""
    from datetime import datetime
    
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Se requiere body JSON'}), 400
    
    skus = data.get('skus', [])
    nuevo_estado = data.get('estado_revision', '').upper()
    notas = data.get('notas_revision')
    
    if not skus:
        return jsonify({'error': 'Se requiere lista de SKUs'}), 400
    
    if nuevo_estado not in ['IMPORTADO', 'EN_REVISION', 'VERIFICADO']:
        return jsonify({'error': 'Estado no válido'}), 400
    
    piezas = PiezaColor.query.filter(PiezaColor.sku.in_(skus)).all()
    
    actualizados = 0
    for p in piezas:
        p.estado_revision = nuevo_estado
        p.fecha_revision = datetime.now()
        if notas is not None:
            p.notas_revision = notas
        actualizados += 1
    
    db.session.commit()
    
    return jsonify({
        'message': f'{actualizados} piezas actualizadas',
        'actualizados': actualizados,
        'solicitados': len(skus)
    })


@catalogo_bp.route('/piezas/revision/estadisticas', methods=['GET'])
@catalogo_bp.route('/piezas-color/revision/estadisticas', methods=['GET'])
def estadisticas_revision_piezas():
    """Retorna estadísticas de revisión de piezas."""
    from sqlalchemy import func
    
    stats_query = db.session.query(
        PiezaColor.estado_revision,
        func.count(PiezaColor.sku)
    ).group_by(PiezaColor.estado_revision).all()
    
    stats = {'IMPORTADO': 0, 'EN_REVISION': 0, 'VERIFICADO': 0}
    total = 0
    for estado, count in stats_query:
        key = estado if estado else 'IMPORTADO'
        stats[key] = count
        total += count
    
    por_linea = db.session.query(
        Linea.nombre,
        func.count(PiezaColor.sku)
    ).join(Linea, PiezaColor.linea_id == Linea.id).filter(
        or_(
            PiezaColor.estado_revision == 'IMPORTADO',
            PiezaColor.estado_revision.is_(None)
        )
    ).group_by(Linea.nombre).order_by(
        func.count(PiezaColor.sku).desc()
    ).limit(5).all()
    
    return jsonify({
        'total': total,
        'por_estado': stats,
        'porcentaje_verificado': round((stats['VERIFICADO'] / total * 100) if total > 0 else 0, 1),
        'pendientes': stats['IMPORTADO'] + stats['EN_REVISION'],
        'por_linea_pendiente': [{'linea': linea or 'Sin Línea', 'cantidad': cant} for linea, cant in por_linea]
    })


# ============================================================
# RECETA COLOR NORMALIZADA — Prefill inteligente de pigmentos
# ============================================================


@catalogo_bp.route('/catalogo/ingredientes-receta-color', methods=['GET'])
def listar_ingredientes_receta_color():
    return jsonify(list_recipe_ingredients(
        db.session,
        include_inactive=_query_flag('include_inactive', default=False),
    )), 200


@catalogo_bp.route('/catalogo/recetas-color', methods=['GET'])
def listar_recetas_color():
    color_id = request.args.get('color_produccion_id', type=int)
    return jsonify({
        'items': list_recipes(
            db.session,
            color_produccion_id=color_id,
            include_inactive=_query_flag('include_inactive', default=False),
        )
    }), 200


@catalogo_bp.route('/catalogo/recetas-color/<int:recipe_id>', methods=['GET'])
def obtener_receta_color_maestra(recipe_id):
    from app.models.receta_color import RecetaColorMaestra

    recipe = db.session.get(RecetaColorMaestra, recipe_id)
    if recipe is None:
        raise ColorRecipeError(
            f'Receta {recipe_id} no encontrada.',
            code='RECETA_NO_ENCONTRADA',
            status=404,
        )
    return jsonify(serialize_recipe(recipe)), 200


@catalogo_bp.route('/catalogo/recetas-color', methods=['POST'])
def crear_receta_color_maestra():
    return jsonify(create_recipe(db.session, request.get_json() or {})), 201


@catalogo_bp.route('/catalogo/recetas-color/<int:recipe_id>', methods=['PUT'])
def actualizar_receta_color_maestra(recipe_id):
    return jsonify(update_recipe(
        db.session,
        recipe_id,
        request.get_json() or {},
    )), 200


@catalogo_bp.route('/catalogo/recetas-color/<int:recipe_id>', methods=['DELETE'])
def inactivar_receta_color_maestra(recipe_id):
    return jsonify(deactivate_recipe(
        db.session,
        recipe_id,
        version=request.args.get('version'),
    )), 200


@catalogo_bp.route('/catalogo/receta-color', methods=['GET'])
def obtener_receta_color():
    """
    Devuelve los pigmentos sugeridos para un color, basados en el
    promedio ponderado acumulado de OPs anteriores.

    Query params:
        color_id    (int, requerido)
        producto_sku (str, opcional) — busca receta específica primero
        meta_kg     (float, opcional) — calcula gramos absolutos si se envía
    """
    from app.models.receta_color import RecetaColorNormalizada

    color_produccion_id = request.args.get('color_produccion_id', type=int)
    if not color_produccion_id:
        return jsonify({'error': 'color_produccion_id requerido'}), 400

    producto_sku = request.args.get('producto_sku') or None
    meta_kg = request.args.get('meta_kg', type=float)

    color = db.session.get(ColorProduccion, color_produccion_id)
    if not color:
        return jsonify({'error': f'ColorProduccion {color_produccion_id} no encontrado'}), 404

    manual_recipe = find_default_recipe(
        db.session,
        color_produccion_id=color_produccion_id,
        producto_sku=producto_sku,
    )
    if manual_recipe is not None:
        kg_virgen_base = request.args.get('kg_virgen_base', type=float)
        materials = []
        pigments = []
        for line in manual_recipe.lineas:
            if line.tipo_componente == 'MATERIA_PRIMA':
                materials.append({
                    'material_id': line.material_id,
                    'nombre': line.material.nombre if line.material else None,
                    'fraccion': float(line.cantidad),
                    'categoria_recepcion_codigo': (
                        line.material.categoria_recepcion.codigo
                        if line.material and line.material.categoria_recepcion
                        else None
                    ),
                    'modalidad_recepcion': (
                        line.material.categoria_recepcion.modalidad_default
                        if line.material and line.material.categoria_recepcion
                        else None
                    ),
                })
                continue
            item = {
                'material_id': line.material_id,
                'colorante_id': (
                    line.material.colorante.id
                    if line.material and line.material.colorante
                    else None
                ),
                'nombre': line.material.nombre if line.material else None,
                'tipo_componente': line.tipo_componente,
                'dosis_gramos': float(line.cantidad),
                'base_kg': float(line.base_kg),
            }
            if kg_virgen_base is not None and kg_virgen_base > 0:
                item['gramos'] = round(
                    float(line.cantidad) * kg_virgen_base / float(line.base_kg),
                    2,
                )
            pigments.append(item)
        return jsonify({
            'color_produccion_id': color_produccion_id,
            'color_nombre': color.nombre,
            'producto_sku': manual_recipe.producto_sku,
            'tiene_receta': True,
            'fuente': 'RECETA_MAESTRA',
            'receta': serialize_recipe(manual_recipe),
            'materias_primas': materials,
            'pigmentos': pigments,
            'n_muestras_min': 0,
        }), 200

    # Estrategia: buscar receta específica primero, luego genérica como fallback
    if producto_sku:
        especificas = RecetaColorNormalizada.query.filter_by(
            color_produccion_id=color_produccion_id, producto_sku=producto_sku
        ).all()
        genericas = RecetaColorNormalizada.query.filter_by(
            color_produccion_id=color_produccion_id, producto_sku=None
        ).all()
        con_especifica = {r.colorante_id for r in especificas}
        recetas = especificas + [r for r in genericas if r.colorante_id not in con_especifica]
        sku_usado = producto_sku if especificas else None
    else:
        recetas = RecetaColorNormalizada.query.filter_by(
            color_produccion_id=color_produccion_id, producto_sku=None
        ).all()
        sku_usado = None

    if not recetas:
        return jsonify({
            'color_produccion_id': color_produccion_id,
            'color_nombre': color.nombre,
            'producto_sku': sku_usado,
            'tiene_receta': False,
            'n_muestras_min': 0,
            'pigmentos': []
        }), 200

    n_muestras_min = min(r.n_muestras for r in recetas)

    return jsonify({
        'color_produccion_id': color_produccion_id,
        'color_nombre': color.nombre,
        'producto_sku': sku_usado,
        'tiene_receta': True,
        'n_muestras_min': n_muestras_min,
        'pigmentos': [r.to_dict(meta_kg=meta_kg) for r in recetas]
    }), 200

def _query_flag(name, default=False):
    raw = request.args.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {'1', 'true', 'si', 'sí', 'yes'}


def _payload_bool(value, field):
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {'1', 'true', 'si', 'sí', 'yes'}:
        return True
    if normalized in {'0', 'false', 'no'}:
        return False
    raise ClassificationError(
        f'{field} debe ser booleano',
        'CATALOGO_INVALIDO',
    )


def _catalog_payload(data, current=None):
    codigo_raw = data.get('codigo', current.codigo if current else None)
    try:
        codigo = int(codigo_raw)
    except (TypeError, ValueError):
        raise ClassificationError(
            'codigo debe ser un entero positivo',
            'CATALOGO_INVALIDO',
        )
    nombre = str(data.get('nombre', current.nombre if current else '')).strip()
    if codigo <= 0 or not nombre:
        raise ClassificationError(
            'codigo positivo y nombre son obligatorios',
            'CATALOGO_INVALIDO',
        )
    activo_raw = data.get('activo', current.activo if current else True)
    activo = _payload_bool(activo_raw, 'activo')
    return codigo, nombre, activo


def _catalog_duplicate(model, *, codigo, nombre, exclude_id=None):
    query = model.query.filter(or_(
        model.codigo == codigo,
        db.func.lower(model.nombre) == nombre.lower(),
    ))
    if exclude_id is not None:
        query = query.filter(model.id != exclude_id)
    return query.first()


def _expected_catalog_version(data=None):
    raw = (data or {}).get('version')
    if raw is None:
        raw = request.args.get('version')
    if raw is None:
        raise ClassificationError(
            'version es obligatoria',
            'VERSION_REQUIRED',
        )
    try:
        version = int(raw)
    except (TypeError, ValueError):
        raise ClassificationError(
            'version debe ser un entero positivo',
            'VERSION_INVALIDA',
        )
    if version <= 0:
        raise ClassificationError(
            'version debe ser un entero positivo',
            'VERSION_INVALIDA',
        )
    return version


def _catalog_usage_response(item, usage):
    return jsonify({
        'error': 'No se puede inactivar un clasificador que está en uso',
        'codigo': 'CATALOGO_EN_USO',
        'actual': item.to_dict(),
        'uso': usage,
    }), 409


def _create_catalog(model, noun, code_key):
    data = request.get_json() or {}
    if data.get('codigo') in (None, ''):
        data['codigo'] = _next_available_numeric_code(model, code_key)
    try:
        codigo, nombre, activo = _catalog_payload(data)
    except ClassificationError as exc:
        return _classification_error_response(exc)
    if _catalog_duplicate(model, codigo=codigo, nombre=nombre):
        return jsonify({
            'error': f'Ya existe otra {noun} con ese código o nombre',
            'codigo': 'CATALOGO_DUPLICADO',
        }), 409
    item = model(codigo=codigo, nombre=nombre, activo=activo)
    db.session.add(item)
    try:
        db.session.commit()
        return jsonify(item.to_dict()), 201
    except Exception as exc:
        db.session.rollback()
        return jsonify({'error': str(exc)}), 409


def _update_catalog(model, item_id, noun, usage_key):
    item = db.session.get(model, item_id)
    if not item:
        return jsonify({'error': f'{noun.capitalize()} no encontrada'}), 404
    data = request.get_json() or {}
    try:
        expected_version = _expected_catalog_version(data)
        codigo, nombre, activo = _catalog_payload(data, current=item)
    except ClassificationError as exc:
        return _classification_error_response(exc)
    if codigo != item.codigo:
        return jsonify({
            'error': 'El código es inmutable.',
            'codigo': 'CODIGO_INMUTABLE',
        }), 400
    if expected_version != item.version:
        return jsonify({
            'error': f'La {noun} cambió desde que fue cargada',
            'codigo': 'VERSION_CONFLICT',
            'actual': item.to_dict(),
        }), 409
    if _catalog_duplicate(
        model,
        codigo=codigo,
        nombre=nombre,
        exclude_id=item.id,
    ):
        return jsonify({
            'error': f'Ya existe otra {noun} con ese código o nombre',
            'codigo': 'CATALOGO_DUPLICADO',
        }), 409
    if item.activo and not activo:
        usage = classification_usage(**{usage_key: item.id})
        if usage['total']:
            return _catalog_usage_response(item, usage)

    item.codigo = codigo
    item.nombre = nombre
    item.activo = activo
    item.version += 1
    try:
        db.session.commit()
        return jsonify(item.to_dict()), 200
    except Exception as exc:
        db.session.rollback()
        return jsonify({'error': str(exc)}), 409


def _deactivate_catalog(model, item_id, noun, usage_key):
    item = db.session.get(model, item_id)
    if not item:
        return jsonify({'error': f'{noun.capitalize()} no encontrada'}), 404
    try:
        expected_version = _expected_catalog_version()
    except ClassificationError as exc:
        return _classification_error_response(exc)
    if expected_version != item.version:
        return jsonify({
            'error': f'La {noun} cambió desde que fue cargada',
            'codigo': 'VERSION_CONFLICT',
            'actual': item.to_dict(),
        }), 409
    if not item.activo:
        return jsonify(item.to_dict()), 200
    usage = classification_usage(**{usage_key: item.id})
    if usage['total']:
        return _catalog_usage_response(item, usage)
    item.activo = False
    item.version += 1
    db.session.commit()
    return jsonify(item.to_dict()), 200


@catalogo_bp.route('/catalogo/lineas', methods=['GET'])
def listar_lineas():
    """Lista líneas y filtra opcionalmente por una familia asociada."""
    query = Linea.query
    if not _query_flag('include_inactive'):
        query = query.filter(Linea.activo.is_(True))
    familia_id = request.args.get('familia_id', type=int)
    if request.args.get('familia_id') is not None and familia_id is None:
        return jsonify({'error': 'familia_id debe ser un entero'}), 400
    if familia_id is not None:
        query = query.join(LineaFamilia).filter(
            LineaFamilia.familia_id == familia_id,
            LineaFamilia.activo.is_(True),
        )
    q = request.args.get('q', '').strip()
    if q:
        criteria = [Linea.nombre.ilike(f'%{q}%')]
        if q.isdigit():
            criteria.append(Linea.codigo == int(q))
        query = query.filter(or_(*criteria))
    return jsonify([
        item.to_dict()
        for item in query.order_by(Linea.codigo, Linea.nombre).all()
    ]), 200


@catalogo_bp.route('/catalogo/lineas/<int:linea_id>', methods=['GET'])
def obtener_linea(linea_id):
    linea = db.session.get(Linea, linea_id)
    if not linea:
        return jsonify({'error': 'Línea no encontrada'}), 404
    return jsonify(linea.to_dict()), 200


@catalogo_bp.route('/catalogo/lineas', methods=['POST'])
def crear_linea():
    return _create_catalog(Linea, 'línea', 'LINEA')


@catalogo_bp.route('/catalogo/lineas/<int:linea_id>', methods=['PUT'])
def actualizar_linea(linea_id):
    return _update_catalog(Linea, linea_id, 'línea', 'linea_id')


@catalogo_bp.route('/catalogo/lineas/<int:linea_id>', methods=['DELETE'])
def inactivar_linea(linea_id):
    return _deactivate_catalog(Linea, linea_id, 'línea', 'linea_id')


@catalogo_bp.route('/catalogo/familias', methods=['GET'])
def listar_familias():
    """Lista familias; ``linea_id`` devuelve solo asociaciones vigentes."""
    query = Familia.query
    if not _query_flag('include_inactive'):
        query = query.filter(Familia.activo.is_(True))
    linea_id = request.args.get('linea_id', type=int)
    if request.args.get('linea_id') is not None and linea_id is None:
        return jsonify({'error': 'linea_id debe ser un entero'}), 400
    if linea_id is not None:
        query = query.join(LineaFamilia).filter(
            LineaFamilia.linea_id == linea_id,
            LineaFamilia.activo.is_(True),
        )
    q = request.args.get('q', '').strip()
    if q:
        criteria = [Familia.nombre.ilike(f'%{q}%')]
        if q.isdigit():
            criteria.append(Familia.codigo == int(q))
        query = query.filter(or_(*criteria))
    return jsonify([
        item.to_dict()
        for item in query.order_by(Familia.codigo, Familia.nombre).all()
    ]), 200


@catalogo_bp.route('/catalogo/familias/<int:familia_id>', methods=['GET'])
def obtener_familia(familia_id):
    familia = db.session.get(Familia, familia_id)
    if not familia:
        return jsonify({'error': 'Familia no encontrada'}), 404
    return jsonify(familia.to_dict()), 200


@catalogo_bp.route('/catalogo/familias', methods=['POST'])
def crear_familia():
    return _create_catalog(Familia, 'familia', 'FAMILIA')


@catalogo_bp.route('/catalogo/familias/<int:familia_id>', methods=['PUT'])
def actualizar_familia(familia_id):
    return _update_catalog(Familia, familia_id, 'familia', 'familia_id')


@catalogo_bp.route('/catalogo/familias/<int:familia_id>', methods=['DELETE'])
def inactivar_familia(familia_id):
    return _deactivate_catalog(Familia, familia_id, 'familia', 'familia_id')


@catalogo_bp.route('/catalogo/lineas/<int:linea_id>/familias', methods=['GET'])
def listar_familias_de_linea(linea_id):
    linea = db.session.get(Linea, linea_id)
    if not linea:
        return jsonify({'error': 'Línea no encontrada'}), 404
    query = LineaFamilia.query.filter_by(linea_id=linea_id)
    if not _query_flag('include_inactive'):
        query = query.filter(LineaFamilia.activo.is_(True))
    relaciones = query.join(Familia).order_by(Familia.codigo, Familia.nombre).all()
    return jsonify([
        relacion.to_dict(include_catalogos=True)
        for relacion in relaciones
    ]), 200


@catalogo_bp.route('/catalogo/lineas/<int:linea_id>/familias', methods=['POST'])
def asociar_familia_a_linea(linea_id):
    data = request.get_json() or {}
    linea = db.session.get(Linea, linea_id)
    if not linea:
        return jsonify({'error': 'Línea no encontrada'}), 404
    if not linea.activo:
        return jsonify({
            'error': 'Solo se pueden asociar familias a una línea activa',
            'codigo': 'CATALOGO_INACTIVO',
        }), 409

    familia_data = data.get('familia')
    familia_id_raw = data.get('familia_id')
    if familia_data is not None and familia_id_raw not in (None, ''):
        return jsonify({
            'error': 'Envíe familia_id o familia, no ambos',
            'codigo': 'CATALOGO_INVALIDO',
        }), 400

    if familia_data is not None:
        if not isinstance(familia_data, dict):
            return jsonify({
                'error': 'familia debe ser un objeto con codigo y nombre',
                'codigo': 'CATALOGO_INVALIDO',
            }), 400
        if familia_data.get('codigo') in (None, ''):
            familia_data['codigo'] = _next_available_numeric_code(
                Familia, 'FAMILIA'
            )
        try:
            codigo, nombre, activo = _catalog_payload(familia_data)
        except ClassificationError as exc:
            return _classification_error_response(exc)
        if not activo:
            return jsonify({
                'error': 'La familia creada en contexto debe iniciar activa',
                'codigo': 'CATALOGO_INACTIVO',
            }), 409
        if _catalog_duplicate(Familia, codigo=codigo, nombre=nombre):
            return jsonify({
                'error': 'Ya existe otra familia con ese código o nombre',
                'codigo': 'CATALOGO_DUPLICADO',
            }), 409
        familia = Familia(codigo=codigo, nombre=nombre, activo=True)
        db.session.add(familia)
        db.session.flush()
    else:
        try:
            familia_id = int(familia_id_raw)
        except (TypeError, ValueError):
            return jsonify({
                'error': 'familia_id debe ser un entero positivo',
                'codigo': 'CATALOGO_INVALIDO',
            }), 400
        if familia_id <= 0:
            return jsonify({
                'error': 'familia_id debe ser un entero positivo',
                'codigo': 'CATALOGO_INVALIDO',
            }), 400
        familia = db.session.get(Familia, familia_id)
        if not familia:
            return jsonify({'error': 'Familia no encontrada'}), 404

    if not familia.activo:
        return jsonify({
            'error': 'Solo se pueden asociar familias activas',
            'codigo': 'CATALOGO_INACTIVO',
        }), 409

    relacion = LineaFamilia.query.filter_by(
        linea_id=linea_id,
        familia_id=familia.id,
    ).first()
    status = 200
    if not relacion:
        relacion = LineaFamilia(
            linea_id=linea_id,
            familia_id=familia.id,
            activo=True,
        )
        db.session.add(relacion)
        status = 201
    elif not relacion.activo:
        relacion.activo = True
        relacion.version += 1
    try:
        db.session.commit()
        return jsonify(relacion.to_dict(include_catalogos=True)), status
    except Exception as exc:
        db.session.rollback()
        return jsonify({
            'error': str(exc),
            'codigo': 'CATALOGO_DUPLICADO',
        }), 409


@catalogo_bp.route(
    '/catalogo/lineas/<int:linea_id>/familias/<int:familia_id>',
    methods=['DELETE'],
)
def desasociar_familia_de_linea(linea_id, familia_id):
    relacion = LineaFamilia.query.filter_by(
        linea_id=linea_id,
        familia_id=familia_id,
        activo=True,
    ).first()
    if not relacion:
        return jsonify({'error': 'Asociación activa no encontrada'}), 404
    usage = classification_usage(linea_id=linea_id, familia_id=familia_id)
    if usage['total']:
        return jsonify({
            'error': 'No se puede desasociar una combinación que está en uso',
            'codigo': 'LINEA_FAMILIA_EN_USO',
            'asociacion': relacion.to_dict(include_catalogos=True),
            'uso': usage,
        }), 409
    relacion.activo = False
    relacion.version += 1
    db.session.commit()
    return jsonify(relacion.to_dict(include_catalogos=True)), 200

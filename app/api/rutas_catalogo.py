"""
Rutas API para el Catálogo de Productos y Piezas (SKU).
Incluye endpoints de listado y búsqueda.
"""
from flask import Blueprint, jsonify, request
from app.extensions import db
from app.models.producto import ProductoTerminado, Pieza, ProductoPieza
from sqlalchemy import or_

catalogo_bp = Blueprint('catalogo', __name__)

@catalogo_bp.route('/productos', methods=['GET'])
def listar_productos():
    """
    Lista Productos Terminados con búsqueda opcional.
    Query params:
        - q: término de búsqueda (busca en producto, familia, sku)
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
                ProductoTerminado.familia.ilike(search),
                ProductoTerminado.cod_sku_pt.ilike(search),
                ProductoTerminado.nombre_gs1.ilike(search)
            )
        )
    
    productos = query.limit(limit).all()
    
    return jsonify([{
        'cod_sku_pt': p.cod_sku_pt,
        'producto': p.producto,
        'familia': p.familia,
        'linea': p.linea,
        'peso_g': p.peso_g,
        'precio_estimado': p.precio_estimado,
        'status': p.status,
        'codigo_barra': p.codigo_barra,
        'num_piezas': len(p.composicion_piezas)  # Cantidad de piezas asociadas
    } for p in productos])


@catalogo_bp.route('/piezas', methods=['GET'])
def listar_piezas():
    """
    Lista Piezas con búsqueda opcional.
    Query params:
        - q: término de búsqueda (busca en sku, nombre pieza, color, familia)
        - producto_id: filtrar por producto terminado (SKU PT)
        - limit: máximo de resultados (default 50)
    """
    q = request.args.get('q', '').strip()
    producto_id = request.args.get('producto_id', '').strip()
    limit = request.args.get('limit', 50, type=int)
    
    query = Pieza.query
    
    # Filtrar por producto via tabla intermedia
    if producto_id:
        query = query.join(ProductoPieza).filter(ProductoPieza.producto_terminado_id == producto_id)
    
    if q:
        search = f"%{q}%"
        query = query.filter(
            or_(
                Pieza.sku.ilike(search),
                Pieza.piezas.ilike(search),
                Pieza.color.ilike(search),
                Pieza.familia.ilike(search)
            )
        )
    
    piezas = query.limit(limit).all()
    
    return jsonify([{
        'sku': p.sku,
        'piezas': p.piezas,
        'familia': p.familia,
        'linea': p.linea,
        'color': p.color,
        'peso': p.peso,
        'cavidad': p.cavidad,
        'tipo_extruccion': p.tipo_extruccion,
        'num_productos': len(p.en_productos),  # En cuantos productos está esta pieza
        'productos': [ep.producto_terminado.producto for ep in p.en_productos[:3]]  # Primeros 3 nombres
    } for p in piezas])


@catalogo_bp.route('/productos/<cod_sku_pt>', methods=['GET'])
def obtener_producto(cod_sku_pt):
    """
    Obtiene un Producto Terminado por su SKU, incluyendo sus piezas.
    """
    producto = db.session.get(ProductoTerminado, cod_sku_pt)
    if not producto:
        return jsonify({'error': 'Producto no encontrado'}), 404
    
    return jsonify({
        'cod_sku_pt': producto.cod_sku_pt,
        'producto': producto.producto,
        'familia': producto.familia,
        'linea': producto.linea,
        'peso_g': producto.peso_g,
        'precio_estimado': producto.precio_estimado,
        'precio_sin_igv': producto.precio_sin_igv,
        'status': producto.status,
        'codigo_barra': producto.codigo_barra,
        'marca': producto.marca,
        'um': producto.um,
        'piezas': [{
            'sku': cp.pieza.sku,
            'nombre': cp.pieza.piezas,
            'color': cp.pieza.color,
            'peso': cp.pieza.peso,
            'cantidad': cp.cantidad
        } for cp in producto.composicion_piezas]
    })

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
        'tipo': p.tipo,
        'tipo_extruccion': p.tipo_extruccion,
        'molde_id': p.molde_id,
        'molde_nombre': p.molde.nombre if p.molde else None,
        'num_productos': len(p.en_productos),
        'productos': [ep.producto_terminado.producto for ep in p.en_productos[:3]]
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


@catalogo_bp.route('/productos', methods=['POST'])
def crear_producto():
    """Crea un nuevo ProductoTerminado con sus piezas (BOM)"""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Payload JSON requerido'}), 400
    
    try:
        producto = ProductoTerminado(
            cod_sku_pt=data['cod_sku_pt'],
            producto=data['producto'],
            familia=data.get('familia'),
            linea=data.get('linea'),
            peso_g=data.get('peso_g'),
            precio_estimado=data.get('precio_estimado'),
            status=data.get('status', 'Activo'),
            codigo_barra=data.get('codigo_barra'),
            marca=data.get('marca'),
            um=data.get('um', 'Unidad')
        )
        db.session.add(producto)
        
        # Agregar piezas (BOM)
        for pieza_data in data.get('piezas', []):
            pp = ProductoPieza(
                producto_terminado_id=producto.cod_sku_pt,
                pieza_sku=pieza_data['pieza_sku'],
                cantidad=pieza_data.get('cantidad', 1)
            )
            db.session.add(pp)
        
        db.session.commit()
        return jsonify({'cod_sku_pt': producto.cod_sku_pt, 'producto': producto.producto}), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400


@catalogo_bp.route('/productos/<cod_sku_pt>', methods=['PUT'])
def actualizar_producto(cod_sku_pt):
    """Actualiza un ProductoTerminado y sus piezas"""
    producto = db.session.get(ProductoTerminado, cod_sku_pt)
    if not producto:
        return jsonify({'error': 'Producto no encontrado'}), 404
    
    data = request.get_json()
    
    producto.producto = data.get('producto', producto.producto)
    producto.familia = data.get('familia', producto.familia)
    producto.linea = data.get('linea', producto.linea)
    producto.peso_g = data.get('peso_g', producto.peso_g)
    producto.precio_estimado = data.get('precio_estimado', producto.precio_estimado)
    producto.status = data.get('status', producto.status)
    producto.codigo_barra = data.get('codigo_barra', producto.codigo_barra)
    producto.marca = data.get('marca', producto.marca)
    producto.um = data.get('um', producto.um)
    
    # Actualizar piezas (BOM) si se proveen
    if 'piezas' in data:
        ProductoPieza.query.filter_by(producto_terminado_id=cod_sku_pt).delete()
        for pieza_data in data['piezas']:
            pp = ProductoPieza(
                producto_terminado_id=cod_sku_pt,
                pieza_sku=pieza_data['pieza_sku'],
                cantidad=pieza_data.get('cantidad', 1)
            )
            db.session.add(pp)
    
    db.session.commit()
    return jsonify({'cod_sku_pt': producto.cod_sku_pt, 'producto': producto.producto}), 200


@catalogo_bp.route('/productos/<cod_sku_pt>', methods=['DELETE'])
def eliminar_producto(cod_sku_pt):
    """Elimina un ProductoTerminado"""
    producto = db.session.get(ProductoTerminado, cod_sku_pt)
    if not producto:
        return jsonify({'error': 'Producto no encontrado'}), 404
    
    # Eliminar relaciones con piezas
    ProductoPieza.query.filter_by(producto_terminado_id=cod_sku_pt).delete()
    
    db.session.delete(producto)
    db.session.commit()
    return jsonify({'message': f'Producto {cod_sku_pt} eliminado'}), 200


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
from app.models.molde import Molde, MoldePieza
from app.models.producto import PiezaComponente

@catalogo_bp.route('/moldes/exportar', methods=['GET'])
def exportar_moldes():
    """Exporta todos los moldes con sus piezas para sincronización offline"""
    moldes = Molde.query.filter_by(activo=True).all()
    
    result = []
    for m in moldes:
        piezas = []
        for mp in m.piezas:
            pieza = db.session.get(Pieza, mp.pieza_sku)
            piezas.append({
                'sku': mp.pieza_sku,
                'nombre': pieza.piezas if pieza else mp.pieza_sku,
                'tipo': pieza.tipo if pieza else 'SIMPLE',
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
    return jsonify(molde.to_dict()), 200


@catalogo_bp.route('/moldes', methods=['POST'])
def crear_molde():
    """Crea un nuevo molde"""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Payload JSON requerido'}), 400
    
    try:
        molde = Molde(
            codigo=data['codigo'],
            nombre=data['nombre'],
            peso_tiro_gr=data['peso_tiro_gr'],
            tiempo_ciclo_std=data.get('tiempo_ciclo_std', 30.0),
            activo=data.get('activo', True),
            notas=data.get('notas')
        )
        db.session.add(molde)
        
        # Agregar piezas si se proveen
        for pieza_data in data.get('piezas', []):
            mp = MoldePieza(
                molde_id=molde.codigo,
                pieza_sku=pieza_data['pieza_sku'],
                cavidades=pieza_data['cavidades'],
                peso_unitario_gr=pieza_data['peso_unitario_gr']
            )
            db.session.add(mp)
        
        db.session.commit()
        return jsonify(molde.to_dict()), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400


@catalogo_bp.route('/moldes/<codigo>', methods=['PUT'])
def actualizar_molde(codigo):
    """Actualiza un molde existente"""
    molde = db.session.get(Molde, codigo)
    if not molde:
        return jsonify({'error': 'Molde no encontrado'}), 404
    
    data = request.get_json()
    
    molde.nombre = data.get('nombre', molde.nombre)
    molde.peso_tiro_gr = data.get('peso_tiro_gr', molde.peso_tiro_gr)
    molde.tiempo_ciclo_std = data.get('tiempo_ciclo_std', molde.tiempo_ciclo_std)
    molde.activo = data.get('activo', molde.activo)
    molde.notas = data.get('notas', molde.notas)
    
    # Actualizar piezas si se proveen
    if 'piezas' in data:
        MoldePieza.query.filter_by(molde_id=codigo).delete()
        for pieza_data in data['piezas']:
            mp = MoldePieza(
                molde_id=codigo,
                pieza_sku=pieza_data['pieza_sku'],
                cavidades=pieza_data['cavidades'],
                peso_unitario_gr=pieza_data['peso_unitario_gr']
            )
            db.session.add(mp)
    
    db.session.commit()
    return jsonify(molde.to_dict()), 200


@catalogo_bp.route('/moldes/<codigo>', methods=['DELETE'])
def eliminar_molde(codigo):
    """Elimina un molde"""
    molde = db.session.get(Molde, codigo)
    if not molde:
        return jsonify({'error': 'Molde no encontrado'}), 404
    
    db.session.delete(molde)
    db.session.commit()
    return jsonify({'message': f'Molde {codigo} eliminado'}), 200


# ============================================================
# PIEZAS CON TIPO Y COMPONENTES
# ============================================================

@catalogo_bp.route('/piezas/<sku>', methods=['GET'])
def obtener_pieza(sku):
    """Obtiene una pieza específica con componentes si es KIT"""
    pieza = db.session.get(Pieza, sku)
    if not pieza:
        return jsonify({'error': 'Pieza no encontrada'}), 404
    
    return jsonify({
        'sku': pieza.sku,
        'nombre': pieza.piezas,
        'tipo': pieza.tipo,
        'peso': pieza.peso,
        'cavidad': pieza.cavidad,
        'linea': pieza.linea,
        'familia': pieza.familia,
        'color': pieza.color,
        'componentes': [c.to_dict() for c in pieza.componentes] if pieza.tipo == 'KIT' else [],
        'moldes': [{'molde_id': mp.molde_id, 'cavidades': mp.cavidades, 'peso_unitario': mp.peso_unitario_gr} 
                   for mp in pieza.molde_piezas] if hasattr(pieza, 'molde_piezas') else []
    }), 200


@catalogo_bp.route('/piezas', methods=['POST'])
def crear_pieza():
    """Crea una nueva pieza"""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Payload JSON requerido'}), 400
    
    try:
        pieza = Pieza(
            sku=data['sku'],
            piezas=data['nombre'],
            tipo=data.get('tipo', 'SIMPLE'),
            peso=data.get('peso'),
            cavidad=data.get('cavidad'),
            linea=data.get('linea'),
            familia=data.get('familia'),
            color=data.get('color'),
            cod_linea=data.get('cod_linea'),
            cod_pieza=data.get('cod_pieza')
        )
        db.session.add(pieza)
        
        # Si es KIT, agregar componentes
        if data.get('tipo') == 'KIT' and data.get('componentes'):
            for comp in data['componentes']:
                pc = PiezaComponente(
                    kit_sku=pieza.sku,
                    componente_sku=comp['componente_sku'],
                    cantidad=comp.get('cantidad', 1)
                )
                db.session.add(pc)
        
        db.session.commit()
        return jsonify({'sku': pieza.sku, 'nombre': pieza.piezas}), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400


@catalogo_bp.route('/piezas/<sku>', methods=['PUT'])
def actualizar_pieza(sku):
    """Actualiza una pieza existente"""
    pieza = db.session.get(Pieza, sku)
    if not pieza:
        return jsonify({'error': 'Pieza no encontrada'}), 404
    
    data = request.get_json()
    
    pieza.piezas = data.get('nombre', pieza.piezas)
    pieza.tipo = data.get('tipo', pieza.tipo)
    pieza.peso = data.get('peso', pieza.peso)
    pieza.cavidad = data.get('cavidad', pieza.cavidad)
    pieza.linea = data.get('linea', pieza.linea)
    pieza.familia = data.get('familia', pieza.familia)
    pieza.color = data.get('color', pieza.color)
    
    # Actualizar componentes si es KIT
    if data.get('componentes') is not None:
        PiezaComponente.query.filter_by(kit_sku=sku).delete()
        for comp in data['componentes']:
            pc = PiezaComponente(
                kit_sku=sku,
                componente_sku=comp['componente_sku'],
                cantidad=comp.get('cantidad', 1)
            )
            db.session.add(pc)
    
    db.session.commit()
    return jsonify({'sku': pieza.sku, 'nombre': pieza.piezas}), 200


@catalogo_bp.route('/piezas/<sku>', methods=['DELETE'])
def eliminar_pieza(sku):
    """Elimina una pieza"""
    pieza = db.session.get(Pieza, sku)
    if not pieza:
        return jsonify({'error': 'Pieza no encontrada'}), 404
    
    # Verificar que no esté en uso
    if MoldePieza.query.filter_by(pieza_sku=sku).first():
        return jsonify({'error': 'No se puede eliminar: pieza está asociada a un molde'}), 400
    
    db.session.delete(pieza)
    db.session.commit()
    return jsonify({'message': f'Pieza {sku} eliminada'}), 200


# ============================================================
# PIEZAS PRODUCIBLES (para selector de OP)
# ============================================================

@catalogo_bp.route('/piezas-producibles', methods=['GET'])
def obtener_piezas_producibles():
    """Retorna solo piezas que tienen un molde asignado (producibles)"""
    piezas = (
        Pieza.query
        .filter(Pieza.molde_id.isnot(None))
        .order_by(Pieza.piezas)
        .all()
    )
    
    result = []
    for p in piezas:
        result.append({
            'sku': p.sku,
            'nombre': p.piezas,
            'tipo': p.tipo,
            'molde': {
                'codigo': p.molde.codigo if p.molde else None,
                'nombre': p.molde.nombre if p.molde else None,
                'peso_tiro_gr': p.molde.peso_tiro_gr if p.molde else None,
                'tiempo_ciclo_std': p.molde.tiempo_ciclo_std if p.molde else None
            },
            'cavidades': p.cavidad,
            'peso_unitario_gr': p.peso
        })
    
    return jsonify(result), 200


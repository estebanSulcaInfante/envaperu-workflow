"""
Rutas API para el Catálogo de Productos y Piezas (SKU).
Incluye endpoints de listado y búsqueda.
"""
from flask import Blueprint, jsonify, request
from app.extensions import db
from app.models.producto import ProductoTerminado, Pieza, ProductoPieza, ColorProducto, Linea, Familia
from sqlalchemy import or_

catalogo_bp = Blueprint('catalogo', __name__)

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
                ProductoTerminado.familia_color.ilike(search)
            )
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
        'cod_producto': p.cod_producto,
        'familia_color': p.familia_color,
        'cod_familia_color': p.cod_familia_color,
        'um': p.um,
        'doc_x_paq': p.doc_x_paq,
        'doc_x_bulto': p.doc_x_bulto,
        'peso_g': p.peso_g,
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
def listar_piezas():
    """
    Lista Piezas con búsqueda opcional.
    Query params:
        - q: término de búsqueda (busca en múltiples campos)
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
                Pieza.mp.ilike(search),
                Pieza.molde_id.ilike(search),
                Pieza.tipo_extruccion.ilike(search)
            )
        )
    
    piezas = query.limit(limit).all()
    
    return jsonify([{
        'sku': p.sku,
        'piezas': p.piezas,
        'tipo': p.tipo,
        # Usar datos normalizados de Linea (campos legacy eliminados)
        'linea': p.linea_rel.nombre if p.linea_rel else None,
        'cod_linea': p.linea_rel.codigo if p.linea_rel else None,
        'linea_id': p.linea_id,
        # Usar datos normalizados de Familia (campos legacy eliminados)
        'familia': p.familia_rel.nombre if p.familia_rel else None,
        'cod_familia': p.familia_rel.codigo if p.familia_rel else None,
        'familia_id': p.familia_id,
        'cod_pieza': p.cod_pieza,
        'molde_id': p.molde_id,
        'molde_nombre': p.molde.nombre if p.molde else None,
        'color': p.color,
        'cod_color': p.cod_color,
        'tipo_color': p.tipo_color,
        'cod_col': p.cod_col,
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
        if 'piezas' in data and len(data['piezas']) > 0:
            for pieza_data in data.get('piezas', []):
                mp = MoldePieza(
                    molde_id=molde.codigo,
                    pieza_sku=pieza_data['pieza_sku'],
                    cavidades=pieza_data['cavidades'],
                    peso_unitario_gr=pieza_data['peso_unitario_gr']
                )
                db.session.add(mp)
        
        # --- SIMPLE MODE: Crear pieza automática si no se especificaron piezas ---
        elif data.get('cavidades') and data.get('peso_unitario_gr'):
            # Generar una pieza automática
            pieza_sku = f"{molde.codigo}-STD"
            
            # Verificar si existe, sino crear
            pieza = db.session.get(Pieza, pieza_sku)
            if not pieza:
                # Obtener o crear Linea y Familia por defecto
                linea_default = Linea.query.filter_by(nombre='GENERAL').first()
                if not linea_default:
                    linea_default = Linea(codigo=99, nombre='GENERAL')
                    db.session.add(linea_default)
                    db.session.flush()
                
                familia_default = Familia.query.filter_by(nombre='COMPONENTES').first()
                if not familia_default:
                    familia_default = Familia(codigo=99, nombre='COMPONENTES')
                    db.session.add(familia_default)
                    db.session.flush()
                
                pieza = Pieza(
                    sku=pieza_sku,
                    piezas=f"{molde.nombre} (Std)",
                    linea_id=linea_default.id,
                    familia_id=familia_default.id,
                    # Asignar peso/cavidad base a la pieza
                    peso=float(data.get('peso_unitario_gr')),
                    cavidad=int(data.get('cavidades')),
                    tipo='SIMPLE',
                    molde_id=molde.codigo # Link directo
                )
                db.session.add(pieza)
            
            # Crear relación Molde-Pieza
            mp = MoldePieza(
                molde_id=molde.codigo,
                pieza_sku=pieza_sku,
                cavidades=int(data.get('cavidades')),
                peso_unitario_gr=float(data.get('peso_unitario_gr'))
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
    # Actualizar piezas si se proveen explicitamente
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
            
    # --- SIMPLE MODE: Actualizar primera pieza existente ---
    elif 'cavidades' in data and 'peso_unitario_gr' in data:
        piezas_molde = MoldePieza.query.filter_by(molde_id=codigo).all()
        
        if piezas_molde:
            # Update first piece
            mp = piezas_molde[0]
            mp.cavidades = int(data['cavidades'])
            mp.peso_unitario_gr = float(data['peso_unitario_gr'])
            
            # Tambien actualizar la pieza base para consistencia
            pieza = db.session.get(Pieza, mp.pieza_sku)
            if pieza:
                pieza.peso = mp.peso_unitario_gr
                pieza.cavidad = mp.cavidades
                
        else:
            # No tiene piezas, CREAR la default (copy logic from create)
            pieza_sku = f"{molde.codigo}-STD"
            pieza = db.session.get(Pieza, pieza_sku)
            if not pieza:
                pieza = Pieza(
                    sku=pieza_sku,
                    piezas=f"{molde.nombre} (Std)",
                    peso=float(data['peso_unitario_gr']),
                    cavidad=int(data['cavidades']),
                    tipo='SIMPLE',
                    molde_id=molde.codigo
                )
                db.session.add(pieza)
            
            mp = MoldePieza(
                molde_id=molde.codigo,
                pieza_sku=pieza_sku,
                cavidades=int(data['cavidades']),
                peso_unitario_gr=float(data['peso_unitario_gr'])
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


# ============================================================
# COLORES
# ============================================================

@catalogo_bp.route('/colores', methods=['GET'])
def listar_colores():
    """Lista todos los colores disponibles"""
    colores = ColorProducto.query.order_by(ColorProducto.nombre).all()
    return jsonify([{
        'id': c.id,
        'nombre': c.nombre,
        'codigo': c.codigo
    } for c in colores])


@catalogo_bp.route('/colores', methods=['POST'])
def crear_color():
    """Crea un nuevo color (para create-on-the-fly)"""
    data = request.get_json()
    nombre = data.get('nombre', '').strip().upper()
    
    if not nombre:
        return jsonify({'error': 'Nombre requerido'}), 400
    
    # Verificar si ya existe (case-insensitive)
    existente = ColorProducto.query.filter(
        db.func.upper(ColorProducto.nombre) == nombre
    ).first()
    if existente:
        return jsonify({
            'id': existente.id,
            'nombre': existente.nombre,
            'codigo': existente.codigo,
            'existed': True
        }), 200
    
    # Auto-generar código: obtener el máximo actual y sumar 1
    max_codigo = db.session.query(db.func.max(ColorProducto.codigo)).scalar() or 0
    nuevo_codigo = max_codigo + 1
    
    # Crear nuevo color
    nuevo = ColorProducto(nombre=nombre, codigo=nuevo_codigo)
    db.session.add(nuevo)
    db.session.commit()
    
    return jsonify({
        'id': nuevo.id,
        'nombre': nuevo.nombre,
        'codigo': nuevo.codigo,
        'existed': False
    }), 201


# ============================================================
# CONFIGURACIÓN RÁPIDA DE PRODUCTO (CASCADA)
# ============================================================

@catalogo_bp.route('/configurar-producto', methods=['POST'])
def configurar_producto_cascada():
    """
    Crea Molde + Pieza(s) + ProductoTerminado(s) en una sola transacción.
    
    Payload:
    {
        "molde": {
            "codigo": "MOL-XXX",
            "nombre": "Nombre Molde",
            "peso_tiro_gr": 200,
            "tiempo_ciclo_std": 30,
            "usar_existente": false  // Si true, solo vincula molde existente
        },
        "piezas": [
            {
                "nombre": "Pieza 1",
                "cavidades": 2,
                "peso_unitario_gr": 85,
                "sku_override": null  // Opcional, genera automáticamente si null
            }
        ],
        "color_ids": [1, 2, 3],  // IDs de colores para generar productos
        "generar_productos": true,  // Si crear ProductoTerminado por pieza+color
        "linea": "JUGUETES",
        "cod_linea": 2,
        "familia": "PLAYEROS",
        "cod_familia": 14
    }
    """
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Payload JSON requerido'}), 400
    
    resultado = {
        'molde_creado': None,
        'piezas_creadas': [],
        'productos_creados': [],
        'errores': []
    }
    
    try:
        # 1. CREAR O OBTENER MOLDE
        molde_data = data.get('molde', {})
        molde_codigo = molde_data.get('codigo')
        
        if not molde_codigo:
            return jsonify({'error': 'Código de molde requerido'}), 400
        
        if molde_data.get('usar_existente'):
            molde = db.session.get(Molde, molde_codigo)
            if not molde:
                return jsonify({'error': f'Molde {molde_codigo} no encontrado'}), 404
        else:
            # Verificar si ya existe
            molde = db.session.get(Molde, molde_codigo)
            if molde:
                resultado['errores'].append(f'Molde {molde_codigo} ya existe, usando existente')
            else:
                molde = Molde(
                    codigo=molde_codigo,
                    nombre=molde_data.get('nombre', molde_codigo),
                    peso_tiro_gr=molde_data.get('peso_tiro_gr', 0),
                    tiempo_ciclo_std=molde_data.get('tiempo_ciclo_std', 30.0),
                    activo=True
                )
                db.session.add(molde)
                db.session.flush()  # Para obtener el codigo
                resultado['molde_creado'] = molde.codigo
        
        # 2. CREAR PIEZAS
        piezas_creadas = []
        for idx, pieza_data in enumerate(data.get('piezas', [])):
            nombre_pieza = pieza_data.get('nombre', f'Pieza {idx+1}')
            cavidades = pieza_data.get('cavidades', 1)
            peso_unitario = pieza_data.get('peso_unitario_gr', 0)
            
            # Generar SKU de pieza
            sku_pieza = pieza_data.get('sku_override')
            if not sku_pieza:
                base_sku = molde_codigo.replace('MOL-', '')
                sku_pieza = f"{base_sku}-{idx+1:02d}" if len(data.get('piezas', [])) > 1 else f"{base_sku}-STD"
            
            # Crear pieza
            pieza = db.session.get(Pieza, sku_pieza)
            if pieza:
                resultado['errores'].append(f'Pieza {sku_pieza} ya existe, usando existente')
            else:
                # Buscar Linea por nombre o código
                linea_obj = None
                linea_nombre = data.get('linea')
                cod_linea = data.get('cod_linea')
                if cod_linea:
                    linea_obj = Linea.query.filter_by(codigo=cod_linea).first()
                elif linea_nombre:
                    linea_obj = Linea.query.filter(Linea.nombre.ilike(linea_nombre)).first()
                
                pieza = Pieza(
                    sku=sku_pieza,
                    piezas=nombre_pieza,
                    peso=peso_unitario,
                    cavidad=cavidades,
                    tipo='SIMPLE',
                    molde_id=molde.codigo,
                    linea_id=linea_obj.id if linea_obj else None,  # FK normalizada
                    familia=data.get('familia')
                )
                db.session.add(pieza)
                resultado['piezas_creadas'].append(sku_pieza)
            
            piezas_creadas.append({
                'pieza': pieza,
                'sku': sku_pieza,
                'cavidades': cavidades,
                'peso_unitario': peso_unitario
            })
            
            # Crear relación MoldePieza
            mp_existente = MoldePieza.query.filter_by(
                molde_id=molde.codigo, 
                pieza_sku=sku_pieza
            ).first()
            if not mp_existente:
                mp = MoldePieza(
                    molde_id=molde.codigo,
                    pieza_sku=sku_pieza,
                    cavidades=cavidades,
                    peso_unitario_gr=peso_unitario
                )
                db.session.add(mp)
        
        # 3. CREAR PRODUCTOS TERMINADOS (por cada pieza × color)
        if data.get('generar_productos', True):
            color_ids = data.get('color_ids', [])
            colores = ColorProducto.query.filter(ColorProducto.id.in_(color_ids)).all() if color_ids else []
            
        for pieza_info in piezas_creadas:
                for color in colores:
                    # Buscar Linea por código
                    cod_linea = data.get('cod_linea', 0)
                    linea_obj = Linea.query.filter_by(codigo=cod_linea).first()
                    
                    # Generar SKU del producto
                    linea_code = linea_obj.codigo if linea_obj else cod_linea
                    cod_familia = data.get('cod_familia', 0)
                    cod_producto = hash(pieza_info['sku']) % 1000  # Simplificado
                    sku_pt = f"0{linea_code}{cod_familia}{cod_producto:03d}0{color.codigo}"
                    
                    # Verificar si existe
                    producto = db.session.get(ProductoTerminado, sku_pt)
                    if producto:
                        resultado['errores'].append(f'Producto {sku_pt} ya existe')
                        continue
                    
                    # Obtener la familia del color
                    familia_color_rel = color.familia if hasattr(color, 'familia') else None
                    familia_color_nombre = familia_color_rel.nombre if familia_color_rel else 'SOLIDO'
                    familia_color_codigo = familia_color_rel.codigo if familia_color_rel else 1
                    
                    producto = ProductoTerminado(
                        cod_sku_pt=sku_pt,
                        linea_id=linea_obj.id if linea_obj else None,  # FK normalizada
                        cod_familia=cod_familia,
                        familia=data.get('familia'),
                        cod_producto=cod_producto,
                        producto=f"{pieza_info['pieza'].piezas} - {color.nombre}",
                        cod_familia_color=familia_color_codigo,  # Antes: cod_color
                        familia_color=familia_color_nombre,      # Nombre de la familia
                        familia_color_id=familia_color_rel.id if familia_color_rel else None,  # FK
                        peso_g=pieza_info['peso_unitario'],
                        um='DOC'
                    )
                    db.session.add(producto)
                    
                    # Crear relación ProductoPieza
                    pp = ProductoPieza(
                        producto_terminado_id=sku_pt,
                        pieza_sku=pieza_info['sku'],
                        cantidad=1
                    )
                    db.session.add(pp)
                    
                    resultado['productos_creados'].append({
                        'sku': sku_pt,
                        'nombre': producto.producto,
                        'color': color.nombre
                    })
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'resultado': resultado
        }), 201
        
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
    color_ids_str = request.args.get('color_ids', '')
    
    result = {
        'valid': True,
        'warnings': [],
        'errors': [],
        'molde': None,
        'colores_info': []
    }
    
    if not molde_id:
        return jsonify(result), 200
    
    # Buscar molde
    from app.models.molde import Molde, MoldePieza
    molde = Molde.query.filter_by(codigo=molde_id).first()
    
    if not molde:
        result['errors'].append(f'Molde "{molde_id}" no encontrado')
        result['valid'] = False
        return jsonify(result), 200
    
    # Info del molde
    piezas_rel = MoldePieza.query.filter_by(molde_id=molde.codigo).all()
    piezas_count = len(piezas_rel)
    
    result['molde'] = {
        'codigo': molde.codigo,
        'nombre': molde.nombre,
        'tiene_piezas': piezas_count > 0,
        'piezas_count': piezas_count,
        'tiempo_ciclo_std': molde.tiempo_ciclo_std,
        'peso_tiro_gr': molde.peso_tiro_gr
    }
    
    if piezas_count == 0:
        result['warnings'].append(f'⚠️ Molde "{molde.nombre}" no tiene piezas vinculadas')
    
    # Validar colores si se proporcionaron
    if color_ids_str:
        try:
            color_ids = [int(cid.strip()) for cid in color_ids_str.split(',') if cid.strip()]
        except ValueError:
            color_ids = []
        
        for color_id in color_ids:
            color = ColorProducto.query.get(color_id)
            if not color:
                result['colores_info'].append({
                    'color_id': color_id,
                    'color_nombre': '(desconocido)',
                    'sku_encontrado': None,
                    'sku_exists': False
                })
                result['warnings'].append(f'⚠️ Color ID {color_id} no encontrado')
                continue
            
            # Buscar SKU: ProductoTerminado que tenga la familia_color Y alguna pieza del molde en su BOM
            # NOTA: ProductoTerminado tiene familia_color, no color específico
            # Debemos obtener la familia del ColorProducto para hacer match
            color_familia_id = color.familia_id if color and hasattr(color, 'familia_id') else None
            
            sku_encontrado = None
            for mp in piezas_rel:
                # Buscar productos que contengan esta pieza en su composición
                producto_pieza = ProductoPieza.query.filter_by(pieza_sku=mp.pieza_sku).first()
                if producto_pieza:
                    # Verificar si el producto terminado tiene la misma familia de color
                    prod = producto_pieza.producto_terminado
                    if prod and prod.familia_color_id == color_familia_id:
                        sku_encontrado = prod.cod_sku_pt
                        break
            
            result['colores_info'].append({
                'color_id': color_id,
                'color_nombre': color.nombre,
                'sku_encontrado': sku_encontrado,
                'sku_exists': sku_encontrado is not None
            })
            
            if not sku_encontrado:
                result['warnings'].append(
                    f'⚠️ No existe SKU para color "{color.nombre}" con las piezas de este molde'
                )
    
    # Si hay warnings, marcamos como válido pero con advertencias
    # Solo errors hacen invalid
    result['valid'] = len(result['errors']) == 0
    
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
    Para PIEZAS: Detecta ColorProducto (Rojo, Azul, Verde, etc.)
    """
    from app.services.import_service import ImportService
    from app.models.producto import FamiliaColor, ColorProducto
    
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
        # Para Piezas: Verificar contra ColorProducto (por CÓDIGO)
        colores_existentes = {c.codigo: c.nombre for c in ColorProducto.query.all()}
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
            'opcionales': ['Cod Linea', 'Cod Pieza', 'Cavidad', 'Peso', 'Cod Color', 'Color', '...']
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
        'familia_color': p.familia_color,
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
    
    query = Pieza.query
    
    if estado in ['IMPORTADO', 'EN_REVISION', 'VERIFICADO']:
        query = query.filter(Pieza.estado_revision == estado)
    
    if q:
        search = f"%{q}%"
        query = query.filter(
            or_(
                Pieza.piezas.ilike(search),
                Pieza.sku.ilike(search)
            )
        )
    
    # Nota: filtro por linea eliminado - Pieza.linea ya no existe
    # Si se necesita filtrar por linea, usar join con Linea table
    
    query = query.order_by(Pieza.fecha_importacion.desc().nullsfirst())
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    
    piezas = [{
        'sku': p.sku,
        'piezas': p.piezas,
        'familia': p.familia_rel.nombre if p.familia_rel else None,
        'linea': p.linea_rel.nombre if p.linea_rel else None,
        'color': p.color,
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
def actualizar_revision_pieza(sku):
    """Actualiza el estado de revisión de una pieza."""
    from datetime import datetime
    
    pieza = Pieza.query.get(sku)
    if not pieza:
        return jsonify({'error': f'Pieza {sku} no encontrada'}), 404
    
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
    
    piezas = Pieza.query.filter(Pieza.sku.in_(skus)).all()
    
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
def estadisticas_revision_piezas():
    """Retorna estadísticas de revisión de piezas."""
    from sqlalchemy import func
    
    stats_query = db.session.query(
        Pieza.estado_revision,
        func.count(Pieza.sku)
    ).group_by(Pieza.estado_revision).all()
    
    stats = {'IMPORTADO': 0, 'EN_REVISION': 0, 'VERIFICADO': 0}
    total = 0
    for estado, count in stats_query:
        key = estado if estado else 'IMPORTADO'
        stats[key] = count
        total += count
    
    por_linea = db.session.query(
        Linea.nombre,
        func.count(Pieza.sku)
    ).join(Linea, Pieza.linea_id == Linea.id).filter(
        or_(
            Pieza.estado_revision == 'IMPORTADO',
            Pieza.estado_revision.is_(None)
        )
    ).group_by(Linea.nombre).order_by(
        func.count(Pieza.sku).desc()
    ).limit(5).all()
    
    return jsonify({
        'total': total,
        'por_estado': stats,
        'porcentaje_verificado': round((stats['VERIFICADO'] / total * 100) if total > 0 else 0, 1),
        'pendientes': stats['IMPORTADO'] + stats['EN_REVISION'],
        'por_linea_pendiente': [{'linea': linea or 'Sin Línea', 'cantidad': cant} for linea, cant in por_linea]
    })

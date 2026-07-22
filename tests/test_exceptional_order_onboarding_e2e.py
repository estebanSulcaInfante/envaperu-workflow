"""Flujo desplegable: maestros vacíos -> OP excepcional -> consulta."""

from app.extensions import db
from app.models.maquina import Maquina, TipoMaquina
from app.models.producto import Familia, Linea, LineaFamilia
from app.models.trabajador import RolOperativo, Trabajador


def _created(response):
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def test_creates_all_required_masters_then_queries_exceptional_order(
    client,
    app,
    scm_config,
):
    del scm_config
    with app.app_context():
        Maquina.query.delete()
        TipoMaquina.query.delete()
        LineaFamilia.query.delete()
        Familia.query.delete()
        Linea.query.delete()
        config_role = RolOperativo.query.filter_by(codigo='CONFIGURACION_SCM').one()
        actor = Trabajador(
            codigo='TRB-E2E-CONFIG',
            nombres='E2E',
            apellidos='Configuracion',
            activo=True,
            roles=[config_role],
        )
        db.session.add(actor)
        db.session.commit()
        actor_id = actor.id

    headers = {'X-Actor-Id': str(actor_id)}
    category = _created(client.post(
        '/api/scm/v1/config/categorias-recepcion',
        headers=headers,
        json={
            'nombre': 'Uso OP E2E',
            'modalidad_default': 'POR_CONFIGURAR',
            'lote_externo_obligatorio': False,
            'recepcion_habilitada': False,
            'activo': True,
        },
    ))
    raw_material = _created(client.post(
        '/api/scm/v1/materiales',
        headers=headers,
        json={
            'nombre': 'PP Virgen E2E',
            'clase': 'MATERIA_PRIMA',
            'categoria_recepcion_id': category['id'],
            'unidad_base': 'KG',
            'activo': True,
        },
    ))
    pigment = _created(client.post(
        '/api/scm/v1/materiales',
        headers=headers,
        json={
            'nombre': 'Amarillo E2E',
            'clase': 'COLORANTE',
            'tipo_colorante': 'COLORANTE',
            'categoria_recepcion_id': category['id'],
            'unidad_base': 'KG',
            'activo': True,
        },
    ))

    line = _created(client.post('/api/catalogo/lineas', json={'nombre': 'Hogar E2E'}))
    family = _created(client.post('/api/catalogo/familias', json={'nombre': 'Organizadores E2E'}))
    relation = _created(client.post(
        f"/api/catalogo/lineas/{line['id']}/familias",
        json={'familia_id': family['id']},
    ))
    assert relation['activo'] is True

    color_family = _created(client.post(
        '/api/familias-color',
        json={'nombre': 'SOLIDO E2E'},
    ))
    color = _created(client.post('/api/colores', json={
        'nombre': 'AMARILLO CAJA E2E',
        'familia_color_id': color_family['id'],
        'hex_referencia': '#F2C94C',
    }))
    piece = _created(client.post('/api/piezas', json={
        'nombre': 'Caja organizadora E2E',
        'peso_nominal_gr': 50,
        'linea_id': line['id'],
        'familia_id': family['id'],
    }))
    mold = _created(client.post('/api/moldes', json={
        'nombre': 'Molde caja E2E',
        'peso_tiro_gr': 110,
        'tiempo_ciclo_std': 20,
    }))
    mold_piece = _created(client.post(
        f"/api/moldes/{mold['codigo']}/formas",
        json={
            'pieza_id': piece['id'],
            'cavidades': 2,
            'peso_unitario_gr': 50,
        },
    ))
    variant = _created(client.post(
        f"/api/formas/{mold_piece['id']}/colores",
        json={'color_id': color['id']},
    ))
    product = _created(client.post('/api/productos', json={
        'producto': 'Caja organizadora amarilla E2E',
        'linea_id': line['id'],
        'familia_id': family['id'],
        'piezas': [{'pieza_sku': variant['sku'], 'cantidad': 1}],
    }))
    recipe = _created(client.post('/api/catalogo/recetas-color', json={
        'color_produccion_id': color['id'],
        'producto_sku': product['cod_sku_pt'],
        'nombre_variante': 'Caja amarilla E2E',
        'estado': 'APROBADA',
        'es_default': True,
        'base_virgen_kg': 25,
        'lineas': [
            {
                'material_id': raw_material['id'],
                'tipo_componente': 'MATERIA_PRIMA',
                'cantidad': 1,
            },
            {
                'material_id': pigment['id'],
                'tipo_componente': 'COLORANTE',
                'cantidad': 500,
                'base_kg': 25,
            },
        ],
    }))
    machine_type = _created(client.post('/api/catalogo/tipos-maquina', json={
        'nombre': 'Inyectora E2E',
        'proceso': 'INYECCION',
    }))
    machine = _created(client.post('/api/catalogo/maquinas', json={
        'nombre': 'Máquina E2E',
        'tipo_maquina_id': machine_type['id'],
        'estado': 'OPERATIVA',
        'activo': True,
    }))

    order = _created(client.post('/api/ordenes', json={
        'numero_op': 'OP-E2E-001',
        'maquina_id': machine['id'],
        'producto_sku': product['cod_sku_pt'],
        'molde_id': mold['codigo'],
        'snapshot_tiempo_ciclo': 20,
        'snapshot_horas_turno': 24,
        'snapshot_peso_colada_gr': 10,
        'auto_snapshot_molde': True,
        'snapshot_composicion': [],
        'lotes': [{
            'color_id': color['id'],
            'meta_kg': 10,
            'personas': 1,
            'receta_aplicada': {'id': recipe['id'], 'revision': recipe['revision']},
            'materiales': [{
                'material_id': raw_material['id'],
                'nombre': raw_material['nombre'],
                'tipo': 'VIRGEN',
                'fraccion': 1,
            }],
            'pigmentos': [{
                'material_id': pigment['id'],
                'nombre': pigment['nombre'],
                'gramos': 200,
            }],
        }],
    }))
    assert order['numero_op'] == 'OP-E2E-001'
    assert order['lotes'][0]['color_hex'] == '#F2C94C'
    assert order['lotes'][0]['receta_aplicada'] == {
        'id': recipe['id'],
        'revision': 1,
        'nombre': 'Caja amarilla E2E',
        'base_virgen_kg': 25.0,
    }
    assert len(order['lotes'][0]['salidas']) == 1
    output = order['lotes'][0]['salidas'][0]
    assert output['pieza_id'] == piece['id']
    assert output['pieza_color_sku'] == variant['sku']
    assert output['cantidad_objetivo'] == 200.0
    assert output['kg_objetivo_neto'] == 10.0

    listed = client.get('/api/ordenes')
    assert listed.status_code == 200
    persisted = next(item for item in listed.get_json() if item['numero_op'] == 'OP-E2E-001')
    assert persisted['lotes'][0]['salidas'][0]['pieza_color_sku'] == variant['sku']

"""Integridad del asistente Molde–Pieza–PiezaColor sobre el modelo N:M."""

from app.extensions import db
from app.models.molde import Molde, MoldePieza
from app.models.producto import PiezaColor


def _crear_pieza_clasificada(client, nombre, peso):
    response = client.post(
        '/api/piezas',
        json={
            'nombre': nombre,
            'peso_nominal_gr': peso,
            'linea_id': 1,
            'familia_id': 1,
        },
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def _crear_molde(client, nombre='Molde multipieza'):
    response = client.post(
        '/api/moldes',
        json={'nombre': nombre, 'peso_tiro_gr': 250, 'tiempo_ciclo_std': 30},
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def _asociar(client, molde_codigo, pieza, cavidades=1):
    response = client.post(
        f'/api/moldes/{molde_codigo}/formas',
        json={
            'pieza_id': pieza['id'],
            'cavidades': cavidades,
            'peso_unitario_gr': pieza['peso_nominal_gr'],
        },
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def _crear_color(client, nombre='AZUL ASISTENTE'):
    response = client.post('/api/colores', json={'nombre': nombre})
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def test_molde_existente_genera_variantes_para_toda_su_composicion(client, app):
    piezas = [
        _crear_pieza_clasificada(client, 'Cuerpo asistente', 100),
        _crear_pieza_clasificada(client, 'Tapa asistente', 20),
        _crear_pieza_clasificada(client, 'Asa asistente', 8),
    ]
    molde = _crear_molde(client)
    relaciones = [
        _asociar(client, molde['codigo'], pieza, index + 1)
        for index, pieza in enumerate(piezas)
    ]
    color = _crear_color(client)

    response = client.post(
        '/api/configurar-producto',
        json={
            'linea_id': 1,
            'familia_id': 1,
            'molde': {'usar_existente': True, 'codigo': molde['codigo']},
            # El formulario puede mencionar una sola asociación. No debe
            # interpretarse como un recorte de la composición ya persistida.
            'formas': [{
                'pieza_id': piezas[0]['id'],
                'cavidades': 1,
                'peso_unitario_gr': piezas[0]['peso_nominal_gr'],
            }],
            'color_ids': [color['id']],
        },
    )

    assert response.status_code == 201, response.get_json()
    result = response.get_json()['resultado']
    assert result['molde_reutilizado'] == molde['codigo']
    assert len(result['composicion_molde']) == 3
    assert result['asociaciones_creadas'] == []
    assert len(result['asociaciones_reutilizadas']) == 3
    assert result['piezas_maestras_creadas'] == []
    assert len(result['piezas_maestras_reutilizadas']) == 3
    assert set(result['variantes_creadas']) == set(result['piezas_creadas'])
    assert len(result['variantes_creadas']) == 3

    with app.app_context():
        assert MoldePieza.query.filter_by(
            molde_id=molde['codigo'],
            activo=True,
        ).count() == 3
        assert {item.id for item in MoldePieza.query.filter_by(
            molde_id=molde['codigo'],
            activo=True,
        ).all()} == {item['id'] for item in relaciones}
        assert PiezaColor.query.filter_by(
            color_produccion_id=color['id'],
        ).count() == 3


def test_habilitar_color_en_molde_es_atomico_para_todas_las_salidas(client, app):
    piezas = [
        _crear_pieza_clasificada(client, 'Cuerpo agrupado', 90),
        _crear_pieza_clasificada(client, 'Tapa agrupada', 18),
        _crear_pieza_clasificada(client, 'Pico agrupado', 12),
    ]
    molde = _crear_molde(client, 'Molde agrupado')
    relaciones = [_asociar(client, molde['codigo'], pieza) for pieza in piezas]
    color = _crear_color(client, 'VERDE AGRUPADO')

    response = client.post(
        f"/api/moldes/{molde['codigo']}/colores",
        json={'color_id': color['id']},
    )

    assert response.status_code == 201, response.get_json()
    assert len(response.get_json()['variantes_creadas']) == 3
    assert len(response.get_json()['variantes']) == 3
    assert {item['pieza_id'] for item in response.get_json()['variantes']} == {
        pieza['id'] for pieza in piezas
    }

    repeated = client.post(
        f"/api/formas/{relaciones[0]['id']}/colores",
        json={'color_id': color['id']},
    )
    assert repeated.status_code == 200, repeated.get_json()
    assert repeated.get_json()['existed'] is True
    assert len(repeated.get_json()['variantes_reutilizadas']) == 3

    with app.app_context():
        assert PiezaColor.query.filter_by(
            color_produccion_id=color['id'],
        ).count() == 3


def test_rechaza_color_inexistente_sin_crear_catalogos_parciales(client, app):
    response = client.post(
        '/api/configurar-producto',
        json={
            'linea_id': 1,
            'familia_id': 1,
            'molde': {'nombre': 'No debe persistir', 'peso_tiro_gr': 100},
            'formas': [{
                'nombre': 'Pieza temporal',
                'cavidades': 1,
                'peso_unitario_gr': 50,
            }],
            'color_ids': [999999],
        },
    )

    assert response.status_code == 400
    assert response.get_json()['codigo'] == 'COLOR_NO_ENCONTRADO'
    with app.app_context():
        assert Molde.query.filter_by(nombre='No debe persistir').count() == 0


def test_kit_legacy_y_molde_nuevo_sin_composicion_se_rechazan(client):
    kit_sin_color = client.post(
        '/api/configurar-producto',
        json={
            'linea_id': 1,
            'familia_id': 1,
            'molde': {'nombre': 'Kit inválido', 'peso_tiro_gr': 100},
            'formas': [
                {'nombre': 'Base kit', 'cavidades': 1, 'peso_unitario_gr': 40},
                {'nombre': 'Tapa kit', 'cavidades': 1, 'peso_unitario_gr': 10},
            ],
            'kit': {'nombre': 'Kit sin color'},
            'color_ids': [],
        },
    )
    assert kit_sin_color.status_code == 422
    assert (
        kit_sin_color.get_json()['codigo']
        == 'LEGACY_KIT_NOT_SUPPORTED'
    )

    sin_composicion = client.post(
        '/api/configurar-producto',
        json={
            'linea_id': 1,
            'familia_id': 1,
            'molde': {'nombre': 'Molde vacío', 'peso_tiro_gr': 100},
            'formas': [],
        },
    )
    assert sin_composicion.status_code == 400
    assert sin_composicion.get_json()['codigo'] == 'COMPOSICION_REQUERIDA'


def test_variante_permite_pieza_sin_clasificacion_tecnica(client, app):
    pieza_response = client.post(
        '/api/piezas',
        json={'nombre': 'Pieza legacy sin clasificar', 'peso_nominal_gr': 12},
    )
    assert pieza_response.status_code == 201
    molde = _crear_molde(client, 'Molde legacy')
    relacion = _asociar(client, molde['codigo'], pieza_response.get_json())
    color = _crear_color(client, 'ROJO CLASIFICACION')

    response = client.post(
        f"/api/formas/{relacion['id']}/colores",
        json={'color_id': color['id']},
    )

    assert response.status_code == 201, response.get_json()
    with app.app_context():
        variant = db.session.get(PiezaColor, response.get_json()['sku'])
        assert variant.linea_id is None
        assert variant.familia_id is None

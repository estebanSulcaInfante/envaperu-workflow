from app.extensions import db
from app.models.producto import Familia, Linea, LineaFamilia, PiezaColor


def _crear_linea(client, codigo, nombre):
    response = client.post(
        '/api/catalogo/lineas',
        json={'codigo': codigo, 'nombre': nombre},
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def _crear_familia(client, codigo, nombre):
    response = client.post(
        '/api/catalogo/familias',
        json={'codigo': codigo, 'nombre': nombre},
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def _asociar(client, linea_id, familia_id):
    response = client.post(
        f'/api/catalogo/lineas/{linea_id}/familias',
        json={'familia_id': familia_id},
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def test_crud_maestros_y_filtro_por_relacion(client):
    linea = _crear_linea(client, 20, 'Agricola')
    familia = _crear_familia(client, 30, 'Cosecha')

    sin_asociar = client.get(
        f"/api/catalogo/familias?linea_id={linea['id']}"
    )
    assert sin_asociar.status_code == 200
    assert sin_asociar.get_json() == []

    asociacion = _asociar(client, linea['id'], familia['id'])
    assert asociacion['linea_id'] == linea['id']
    assert asociacion['familia']['id'] == familia['id']
    assert asociacion['activo'] is True

    filtradas = client.get(
        f"/api/catalogo/familias?linea_id={linea['id']}"
    ).get_json()
    assert [item['id'] for item in filtradas] == [familia['id']]

    actualizada = client.put(
        f"/api/catalogo/lineas/{linea['id']}",
        json={
            'codigo': 20,
            'nombre': 'Agricola Norte',
            'activo': True,
            'version': linea['version'],
        },
    )
    assert actualizada.status_code == 200, actualizada.get_json()
    assert actualizada.get_json()['version'] == 2

    codigo_inmutable = client.put(
        f"/api/catalogo/lineas/{linea['id']}",
        json={'codigo': 21, 'version': 2},
    )
    assert codigo_inmutable.status_code == 400
    assert codigo_inmutable.get_json()['codigo'] == 'CODIGO_INMUTABLE'

    conflicto = client.put(
        f"/api/catalogo/lineas/{linea['id']}",
        json={'nombre': 'Cambio obsoleto', 'version': 1},
    )
    assert conflicto.status_code == 409
    assert conflicto.get_json()['codigo'] == 'VERSION_CONFLICT'


def test_asociacion_se_desactiva_y_reactiva_sin_borrado_fisico(client, app):
    linea = _crear_linea(client, 40, 'Industrial pesada')
    familia = _crear_familia(client, 41, 'Contenedores')
    asociacion = _asociar(client, linea['id'], familia['id'])

    eliminada = client.delete(
        f"/api/catalogo/lineas/{linea['id']}/familias/{familia['id']}"
    )
    assert eliminada.status_code == 200, eliminada.get_json()
    assert eliminada.get_json()['activo'] is False
    assert eliminada.get_json()['version'] == asociacion['version'] + 1

    reactivada = client.post(
        f"/api/catalogo/lineas/{linea['id']}/familias",
        json={'familia_id': familia['id']},
    )
    assert reactivada.status_code == 200, reactivada.get_json()
    assert reactivada.get_json()['activo'] is True
    assert reactivada.get_json()['version'] == asociacion['version'] + 2

    with app.app_context():
        assert LineaFamilia.query.filter_by(
            linea_id=linea['id'],
            familia_id=familia['id'],
        ).count() == 1


def test_consumidores_rechazan_par_no_asociado_y_bloquean_baja(client):
    linea = _crear_linea(client, 50, 'Promocional')
    familia = _crear_familia(client, 51, 'Souvenirs')

    invalido = client.post(
        '/api/productos',
        json={
            'producto': 'Vaso promocional',
            'linea_id': linea['id'],
            'familia_id': familia['id'],
        },
    )
    assert invalido.status_code == 409
    assert invalido.get_json()['codigo'] == 'LINEA_FAMILIA_NO_ASOCIADA'

    _asociar(client, linea['id'], familia['id'])
    valido = client.post(
        '/api/productos',
        json={
            'producto': 'Vaso promocional',
            'linea_id': linea['id'],
            'familia_id': familia['id'],
        },
    )
    assert valido.status_code == 201, valido.get_json()

    desasociar = client.delete(
        f"/api/catalogo/lineas/{linea['id']}/familias/{familia['id']}"
    )
    assert desasociar.status_code == 409
    assert desasociar.get_json()['codigo'] == 'LINEA_FAMILIA_EN_USO'
    assert desasociar.get_json()['uso']['productos'] == 1

    inactivar = client.delete(
        f"/api/catalogo/familias/{familia['id']}?version={familia['version']}"
    )
    assert inactivar.status_code == 409
    assert inactivar.get_json()['codigo'] == 'CATALOGO_EN_USO'


def test_variante_deriva_clasificacion_de_pieza_y_no_puede_divergir(client):
    linea = _crear_linea(client, 60, 'Hogar')
    familia = _crear_familia(client, 61, 'Jarras')
    _asociar(client, linea['id'], familia['id'])
    pieza = client.post(
        '/api/piezas',
        json={
            'nombre': 'Cuerpo de jarra',
            'peso_nominal_gr': 150,
            'linea_id': linea['id'],
            'familia_id': familia['id'],
        },
    )
    assert pieza.status_code == 201, pieza.get_json()

    variante = client.post(
        '/api/piezas-color',
        json={'nombre': 'Cuerpo azul', 'pieza_id': pieza.get_json()['id']},
    )
    assert variante.status_code == 201, variante.get_json()
    sku = variante.get_json()['sku']
    detalle = client.get(f'/api/piezas-color/{sku}').get_json()
    assert (detalle['linea_id'], detalle['familia_id']) == (
        linea['id'],
        familia['id'],
    )

    otra_linea = _crear_linea(client, 62, 'Exportación industrial')
    otra_familia = _crear_familia(client, 63, 'Bidones')
    _asociar(client, otra_linea['id'], otra_familia['id'])
    divergente = client.put(
        f'/api/piezas-color/{sku}',
        json={
            'linea_id': otra_linea['id'],
            'familia_id': otra_familia['id'],
        },
    )
    assert divergente.status_code == 409
    assert divergente.get_json()['codigo'] == 'CLASIFICACION_PIEZA_DIVERGENTE'


def test_baja_logica_exige_version_y_listado_puede_incluir_inactivos(client):
    linea = _crear_linea(client, 70, 'Temporal')

    sin_version = client.delete(f"/api/catalogo/lineas/{linea['id']}")
    assert sin_version.status_code == 400
    assert sin_version.get_json()['codigo'] == 'VERSION_REQUIRED'

    eliminada = client.delete(
        f"/api/catalogo/lineas/{linea['id']}?version={linea['version']}"
    )
    assert eliminada.status_code == 200, eliminada.get_json()
    assert eliminada.get_json()['activo'] is False

    activos = client.get('/api/catalogo/lineas').get_json()
    assert linea['id'] not in {item['id'] for item in activos}
    todos = client.get('/api/catalogo/lineas?include_inactive=true').get_json()
    assert linea['id'] in {item['id'] for item in todos}


def test_nombre_y_codigo_son_unicos_en_el_crud(client):
    linea = _crear_linea(client, 80, 'Exportacion')
    mismo_codigo = client.post(
        '/api/catalogo/lineas',
        json={'codigo': 80, 'nombre': 'Otra'},
    )
    assert mismo_codigo.status_code == 409
    assert mismo_codigo.get_json()['codigo'] == 'CATALOGO_DUPLICADO'

    mismo_nombre = client.post(
        '/api/catalogo/lineas',
        json={'codigo': 81, 'nombre': linea['nombre'].lower()},
    )
    assert mismo_nombre.status_code == 409
    assert mismo_nombre.get_json()['codigo'] == 'CATALOGO_DUPLICADO'


def test_modelos_conservan_un_solo_par_activo(app):
    with app.app_context():
        linea = Linea.query.filter_by(codigo=1).one()
        familia = Familia.query.filter_by(codigo=1).one()
        relacion = LineaFamilia.query.filter_by(
            linea_id=linea.id,
            familia_id=familia.id,
        ).one()
        assert relacion.activo is True
        assert PiezaColor.query.count() == 0
        assert db.session.get(LineaFamilia, relacion.id) is relacion


def test_alta_en_contexto_crea_familia_y_asociacion_en_una_transaccion(client, app):
    linea = _crear_linea(client, 90, 'Limpieza')

    response = client.post(
        f"/api/catalogo/lineas/{linea['id']}/familias",
        json={
            'familia': {
                'codigo': 901,
                'nombre': 'Recogedores',
            },
        },
    )

    assert response.status_code == 201, response.get_json()
    payload = response.get_json()
    assert payload['linea_id'] == linea['id']
    assert payload['activo'] is True
    assert payload['familia']['codigo'] == 901
    assert payload['familia']['nombre'] == 'Recogedores'

    with app.app_context():
        familia = Familia.query.filter_by(codigo=901).one()
        assert LineaFamilia.query.filter_by(
            linea_id=linea['id'],
            familia_id=familia.id,
            activo=True,
        ).count() == 1


def test_alta_contextual_reactiva_familia_existente_sin_duplicarla(client, app):
    linea = _crear_linea(client, 92, 'Cocina')
    familia = _crear_familia(client, 904, 'Escurridores')
    inactiva = client.delete(
        f"/api/catalogo/familias/{familia['id']}?version={familia['version']}"
    )
    assert inactiva.status_code == 200

    response = client.post(
        f"/api/catalogo/lineas/{linea['id']}/familias",
        json={
            'familia': {
                'codigo': familia['codigo'],
                'nombre': familia['nombre'],
            },
        },
    )

    assert response.status_code == 201, response.get_json()
    payload = response.get_json()
    assert payload['familia']['id'] == familia['id']
    assert payload['familia']['activo'] is True
    assert payload['familia']['version'] == (
        inactiva.get_json()['version'] + 1
    )
    with app.app_context():
        assert Familia.query.filter_by(nombre='Escurridores').count() == 1
        assert LineaFamilia.query.filter_by(
            linea_id=linea['id'],
            familia_id=familia['id'],
            activo=True,
        ).count() == 1


def test_asociacion_contextual_por_id_reactiva_familia_inactiva(client, app):
    linea = _crear_linea(client, 93, 'Organizacion')
    familia = _crear_familia(client, 905, 'Canastas')
    inactiva = client.delete(
        f"/api/catalogo/familias/{familia['id']}?version={familia['version']}"
    )
    assert inactiva.status_code == 200

    response = client.post(
        f"/api/catalogo/lineas/{linea['id']}/familias",
        json={'familia_id': familia['id']},
    )

    assert response.status_code == 201, response.get_json()
    assert response.get_json()['familia']['id'] == familia['id']
    assert response.get_json()['familia']['activo'] is True
    with app.app_context():
        assert Familia.query.filter_by(id=familia['id']).count() == 1
        assert LineaFamilia.query.filter_by(
            linea_id=linea['id'],
            familia_id=familia['id'],
            activo=True,
        ).count() == 1


def test_alta_en_contexto_no_deja_familia_huerfana_si_linea_no_existe(client, app):
    response = client.post(
        '/api/catalogo/lineas/999999/familias',
        json={
            'familia': {
                'codigo': 902,
                'nombre': 'No debe persistir',
            },
        },
    )

    assert response.status_code == 404
    with app.app_context():
        assert Familia.query.filter_by(codigo=902).count() == 0
        assert Familia.query.filter_by(nombre='No debe persistir').count() == 0


def test_alta_generica_de_familia_no_inventa_asociacion_con_linea(client, app):
    linea = _crear_linea(client, 91, 'Escolar')

    familia = _crear_familia(client, 903, 'Loncheras')

    with app.app_context():
        assert Familia.query.filter_by(id=familia['id']).count() == 1
        assert LineaFamilia.query.filter_by(
            linea_id=linea['id'],
            familia_id=familia['id'],
        ).count() == 0

    contextual = client.post(
        f"/api/catalogo/lineas/{linea['id']}/familias",
        json={'familia_id': familia['id']},
    )
    assert contextual.status_code == 201, contextual.get_json()
    assert contextual.get_json()['familia']['id'] == familia['id']

from app.extensions import db
from app.models.maquina import TipoMaquina
from app.models.scm_catalogos import ScmCapacidad
from app.models.trabajador import Trabajador


def _admin_headers(app):
    with app.app_context():
        actor = Trabajador.query.filter_by(codigo='TRB-01').one()
        capability = ScmCapacidad(
            codigo='AUTORIZACION_SCM_ADMINISTRAR',
            nombre='Administrar autorizaciones SCM',
        )
        actor.roles[0].capacidades.append(capability)
        db.session.add(capability)
        db.session.commit()
        return {'X-Actor-Id': str(actor.id)}


def test_trabajador_code_is_automatic_and_immutable(client, app):
    headers = _admin_headers(app)
    created = client.post('/api/catalogo/trabajadores', json={
        'nombres': 'Ana',
        'apellidos': 'Prueba',
    }, headers=headers)
    assert created.status_code == 201, created.get_json()
    worker = created.get_json()
    assert worker['codigo'] == 'TRB-000001'

    manual = client.post('/api/catalogo/trabajadores', headers=headers, json={
        'codigo': 'TRB-MANUAL',
        'nombres': 'Manual',
        'apellidos': 'No permitido',
    })
    assert manual.status_code == 400
    assert manual.get_json()['codigo'] == 'CODIGO_MANUAL_NO_PERMITIDO'

    changed = client.put(
        f"/api/catalogo/trabajadores/{worker['id']}",
        json={'codigo': 'TRB-999999'},
        headers=headers,
    )
    assert changed.status_code == 400
    assert changed.get_json()['codigo'] == 'CODIGO_INMUTABLE'


def test_maquina_code_is_automatic_and_immutable(client, app):
    with app.app_context():
        machine_type = TipoMaquina(
            codigo='INY',
            nombre='Inyectora prueba',
            proceso='INYECCION',
        )
        db.session.add(machine_type)
        db.session.commit()
        type_id = machine_type.id

    created = client.post('/api/catalogo/maquinas', json={
        'nombre': 'Inyectora 1',
        'tipo_maquina_id': type_id,
    })
    assert created.status_code == 201, created.get_json()
    machine = created.get_json()
    assert machine['codigo'] == 'MAQ-000001'

    manual = client.post('/api/catalogo/maquinas', json={
        'codigo': 'MAQ-MANUAL',
        'nombre': 'Manual',
        'tipo_maquina_id': type_id,
    })
    assert manual.status_code == 400
    assert manual.get_json()['codigo'] == 'CODIGO_MANUAL_NO_PERMITIDO'

    changed = client.put(
        f"/api/catalogo/maquinas/{machine['id']}",
        json={'codigo': 'MAQ-999999'},
    )
    assert changed.status_code == 400
    assert changed.get_json()['codigo'] == 'CODIGO_INMUTABLE'


def test_tipo_maquina_has_full_versioned_crud(client):
    created = client.post('/api/catalogo/tipos-maquina', json={
        'nombre': 'Sopladora de prueba',
        'proceso': 'SOPLADO',
        'fabricante': 'Prueba',
    })
    assert created.status_code == 201, created.get_json()
    machine_type = created.get_json()
    assert machine_type['codigo'] == 'TMQ-000001'
    assert machine_type['version'] == 1

    manual = client.post('/api/catalogo/tipos-maquina', json={
        'codigo': 'TMQ-MANUAL',
        'nombre': 'Manual',
    })
    assert manual.status_code == 400
    assert manual.get_json()['codigo'] == 'CODIGO_MANUAL_NO_PERMITIDO'

    changed = client.put(
        f"/api/catalogo/tipos-maquina/{machine_type['id']}",
        json={
            'version': machine_type['version'],
            'nombre': 'Sopladora actualizada',
        },
    )
    assert changed.status_code == 200, changed.get_json()
    updated = changed.get_json()
    assert updated['codigo'] == machine_type['codigo']
    assert updated['nombre'] == 'Sopladora actualizada'
    assert updated['version'] == 2

    stale = client.put(
        f"/api/catalogo/tipos-maquina/{machine_type['id']}",
        json={'version': 1, 'nombre': 'Edición obsoleta'},
    )
    assert stale.status_code == 409
    assert stale.get_json()['codigo'] == 'VERSION_CONFLICT'

    deactivated = client.delete(
        f"/api/catalogo/tipos-maquina/{machine_type['id']}?version=2"
    )
    assert deactivated.status_code == 200, deactivated.get_json()
    inactive = deactivated.get_json()
    assert inactive['activo'] is False
    assert inactive['version'] == 3

    visible = client.get('/api/catalogo/tipos-maquina').get_json()
    assert all(item['id'] != machine_type['id'] for item in visible)
    all_items = client.get(
        '/api/catalogo/tipos-maquina?include_inactive=true'
    ).get_json()
    assert any(item['id'] == machine_type['id'] for item in all_items)

    reactivated = client.put(
        f"/api/catalogo/tipos-maquina/{machine_type['id']}",
        json={'version': 3, 'activo': True},
    )
    assert reactivated.status_code == 200
    assert reactivated.get_json()['activo'] is True


def test_tipo_maquina_cannot_be_deactivated_while_used(client, app):
    created_type = client.post('/api/catalogo/tipos-maquina', json={
        'nombre': 'Inyectora usada',
    }).get_json()
    created_machine = client.post('/api/catalogo/maquinas', json={
        'nombre': 'Máquina vinculada',
        'tipo_maquina_id': created_type['id'],
    })
    assert created_machine.status_code == 201, created_machine.get_json()

    blocked = client.delete(
        f"/api/catalogo/tipos-maquina/{created_type['id']}?version=1"
    )
    assert blocked.status_code == 409
    assert blocked.get_json()['codigo'] == 'TIPO_MAQUINA_EN_USO'

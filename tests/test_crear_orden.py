import pytest
from app.models.orden import OrdenProduccion, SnapshotComposicionMolde
from app.models.lote import LoteColor
from app.models.materiales import MateriaPrima, Colorante
from app.models.molde import Molde, MoldePieza, Pieza
from app.models.producto import PiezaColor, Linea, Familia
from app.extensions import db
from app.services.scm_material_service import create_colorante_with_scm, create_materia_prima_with_scm

def _get_or_create_fam(nombre="SOLIDO", codigo=1):
    from app.models.producto import FamiliaColor
    fam = FamiliaColor.query.filter_by(nombre=nombre).first()
    if not fam:
        from app.extensions import db
        fam = FamiliaColor(nombre=nombre, codigo=codigo)
        db.session.add(fam)
        db.session.flush()
    return fam

def _create_color_prod(nombre, codigo=None, familia_id=None):
    from app.models.producto import ColorBase, ColorProduccion
    from app.extensions import db
    cb = ColorBase.query.filter_by(nombre=nombre).first()
    if not cb:
        cb = ColorBase(nombre=nombre)
        db.session.add(cb)
        db.session.flush()
    fam_id = familia_id if familia_id else _get_or_create_fam().id
    cp = ColorProduccion(color_base_id=cb.id, familia_color_id=fam_id, codigo_legacy=codigo)
    db.session.add(cp)
    db.session.flush()
    return cp



def _setup_molde(app):
    """Helper: crea un molde simple con 1 pieza, 2 cavidades.
    Reutiliza Linea y Familia creadas por conftest (codigo=1).
    """
    with app.app_context():
        # Reuse existing Linea/Familia from conftest — no crear duplicados
        linea = Linea.query.first()
        familia = Familia.query.first()

        molde = Molde(codigo="MOL-TEST", nombre="Molde Test", peso_tiro_gr=22.0, tiempo_ciclo_std=10.0)
        db.session.add(molde)
        db.session.flush()

        pieza_global = Pieza(
            codigo="PZ-ORDEN-TEST",
            nombre="Pieza Test",
            linea_id=linea.id,
            familia_id=familia.id,
            peso_nominal_gr=10.0,
        )
        db.session.add(pieza_global)
        db.session.flush()

        pieza = PiezaColor(sku="PIEZA-TEST", cod_pieza=101, piezas="PiezaColor Test",
                      linea_id=linea.id, familia_id=familia.id,
                      cavidad=2, peso=10.0, pieza_id=pieza_global.id)
        mp_rel = MoldePieza(
            molde=molde,
            pieza=pieza_global,
            cavidades=2,
            peso_unitario_gr=10.0,
        )
        db.session.add_all([pieza, mp_rel])
        db.session.commit()
        return {
            "pieza_id": pieza_global.id,
            "pieza_codigo": pieza_global.codigo,
            "pieza_nombre": pieza_global.nombre,
        }


def test_crear_orden_manual_snapshot(client, app, scm_config):
    """
    Crea una OP usando snapshot_composicion manual (sin auto_snapshot_molde).
    Verifica que la composición se persiste correctamente.
    """
    with app.app_context():
        from app.models.producto import ColorProduccion, ColorBase, FamiliaColor, FamiliaColor
        fam = FamiliaColor(nombre="STD")
        db.session.add(fam)
        db.session.flush()
        c_prod = _create_color_prod(nombre="ROJO", codigo=10, familia_id=fam.id)
        db.session.add(c_prod)
        db.session.commit()
        c_prod_id = c_prod.id

    payload = {
        "numero_op": "OP-MANUAL-SNAP",
        "maquina_id": 1,
        "producto": "PiezaColor Simple",
        "snapshot_tiempo_ciclo": 10.0,
        "snapshot_horas_turno": 8.0,
        "snapshot_peso_colada_gr": 2.0,

        # Modo manual: composición explícita
        "snapshot_composicion": [
            {"pieza_sku": None, "cavidades": 2, "peso_unit_gr": 10.0}
        ],

        "lotes": [
            {
                "color_id": c_prod_id,
                "meta_kg": 100.0,
                "personas": 3,
                "materiales": [{"nombre": "TEST MP", "tipo": "VIRGEN", "fraccion": 1.0}],
                "pigmentos": [{"nombre": "TEST COL", "gramos": 15.0}]
            }
        ]
    }

    response = client.post('/api/ordenes', json=payload)
    if response.status_code != 201:
        print(f"\nResponse Error: {response.get_json()}")

    assert response.status_code == 201
    data = response.get_json()
    assert data['numero_op'] == "OP-MANUAL-SNAP"
    assert len(data['lotes']) == 1

    lote = data['lotes'][0]
    assert lote['Color'] == "ROJO STD"
    assert lote['mano_obra']['personas'] == 3
    assert len(lote['materiales']) == 1
    assert lote['materiales'][0]['fraccion'] == 1.0

    # Verificar snapshot técnico
    snap = data['snapshot_tecnico']
    assert snap['cavidades_totales'] == 2
    assert snap['peso_neto_golpe_gr'] == 20.0   # 2 cav × 10g
    assert snap['peso_tiro_gr'] == 22.0          # 20g + 2g colada
    assert snap['es_multipieza'] == False

    # Verificar persistencia en BD
    with app.app_context():
        orden_db = OrdenProduccion.query.filter_by(numero_op="OP-MANUAL-SNAP").first()
        assert orden_db is not None
        assert len(orden_db.snapshot_composicion) == 1
        assert orden_db.snapshot_composicion[0].cavidades == 2
        assert orden_db.snapshot_composicion[0].peso_unit_gr == 10.0
        assert len(orden_db.lotes) == 1
        lote_db = orden_db.lotes[0]
        assert len(lote_db.materias_primas) == 1
        assert len(lote_db.colorantes) == 1
        materia = MateriaPrima.query.filter_by(nombre="TEST MP").one()
        colorante = Colorante.query.filter_by(nombre="TEST COL").one()
        assert materia.scm_material is not None
        assert materia.scm_material.codigo.startswith("MP-AUTO-")
        assert (
            materia.scm_material.categoria_recepcion.codigo
            == "LEGACY_POR_CONFIGURAR"
        )
        assert colorante.scm_material is not None
        assert colorante.scm_material.codigo.startswith("COL-AUTO-")
        assert (
            colorante.scm_material.categoria_recepcion.codigo
            == "LEGACY_POR_CONFIGURAR"
        )


def test_crear_orden_usa_identidades_scm_seleccionadas(client, app, scm_config):
    with app.app_context():
        family = _get_or_create_fam(nombre="SCM OP", codigo=902)
        color = _create_color_prod(nombre="AZUL SCM", familia_id=family.id)
        raw = create_materia_prima_with_scm(
            session=db.session,
            nombre="PP VIRGEN IDENTIDAD SCM",
            tipo="VIRGEN",
            categoria_codigo="RESINA_VIRGEN",
            codigo_scm="MP-OP-SCM-001",
        )
        pigment = create_colorante_with_scm(
            session=db.session,
            nombre="AZUL IDENTIDAD SCM",
            codigo_scm="COL-OP-SCM-001",
        )
        db.session.commit()
        color_id = color.id
        raw_material_id = raw.scm_material.id
        pigment_material_id = pigment.scm_material.id
        raw_legacy_id = raw.id
        pigment_legacy_id = pigment.id

    response = client.post('/api/ordenes', json={
        "numero_op": "OP-SCM-IDENTIDADES",
        "maquina_id": 1,
        "snapshot_tiempo_ciclo": 10,
        "snapshot_horas_turno": 8,
        "snapshot_peso_colada_gr": 2,
        "snapshot_composicion": [{"pieza_sku": None, "cavidades": 1, "peso_unit_gr": 10}],
        "lotes": [{
            "color_id": color_id,
            "meta_kg": 25,
            "personas": 1,
            "materiales": [{"material_id": raw_material_id, "nombre": "nombre ignorado", "tipo": "VIRGEN", "fraccion": 1}],
            "pigmentos": [{"material_id": pigment_material_id, "nombre": "nombre ignorado", "gramos": 500}],
        }],
    })
    assert response.status_code == 201, response.get_json()
    with app.app_context():
        lot = OrdenProduccion.query.filter_by(numero_op="OP-SCM-IDENTIDADES").one().lotes[0]
        assert lot.materias_primas[0].id == raw_legacy_id
        assert lot.colorantes[0].id == pigment_legacy_id


def test_crear_orden_auto_snapshot(client, app):
    """
    Crea una OP con auto_snapshot_molde:true.
    Verifica que la composición se deriva desde Pieza del catálogo.
    """
    catalog = _setup_molde(app)

    payload = {
        "numero_op": "OP-AUTO-SNAP",
        "maquina_id": 1,
        "molde_id": "MOL-TEST",
        "snapshot_tiempo_ciclo": 10.0,
        "snapshot_horas_turno": 8.0,
        "snapshot_peso_colada_gr": 2.0,

        "auto_snapshot_molde": True,
        "lotes": []
    }

    response = client.post('/api/ordenes', json=payload)
    if response.status_code != 201:
        print(f"\nResponse Error: {response.get_json()}")

    assert response.status_code == 201
    data = response.get_json()

    snap = data['snapshot_tecnico']
    assert snap['cavidades_totales'] == 2
    assert snap['peso_neto_golpe_gr'] == 20.0    # 2 × 10g
    assert snap['peso_tiro_gr'] == 22.0           # 20 + 2 colada
    assert len(snap['composicion']) == 1
    assert snap['composicion'][0]['pieza_id'] == catalog["pieza_id"]
    assert snap['composicion'][0]['pieza_codigo_snapshot'] == catalog["pieza_codigo"]
    assert snap['composicion'][0]['pieza_nombre_snapshot'] == catalog["pieza_nombre"]
    assert snap['composicion'][0]['pieza_sku_legacy'] is None
    assert 'pieza_sku' not in snap['composicion'][0]

    # Verificar que el catálogo ya no afecta el snapshot congelado
    with app.app_context():
        mp_row = MoldePieza.query.filter_by(molde_id="MOL-TEST", activo=True).one()
        mp_row.peso_unitario_gr = 99.0  # Cambiamos el catálogo
        db.session.commit()

        orden_db = OrdenProduccion.query.filter_by(numero_op="OP-AUTO-SNAP").first()
        # El snapshot sigue en 10g, no en 99g
        assert orden_db.snapshot_composicion[0].peso_unit_gr == 10.0

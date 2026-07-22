import pytest
from app.models.molde import Molde, MoldePieza, Pieza
from app.models.producto import PiezaColor, ProductoPieza, ProductoTerminado
from app.models.orden import OrdenProduccion, SnapshotComposicionMolde
from app.models.maquina import Maquina
import json

@pytest.fixture
def session(app):
    from app import db
    return db.session

@pytest.fixture
def mock_data(session):
    from app.models.producto import Linea, Familia
    
    linea = session.query(Linea).first()
    if not linea:
        linea = Linea(codigo=99, nombre='TEST_LINEA')
        session.add(linea)
        
    familia = session.query(Familia).first()
    if not familia:
        familia = Familia(codigo=99, nombre='TEST_FAMILIA')
        session.add(familia)
    
    session.flush()

    maq = Maquina(codigo="INJ-01", nombre="INJ-01", tipo_maquina_id=1, tipo="Inyectora")
    session.add(maq)

    # Simple
    molde_balde = Molde(codigo="MLB01", nombre="Molde Balde Playero", peso_tiro_gr=150.0)
    prod_balde = ProductoTerminado(cod_sku_pt="PT-BLD", producto="Balde Romano", linea_id=linea.id, familia_id=familia.id)
    pieza_balde = Pieza(codigo="PZ-BALDE", nombre="PiezaColor Balde", linea_id=linea.id, familia_id=familia.id, peso_nominal_gr=145.0)
    molde_pieza_balde = MoldePieza(molde=molde_balde, pieza=pieza_balde, peso_unitario_gr=145.0, cavidades=1)
    session.add_all([molde_balde, prod_balde, pieza_balde, molde_pieza_balde])

    # Complejo
    molde_jarra = Molde(codigo="MLJ01", nombre="Molde Jarra Regadera", peso_tiro_gr=150.0)
    prod_jarra = ProductoTerminado(cod_sku_pt="PT-JRG", producto="Jarra Regadera", linea_id=linea.id, familia_id=familia.id)
    pieza_jarra_base = Pieza(codigo="PZ-JBASE", nombre="MZ-JBASE", linea_id=linea.id, familia_id=familia.id, peso_nominal_gr=100.0)
    pieza_jarra_tapa = Pieza(codigo="PZ-JTAPA", nombre="MZ-JTAPA", linea_id=linea.id, familia_id=familia.id, peso_nominal_gr=20.0)
    pieza_jarra_roseta = Pieza(codigo="PZ-JROS", nombre="MZ-JROS", linea_id=linea.id, familia_id=familia.id, peso_nominal_gr=5.0)
    molde_pieza_jarra_base = MoldePieza(molde=molde_jarra, pieza=pieza_jarra_base, peso_unitario_gr=100.0, cavidades=1)
    molde_pieza_jarra_tapa = MoldePieza(molde=molde_jarra, pieza=pieza_jarra_tapa, peso_unitario_gr=20.0, cavidades=1)
    molde_pieza_jarra_roseta = MoldePieza(molde=molde_jarra, pieza=pieza_jarra_roseta, peso_unitario_gr=5.0, cavidades=2)
    session.add_all([
        molde_jarra, prod_jarra,
        pieza_jarra_base, pieza_jarra_tapa, pieza_jarra_roseta,
        molde_pieza_jarra_base, molde_pieza_jarra_tapa, molde_pieza_jarra_roseta,
    ])

    # La OP valida la BOM por Pieza abstracta. Los SKU genericos sin color se
    # conservan como referencias de transicion mientras las salidas coloreadas
    # se resuelven por cada lote.
    session.flush()
    sku_balde = PiezaColor(
        sku="PC-BALDE-GENERICA",
        piezas="Balde generico",
        pieza_id=pieza_balde.id,
        linea_id=linea.id,
        familia_id=familia.id,
    )
    sku_jarra_base = PiezaColor(
        sku="PC-JBASE-GENERICA",
        piezas="Jarra base generica",
        pieza_id=pieza_jarra_base.id,
        linea_id=linea.id,
        familia_id=familia.id,
    )
    sku_jarra_tapa = PiezaColor(
        sku="PC-JTAPA-GENERICA",
        piezas="Jarra tapa generica",
        pieza_id=pieza_jarra_tapa.id,
        linea_id=linea.id,
        familia_id=familia.id,
    )
    sku_jarra_roseta = PiezaColor(
        sku="PC-JROS-GENERICA",
        piezas="Jarra roseta generica",
        pieza_id=pieza_jarra_roseta.id,
        linea_id=linea.id,
        familia_id=familia.id,
    )
    session.add_all([
        sku_balde,
        sku_jarra_base,
        sku_jarra_tapa,
        sku_jarra_roseta,
    ])
    session.flush()
    session.add_all([
        ProductoPieza(
            producto_terminado_id=prod_balde.cod_sku_pt,
            pieza_sku=sku_balde.sku,
        ),
        ProductoPieza(
            producto_terminado_id=prod_jarra.cod_sku_pt,
            pieza_sku=sku_jarra_base.sku,
        ),
        ProductoPieza(
            producto_terminado_id=prod_jarra.cod_sku_pt,
            pieza_sku=sku_jarra_tapa.sku,
        ),
        ProductoPieza(
            producto_terminado_id=prod_jarra.cod_sku_pt,
            pieza_sku=sku_jarra_roseta.sku,
        ),
    ])

    session.commit()

    return {
        'maquina_1': maq,
        'molde_balde': molde_balde,
        'producto_balde': prod_balde,
        'pieza_balde': pieza_balde,
        'molde_pieza_balde': molde_pieza_balde,
        'molde_jarra': molde_jarra,
        'producto_jarra': prod_jarra,
        'pieza_jarra_base': pieza_jarra_base,
        'pieza_jarra_tapa': pieza_jarra_tapa,
        'pieza_jarra_roseta': pieza_jarra_roseta,
        'molde_pieza_jarra_base': molde_pieza_jarra_base,
        'molde_pieza_jarra_tapa': molde_pieza_jarra_tapa,
        'molde_pieza_jarra_roseta': molde_pieza_jarra_roseta,
    }

def test_creacion_op_simple_un_molde_una_pieza(client, session, mock_data):
    """
    Escenario 1: Creación de OP para 'Balde Playero Romano'
    1 Molde -> 1 PiezaColor
    """
    # Preparar el payload enviando auto_snapshot_molde: true
    payload = {
        "numero_op": "OP-TEST-001",
        "maquina_id": mock_data['maquina_1'].id,
        "producto": "Balde Romano",
        "producto_sku": mock_data['producto_balde'].cod_sku_pt,
        "molde": mock_data['molde_balde'].nombre,
        "molde_id": mock_data['molde_balde'].codigo,
        "snapshot_tiempo_ciclo": 45.0,
        "snapshot_horas_turno": 12.0,
        "snapshot_peso_colada_gr": 5.0, # Asumiendo un peso de tiro de la colada
        "auto_snapshot_molde": True,
        "lotes": [
            {
                "color_nombre": "ROJO",
                "meta_kg": 500,
                "personas": 1,
                "materiales": [],
                "pigmentos": []
            }
        ]
    }

    # Act
    res = client.post('/api/ordenes', json=payload)
    
    # Assert
    assert res.status_code == 201, f"Error: {res.json}"
    
    op = session.get(OrdenProduccion, "OP-TEST-001")
    assert op is not None
    
    # Verificar que haya solo 1 registro de snapshot (1 pieza)
    assert len(op.snapshot_composicion) == 1
    snap = op.snapshot_composicion[0]
    
    # El molde_balde mockeado tiene 1 pieza, revisar que el peso y cavidades sea de Pieza catalog
    assert snap.cavidades == mock_data['molde_pieza_balde'].cavidades
    
    # Verificar calculo del peso neto (Debe ser cavidades * peso_unit)
    esperado_neto = snap.cavidades * snap.peso_unit_gr
    assert op.calculo_peso_neto_golpe == esperado_neto
    assert op.calculo_cavidades_totales == snap.cavidades

def test_creacion_op_compleja_multi_pieza(client, session, mock_data):
    """
    Escenario 2: Creación de OP para 'Jarra Regadera'
    1 Molde -> 3 Piezas (Base, Tapa, Roseta)
    """
    payload = {
        "numero_op": "OP-TEST-002",
        "maquina_id": mock_data['maquina_1'].id,
        "producto": "Jarra Regadera",
        "producto_sku": mock_data['producto_jarra'].cod_sku_pt,
        "molde": mock_data['molde_jarra'].nombre,
        "molde_id": mock_data['molde_jarra'].codigo,
        "snapshot_tiempo_ciclo": 60.0,
        "snapshot_horas_turno": 24.0,
        "snapshot_peso_colada_gr": 15.0,
        "auto_snapshot_molde": True,
        "lotes": []
    }

    res = client.post('/api/ordenes', json=payload)
    
    assert res.status_code == 201
    
    op = session.get(OrdenProduccion, "OP-TEST-002")
    
    # Verificar que inserto las N piezas vinculadas al molde_jarra
    assert len(op.snapshot_composicion) == 3
    
    # Sumar cavidades y peso
    total_cavs = sum([s.cavidades for s in op.snapshot_composicion])
    total_peso = sum([s.cavidades * s.peso_unit_gr for s in op.snapshot_composicion])
    
    assert op.calculo_cavidades_totales == total_cavs
    assert op.calculo_peso_neto_golpe == total_peso

def test_creacion_op_manual_sobrescrita(client, session, mock_data):
    """
    Escenario 3: OP multi-pieza manual con cavidades efectivas positivas.
    """
    payload = {
        "numero_op": "OP-TEST-003",
        "maquina_id": mock_data['maquina_1'].id,
        "producto": "Jarra Regadera",
        "producto_sku": mock_data['producto_jarra'].cod_sku_pt,
        "molde": mock_data['molde_jarra'].nombre,
        "molde_id": mock_data['molde_jarra'].codigo,
        "snapshot_tiempo_ciclo": 60.0,
        "snapshot_horas_turno": 24.0,
        "snapshot_peso_colada_gr": 15.0,
        "auto_snapshot_molde": False, # MANUAL
        "snapshot_composicion": [
            {
                "molde_pieza_id": mock_data['molde_pieza_jarra_base'].id,
                "cavidades": 1,
                "peso_unit_gr": 100.0
            },
            {
                "molde_pieza_id": mock_data['molde_pieza_jarra_tapa'].id,
                "cavidades": 1,
                "peso_unit_gr": 20.0
            },
            {
                "molde_pieza_id": mock_data['molde_pieza_jarra_roseta'].id,
                "cavidades": 2,
                "peso_unit_gr": 5.0
            }
        ],
        "lotes": []
    }

    res = client.post('/api/ordenes', json=payload)
    
    assert res.status_code == 201
    
    op = session.get(OrdenProduccion, "OP-TEST-003")
    
    assert len(op.snapshot_composicion) == 3
    
    # Verifica calculos
    assert op.calculo_cavidades_totales == 4
    esperado_peso = (1 * 100.0) + (1 * 20.0) + (2 * 5.0)
    assert op.calculo_peso_neto_golpe == esperado_peso

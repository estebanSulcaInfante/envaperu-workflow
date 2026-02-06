import pytest
from app.models.molde import Molde, MoldePieza
from app.models.producto import Pieza
from app.extensions import db

def test_simple_molde_flow(client, app):
    """
    Test creation and update of Molde using Simple Mode (hidden Pieza).
    """
    molde_code = "MOL-SIMPLE-TEST"
    
    # 1. CREATE MOLDE (Simple Mode)
    payload = {
        "codigo": molde_code,
        "nombre": "Molde Simple Test",
        "peso_tiro_gr": 100.0,
        "tiempo_ciclo_std": 20.0,
        "cavidades": 4,         # Simple Mode
        "peso_unitario_gr": 20.0 # Simple Mode (Total Net = 80g, Merma = 20g)
    }
    
    resp = client.post('/api/moldes', json=payload)
    if resp.status_code != 201:
        print(f"CREATE ERROR: {resp.get_json()}")
        
    assert resp.status_code == 201
    data = resp.get_json()
    assert data['codigo'] == molde_code
    
    # Verify DB side effects
    with app.app_context():
        # A. Molde created
        molde = db.session.get(Molde, molde_code)
        assert molde is not None
        assert molde.peso_tiro_gr == 100.0
        
        # B. Pieza created
        pieza_sku = f"{molde_code}-STD"
        pieza = db.session.get(Pieza, pieza_sku)
        assert pieza is not None
        assert pieza.tipo == 'SIMPLE'
        assert pieza.peso == 20.0
        assert pieza.cavidad == 4
        
        # C. Relation created
        mp = MoldePieza.query.filter_by(molde_id=molde_code, pieza_sku=pieza_sku).first()
        assert mp is not None
        assert mp.cavidades == 4
        assert mp.peso_unitario_gr == 20.0
        
        # D. Calculated Properties
        # Refresh dynamic properties
        assert molde.peso_neto_gr == 80.0  # 4 * 20
        assert abs(molde.merma_pct - 0.2) < 0.001 # (100-80)/100 = 0.2
        
    # 2. UPDATE MOLDE (Simple Mode)
    update_payload = {
        "peso_tiro_gr": 110.0,
        "cavidades": 4,
        "peso_unitario_gr": 25.0 # Increased weight -> Total Net 100g -> Merma 10g
    }
    
    resp = client.put(f'/api/moldes/{molde_code}', json=update_payload)
    assert resp.status_code == 200
    
    with app.app_context():
        # Verify Update
        molde = db.session.get(Molde, molde_code)
        assert molde.peso_tiro_gr == 110.0
        
        # Verify Pieza updated
        pieza_sku = f"{molde_code}-STD"
        pieza = db.session.get(Pieza, pieza_sku)
        assert pieza.peso == 25.0
        
        # Verify Relation updated
        mp = MoldePieza.query.filter_by(molde_id=molde_code).first()
        assert mp.peso_unitario_gr == 25.0
        
        # Verify Metrics
        assert molde.peso_neto_gr == 100.0 # 4 * 25
        # Merma: (110 - 100) / 110 = 10 / 110 = 0.0909
        assert abs(molde.merma_pct - (10/110)) < 0.001

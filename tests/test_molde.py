"""
Tests for Molde entity and related models
"""
import pytest
from app import create_app
from app.extensions import db
from app.models.molde import Molde, MoldePieza
from app.models.producto import Pieza, PiezaComponente, Linea, Familia


@pytest.fixture
def app():
    """Create test app with test database"""
    app = create_app()
    app.config['TESTING'] = True
    
    with app.app_context():
        db.create_all()
        yield app
        db.session.rollback()


@pytest.fixture
def client(app):
    return app.test_client()


def get_default_linea_familia(app):
    """Helper to get or create default Linea and Familia IDs"""
    linea = Linea.query.filter_by(nombre='TEST').first()
    if not linea:
        linea = Linea(codigo=99, nombre='TEST')
        db.session.add(linea)
        db.session.flush()
    
    familia = Familia.query.filter_by(nombre='TEST').first()
    if not familia:
        familia = Familia(codigo=99, nombre='TEST')
        db.session.add(familia)
        db.session.flush()
    
    db.session.commit()
    return linea.id, familia.id


class TestMoldeHomogeneo:
    """Test para moldes con una sola pieza (Balde Romano)"""
    
    def test_crear_molde_homogeneo(self, client, app):
        """Crear un molde con una sola pieza"""
        with app.app_context():
            linea_id, familia_id = get_default_linea_familia(app)
            
            # Crear pieza
            pieza = Pieza(
                sku="BALDE-001",
                piezas="Balde Romano",
                tipo="SIMPLE",
                peso=87.0,
                cavidad=4,
                linea_id=linea_id,
                familia_id=familia_id
            )
            db.session.add(pieza)
            db.session.commit()
            
            # Crear molde
            molde = Molde(
                codigo="MOL-BALDE",
                nombre="Balde Romano",
                peso_tiro_gr=352.0,
                tiempo_ciclo_std=30.0
            )
            db.session.add(molde)
            db.session.commit()
            
            # Relacionar
            mp = MoldePieza(
                molde_id="MOL-BALDE",
                pieza_sku="BALDE-001",
                cavidades=4,
                peso_unitario_gr=87.0
            )
            db.session.add(mp)
            db.session.commit()
            
            # Verificar cálculos
            assert molde.peso_neto_gr == 348.0  # 87 × 4
            assert molde.peso_colada_gr == 4.0  # 352 - 348
            assert molde.cavidades_totales == 4
            assert molde.merma_pct == pytest.approx(4.0 / 352.0, abs=0.001)


class TestMoldeHeterogeneo:
    """Test para moldes con múltiples piezas (Kit Regadera)"""
    
    def test_crear_molde_heterogeneo_con_kit(self, client, app):
        """Crear un molde que produce un kit (tapa + asa + base)"""
        with app.app_context():
            linea_id, familia_id = get_default_linea_familia(app)
            
            # Crear piezas componentes
            tapa = Pieza(sku="REG-TAPA", piezas="Tapa Regadera", tipo="COMPONENTE", peso=25.0, linea_id=linea_id, familia_id=familia_id)
            asa = Pieza(sku="REG-ASA", piezas="Asa Regadera", tipo="COMPONENTE", peso=40.0, linea_id=linea_id, familia_id=familia_id)
            base = Pieza(sku="REG-BASE", piezas="Base Regadera", tipo="COMPONENTE", peso=120.0, linea_id=linea_id, familia_id=familia_id)
            
            # Crear pieza kit
            kit = Pieza(sku="REG-KIT", piezas="Kit Regadera", tipo="KIT", peso=185.0, linea_id=linea_id, familia_id=familia_id)
            
            db.session.add_all([tapa, asa, base, kit])
            db.session.commit()
            
            # Crear relaciones de componentes
            pc1 = PiezaComponente(kit_sku="REG-KIT", componente_sku="REG-TAPA", cantidad=1)
            pc2 = PiezaComponente(kit_sku="REG-KIT", componente_sku="REG-ASA", cantidad=1)
            pc3 = PiezaComponente(kit_sku="REG-KIT", componente_sku="REG-BASE", cantidad=1)
            db.session.add_all([pc1, pc2, pc3])
            db.session.commit()
            
            # Crear molde
            molde = Molde(
                codigo="MOL-REGADERA",
                nombre="Regadera Completa",
                peso_tiro_gr=195.0
            )
            db.session.add(molde)
            db.session.commit()
            
            # Relacionar molde con kit
            mp = MoldePieza(
                molde_id="MOL-REGADERA",
                pieza_sku="REG-KIT",
                cavidades=1,
                peso_unitario_gr=185.0
            )
            db.session.add(mp)
            db.session.commit()
            
            # Verificar kit tiene componentes
            assert len(kit.componentes) == 3
            
            # Verificar molde cálculos
            assert molde.peso_neto_gr == 185.0  # 185 × 1
            assert molde.peso_colada_gr == 10.0  # 195 - 185
            assert molde.cavidades_totales == 1


class TestPiezasProducibles:
    """Test para validación de piezas producibles"""
    
    def test_solo_piezas_en_molde_son_producibles(self, client, app):
        """Solo piezas asociadas a un molde via MoldePieza son producibles"""
        with app.app_context():
            linea_id, familia_id = get_default_linea_familia(app)
            
            # Crear pieza producible (con molde)
            pieza_prod = Pieza(sku="PROD-001", piezas="Producible", tipo="SIMPLE", linea_id=linea_id, familia_id=familia_id)
            pieza_no_prod = Pieza(sku="COMP-001", piezas="Componente", tipo="COMPONENTE", linea_id=linea_id, familia_id=familia_id)
            
            db.session.add_all([pieza_prod, pieza_no_prod])
            db.session.commit()
            
            # Crear molde solo para la pieza producible
            molde = Molde(codigo="MOL-TEST", nombre="Test", peso_tiro_gr=100.0)
            db.session.add(molde)
            db.session.commit()
            
            mp = MoldePieza(
                molde_id="MOL-TEST",
                pieza_sku="PROD-001",
                cavidades=2,
                peso_unitario_gr=45.0
            )
            db.session.add(mp)
            db.session.commit()
            
            # Query para piezas producibles
            piezas_producibles = Pieza.query.join(MoldePieza).distinct().all()
            skus_producibles = [p.sku for p in piezas_producibles]
            
            assert "PROD-001" in skus_producibles
            assert "COMP-001" not in skus_producibles


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


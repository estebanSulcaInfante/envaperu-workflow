"""
Tests for Molde entity and related models
"""
import pytest
from app import create_app
from app.extensions import db
from app.models.molde import Molde, MoldePieza, Pieza
from app.models.producto import PiezaColor, Linea, Familia


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
            
            # Crear maestro global y su variante coloreada.
            pieza_global = Pieza(
                codigo="PZ-BALDE-001",
                nombre="Balde Romano",
                linea_id=linea_id,
                familia_id=familia_id,
                peso_nominal_gr=87.0,
            )
            db.session.add(pieza_global)
            db.session.flush()

            pieza = PiezaColor(
                sku="BALDE-001",
                piezas="Balde Romano",
                peso=87.0,
                cavidad=4,
                linea_id=linea_id,
                familia_id=familia_id,
                pieza_id=pieza_global.id,
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
                molde=molde,
                pieza=pieza_global,
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
    """Test para moldes con múltiples piezas físicas."""
    
    def test_crear_molde_heterogeneo_con_piezas(self, client, app):
        """Crear un molde que produce tapa, asa y base en un mismo tiro."""
        with app.app_context():
            linea_id, familia_id = get_default_linea_familia(app)
            
            maestros = {
                "tapa": Pieza(codigo="PZ-REG-TAPA", nombre="Tapa Regadera", linea_id=linea_id, familia_id=familia_id, peso_nominal_gr=25.0),
                "asa": Pieza(codigo="PZ-REG-ASA", nombre="Asa Regadera", linea_id=linea_id, familia_id=familia_id, peso_nominal_gr=40.0),
                "base": Pieza(codigo="PZ-REG-BASE", nombre="Base Regadera", linea_id=linea_id, familia_id=familia_id, peso_nominal_gr=120.0),
            }
            db.session.add_all(maestros.values())
            db.session.flush()

            # Cada SKU coloreado apunta al maestro global correspondiente.
            tapa = PiezaColor(sku="REG-TAPA", piezas="Tapa Regadera", peso=25.0, linea_id=linea_id, familia_id=familia_id, pieza_id=maestros["tapa"].id)
            asa = PiezaColor(sku="REG-ASA", piezas="Asa Regadera", peso=40.0, linea_id=linea_id, familia_id=familia_id, pieza_id=maestros["asa"].id)
            base = PiezaColor(sku="REG-BASE", piezas="Base Regadera", peso=120.0, linea_id=linea_id, familia_id=familia_id, pieza_id=maestros["base"].id)

            db.session.add_all([tapa, asa, base])
            db.session.commit()
            
            # Crear molde
            molde = Molde(
                codigo="MOL-REGADERA",
                nombre="Regadera Completa",
                peso_tiro_gr=195.0
            )
            db.session.add(molde)
            db.session.commit()
            
            # La composición física del molde permanece separada de la BOM.
            db.session.add_all([
                MoldePieza(
                    molde=molde,
                    pieza=maestros["tapa"],
                    cavidades=1,
                    peso_unitario_gr=25.0,
                ),
                MoldePieza(
                    molde=molde,
                    pieza=maestros["asa"],
                    cavidades=1,
                    peso_unitario_gr=40.0,
                ),
                MoldePieza(
                    molde=molde,
                    pieza=maestros["base"],
                    cavidades=1,
                    peso_unitario_gr=120.0,
                ),
            ])
            db.session.commit()

            # Verificar molde cálculos
            assert molde.peso_neto_gr == 185.0  # 185 × 1
            assert molde.peso_colada_gr == 10.0  # 195 - 185
            assert molde.cavidades_totales == 3


class TestPiezasProducibles:
    """Test para validación de piezas producibles"""
    
    def test_solo_piezas_en_molde_son_producibles(self, client, app):
        """Solo piezas asociadas a un molde via Pieza son producibles"""
        with app.app_context():
            linea_id, familia_id = get_default_linea_familia(app)
            
            maestro_prod = Pieza(codigo="PZ-PROD-001", nombre="Producible", linea_id=linea_id, familia_id=familia_id, peso_nominal_gr=45.0)
            maestro_no_prod = Pieza(codigo="PZ-COMP-001", nombre="Componente", linea_id=linea_id, familia_id=familia_id, peso_nominal_gr=12.0)
            db.session.add_all([maestro_prod, maestro_no_prod])
            db.session.flush()

            pieza_prod = PiezaColor(sku="PROD-001", piezas="Producible", linea_id=linea_id, familia_id=familia_id, pieza_id=maestro_prod.id)
            pieza_no_prod = PiezaColor(sku="COMP-001", piezas="Componente", linea_id=linea_id, familia_id=familia_id, pieza_id=maestro_no_prod.id)

            db.session.add_all([pieza_prod, pieza_no_prod])
            db.session.commit()
            
            # Crear molde solo para la pieza producible
            molde = Molde(codigo="MOL-TEST", nombre="Test", peso_tiro_gr=100.0)
            db.session.add(molde)
            db.session.commit()
            
            mp = MoldePieza(
                molde=molde,
                pieza=maestro_prod,
                cavidades=2,
                peso_unitario_gr=45.0
            )
            db.session.add(mp)
            db.session.commit()
            
            # Query para piezas producibles
            piezas_producibles = (
                PiezaColor.query
                .join(Pieza, PiezaColor.pieza_id == Pieza.id)
                .join(MoldePieza, MoldePieza.pieza_id == Pieza.id)
                .filter(MoldePieza.activo.is_(True))
                .distinct()
                .all()
            )
            skus_producibles = [p.sku for p in piezas_producibles]
            
            assert "PROD-001" in skus_producibles
            assert "COMP-001" not in skus_producibles


class TestMoldePiezaManyToMany:
    """Una pieza global puede formar parte de más de un molde."""

    def test_misma_pieza_en_dos_moldes_con_configuracion_independiente(self, app):
        with app.app_context():
            linea_id, familia_id = get_default_linea_familia(app)
            pieza = Pieza(
                codigo="PZ-COMPARTIDA-001",
                nombre="Tapa compartida",
                linea_id=linea_id,
                familia_id=familia_id,
                peso_nominal_gr=12.5,
            )
            molde_a = Molde(codigo="MOL-NM-A", nombre="Molde A", peso_tiro_gr=60.0)
            molde_b = Molde(codigo="MOL-NM-B", nombre="Molde B", peso_tiro_gr=90.0)
            db.session.add_all([pieza, molde_a, molde_b])
            db.session.flush()

            relacion_a = MoldePieza(
                molde=molde_a,
                pieza=pieza,
                cavidades=4,
                peso_unitario_gr=12.5,
            )
            relacion_b = MoldePieza(
                molde=molde_b,
                pieza=pieza,
                cavidades=6,
                peso_unitario_gr=13.0,
            )
            db.session.add_all([relacion_a, relacion_b])
            db.session.commit()

            assert relacion_a.pieza_id == relacion_b.pieza_id == pieza.id
            assert molde_a.peso_neto_gr == 50.0
            assert molde_b.peso_neto_gr == 78.0

            relacion_a.cavidades = 3
            relacion_a.peso_unitario_gr = 12.0
            relacion_a.version += 1
            db.session.commit()

            db.session.refresh(relacion_b)
            db.session.refresh(pieza)
            assert relacion_a.cavidades == 3
            assert relacion_a.peso_unitario_gr == 12.0
            assert relacion_b.cavidades == 6
            assert relacion_b.peso_unitario_gr == 13.0
            assert pieza.peso_nominal_gr == 12.5


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


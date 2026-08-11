
import os
# Force SQLite for tests -> MUST be done before importing app.config
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

import pytest
from sqlalchemy import text
from app import create_app, db

@pytest.fixture
def app():
    app = create_app()
    app.config.update({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"
    })

    with app.app_context():
        db.create_all()
        db.session.execute(text("""
            CREATE VIEW v_avance_produccion AS
            SELECT
                station_id,
                operational_date,
                op_normalized AS op,
                ot_normalized AS ot,
                mold_normalized AS mold,
                color_normalized AS color,
                machine_normalized AS machine_code,
                shift_normalized AS shift,
                COUNT(*) AS bags,
                SUM(weight_kg) AS weight_kg,
                MIN(captured_at_utc) AS first_capture_at_utc,
                MAX(captured_at_utc) AS last_capture_at_utc,
                MAX(imported_at_utc) AS last_received_at_utc
            FROM estacion_pesaje_legacy
            WHERE is_deleted = false
            GROUP BY
                station_id,
                operational_date,
                op_normalized,
                ot_normalized,
                mold_normalized,
                color_normalized,
                machine_normalized,
                shift_normalized
        """))
        
        from app.models.producto import Familia, Linea, LineaFamilia
        default_linea = Linea(codigo=1, nombre='INDUSTRIAL')
        default_familia = Familia(codigo=1, nombre='TEST')
        db.session.add_all([default_linea, default_familia])
        db.session.flush()
        db.session.add(LineaFamilia(
            linea_id=default_linea.id,
            familia_id=default_familia.id,
        ))
        
        # TS-009 Dependencies
        from app.models.maquina import TipoMaquina, Maquina
        from app.models.trabajador import RolOperativo, Trabajador
        
        tipo_maq = TipoMaquina(codigo="INYECCION", nombre="INYECCION", proceso="PRODUCCION")
        db.session.add(tipo_maq)
        db.session.flush()
        
        maq = Maquina(codigo="MQ-01", nombre="Maquina 1", tipo_maquina_id=tipo_maq.id, estado="OPERATIVA", activo=True)
        db.session.add(maq)
        
        rol_maq = RolOperativo(codigo="MAQUINISTA", nombre="MAQUINISTA")
        db.session.add(rol_maq)
        db.session.flush()
        
        trab = Trabajador(codigo="TRB-01", nombres="Juan", apellidos="Perez", nombre_corto="Juan P.", activo=True)
        trab.roles.append(rol_maq)
        db.session.add(trab)
        db.session.flush()

        db.session.commit()

        yield app
        # One contract test enables SQLite FK enforcement on the shared
        # in-memory connection. Disable it before dropping the full metadata;
        # the production schema contains an intentional cyclic FK between OT
        # header/context tables that SQLite cannot topologically drop.
        db.session.execute(text("PRAGMA foreign_keys = OFF"))
        db.session.execute(text("DROP VIEW IF EXISTS v_avance_produccion"))
        db.session.commit()
        db.session.remove()
        db.drop_all()

@pytest.fixture
def client(app):
    return app.test_client()

@pytest.fixture
def runner(app):
    return app.test_cli_runner()


@pytest.fixture
def scm_config(app):
    from app.services.scm_configuration import ensure_initial_scm_configuration

    with app.app_context():
        return ensure_initial_scm_configuration()

@pytest.fixture
def test_linea(app):
    """Provide a default Linea for tests that require linea_id FK."""
    from app.models.producto import Linea
    with app.app_context():
        linea = Linea.query.filter_by(nombre='TEST').first()
        if not linea:
            linea = Linea(codigo=99, nombre='TEST')
            db.session.add(linea)
            db.session.commit()
        return linea.id

@pytest.fixture
def test_familia(app):
    """Provide a default Familia for tests that require familia_id FK."""
    from app.models.producto import Familia
    with app.app_context():
        familia = Familia.query.filter_by(nombre='TEST').first()
        if not familia:
            familia = Familia(codigo=99, nombre='TEST')
            db.session.add(familia)
            db.session.commit()
        return familia.id


def get_or_create_test_dependencies():
    """
    Utility function for tests to ensure Linea and Familia exist.
    Call this inside app_context to get IDs for required FKs.
    Returns: (linea_id, familia_id)
    """
    from app.models.producto import Familia, Linea, LineaFamilia
    
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

    relacion = LineaFamilia.query.filter_by(
        linea_id=linea.id,
        familia_id=familia.id,
    ).first()
    if not relacion:
        db.session.add(LineaFamilia(
            linea_id=linea.id,
            familia_id=familia.id,
        ))
    elif not relacion.activo:
        relacion.activo = True
        relacion.version += 1
    
    db.session.commit()
    return linea.id, familia.id

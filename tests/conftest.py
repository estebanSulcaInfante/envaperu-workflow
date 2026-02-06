
import os
# Force SQLite for tests -> MUST be done before importing app.config
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

import pytest
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
        
        # Create default Linea and Familia for tests that need them
        from app.models.producto import Linea, Familia
        default_linea = Linea(codigo=1, nombre='INDUSTRIAL')
        default_familia = Familia(codigo=1, nombre='TEST')
        db.session.add_all([default_linea, default_familia])
        db.session.commit()
        
        yield app
        db.session.remove()
        db.drop_all()

@pytest.fixture
def client(app):
    return app.test_client()

@pytest.fixture
def runner(app):
    return app.test_cli_runner()

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
    from app.models.producto import Linea, Familia
    
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

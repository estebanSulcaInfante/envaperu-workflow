from flask import Flask
from app.config import Config
from app.extensions import db, cors

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    db.init_app(app)
    cors.init_app(app) # Importante para que el Frontend pueda llamar al Backend

    # --- IMPORTAR MODELOS ---
    # Es crucial importar los modelos aquí para que SQLAlchemy los registre
    # antes de que cualquier blueprint intente usarlos.
    from app.models import orden, lote, materiales, recetas, producto, registro, control_peso

    # --- REGISTRO DE RUTAS ---
    from app.api.rutas_produccion import produccion_bp
    from app.api.rutas_catalogo import catalogo_bp
    from app.api.rutas_talonarios import talonarios_bp
    from app.api.rutas_sync import sync_bp
    
    # Todo lo que esté en ese archivo empezará con /api
    app.register_blueprint(produccion_bp, url_prefix='/api')
    app.register_blueprint(catalogo_bp, url_prefix='/api')
    app.register_blueprint(talonarios_bp)
    app.register_blueprint(sync_bp, url_prefix='/api')

    return app
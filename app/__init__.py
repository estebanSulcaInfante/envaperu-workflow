from flask import Flask, jsonify
from app.config import Config
from app.extensions import db, cors
import logging
from logging.handlers import RotatingFileHandler
import os

def setup_logging(app):
    """Configura logging estructurado para la aplicación"""
    # Crear directorio de logs si no existe
    log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'logs')
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
    
    # Configurar handler de archivo rotativo
    file_handler = RotatingFileHandler(
        os.path.join(log_dir, 'envaperu.log'),
        maxBytes=10240000,  # 10MB
        backupCount=5
    )
    file_handler.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)s [%(name)s] %(message)s'
    ))
    file_handler.setLevel(logging.INFO)
    
    # Configurar logger principal
    logger = logging.getLogger('envaperu')
    logger.setLevel(logging.INFO)
    logger.addHandler(file_handler)
    
    # También agregar al logger de Flask
    app.logger.addHandler(file_handler)
    app.logger.setLevel(logging.INFO)
    
    return logger

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    db.init_app(app)
    cors.init_app(app) # Importante para que el Frontend pueda llamar al Backend
    
    # Configurar logging
    setup_logging(app)

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
    
    # --- MANEJADORES DE ERROR GLOBALES ---
    @app.errorhandler(404)
    def not_found_error(error):
        return jsonify({'error': 'Recurso no encontrado', 'status': 404}), 404
    
    @app.errorhandler(500)
    def internal_error(error):
        db.session.rollback()
        app.logger.error(f'Server Error: {error}')
        return jsonify({'error': 'Error interno del servidor', 'status': 500}), 500

    return app
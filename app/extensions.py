from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from flask_migrate import Migrate

# Instanciamos vacíos, luego los iniciamos en __init__.py
db = SQLAlchemy()
cors = CORS()
migrate = Migrate()

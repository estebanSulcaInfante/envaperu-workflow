from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS

# Instanciamos vacíos, luego los iniciamos en __init__.py
db = SQLAlchemy()
cors = CORS()

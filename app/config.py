import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    # Heroku usa postgres:// pero SQLAlchemy requiere postgresql://
    _db_url = os.getenv('DATABASE_URL', 'postgresql://postgres:1234@localhost:5432/enva_test')
    if _db_url.startswith('postgres://'):
        _db_url = _db_url.replace('postgres://', 'postgresql://', 1)
    SQLALCHEMY_DATABASE_URI = _db_url
    SQLALCHEMY_TRACK_MODIFICATIONS = False
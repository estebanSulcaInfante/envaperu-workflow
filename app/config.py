import os
from dotenv import load_dotenv

load_dotenv()


def _csv_env(name, default=""):
    return [
        item.strip()
        for item in os.getenv(name, default).split(",")
        if item.strip()
    ]

class Config:
    # Heroku usa postgres:// pero SQLAlchemy requiere postgresql://
    _db_url = os.getenv('DATABASE_URL', 'postgresql://postgres:1234@localhost:5432/enva_test')
    if _db_url.startswith('postgres://'):
        _db_url = _db_url.replace('postgres://', 'postgresql://', 1)
    SQLALCHEMY_DATABASE_URI = _db_url
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        'pool_pre_ping': True,
        'pool_recycle': 300,
    }
    CORS_ORIGINS = _csv_env('ALLOWED_ORIGINS', '*') or ['*']
    CATALOG_IMAGE_STORAGE = os.getenv(
        'CATALOG_IMAGE_STORAGE',
        'database',
    ).strip().lower()
    CATALOG_IMAGE_KEEP_DATABASE_COPY = os.getenv(
        'CATALOG_IMAGE_KEEP_DATABASE_COPY',
        'true',
    ).strip().lower() == 'true'
    SUPABASE_S3_ENDPOINT = os.getenv('SUPABASE_S3_ENDPOINT', '').strip()
    SUPABASE_S3_REGION = os.getenv('SUPABASE_S3_REGION', '').strip()
    SUPABASE_S3_ACCESS_KEY_ID = os.getenv(
        'SUPABASE_S3_ACCESS_KEY_ID',
        '',
    ).strip()
    SUPABASE_S3_SECRET_ACCESS_KEY = os.getenv(
        'SUPABASE_S3_SECRET_ACCESS_KEY',
        '',
    ).strip()
    SUPABASE_STORAGE_BUCKET = os.getenv(
        'SUPABASE_STORAGE_BUCKET',
        'catalog-images',
    ).strip()
    MINIMUM_STATION_VERSION = os.getenv('MINIMUM_STATION_VERSION', '1.1.0')
    HEARTBEAT_SECONDS = int(os.getenv('HEARTBEAT_SECONDS', '30'))
    HEARTBEAT_DELAYED_SECONDS = int(
        os.getenv('HEARTBEAT_DELAYED_SECONDS', '90')
    )
    HEARTBEAT_DISCONNECTED_SECONDS = int(
        os.getenv('HEARTBEAT_DISCONNECTED_SECONDS', '300')
    )
    STATION_CATALOG_ENABLED = (
        os.getenv('STATION_CATALOG_ENABLED', 'false').lower() == 'true'
    )
    SCM_RECEPCION_ENABLED = (
        os.getenv('SCM_RECEPCION_ENABLED', 'false').lower() == 'true'
    )
    SCM_AUTH_MODE = os.getenv('SCM_AUTH_MODE', 'local_actor').strip().lower()
    SUPABASE_URL = os.getenv('SUPABASE_URL', '').strip().rstrip('/')
    SUPABASE_JWT_AUDIENCE = os.getenv(
        'SUPABASE_JWT_AUDIENCE',
        'authenticated',
    ).strip()
    SUPABASE_JWT_ISSUER = os.getenv('SUPABASE_JWT_ISSUER', '').strip().rstrip('/')

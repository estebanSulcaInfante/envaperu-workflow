import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    # La URL de Supabase debe empezar con postgresql:// (no postgres://)
    SQLALCHEMY_DATABASE_URI = os.getenv('DATABASE_URL','postgresql://postgres:1234@localhost:5432/enva_test')
    SQLALCHEMY_TRACK_MODIFICATIONS = False
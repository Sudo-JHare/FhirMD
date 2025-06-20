import os

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'your-fallback-secret-key')
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL', 'sqlite:///instance/app.db')
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    FHIR_PACKAGES_DIR = os.environ.get('FHIR_PACKAGES_DIR', '/app/instance/fhir_packages')
    UPLOAD_FOLDER = os.environ.get('UPLOAD_FOLDER', '/app/static/uploads')
    TEST_DATA_FOLDER = os.environ.get('TEST_DATA_FOLDER', '/app/test_data')
    APP_BASE_URL = os.environ.get('APP_BASE_URL', 'http://localhost:5000')
    API_KEY = os.environ.get('API_KEY', 'your-fallback-api-key')

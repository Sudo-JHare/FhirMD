from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

class CachedPackage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    package_name = db.Column(db.String(128), nullable=False)
    version = db.Column(db.String(64), nullable=False)
    author = db.Column(db.String(128))
    fhir_version = db.Column(db.String(64))
    version_count = db.Column(db.Integer)
    url = db.Column(db.String(256))
    all_versions = db.Column(db.JSON, nullable=True)
    dependencies = db.Column(db.JSON, nullable=True)
    latest_absolute_version = db.Column(db.String(64))
    latest_official_version = db.Column(db.String(64))
    canonical = db.Column(db.String(256))
    registry = db.Column(db.String(256))
    __table_args__ = (db.UniqueConstraint('package_name', 'version', name='uq_cached_package_version'),)

class RegistryCacheInfo(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    last_fetch_timestamp = db.Column(db.DateTime(timezone=True), nullable=True)

class ProcessedIg(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    package_name = db.Column(db.String(128), nullable=False)
    version = db.Column(db.String(64), nullable=False)
    processed_date = db.Column(db.DateTime, nullable=False)
    resource_types_info = db.Column(db.JSON, nullable=False)
    must_support_elements = db.Column(db.JSON, nullable=True)
    examples = db.Column(db.JSON, nullable=True)
    complies_with_profiles = db.Column(db.JSON, nullable=True)
    imposed_profiles = db.Column(db.JSON, nullable=True)
    optional_usage_elements = db.Column(db.JSON, nullable=True)
    search_param_conformance = db.Column(db.JSON, nullable=True)
    __table_args__ = (db.UniqueConstraint('package_name', 'version', name='uq_package_version'),)

class TestDataResource(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(256), nullable=False, unique=True)
    file_path = db.Column(db.String(512), nullable=False)
    resource_type = db.Column(db.String(64), nullable=False)
    resource_id = db.Column(db.String(128), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=datetime.datetime.now)
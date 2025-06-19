import sys
import os
import datetime
import queue
import threading
import json
import requests
import logging
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, Response, session
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_wtf import FlaskForm, CSRFProtect
from werkzeug.utils import secure_filename
from cachetools import TTLCache
from flasgger import Swagger
from logging.handlers import RotatingFileHandler
from forms import IgImportForm, ContributeTestDataForm
from services import (
    services_bp,
    fetch_packages_from_registries,
    normalize_package_data,
    cache_packages,
    import_package_and_dependencies,
    parse_package_filename,
    construct_tgz_filename,
    get_package_description,
    parse_test_data_folder,
    validate_resource_against_hapi,
    create_github_pull_request
)
from models import db, CachedPackage, RegistryCacheInfo, ProcessedIg, TestDataResource

# App setup
app = Flask(__name__)
app.config.from_pyfile('config.py')
db.init_app(app)
csrf = CSRFProtect(app)
migrate = Migrate(app, db)

# Swagger configuration
app.config['SWAGGER'] = {
    'title': 'FHIR Test Data App API',
    'uiversion': 3,
    'version': '1.0.0',
    'description': 'API for managing FHIR test data and Implementation Guides.',
    'termsOfService': 'https://example.com/terms',
    'contact': {'name': 'Support', 'email': 'support@example.com'},
    'license': {'name': 'MIT', 'url': 'https://opensource.org/licenses/MIT'},
    'securityDefinitions': {
        'ApiKeyAuth': {'type': 'apiKey', 'name': 'X-API-Key', 'in': 'header'}
    },
    'specs_route': '/apidocs/'
}
swagger = Swagger(app)

# Register blueprints
app.register_blueprint(services_bp, url_prefix='/api')

# Logging setup
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
instance_folder = app.instance_path
os.makedirs(instance_folder, exist_ok=True)
log_file_path = os.path.join(instance_folder, 'app_debug.log')
file_handler = RotatingFileHandler(log_file_path, maxBytes=5*1024*1024, backupCount=5, encoding='utf-8')
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
file_handler.setLevel(logging.DEBUG)
logging.getLogger().addHandler(file_handler)
logger.info(f"File logging initialized to {log_file_path}")

# In-memory cache
package_cache = TTLCache(maxsize=100, ttl=300)

# Log queue for SSE
log_queue = queue.Queue()
class StreamLogHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.INFO)
        self.formatter = logging.Formatter('%(levelname)s:%(name)s:%(message)s')
    def emit(self, record):
        if record.name == 'services' and record.levelno == logging.INFO:
            msg = self.format(record)
            log_queue.put(msg)
services_logger = logging.getLogger('services')
services_logger.addHandler(StreamLogHandler())

# Ensure directories
os.makedirs(app.config['FHIR_PACKAGES_DIR'], exist_ok=True)
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Context processor
@app.context_processor
def inject_app_mode():
    return dict(app_mode=app.config.get('APP_MODE', 'standalone'))

# Routes
@app.route('/')
def index():
    return render_template('index.html', site_name='FHIR Test Data App', now=datetime.datetime.now())

@app.route('/search-and-import')
def search_and_import():
    page = request.args.get('page', 1, type=int)
    per_page = 50
    in_memory_packages = app.config.get('MANUAL_PACKAGE_CACHE')
    in_memory_timestamp = app.config.get('MANUAL_CACHE_TIMESTAMP')
    db_timestamp_info = RegistryCacheInfo.query.first()
    db_timestamp = db_timestamp_info.last_fetch_timestamp if db_timestamp_info else None
    normalized_packages = None
    fetch_failed_flag = session.get('fetch_failed', False)
    display_timestamp = None
    is_fetching = False
    fetch_in_progress = session.get('fetch_in_progress', False)

    if fetch_in_progress and in_memory_packages is not None:
        session['fetch_in_progress'] = False
        normalized_packages = in_memory_packages
        display_timestamp = in_memory_timestamp
        fetch_failed_flag = session.get('fetch_failed', False)
    elif in_memory_packages is not None:
        normalized_packages = in_memory_packages
        display_timestamp = in_memory_timestamp
    else:
        try:
            cached_packages = CachedPackage.query.all()
            if cached_packages:
                normalized_packages = [
                    {
                        'name': pkg.package_name,
                        'version': pkg.version,
                        'latest_absolute_version': pkg.latest_absolute_version,
                        'latest_official_version': pkg.latest_official_version,
                        'author': pkg.author or '',
                        'fhir_version': pkg.fhir_version or '',
                        'url': pkg.url or '',
                        'canonical': pkg.canonical or '',
                        'dependencies': pkg.dependencies or [],
                        'version_count': pkg.version_count or 1,
                        'all_versions': pkg.all_versions or [{'version': pkg.version, 'pubDate': ''}],
                        'registry': pkg.registry or ''
                    } for pkg in cached_packages
                ]
                app.config['MANUAL_PACKAGE_CACHE'] = normalized_packages
                app.config['MANUAL_CACHE_TIMESTAMP'] = db_timestamp or datetime.datetime.now(datetime.timezone.utc)
                display_timestamp = app.config['MANUAL_CACHE_TIMESTAMP']
            else:
                is_fetching = True
        except Exception as db_err:
            logger.error(f"Error loading packages from database: {db_err}", exc_info=True)
            flash("Error loading package cache. Fetching from registries...", "warning")
            is_fetching = True

    if normalized_packages is None:
        try:
            while not log_queue.empty():
                log_queue.get()
            session['fetch_in_progress'] = True
            raw_packages = fetch_packages_from_registries(search_term='')
            if not raw_packages:
                normalized_packages = []
                fetch_failed_flag = True
                session['fetch_failed'] = True
                app.config['MANUAL_PACKAGE_CACHE'] = []
                app.config['MANUAL_CACHE_TIMESTAMP'] = None
            else:
                normalized_packages = normalize_package_data(raw_packages)
                now_ts = datetime.datetime.now(datetime.timezone.utc)
                app.config['MANUAL_PACKAGE_CACHE'] = normalized_packages
                app.config['MANUAL_CACHE_TIMESTAMP'] = now_ts
                cache_packages(normalized_packages, db, CachedPackage)
                if db_timestamp_info:
                    db_timestamp_info.last_fetch_timestamp = now_ts
                else:
                    db_timestamp_info = RegistryCacheInfo(last_fetch_timestamp=now_ts)
                    db.session.add(db_timestamp_info)
                db.session.commit()
                session['fetch_failed'] = False
                fetch_failed_flag = False
                display_timestamp = now_ts
        except Exception as fetch_err:
            logger.error(f"Error fetching packages: {fetch_err}", exc_info=True)
            normalized_packages = []
            fetch_failed_flag = True
            session['fetch_failed'] = True
            app.config['MANUAL_PACKAGE_CACHE'] = []
            app.config['MANUAL_CACHE_TIMESTAMP'] = None
            flash("Error fetching package list.", "error")

    if not isinstance(normalized_packages, list):
        normalized_packages = []
        fetch_failed_flag = True
        session['fetch_failed'] = True

    total_packages = len(normalized_packages)
    start = (page - 1) * per_page
    end = start + per_page
    packages_processed = [
        {**pkg, 'display_version': pkg.get('latest_official_version') or pkg.get('latest_absolute_version') or 'N/A'}
        for pkg in normalized_packages
    ]
    packages_on_page = packages_processed[start:end]
    total_pages = max(1, (total_packages + per_page - 1) // per_page)

    def iter_pages(left_edge=1, left_current=1, right_current=2, right_edge=1):
        pages = []
        last_page = 0
        for i in range(1, min(left_edge + 1, total_pages + 1)):
            pages.append(i)
            last_page = i
        if last_page < page - left_current - 1:
            pages.append(None)
        for i in range(max(last_page + 1, page - left_current), min(page + right_current + 1, total_pages + 1)):
            pages.append(i)
            last_page = i
        if last_page < total_pages - right_edge:
            pages.append(None)
        for i in range(max(last_page + 1, total_pages - right_edge + 1), total_pages + 1):
            pages.append(i)
        return pages

    pagination = SimpleNamespace(
        items=packages_on_page,
        page=page,
        pages=total_pages,
        total=total_packages,
        per_page=per_page,
        has_prev=(page > 1),
        has_next=(page < total_pages),
        prev_num=page - 1 if page > 1 else None,
        next_num=page + 1 if page < total_pages else None,
        iter_pages=iter_pages()
    )

    form = IgImportForm()
    return render_template('search_and_import_ig.html',
                           packages=packages_on_page,
                           pagination=pagination,
                           form=form,
                           fetch_failed=fetch_failed_flag,
                           last_cached_timestamp=display_timestamp,
                           is_fetching=is_fetching)

@app.route('/api/refresh-cache-task', methods=['POST'])
@csrf.exempt
def refresh_cache_task():
    while not log_queue.empty():
        log_queue.get_nowait()
    logger.info("Received API request to refresh cache.")
    thread = threading.Thread(target=perform_cache_refresh_and_log, daemon=True)
    thread.start()
    return jsonify({"status": "accepted", "message": "Cache refresh started."}), 202

def perform_cache_refresh_and_log():
    with app.app_context():
        logger.info("Starting background cache refresh")
        try:
            app.config['MANUAL_PACKAGE_CACHE'] = None
            app.config['MANUAL_CACHE_TIMESTAMP'] = None
            timestamp_info = RegistryCacheInfo.query.first()
            if timestamp_info:
                timestamp_info.last_fetch_timestamp = None
            db.session.query(CachedPackage).delete()
            db.session.flush()
            raw_packages = fetch_packages_from_registries(search_term='')
            if not raw_packages:
                logger.warning("No packages returned from registries.")
                fetch_failed = True
                normalized_packages = []
            else:
                normalized_packages = normalize_package_data(raw_packages)
            now_ts = datetime.datetime.now(datetime.timezone.utc)
            app.config['MANUAL_PACKAGE_CACHE'] = normalized_packages
            app.config['MANUAL_CACHE_TIMESTAMP'] = now_ts
            session['fetch_failed'] = fetch_failed
            if not fetch_failed and normalized_packages:
                cache_packages(normalized_packages, db, CachedPackage)
            if not fetch_failed:
                if timestamp_info:
                    timestamp_info.last_fetch_timestamp = now_ts
                else:
                    timestamp_info = RegistryCacheInfo(last_fetch_timestamp=now_ts)
                    db.session.add(timestamp_info)
                db.session.commit()
            else:
                db.session.rollback()
        except Exception as e:
            db.session.rollback()
            logger.error(f"Critical error during cache refresh: {e}", exc_info=True)
            log_queue.put(f"CRITICAL ERROR: {e}")
        finally:
            log_queue.put("[DONE]")

@app.route('/stream-import-logs')
def stream_import_logs():
    def generate():
        while True:
            try:
                msg = log_queue.get(timeout=300)
                clean_msg = str(msg).replace('INFO:services:', '').replace('INFO:app:', '').strip()
                yield f"data: {clean_msg}\n\n"
                if msg == '[DONE]':
                    break
            except queue.Empty:
                yield "data: ERROR: Timeout waiting for logs.\n\n"
                yield "data: [DONE]\n\n"
                break
    return Response(generate(), mimetype='text/event-stream')

@app.route('/api/search-packages', methods=['GET'])
def api_search_packages():
    search_term = request.args.get('search', '').lower()
    page = request.args.get('page', 1, type=int)
    per_page = 50
    all_cached_packages = app.config.get('MANUAL_PACKAGE_CACHE') or []
    if search_term:
        filtered_packages = [
            pkg for pkg in all_cached_packages
            if search_term in pkg.get('name', '').lower() or search_term in pkg.get('author', '').lower()
        ]
    else:
        filtered_packages = all_cached_packages
    filtered_packages = [
        {**pkg, 'display_version': pkg.get('latest_official_version') or pkg.get('latest_absolute_version') or 'N/A'}
        for pkg in filtered_packages
    ]
    total_filtered = len(filtered_packages)
    start = (page - 1) * per_page
    end = start + per_page
    packages_on_page = filtered_packages[start:end]
    total_pages = max(1, (total_filtered + per_page - 1) // per_page)

    def iter_pages(left_edge=1, left_current=1, right_current=2, right_edge=1):
        pages = []
        last_page = 0
        for i in range(1, min(left_edge + 1, total_pages + 1)):
            pages.append(i)
            last_page = i
        if last_page < page - left_current - 1:
            pages.append(None)
        for i in range(max(last_page + 1, page - left_current), min(page + right_current + 1, total_pages + 1)):
            pages.append(i)
            last_page = i
        if last_page < total_pages - right_edge:
            pages.append(None)
        for i in range(max(last_page + 1, total_pages - right_edge + 1), total_pages + 1):
            pages.append(i)
        return pages

    pagination = SimpleNamespace(
        items=packages_on_page,
        page=page,
        pages=total_pages,
        total=total_filtered,
        per_page=per_page,
        has_prev=(page > 1),
        has_next=(page < total_pages),
        prev_num=page - 1 if page > 1 else None,
        next_num=page + 1 if page < total_pages else None,
        iter_pages=iter_pages()
    )
    return render_template('_search_results_table.html', packages=packages_on_page, pagination=pagination)

@app.route('/import-ig', methods=['GET', 'POST'])
def import_ig():
    form = IgImportForm()
    is_ajax = request.headers.get('HX-Request') == 'true'
    if form.validate_on_submit():
        name = form.package_name.data
        version = form.package_version.data
        dependency_mode = form.dependency_mode.data
        while not log_queue.empty():
            log_queue.get()
        try:
            result = import_package_and_dependencies(name, version, dependency_mode=dependency_mode)
            if result['errors'] and not result['downloaded']:
                error_msg = result['errors'][0]
                simplified_msg = error_msg
                if "HTTP error" in error_msg and "404" in error_msg:
                    simplified_msg = "Package not found (404)."
                elif "HTTP error" in error_msg:
                    simplified_msg = f"Registry error: {error_msg.split(': ', 1)[-1]}"
                elif "Connection error" in error_msg:
                    simplified_msg = "Could not connect to registry."
                flash(f"Failed to import {name}#{version}: {simplified_msg}", "error")
                if is_ajax:
                    return jsonify({"status": "error", "message": simplified_msg}), 400
                return render_template('import_ig.html', form=form, site_name='FHIR Test Data App')
            else:
                if result['errors']:
                    flash(f"Partially imported {name}#{version} with errors.", "warning")
                else:
                    flash(f"Successfully imported {name}#{version}!", "success")
                if is_ajax:
                    return jsonify({"status": "success", "message": f"Imported {name}#{version}", "redirect": url_for('search_and_import')}), 200
                return redirect(url_for('search_and_import'))
        except Exception as e:
            logger.error(f"Unexpected error during IG import: {e}", exc_info=True)
            flash(f"Error importing IG: {e}", "error")
            if is_ajax:
                return jsonify({"status": "error", "message": str(e)}), 500
            return render_template('import_ig.html', form=form, site_name='FHIR Test Data App')
    else:
        for field, errors in form.errors.items():
            for error in errors:
                flash(f"Error in {getattr(form, field).label.text}: {error}", "danger")
        if is_ajax:
            return jsonify({"status": "error", "message": "Form validation failed", "errors": form.errors}), 400
        return render_template('import_ig.html', form=form, site_name='FHIR Test Data App')

@app.route('/package-details/<name>')
def package_details_view(name):
    from services import safe_parse_version
    packages = None
    source = "Not Found"
    in_memory_cache = app.config.get('MANUAL_PACKAGE_CACHE')
    if in_memory_cache:
        cached_data = [pkg for pkg in in_memory_cache if pkg.get('name', '').lower() == name.lower()]
        if cached_data:
            packages = cached_data
            source = "In-Memory Cache"
    if packages is None:
        try:
            db_packages = CachedPackage.query.filter(CachedPackage.package_name.ilike(name)).all()
            if db_packages:
                packages = db_packages
                source = "Database"
        except Exception as db_err:
            logger.error(f"Database error querying package '{name}': {db_err}", exc_info=True)
    if packages is None:
        try:
            raw_packages = fetch_packages_from_registries(search_term=name)
            normalized_packages = normalize_package_data(raw_packages)
            packages = [pkg for pkg in normalized_packages if pkg.get('name', '').lower() == name.lower()]
            source = "Fetched from Registries"
        except Exception as fetch_err:
            logger.error(f"Error fetching package '{name}': {fetch_err}", exc_info=True)
            flash(f"Error fetching package details.", "error")
            return redirect(url_for('search_and_import'))
    if not packages:
        flash(f"Package {name} not found.", "error")
        return redirect(url_for('search_and_import'))
    is_dict_list = isinstance(packages[0], dict)
    package = packages[0]
    latest_absolute_version = package.get('latest_absolute_version') if is_dict_list else package.version
    latest_official_version = package.get('latest_official_version') if is_dict_list else package.latest_official_version
    all_versions = package.get('all_versions', []) if is_dict_list else package.all_versions or []
    dependencies = package.get('dependencies', []) if is_dict_list else package.dependencies or []
    actual_package_name = package.get('name', name) if is_dict_list else package.package_name
    package_json = {
        'name': actual_package_name,
        'version': latest_absolute_version,
        'author': package.get('author') if is_dict_list else package.author,
        'fhir_version': package.get('fhir_version') if is_dict_list else package.fhir_version,
        'canonical': package.get('canonical', '') if is_dict_list else package.canonical or '',
        'dependencies': dependencies,
        'url': package.get('url') if is_dict_list else package.url,
        'registry': package.get('registry', 'https://packages.simplifier.net'),
        'description': get_package_description(actual_package_name, latest_absolute_version, app.config['FHIR_PACKAGES_DIR'])
    }
    versions_sorted = sorted(all_versions, key=lambda x: safe_parse_version(x['version']), reverse=True)
    return render_template('package_details.html',
                           package_json=package_json,
                           dependencies=dependencies,
                           versions=[v['version'] for v in versions_sorted],
                           package_name=actual_package_name,
                           latest_official_version=latest_official_version)

@app.route('/test-data', methods=['GET', 'POST'])
def test_data():
    form = FlaskForm()  # For CSRF protection
    ig_filter = request.form.get('ig_filter') if request.method == 'POST' else None
    try:
        test_resources = TestDataResource.query.all()
        processed_igs = ProcessedIg.query.order_by(ProcessedIg.package_name, ProcessedIg.version).all()
        ig_choices = [('', 'No Filter')] + [(f"{ig.package_name}#{ig.version}", f"{ig.package_name}#{ig.version}") for ig in processed_igs]
    except Exception as e:
        logger.error(f"Error fetching test data or IGs: {e}", exc_info=True)
        flash("Error loading test data.", "error")
        test_resources = []
        ig_choices = [('', 'No Filter')]
    if ig_filter and ig_filter != '':
        try:
            pkg_name, pkg_version = ig_filter.split('#')
            selected_ig = ProcessedIg.query.filter_by(package_name=pkg_name, version=pkg_version).first()
            if selected_ig:
                ig_resource_types = {info['type'] for info in selected_ig.resource_types_info if info.get('type')}
                test_resources = [res for res in test_resources if res.resource_type in ig_resource_types]
        except Exception as e:
            logger.error(f"Error applying IG filter: {e}", exc_info=True)
            flash("Error applying filter.", "error")
    return render_template('test_data.html',
                           form=form,
                           test_resources=test_resources,
                           ig_choices=ig_choices,
                           selected_ig=ig_filter,
                           site_name='FHIR Test Data App')

@app.route('/contribute', methods=['GET', 'POST'])
def contribute():
    form = ContributeTestDataForm()
    if form.validate_on_submit():
        test_file = form.test_file.data
        contributor = form.contributor.data
        filename = secure_filename(test_file.filename)
        temp_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        test_file.save(temp_path)
        try:
            with open(temp_path, 'r', encoding='utf-8') as f:
                resource = json.load(f)
            resource_type = resource.get('resourceType')
            resource_id = resource.get('id')
            if not resource_type or not resource_id:
                raise ValueError("Invalid FHIR resource: missing resourceType or id")
            validation_result = validate_resource_against_hapi(resource, app.config['HAPI_FHIR_URL'])
            if not validation_result.get('valid', False):
                flash(f"Validation failed: {'; '.join(validation_result.get('errors', []))}", "error")
                os.remove(temp_path)
                return render_template('contribute.html', form=form, site_name='FHIR Test Data App')
            metadata = {
                'resource_type': resource_type,
                'resource_id': resource_id,
                'contributor': contributor,
                'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                'validation_status': 'valid',
                'validation_report': validation_result
            }
            metadata_filename = f"{os.path.splitext(filename)[0]}.metadata.json"
            metadata_path = os.path.join(app.config['UPLOAD_FOLDER'], metadata_filename)
            with open(metadata_path, 'w', encoding='utf-8') as f:
                json.dump(metadata, f, indent=2)
            pr_url = create_github_pull_request(
                resource_filename=filename,
                resource_path=temp_path,
                metadata_filename=metadata_filename,
                metadata_path=metadata_path,
                contributor=contributor,
                repo_owner=app.config['GITHUB_REPO_OWNER'],
                repo_name=app.config['GITHUB_REPO_NAME'],
                github_token=app.config['GITHUB_TOKEN']
            )
            flash(f"Test data submitted successfully! PR created: {pr_url}", "success")
            os.remove(temp_path)
            os.remove(metadata_path)
            return redirect(url_for('test_data'))
        except Exception as e:
            logger.error(f"Error processing contribution: {e}", exc_info=True)
            flash(f"Error submitting test data: {e}", "error")
            if os.path.exists(temp_path):
                os.remove(temp_path)
            return render_template('contribute.html', form=form, site_name='FHIR Test Data App')
    return render_template('contribute.html', form=form, site_name='FHIR Test Data App')

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        parse_test_data_folder(app.config['TEST_DATA_FOLDER'])
    app.run(host='0.0.0.0', port=5000, debug=False)
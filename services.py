import requests
import os
import tarfile
import json
import re
import logging
from flask import current_app, Blueprint
from collections import defaultdict
from urllib.parse import quote
from pathlib import Path
from models import db, TestDataResource

services_bp = Blueprint('services', __name__)
logger = logging.getLogger(__name__)

FHIR_REGISTRY_BASE_URL = "https://packages.fhir.org"

try:
    import packaging.version as pkg_version
    HAS_PACKAGING_LIB = True
except ImportError:
    HAS_PACKAGING_LIB = False
    class BasicVersion:
        def __init__(self, v_str): self.v_str = str(v_str)
        def __lt__(self, other): return self.v_str < str(other)
        def __gt__(self, other): return self.v_str > str(other)
        def __eq__(self, other): return self.v_str == str(other)
        def __str__(self): return self.v_str
    pkg_version = SimpleNamespace(parse=BasicVersion, InvalidVersion=ValueError)

def safe_parse_version(v_str):
    if not v_str or not isinstance(v_str, str):
        return pkg_version.parse("0.0.0a0")
    try:
        return pkg_version.parse(v_str)
    except pkg_version.InvalidVersion:
        v_str_norm = v_str.lower()
        base_part = v_str_norm.split('-', 1)[0] if '-' in v_str_norm else v_str_norm
        suffix = v_str_norm.split('-', 1)[1] if '-' in v_str_norm else None
        if re.match(r'^\d+(\.\d+)*$', base_part):
            try:
                if suffix in ['dev', 'snapshot', 'ci-build']: return pkg_version.parse(f"{base_part}a0")
                elif suffix in ['draft', 'ballot', 'preview']: return pkg_version.parse(f"{base_part}b0")
                elif suffix and suffix.startswith('rc'): return pkg_version.parse(f"{base_part}rc{''.join(filter(str.isdigit, suffix)) or '0'}")
                return pkg_version.parse(base_part)
            except pkg_version.InvalidVersion:
                logger.warning(f"Invalid version '{base_part}'. Treating as alpha.")
                return pkg_version.parse("0.0.0a0")
        else:
            logger.warning(f"Unparseable version '{v_str}'. Treating as alpha.")
            return pkg_version.parse("0.0.0a0")

def get_additional_registries():
    feed_registry_url = 'https://raw.githubusercontent.com/FHIR/ig-registry/master/package-feeds.json'
    feeds = []
    try:
        response = requests.get(feed_registry_url, timeout=15)
        response.raise_for_status()
        data = json.loads(response.text)
        feeds = [{'name': feed['name'], 'url': feed['url']} for feed in data.get('feeds', []) if 'name' in feed and 'url' in feed]
    except Exception as e:
        logger.error(f"Error fetching registries: {e}", exc_info=True)
    return feeds

def fetch_packages_from_registries(search_term=''):
    packages_dict = defaultdict(list)
    feed_registries = get_additional_registries()
    if not feed_registries:
        logger.warning("No registries available.")
        return []
    for feed in feed_registries:
        try:
            response = requests.get(feed['url'], timeout=30)
            response.raise_for_status()
            try:
                data = json.loads(response.text)
                for pkg in data.get('packages', []):
                    if not isinstance(pkg, dict) or not pkg.get('name'):
                        continue
                    if search_term and search_term.lower() not in pkg['name'].lower():
                        continue
                    packages_dict[pkg['name']].append(pkg)
            except json.JSONDecodeError:
                logger.warning(f"Non-JSON feed: {feed['name']}")
        except Exception as e:
            logger.error(f"Error fetching feed {feed['name']}: {e}")
    packages = []
    for pkg_name, entries in packages_dict.items():
        versions = [{"version": entry.get('version', ''), "pubDate": entry.get('pubDate', '')} for entry in entries if entry.get('version')]
        versions.sort(key=lambda x: x.get('pubDate', ''), reverse=True)
        if not versions:
            continue
        latest_entry = entries[0]
        package = {
            'name': pkg_name,
            'version': latest_entry.get('version', ''),
            'author': latest_entry.get('author', ''),
            'fhirVersion': latest_entry.get('fhirVersion', ''),
            'url': latest_entry.get('url', ''),
            'canonical': latest_entry.get('canonical', ''),
            'dependencies': latest_entry.get('dependencies', []),
            'versions': versions,
            'registry': latest_entry.get('registry', '')
        }
        packages.append(package)
    return packages

def normalize_package_data(raw_packages):
    packages_grouped = defaultdict(list)
    for entry in raw_packages:
        if not isinstance(entry, dict):
            continue
        raw_name = entry.get('name') or entry.get('title') or ''
        name_part = raw_name.split('#', 1)[0].strip().lower()
        if name_part:
            packages_grouped[name_part].append(entry)
    normalized_list = []
    for name_key, entries in packages_grouped.items():
        latest_absolute_data = None
        latest_official_data = None
        latest_absolute_ver = safe_parse_version("0.0.0a0")
        latest_official_ver = safe_parse_version("0.0.0a0")
        all_versions = []
        package_name_display = name_key
        processed_versions = set()
        for package_entry in entries:
            for version_info in package_entry.get('versions', []):
                if isinstance(version_info, dict) and 'version' in version_info:
                    version_str = version_info.get('version', '')
                    if version_str and version_str not in processed_versions:
                        all_versions.append(version_info)
                        processed_versions.add(version_str)
        processed_entries = []
        for package_entry in entries:
            version_str = package_entry.get('version') or (raw_name.split('#')[1] if '#' in raw_name else '')
            if not version_str:
                continue
            current_display_name = raw_name.split('#')[0].strip()
            if current_display_name and current_display_name != name_key:
                package_name_display = current_display_name
            entry_with_version = package_entry.copy()
            entry_with_version['version'] = version_str
            processed_entries.append(entry_with_version)
            try:
                current_ver = safe_parse_version(version_str)
                if latest_absolute_data is None or current_ver > latest_absolute_ver:
                    latest_absolute_ver = current_ver
                    latest_absolute_data = entry_with_version
                if re.match(r'^\d+\.\d+\.\d+(?:-[a-zA-Z0-9\.]+)?$', version_str):
                    if latest_official_data is None or current_ver > latest_official_ver:
                        latest_official_ver = current_ver
                        latest_official_data = entry_with_version
            except Exception as e:
                logger.error(f"Error comparing version '{version_str}': {e}")
        if latest_absolute_data:
            final_absolute_version = latest_absolute_data.get('version', 'unknown')
            final_official_version = latest_official_data.get('version') if latest_official_data else None
            author = str(latest_absolute_data.get('author') or '')
            fhir_version = latest_absolute_data.get('fhirVersion') or 'unknown'
            url = str(latest_absolute_data.get('url') or '')
            canonical = str(latest_absolute_data.get('canonical') or url)
            dependencies = []
            dependencies_raw = latest_absolute_data.get('dependencies', [])
            if isinstance(dependencies_raw, dict):
                dependencies = [{"name": str(dn), "version": str(dv)} for dn, dv in dependencies_raw.items()]
            elif isinstance(dependencies_raw, list):
                for dep in dependencies_raw:
                    if isinstance(dep, str) and '@' in dep:
                        dep_name, dep_version = dep.split('@', 1)
                        dependencies.append({"name": dep_name, "version": dep_version})
                    elif isinstance(dep, dict) and 'name' in dep:
                        dependencies.append(dep)
            all_versions.sort(key=lambda x: x.get('pubDate', ''), reverse=True)
            normalized_entry = {
                'name': package_name_display,
                'version': final_absolute_version,
                'latest_absolute_version': final_absolute_version,
                'latest_official_version': final_official_version,
                'author': author.strip(),
                'fhir_version': fhir_version.strip(),
                'url': url.strip(),
                'canonical': canonical.strip(),
                'dependencies': dependencies,
                'version_count': len(all_versions),
                'all_versions': all_versions,
                'versions_data': processed_entries,
                'registry': latest_absolute_data.get('registry', '')
            }
            normalized_list.append(normalized_entry)
    normalized_list.sort(key=lambda x: x.get('name', '').lower())
    return normalized_list

def cache_packages(normalized_packages, db, CachedPackage):
    try:
        for package in normalized_packages:
            existing = CachedPackage.query.filter_by(package_name=package['name'], version=package['version']).first()
            if existing:
                existing.author = package['author']
                existing.fhir_version = package['fhir_version']
                existing.version_count = package['version_count']
                existing.url = package['url']
                existing.all_versions = package['all_versions']
                existing.dependencies = package['dependencies']
                existing.latest_absolute_version = package['latest_absolute_version']
                existing.latest_official_version = package['latest_official_version']
                existing.canonical = package['canonical']
                existing.registry = package.get('registry', '')
            else:
                new_package = CachedPackage(
                    package_name=package['name'],
                    version=package['version'],
                    author=package['author'],
                    fhir_version=package['fhir_version'],
                    version_count=package['version_count'],
                    url=package['url'],
                    all_versions=package['all_versions'],
                    dependencies=package['dependencies'],
                    latest_absolute_version=package['latest_absolute_version'],
                    latest_official_version=package['latest_official_version'],
                    canonical=package['canonical'],
                    registry=package.get('registry', '')
                )
                db.session.add(new_package)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error caching packages: {e}")
        raise

def _get_download_dir():
    packages_dir = current_app.config.get('FHIR_PACKAGES_DIR')
    os.makedirs(packages_dir, exist_ok=True)
    return packages_dir

def sanitize_filename_part(text):
    if not isinstance(text, str):
        text = str(text)
    safe_text = "".join(c if c.isalnum() or c in ['.', '-'] else '_' for c in text)
    safe_text = re.sub(r'_+', '_', safe_text).strip('_-.')
    return safe_text or "invalid_name"

def construct_tgz_filename(name, version):
    if not name or not version:
        logger.error(f"Missing name ('{name}') or version ('{version}')")
        return None
    return f"{sanitize_filename_part(name)}-{sanitize_filename_part(version)}.tgz"

def parse_package_filename(filename):
    if not filename or not filename.endswith('.tgz'):
        return None, None
    base_name = filename[:-4]
    version_pattern = r'(\d+\.\d+\.\d+)(?:-(?:preview|ballot|draft|snapshot|alpha|beta|RC\d*|buildnumbersuffix\d*|alpha\d+\.\d+\.\d+|snapshot-\d+|ballot-\d+|alpha\.\d+))?$'
    match = None
    for i in range(len(base_name), 0, -1):
        substring = base_name[:i]
        if re.search(version_pattern, substring):
            match = re.search(version_pattern, base_name[:i])
            if match:
                break
    if not match:
        name = base_name.replace('_', '.')
        version = ""
        return name, version
    version_start_idx = match.start(1)
    name = base_name[:version_start_idx].rstrip('-').replace('_', '.')
    version = base_name[version_start_idx:]
    if not name or not version:
        name = base_name.replace('_', '.')
        version = ""
    return name, version

def get_package_description(package_name, package_version, packages_dir):
    tgz_filename = construct_tgz_filename(package_name, package_version)
    if not tgz_filename:
        return "Error: Could not construct filename."
    tgz_path = os.path.join(packages_dir, tgz_filename)
    if not os.path.exists(tgz_path):
        return f"Error: Package file not found."
    try:
        with tarfile.open(tgz_path, "r:gz") as tar:
            pkg_json_member = next((m for m in tar if m.name == 'package/package.json'), None)
            if pkg_json_member:
                with tar.extractfile(pkg_json_member) as f:
                    pkg_data = json.load(f)
                    return pkg_data.get('description', 'No description found.')
            return "Error: package.json not found."
    except Exception as e:
        logger.error(f"Error reading description: {e}")
        return f"Error reading package details: {e}"

def import_package_and_dependencies(name, version, dependency_mode='recursive'):
    download_dir = _get_download_dir()
    result = {
        'requested': f"{name}#{version}",
        'downloaded': {},
        'dependencies': [],
        'errors': []
    }
    try:
        tgz_filename = construct_tgz_filename(name, version)
        if not tgz_filename:
            result['errors'].append(f"Invalid filename for {name}#{version}")
            return result
        tgz_path = os.path.join(download_dir, tgz_filename)
        if os.path.exists(tgz_path):
            result['downloaded'][name, version] = tgz_path
            logger.info(f"Package {name}#{version} already exists at {tgz_path}")
        else:
            url = f"{FHIR_REGISTRY_BASE_URL}/{quote(name)}/{quote(version)}"
            response = requests.get(url, stream=True, timeout=30)
            response.raise_for_status()
            with open(tgz_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            result['downloaded'][name, version] = tgz_path
            logger.info(f"Downloaded {name}#{version} to {tgz_path}")
        with tarfile.open(tgz_path, "r:gz") as tar:
            pkg_json_member = next((m for m in tar if m.name == 'package/package.json'), None)
            if pkg_json_member:
                with tar.extractfile(pkg_json_member) as f:
                    pkg_data = json.load(f)
                    dependencies = pkg_data.get('dependencies', {})
                    for dep_name, dep_version in dependencies.items():
                        if dependency_mode == 'recursive':
                            dep_result = import_package_and_dependencies(dep_name, dep_version, dependency_mode)
                            result['downloaded'].update(dep_result['downloaded'])
                            result['dependencies'].extend(dep_result['dependencies'])
                            result['errors'].extend(dep_result['errors'])
                        result['dependencies'].append({'name': dep_name, 'version': dep_version})
    except requests.HTTPError as e:
        result['errors'].append(f"HTTP error downloading {name}#{version}: {e}")
    except requests.RequestException as e:
        result['errors'].append(f"Connection error downloading {name}#{version}: {e}")
    except Exception as e:
        result['errors'].append(f"Error importing {name}#{version}: {e}")
        logger.error(f"Import error for {name}#{version}: {e}", exc_info=True)
    return result

def parse_test_data_folder(folder_path):
    if not os.path.exists(folder_path):
        logger.error(f"Test data folder not found: {folder_path}")
        return
    try:
        db.session.query(TestDataResource).delete()
        for root, _, files in os.walk(folder_path):
            for filename in files:
                if filename.lower().endswith('.json'):
                    file_path = os.path.join(root, filename)
                    try:
                        with open(file_path, 'r', encoding='utf-8') as f:
                            content = json.load(f)
                        resource_type = content.get('resourceType')
                        resource_id = content.get('id')
                        if resource_type and resource_id:
                            existing = TestDataResource.query.filter_by(filename=filename).first()
                            if not existing:
                                test_resource = TestDataResource(
                                    filename=filename,
                                    file_path=file_path,
                                    resource_type=resource_type,
                                    resource_id=resource_id
                                )
                                db.session.add(test_resource)
                    except Exception as e:
                        logger.error(f"Error parsing {filename}: {e}")
        db.session.commit()
        logger.info(f"Parsed test data folder: {folder_path}")
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error parsing test data folder: {e}", exc_info=True)

def validate_resource_against_hapi(resource, hapi_fhir_url):
    try:
        headers = {'Content-Type': 'application/fhir+json', 'Accept': 'application/fhir+json'}
        response = requests.post(f"{hapi_fhir_url}/$validate", json=resource, headers=headers, timeout=30)
        response.raise_for_status()
        outcome = response.json()
        errors = []
        warnings = []
        for issue in outcome.get('issue', []):
            severity = issue.get('severity', 'info')
            diagnostics = issue.get('diagnostics') or issue.get('details', {}).get('text', 'No details')
            if severity in ['error', 'fatal']:
                errors.append(diagnostics)
            else:
                warnings.append(diagnostics)
        return {
            'valid': not errors,
            'errors': errors,
            'warnings': warnings
        }
    except Exception as e:
        logger.error(f"Validation error: {e}", exc_info=True)
        return {'valid': False, 'errors': [str(e)], 'warnings': []}

def create_github_pull_request(resource_filename, resource_path, metadata_filename, metadata_path, contributor, repo_owner, repo_name, github_token):
    headers = {'Authorization': f'token {github_token}', 'Accept': 'application/vnd.github.v3+json'}
    branch_name = f"add-test-data-{contributor.lower().replace(' ', '-')}-{int(datetime.datetime.now().timestamp())}"
    base_url = f"https://api.github.com/repos/{repo_owner}/{repo_name}"

    # Get default branch
    repo_response = requests.get(base_url, headers=headers)
    repo_response.raise_for_status()
    default_branch = repo_response.json()['default_branch']

    # Get HEAD SHA of default branch
    ref_response = requests.get(f"{base_url}/git/ref/heads/{default_branch}", headers=headers)
    ref_response.raise_for_status()
    head_sha = ref_response.json()['object']['sha']

    # Create new branch
    create_ref = requests.post(
        f"{base_url}/git/refs",
        headers=headers,
        json={'ref': f'refs/heads/{branch_name}', 'sha': head_sha}
    )
    create_ref.raise_for_status()

    # Upload resource file
    with open(resource_path, 'r', encoding='utf-8') as f:
        resource_content = f.read()
    resource_b64 = base64.b64encode(resource_content.encode('utf-8')).decode('utf-8')
    resource_commit = requests.put(
        f"{base_url}/contents/test-data/{resource_filename}",
        headers=headers,
        json={
            'message': f"Add test data {resource_filename} by {contributor}",
            'content': resource_b64,
            'branch': branch_name
        }
    )
    resource_commit.raise_for_status()

    # Upload metadata file
    with open(metadata_path, 'r', encoding='utf-8') as f:
        metadata_content = f.read()
    metadata_b64 = base64.b64encode(metadata_content.encode('utf-8')).decode('utf-8')
    metadata_commit = requests.put(
        f"{base_url}/contents/test-data/{metadata_filename}",
        headers=headers,
        json={
            'message': f"Add metadata for {resource_filename} by {contributor}",
            'content': metadata_b64,
            'branch': branch_name
        }
    )
    metadata_commit.raise_for_status()

    # Create pull request
    pr_response = requests.post(
        f"{base_url}/pulls",
        headers=headers,
        json={
            'title': f"Add test data {resource_filename} by {contributor}",
            'head': branch_name,
            'base': default_branch,
            'body': f"Submitted by {contributor}. Includes {resource_filename} and metadata."
        }
    )
    pr_response.raise_for_status()
    return pr_response.json()['html_url']
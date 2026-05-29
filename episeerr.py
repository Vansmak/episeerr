__version__ = "3.7.7"
from flask import Flask, render_template, request, redirect, url_for, jsonify, session
import subprocess
import os
import atexit
import re
import time
import logging
import json
import shutil
import sonarr_utils
from flask import current_app
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
import threading
import shutil
from threading import Lock
from functools import lru_cache
from logging.handlers import RotatingFileHandler
import requests
import episeerr_utils
from episeerr_utils import EPISEERR_DEFAULT_TAG_ID, EPISEERR_SELECT_TAG_ID, normalize_url, http
import pending_deletions
from dashboard import dashboard_bp
from webhooks import sonarr_webhooks_bp, radarr_webhooks_bp
import media_processor
from settings_db import (
    save_service, get_service, delete_service,
    update_service_test_result, get_all_services,
    set_setting, get_setting,
    add_pending_request, get_pending_request, get_all_pending_requests,
    delete_pending_request, find_pending_request_by_series,
    find_pending_request_by_tmdb, migrate_pending_requests_from_files,
)
from logging_config import main_logger as logger
# Import plugin system
from integrations import get_integration, get_all_integrations
from integrations import register_integration_blueprints
app = Flask(__name__)

register_integration_blueprints(app)
app.register_blueprint(dashboard_bp)
app.register_blueprint(sonarr_webhooks_bp)
app.register_blueprint(radarr_webhooks_bp)

# Session / Auth configuration
_secret = os.getenv('SECRET_KEY')
if not _secret:
    app.logger.warning("SECRET_KEY not set — sessions will not survive restarts. Set SECRET_KEY env var.")
    _secret = os.urandom(24).hex()
app.config['SECRET_KEY'] = _secret
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SECURE'] = os.getenv('SESSION_SECURE', 'false').lower() == 'true'
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_DOMAIN'] = None  # Accept cookies on any domain/IP
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(
    seconds=int(os.getenv('AUTH_SESSION_TIMEOUT', '86400'))
)


# Load environment variables
load_dotenv()
BASE_DIR = os.getcwd()

# Initialize settings database
from settings_db import get_sonarr_config, get_radarr_config, init_settings_db
init_settings_db()

# Sonarr variables (with DB support)
sonarr_config = get_sonarr_config()
SONARR_URL = normalize_url(sonarr_config.get('url')) if sonarr_config else None
SONARR_API_KEY = sonarr_config.get('api_key') if sonarr_config else None

# TMDB API Key - check database first
tmdb_service = get_service('tmdb', 'default')
TMDB_API_KEY = tmdb_service['api_key'] if tmdb_service else os.getenv('TMDB_API_KEY')
app.config['TMDB_API_KEY'] = TMDB_API_KEY
if app.config['TMDB_API_KEY']:
    app.logger.info("TMDB_API_KEY is set - request system will function normally")
else:
    app.logger.warning("TMDB_API_KEY is missing - you may encounter issues fetching series details and seasons")

# Request storage
REQUESTS_DIR = os.path.join(os.getcwd(), 'data', 'pending_requests')
os.makedirs(REQUESTS_DIR, exist_ok=True)

LAST_PROCESSED_FILE = os.path.join(os.getcwd(), 'data', 'last_processed.json')
os.makedirs(os.path.dirname(LAST_PROCESSED_FILE), exist_ok=True)


# ============================================================================
# AUTHENTICATION
# ============================================================================

# Endpoints that are always accessible without authentication
_AUTH_EXEMPT_ENDPOINTS = {
    'login',
    'logout',
    'static',
    # Sonarr + legacy Tautulli webhooks (Blueprint endpoints)
    'sonarr_webhooks.process_sonarr_webhook',
    'sonarr_webhooks.handle_server_webhook',
    # Radarr webhook
    'radarr_webhooks.process_radarr_webhook',
    # Integration webhooks (Blueprint endpoints — external services can't authenticate)
    'jellyfin_integration.jellyfin_webhook',
    'emby_integration.emby_webhook',
    'seerr_integration.seerr_webhook',
    'plex_integration.webhook',
    'tautulli_integration.tautulli_webhook',
}


@app.before_request
def check_authentication():
    """Global authentication gate. Skipped when REQUIRE_AUTH != 'true'."""
    if not os.getenv('REQUIRE_AUTH', 'false').lower() == 'true':
        return None

    if os.getenv('AUTH_BYPASS_LOCALHOST', 'true').lower() == 'true':
        if request.remote_addr in ('127.0.0.1', '::1'):
            return None

    # endpoint is None when URL doesn't match any route (404)
    if not request.endpoint or request.endpoint in _AUTH_EXEMPT_ENDPOINTS:
        return None

    if not session.get('authenticated'):
        # API paths and AJAX calls always get 401, never a redirect
        if (request.path.startswith('/api/')
                or request.headers.get('X-Requested-With') == 'XMLHttpRequest'
                or request.is_json):
            return jsonify({'error': 'Unauthorized', 'redirect': url_for('login')}), 401
        return redirect(url_for('login', next=request.url))

    return None


@app.route('/login', methods=['GET', 'POST'])
def login():
    """Login page using username/password authentication."""
    if not os.getenv('REQUIRE_AUTH', 'false').lower() == 'true':
        return redirect(url_for('index'))

    if session.get('authenticated'):
        next_url = request.args.get('next')
        return redirect(next_url or url_for('index'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()

        valid_username = os.getenv('AUTH_USERNAME', 'admin').strip()
        valid_password = os.getenv('AUTH_PASSWORD', '').strip()

        if not valid_password:
            return render_template('login.html',
                                   error='Authentication not configured — set AUTH_PASSWORD environment variable',
                                   username=username)

        if username == valid_username and password == valid_password:
            session['authenticated'] = True
            session['username'] = username
            session.permanent = True
            app.logger.info(f"Successful login for user '{username}' from {request.remote_addr}")
            next_url = request.args.get('next')
            return redirect(next_url or url_for('index'))

        app.logger.warning(f"Failed login attempt for user '{username}' from {request.remote_addr}")
        return render_template('login.html',
                               error='Invalid username or password',
                               username=username)

    return render_template('login.html')


@app.route('/logout')
def logout():
    """Clear session and redirect to login."""
    session.clear()
    return redirect(url_for('login'))


def reload_module_configs():
    """Reload configuration in all modules after saving to database"""
    try:
        import importlib
        import sonarr_utils
        import media_processor
        
        # Reload the modules to pick up new database config
        importlib.reload(sonarr_utils)
        importlib.reload(media_processor)
        
        app.logger.info("Reloaded module configurations from database")
    except Exception as e:
        app.logger.error(f"Failed to reload module configs: {e}")

def get_smart_url(service_data, req):
    """Return the best URL for the current request context (HTTP vs HTTPS)."""
    primary_url = service_data.get('url', '') or ''
    config = service_data.get('config') or {}
    alternate_url = config.get('alternate_url', '') or ''
    is_https = (
        req.is_secure or
        req.headers.get('X-Forwarded-Proto') == 'https' or
        req.headers.get('X-Forwarded-Ssl') == 'on'
    )
    if is_https and alternate_url:
        return alternate_url
    return primary_url or None


def get_smart_url_for_link(link, req):
    """Return the best URL for a quick_link dict."""
    alternate_url = link.get('alternate_url', '') or ''
    is_https = (
        req.is_secure or
        req.headers.get('X-Forwarded-Proto') == 'https' or
        req.headers.get('X-Forwarded-Ssl') == 'on'
    )
    if is_https and alternate_url:
        return alternate_url
    return link.get('url', '')


# ---------------------------------------------------------------------------
# Container liveness filter (cached 30 s)
# ---------------------------------------------------------------------------
_container_cache: dict = {'data': None, 'ts': 0.0}
_CONTAINER_CACHE_TTL = 30


def get_running_containers():
    """Return (running_names: set[str], running_ports: set[int]) or (None, None) if Docker unavailable."""
    import time
    global _container_cache
    now = time.time()
    if _container_cache['data'] is not None and now - _container_cache['ts'] < _CONTAINER_CACHE_TTL:
        return _container_cache['data']
    try:
        from integrations.docker import _docker_get
        from settings_db import get_service as _gs
        docker_svc = _gs('docker', 'default')
        if not docker_svc:
            raise RuntimeError('Docker not configured')
        cfg = docker_svc.get('config') or {}
        host = cfg.get('docker_host') or docker_svc.get('url') or 'unix:///var/run/docker.sock'
        containers = _docker_get(host, '/containers/json')  # no all=true → running only
        running_names, running_ports = set(), set()
        for c in containers:
            for n in c.get('Names', []):
                running_names.add(n.lstrip('/').lower())
            for p in c.get('Ports', []):
                if p.get('PublicPort'):
                    running_ports.add(p['PublicPort'])
        result = (running_names, running_ports)
    except Exception as e:
        app.logger.debug(f'Container liveness check unavailable: {e}')
        result = (None, None)
    _container_cache = {'data': result, 'ts': now}
    return result


def is_container_running(url, running_names, running_ports, name=None):
    """True if the service appears to be running, or if Docker info is unavailable."""
    if running_names is None:
        return True  # Docker not configured/reachable — show everything
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url or '')
        host = (parsed.hostname or '').lower()
        is_local = (
            host in ('localhost', '127.0.0.1') or
            host.startswith('192.168.') or
            host.startswith('10.') or
            any(host.startswith(f'172.{i}.') for i in range(16, 32))
        )
        if not is_local:
            return True  # External / public URL — always show
        if parsed.port and parsed.port in running_ports:
            return True
    except Exception:
        return True
    # Port check failed (host-networked containers don't expose ports) — try name
    if name:
        nl = name.lower()
        for cname in running_names:
            if nl in cname or cname in nl:
                return True
    return False


def auto_add_quick_link(name, url, icon, open_in_iframe=False, alternate_url=None):
    """Automatically add or update a service quick link"""
    from settings_db import get_all_quick_links, add_quick_link, delete_quick_link

    existing_links = get_all_quick_links()
    normalized_url = url.rstrip('/').lower()

    for link in existing_links:
        if link['url'].rstrip('/').lower() == normalized_url:
            app.logger.debug(f"Quick link for {name} already exists (ID: {link['id']})")
            # Recreate if iframe or alternate_url changed
            if (link.get('open_in_iframe') != open_in_iframe or
                    (link.get('alternate_url') or '') != (alternate_url or '')):
                delete_quick_link(link['id'])
                new_id = add_quick_link(name, url, icon, open_in_iframe, alternate_url)
                app.logger.info(f"Updated quick link {name} (ID: {new_id})")
            return

    add_quick_link(name, url, icon, open_in_iframe, alternate_url)
    app.logger.info(f"Auto-added {name} to quick links")

@app.route('/setup')
def setup():
    """Service setup page"""
    # Get all service configurations (existing)
    sonarr = get_service('sonarr', 'default')
    tautulli = get_service('tautulli', 'default')
    tmdb = get_service('tmdb', 'default')

    def _svc_enabled(service_type):
        try:
            import sqlite3 as _sql
            from settings_db import DB_PATH as _DB
            row = _sql.connect(_DB).execute(
                "SELECT enabled FROM services WHERE service_type = ? AND name = 'default'",
                (service_type,)
            ).fetchone()
            return bool(row[0]) if row is not None else True
        except Exception:
            return True

    sonarr_enabled = _svc_enabled('sonarr')
    tmdb_enabled   = _svc_enabled('tmdb')

        # NEW: Get integration configurations with guaranteed fields
    integration_configs = {}
    for integration in get_all_integrations():
        config = get_service(integration.service_name, 'default')
        
        # Force fallback fields — ignore whatever get_setup_fields() returns for now
        setup_fields = [
            {
                'name': 'url',
                'label': 'Service URL',
                'type': 'url',
                'placeholder': f'http://localhost:{getattr(integration, "default_port", 80)}',
                'required': True,
                'help': 'Full base URL including http(s):// and port'
            },
            {
                'name': 'apikey',
                'label': 'API Key / Token',
                'type': 'text',
                'placeholder': 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx',
                'required': True,
                'help': f'From {integration.display_name} settings'
            }
        ]
        
        # If the integration actually defines custom fields, use them instead
        try:
            custom = integration.get_setup_fields()
            if isinstance(custom, list) and len(custom) > 0:
                setup_fields = custom
                print(f"Using CUSTOM fields for {integration.service_name} ({len(custom)} fields)")
            else:
                print(f"Using FALLBACK fields for {integration.service_name} (2 fields)")
        except Exception as e:
            print(f"Error reading custom fields for {integration.service_name}: {e} — using fallback")
        
        # Pre-fill values — flatten everything into one dict
        saved_values = {}
        if config:
            saved_values['url'] = config.get('url')
            saved_values['apikey'] = config.get('api_key')
            # Merge any old-style config dict
            if isinstance(config.get('config'), dict):
                saved_values.update(config['config'])
        
        integration_configs[integration.service_name] = {
            'connected': config is not None,
            'enabled': _svc_enabled(integration.service_name),
            'url': saved_values.get('url'),
            'apikey': saved_values.get('apikey'),
            'integration': integration,
            'setup_fields': setup_fields,
            'saved_values': saved_values  # for template pre-fill
        }
    
    # Check if setup is complete — needs Sonarr + at least one media server
    plex_svc     = get_service('plex',     'default')
    jellyfin_svc = get_service('jellyfin', 'default')
    emby_svc     = get_service('emby',     'default')
    has_media_server = bool(tautulli or plex_svc or jellyfin_svc or emby_svc)
    setup_complete = bool(sonarr and has_media_server)
    
    # Quick links
    from settings_db import get_all_quick_links
    quick_links = get_all_quick_links()
    
    sonarr_config = (sonarr.get('config') or {}) if sonarr else {}

    return render_template('setup.html',
        setup_complete=setup_complete,
        sonarr_enabled=sonarr_enabled,
        sonarr_connected=sonarr is not None,
        sonarr_url=sonarr['url'] if sonarr else None,
        sonarr_apikey=sonarr['api_key'] if sonarr else None,
        sonarr_alternate_url=sonarr_config.get('alternate_url', ''),
        sonarr_open_in_iframe=sonarr_config.get('open_in_iframe', False),
        tmdb_enabled=tmdb_enabled,
        tmdb_connected=tmdb is not None,
        tmdb_apikey=tmdb['api_key'] if tmdb else None,
        integrations=get_all_integrations(),
        integration_configs=integration_configs,
        quick_links=quick_links
    )

# Replace test_connection in episeerr.py with this version:

@app.route('/api/test-connection/<service>', methods=['POST'])
def test_connection(service):
    data = request.json
    
    # Try integration first
    integration = get_integration(service)
    if integration:
        try:
            # Extract ALL fields prefixed with {service}-
            integration_data = {}
            for key, value in data.items():
                if key.startswith(f'{service}-'):
                    field_name = key[len(f'{service}-'):]
                    integration_data[field_name] = value.strip() if isinstance(value, str) else value
            
            # Smart field detection
            url = (integration_data.get('url') or 
                   integration_data.get('service_url') or 
                   integration_data.get('base_url') or
                   '')
            
            api_key = (integration_data.get('apikey') or 
                       integration_data.get('api_key') or 
                       integration_data.get('token') or
                       integration_data.get('key') or
                       integration_data.get('path') or
                       '')
            
            # Allow test if we have url, api_key, or any other integration field (e.g. docker_host)
            has_data = api_key or url or any(v for v in integration_data.values() if v)
            if has_data:
                success, message = integration.test_connection(url, api_key)
                
                if success:
                    update_service_test_result(service, 'default', 'success')
                    return jsonify({'status': 'success', 'message': message or 'Connection successful'})
                else:
                    update_service_test_result(service, 'default', 'failed')
                    return jsonify({'status': 'error', 'message': message or 'Connection failed'}), 400
        except Exception as e:
            app.logger.error(f"Integration test error for {service}: {e}")
            # Fall through to legacy handlers
    
    # Legacy service handlers (keep existing code for sonarr, jellyfin, etc.)
    try:
        if service == 'sonarr':
            url = data.get('sonarr-url')
            api_key = data.get('sonarr-apikey')
            
            if not url or not api_key:
                return jsonify({'status': 'error', 'message': 'URL and API key are required'}), 400
            
            response = http.get(f"{url}/api/v3/system/status", 
                                  headers={'X-Api-Key': api_key}, timeout=10)
            response.raise_for_status()
            
            update_service_test_result('sonarr', 'default', 'success')
            return jsonify({'status': 'success', 'message': 'Connected to Sonarr successfully'})
        
        elif service == 'tautulli':
            url = data.get('tautulli-url')
            api_key = data.get('tautulli-apikey')
            
            if not url or not api_key:
                return jsonify({'status': 'error', 'message': 'URL and API key are required'}), 400
            
            response = http.get(f"{url}/api/v2",
                                  params={'apikey': api_key, 'cmd': 'get_server_info'},
                                  timeout=10)
            response.raise_for_status()
            
            update_service_test_result('tautulli', 'default', 'success')
            return jsonify({'status': 'success', 'message': 'Connected to Tautulli successfully'})
        
        
    except Exception as e:
        app.logger.error(f"Test connection error for {service}: {e}")
        update_service_test_result(service, 'default', 'failed')
        return jsonify({'status': 'error', 'message': f'Connection failed: {str(e)}'}), 400

# Replace save_service_config in episeerr.py with this:

@app.route('/api/toggle-service/<service>', methods=['POST'])
def toggle_service_enabled(service):
    """Enable or disable a service without changing its config."""
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get('enabled', True))
    try:
        import sqlite3 as _sql
        from settings_db import DB_PATH as _DB
        conn = _sql.connect(_DB)
        result = conn.execute(
            "UPDATE services SET enabled = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE service_type = ? AND name = 'default'",
            (1 if enabled else 0, service)
        )
        conn.commit()
        conn.close()
        if result.rowcount == 0:
            return jsonify({'ok': False, 'error': f'Service {service!r} not found in DB'}), 404
        app.logger.info("Service %s %s", service, "enabled" if enabled else "disabled")
        return jsonify({'ok': True, 'service': service, 'enabled': enabled})
    except Exception as exc:
        app.logger.error("toggle_service_enabled %s: %s", service, exc)
        return jsonify({'ok': False, 'error': str(exc)}), 500


# Replace save_service_config in episeerr.py:

@app.route('/api/save-service/<service>', methods=['POST'])
def save_service_config(service):
    data = request.json
    
    # Try integration first
    integration = get_integration(service)
    if integration:
        try:
            # Extract ALL fields prefixed with {service}-
            integration_data = {}
            for key, value in data.items():
                if key.startswith(f'{service}-'):
                    field_name = key[len(f'{service}-'):]
                    integration_data[field_name] = value.strip() if isinstance(value, str) else value
            
            # Smart field detection
            url = (integration_data.get('url') or 
                   integration_data.get('service_url') or 
                   integration_data.get('base_url') or 
                   '')
            
            api_key = (integration_data.get('apikey') or 
                       integration_data.get('api_key') or 
                       integration_data.get('token') or
                       integration_data.get('key') or
                       integration_data.get('path') or
                       '')
            
            # Check if we got any data at all
            if not integration_data:
                # No integration fields found, fall through to legacy handlers
                pass
            elif not api_key and not url:
                # Neither api_key nor url — check for other meaningful fields (e.g. docker_host)
                other_data = {k: v for k, v in integration_data.items()
                              if k not in ('url', 'api_key', 'apikey', 'open_in_iframe') and v}
                if not other_data:
                    return jsonify({
                        'status': 'error',
                        'message': 'At least a URL or API key is required'
                    }), 400
                # Fall through — has other required fields (like docker_host)
                from integrations import get_integration as _get_int
                _int = _get_int(service)
                normalized_data = integration_data.copy()
                save_service(
                    service_type=service,
                    name='default',
                    url=url,
                    api_key=api_key,
                    config=normalized_data
                )
                reload_module_configs()
                if _int and hasattr(_int, 'on_after_save'):
                    _int.on_after_save(normalized_data)
                return jsonify({
                    'status': 'success',
                    'message': f'{integration.display_name} saved successfully'
                })
            else:
                # We have valid integration data - save it
                # Normalize field names for storage
                normalized_data = integration_data.copy()
                if 'apikey' in normalized_data and 'api_key' not in normalized_data:
                    normalized_data['api_key'] = normalized_data['apikey']
                elif 'api_key' in normalized_data and 'apikey' not in normalized_data:
                    normalized_data['apikey'] = normalized_data['api_key']
                
                # Allow integration to reshape data before saving (e.g. nest sync fields)
                if hasattr(integration, 'preprocess_save_data'):
                    integration.preprocess_save_data(normalized_data)

                # Save to database
                save_service(
                    service_type=service,  # Changed from service_name
                    name='default',        # Changed from instance
                    url=url,
                    api_key=api_key,
                    config=normalized_data
                )
                
                # Auto-add/update quick links if URL provided
                if url and url.startswith('http'):
                    try:
                        open_in_iframe = normalized_data.get('open_in_iframe', False)
                        alternate_url = normalized_data.get('alternate_url') or None
                        auto_add_quick_link(
                            integration.display_name,
                            url,
                            integration.icon,
                            open_in_iframe,
                            alternate_url
                        )
                    except Exception as e:
                        app.logger.warning(f"Failed to add/update quick link: {e}")
                
                reload_module_configs()

                # Allow integration to react post-save (e.g. start/stop scheduler)
                if hasattr(integration, 'on_after_save'):
                    integration.on_after_save(normalized_data)

                return jsonify({
                    'status': 'success',
                    'message': f'{integration.display_name} saved successfully'
                })
                
        except Exception as e:
            app.logger.error(f"Integration save error for {service}: {e}")
            return jsonify({
                'status': 'error',
                'message': f'Error saving: {str(e)}'
            }), 500
    
    # Legacy service handlers (if no integration found or integration had no data)
    try:
        if service == 'sonarr':
            url = data.get('sonarr-url')
            apikey = data.get('sonarr-apikey')
            open_in_iframe = data.get('sonarr-open_in_iframe', False)
            alternate_url = data.get('sonarr-alternate_url') or None

            if not url or not apikey:
                return jsonify({'status': 'error', 'message': 'URL and API key required'}), 400

            save_service('sonarr', 'default', url, apikey,
                         config={'alternate_url': alternate_url, 'open_in_iframe': open_in_iframe})
            auto_add_quick_link('Sonarr', url,
                                'https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/sonarr.png',
                                open_in_iframe, alternate_url)

            return jsonify({
                'status': 'success',
                'message': 'Sonarr saved successfully'
            })



        elif service == 'tautulli':
            url = data.get('tautulli-url')
            apikey = data.get('tautulli-apikey')
            open_in_iframe = data.get('tautulli-open_in_iframe', False)
            alternate_url = data.get('tautulli-alternate_url') or None

            if not url or not apikey:
                return jsonify({'status': 'error', 'message': 'URL and API key required'}), 400

            save_service('tautulli', 'default', url, apikey,
                         config={'alternate_url': alternate_url, 'open_in_iframe': open_in_iframe})
            auto_add_quick_link('Tautulli', url,
                                'https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/tautulli.png',
                                open_in_iframe, alternate_url)
            
            return jsonify({
                'status': 'success',
                'message': 'Tautulli saved successfully'
            })
        
        elif service == 'tmdb':
            apikey = data.get('tmdb-apikey')

            if not apikey:
                return jsonify({'status': 'error', 'message': 'API key required'}), 400

            save_service('tmdb', 'default', '', apikey)

            return jsonify({
                'status': 'success',
                'message': 'TMDB saved successfully'
            })

        else:
            return jsonify({
                'status': 'error',
                'message': f'Unknown service: {service}'
            }), 400
            
    except Exception as e:
        app.logger.error(f"Save service error for {service}: {e}")
        return jsonify({
            'status': 'error',
            'message': f'Error saving: {str(e)}'
        }), 500

@app.route('/api/quick-links', methods=['GET', 'POST'])
def manage_quick_links():
    """Get or add quick links"""
    from settings_db import get_all_quick_links, add_quick_link

    if request.method == 'GET':
        links = get_all_quick_links()
        running_names, running_ports = get_running_containers()
        result = []
        for link in links:
            # Custom (manually-added) links always show; auto-added links require container check
            if link.get('custom') or is_container_running(link.get('url', ''), running_names, running_ports, name=link.get('name')):
                link['url'] = get_smart_url_for_link(link, request)
                result.append(link)
        return jsonify({'status': 'success', 'links': result})

    else:  # POST
        data = request.json
        link_id = add_quick_link(
            data.get('name'),
            data.get('url'),
            data.get('icon', 'fas fa-link'),
            data.get('open_in_iframe', False),
            data.get('alternate_url') or None,
            custom=True
        )
        return jsonify({'status': 'success', 'id': link_id})

# 2. ADD this NEW route after delete_quick_link_route (around line 586)

@app.route('/iframe/<int:service_id>')
def iframe_view(service_id):
    """Display a service in an iframe.
    Optional ?url= param allows opening a deep-link within the service instead of its root URL.
    The provided URL must start with the service's configured base URL for security.
    """
    from settings_db import get_quick_link_by_id

    service = get_quick_link_by_id(service_id)
    if not service:
        return "Service not found", 404

    smart_url = get_smart_url_for_link(service, request)

    if not service.get('open_in_iframe'):
        return redirect(smart_url)

    # Allow a specific deep-link via ?path=/series/foo (preferred) or ?url=http://...
    deep_path = request.args.get('path', '').strip()
    deep_url  = request.args.get('url', '').strip()

    if deep_path and deep_path.startswith('/') and '..' not in deep_path:
        display_url = smart_url.rstrip('/') + deep_path
    elif deep_url:
        # Accept if it matches any configured base URL for this service
        base_urls = [u for u in [service.get('url', ''), service.get('alternate_url', ''), smart_url] if u]
        if any(deep_url.startswith(b.rstrip('/')) for b in base_urls):
            display_url = deep_url
        else:
            display_url = smart_url
    else:
        display_url = smart_url

    return render_template('iframe_view.html', service={**service, 'url': display_url})


@app.route('/api/services-sidebar')
def services_sidebar():
    """Get all configured integrations for sidebar display"""
    from settings_db import get_service
    from integrations import get_all_integrations

    services = []

    for integration in get_all_integrations():
        config = get_service(integration.service_name, 'default')
        if config:  # Only include if configured
            open_in_iframe = False
            if config.get('config'):
                open_in_iframe = config['config'].get('open_in_iframe', False)

            service_entry = {
                'id': f'integration-{integration.service_name}',
                'name': integration.display_name,
                'icon': integration.icon,
                'service_type': integration.service_name,
                'open_in_iframe': open_in_iframe,
                'iframe_url': f'/iframe-service/{integration.service_name}'
            }

            # Only include url if it exists (e.g. Docker may have no web UI URL)
            if config.get('url'):
                service_entry['url'] = config['url']

            services.append(service_entry)
    
    return jsonify({'status': 'success', 'services': services})


@app.route('/api/media-server')
def get_media_server():
    """Get all configured media servers (Plex, Jellyfin, Emby)."""
    from integrations import get_integration
    from settings_db import get_service

    media_servers = []
    running_names, running_ports = get_running_containers()

    # Plex (direct integration)
    plex_data = get_service('plex', 'default')
    if plex_data and is_container_running(plex_data.get('url', ''), running_names, running_ports, name='plex'):
        media_servers.append({
            'type': 'plex',
            'name': 'Plex',
            'url': get_smart_url(plex_data, request),
            'icon': 'https://www.plex.tv/wp-content/themes/plex/assets/img/plex-logo.svg'
        })

    # Tautulli — kept for backward compat; users may still rely on this link
    tautulli = get_service('tautulli', 'default')
    if tautulli and is_container_running(tautulli.get('url', ''), running_names, running_ports, name='tautulli'):
        media_servers.append({
            'type': 'tautulli',
            'name': 'Tautulli',
            'url': get_smart_url(tautulli, request),
            'icon': 'https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/tautulli.png'
        })

    # Jellyfin
    jellyfin = get_integration('jellyfin')
    if jellyfin:
        data = get_service('jellyfin', 'default')
        if data and is_container_running(data.get('url', ''), running_names, running_ports, name='jellyfin'):
            media_servers.append({
                'type': 'jellyfin',
                'name': 'Jellyfin',
                'url': get_smart_url(data, request),
                'icon': 'https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/jellyfin.png'
            })

    # Emby
    emby = get_integration('emby')
    if emby:
        data = get_service('emby', 'default')
        if data and is_container_running(data.get('url', ''), running_names, running_ports, name='emby'):
            media_servers.append({
                'type': 'emby',
                'name': 'Emby',
                'url': get_smart_url(data, request),
                'icon': 'https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/emby.png'
            })

    return jsonify(media_servers)


@app.route('/api/optional-integrations')
def get_optional_integrations():
    """Get configured optional integrations (excludes media servers)."""
    from integrations import get_all_integrations
    from settings_db import get_service

    # Exclude media server integrations from the optional list — they appear in /api/media-server
    media_servers = {'jellyfin', 'emby', 'plex', 'tautulli'}
    connected = []
    running_names, running_ports = get_running_containers()

    for integration in get_all_integrations():
        service_name = integration.service_name
        if service_name in media_servers:
            continue
        data = get_service(service_name, 'default')
        # Only include if configured AND has a URL AND container is running
        if data and data.get('url') and is_container_running(data.get('url', ''), running_names, running_ports, name=service_name):
            config = data.get('config') or {}
            connected.append({
                'name': integration.display_name,
                'url': get_smart_url(data, request),
                'icon': integration.icon,
                'service_name': service_name,
                'open_in_iframe': bool(config.get('open_in_iframe', False)),
                'iframe_url': f'/iframe-service/{service_name}'
            })

    return jsonify(connected)


@app.route('/iframe-service/<service_type>')
def iframe_service_view(service_type):
    """Display a configured service in an iframe"""
    from settings_db import get_service
    
    service = get_service(service_type, 'default')
    
    if not service:
        return "Service not found", 404
    
    # Check if this service is configured for iframe
    open_in_iframe = False
    if service.get('config'):
        open_in_iframe = service['config'].get('open_in_iframe', False)
    
    smart_url = get_smart_url(service, request)

    if not open_in_iframe:
        return redirect(smart_url)

    return render_template('iframe_view.html', service={
        'name': service_type.replace('_', ' ').title(),
        'url': smart_url
    })

@app.route('/api/quick-links/<int:link_id>', methods=['DELETE'])
def delete_quick_link_route(link_id):
    """Delete a quick link"""
    from settings_db import delete_quick_link
    delete_quick_link(link_id)
    return jsonify({'status': 'success'})


@app.route('/api/invalidate-container-cache', methods=['POST'])
def invalidate_container_cache():
    """Force next sidebar fetch to query Docker fresh (called after container start/stop)."""
    global _container_cache
    _container_cache['ts'] = 0.0
    return jsonify({'status': 'ok'})

# Configuration management
config_path = os.path.join(app.root_path, 'config', 'config.json')

def get_tmdb_endpoint(endpoint, params=None):
    """Make a request to any TMDB endpoint with the given parameters."""
    base_url = f"https://api.themoviedb.org/3/{endpoint}"
    if params is None:
        params = {}
    
    # Check if we have a v3 or v4 token
    auth_token = TMDB_API_KEY.strip('"\'') if TMDB_API_KEY else ""
    
    try:
        headers = {}
        # If it's a long token (v4), use it as a bearer token
        if len(auth_token) > 40:
            headers["Authorization"] = f"Bearer {auth_token}"
        else:
            # Otherwise use as API key in params (v3)
            params['api_key'] = auth_token
        
        response = http.get(base_url, params=params, headers=headers)
        
        if response.status_code == 200:
            return response.json()
        else:
            app.logger.error(f"Error fetching {endpoint}: {response.status_code}")
            return None
    except Exception as e:
        app.logger.error(f"Exception during API request: {str(e)}")
        return None

def search_tv_shows(query):
    """Search for TV shows using TMDB API."""
    return get_tmdb_endpoint("search/tv", {
        'query': query,
        'language': 'en-US',
        'page': 1
    })

def get_external_ids(tmdb_id, media_type='tv'):
    """Get external IDs for a TV show or movie."""
    endpoint = f"{media_type}/{tmdb_id}/external_ids"
    return get_tmdb_endpoint(endpoint)

def get_tmdb_poster_path(tmdb_id):
    """Get poster path from TMDB API given a TMDB ID."""
    try:
        if not TMDB_API_KEY:
            return None
        
        # Use existing get_tmdb_endpoint function
        tv_data = get_tmdb_endpoint(f"tv/{tmdb_id}")
        
        if tv_data and tv_data.get('poster_path'):
            return tv_data['poster_path']  # Returns "/abc123.jpg"
        
        return None
        
    except Exception as e:
        app.logger.error(f"Error fetching TMDB poster for ID {tmdb_id}: {e}")
        return None
    
def get_sonarr_stats():
    """Get comprehensive Sonarr statistics using existing sonarr_utils patterns."""
    try:
        sonarr_preferences = sonarr_utils.load_preferences()
        headers = {
            'X-Api-Key': sonarr_preferences['SONARR_API_KEY'],
            'Content-Type': 'application/json'
        }
        sonarr_url = sonarr_preferences['SONARR_URL']
        
        stats = {
            'disk_stats': None,
            'queue_stats': None,
            'missing_stats': None,
            'recent_stats': None
        }
        
        # Get disk usage (reuse existing media_processor function)
        try:
            from media_processor import get_sonarr_disk_space
            disk_info = get_sonarr_disk_space()
            if disk_info:
                stats['disk_stats'] = {
                    'used_gb': round(disk_info['total_space_gb'] - disk_info['free_space_gb'], 1),
                    'free_gb': disk_info['free_space_gb'],
                    'total_gb': disk_info['total_space_gb'],
                    'usage_percent': round(((disk_info['total_space_gb'] - disk_info['free_space_gb']) / disk_info['total_space_gb']) * 100, 1)
                }
        except Exception as e:
            app.logger.warning(f"Could not get disk stats: {str(e)}")
        
        # Get queue statistics
        try:
            response = http.get(f"{sonarr_url}/api/v3/queue", headers=headers, timeout=5)
            if response.ok:
                queue_data = response.json()
                records = queue_data.get('records', [])
                
                downloading = len([r for r in records if r.get('status') == 'downloading'])
                queued = len([r for r in records if r.get('status') in ['queued', 'delay']])
                
                stats['queue_stats'] = {
                    'downloading': downloading,
                    'queued': queued,
                    'total': len(records)
                }
        except Exception as e:
            app.logger.warning(f"Could not get queue stats: {str(e)}")
        
        # Get missing episodes count
        try:
            response = http.get(f"{sonarr_url}/api/v3/wanted/missing", headers=headers, timeout=5)
            if response.ok:
                missing_data = response.json()
                stats['missing_stats'] = {
                    'count': missing_data.get('totalRecords', 0)
                }
        except Exception as e:
            app.logger.warning(f"Could not get missing stats: {str(e)}")
        
        # Get recent activity (imports from today)
        try:
            from datetime import datetime, timezone
            today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
            
            response = http.get(
                f"{sonarr_url}/api/v3/history",
                headers=headers,
                params={'page': 1, 'pageSize': 50, 'sortKey': 'date', 'sortDirection': 'descending'},
                timeout=5
            )
            
            if response.ok:
                history_data = response.json()
                records = history_data.get('records', [])
                
                imported_today = 0
                for record in records:
                    if record.get('eventType') == 'downloadFolderImported':
                        record_date = record.get('date', '')[:10]
                        if record_date == today:
                            imported_today += 1
                
                stats['recent_stats'] = {
                    'imported_today': imported_today
                }
        except Exception as e:
            app.logger.warning(f"Could not get recent stats: {str(e)}")
        
        return stats
        
    except Exception as e:
        app.logger.error(f"Error getting Sonarr stats: {str(e)}")
        return {
            'disk_stats': None,
            'queue_stats': None, 
            'missing_stats': None,
            'recent_stats': None
        }
# Scheduler
class OCDarrScheduler:
    def __init__(self):
        self.cleanup_thread = None
        self.running = False
        self.last_cleanup = 0
        self.last_aired_check = 0
        self.update_interval_from_settings()
    
    def update_interval_from_settings(self):
        """Update cleanup interval from global settings."""
        try:
            import media_processor
            global_settings = media_processor.load_global_settings()
            self.cleanup_interval_hours = global_settings.get('cleanup_interval_hours', 6)
        except:
            self.cleanup_interval_hours = 6  # Fallback
    
    def start_scheduler(self):
        if self.running:
            return
        self.running = True
        self.cleanup_thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self.cleanup_thread.start()
        print(f"✓ Global storage gate scheduler started - cleanup every {self.cleanup_interval_hours} hours")
    
    def _scheduler_loop(self):
        time.sleep(300)  # Wait 5 minutes after startup
        while self.running:
            try:
                # Update interval from settings each loop
                self.update_interval_from_settings()
                
                current_time = time.time()
                hours_since_last = (current_time - self.last_cleanup) / 3600
                
                if hours_since_last >= self.cleanup_interval_hours:
                    print("⏰ Starting scheduled global storage gate cleanup...")
                    self._run_cleanup()
                    self.last_cleanup = current_time

                # Daily aired-but-not-downloaded notification check
                hours_since_aired = (current_time - self.last_aired_check) / 3600
                if hours_since_aired >= 24:
                    try:
                        check_aired_not_downloaded()
                    except Exception as aired_err:
                        print(f"Aired not downloaded check error: {aired_err}")
                    self.last_aired_check = current_time

                time.sleep(600)  # Check every 10 minutes
            except Exception as e:
                print(f"Scheduler error: {str(e)}")
                time.sleep(300)
    
    def _run_cleanup(self):
        try:
            import media_processor
            # Use subprocess to run the unified cleanup
            result = subprocess.run(["python3", os.path.join(os.getcwd(), "media_processor.py")], capture_output=True, text=True)
            
            # FIXED: Check return code instead of stderr
            if result.returncode != 0:
                print(f"Cleanup failed with return code {result.returncode}")
                if result.stderr:
                    print(f"Cleanup errors: {result.stderr}")
            # Remove the automatic stderr logging
            
            print("✓ Scheduled cleanup completed (unified 3-function cleanup)")
            return result
            
        except Exception as e:
            print(f"Cleanup failed: {str(e)}")
            return None

    def force_cleanup(self):
        cleanup_thread = threading.Thread(target=self._run_cleanup, daemon=True)
        cleanup_thread.start()
        return "Unified cleanup started"
    
    def get_status(self):
        if not self.running:
            return {"status": "stopped", "next_cleanup": None}
        
        if self.last_cleanup == 0:
            next_cleanup = "5 minutes after startup"
        else:
            next_time = self.last_cleanup + (self.cleanup_interval_hours * 3600)
            next_cleanup = datetime.fromtimestamp(next_time).strftime("%Y-%m-%d %H:%M:%S")
            
        return {
            "status": "running",
            "type": "global_storage_gate",
            "interval_hours": self.cleanup_interval_hours,
            "last_cleanup": datetime.fromtimestamp(self.last_cleanup).strftime("%Y-%m-%d %H:%M:%S") if self.last_cleanup else "Never",
            "next_cleanup": next_cleanup
        }

# Cleanup Logging
def setup_cleanup_logging():
    LOG_PATH = os.getenv('LOG_PATH', '/app/logs/app.log')
    CLEANUP_LOG_PATH = os.getenv('CLEANUP_LOG_PATH', '/app/logs/cleanup.log')
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    os.makedirs(os.path.dirname(CLEANUP_LOG_PATH), exist_ok=True)
    cleanup_logger = logging.getLogger('cleanup')
    cleanup_logger.setLevel(logging.INFO)
    cleanup_logger.handlers.clear()
    main_file_handler = RotatingFileHandler(
        LOG_PATH,
        maxBytes=10*1024*1024,
        backupCount=3,
        encoding='utf-8'
    )
    main_file_handler.setLevel(logging.INFO)
    main_file_formatter = logging.Formatter('%(asctime)s - CLEANUP - %(levelname)s - %(message)s')
    main_file_handler.setFormatter(main_file_formatter)
    cleanup_file_handler = RotatingFileHandler(
        CLEANUP_LOG_PATH,
        maxBytes=5*1024*1024,
        backupCount=5,
        encoding='utf-8'
    )
    cleanup_file_handler.setLevel(logging.INFO)
    cleanup_file_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    cleanup_file_handler.setFormatter(cleanup_file_formatter)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_formatter = logging.Formatter('%(asctime)s - CLEANUP - %(levelname)s - %(message)s')
    console_handler.setFormatter(console_formatter)
    cleanup_logger.addHandler(main_file_handler)
    cleanup_logger.addHandler(cleanup_file_handler)
    cleanup_logger.addHandler(console_handler)
    cleanup_logger.propagate = False
    return cleanup_logger

cleanup_logger = setup_cleanup_logging()
# import pending_deletions

"""
Flask API Endpoints for Episeerr Sidebar

Add these routes to your Flask application to support the sidebar navigation.
"""


@app.route('/rules')
def rules_page():
    """Rules management page with series assignment interface."""
    config = load_config()
    try:
        all_series = get_sonarr_series()
    except requests.exceptions.ConnectionError:
        all_series = []
    
    # Get SONARR_URL for template links
    sonarr_preferences = sonarr_utils.load_preferences()
    sonarr_url = sonarr_preferences['SONARR_URL']
    
    # Map series to their assigned rules
    rules_mapping = {}
    for rule_name, details in config['rules'].items():
        series_dict = details.get('series', {})
        for series_id in series_dict.keys():
            rules_mapping[str(series_id)] = rule_name
    
    for series in all_series:
        series['assigned_rule'] = rules_mapping.get(str(series['id']), 'None')
    
    all_series.sort(key=lambda x: x.get('title', '').lower())
    
    return render_template('rules.html', 
                         config=config,
                         all_series=all_series,
                         SONARR_URL=sonarr_url)
# Add this route to provide rules list for the sidebar
@app.route('/api/rules-list')
def api_rules_list():
    """Return formatted rules data for sidebar display"""
    try:
        config_data = load_config()
        default_rule = config_data.get('default_rule')
        rules = config_data.get('rules', {})
        
        rules_list = []
        
        # Process each rule
        for rule_name, rule_details in sorted(rules.items()):
            is_default = (rule_name == default_rule)
            series_dict = rule_details.get('series', {})
            series_count = len(series_dict)
            
            # Create display name (title case with spaces)
            display_name = rule_name.replace('_', ' ').title()
            
            # Get rule configuration
            get_type = rule_details.get('get_type', 'episodes')
            get_count = rule_details.get('get_count')
            keep_type = rule_details.get('keep_type', 'episodes')
            keep_count = rule_details.get('keep_count')
            grace_watched = rule_details.get('grace_watched')
            grace_unwatched = rule_details.get('grace_unwatched')
            
            rules_list.append({
                'name': rule_name,
                'display_name': display_name,
                'description': rule_details.get('description', ''),
                'series_count': series_count,
                'is_default': is_default,
                'keep_last_n_episodes': keep_count if keep_type == 'episodes' else None,
                'keep_first_n_unwatched': get_count if get_type == 'episodes' else None,
                'grace_period_days': grace_watched or grace_unwatched
            })
        
        # Sort: default first, then alphabetically
        rules_list.sort(key=lambda x: (not x['is_default'], x['display_name']))
        
        return jsonify({
            'success': True,
            'rules': rules_list,
            'total_count': len(rules_list)
        })
    
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e),
            'rules': []
        }), 500


# You may also want to update your existing /api/series-data endpoint
# to include poster image URLs from Sonarr
@app.route('/api/series-data-enhanced')
def api_series_data_enhanced():
    """Enhanced series data with poster images"""
    try:
        # Get series from Sonarr
        sonarr_url = os.environ.get('SONARR_URL')
        sonarr_api_key = os.environ.get('SONARR_API_KEY')
        
        headers = {'X-Api-Key': sonarr_api_key}
        response = http.get(f'{sonarr_url}/api/v3/series', headers=headers)
        
        if response.status_code != 200:
            return jsonify({'success': False, 'error': 'Failed to fetch from Sonarr'}), 500
        
        series_data = response.json()
        config_data = load_config()
        
        # Enhance each series with rule assignment and poster URL
        for series in series_data:
            series_id = series.get('id')
            
            # Find assigned rule
            assigned_rule = None
            for rule_name, rule_details in config_data.get('rules', {}).items():
                if series_id in rule_details.get('series', []):
                    assigned_rule = rule_name
                    break
            
            series['assigned_rule'] = assigned_rule
            
            # Add poster URL - Sonarr provides this in images array
            # but we'll construct the direct URL for easier access
            series['poster_url'] = f"{sonarr_url}/api/v3/mediacover/{series_id}/poster.jpg?apikey={sonarr_api_key}"
        
        return jsonify({
            'success': True,
            'series': series_data,
            'total_count': len(series_data)
        })
    
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e),
            'series': []
        }), 500


# Optional: Quick stats for sidebar
@app.route('/api/sidebar-stats')
def api_sidebar_stats():
    """Return quick stats for sidebar badges"""
    try:
        config_data = load_config()
        
        stats = {
            'total_rules': len(config_data.get('rules', {})),
            'default_rule': config_data.get('default_rule'),
            'rules': {}
        }
        
        # Count series per rule
        for rule_name, rule_details in config_data.get('rules', {}).items():
            stats['rules'][rule_name] = {
                'series_count': len(rule_details.get('series', [])),
                'is_default': (rule_name == config_data.get('default_rule'))
            }
        
        return jsonify(stats)
    
    except Exception as e:
        return jsonify({
            'error': str(e)
        }), 500

@app.route('/pending-deletions')
def view_pending_deletions():
    """View all pending deletions (episodes + movies)"""
    import pending_deletions
    summary = pending_deletions.get_pending_deletions_summary()
    movie_summary = pending_deletions.get_pending_movies_summary()
    return render_template('pending_deletions.html', summary=summary, movie_summary=movie_summary)


@app.route('/pending-deletions/approve', methods=['POST'])
def approve_pending_deletions():
    """Approve and execute deletions"""
    import pending_deletions
    from media_processor import delete_episodes_immediately
    
    try:
        data = request.get_json()
        episode_ids = data.get('episode_ids', [])
        
        if not episode_ids:
            return jsonify({'success': False, 'error': 'No episodes specified'}), 400
        
        result = pending_deletions.approve_deletions(episode_ids, delete_episodes_immediately)
        
        return jsonify({
            'success': True,
            'deleted_count': result['deleted_count'],
            'errors': result['errors']
        })
        
    except Exception as e:
        app.logger.error(f"Error approving deletions: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/pending-deletions/reject', methods=['POST'])
def reject_pending_deletions():
    """Reject deletions and add to rejection cache"""
    import pending_deletions
    
    try:
        data = request.get_json()
        episode_ids = data.get('episode_ids', [])
        
        if not episode_ids:
            return jsonify({'success': False, 'error': 'No episodes specified'}), 400
        
        rejected_count = pending_deletions.reject_deletions(episode_ids)
        
        return jsonify({
            'success': True,
            'rejected_count': rejected_count
        })
        
    except Exception as e:
        app.logger.error(f"Error rejecting deletions: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/pending-deletions/series/<int:series_id>/episodes')
def get_series_episodes(series_id):
    """Get all episode IDs for a series"""
    import pending_deletions
    episode_ids = pending_deletions.get_episode_ids_for_series(series_id)
    return jsonify({'episode_ids': episode_ids})


@app.route('/pending-deletions/series/<int:series_id>/season/<int:season_num>/episodes')
def get_season_episodes(series_id, season_num):
    """Get all episode IDs for a season"""
    import pending_deletions
    episode_ids = pending_deletions.get_episode_ids_for_season(series_id, season_num)
    return jsonify({'episode_ids': episode_ids})


@app.route('/pending-deletions/clear', methods=['POST'])
def clear_pending_deletions():
    """Clear all pending deletions"""
    import pending_deletions
    try:
        pending_deletions.clear_all_pending_deletions()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/pending-deletions/count')
def get_pending_deletions_count():
    """API endpoint to get count of pending deletions for notifications"""
    import pending_deletions
    ep_summary = pending_deletions.get_pending_deletions_summary()
    movie_summary = pending_deletions.get_pending_movies_summary()
    return jsonify({
        'count': ep_summary['total_episodes'] + movie_summary['total_movies'],
        'series_count': ep_summary['total_series'],
        'size_gb': round(ep_summary['total_size_gb'] + movie_summary['total_size_gb'], 2)
    })


# ============================================================================
# MOVIE RULES ROUTES
# ============================================================================

@app.route('/movie-rules')
def movie_rules_page():
    """Movie rules management page."""
    config = load_config()
    movie_rules = config.get('movie_rules', {})

    radarr_cfg = None
    radarr_ok = False
    radarr_msg = ''
    try:
        from settings_db import get_radarr_config
        radarr_cfg = get_radarr_config()
        if radarr_cfg and radarr_cfg.get('url') and radarr_cfg.get('api_key'):
            resp = http.get(
                f"{normalize_url(radarr_cfg['url'])}/api/v3/system/status",
                headers={'X-Api-Key': radarr_cfg['api_key']},
                timeout=5
            )
            radarr_ok = resp.ok
            radarr_msg = f"Connected to Radarr v{resp.json().get('version','?')}" if resp.ok else f"Radarr error {resp.status_code}"
        else:
            radarr_msg = 'Radarr not configured'
    except Exception as e:
        radarr_msg = f'Radarr unreachable: {e}'

    return render_template(
        'movie_rules.html',
        movie_rules=movie_rules,
        default_movie_rule=config.get('default_movie_rule', ''),
        radarr_ok=radarr_ok,
        radarr_msg=radarr_msg,
    )


@app.route('/movie-rules/create', methods=['POST'])
def create_movie_rule():
    """Create a new movie rule."""
    config = load_config()
    if 'movie_rules' not in config:
        config['movie_rules'] = {}

    rule_name = request.form.get('rule_name', '').strip()
    if not rule_name:
        return redirect(url_for('movie_rules_page'))

    grace_watched_raw = request.form.get('grace_watched', '').strip()
    dormant_days_raw = request.form.get('dormant_days', '').strip()

    rule = {
        'grace_watched': int(grace_watched_raw) if grace_watched_raw else None,
        'dormant_days': int(dormant_days_raw) if dormant_days_raw else None,
        'require_approval': request.form.get('require_approval') == 'true',
        'dry_run': request.form.get('dry_run') == 'true',
        'delete_option': request.form.get('delete_option', 'file_only'),
        'description': request.form.get('description', '').strip(),
        'movies': {},
    }

    config['movie_rules'][rule_name] = rule

    if request.form.get('set_as_default') == 'true':
        config['default_movie_rule'] = rule_name
    elif config.get('default_movie_rule') == rule_name and request.form.get('set_as_default') != 'true':
        config.pop('default_movie_rule', None)

    save_config(config)

    # Create Radarr tag
    try:
        from movie_processor import get_or_create_radarr_tag, get_radarr_settings
        radarr_url, api_key = get_radarr_settings()
        if radarr_url and api_key:
            get_or_create_radarr_tag(rule_name, radarr_url, api_key)
    except Exception as e:
        app.logger.warning(f"Could not create Radarr tag for movie rule '{rule_name}': {e}")

    return redirect(url_for('movie_rules_page'))


@app.route('/movie-rules/<rule_name>/edit', methods=['GET', 'POST'])
def edit_movie_rule(rule_name):
    """Edit an existing movie rule."""
    config = load_config()
    movie_rules = config.get('movie_rules', {})

    if rule_name not in movie_rules:
        return redirect(url_for('movie_rules_page'))

    if request.method == 'POST':
        grace_watched_raw = request.form.get('grace_watched', '').strip()
        dormant_days_raw = request.form.get('dormant_days', '').strip()

        new_name = request.form.get('rule_name', '').strip() or rule_name

        movie_rules[rule_name].update({
            'grace_watched': int(grace_watched_raw) if grace_watched_raw else None,
            'dormant_days': int(dormant_days_raw) if dormant_days_raw else None,
            'require_approval': request.form.get('require_approval') == 'true',
            'dry_run': request.form.get('dry_run') == 'true',
            'delete_option': request.form.get('delete_option', 'file_only'),
            'description': request.form.get('description', '').strip(),
        })

        if request.form.get('set_as_default') == 'true':
            config['default_movie_rule'] = new_name
        elif config.get('default_movie_rule') == rule_name:
            config.pop('default_movie_rule', None)

        if new_name and new_name != rule_name:
            movie_rules[new_name] = movie_rules.pop(rule_name)
            if config.get('default_movie_rule') == rule_name:
                config['default_movie_rule'] = new_name
            config['movie_rules'] = movie_rules
            save_config(config)
            _swap_movie_rule_tag_on_all(rule_name, new_rule=new_name)
        else:
            save_config(config)

        return redirect(url_for('movie_rules_page'))

    return render_template('movie_rules.html',
                           movie_rules=movie_rules,
                           edit_rule_name=rule_name,
                           edit_rule=movie_rules[rule_name],
                           radarr_ok=True, radarr_msg='')


def _swap_movie_rule_tag_on_all(old_rule, new_rule=None):
    """
    Remove the episeerr-<old_rule> tag from every Radarr movie that has it.
    If new_rule is given, add episeerr-<new_rule> in its place (rename path).
    """
    try:
        from movie_processor import get_radarr_settings, get_or_create_radarr_tag, _rule_to_tag_label
        radarr_url, api_key = get_radarr_settings()
        if not radarr_url or not api_key:
            return
        headers = {'X-Api-Key': api_key}
        old_label = _rule_to_tag_label(old_rule)

        tags_resp = http.get(f"{radarr_url}/api/v3/tag", headers=headers, timeout=10)
        all_tags = tags_resp.json() if tags_resp.ok else []
        tag_id_to_label = {t['id']: t['label'] for t in all_tags}
        old_tag_id = next((t['id'] for t in all_tags if t['label'] == old_label), None)
        if not old_tag_id:
            return

        new_tag_id = None
        if new_rule:
            new_tag_id = get_or_create_radarr_tag(new_rule, radarr_url, api_key)

        movies_resp = http.get(f"{radarr_url}/api/v3/movie", headers=headers, timeout=30)
        if not movies_resp.ok:
            return
        for movie in movies_resp.json():
            if old_tag_id not in movie.get('tags', []):
                continue
            new_tags = [tid for tid in movie['tags'] if not tag_id_to_label.get(tid, '').startswith('episeerr-')]
            if new_tag_id:
                new_tags.append(new_tag_id)
            movie['tags'] = new_tags
            http.put(f"{radarr_url}/api/v3/movie/{movie['id']}", headers=headers, json=movie, timeout=10)
    except Exception as e:
        app.logger.error(f"Error swapping movie rule tags ({old_rule} → {new_rule}): {e}")


@app.route('/movie-rules/<rule_name>/delete', methods=['POST'])
def delete_movie_rule(rule_name):
    """Delete a movie rule and remove its tag from all Radarr movies."""
    config = load_config()
    movie_rules = config.get('movie_rules', {})
    movie_rules.pop(rule_name, None)
    if config.get('default_movie_rule') == rule_name:
        config.pop('default_movie_rule', None)
    save_config(config)
    _swap_movie_rule_tag_on_all(rule_name, new_rule=None)
    return redirect(url_for('movie_rules_page'))


@app.route('/api/movie-rules/ensure-tags', methods=['POST'])
def ensure_movie_tags():
    """Create Radarr tags for all movie rules (called on startup or demand)."""
    try:
        from movie_processor import ensure_movie_rule_tags
        config = load_config()
        movie_rules = config.get('movie_rules', {})
        tag_ids = ensure_movie_rule_tags(movie_rules)
        return jsonify({'success': True, 'created': len(tag_ids), 'tags': tag_ids})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/movie-rules/clean-orphaned-tags', methods=['POST'])
def clean_orphaned_movie_tags():
    """Remove episeerr- tags from Radarr movies where the tag has no matching movie rule."""
    try:
        from movie_processor import get_radarr_settings, _rule_to_tag_label
        config = load_config()
        movie_rules = config.get('movie_rules', {})
        valid_labels = {_rule_to_tag_label(rn) for rn in movie_rules}

        radarr_url, api_key = get_radarr_settings()
        if not radarr_url or not api_key:
            return jsonify({'success': False, 'error': 'Radarr not configured'}), 503

        headers = {'X-Api-Key': api_key}
        tags_resp = http.get(f"{radarr_url}/api/v3/tag", headers=headers, timeout=10)
        all_tags = tags_resp.json() if tags_resp.ok else []
        tag_id_to_label = {t['id']: t['label'] for t in all_tags}

        movies_resp = http.get(f"{radarr_url}/api/v3/movie", headers=headers, timeout=30)
        if not movies_resp.ok:
            return jsonify({'success': False, 'error': 'Failed to fetch movies from Radarr'}), 500

        cleaned = 0
        for movie in movies_resp.json():
            current_tags = movie.get('tags', [])
            orphaned = [tid for tid in current_tags
                        if tag_id_to_label.get(tid, '').startswith('episeerr-')
                        and tag_id_to_label.get(tid) not in valid_labels]
            if not orphaned:
                continue
            movie['tags'] = [tid for tid in current_tags if tid not in orphaned]
            http.put(f"{radarr_url}/api/v3/movie/{movie['id']}", headers=headers, json=movie, timeout=10)
            cleaned += 1
            app.logger.info(f"Removed orphaned tag(s) from '{movie.get('title')}'")

        return jsonify({'success': True, 'cleaned': cleaned})
    except Exception as e:
        app.logger.error(f"Error cleaning orphaned movie tags: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ── Movie pending deletions ───────────────────────────────────────────────────

@app.route('/pending-deletions/movies/approve', methods=['POST'])
def approve_pending_movie_deletions():
    """Approve and execute movie deletions."""
    import pending_deletions
    try:
        data = request.get_json()
        movie_ids = data.get('movie_ids', [])
        if not movie_ids:
            return jsonify({'success': False, 'error': 'No movies specified'}), 400
        result = pending_deletions.approve_movie_deletions(movie_ids)
        return jsonify({'success': True, **result})
    except Exception as e:
        app.logger.error(f"Error approving movie deletions: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/pending-deletions/movies/reject', methods=['POST'])
def reject_pending_movie_deletions():
    """Reject movie deletions."""
    import pending_deletions
    try:
        data = request.get_json()
        movie_ids = data.get('movie_ids', [])
        if not movie_ids:
            return jsonify({'success': False, 'error': 'No movies specified'}), 400
        rejected_count = pending_deletions.reject_movie_deletions(movie_ids)
        return jsonify({'success': True, 'rejected_count': rejected_count})
    except Exception as e:
        app.logger.error(f"Error rejecting movie deletions: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# Add these routes to episeerr.py
@app.route('/api/recent-cleanup-activity')
def recent_cleanup_activity():
    """Get recent cleanup activity for dashboard."""
    try:
        # Use the correct log path from environment or default
        log_path = os.getenv('CLEANUP_LOG_PATH', '/app/logs/cleanup.log')
        
        if os.path.exists(log_path):
            with open(log_path, 'r') as f:
                lines = f.readlines()
                # Get last 50 lines for context
                recent_lines = lines[-50:] if len(lines) > 50 else lines
                return jsonify({
                    'success': True,
                    'log_lines': [line.strip() for line in recent_lines]
                })
        return jsonify({'success': False, 'error': 'Log file not found'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})
@app.route('/logs')
def view_logs():
    """View and filter log files."""
    import os
    from datetime import datetime
    
    # Get parameters
    log_file = request.args.get('log_file', 'episeerr.log')
    lines = int(request.args.get('lines', 100))
    level = request.args.get('level', 'ALL')
    search = request.args.get('search', '')
    download = request.args.get('download', 'false') == 'true'
    
    # Security: Only allow specific log files
    allowed_logs = ['episeerr.log', 'cleanup.log', 'app.log']
    if log_file not in allowed_logs:
        log_file = 'episeerr.log'
    
    # Get log file path
    log_path = os.path.join(os.getcwd(), 'logs', log_file)
    
    if not os.path.exists(log_path):
        return render_template('view_logs.html', 
                             log_file=log_file,
                             log_lines=[],
                             total_lines=0,
                             lines=lines,
                             level=level,
                             search=search,
                             log_size='0 KB',
                             current_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    
    try:
        # Get file size
        file_size = os.path.getsize(log_path)
        if file_size < 1024:
            log_size = f"{file_size} bytes"
        elif file_size < 1024*1024:
            log_size = f"{file_size/1024:.1f} KB"
        else:
            log_size = f"{file_size/(1024*1024):.1f} MB"
        
        # Read last N lines efficiently
        with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
            # Get total line count
            total_lines = sum(1 for _ in f)
            
            # Go back to start and read last N lines
            f.seek(0)
            all_lines = f.readlines()
            log_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines
        
        # Filter by log level
        if level != 'ALL':
            log_lines = [line for line in log_lines if level in line]
        
        # Filter by search text
        if search:
            log_lines = [line for line in log_lines if search.lower() in line.lower()]
        
        # Strip newlines for display
        log_lines = [line.rstrip('\n') for line in log_lines]
        
        # Download filtered logs
        if download:
            from flask import Response
            content = '\n'.join(log_lines)
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"{log_file.replace('.log', '')}_{timestamp}.txt"
            
            return Response(
                content,
                mimetype='text/plain',
                headers={'Content-Disposition': f'attachment; filename={filename}'}
            )
        
        return render_template('view_logs.html',
                             log_file=log_file,
                             log_lines=log_lines,
                             total_lines=total_lines,
                             lines=lines,
                             level=level,
                             search=search,
                             log_size=log_size,
                             current_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    
    except Exception as e:
        app.logger.error(f"Error reading log file: {str(e)}")
        return render_template('view_logs.html',
                             log_file=log_file,
                             log_lines=[f"Error reading log: {str(e)}"],
                             total_lines=0,
                             lines=lines,
                             level=level,
                             search=search,
                             log_size='Unknown',
                             current_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))


@app.route('/logs/clear', methods=['POST'])
def clear_old_logs():
    """Delete rotated log files older than 7 days."""
    import os
    import time
    from datetime import datetime, timedelta
    
    try:
        logs_dir = os.path.join(os.getcwd(), 'logs')
        cutoff_time = time.time() - (7 * 24 * 60 * 60)  # 7 days ago
        
        deleted_files = []
        
        # Look for rotated log files (*.log.1, *.log.2, etc.)
        for filename in os.listdir(logs_dir):
            filepath = os.path.join(logs_dir, filename)
            
            # Skip current log files
            if filename in ['episeerr.log', 'cleanup.log', 'app.log', 'missing.log']:
                continue
            
            # Only delete rotated logs (have numbers)
            if '.log.' in filename and os.path.isfile(filepath):
                file_mtime = os.path.getmtime(filepath)
                
                if file_mtime < cutoff_time:
                    os.remove(filepath)
                    deleted_files.append(filename)
                    app.logger.info(f"Deleted old log file: {filename}")
        
        if deleted_files:
            return jsonify({
                'status': 'success',
                'message': f"Deleted {len(deleted_files)} old log files: {', '.join(deleted_files)}"
            })
        else:
            return jsonify({
                'status': 'success',
                'message': 'No old log files to delete (all files are less than 7 days old)'
            })
    
    except Exception as e:
        app.logger.error(f"Error clearing old logs: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

# ============================================================================
# SIMPLIFIED CONFIG MANAGEMENT
# ============================================================================

_config_cache = None
_config_cache_time = 0
_CONFIG_CACHE_TTL = 30  # seconds

def _invalidate_config_cache():
    global _config_cache, _config_cache_time
    _config_cache = None
    _config_cache_time = 0

def load_config():
    """Load configuration with simplified migration."""
    global _config_cache, _config_cache_time
    now = time.time()
    if _config_cache is not None and (now - _config_cache_time) < _CONFIG_CACHE_TTL:
        return _config_cache
    try:
        # REMOVED: Backup on every load (was causing spam)
        with open(config_path, 'r') as file:
            config = json.load(file)
        if 'rules' not in config:
            config['rules'] = {}
        
        # Migration: Add grace_scope to existing rules
        migrated = False
        for rule_name, rule_details in config.get('rules', {}).items():
            if 'grace_scope' not in rule_details:
                rule_details['grace_scope'] = 'series'  # Default to current behavior
                migrated = True
        
        if migrated:
            save_config(config)
            app.logger.info("✓ Migrated rules to include grace_scope field (defaulted to 'series')")

        _config_cache = config
        _config_cache_time = time.time()
        return config
    except FileNotFoundError:
        default_config = {
            'rules': {
                'default': {
                    'get_type': 'episodes',
                    'get_count': 1,
                    'keep_type': 'episodes',
                    'keep_count': 1,
                    'action_option': 'search',
                    'monitor_watched': False,
                    'grace_watched': None,
                    'grace_unwatched': None,
                    'dormant_days': None,
                    'grace_scope': 'series',
                    'series': {},
                    'dry_run': False
                }
            },
            'default_rule': 'default'
        }
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        save_config(default_config)
        return default_config


def save_config(config):
    """Save configuration to JSON file with automatic backup."""
    _invalidate_config_cache()
    try:
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        
        # Backup BEFORE saving (only when actually writing)
        if os.path.exists(config_path):
            backup_path = config_path + '.bak'
            try:
                shutil.copy2(config_path, backup_path)
                app.logger.debug(f"Backed up config.json to {backup_path}")
            except Exception as e:
                app.logger.warning(f"Could not backup config.json: {e}")
        
        # Write to a temp file then rename so concurrent writes can't corrupt the file
        tmp_path = config_path + '.tmp'
        with open(tmp_path, 'w') as file:
            json.dump(config, file, indent=4)
        os.replace(tmp_path, config_path)
        app.logger.debug("Config saved successfully")
    except Exception as e:
        app.logger.error(f"Save failed: {str(e)}")
        raise


# Add this new function after load_config():

def backup_global_settings():
    """Backup global_settings.json if it exists."""
    try:
        settings_path = os.path.join(os.getcwd(), 'config', 'global_settings.json')
        if os.path.exists(settings_path):
            backup_path = settings_path + '.bak'
            shutil.copy2(settings_path, backup_path)
            app.logger.debug(f"Backed up global_settings.json to {backup_path}")
    except Exception as e:
        app.logger.warning(f"Could not backup global_settings.json: {e}")



def get_notification_config():
    """Load notification settings from global config"""
    import media_processor
    try:
        settings = media_processor.load_global_settings()
        return {
            'NOTIFICATIONS_ENABLED': settings.get('notifications_enabled', False),
            'DISCORD_WEBHOOK_URL': settings.get('discord_webhook_url', ''),
            'EPISEERR_URL': settings.get('episeerr_url', 'http://localhost:5002'),
            'NOTIFY_AIRED_NOT_DOWNLOADED': settings.get('notify_aired_not_downloaded', False),
        }
    except Exception as e:
        app.logger.warning(f"Could not load notification config: {e}")
        return {
            'NOTIFICATIONS_ENABLED': False,
            'DISCORD_WEBHOOK_URL': '',
            'EPISEERR_URL': 'http://localhost:5002',
            'NOTIFY_AIRED_NOT_DOWNLOADED': False,
        }

def check_aired_not_downloaded():
    """Query Sonarr calendar for the past 48 hours and notify about aired-but-not-downloaded episodes.

    Skips series with Sonarr status 'ended'. Only notifies once per episode
    (tracked in /data/aired_notifications.json). Entries are auto-cleaned after 30 days.
    """
    import media_processor
    from notification_storage import (
        aired_notification_exists, store_aired_notification,
        cleanup_old_aired_notifications
    )

    global_settings = media_processor.load_global_settings()
    if not global_settings.get('notify_aired_not_downloaded', False):
        app.logger.debug("Aired not downloaded notifications disabled, skipping check")
        return

    if not global_settings.get('notifications_enabled', False):
        app.logger.debug("Notifications disabled globally, skipping aired check")
        return

    prefs = sonarr_utils.load_preferences()
    sonarr_url = prefs.get('SONARR_URL', '')
    api_key = prefs.get('SONARR_API_KEY', '')

    if not sonarr_url or not api_key:
        app.logger.warning("Sonarr not configured, skipping aired-not-downloaded check")
        return

    headers = {'X-Api-Key': api_key, 'Content-Type': 'application/json'}

    now = datetime.utcnow()
    start = (now - timedelta(hours=48)).strftime('%Y-%m-%dT%H:%M:%SZ')
    end = now.strftime('%Y-%m-%dT%H:%M:%SZ')

    try:
        response = http.get(
            f"{sonarr_url}/api/v3/calendar",
            headers=headers,
            params={'start': start, 'end': end, 'unmonitored': 'false'},
            timeout=30
        )
        response.raise_for_status()
        episodes = response.json()
    except Exception as e:
        app.logger.error(f"Failed to fetch Sonarr calendar for aired check: {e}")
        return

    new_episodes = []
    for ep in episodes:
        if ep.get('hasFile', True):
            continue
        series_status = ep.get('series', {}).get('status', '').lower()
        if series_status == 'ended':
            continue
        episode_id = ep.get('id')
        if not episode_id:
            continue
        if aired_notification_exists(episode_id):
            continue
        new_episodes.append(ep)

    if not new_episodes:
        app.logger.debug("No new aired-but-not-downloaded episodes to notify about")
        return

    app.logger.info(f"Found {len(new_episodes)} aired-but-not-downloaded episode(s), sending notification")

    try:
        from notifications import send_notification
        send_notification('aired_not_downloaded', episodes=new_episodes)
        for ep in new_episodes:
            store_aired_notification(ep['id'])
        cleanup_old_aired_notifications()
    except Exception as e:
        app.logger.error(f"Failed to send aired-not-downloaded notification: {e}")


def get_sonarr_series():
    """Get series list from Sonarr, excluding series with 'watched' tag."""
    try:
        sonarr_preferences = sonarr_utils.load_preferences()
        headers = {
            'X-Api-Key': sonarr_preferences['SONARR_API_KEY'],
            'Content-Type': 'application/json'
        }
        sonarr_url = sonarr_preferences['SONARR_URL']
        
        # Get all series
        response = http.get(f"{sonarr_url}/api/v3/series", headers=headers)
        if not response.ok:
            app.logger.error(f"Failed to fetch series from Sonarr: {response.status_code}")
            return []
        
        all_series = response.json()
        
        # Get all tags to find 'watched' tag ID
        tags_response = http.get(f"{sonarr_url}/api/v3/tag", headers=headers)
        if tags_response.ok:
            tags = tags_response.json()
            watched_tag_id = None
            for tag in tags:
                if tag.get('label', '').lower() == 'watched':
                    watched_tag_id = tag.get('id')
                    break
            
            # Filter out series with 'watched' tag
            if watched_tag_id:
                filtered_series = []
                for series in all_series:
                    series_tags = series.get('tags', [])
                    if watched_tag_id not in series_tags:
                        filtered_series.append(series)
                app.logger.debug(f"Filtered out {len(all_series) - len(filtered_series)} series with 'watched' tag")
                return filtered_series
        
        return all_series
        
    except requests.exceptions.ConnectionError:
        app.logger.warning("Sonarr not reachable - is it running?")
        raise
    except Exception as e:
        app.logger.error(f"Error fetching Sonarr series: {str(e)}")
        return []

# ============================================================================
# WEB UI ROUTES
# ============================================================================

# Update your index() route to also pass SONARR_URL:
@app.route('/docs')
@app.route('/documentation')
def documentation():
    """Display comprehensive documentation page."""
    return render_template('documentation.html')

@app.route('/')
def index():
    """Redirect to dashboard as main page."""
    return redirect(url_for('dashboard.dashboard'))

@app.route('/tv')
def tv_dashboard():
    """TV-optimized dashboard for Android TV."""
    return render_template('tv_dashboard.html')

@app.route('/series')  # or whatever you want to call it
def series_management():  # Changed from index to avoid confusion
    """Main series/rules management page."""
    config = load_config()
    try:
        all_series = get_sonarr_series()
    except requests.exceptions.ConnectionError:
        all_series = []
        app.logger.warning("Sonarr not reachable - showing empty series list")
    sonarr_stats = get_sonarr_stats()
    
    # Get SONARR_URL for template links
    sonarr_preferences = sonarr_utils.load_preferences()
    sonarr_url = sonarr_preferences['SONARR_URL']
    
    rules_mapping = {}
    for rule_name, details in config['rules'].items():
        series_dict = details.get('series', {})
        for series_id in series_dict.keys():
            rules_mapping[str(series_id)] = rule_name
    for series in all_series:
        series['assigned_rule'] = rules_mapping.get(str(series['id']), 'None')
    all_series.sort(key=lambda x: x.get('title', '').lower())
    
    radarr_cfg = get_radarr_config()
    radarr_url = radarr_cfg['url'] if radarr_cfg else ''

    return render_template('rules_index.html',
                         config=config,
                         all_series=all_series,
                         sonarr_stats=sonarr_stats,
                         SONARR_URL=sonarr_url,
                         SONARR_API_KEY=sonarr_preferences['SONARR_API_KEY'],
                         RADARR_URL=radarr_url,
                         current_rule=request.args.get('rule', list(config['rules'].keys())[0] if config['rules'] else 'default'))

# Add new API route for real-time stats updates
@app.route('/api/sonarr-stats')
def get_sonarr_stats_api():
    """Get Sonarr statistics via API."""
    try:
        stats = get_sonarr_stats()
        return jsonify({
            'status': 'success',
            'stats': stats
        })
    except Exception as e:
        app.logger.error(f"Error in sonarr stats API: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500
    

# ============================================================================
# RADARR API PROXY ROUTES
# ============================================================================

def _radarr_headers():
    cfg = get_radarr_config()
    if not cfg or not cfg.get('url') or not cfg.get('api_key'):
        return None, None
    return cfg, {'X-Api-Key': cfg['api_key'], 'Content-Type': 'application/json'}


@app.route('/api/radarr/movies')
def radarr_movies():
    """Fetch all movies from Radarr with quality profile names and assigned movie rule."""
    cfg, headers = _radarr_headers()
    if not cfg:
        return jsonify({'success': False, 'error': 'Radarr not configured'}), 503
    try:
        base = cfg['url'].rstrip('/')
        movies_resp = http.get(f"{base}/api/v3/movie", headers=headers, timeout=15)
        movies_resp.raise_for_status()
        movies = movies_resp.json()

        # Fetch quality profiles for mapping
        qp_resp = http.get(f"{base}/api/v3/qualityprofile", headers=headers, timeout=10)
        qp_map = {}
        if qp_resp.ok:
            qp_map = {p['id']: p['name'] for p in qp_resp.json()}

        # Fetch tags to resolve assigned movie rule per movie
        tags_resp = http.get(f"{base}/api/v3/tag", headers=headers, timeout=10)
        tag_id_to_label = {}
        if tags_resp.ok:
            tag_id_to_label = {t['id']: t['label'] for t in tags_resp.json()}

        from movie_processor import _rule_to_tag_label
        movie_rules = load_config().get('movie_rules', {})
        slug_to_rule = {_rule_to_tag_label(rn): rn for rn in movie_rules}

        # Build config-based mapping (primary source, mirrors series rules)
        config_movie_rule = {}
        for rn, rd in movie_rules.items():
            for mid in rd.get('movies', {}):
                config_movie_rule[str(mid)] = rn

        result = []
        for m in movies:
            poster = next(
                (img['remoteUrl'] for img in m.get('images', []) if img.get('coverType') == 'poster'),
                None
            )
            # Prefer config.json assignment; fall back to Radarr tags
            assigned_rule = config_movie_rule.get(str(m['id']))
            if assigned_rule is None:
                for tid in m.get('tags', []):
                    lbl = tag_id_to_label.get(tid, '')
                    if lbl.startswith('episeerr_') and lbl in slug_to_rule:
                        assigned_rule = slug_to_rule[lbl]
                        break
            result.append({
                'id': m['id'],
                'title': m.get('title', ''),
                'year': m.get('year', ''),
                'overview': m.get('overview', ''),
                'genres': m.get('genres', []),
                'status': m.get('status', ''),
                'hasFile': m.get('hasFile', False),
                'monitored': m.get('monitored', False),
                'sizeOnDisk': m.get('sizeOnDisk', 0),
                'path': m.get('path', ''),
                'qualityProfileId': m.get('qualityProfileId'),
                'qualityProfileName': qp_map.get(m.get('qualityProfileId'), 'Unknown'),
                'tmdbId': m.get('tmdbId'),
                'poster': poster,
                'titleSlug': m.get('titleSlug', ''),
                'assigned_rule': assigned_rule,
            })
        result.sort(key=lambda x: x['title'].lower())
        return jsonify({'success': True, 'movies': result})
    except Exception as e:
        app.logger.error(f"Error fetching Radarr movies: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/movie-rules/assign', methods=['POST'])
def api_assign_movie_rule():
    """Assign or unassign a movie rule to a Radarr movie via episeerr_ tags."""
    data = request.get_json(silent=True) or {}
    movie_id = data.get('movie_id')
    rule_name = (data.get('rule_name') or '').strip()

    if not movie_id:
        return jsonify({'success': False, 'error': 'movie_id required'}), 400

    cfg, headers = _radarr_headers()
    if not cfg:
        return jsonify({'success': False, 'error': 'Radarr not configured'}), 503

    try:
        base = cfg['url'].rstrip('/')

        movie_resp = http.get(f"{base}/api/v3/movie/{movie_id}", headers=headers, timeout=10)
        if not movie_resp.ok:
            return jsonify({'success': False, 'error': 'Movie not found'}), 404
        movie = movie_resp.json()

        tags_resp = http.get(f"{base}/api/v3/tag", headers=headers, timeout=10)
        all_tags = tags_resp.json() if tags_resp.ok else []
        tag_label_to_id = {t['label']: t['id'] for t in all_tags}
        tag_id_to_label = {t['id']: t['label'] for t in all_tags}

        # Strip existing episeerr- tags, keep all others
        new_tags = [tid for tid in movie.get('tags', [])
                    if not tag_id_to_label.get(tid, '').startswith('episeerr-')]

        if rule_name:
            from movie_processor import _rule_to_tag_label, get_or_create_radarr_tag
            tag_label = _rule_to_tag_label(rule_name)
            tag_id = tag_label_to_id.get(tag_label) or get_or_create_radarr_tag(rule_name, base, cfg['api_key'])
            if tag_id and tag_id not in new_tags:
                new_tags.append(tag_id)

        movie['tags'] = new_tags
        put_resp = http.put(f"{base}/api/v3/movie/{movie_id}", headers=headers, json=movie, timeout=15)
        if not put_resp.ok:
            return jsonify({'success': False, 'error': f'Radarr update failed: {put_resp.status_code}'}), 500

        # Persist assignment in config.json (mirrors how series rules store series IDs)
        config = load_config()
        movie_id_str = str(movie_id)
        for rn, rd in config.get('movie_rules', {}).items():
            rd.setdefault('movies', {})
            rd['movies'].pop(movie_id_str, None)
        if rule_name and rule_name in config.get('movie_rules', {}):
            config['movie_rules'][rule_name].setdefault('movies', {})[movie_id_str] = {}
        save_config(config)

        return jsonify({'success': True, 'assigned_rule': rule_name or None})
    except Exception as e:
        app.logger.error(f"Error assigning movie rule: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/radarr/quality-profiles')
def radarr_quality_profiles():
    """Fetch quality profiles from Radarr."""
    cfg, headers = _radarr_headers()
    if not cfg:
        return jsonify({'success': False, 'error': 'Radarr not configured'}), 503
    try:
        resp = http.get(f"{cfg['url'].rstrip('/')}/api/v3/qualityprofile", headers=headers, timeout=10)
        resp.raise_for_status()
        profiles = [{'id': p['id'], 'name': p['name']} for p in resp.json()]
        return jsonify({'success': True, 'profiles': profiles})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/radarr/root-folders')
def radarr_root_folders():
    """Fetch root folders from Radarr."""
    cfg, headers = _radarr_headers()
    if not cfg:
        return jsonify({'success': False, 'error': 'Radarr not configured'}), 503
    try:
        resp = http.get(f"{cfg['url'].rstrip('/')}/api/v3/rootfolder", headers=headers, timeout=10)
        resp.raise_for_status()
        folders = [{'path': f['path'], 'freeSpace': f.get('freeSpace', 0)} for f in resp.json()]
        return jsonify({'success': True, 'folders': folders})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/radarr/add-movie', methods=['POST'])
def radarr_add_movie():
    """Look up by TMDB ID and add movie to Radarr."""
    cfg, headers = _radarr_headers()
    if not cfg:
        return jsonify({'success': False, 'error': 'Radarr not configured'}), 503
    data = request.json or {}
    tmdb_id = data.get('tmdb_id')
    quality_profile_id = data.get('quality_profile_id')
    root_folder_path = data.get('root_folder_path')
    if not all([tmdb_id, quality_profile_id, root_folder_path]):
        return jsonify({'success': False, 'error': 'tmdb_id, quality_profile_id, and root_folder_path required'}), 400
    try:
        base = cfg['url'].rstrip('/')
        lookup = http.get(f"{base}/api/v3/movie/lookup/tmdb", headers=headers,
                          params={'tmdbId': tmdb_id}, timeout=10)
        lookup.raise_for_status()
        movie_data = lookup.json()
        if not movie_data:
            return jsonify({'success': False, 'error': 'Movie not found on TMDB lookup'}), 404

        payload = {
            'title': movie_data.get('title'),
            'titleSlug': movie_data.get('titleSlug'),
            'qualityProfileId': int(quality_profile_id),
            'rootFolderPath': root_folder_path,
            'tmdbId': tmdb_id,
            'images': movie_data.get('images', []),
            'monitored': True,
            'addOptions': {'searchForMovie': True},
        }
        add_resp = http.post(f"{base}/api/v3/movie", headers=headers, json=payload, timeout=15)
        if add_resp.status_code in (200, 201):
            _plex_watchlist_add_silent(tmdb_id, 'movie', (movie_data or {}).get('title', ''))
            return jsonify({'success': True, 'movie': add_resp.json()})
        return jsonify({'success': False, 'error': add_resp.text}), add_resp.status_code
    except Exception as e:
        app.logger.error(f"Error adding movie to Radarr: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# SONARR LOOKUP/ADD ROUTES (for Discover)
# ============================================================================

@app.route('/api/sonarr/quality-profiles')
def sonarr_quality_profiles():
    """Fetch quality profiles from Sonarr."""
    prefs = sonarr_utils.load_preferences()
    sonarr_url = prefs.get('SONARR_URL')
    api_key = prefs.get('SONARR_API_KEY')
    if not sonarr_url or not api_key:
        return jsonify({'success': False, 'error': 'Sonarr not configured'}), 503
    try:
        headers = {'X-Api-Key': api_key}
        resp = http.get(f"{sonarr_url}/api/v3/qualityprofile", headers=headers, timeout=10)
        resp.raise_for_status()
        profiles = [{'id': p['id'], 'name': p['name']} for p in resp.json()]
        return jsonify({'success': True, 'profiles': profiles})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/sonarr/root-folders')
def sonarr_root_folders():
    """Fetch root folders from Sonarr."""
    prefs = sonarr_utils.load_preferences()
    sonarr_url = prefs.get('SONARR_URL')
    api_key = prefs.get('SONARR_API_KEY')
    if not sonarr_url or not api_key:
        return jsonify({'success': False, 'error': 'Sonarr not configured'}), 503
    try:
        headers = {'X-Api-Key': api_key}
        resp = http.get(f"{sonarr_url}/api/v3/rootfolder", headers=headers, timeout=10)
        resp.raise_for_status()
        folders = [{'path': f['path'], 'freeSpace': f.get('freeSpace', 0)} for f in resp.json()]
        return jsonify({'success': True, 'folders': folders})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/sonarr/add-series', methods=['POST'])
def sonarr_add_series():
    """Add a series to Sonarr using default quality profile / root folder, then
    redirect straight to episode selection. No quality/folder prompt needed."""
    prefs = sonarr_utils.load_preferences()
    sonarr_url = prefs.get('SONARR_URL')
    api_key = prefs.get('SONARR_API_KEY')
    if not sonarr_url or not api_key:
        return jsonify({'success': False, 'error': 'Sonarr not configured'}), 503
    req_data = request.json or {}
    tmdb_id = req_data.get('tmdb_id')
    if not tmdb_id:
        return jsonify({'success': False, 'error': 'tmdb_id required'}), 400
    try:
        headers = {'X-Api-Key': api_key, 'Content-Type': 'application/json'}

        # Already in Sonarr via a previous pending entry → go straight to selection
        existing = find_pending_request_by_tmdb(tmdb_id)
        if existing and existing.get('series_id'):
            return jsonify({'success': True,
                            'redirect_url': f'/api/send-to-selection/{existing["series_id"]}'})

        # Look up series in Sonarr
        lookup = http.get(f"{sonarr_url}/api/v3/series/lookup",
                          headers=headers, params={'term': f'tmdb:{tmdb_id}'}, timeout=10)
        lookup.raise_for_status()
        results = lookup.json()
        if not results:
            return jsonify({'success': False, 'error': 'Series not found in Sonarr lookup'}), 404

        series_meta = results[0]
        title   = series_meta.get('title', 'Unknown')
        tvdb_id = series_meta.get('tvdbId')

        # Series is already in the Sonarr library (lookup returns id > 0)
        existing_id = series_meta.get('id') or 0
        if existing_id > 0:
            _plex_watchlist_add_silent(tmdb_id, 'tv', title)
            return jsonify({'success': True,
                            'redirect_url': f'/api/send-to-selection/{existing_id}'})

        # Fetch Sonarr defaults (first quality profile + first root folder)
        qp_resp = http.get(f"{sonarr_url}/api/v3/qualityprofile",
                           headers={'X-Api-Key': api_key}, timeout=10)
        rf_resp = http.get(f"{sonarr_url}/api/v3/rootfolder",
                           headers={'X-Api-Key': api_key}, timeout=10)
        quality_profiles = qp_resp.json() if qp_resp.ok else []
        root_folders     = rf_resp.json() if rf_resp.ok else []
        if not quality_profiles or not root_folders:
            return jsonify({'success': False,
                            'error': 'Sonarr has no quality profiles or root folders configured'}), 503
        quality_profile_id = quality_profiles[0]['id']
        root_folder_path   = root_folders[0]['path']

        # Add to Sonarr (unmonitored — selection page controls what actually downloads)
        payload = {
            'title':            series_meta.get('title'),
            'titleSlug':        series_meta.get('titleSlug'),
            'qualityProfileId': quality_profile_id,
            'rootFolderPath':   root_folder_path,
            'tvdbId':           tvdb_id,
            'images':           series_meta.get('images', []),
            'seasons':          series_meta.get('seasons', []),
            'monitored':        False,
            'addOptions': {'searchForMissingEpisodes': False, 'monitor': 'none'},
        }
        add_resp = http.post(f"{sonarr_url}/api/v3/series",
                              headers=headers, json=payload, timeout=15)
        if add_resp.status_code in (200, 201):
            series_id = add_resp.json()['id']
            app.logger.info(f"Added '{title}' to Sonarr (ID {series_id}) → directing to selection")
            _plex_watchlist_add_silent(tmdb_id, 'tv', title)
            return jsonify({'success': True,
                            'redirect_url': f'/api/send-to-selection/{series_id}'})

        app.logger.error(f"Sonarr add returned {add_resp.status_code}: {add_resp.text[:200]}")
        return jsonify({'success': False,
                        'error': f'Sonarr returned {add_resp.status_code}'}), 502

    except Exception as e:
        app.logger.error(f"Error adding series: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/sonarr/prepare-series', methods=['POST'])
def sonarr_prepare_series():
    """Prepare a series for the selection flow without adding to Sonarr yet.
    Stores a pending request with Sonarr lookup data; Sonarr add is deferred
    until the user confirms their rule/season choices."""
    prefs = sonarr_utils.load_preferences()
    sonarr_url = prefs.get('SONARR_URL')
    api_key = prefs.get('SONARR_API_KEY')
    if not sonarr_url or not api_key:
        return jsonify({'success': False, 'error': 'Sonarr not configured'}), 503
    req_data = request.json or {}
    tmdb_id = req_data.get('tmdb_id')
    if not tmdb_id:
        return jsonify({'success': False, 'error': 'tmdb_id required'}), 400
    try:
        headers = {'X-Api-Key': api_key, 'Content-Type': 'application/json'}

        # Pending request already exists — go straight to selection
        existing = find_pending_request_by_tmdb(tmdb_id)
        if existing:
            return jsonify({'success': True, 'tmdb_id': tmdb_id})

        # Look up series in Sonarr
        lookup = http.get(f"{sonarr_url}/api/v3/series/lookup",
                          headers=headers, params={'term': f'tmdb:{tmdb_id}'}, timeout=10)
        lookup.raise_for_status()
        results = lookup.json()
        if not results:
            return jsonify({'success': False, 'error': 'Series not found in Sonarr lookup'}), 404

        series_meta = results[0]
        title = series_meta.get('title', 'Unknown')
        tvdb_id = series_meta.get('tvdbId')

        # Series already in Sonarr library — redirect straight to selection
        # (Plex watchlist add deferred to confirmation, same as new series)
        existing_id = series_meta.get('id') or 0
        if existing_id > 0:
            return jsonify({'success': True,
                            'redirect_url': f'/api/send-to-selection/{existing_id}'})

        # Fetch Sonarr defaults (first quality profile + first root folder)
        qp_resp = http.get(f"{sonarr_url}/api/v3/qualityprofile",
                           headers={'X-Api-Key': api_key}, timeout=10)
        rf_resp = http.get(f"{sonarr_url}/api/v3/rootfolder",
                           headers={'X-Api-Key': api_key}, timeout=10)
        quality_profiles = qp_resp.json() if qp_resp.ok else []
        root_folders = rf_resp.json() if rf_resp.ok else []
        if not quality_profiles or not root_folders:
            return jsonify({'success': False,
                            'error': 'Sonarr has no quality profiles or root folders configured'}), 503

        quality_profile_id = quality_profiles[0]['id']
        root_folder_path = root_folders[0]['path']

        # Store pending request — series_id left None, Sonarr add deferred to confirmation
        request_id = f"search-{tmdb_id}-{int(time.time())}"
        pending = {
            'id': request_id,
            'series_id': None,
            'title': title,
            'tmdb_id': tmdb_id,
            'tvdb_id': tvdb_id,
            'source': 'search',
            'source_name': 'Search',
            'needs_season_selection': True,
            'needs_attention': True,
            'quality_profile_id': quality_profile_id,
            'root_folder_path': root_folder_path,
            'sonarr_lookup': series_meta,
        }
        add_pending_request(pending)
        app.logger.info(f"Prepared '{title}' (tmdb:{tmdb_id}) for selection — Sonarr add deferred")
        return jsonify({'success': True, 'tmdb_id': tmdb_id})

    except Exception as e:
        app.logger.error(f"Error preparing series: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# DISCOVER API (search + details — browse routes removed)
# ============================================================================

@app.route('/api/discover/details')
def discover_details():
    """Fetch full TMDB details + trailer for a single item."""
    media_type = request.args.get('type')   # movie | tv
    tmdb_id    = request.args.get('id')
    if not media_type or not tmdb_id or media_type not in ('movie', 'tv'):
        return jsonify({'success': False, 'error': 'type and id required'}), 400
    if not TMDB_API_KEY:
        return jsonify({'success': False, 'error': 'TMDB not configured'}), 503
    try:
        details = get_tmdb_endpoint(f'{media_type}/{tmdb_id}',
                                    {'append_to_response': 'videos,credits'})
        if not details:
            return jsonify({'success': False, 'error': 'Not found'}), 404

        # Pick best trailer: official YouTube trailer first
        trailer_url = None
        for v in details.get('videos', {}).get('results', []):
            if v.get('site') == 'YouTube' and v.get('type') == 'Trailer' and v.get('official'):
                trailer_url = f"https://www.youtube.com/watch?v={v['key']}"
                break
        if not trailer_url:
            for v in details.get('videos', {}).get('results', []):
                if v.get('site') == 'YouTube' and v.get('type') == 'Trailer':
                    trailer_url = f"https://www.youtube.com/watch?v={v['key']}"
                    break

        # Top cast (max 6)
        cast = [
            {'name': m['name'], 'character': m.get('character', '')}
            for m in details.get('credits', {}).get('cast', [])[:6]
        ]

        genres = [g['name'] for g in details.get('genres', [])]
        runtime = details.get('runtime') or (
            details.get('episode_run_time') or [None])[0]

        return jsonify({
            'success': True,
            'title':       details.get('title') or details.get('name', ''),
            'year':        (details.get('release_date') or details.get('first_air_date') or '')[:4],
            'overview':    details.get('overview', ''),
            'poster':      f"https://image.tmdb.org/t/p/w342{details['poster_path']}" if details.get('poster_path') else None,
            'backdrop':    f"https://image.tmdb.org/t/p/w780{details['backdrop_path']}" if details.get('backdrop_path') else None,
            'vote_average': round(details.get('vote_average', 0), 1),
            'genres':      genres,
            'runtime':     runtime,
            'trailer_url': trailer_url,
            'cast':        cast,
        })
    except Exception as e:
        app.logger.error(f"Discover details error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500



@app.route('/api/discover/search')
def discover_search():
    """TMDB multi search, cross-referenced against Sonarr and Radarr."""
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify({'success': False, 'error': 'Query required'}), 400
    if not TMDB_API_KEY:
        return jsonify({'success': False, 'error': 'TMDB API key not configured'}), 503
    try:
        data = get_tmdb_endpoint('search/multi', params={'query': q})
        if not data:
            return jsonify({'success': False, 'error': 'TMDB request failed'}), 500
        results = _enrich_tmdb_results(data.get('results', []))
        return jsonify({'success': True, 'results': results})
    except Exception as e:
        app.logger.error(f"Discover search error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# UNIVERSAL SIDEBAR SEARCH
# ============================================================================

_SEARCH_SETTINGS_INDEX = [
    {'keywords': {'sonarr'}, 'title': 'Sonarr', 'subtitle': 'Service configuration', 'url': '/setup#sonarr-config', 'icon': 'fas fa-satellite-dish'},
    {'keywords': {'radarr'}, 'title': 'Radarr', 'subtitle': 'Service configuration', 'url': '/setup#radarr-config', 'icon': 'fas fa-film'},
    {'keywords': {'plex'}, 'title': 'Plex', 'subtitle': 'Service configuration', 'url': '/setup#plex-config', 'icon': 'fas fa-server'},
    {'keywords': {'jellyfin'}, 'title': 'Jellyfin', 'subtitle': 'Service configuration', 'url': '/setup#jellyfin-config', 'icon': 'fas fa-server'},
    {'keywords': {'emby'}, 'title': 'Emby', 'subtitle': 'Service configuration', 'url': '/setup#emby-config', 'icon': 'fas fa-server'},
    {'keywords': {'tautulli', 'stats'}, 'title': 'Tautulli', 'subtitle': 'Service configuration', 'url': '/setup#tautulli-config', 'icon': 'fas fa-chart-bar'},
    {'keywords': {'tmdb', 'database'}, 'title': 'TMDB', 'subtitle': 'Service configuration', 'url': '/setup#tmdb-config', 'icon': 'fas fa-database'},
    {'keywords': {'jellyseerr', 'overseerr', 'requests'}, 'title': 'Jellyseerr', 'subtitle': 'Service configuration', 'url': '/setup#jellyseerr-config', 'icon': 'fas fa-question-circle'},
    {'keywords': {'docker', 'containers', 'container'}, 'title': 'Docker', 'subtitle': 'Container management', 'url': '/setup#docker-config', 'icon': 'fas fa-cube'},
    {'keywords': {'webhook', 'webhooks', 'notification', 'notifications'}, 'title': 'Webhooks', 'subtitle': 'Notification settings', 'url': '/scheduler', 'icon': 'fas fa-bell'},
    {'keywords': {'grace', 'grace period'}, 'title': 'Grace Period', 'subtitle': 'Cleanup settings', 'url': '/scheduler', 'icon': 'fas fa-clock'},
    {'keywords': {'cleanup', 'schedule', 'scheduler', 'automation'}, 'title': 'Cleanup Scheduler', 'subtitle': 'Automation settings', 'url': '/scheduler', 'icon': 'fas fa-broom'},
    {'keywords': {'logs', 'log', 'history', 'activity', 'debug', 'troubleshoot', 'troubleshooting'}, 'title': 'View Logs', 'subtitle': 'App & cleanup logs (episeerr.log)', 'url': '/logs', 'icon': 'fas fa-terminal'},
    {'keywords': {'cleanup logs', 'cleanuplogs'}, 'title': 'Cleanup Logs', 'subtitle': 'Scheduled cleanup history', 'url': '/cleanup-logs', 'icon': 'fas fa-file-alt'},
    {'keywords': {'auth', 'authentication', 'login', 'password', 'security'}, 'title': 'Authentication', 'subtitle': 'Security settings', 'url': '/scheduler', 'icon': 'fas fa-lock'},
]

_SEARCH_NAV_INDEX = [
    {'keywords': {'dashboard', 'home', 'overview'}, 'title': 'Dashboard', 'subtitle': 'Main overview & stats', 'url': '/dashboard', 'icon': 'fas fa-chart-line'},
    {'keywords': {'library', 'series', 'shows', 'tv'}, 'title': 'Library', 'subtitle': 'Series & rules management', 'url': '/series', 'icon': 'fas fa-photo-video'},
    {'keywords': {'rules', 'rule'}, 'title': 'Rules', 'subtitle': 'Manage episode rules', 'url': '/series', 'icon': 'fas fa-list'},
    {'keywords': {'pending', 'queue', 'items', 'selections'}, 'title': 'Pending Items', 'subtitle': 'Pending episode selections', 'url': '/episeerr', 'icon': 'fas fa-bell'},
    {'keywords': {'services', 'setup', 'connect', 'integrations'}, 'title': 'Services', 'subtitle': 'Configure external services', 'url': '/setup', 'icon': 'fas fa-plug'},
    {'keywords': {'settings', 'admin', 'scheduler', 'preferences', 'configuration', 'configure'}, 'title': 'Settings', 'subtitle': 'App settings & scheduler', 'url': '/scheduler', 'icon': 'fas fa-sliders-h'},
    {'keywords': {'docs', 'documentation', 'help', 'guide', 'troubleshooting'}, 'title': 'Documentation', 'subtitle': 'Guides & help', 'url': '/documentation', 'icon': 'fas fa-book'},
    {'keywords': {'logs', 'log', 'episeerr.log', 'debug', 'activity', 'troubleshooting'}, 'title': 'View Logs', 'subtitle': 'App logs & debug output', 'url': '/logs', 'icon': 'fas fa-terminal'},
    {'keywords': {'theme', 'appearance', 'color', 'dark', 'light'}, 'title': 'Theme', 'subtitle': 'Change color theme', 'url': '/dashboard', 'icon': 'fas fa-palette'},
]


@app.route('/api/search')
def unified_search():
    """Universal search — Tier 1 internal data instantly, Tier 2/3 external services in parallel."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from collections import defaultdict
    from settings_db import get_all_quick_links, get_service as _get_svc
    from settings_db import get_jellyfin_config, get_emby_config, get_plex_config

    q_orig = request.args.get('q', '').strip()
    q = q_orig.lower()
    if len(q) < 2:
        return jsonify({'results': []}), 400

    results = []

    # ── Tier 1: Internal data ────────────────────────────────────────────

    # Pre-load watch history — most recent event per series title
    _watches_by_title = {}
    try:
        import json as _json_lib
        from activity_storage import WATCHES_FILE
        if os.path.exists(WATCHES_FILE):
            with open(WATCHES_FILE) as _wf:
                for _we in _json_lib.load(_wf):
                    _wt = (_we.get('series_title') or '').lower()
                    if not _wt:
                        continue
                    ts = _we.get('timestamp', 0)
                    if _wt not in _watches_by_title or ts > _watches_by_title[_wt].get('timestamp', 0):
                        _watches_by_title[_wt] = _we
    except Exception:
        pass

    # Resolve Tautulli URL once — used to make Watched chips clickable
    _tautulli_url = None
    try:
        from settings_db import get_tautulli_config as _get_tau_cfg
        _tau = _get_tau_cfg()
        if _tau and _tau.get('url') and _tau.get('api_key'):
            _tautulli_url = _tau['url'].rstrip('/')
    except Exception:
        pass

    # Library: Series from Sonarr
    try:
        config = load_config()
        rules_mapping = {}
        for rule_name, details in config.get('rules', {}).items():
            for sid in details.get('series', {}).keys():
                rules_mapping[str(sid)] = rule_name
        prefs = sonarr_utils.load_preferences()
        s_url = prefs.get('SONARR_URL', '')
        s_key = prefs.get('SONARR_API_KEY', '')
        if s_url and s_key:
            resp = http.get(f"{s_url}/api/v3/series",
                            headers={'X-Api-Key': s_key}, timeout=5)
            if resp.ok:
                for s in resp.json():
                    title = s.get('title', '')
                    if q not in title.lower():
                        continue
                    rule = rules_mapping.get(str(s['id']))
                    links = []
                    slug = s.get('titleSlug', '')
                    if slug:
                        links.append({
                            'label': 'Sonarr',
                            'url': f"{s_url.rstrip('/')}/series/{slug}",
                            'icon': 'fas fa-satellite-dish',
                            'action': 'open_tab',
                        })
                    if rule:
                        links.append({
                            'label': f'Rule: {rule}',
                            'url': f'/rules?highlight={rule}',
                            'icon': 'fas fa-list',
                            'action': 'navigate',
                        })
                    # Single Watched chip — always from watched.json (most recent).
                    # Clickable → Tautulli when configured; static badge otherwise.
                    # Cross-service grouping skips adding a second chip (dedup below).
                    _lw = _watches_by_title.get(title.lower())
                    if _lw:
                        if _tautulli_url:
                            links.append({
                                'label': 'Watched',
                                'url': _tautulli_url,
                                'icon': 'fas fa-eye',
                                'action': 'open_tab',
                            })
                        else:
                            links.append({
                                'label': f"Watched {time_ago(_lw.get('timestamp', 0))}",
                                'url': None,
                                'icon': 'fas fa-eye',
                                'action': None,
                                'static': True,
                            })
                    results.append({
                        'category': 'Library',
                        'title': title,
                        'subtitle': s.get('status', '').title(),
                        'action': 'navigate',
                        'url': f"/series?highlight={s['id']}",
                        'icon': 'fas fa-tv',
                        'badge': None,
                        'data': None,
                        'links': links,
                    })
    except Exception:
        pass

    # Library: Movies from Radarr
    try:
        r_cfg, r_hdrs = _radarr_headers()
        if r_cfg:
            resp = http.get(f"{r_cfg['url'].rstrip('/')}/api/v3/movie",
                            headers=r_hdrs, timeout=5)
            if resp.ok:
                for m in resp.json():
                    title = m.get('title', '')
                    if q not in title.lower():
                        continue
                    links = []
                    slug = m.get('titleSlug', '')
                    if slug:
                        links.append({
                            'label': 'Radarr',
                            'url': f"{r_cfg['url'].rstrip('/')}/movie/{slug}",
                            'icon': 'fas fa-film',
                            'action': 'open_tab',
                        })
                    results.append({
                        'category': 'Library',
                        'title': title,
                        'subtitle': f"Movie · {m.get('year', '')}",
                        'action': 'navigate',
                        'url': '/series',
                        'icon': 'fas fa-film',
                        'badge': None,
                        'data': None,
                        'links': links,
                    })
    except Exception:
        pass

    # Rules
    try:
        config = load_config()
        for rule_name, details in config.get('rules', {}).items():
            if q in rule_name.lower():
                get_c = details.get('get_count') or 'all'
                keep_c = details.get('keep_count') or 'all'
                results.append({
                    'category': 'Rules',
                    'title': rule_name,
                    'subtitle': f"Get {get_c} {details.get('get_type', 'episodes')} · Keep {keep_c}",
                    'action': 'navigate',
                    'url': f"/series?rule={rule_name}",
                    'icon': 'fas fa-list',
                    'badge': None,
                    'data': None,
                })
    except Exception:
        pass

    # Settings sections (keyword match)
    for entry in _SEARCH_SETTINGS_INDEX:
        if any(q in kw or kw.startswith(q) for kw in entry['keywords']):
            results.append({
                'category': 'Settings',
                'title': entry['title'],
                'subtitle': entry['subtitle'],
                'action': 'navigate',
                'url': entry['url'],
                'icon': entry['icon'],
                'badge': None,
                'data': None,
            })

    # Nav items (keyword match)
    for entry in _SEARCH_NAV_INDEX:
        if any(q in kw or kw.startswith(q) for kw in entry['keywords']):
            results.append({
                'category': 'Navigate',
                'title': entry['title'],
                'subtitle': entry['subtitle'],
                'action': 'navigate',
                'url': entry['url'],
                'icon': entry['icon'],
                'badge': None,
                'data': None,
            })

    # Quick links — only user-added (custom=True); auto-generated service links
    # (custom=False) are already covered by the Settings index with /setup URLs
    try:
        for link in get_all_quick_links():
            if not link.get('custom'):
                continue
            if q in link.get('name', '').lower():
                results.append({
                    'category': 'Quick Links',
                    'title': link['name'],
                    'subtitle': (link.get('url') or '')[:60],
                    'action': 'open_tab',
                    'url': link.get('alternate_url') or link.get('url', ''),
                    'icon': link.get('icon') or 'fas fa-link',
                    'badge': None,
                    'data': None,
                })
    except Exception:
        pass

    # Pending selections (awaiting episode choice)
    try:
        for pr in get_all_pending_requests():
            title = pr.get('title', '')
            if title and q in title.lower():
                results.append({
                    'category': 'Pending',
                    'title': title,
                    'subtitle': 'Awaiting episode selection',
                    'action': 'navigate',
                    'url': '/episeerr',
                    'icon': 'fas fa-clock',
                    'badge': 'Pending',
                    'data': None,
                })
    except Exception:
        pass

    # Recent activity (watches + episode downloads from activity_storage files)
    try:
        import json as _json
        from activity_storage import WATCHES_FILE, SEARCHES_FILE
        _activity_events = []
        for _filepath, _badge in [(WATCHES_FILE, 'Watched'), (SEARCHES_FILE, 'Downloaded')]:
            if os.path.exists(_filepath):
                with open(_filepath) as _f:
                    for _e in _json.load(_f):
                        _title = _e.get('series_title', '')
                        if _title and q in _title.lower():
                            _activity_events.append((_e.get('timestamp', 0), _e, _badge))
        _activity_events.sort(key=lambda x: x[0], reverse=True)
        _seen_act = set()
        for _ts, _e, _badge in _activity_events[:6]:
            _t = _e.get('series_title', '')
            if _t.lower() in _seen_act:
                continue
            _seen_act.add(_t.lower())
            _s, _ep = _e.get('season', 0), _e.get('episode', 0)
            _ago = time_ago(_ts)
            results.append({
                'category': 'Recent',
                'title': _t,
                'subtitle': f"S{_s:02d}E{_ep:02d} · {_badge} {_ago}",
                'action': 'navigate',
                'url': f"/series?highlight={_e.get('series_id', '')}",
                'icon': 'fas fa-history',
                'badge': _badge,
                'data': None,
            })
    except Exception:
        pass

    # ── Tier 2 & 3: External services (parallel) ────────────────────────

    def _search_plex(query):
        try:
            plex_cfg = get_plex_config()
            if not plex_cfg:
                return []
            p_url = plex_cfg['url'].rstrip('/')
            p_key = plex_cfg['api_key']
            if not p_url or not p_key:
                return []
            resp = http.get(f"{p_url}/search",
                            params={'query': query, 'X-Plex-Token': p_key, 'limit': 4},
                            headers={'Accept': 'application/json'}, timeout=3)
            if not resp.ok:
                return []
            metadata = resp.json().get('MediaContainer', {}).get('Metadata') or []
            out = []
            for item in metadata:
                plex_type = item.get('type', '')
                if plex_type not in ('show', 'movie'):
                    continue  # skip episodes, seasons, tracks, etc.
                type_label = 'Series' if plex_type == 'show' else 'Movie'
                out.append({
                    'category': 'Plex',
                    'title': item.get('title', ''),
                    'subtitle': f"{type_label} · {item.get('year', '')}",
                    'action': 'open_tab',
                    'url': p_url,
                    'icon': 'fas fa-server',
                    'badge': None,
                    'data': None,
                })
                if len(out) >= 4:
                    break
            return out
        except Exception:
            return []

    def _search_jellyfin(query):
        try:
            jf_cfg = get_jellyfin_config()
            if not jf_cfg:
                return []
            j_url = jf_cfg['url'].rstrip('/')
            j_key = jf_cfg['api_key']
            if not j_url or not j_key:
                return []
            user_id = jf_cfg.get('user_id', '')
            endpoint = f"{j_url}/Users/{user_id}/Items" if user_id else f"{j_url}/Items"
            resp = http.get(endpoint, params={
                'searchTerm': query,
                'IncludeItemTypes': 'Series,Movie',
                'Limit': 4,
                'api_key': j_key,
            }, timeout=3)
            if not resp.ok:
                return []
            out = []
            for item in (resp.json().get('Items') or [])[:4]:
                out.append({
                    'category': 'Jellyfin',
                    'title': item.get('Name', ''),
                    'subtitle': item.get('Type', '').title(),
                    'action': 'open_tab',
                    'url': j_url,
                    'icon': 'fas fa-server',
                    'badge': None,
                    'data': None,
                })
            return out
        except Exception:
            return []

    def _search_emby(query):
        try:
            emby_cfg = get_emby_config()
            if not emby_cfg:
                return []
            e_url = emby_cfg['url'].rstrip('/')
            e_key = emby_cfg['api_key']
            if not e_url or not e_key:
                return []
            user_id = emby_cfg.get('user_id', '')
            endpoint = f"{e_url}/Users/{user_id}/Items" if user_id else f"{e_url}/Items"
            resp = http.get(endpoint, params={
                'searchTerm': query,
                'IncludeItemTypes': 'Series,Movie',
                'Limit': 4,
                'api_key': e_key,
            }, timeout=3)
            if not resp.ok:
                return []
            out = []
            for item in (resp.json().get('Items') or [])[:4]:
                out.append({
                    'category': 'Emby',
                    'title': item.get('Name', ''),
                    'subtitle': item.get('Type', '').title(),
                    'action': 'open_tab',
                    'url': e_url,
                    'icon': 'fas fa-server',
                    'badge': None,
                    'data': None,
                })
            return out
        except Exception:
            return []

    def _search_tmdb(query):
        try:
            if not TMDB_API_KEY:
                return []
            data = get_tmdb_endpoint('search/multi', params={'query': query})
            if not data:
                return []
            enriched = _enrich_tmdb_results(data.get('results', []))
            out = []
            for item in enriched[:5]:
                media_type = item.get('media_type', 'tv')
                in_library = item.get('in_library', False)
                is_pending = item.get('pending', False)
                if in_library:
                    badge = 'In Library'
                    if media_type == 'tv' and item.get('library_id'):
                        action, url = 'navigate', f"/series?highlight={item['library_id']}"
                    else:
                        r_cfg = get_radarr_config()
                        action, url = 'open_tab', (r_cfg['url'] if r_cfg else '#')
                elif is_pending:
                    badge, action, url = 'Pending', 'navigate', '/episeerr'
                else:
                    badge = 'Add'
                    action = 'add_series' if media_type == 'tv' else 'add_movie'
                    url = None
                out.append({
                    'category': 'Discover',
                    'title': item.get('title', ''),
                    'subtitle': f"{'Series' if media_type == 'tv' else 'Movie'} · {item.get('year', '')}",
                    'action': action,
                    'url': url,
                    'icon': 'fas fa-tv' if media_type == 'tv' else 'fas fa-film',
                    'badge': badge,
                    'data': {
                        'tmdb_id': item.get('tmdb_id'),
                        'media_type': media_type,
                        'title': item.get('title', ''),
                        'year': item.get('year', ''),
                        'poster': item.get('poster'),
                        'overview': item.get('overview', ''),
                    },
                })
            return out
        except Exception:
            return []

    def _search_jellyseerr(query):
        try:
            svc = _get_svc('jellyseerr', 'default') or {}
            js_url = (svc.get('url') or '').rstrip('/')
            js_key = svc.get('api_key', '')
            if not js_url or not js_key:
                return []
            resp = http.get(f"{js_url}/api/v1/search",
                            headers={'X-Api-Key': js_key},
                            params={'query': query, 'take': 4}, timeout=3)
            if not resp.ok:
                return []
            STATUS_MAP = {1: 'Unknown', 2: 'Pending', 3: 'Processing',
                          4: 'Partial', 5: 'Available'}
            out = []
            for result in (resp.json().get('results') or [])[:4]:
                media_info = result.get('mediaInfo') or {}
                badge = STATUS_MAP.get(media_info.get('status'))
                title = result.get('title') or result.get('name', '')
                out.append({
                    'category': 'Jellyseerr',
                    'title': title,
                    'subtitle': result.get('mediaType', '').title(),
                    'action': 'open_tab',
                    'url': js_url,
                    'icon': 'fas fa-question-circle',
                    'badge': badge,
                    'data': None,
                })
            return out
        except Exception:
            return []

    def _search_tautulli(query):
        try:
            from settings_db import get_tautulli_config
            cfg = get_tautulli_config()
            if not cfg:
                return []
            t_url = cfg['url'].rstrip('/')
            t_key = cfg['api_key']
            if not t_url or not t_key:
                return []
            resp = http.get(f"{t_url}/api/v2", params={
                'apikey': t_key, 'cmd': 'get_history',
                'search': query, 'length': 10,
            }, timeout=3)
            if not resp.ok:
                return []
            entries = (resp.json().get('response', {})
                       .get('data', {}).get('data', []))
            if not isinstance(entries, list):
                return []
            # Group by show/movie title, keep most recent play per title
            seen = {}
            for e in entries:
                if e.get('media_type') == 'episode':
                    title = e.get('grandparent_title') or e.get('full_title', '')
                else:
                    title = e.get('full_title') or e.get('title', '')
                if not title:
                    continue
                # Tautulli searches full_title (show + episode), but we display the
                # show title — skip if the query isn't actually in the show title
                if query.lower() not in title.lower():
                    continue
                ts = e.get('date') or e.get('stopped') or 0
                user = e.get('user', '')
                if title not in seen or ts > seen[title]['ts']:
                    seen[title] = {'ts': ts, 'user': user}
            out = []
            for title, info in list(seen.items())[:4]:
                ago = time_ago(info['ts']) if info['ts'] else 'recently'
                user_str = f" by {info['user']}" if info['user'] else ''
                out.append({
                    'category': 'Tautulli',
                    'title': title,
                    'subtitle': f"Watched{user_str} {ago}",
                    'action': 'open_tab',
                    'url': t_url,
                    'icon': 'fas fa-chart-bar',
                    'badge': None,
                    'data': None,
                    'links': [],
                })
            return out
        except Exception:
            return []

    def _search_containers(query):
        try:
            from integrations.docker import _docker_get
            from settings_db import get_service as _gs
            docker_svc = _gs('docker', 'default')
            if not docker_svc:
                return []
            cfg = docker_svc.get('config') or {}
            host = cfg.get('docker_host') or docker_svc.get('url') or 'unix:///var/run/docker.sock'
            raw = _docker_get(host, '/containers/json', {'all': 'true'})
            out = []
            for c in raw:
                name = (c.get('Names') or [''])[0].lstrip('/')
                if not name or query.lower() not in name.lower():
                    continue
                status = c.get('State', 'unknown')
                is_running = status == 'running'
                out.append({
                    'category': 'Containers',
                    'title': name,
                    'subtitle': c.get('Image', ''),
                    'action': 'navigate',
                    'url': '/dashboard',
                    'icon': 'fas fa-cube',
                    'badge': 'Running' if is_running else 'Stopped',
                    'data': {'id': c.get('Id', '')[:12], 'status': status},
                })
                if len(out) >= 4:
                    break
            return out
        except Exception:
            return []

    def _search_plex_watchlist(query):
        try:
            from settings_db import get_plex_config
            from integrations.plex import PlexIntegration
            plex_cfg = get_plex_config()
            if not plex_cfg or not plex_cfg.get('api_key'):
                return []
            items = PlexIntegration().fetch_watchlist(plex_cfg['api_key'])
            out = []
            for item in items:
                title = item.get('title', '')
                if not title or query.lower() not in title.lower():
                    continue
                plex_type = item.get('type', 'movie')
                type_label = 'Series' if plex_type == 'show' else 'Movie'
                out.append({
                    'category': 'Watchlist',
                    'title': title,
                    'subtitle': f"{type_label} · {item.get('year', '')}",
                    'action': 'navigate',
                    'url': '/dashboard',
                    'icon': 'fas fa-bookmark',
                    'badge': 'Watchlist',
                    'data': {
                        'tmdb_id': item.get('tmdb_id'),
                        'media_type': 'tv' if plex_type == 'show' else 'movie',
                        'title': title,
                        'year': item.get('year', ''),
                    },
                })
                if len(out) >= 4:
                    break
            return out
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures_map = {}
        futures_map[executor.submit(_search_plex, q_orig)] = 'plex'
        futures_map[executor.submit(_search_jellyfin, q_orig)] = 'jellyfin'
        futures_map[executor.submit(_search_emby, q_orig)] = 'emby'
        futures_map[executor.submit(_search_tmdb, q_orig)] = 'tmdb'
        futures_map[executor.submit(_search_jellyseerr, q_orig)] = 'jellyseerr'
        futures_map[executor.submit(_search_containers, q_orig)] = 'containers'
        futures_map[executor.submit(_search_plex_watchlist, q_orig)] = 'watchlist'
        futures_map[executor.submit(_search_tautulli, q_orig)] = 'tautulli'
        try:
            for future in as_completed(futures_map, timeout=4):
                try:
                    results.extend(future.result())
                except Exception:
                    pass
        except Exception:
            pass

    import re as _re

    def _norm(t):
        return _re.sub(r'[^a-z0-9]', '', (t or '').lower())

    # ── Step 1: Container merge ───────────────────────────────────────────
    # When a container name matches a quick link/service result, collapse
    # them into one entry — service URL is kept, badge shows live status.
    _CTR_MERGE_INTO = {'Quick Links', 'Navigate', 'Settings',
                       'Plex', 'Jellyfin', 'Emby', 'Jellyseerr'}
    _ctr_absorbed = set()
    _svc_by_norm = {_norm(r['title']): r for r in results
                    if r['category'] in _CTR_MERGE_INTO}
    for cr in results:
        if cr['category'] != 'Containers':
            continue
        match = _svc_by_norm.get(_norm(cr['title']))
        if match:
            match['badge'] = cr['badge']
            match['subtitle'] = f"{cr['badge']} · {match.get('subtitle', '')}".strip(' ·')
            _ctr_absorbed.add(id(cr))
    results = [r for r in results if id(r) not in _ctr_absorbed]

    # Ensure every result has a links list
    for r in results:
        r.setdefault('links', [])

    # ── Step 2: Cross-service grouping ───────────────────────────────────
    # Same content in multiple services → one result with extra service chips.
    _GROUPABLE = {'Library',
                  'Plex', 'Jellyfin', 'Emby',
                  'Tautulli', 'Discover', 'Watchlist', 'Jellyseerr'}
    _GROUP_PRIORITY = {
        'Library': 0,
        'Plex': 1, 'Jellyfin': 1, 'Emby': 1,
        'Discover': 2,
        'Watchlist': 3, 'Tautulli': 4,
        'Jellyseerr': 4,
    }
    _SVC_ICONS = {
        'Plex': 'fas fa-server', 'Jellyfin': 'fas fa-server', 'Emby': 'fas fa-server',
        'Tautulli': 'fas fa-chart-bar', 'Discover': 'fas fa-globe',
        'Watchlist': 'fas fa-bookmark', 'Jellyseerr': 'fas fa-concierge-bell',
    }

    _groups = {}   # norm_title → list[result]
    _non_groupable = []
    for r in results:
        if r['category'] not in _GROUPABLE:
            _non_groupable.append(r)
            continue
        key = _norm(r.get('title', ''))
        if not key:
            _non_groupable.append(r)
            continue
        _groups.setdefault(key, []).append(r)

    _merged = []
    for key, group in _groups.items():
        if len(group) == 1:
            _merged.append(group[0])
            continue
        group.sort(key=lambda r: (_GROUP_PRIORITY.get(r['category'], 99),))
        primary = group[0]
        for sec in group[1:]:
            cat = sec['category']
            if cat == 'Tautulli':
                # Skip if a Watched chip was already added in Tier 1 (from watched.json)
                if not any(l.get('label', '').startswith('Watched') for l in primary['links']):
                    primary['links'].append({
                        'label': 'Watched',
                        'url': sec['url'],
                        'icon': 'fas fa-eye',
                        'action': sec['action'],
                    })
            elif cat == 'Discover' and sec.get('badge') in ('In Library', 'Pending'):
                pass  # already represented by the Library primary
            elif cat == 'Discover' and sec.get('badge') == 'Add' and primary['category'] != 'Library':
                primary['links'].append({
                    'label': 'Add',
                    'url': sec['url'],
                    'icon': 'fas fa-plus',
                    'action': sec['action'],
                    'data': sec.get('data'),
                })
            elif cat == 'Jellyseerr':
                status_part = f" · {sec['badge']}" if sec.get('badge') else ''
                primary['links'].append({
                    'label': f"Jellyseerr{status_part}",
                    'url': sec['url'],
                    'icon': 'fas fa-concierge-bell',
                    'action': sec['action'],
                })
            else:
                primary['links'].append({
                    'label': cat,
                    'url': sec['url'],
                    'icon': _SVC_ICONS.get(cat, 'fas fa-external-link-alt'),
                    'action': sec['action'],
                })
        _merged.append(primary)

    results = _merged + _non_groupable

    # ── Step 3: Rank and cap ──────────────────────────────────────────────
    _ORDER = [
        'Library', 'Pending', 'Recent', 'Rules',
        'Navigate', 'Quick Links', 'Settings',
        'Containers', 'Watchlist',
        'Discover', 'Tautulli',
        'Plex', 'Jellyfin', 'Emby', 'Jellyseerr',
    ]
    by_cat = defaultdict(list)
    for r in results:
        by_cat[r['category']].append(r)

    final = []
    for cat in _ORDER:
        for item in by_cat.pop(cat, [])[:4]:
            final.append(item)
            if len(final) >= 15:
                break
        if len(final) >= 15:
            break

    return jsonify({'results': final})


@app.route('/api/plex/debug-search')
def plex_debug_search():
    """Temporary: dump raw Plex discover search response for a query."""
    from settings_db import get_service as _get_svc
    import requests as _req
    svc = _get_svc('plex', 'default') or {}
    api_key = svc.get('api_key', '')
    query = request.args.get('q', 'Severance')
    resp = _req.get(
        'https://discover.provider.plex.tv/library/search',
        headers={'X-Plex-Token': api_key},
        params={'query': query, 'limit': 5, 'includeGuids': 1, 'searchTypes': 'movies,tv'},
        timeout=10,
    )
    return f"<pre>Status: {resp.status_code}\n\nHeaders:\n{dict(resp.headers)}\n\nBody:\n{resp.text[:3000]}</pre>"


@app.route('/api/plex/watchlist-enabled')
def plex_watchlist_enabled():
    """Return whether Plex is configured so the Discover page can show the watchlist button."""
    try:
        from settings_db import get_service as _get_svc
        svc = _get_svc('plex', 'default') or {}
        return jsonify({'enabled': bool(svc.get('api_key', ''))})
    except Exception:
        return jsonify({'enabled': False})


def _plex_watchlist_add_silent(tmdb_id: str, media_type: str, title: str = ''):
    """Add to Plex watchlist as a background side-effect of a Discover add."""
    try:
        from settings_db import get_service as _get_svc
        from integrations.plex import PlexIntegration
        svc = _get_svc('plex', 'default') or {}
        api_key = svc.get('api_key', '')
        if not api_key:
            return
        ok, detail = PlexIntegration().add_to_watchlist(api_key, str(tmdb_id), media_type, title)
        if ok:
            app.logger.info(f"Added TMDB {tmdb_id} ({media_type}) to Plex watchlist")
        else:
            app.logger.debug(f"Plex watchlist add skipped/failed for TMDB {tmdb_id}: {detail}")
    except Exception as e:
        app.logger.debug(f"Plex watchlist add error for TMDB {tmdb_id}: {e}")


@app.route('/api/plex/add-to-watchlist', methods=['POST'])
def plex_add_to_watchlist():
    """Add an item to the user's Plex watchlist by TMDB ID."""
    try:
        from settings_db import get_service as _get_svc
        from integrations.plex import PlexIntegration
        svc = _get_svc('plex', 'default') or {}
        api_key = svc.get('api_key', '')
        if not api_key:
            return jsonify({'success': False, 'error': 'Plex not configured'}), 503

        data = request.json or {}
        tmdb_id    = str(data.get('tmdb_id', ''))
        media_type = data.get('media_type', 'movie')
        title      = data.get('title', '')
        if not tmdb_id:
            return jsonify({'success': False, 'error': 'tmdb_id required'}), 400

        ok, detail = PlexIntegration().add_to_watchlist(api_key, tmdb_id, media_type, title)
        if ok:
            return jsonify({'success': True})
        return jsonify({'success': False, 'error': detail}), 502
    except Exception as e:
        app.logger.error(f"Error adding to Plex watchlist: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


def _enrich_tmdb_results(items):
    """Cross-reference TMDB results against Sonarr, Radarr, and pending discover queue."""
    # Build lookup sets from Sonarr and Radarr
    sonarr_by_tmdb = {}  # tmdb_id -> sonarr_series_id
    radarr_by_tmdb = {}  # tmdb_id -> radarr_movie_id
    pending_tmdb   = set()  # tmdb_ids queued from Discover but not yet in Sonarr

    try:
        prefs = sonarr_utils.load_preferences()
        sonarr_url = prefs.get('SONARR_URL')
        api_key = prefs.get('SONARR_API_KEY')
        if sonarr_url and api_key:
            headers = {'X-Api-Key': api_key}
            resp = http.get(f"{sonarr_url}/api/v3/series", headers=headers, timeout=10)
            if resp.ok:
                for s in resp.json():
                    if s.get('tmdbId'):
                        sonarr_by_tmdb[int(s['tmdbId'])] = s['id']
    except Exception:
        pass

    try:
        cfg, headers = _radarr_headers()
        if cfg:
            resp = http.get(f"{cfg['url'].rstrip('/')}/api/v3/movie", headers=headers, timeout=10)
            if resp.ok:
                for m in resp.json():
                    if m.get('tmdbId'):
                        radarr_by_tmdb[int(m['tmdbId'])] = m['id']
    except Exception:
        pass

    try:
        for pr in get_all_pending_requests():
            if pr.get('source') == 'discover' and pr.get('series_id') is None:
                tid = pr.get('tmdb_id')
                if tid:
                    pending_tmdb.add(int(tid))
    except Exception:
        pass

    enriched = []
    for item in items:
        media_type = item.get('media_type')
        if media_type not in ('movie', 'tv'):
            continue  # skip 'person' etc.

        tmdb_id = item.get('id')
        title = item.get('title') or item.get('name', '')
        year_raw = item.get('release_date') or item.get('first_air_date') or ''
        year = year_raw[:4] if year_raw else ''
        poster_path = item.get('poster_path')
        poster = f"https://image.tmdb.org/t/p/w342{poster_path}" if poster_path else None

        in_library = False
        library_id = None
        is_pending = False
        if media_type == 'tv' and tmdb_id in sonarr_by_tmdb:
            in_library = True
            library_id = sonarr_by_tmdb[tmdb_id]
        elif media_type == 'movie' and tmdb_id in radarr_by_tmdb:
            in_library = True
            library_id = radarr_by_tmdb[tmdb_id]
        elif media_type == 'tv' and tmdb_id in pending_tmdb:
            is_pending = True

        enriched.append({
            'tmdb_id': tmdb_id,
            'title': title,
            'year': year,
            'media_type': media_type,
            'poster': poster,
            'overview': item.get('overview', ''),
            'genres': [],  # genre_ids only in search; skip for now
            'vote_average': item.get('vote_average', 0),
            'in_library': in_library,
            'library_id': library_id,
            'pending': is_pending,
        })
    return enriched


@app.route('/create-rule', methods=['GET', 'POST'])
def create_rule():
    """Create a new rule."""
    if request.method == 'POST':
        config = load_config()
        rule_name = request.form.get('rule_name', '').strip()
        if not rule_name:
            return redirect(url_for('index', message="Rule name is required"))
        if rule_name in config['rules']:
            return redirect(url_for('index', message=f"Rule '{rule_name}' already exists"))
         
        # Parse dropdown values
        get_type = request.form.get('get_type', 'episodes')
        get_count = request.form.get('get_count', '').strip()
        keep_type = request.form.get('keep_type', 'episodes')
        keep_count = request.form.get('keep_count', '').strip()
        
        # Convert to integer or None for 'all' type
        get_count = None if get_type == 'all' else int(get_count) if get_count else 1
        keep_count = None if keep_type == 'all' else int(keep_count) if keep_count else 1
        
        # Parse grace fields
        grace_watched = request.form.get('grace_watched', '').strip()
        grace_unwatched = request.form.get('grace_unwatched', '').strip()
        dormant_days = request.form.get('dormant_days', '').strip()
        grace_scope = request.form.get('grace_scope', 'series')  # Default to 'series'
        
        grace_watched = None if not grace_watched else int(grace_watched)
        grace_unwatched = None if not grace_unwatched else int(grace_unwatched)
        dormant_days = None if not dormant_days else int(dormant_days)
        
        always_have = request.form.get('always_have', '').strip()
        ah_valid, ah_error = media_processor.validate_always_have_expression(always_have)
        if not ah_valid:
            return render_template('create_rule.html', error=ah_error, always_have_value=always_have)

        # Save rule
        config['rules'][rule_name] = {
            'description': request.form.get('description', ''),
            'get_type': get_type,
            'get_count': get_count,
            'keep_type': keep_type,
            'keep_count': keep_count,
            'action_option': request.form.get('action_option', 'monitor'),
            'monitor_watched': 'monitor_watched' in request.form,
            'grace_watched': grace_watched,
            'grace_unwatched': grace_unwatched,
            'dormant_days': dormant_days,
            'grace_scope': grace_scope,
            'keep_pilot': 'keep_pilot' in request.form,
            'release_keep_on_finale': 'release_keep_on_finale' in request.form,
            'always_have': always_have,
            'series': {},
            'dry_run': False
        }
        
        # Handle default rule setting
        if 'set_as_default' in request.form:
            config['default_rule'] = rule_name
        
        save_config(config)
        # NOTE: No longer updating delay profile here - only control tags are in delay profile
        try:
            tag_id = episeerr_utils.get_or_create_rule_tag_id(rule_name)
            if tag_id:
                app.logger.info(f"✓ Created/verified tag episeerr_{rule_name} with ID {tag_id}")
            else:
                app.logger.warning(f"⚠️ Could not create tag for rule '{rule_name}'")
        except Exception as e:
            app.logger.error(f"Error creating rule tag: {str(e)}")
        message = f"Rule '{rule_name}' created successfully"
        if 'set_as_default' in request.form:
            message += " and set as default"

        # Always redirect to rules page after creating a rule
        return redirect(url_for('rules_page'))
    return render_template('create_rule.html')


@app.route('/edit-rule/<rule_name>', methods=['GET', 'POST'])
def edit_rule(rule_name):
    """Edit an existing rule."""
    config = load_config()
    app.logger.info(f"Edit rule request for: '{rule_name}'")
    app.logger.info(f"Available rules: {list(config['rules'].keys())}")
    if rule_name not in config['rules']:
        return redirect(url_for('index', message=f"Rule '{rule_name}' not found"))
    
    if request.method == 'POST':
        # Parse dropdown values
        get_type = request.form.get('get_type', 'episodes')
        get_count = request.form.get('get_count', '').strip()
        keep_type = request.form.get('keep_type', 'episodes')
        keep_count = request.form.get('keep_count', '').strip()
        
        # Convert to integer or None for 'all' type
        get_count = None if get_type == 'all' else int(get_count) if get_count else 1
        keep_count = None if keep_type == 'all' else int(keep_count) if keep_count else 1
        
        # Parse grace fields
        grace_watched = request.form.get('grace_watched', '').strip()
        grace_unwatched = request.form.get('grace_unwatched', '').strip()
        dormant_days = request.form.get('dormant_days', '').strip()
        grace_scope = request.form.get('grace_scope', 'series')  # Default to 'series'
        
        grace_watched = None if not grace_watched else int(grace_watched)
        grace_unwatched = None if not grace_unwatched else int(grace_unwatched)
        dormant_days = None if not dormant_days else int(dormant_days)
        
        always_have = request.form.get('always_have', '').strip()
        ah_valid, ah_error = media_processor.validate_always_have_expression(always_have)
        if not ah_valid:
            rule = config['rules'][rule_name]
            return render_template('edit_rule.html', rule_name=rule_name, rule=rule,
                                   config=config, error=ah_error)

        # Update rule
        config['rules'][rule_name].update({
            'description': request.form.get('description', ''),
            'get_type': get_type,
            'get_count': get_count,
            'keep_type': keep_type,
            'keep_count': keep_count,
            'action_option': request.form.get('action_option', 'monitor'),
            'monitor_watched': 'monitor_watched' in request.form,
            'grace_watched': grace_watched,
            'grace_unwatched': grace_unwatched,
            'dormant_days': dormant_days,
            'grace_scope': grace_scope,
            'keep_pilot': 'keep_pilot' in request.form,
            'release_keep_on_finale': 'release_keep_on_finale' in request.form,
            'always_have': always_have
        })
        
        # Handle default rule setting
        if 'set_as_default' in request.form:
            config['default_rule'] = rule_name
        elif rule_name == config.get('default_rule') and 'set_as_default' not in request.form:
            # If this was the default rule but checkbox is unchecked, we need a new default
            # Set the first available rule as default, or remove default if this is the only rule
            other_rules = [r for r in config['rules'].keys() if r != rule_name]
            if other_rules:
                config['default_rule'] = other_rules[0]
            else:
                config.pop('default_rule', None)
        
        save_config(config)
        
        # NEW: Ensure tag exists (in case it was manually deleted)
        try:
            tag_id = episeerr_utils.get_or_create_rule_tag_id(rule_name)
            if tag_id:
                app.logger.debug(f"✓ Verified tag episeerr_{rule_name}")
        except Exception as e:
            app.logger.error(f"Error verifying rule tag: {str(e)}")
        
        message = f"Rule '{rule_name}' updated successfully"
        if 'set_as_default' in request.form and config.get('default_rule') == rule_name:
            message += " and set as default"
        
        return redirect(url_for('rules_page'))
    
    rule = config['rules'][rule_name]
    return render_template('edit_rule.html', rule_name=rule_name, rule=rule, config=config)
@app.route('/delete-rule/<rule_name>', methods=['POST'])
def delete_rule(rule_name):
    """Delete a rule and clean up its tag from Sonarr and delay profile."""
    config = load_config()
    if rule_name not in config['rules']:
        return redirect(url_for('index', message=f"Rule '{rule_name}' not found"))
    
    if rule_name == config.get('default_rule'):
        return redirect(url_for('index', message="Cannot delete the default rule"))
    
    # PROTECTION: Never delete special workflow tags
    if rule_name.lower() in ['select', 'default']:
        return redirect(url_for('index', message=f"Cannot delete special tag '{rule_name}'"))
    
    # Delete rule from config
    del config['rules'][rule_name]
    save_config(config)
    
    # Get tag ID for the deleted rule (do this before deleting)
    tag_id = None
    try:
        tag_id = episeerr_utils.get_or_create_rule_tag_id(rule_name)
    except Exception as e:
        app.logger.warning(f"Could not get tag ID for '{rule_name}': {str(e)}")
    
    # Clean up the tag from series in Sonarr
    tag_removed = False
    if tag_id:
        try:
            headers = episeerr_utils.get_sonarr_headers()
            series_response = http.get(f"{SONARR_URL}/api/v3/series", headers=headers)
            if series_response.ok:
                all_series = series_response.json()
                removed_from_count = 0
                
                for series in all_series:
                    tags = series.get('tags', [])
                    if tag_id in tags:
                        tags.remove(tag_id)
                        series['tags'] = tags
                        update_resp = http.put(
                            f"{SONARR_URL}/api/v3/series/{series['id']}",
                            headers=headers,
                            json=series
                        )
                        if update_resp.ok:
                            removed_from_count += 1
                            tag_removed = True
                        else:
                            app.logger.warning(f"Failed to remove tag from series {series['id']}")
                
                if removed_from_count > 0:
                    app.logger.info(f"Removed deleted rule tag '{rule_name}' from {removed_from_count} series")
                else:
                    app.logger.debug(f"No series had the tag for deleted rule '{rule_name}'")
            else:
                app.logger.error("Failed to fetch series list for tag cleanup")
        except Exception as e:
            app.logger.warning(f"Could not clean up tag for deleted rule '{rule_name}': {str(e)}")
    
    # Remove from delay profile (only the deleted tag, don't touch others)
    if tag_id:
        try:
            profile_id = episeerr_utils.get_episeerr_delay_profile_id()
            if profile_id:
                headers = episeerr_utils.get_sonarr_headers()
                get_resp = http.get(f"{SONARR_URL}/api/v3/delayprofile/{profile_id}", headers=headers)
                if get_resp.ok:
                    profile = get_resp.json()
                    current_tags = profile.get('tags', [])
                    if tag_id in current_tags:
                        current_tags.remove(tag_id)
                        profile['tags'] = current_tags
                        put_resp = http.put(
                            f"{SONARR_URL}/api/v3/delayprofile/{profile_id}",
                            headers=headers,
                            json=profile
                        )
                        if put_resp.ok:
                            app.logger.info(f"Removed deleted rule tag from delay profile {profile_id}")
                        else:
                            app.logger.warning(f"Failed to remove tag from delay profile")
        except Exception as e:
            app.logger.warning(f"Could not clean up delay profile for deleted rule: {str(e)}")
    
    # Delete the tag from Sonarr entirely (last step after all cleanup)
    if tag_id:
        try:
            headers = episeerr_utils.get_sonarr_headers()
            delete_resp = http.delete(
                f"{SONARR_URL}/api/v3/tag/{tag_id}",
                headers=headers
            )
            if delete_resp.ok:
                app.logger.info(f"✓ Deleted tag 'episeerr_{rule_name}' from Sonarr (ID: {tag_id})")
            else:
                app.logger.warning(f"Could not delete tag from Sonarr: {delete_resp.status_code}")
        except Exception as e:
            app.logger.warning(f"Could not delete tag from Sonarr: {str(e)}")
    
    message = f"Rule '{rule_name}' deleted successfully"
    if tag_removed:
        message += " (tag cleaned up from Sonarr)"
    
    return redirect(url_for('rules_page'))

@app.route('/assign-rules', methods=['POST'])
def assign_rules():
    """Assign series to rules while preserving activity data."""
    config = load_config()
    rule_name = request.form.get('rule_name')
    series_ids = request.form.getlist('series_ids')
    if not rule_name or rule_name not in config['rules']:
        referer = request.referrer or ''
        if '/rules' in referer:
            return redirect(url_for('rules_page'))
        return redirect(url_for('index', message="Invalid rule selected"))
    
    # STEP 1: Collect existing activity data BEFORE removing
    existing_activity = {}
    for series_id in series_ids:
        for rule, details in config['rules'].items():
            series_dict = details.get('series', {})
            if series_id in series_dict and isinstance(series_dict[series_id], dict):
                # Preserve the complete activity data
                existing_activity[series_id] = series_dict[series_id].copy()
                break
    
    # STEP 2: Remove from old rules
    for rule, details in config['rules'].items():
        series_dict = details.get('series', {})
        for series_id in series_ids:
            if series_id in series_dict:
                del series_dict[series_id]
    
    # STEP 3: Add to new rule WITH preserved activity data
    target_series_dict = config['rules'][rule_name].get('series', {})
    preserved_count = 0
    for series_id in series_ids:
        if series_id in existing_activity:
            target_series_dict[series_id] = existing_activity[series_id]  # Preserve complete data!
            preserved_count += 1
        else:
            target_series_dict[series_id] = {'activity_date': None}  # New series
    
    save_config(config)

    # Process always_have for newly assigned series (additive only - never unmonitors)
    rule_always_have = config['rules'][rule_name].get('always_have', '')
    if rule_always_have:
        for sid in series_ids:
            try:
                media_processor.process_always_have(int(sid), rule_always_have)
            except Exception as e:
                app.logger.error(f"always_have processing failed for series {sid}: {e}")

    # NEW STEP 4: Sync tags to Sonarr
    tag_sync_success = 0
    tag_sync_failed = 0

    for series_id in series_ids:
        try:
            success = episeerr_utils.sync_rule_tag_to_sonarr(int(series_id), rule_name)
            if success:
                tag_sync_success += 1
            else:
                tag_sync_failed += 1
                app.logger.warning(f"Failed to sync tag for series {series_id}")
        except Exception as e:
            tag_sync_failed += 1
            app.logger.error(f"Error syncing tag for series {series_id}: {str(e)}")
    
    # Build result message
    message = f"Assigned {len(series_ids)} series to rule '{rule_name}'"
    if preserved_count > 0:
        message += f" (preserved activity data for {preserved_count} series)"
    
    if tag_sync_success > 0:
        message += f" - synced {tag_sync_success} tags to Sonarr"
    if tag_sync_failed > 0:
        message += f" ({tag_sync_failed} tag syncs failed)"
    
    # Redirect back to where they came from
    referer = request.referrer or ''
    if '/rules' in referer:
        return redirect(url_for('rules_page'))
    return redirect(url_for('index', message=message))

@app.context_processor
def inject_service_urls():
    """Inject service URLs into all templates based on .env configuration."""
    services = {}
    
    # Default name and icon mappings for known services
    default_names = {
        'sonarr': 'Sonarr',
        'jellyseerr': 'Jellyseerr',
        'overseerr': 'Overseerr',
        'plex': 'Plex',
        'jellyfin': 'Jellyfin',
        'tautulli': 'Tautulli',
        'sabnzbd': 'SABnzbd',
        'prowlarr': 'Prowlarr',
        'radarr': 'Radarr'
    }
    
    default_icons = {
        'sonarr': 'fas fa-tv',              # TV icon for Sonarr (TV show management)
        'jellyseerr': 'fas fa-search',      # Search icon for Jellyseerr
        'overseerr': 'fas fa-search',       # Search icon for Overseerr
        'plex': 'fas fa-play-circle',       # Play icon for Plex
        'jellyfin': 'fas fa-film',          # Film icon for Jellyfin
        'tautulli': 'fas fa-chart-bar',     # Chart icon for Tautulli (analytics)
        'sabnzbd': 'fas fa-download',       # Download icon for SABnzbd (downloader)
        'prowlarr': 'fas fa-search-plus',   # Advanced search icon for Prowlarr (indexer)
        'radarr': 'fas fa-video'            # Video icon for Radarr (movie management)
    }
    
    # Iterate over environment variables to find those ending with _URL
    for key, value in os.environ.items():
        if key.endswith('_URL') and value.strip():  # skip empty values
            service_id = key[:-4].lower()  # remove _URL
            
            name_key = f"{key[:-4]}_NAME"
            service_name = os.getenv(name_key, default_names.get(service_id, service_id.capitalize()))
            
            icon_key = f"{key[:-4]}_ICON"
            service_icon = os.getenv(icon_key, default_icons.get(service_id, 'fas fa-link'))
            
            services[service_id] = {
                'name': service_name,
                'url': value.strip(),
                'icon': service_icon
            }
    
    return {'services': services, 'app_version': __version__}

# UPDATE unassign_series() function

@app.route('/unassign-series', methods=['POST'])
def unassign_series():
    """Unassign series from all rules."""
    config = load_config()
    series_ids = request.form.getlist('series_ids')
    total_removed = 0
    
    # Remove from config
    for rule_name, details in config['rules'].items():
        original_count = len(details.get('series', {}))
        series_dict = details.get('series', {})
        for series_id in series_ids:
            if series_id in series_dict:
                del series_dict[series_id]
        total_removed += original_count - len(details['series'])
    
    save_config(config)
    
    # NEW: Remove episeerr tags from Sonarr
    tag_removal_success = 0
    tag_removal_failed = 0
    
    for series_id in series_ids:
        try:
            success = episeerr_utils.remove_all_episeerr_tags(int(series_id))
            if success:
                tag_removal_success += 1
            else:
                tag_removal_failed += 1
        except Exception as e:
            tag_removal_failed += 1
            app.logger.error(f"Error removing tags from series {series_id}: {str(e)}")
    
    message = f"Unassigned {len(series_ids)} series from all rules"
    if tag_removal_success > 0:
        message += f" - removed tags from {tag_removal_success} series"
    if tag_removal_failed > 0:
        message += f" ({tag_removal_failed} tag removals failed)"
    
    referer = request.referrer or ''
    if '/rules' in referer:
        return redirect(url_for('rules_page'))
    return redirect(url_for('index', message=message))
# ============================================================================
# API ROUTES
# ============================================================================

@app.route('/api/series-with-titles')
def get_series_with_titles():
    """Get series list with titles and rule assignments for dropdowns."""
    try:
        config = load_config()
        all_series = get_sonarr_series()
        
        # Build assignment mapping
        assignments = {}
        for rule_name, details in config['rules'].items():
            series_dict = details.get('series', {})
            for series_id in series_dict.keys():
                assignments[str(series_id)] = rule_name
        
        # Format for dropdown
        series_list = []
        for series in all_series:
            series_id = str(series['id'])
            rule_name = assignments.get(series_id, 'Unassigned')
            
            # Add time-based cleanup info if assigned to a rule
            cleanup_info = ""
            if rule_name != 'Unassigned' and rule_name in config['rules']:
                rule = config['rules'][rule_name]
                grace_watched = rule.get('grace_watched')
                grace_unwatched = rule.get('grace_unwatched')
                dormant_days = rule.get('dormant_days')
                
                if grace_watched or grace_unwatched or dormant_days:
                    cleanup_parts = []
                    if grace_watched:
                        cleanup_parts.append(f"GW: {grace_watched}d")
                    if grace_unwatched:
                        cleanup_parts.append(f"GU: {grace_unwatched}d")
                    if dormant_days:
                        cleanup_parts.append(f"Dormant: {dormant_days}d")
                    cleanup_info = f" ({', '.join(cleanup_parts)})"
            
            series_list.append({
                'id': series_id,
                'title': series['title'],
                'rule': rule_name,
                'display_text': f"{series['title']} - Rule: {rule_name}{cleanup_info}"
            })
        
        # Sort by title
        series_list.sort(key=lambda x: x['title'].lower())
        
        return jsonify({
            'status': 'success',
            'series': series_list,
            'count': len(series_list)
        })
        
    except Exception as e:
        app.logger.error(f"Error getting series with titles: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/recent-activity')
def get_recent_activity():
    """Get recent activity (simplified version)."""
    try:
        return jsonify({
            "recentDownloads": [],
            "recentRuleApplications": []
        })
    except Exception as e:
        app.logger.error(f"Error getting recent activity: {str(e)}")
        return jsonify({
            "recentDownloads": [],
            "recentRuleApplications": []
        }), 500

@app.route('/api/series-stats')
def series_stats():
    """Get series statistics."""
    try:
        config = load_config()
        all_series = get_sonarr_series()
        all_series_ids = set(str(s['id']) for s in all_series)
        rules_mapping = {}
        for rule_name, details in config['rules'].items():
            for series_id in details.get('series', {}):
                if str(series_id) in all_series_ids:  # ignore stale config entries
                    rules_mapping[str(series_id)] = rule_name
        stats = {
            'total_series': len(all_series),
            'assigned_series': len(rules_mapping),
            'unassigned_series': len(all_series) - len(rules_mapping),
            'total_rules': len(config['rules']),
            'rule_breakdown': {}
        }
        for rule_name, details in config['rules'].items():
            count = sum(1 for sid in details.get('series', {}) if str(sid) in all_series_ids)
            stats['rule_breakdown'][rule_name] = count
        return jsonify(stats)
    except requests.exceptions.ConnectionError:
        return jsonify({
            "status": "error",
            "message": "Sonarr unavailable",
            "series_count": 0,
            "monitored": 0,
            "unassigned": 0
        }), 503
    except Exception as e:
        app.logger.error(f"Error getting series stats: {str(e)}")
        return jsonify({
            "status": "error",
            "message": str(e),
            "series_count": 0,
            "monitored": 0,
            "unassigned": 0
        }), 500

def cleanup_config_rules():
    """Clean up config by removing non-existent series AND comprehensive tag reconciliation."""
    try:
        config = load_config()
        existing_series = get_sonarr_series()
        existing_series_ids = set(str(series['id']) for series in existing_series)
        changes_made = False
        
        # Remove series that don't exist in Sonarr
        for rule_name, rule_details in config['rules'].items():
            original_count = len(rule_details.get('series', {}))
            rule_details['series'] = {
                series_id: details for series_id, details in rule_details.get('series', {}).items()
                if series_id in existing_series_ids
            }
            if len(rule_details['series']) != original_count:
                removed_count = original_count - len(rule_details['series'])
                app.logger.info(f"Cleaned up rule '{rule_name}': Removed {removed_count} non-existent series")
                changes_made = True

        # Remove movies that don't exist in Radarr
        try:
            cfg, radarr_headers = _radarr_headers()
            if cfg:
                movies_resp = http.get(f"{cfg['url'].rstrip('/')}/api/v3/movie", headers=radarr_headers, timeout=15)
                if movies_resp.ok:
                    existing_movie_ids = {str(m['id']) for m in movies_resp.json()}
                    for rule_name, rule_details in config.get('movie_rules', {}).items():
                        original_count = len(rule_details.get('movies', {}))
                        rule_details['movies'] = {
                            mid: details for mid, details in rule_details.get('movies', {}).items()
                            if mid in existing_movie_ids
                        }
                        if len(rule_details['movies']) != original_count:
                            removed_count = original_count - len(rule_details['movies'])
                            app.logger.info(f"Cleaned up movie rule '{rule_name}': Removed {removed_count} non-existent movies")
                            changes_made = True
        except Exception as e:
            app.logger.warning(f"Could not clean movie rules (Radarr unreachable?): {e}")
        
        
        
        # Tag reconciliation (create tags, drift, orphaned)
        app.logger.info("Starting tag reconciliation during cleanup...")

        created, failed = migrate_create_rule_tags()
        if created > 0 or failed > 0:
            app.logger.info(f"  Tag creation: {created} verified, {failed} failed")

        # Collect all series IDs to check: known + orphaned candidates
        known_ids = [
            int(sid)
            for rule_details in config['rules'].values()
            for sid in list(rule_details.get('series', {}).keys())
        ]
        config_series_ids = {
            sid
            for rule_details in config['rules'].values()
            for sid in rule_details.get('series', {}).keys()
        }
        orphaned_ids = [
            s['id'] for s in existing_series
            if str(s['id']) not in config_series_ids
        ]

        series_lookup = {s['id']: s for s in existing_series}
        reconciled = 0
        for series_id in known_ids + orphaned_ids:
            try:
                _, changed = episeerr_utils.reconcile_series_drift(series_id, config, series_data=series_lookup.get(series_id))
                if changed:
                    reconciled += 1
                    changes_made = True
            except Exception as e:
                app.logger.error(f"  Error reconciling series {series_id}: {e}")

        if changes_made:
            save_config(config)
            app.logger.info(f"✓ Tag reconciliation complete: {reconciled} corrections made")
        else:
            app.logger.info("✓ Tag reconciliation complete: No changes needed")
            
    except requests.exceptions.ConnectionError:
        app.logger.warning("Sonarr not reachable during cleanup - skipping")
        return
    except Exception as e:
        app.logger.error(f"Error during config cleanup: {str(e)}")

@app.route('/cleanup')
def cleanup():
    """Clean up configuration."""
    cleanup_config_rules()
    referer = request.referrer or ''
    if '/rules' in referer:
        return redirect(url_for('rules_page'))
    return redirect(url_for('index', message="Configuration cleaned up successfully"))

@app.route('/api/series-with-status')
def api_series_with_status():
    """Get series with enhanced status information for the sortable table."""
    try:
        config = load_config()
        all_series = get_sonarr_series()
        
        # Build assignment mapping
        assignments = {}
        for rule_name, details in config['rules'].items():
            series_dict = details.get('series', {})
            for series_id in series_dict.keys():
                assignments[str(series_id)] = rule_name
        
        enhanced_series = []
        for series in all_series:
            series_id = str(series['id'])
            
            # Get assigned rule
            assigned_rule = assignments.get(series_id, 'None')
            
            # Determine status from Sonarr data
            status = 'unknown'
            if series.get('ended'):
                status = 'ended'
            elif series.get('status') == 'continuing':
                status = 'continuing'
            elif series.get('status') == 'upcoming':
                status = 'upcoming'
            
            # Get last episode info
            last_episode = None
            if series.get('lastInfoSync'):
                last_episode = series['lastInfoSync'][:10]  # Extract date part
            elif series.get('previousAiring'):
                last_episode = series['previousAiring'][:10]
            
            enhanced_series.append({
                'id': series['id'],
                'title': series['title'],
                'assigned_rule': assigned_rule,
                'status': status,
                'year': series.get('year'),
                'lastEpisode': last_episode,
                'titleSlug': series.get('titleSlug'),
                'ended': series.get('ended', False)
            })
        
        return jsonify({
            'success': True,
            'series': enhanced_series
        })
        
    except Exception as e:
        app.logger.error(f"Error in series-with-status API: {str(e)}")
        return jsonify({'success': False, 'message': str(e)}), 500

# ============================================================================
# SCHEDULER & SETTINGS ROUTES
# ============================================================================

@app.route('/scheduler')
def scheduler_admin():
    """Scheduler administration page."""
    config = load_config()
    return render_template('scheduler_admin.html', config=config)

@app.route('/api/scheduler-status')
def scheduler_status():
    """Get scheduler status."""
    try:
        if 'cleanup_scheduler' not in globals():
            return jsonify({"status": "error", "message": "Scheduler not initialized"}), 500
        return jsonify(cleanup_scheduler.get_status())
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/global-settings')
def get_global_settings():
    """Get global settings including storage gate."""
    try:
        import media_processor
        settings = media_processor.load_global_settings()
        
        # Get current disk space for display
        disk_info = media_processor.get_sonarr_disk_space()
        
        return jsonify({
            "status": "success",
            "settings": settings,
            "disk_info": disk_info
        })
    except Exception as e:
        app.logger.error(f"Error getting global settings: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/global-settings', methods=['POST'])
def update_global_settings():
    """Update global settings."""
    try:
        import media_processor
        
        data = request.json
        storage_min_gb = data.get('global_storage_min_gb')
        cleanup_interval_hours = data.get('cleanup_interval_hours', 6)
        dry_run_mode = data.get('dry_run_mode', False)
        auto_assign_new_series = data.get('auto_assign_new_series', False)
        
        
        # NEW: Notification settings
        notifications_enabled = data.get('notifications_enabled', False)
        discord_webhook_url = data.get('discord_webhook_url', '')
        episeerr_url = data.get('episeerr_url', 'http://localhost:5002')
        notify_aired_not_downloaded = data.get('notify_aired_not_downloaded', False)

        # Validate inputs
        if storage_min_gb is not None:
            storage_min_gb = int(storage_min_gb) if storage_min_gb else None

        settings = {
            'global_storage_min_gb': storage_min_gb,
            'cleanup_interval_hours': int(cleanup_interval_hours),
            'dry_run_mode': bool(dry_run_mode),
            'auto_assign_new_series': bool(auto_assign_new_series),

            # NEW: Save notification settings
            'notifications_enabled': bool(notifications_enabled),
            'discord_webhook_url': str(discord_webhook_url),
            'episeerr_url': str(episeerr_url),
            'notify_aired_not_downloaded': bool(notify_aired_not_downloaded),
        }
        
        media_processor.save_global_settings(settings)
        
        app.logger.info(f"Global settings updated: {settings}")
        
        return jsonify({
            "status": "success",
            "message": "Global settings updated successfully",
            "settings": settings
        })
        
    except Exception as e:
        app.logger.error(f"Error updating global settings: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/scheduler-status-global')
def scheduler_status_global():
    """Enhanced scheduler status with global settings."""
    try:
        import media_processor
        
        if 'cleanup_scheduler' not in globals():
            return jsonify({"status": "error", "message": "Scheduler not initialized"}), 500
        
        # Get basic scheduler status
        status = cleanup_scheduler.get_status()
        
        # Add global settings
        global_settings = media_processor.load_global_settings()
        status["global_settings"] = global_settings
        
        # Add disk info
        disk_info = media_processor.get_sonarr_disk_space()
        if disk_info:
            status["disk_info"] = disk_info
            
            # Check if storage gate would trigger
            storage_min_gb = global_settings.get('global_storage_min_gb')
            if storage_min_gb:
                gate_open = disk_info['free_space_gb'] < storage_min_gb
                status["storage_gate"] = {
                    "enabled": True,
                    "threshold_gb": storage_min_gb,
                    "current_free_gb": disk_info['free_space_gb'],
                    "gate_open": gate_open,
                    "status": "OPEN - Cleanup will run" if gate_open else "CLOSED - No cleanup needed"
                }
            else:
                status["storage_gate"] = {
                    "enabled": False,
                    "status": "Disabled - Cleanup always runs on schedule"
                }
        
        # Add configuration summary (series + movie rules)
        config = load_config()
        rule_summary = {
            "total_rules": len(config.get('rules', {})),
            "cleanup_rules": 0,
            "protected_rules": 0,
            "movie_rules": len(config.get('movie_rules', {})),
            "movie_cleanup_rules": 0,
        }

        for rule_name, rule in config.get('rules', {}).items():
            has_cleanup = rule.get('grace_watched') or rule.get('grace_unwatched') or rule.get('dormant_days')
            if has_cleanup:
                rule_summary["cleanup_rules"] += 1
            else:
                rule_summary["protected_rules"] += 1

        for rule_name, rule in config.get('movie_rules', {}).items():
            if rule.get('grace_watched') or rule.get('grace_unwatched'):
                rule_summary["movie_cleanup_rules"] += 1

        status["rule_summary"] = rule_summary
        return jsonify(status)
        
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/force-cleanup', methods=['POST'])
def force_cleanup():
    """Force cleanup manually."""
    try:
        print("Manual cleanup requested via API")
        result = cleanup_scheduler.force_cleanup()
        print("Manual cleanup started successfully")
        return jsonify({"status": "success", "message": result})
    except Exception as e:
        print(f"Failed to start manual cleanup: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/safety-status')
def safety_status():
    """Get dry run safety status."""
    try:
        config = load_config()
        global_dry_run = os.getenv('CLEANUP_DRY_RUN', 'false').lower() == 'true'
        rules_with_dry_run = []
        for rule_name, rule_details in config.get('rules', {}).items():
            if rule_details.get('dry_run', False):
                rules_with_dry_run.append(rule_name.replace('_', ' ').title())
        return jsonify({
            "global_dry_run": global_dry_run,
            "rules_with_dry_run": rules_with_dry_run,
            "total_rules": len(config.get('rules', {})),
            "status": "success"
        })
    except Exception as e:
        current_app.logger.error(f"Error getting safety status: {str(e)}")
        return jsonify({
            "global_dry_run": False,
            "rules_with_dry_run": [],
            "total_rules": 0,
            "status": "error",
            "message": str(e)
        }), 500

@app.route('/api/current-assignments')
def get_current_assignments():
    """Get current rule assignments for all series."""
    try:
        config = load_config()
        all_series = get_sonarr_series()
        
        assignments = {}
        
        # Build assignment mapping
        for rule_name, details in config['rules'].items():
            series_dict = details.get('series', {})
            for series_id in series_dict.keys():
                assignments[str(series_id)] = rule_name
        
        # Add series without assignments
        for series in all_series:
            series_id = str(series['id'])
            if series_id not in assignments:
                assignments[series_id] = 'None'
        
        return jsonify(assignments)
        
    except Exception as e:
        app.logger.error(f"Error getting current assignments: {str(e)}")
        return jsonify({}), 500

@app.route('/api/quick-stats')
def get_quick_stats():
    """Get quick stats for change detection."""
    try:
        config = load_config()
        all_series = get_sonarr_series()
        
        # Count assigned series
        assigned_count = 0
        for rule_name, details in config['rules'].items():
            assigned_count += len(details.get('series', {}))
        
        stats = {
            'total_series': len(all_series),
            'assigned_series': assigned_count,
            'unassigned_series': len(all_series) - assigned_count,
            'total_rules': len(config['rules']),
            'timestamp': int(time.time())
        }
        
        return jsonify(stats)
        
    except Exception as e:
        app.logger.error(f"Error getting quick stats: {str(e)}")
        return jsonify({
            'total_series': 0,
            'assigned_series': 0,
            'unassigned_series': 0,
            'total_rules': 0,
            'timestamp': int(time.time())
        }), 500

# ============================================================================
# DRY RUN SETTINGS
# ============================================================================

@app.route('/dry-run-settings', methods=['GET', 'POST'])
def dry_run_settings():
    """Manage dry run settings."""
    if request.method == 'POST':
        try:
            config = load_config()

            for rule_name in config.get('rules', {}).keys():
                rule_dry_run_key = f'rule_dry_run_{rule_name}'
                config['rules'][rule_name]['dry_run'] = rule_dry_run_key in request.form

            save_config(config)
            app.logger.info("Dry run settings saved")
            
            return redirect(url_for('scheduler_admin', message="Dry run settings saved successfully"))
        except Exception as e:
            current_app.logger.error(f"Error saving dry run settings: {str(e)}")
            return redirect(url_for('scheduler_admin', message=f"Error saving settings: {str(e)}"))
    try:
        config = load_config()
        global_dry_run = os.getenv('CLEANUP_DRY_RUN', 'false').lower() == 'true'
        return render_template('dry_run_settings.html',
                             config=config,
                             global_dry_run=global_dry_run)
    except Exception as e:
        current_app.logger.error(f"Error loading dry run settings: {str(e)}")
        return redirect(url_for('scheduler_admin', message=f"Error loading settings: {str(e)}"))

# ============================================================================
# CLEANUP LOGS
# ============================================================================

@app.route('/cleanup-logs')
def cleanup_logs():
    """Display cleanup logs."""
    try:
        CLEANUP_LOG_PATH = os.getenv('CLEANUP_LOG_PATH', '/app/logs/cleanup.log')
        if not os.path.exists(CLEANUP_LOG_PATH):
            return render_simple_logs_page("No cleanup logs found yet.")
        try:
            with open(CLEANUP_LOG_PATH, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        except Exception as e:
            return render_simple_logs_page(f"Error reading log file: {str(e)}")
        recent_lines = lines[-200:] if len(lines) > 200 else lines
        recent_lines.reverse()
        try:
            return render_template('cleanup_logs.html', logs=recent_lines)
        except:
            return render_simple_logs_page(recent_lines)
    except Exception as e:
        current_app.logger.error(f"Error in cleanup logs route: {str(e)}")
        return render_simple_logs_page(f"Error loading logs: {str(e)}")

def render_simple_logs_page(logs_or_message):
    """Render simple logs page fallback."""
    if isinstance(logs_or_message, str):
        content = f'<div class="alert alert-warning">{logs_or_message}</div>'
    else:
        content = '<div style="font-family: monospace; font-size: 0.9em; max-height: 600px; overflow-y: auto; border: 1px solid #333; padding: 1rem; background: #1a1a1a;">'
        for line in logs_or_message:
            line_class = ""
            if 'ERROR' in line or 'Failed' in line:
                line_class = 'style="color: #ff6b6b;"'
            elif 'DRY RUN' in line:
                line_class = 'style="color: #74c0fc;"'
            elif 'CLEANUP STARTED' in line or 'CLEANUP COMPLETED' in line:
                line_class = 'style="color: #51cf66;"'
            elif 'deleted' in line and 'would delete' not in line:
                line_class = 'style="color: #ffd43b;"'
            elif 'SKIPPED' in line:
                line_class = 'style="color: #868e96;"'
            else:
                line_class = 'style="color: #f8f9fa;"'
            content += f'<div {line_class}>{line.strip()}</div>'
        content += '</div>'
    html = f'''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Cleanup Logs - OCDarr</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/css/bootstrap.min.css" rel="stylesheet">
        <style>
            body {{ background-color: #1a1a1a; color: #e0e0e0; }}
            .navbar {{ background-color: #2d2d2d !important; }}
            .card {{ background-color: #2d2d2d; border: 1px solid #404040; }}
        </style>
    </head>
    <body>
        <nav class="navbar navbar-expand-lg navbar-dark">
            <div class="container">
                <a class="navbar-brand" href="/">OCDarr</a>
            </div>
        </nav>
        <div class="container mt-4">
            <div class="card">
                <div class="card-header d-flex justify-content-between">
                    <h5><i class="fas fa-file-alt me-2"></i>Cleanup Logs</h5>
                    <a href="/scheduler" class="btn btn-primary btn-sm">Back to Scheduler</a>
                </div>
                <div class="card-body">
                    {content}
                </div>
            </div>
        </div>
    </body>
    </html>
    '''
    return html

# ============================================================================
# EPISEERR FUNCTIONALITY
# ============================================================================

@app.route('/episeerr')
def episeerr_index():
    """Pending items page - shows requests AND deletion summary"""
    import pending_deletions
    
    # Get pending deletions summary
    deletion_summary = pending_deletions.get_pending_deletions_summary()
    
    return render_template('episeerr_index.html', deletions=deletion_summary)

@app.route('/search')
def search_page():
    q = request.args.get('q', '').strip()
    return render_template('search.html', q=q)


@app.route('/api/send-to-selection/<int:series_id>')
def send_to_selection(series_id):
    """Create a pending selection request for a Sonarr series and redirect to season selection."""
    try:
        # Check if a pending request already exists for this series — reuse it if so
        existing = find_pending_request_by_series(series_id)
        if existing and existing.get('tmdb_id'):
            config = load_config()
            current_rule = next(
                (rn for rn, rd in config.get('rules', {}).items() if str(series_id) in rd.get('series', {})), ''
            )
            app.logger.info(f"Reusing existing pending request for series {series_id}")
            return redirect(url_for('select_seasons', tmdb_id=existing['tmdb_id'], current_rule=current_rule))

        sonarr_preferences = sonarr_utils.load_preferences()
        headers = {
            'X-Api-Key': sonarr_preferences['SONARR_API_KEY'],
            'Content-Type': 'application/json'
        }
        sonarr_url = sonarr_preferences['SONARR_URL']

        # Get series info from Sonarr
        resp = http.get(f"{sonarr_url}/api/v3/series/{series_id}", headers=headers, timeout=10)
        if not resp.ok:
            return render_template('error.html', message=f"Could not find series in Sonarr (ID: {series_id})")

        series = resp.json()
        series_title = series.get('title', 'Unknown')
        tvdb_id = series.get('tvdbId')
        tmdb_id = series.get('tmdbId')  # Sonarr v4+ provides this

        # Look up TMDB ID via TMDB find-by-external-id if Sonarr didn't give us one
        if not tmdb_id and tvdb_id:
            try:
                find_result = get_tmdb_endpoint(f"find/{tvdb_id}", {'external_source': 'tvdb_id'})
                tv_results = (find_result or {}).get('tv_results', [])
                if tv_results:
                    tmdb_id = tv_results[0]['id']
                else:
                    search_results = search_tv_shows(series_title)
                    if (search_results or {}).get('results'):
                        tmdb_id = search_results['results'][0]['id']
            except Exception as e:
                app.logger.error(f"Error finding TMDB ID for {series_title}: {e}")

        if not tmdb_id:
            return render_template('error.html', message=f"Could not determine TMDB ID for \"{series_title}\"")

        # Create a selection request in the DB
        request_id = f"sonarr-select-{series_id}-{int(time.time())}"
        pending_request = {
            "id": request_id,
            "series_id": series_id,
            "title": series_title,
            "needs_season_selection": True,
            "tmdb_id": tmdb_id,
            "tvdb_id": tvdb_id,
            "source": "sonarr",
            "source_name": "Sonarr Episode Selection",
            "needs_attention": True,
            "jellyseerr_request_id": None,
            "created_at": int(time.time())
        }
        add_pending_request(pending_request)

        app.logger.info(f"✓ Created manual selection request for {series_title} (TMDB: {tmdb_id})")

        # Find which rule this series is currently assigned to
        config = load_config()
        current_rule = ''
        series_id_str = str(series_id)
        for rule_name_iter, rule_data in config.get('rules', {}).items():
            if series_id_str in rule_data.get('series', {}):
                current_rule = rule_name_iter
                break

        return redirect(url_for('select_seasons', tmdb_id=tmdb_id, current_rule=current_rule))

    except Exception as e:
        app.logger.error(f"Error in send_to_selection for series {series_id}: {e}")
        return render_template('error.html', message=f"Error: {str(e)}")


@app.route('/select-seasons/<tmdb_id>')
def select_seasons(tmdb_id):
    """Show season selection page."""
    try:
        # Get TV show details from TMDB
        show_data = get_tmdb_endpoint(f"tv/{tmdb_id}")
        
        if not show_data:
            return render_template('error.html', message="Failed to get show details from TMDB")
        
        # Format show data for template
        formatted_show = {
            'id': tmdb_id,
            'name': show_data.get('name', 'Unknown Series'),
            'overview': show_data.get('overview', ''),
            'posterUrl': f"https://image.tmdb.org/t/p/w300{show_data['poster_path']}" if show_data.get('poster_path') else '/static/placeholder-banner.png',
            'seasons': []
        }
        
        for season in show_data.get('seasons', []):
            if season.get('season_number', 0) > 0:
                formatted_show['seasons'].append({
                    'seasonNumber': season['season_number'],
                    'episodeCount': season.get('episode_count', '?')
                })
        
        # NEW: Load available rules for the rule picker
        config = load_config()
        rules = []
        default_rule = config.get('default_rule')
        for rule_name, rule_data in config.get('rules', {}).items():
            rules.append({
                'name': rule_name,
                'is_default': rule_name == default_rule,
                'get_type': rule_data.get('get_type', 'episodes'),
                'get_count': rule_data.get('get_count', 1),
                'description': f"{rule_data.get('get_type', 'episodes')} × {rule_data.get('get_count', 1)}"
            })
        
        current_rule = request.args.get('current_rule', '')

        # Look up the pending request ID for this tmdb_id so the template can delete it on cancel
        request_id = ''
        try:
            req_data = find_pending_request_by_tmdb(tmdb_id)
            if req_data:
                request_id = req_data.get('id', '')
        except Exception:
            pass

        return render_template('season_selection.html',
                            show=formatted_show,
                            tmdb_id=tmdb_id,
                            rules=rules,
                            default_rule=default_rule,
                            current_rule=current_rule,
                            request_id=request_id)

    except Exception as e:
        app.logger.error(f"Error in select_seasons: {str(e)}", exc_info=True)
        return render_template('error.html', message=f"Error loading season selection: {str(e)}")

@app.route('/api/apply-rule-to-selection', methods=['POST'])
def apply_rule_to_selection():


    try:
        tmdb_id = request.form.get('tmdb_id')
        rule_name = request.form.get('rule_name')
        
        if not tmdb_id or not rule_name:
            return redirect(url_for('rules_page'))
        
        # Find the pending request to get series_id
        series_id = None
        request_id = None
        request_data = find_pending_request_by_tmdb(tmdb_id)
        if request_data:
            series_id = request_data.get('series_id')
            request_id = request_data.get('id')

        if not series_id:
            # Deferred add: series was queued from Discover or Search without adding to Sonarr yet
            if request_data and request_data.get('source') in ('discover', 'search'):
                try:
                    prefs = sonarr_utils.load_preferences()
                    s_url = prefs.get('SONARR_URL')
                    s_key = prefs.get('SONARR_API_KEY')
                    if not s_url or not s_key:
                        app.logger.error("Sonarr not configured — cannot add deferred series")
                        return redirect(url_for('rules_page'))

                    s_headers = {'X-Api-Key': s_key, 'Content-Type': 'application/json'}
                    lookup_data = request_data.get('sonarr_lookup') or {}
                    payload = {
                        'title':            lookup_data.get('title'),
                        'titleSlug':        lookup_data.get('titleSlug'),
                        'qualityProfileId': int(request_data['quality_profile_id']),
                        'rootFolderPath':   request_data['root_folder_path'],
                        'tvdbId':           lookup_data.get('tvdbId'),
                        'images':           lookup_data.get('images', []),
                        'seasons':          lookup_data.get('seasons', []),
                        'monitored':        False,
                        'addOptions': {'searchForMissingEpisodes': False, 'monitor': 'none'},
                    }
                    add_resp = http.post(f"{s_url}/api/v3/series",
                                         headers=s_headers, json=payload, timeout=15)
                    if add_resp.status_code not in (200, 201):
                        app.logger.error(f"Failed to add deferred series to Sonarr: {add_resp.text}")
                        return redirect(url_for('rules_page'))

                    new_series = add_resp.json()
                    series_id = new_series['id']
                    # Update pending request with new series_id so later steps work
                    request_data['series_id'] = series_id
                    add_pending_request(request_data)
                    app.logger.info(f"Added '{request_data.get('title')}' to Sonarr (ID {series_id}) from {request_data.get('source')} queue")
                except Exception as e:
                    app.logger.error(f"Error adding deferred series to Sonarr: {e}")
                    return redirect(url_for('rules_page'))
            else:
                return redirect(url_for('rules_page'))

        config = load_config()

        if rule_name not in config.get('rules', {}):
            return redirect(url_for('rules_page'))
        
        # Remove series from any rule it was previously in
        series_id_str = str(series_id)
        for rname, rdata in config['rules'].items():
            if rname != rule_name and series_id_str in rdata.get('series', {}):
                del rdata['series'][series_id_str]
                app.logger.info(f"✓ Removed series {series_id} from rule '{rname}'")

        # Assign series to the chosen rule
        target_rule = config['rules'][rule_name]
        target_rule.setdefault('series', {})
        target_rule['series'][series_id_str] = {'activity_date': None}
        save_config(config)

        _rule_cfg = config['rules'][rule_name]
        _always_have = _rule_cfg.get('always_have', '')
        _request_source = request_data.get('source', '')
        _rule_headers = {'X-Api-Key': SONARR_API_KEY}

        # always_have: monitor matching episodes (additive, never unmonitors)
        if _always_have:
            try:
                media_processor.process_always_have(series_id, _always_have)
            except Exception as e:
                app.logger.error(f"always_have processing failed for series {series_id}: {e}")

        # For new shows (not a series_page reassignment) also run get_type/get_count monitoring
        if _request_source != 'series_page':
            try:
                _get_type = _rule_cfg.get('get_type', 'episodes')
                _get_count = _rule_cfg.get('get_count', 1)
                _action_option = _rule_cfg.get('action_option', 'monitor')

                _eps_resp = http.get(
                    f"{SONARR_URL}/api/v3/episode?seriesId={series_id}",
                    headers=_rule_headers
                )
                if _eps_resp.ok:
                    _all_eps = _eps_resp.json()
                    _starting_season = 1
                    _season_eps = sorted(
                        [ep for ep in _all_eps if ep.get('seasonNumber') == _starting_season],
                        key=lambda x: x.get('episodeNumber', 0)
                    )

                    _to_monitor = []
                    if _get_type == 'all':
                        _to_monitor = [ep['id'] for ep in _all_eps if ep.get('seasonNumber', 0) >= _starting_season]
                    elif _get_type == 'seasons':
                        _n = _get_count if _get_count is not None else 1
                        _to_monitor = [
                            ep['id'] for ep in _all_eps
                            if _starting_season <= ep.get('seasonNumber', 0) < (_starting_season + _n)
                        ]
                    else:  # episodes
                        _n = _get_count if _get_count is not None else 1
                        _to_monitor = [ep['id'] for ep in _season_eps[:_n]]

                    if _to_monitor:
                        _mon_resp = http.put(
                            f"{SONARR_URL}/api/v3/episode/monitor",
                            headers=_rule_headers,
                            json={"episodeIds": _to_monitor, "monitored": True}
                        )
                        if _mon_resp.ok:
                            app.logger.info(
                                f"Monitored {len(_to_monitor)} episodes (get_type={_get_type}) "
                                f"for series {series_id}"
                            )
                            if _action_option == 'search':
                                _srch_resp = http.post(
                                    f"{SONARR_URL}/api/v3/command",
                                    headers=_rule_headers,
                                    json={"name": "EpisodeSearch", "episodeIds": _to_monitor}
                                )
                                if _srch_resp.ok:
                                    app.logger.info(f"Triggered episode search for series {series_id}")
                                else:
                                    app.logger.error(f"Episode search failed: {_srch_resp.text}")
                        else:
                            app.logger.error(f"Failed to monitor episodes: {_mon_resp.text}")
            except Exception as e:
                app.logger.error(f"get_type monitoring failed for series {series_id}: {e}")

        # Sync the rule tag to Sonarr
        try:
            episeerr_utils.sync_rule_tag_to_sonarr(series_id, rule_name)
            app.logger.info(f"✓ Synced tag episeerr_{rule_name} for series {series_id}")
        except Exception as e:
            app.logger.error(f"Tag sync failed: {e}")
        
        # Clean up the pending request
        if request_id:
            delete_pending_request(request_id)
            app.logger.info(f"✓ Removed pending request {request_id}")
        
        # Remove episeerr_select tag (keep rule tag)
        try:
            tag_resp = http.get(f"{SONARR_URL}/api/v3/tag", headers=headers)
            if tag_resp.ok:
                tag_map = {t['label'].lower(): t['id'] for t in tag_resp.json()}
                select_tag_id = tag_map.get('episeerr_select')
                
                if select_tag_id:
                    series_resp = http.get(f"{SONARR_URL}/api/v3/series/{series_id}", headers=headers)
                    if series_resp.ok:
                        series_data = series_resp.json()
                        current_tags = series_data.get('tags', [])
                        if select_tag_id in current_tags:
                            current_tags.remove(select_tag_id)
                            series_data['tags'] = current_tags
                            http.put(f"{SONARR_URL}/api/v3/series", headers=headers, json=series_data)
        except Exception as e:
            app.logger.debug(f"Tag cleanup: {e}")
        
        app.logger.info(f"Applied rule '{rule_name}' to {request_data.get('title', 'series')}")
        _plex_watchlist_add_silent(tmdb_id, 'tv', request_data.get('title', ''))
        return redirect(url_for('rules_page'))

    except Exception as e:
        app.logger.error(f"Error applying rule to selection: {e}", exc_info=True)
        return redirect(url_for('rules_page'))

@app.route('/select-episodes/<tmdb_id>')
def select_episodes(tmdb_id):
    """Show episode selection page after season selection."""
    try:
        # Get selected seasons from URL parameter
        selected_seasons_param = request.args.get('seasons', '1')
        try:
            selected_seasons = [int(s.strip()) for s in selected_seasons_param.split(',') if s.strip()]
        except ValueError:
            app.logger.error(f"Invalid seasons parameter: {selected_seasons_param}")
            selected_seasons = [1]  # Fallback to season 1
            
        if not selected_seasons:
            selected_seasons = [1]  # Ensure we always have at least one season
        
        app.logger.info(f"Episode selection for TMDB ID {tmdb_id}, seasons: {selected_seasons}")
        
        # Find the corresponding series_id from pending requests
        req = find_pending_request_by_tmdb(tmdb_id)
        if not req:
            return render_template('error.html', message="No pending request found for this series")
        series_id = req.get('series_id')
        request_id = req.get('id')

        # Deferred add: add to Sonarr now before episode selection
        if not series_id and req.get('source') in ('discover', 'search'):
            try:
                prefs = sonarr_utils.load_preferences()
                s_url = prefs.get('SONARR_URL')
                s_key = prefs.get('SONARR_API_KEY')
                if s_url and s_key:
                    s_headers = {'X-Api-Key': s_key, 'Content-Type': 'application/json'}
                    lookup_data = req.get('sonarr_lookup') or {}
                    payload = {
                        'title':            lookup_data.get('title'),
                        'titleSlug':        lookup_data.get('titleSlug'),
                        'qualityProfileId': int(req['quality_profile_id']),
                        'rootFolderPath':   req['root_folder_path'],
                        'tvdbId':           lookup_data.get('tvdbId'),
                        'images':           lookup_data.get('images', []),
                        'seasons':          lookup_data.get('seasons', []),
                        'monitored':        False,
                        'addOptions': {'searchForMissingEpisodes': False, 'monitor': 'none'},
                    }
                    add_resp = http.post(f"{s_url}/api/v3/series",
                                         headers=s_headers, json=payload, timeout=15)
                    if add_resp.status_code in (200, 201):
                        new_series = add_resp.json()
                        series_id = new_series['id']
                        req['series_id'] = series_id
                        add_pending_request(req)
                        app.logger.info(f"Added '{req.get('title')}' to Sonarr (ID {series_id}) for episode selection")
            except Exception as e:
                app.logger.error(f"Error adding discover series to Sonarr: {e}")

        app.logger.info(f"Found matching request: series_id={series_id}, request_id={request_id}")
        
        # Get TV show details from TMDB
        show_data = get_tmdb_endpoint(f"tv/{tmdb_id}")
        
        if not show_data:
            return render_template('error.html', message="Failed to get show details from TMDB")
        
        # Format show data for template
        formatted_show = {
            'id': tmdb_id,
            'name': show_data.get('name', 'Unknown Series'),
            'overview': show_data.get('overview', ''),
            'posterUrl': f"https://image.tmdb.org/t/p/w300{show_data['poster_path']}" if show_data.get('poster_path') else '/static/placeholder-banner.png',
            'seasons': []
        }
        
        # Only include selected seasons
        for season in show_data.get('seasons', []):
            season_num = season.get('season_number', 0)
            if season_num in selected_seasons:
                formatted_show['seasons'].append({
                    'seasonNumber': season_num,
                    'episodeCount': season.get('episode_count', '?')
                })
        
        app.logger.info(f"Rendering episode selection with selected_seasons: {selected_seasons}")
        
        selected_rule = request.args.get('rule', None)
        return render_template('episode_selection.html',
        show=formatted_show,
        request_id=request_id,
        series_id=series_id,
        selected_seasons=selected_seasons,
        selected_rule=selected_rule)
    
    except Exception as e:
        app.logger.error(f"Error in select_episodes: {str(e)}", exc_info=True)
        return render_template('error.html', message=f"Error loading episode selection: {str(e)}")

@app.route('/api/process-episode-selection', methods=['POST'])
def process_episode_selection():
    """Process episode selection with multi-season support."""
    try:
        # Get form data
        request_id = request.form.get('request_id')
        episodes = request.form.getlist('episodes')  # Gets ALL values with name 'episodes'
        action = request.form.get('action')

        app.logger.info(f"Processing: request_id={request_id}, action={action}, episodes={episodes}")
        
        if action == 'cancel':
            if request_id:
                delete_pending_request(request_id)
                app.logger.info(f"Cancelled and removed request {request_id}")

            return redirect(url_for('rules_page'))
        
        elif action == 'process':
            # Load request data
            request_data = get_pending_request(request_id)
            if not request_data:
                return redirect(url_for('rules_page'))
            
            series_id = request_data['series_id']
            
            if not episodes:
                return redirect(url_for('rules_page'))
            
            app.logger.debug(f"Processing {len(episodes)} episodes: {episodes}")
            
            # Parse episodes by season: "season:episode" format
            episodes_by_season = {}
            for episode_str in episodes:
                try:
                    if ':' not in episode_str:
                        app.logger.error(f"Invalid episode format (no colon): {episode_str}")
                        continue
                        
                    season_str, episode_str = episode_str.split(':', 1)
                    season_num = int(season_str)
                    episode_num = int(episode_str)
                    
                    if season_num not in episodes_by_season:
                        episodes_by_season[season_num] = []
                    episodes_by_season[season_num].append(episode_num)
                    
                except ValueError as e:
                    app.logger.warning(f"Invalid episode format: {episode_str} - {str(e)}")
                    continue
            
            if not episodes_by_season:
                return redirect(url_for('rules_page'))
            
            app.logger.info(f"Processing multi-season selection for series {series_id}: {episodes_by_season}")
            
            # Store in pending_selections for processing
            episeerr_utils.pending_selections[str(series_id)] = {
                'title': request_data.get('title', 'Unknown'),
                'episodes_by_season': episodes_by_season,
                'selected_episodes': set(),
                'multi_season': True
            }
            
            # Process each season separately
            total_processed = 0
            failed_seasons = []
            
            for season_number, episode_numbers in episodes_by_season.items():
                app.logger.info(f"Processing Season {season_number}: {episode_numbers}")
                
                success = episeerr_utils.process_episode_selection_with_season(
                    series_id, season_number, episode_numbers
                )
                
                if success:
                    total_processed += len(episode_numbers)
                    app.logger.info(f"✓ Successfully processed Season {season_number}")
                else:
                    failed_seasons.append(season_number)
                    app.logger.error(f"✗ Failed to process Season {season_number}")
            
            # ── NEW: Assign series to rule after episode processing ──
            selected_rule = request.form.get('selected_rule')
            if not selected_rule:
                config = load_config()
                selected_rule = config.get('default_rule', 'default')
            
            if selected_rule:
                config = load_config()
                if selected_rule in config.get('rules', {}):
                    series_id_str = str(series_id)
                    target = config['rules'][selected_rule]
                    target.setdefault('series', {})
                    
                    if series_id_str not in target['series']:
                        target['series'][series_id_str] = {'activity_date': None}
                        save_config(config)
                        app.logger.info(f"✓ Assigned series {series_id} to rule '{selected_rule}'")
                    
                    try:
                        episeerr_utils.sync_rule_tag_to_sonarr(series_id, selected_rule)
                    except Exception as e:
                        app.logger.error(f"Rule tag sync failed: {e}")
                else:
                    app.logger.warning(f"Selected rule '{selected_rule}' not found in config")
            # ── END NEW ──────────────────────────────────────────────
            
            # Clean up request
            delete_pending_request(request_id)
            app.logger.info(f"Removed pending request: {request_id}")

            _plex_watchlist_add_silent(
                request_data.get('tmdb_id', ''), 'tv', request_data.get('title', ''))
            return redirect(url_for('rules_page'))

        else:
            return redirect(url_for('rules_page'))

    except Exception as e:
        app.logger.error(f"Error processing episode selection: {str(e)}", exc_info=True)
        return redirect(url_for('rules_page'))

@app.route('/api/tmdb/season/<tmdb_id>/<season_number>')
def get_tmdb_season(tmdb_id, season_number):
    """Get season details including episodes from TMDB API."""
    try:
        if not TMDB_API_KEY:
            return jsonify({"error": "TMDB API key not configured"}), 500
        
        season_data = get_tmdb_endpoint(f"tv/{tmdb_id}/season/{season_number}")
        
        if not season_data:
            return jsonify({"error": "Failed to get season data from TMDB"}), 500
        
        return jsonify(season_data)
        
    except Exception as e:
        app.logger.error(f"Error getting TMDB season data: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/pending-requests')
def get_pending_requests():
    """Get pending requests."""
    if not TMDB_API_KEY:
        return jsonify({"success": False, "requests": [], "count": 0})
    try:
        rows = get_all_pending_requests()
        pending_requests = sorted(rows, key=lambda x: x.get('created_at', 0), reverse=True)
        return jsonify({
            "success": True,
            "requests": pending_requests,
            "count": len(pending_requests)
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/delete-request/<request_id>', methods=['POST'])
def delete_request(request_id):
    """Delete a pending request by its unique request_id (filename base)."""
    try:
        deleted = delete_pending_request(request_id)
        if deleted:
            app.logger.info(f"Deleted pending request {request_id}")
            return jsonify({"status": "success", "message": "Request deleted successfully"}), 200
        else:
            app.logger.warning(f"Pending request {request_id} not found")
            return jsonify({"status": "error", "message": "Request not found"}), 404

    except Exception as e:
        app.logger.error(f"Error deleting request {request_id}: {str(e)}")
        return jsonify({"status": "error", "message": "Failed to delete request"}), 500


# ============================================================================
# WEBHOOK ROUTES — moved to webhooks.py (sonarr_webhooks_bp)
# ============================================================================

# ============================================================================
# DEBUG & TEST ROUTES (Simplified)
# ============================================================================

@app.route('/api/test-cleanup/<int:series_id>')
def test_cleanup(series_id):
    """Test cleanup for a specific series."""
    try:
        config = load_config()
        rule = None
        rule_name = None
        for r_name, r_details in config['rules'].items():
            if str(series_id) in r_details.get('series', {}):
                rule = r_details
                rule_name = r_name
                break
        
        if not rule:
            return jsonify({"status": "error", "message": "Series not assigned to any rule"}), 404
        
        test_rule = rule.copy()
        test_rule['dry_run'] = True
        
        from media_processor import check_time_based_cleanup
        
        should_cleanup, reason = check_time_based_cleanup(series_id, test_rule)
        
        if should_cleanup:
            return jsonify({
                "status": "success",
                "message": f"Test completed: {reason}",
                "would_cleanup": True,
                "rule": rule_name,
                "reason": reason
            })
        else:
            return jsonify({
                "status": "success", 
                "message": f"No cleanup needed: {reason}",
                "would_cleanup": False,
                "rule": rule_name,
                "reason": reason
            })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# ============================================================================
# ERROR HANDLERS
# ============================================================================

@app.errorhandler(404)
def not_found(error):
    """Handle 404 errors."""
    return render_template('error.html', message="Page not found"), 404

@app.errorhandler(500)
def internal_error(error):
    """Handle 500 errors."""
    return render_template('error.html', message="Internal server error"), 500


@app.route('/api/migrate-tags', methods=['POST'])
def migrate_tags_endpoint():
    """API endpoint to manually create tags for existing rules"""
    try:
        created, failed = migrate_create_rule_tags()
        
        return jsonify({
            "status": "success",
            "message": f"Migration complete: {created} created, {failed} failed",
            "created": created,
            "failed": failed
        })
    except Exception as e:
        app.logger.error(f"Error in manual tag migration: {str(e)}")
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500
    
@app.route('/api/sync-all-tags', methods=['POST'])
def sync_all_tags_endpoint():
    """API endpoint to manually bulk sync tags for all existing series"""
    try:
        synced, failed, not_found = sync_all_series_tags()
        
        return jsonify({
            "status": "success",
            "message": f"Bulk sync complete: {synced} synced, {failed} failed, {not_found} not in Sonarr",
            "synced": synced,
            "failed": failed,
            "not_found": not_found
        })
    except Exception as e:
        app.logger.error(f"Error in bulk tag sync: {str(e)}")
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

    
# Add this API route to episeerr.py (near other @app.route definitions)

@app.route('/api/recent-activity-cards')
def get_recent_activity_cards():
    """Get data for the three activity cards"""
    try:
        from activity_storage import get_last_request, get_last_search, get_last_watch
        
        return jsonify({
            'last_request': format_request_card(get_last_request()),
            'last_search': format_search_card(get_last_search()),
            'last_watch': format_watch_card(get_last_watch())
        })
        
    except Exception as e:
        app.logger.error(f"Error getting activity cards: {str(e)}")
        return jsonify({'error': str(e)}), 500

# Add these helper functions (put them near other helper functions in episeerr.py)

def format_request_card(data):
    """Format Overseerr request for card display"""
    if not data:
        return None
    
    return {
        'title': data.get('title', 'Unknown'),
        'timestamp': data.get('timestamp'),
        'tmdb_id': data.get('tmdb_id'),
        'display': f"Requested {time_ago(data.get('timestamp'))}"
    }

def format_search_card(data):
    """Format Sonarr search for card display"""
    if not data:
        return None
    
    return {
        'title': data.get('series_title', 'Unknown'),
        'episode': f"S{data.get('season', 0):02d}E{data.get('episode', 0):02d}",
        'timestamp': data.get('timestamp'),
        'series_id': data.get('series_id'),
        'display': f"Searched {time_ago(data.get('timestamp'))}"
    }

def format_watch_card(data):
    """Format watch event for card display"""
    if not data:
        return None
    
    user = data.get('user', 'System')
    user_display = f" by {user}" if user != "System" else ""
    
    return {
        'title': data.get('series_title', 'Unknown'),
        'episode': f"S{data.get('season', 0):02d}E{data.get('episode', 0):02d}",
        'timestamp': data.get('timestamp'),
        'series_id': data.get('series_id'),
        'display': f"Watched{user_display} {time_ago(data.get('timestamp'))}"
    }

def time_ago(timestamp):
    """Convert timestamp to human-readable time ago"""
    if not timestamp:
        return "recently"
    
    try:
        delta = int(time.time()) - timestamp
        
        if delta < 60:
            return "just now"
        elif delta < 3600:
            minutes = delta // 60
            return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
        elif delta < 86400:
            hours = delta // 3600
            return f"{hours} hour{'s' if hours != 1 else ''} ago"
        else:
            days = delta // 86400
            return f"{days} day{'s' if days != 1 else ''} ago"
    except:
        return "recently"
    

    
def migrate_create_rule_tags():
    """One-time migration: Create tags for all existing rules in Sonarr"""
    try:
        config = load_config()
        created = []
        failed = []
        
        app.logger.info("=== Starting rule tag migration ===")
        
        for rule_name in config['rules'].keys():
            try:
                tag_id = episeerr_utils.get_or_create_rule_tag_id(rule_name)
                if tag_id:
                    created.append(f"episeerr_{rule_name}")
                    app.logger.info(f"✓ Created/verified tag: episeerr_{rule_name} (ID: {tag_id})")
                else:
                    failed.append(rule_name)
                    app.logger.warning(f"✗ Failed to create tag for rule: {rule_name}")
            except requests.exceptions.ConnectionError:
                failed.append(rule_name)
                app.logger.warning(f"✗ Sonarr not reachable - skipping tag for rule: {rule_name}")
            except Exception as e:
                failed.append(rule_name)
                app.logger.error(f"✗ Error creating tag for {rule_name}: {str(e)}")
        
        app.logger.info(f"=== Migration complete ===")
        app.logger.info(f"✓ Created/verified: {len(created)} tags")
        if failed:
            app.logger.warning(f"✗ Failed: {len(failed)} tags - {failed}")
        
        return len(created), len(failed)
        
    except Exception as e:
        app.logger.error(f"Migration failed: {str(e)}")
        return 0, 0
    
def sync_all_series_tags():
    """Bulk sync: Apply tags to all series currently in config"""
    try:
        config = load_config()
        synced = 0
        failed = 0
        not_found = 0
        
        app.logger.info("=== Starting bulk tag sync for all series ===")
        
        for rule_name, rule_details in config['rules'].items():
            series_dict = rule_details.get('series', {})
            app.logger.info(f"Processing rule '{rule_name}' with {len(series_dict)} series")
            
            for series_id in series_dict.keys():
                try:
                    # Check if series exists in Sonarr first
                    series_data = episeerr_utils.get_series_from_sonarr(int(series_id))
                    if not series_data:
                        not_found += 1
                        app.logger.warning(f"✗ Series {series_id} not found in Sonarr (may have been deleted)")
                        continue
                    
                    success = episeerr_utils.sync_rule_tag_to_sonarr(int(series_id), rule_name)
                    if success:
                        synced += 1
                        app.logger.debug(f"✓ Synced series {series_id} ({series_data['title']}) to '{rule_name}'")
                    else:
                        failed += 1
                        app.logger.warning(f"✗ Failed to sync series {series_id}")
                except Exception as e:
                    failed += 1
                    app.logger.error(f"✗ Error syncing series {series_id}: {str(e)}")
        
        app.logger.info(f"=== Bulk sync complete: {synced} synced, {failed} failed, {not_found} not found ===")
        return synced, failed, not_found
        
    except Exception as e:
        app.logger.error(f"Bulk sync failed: {str(e)}")
        return 0, 0, 0  

# ============================================================================
# FINAL STARTUP (run AFTER everything is defined)
# ============================================================================

def initialize_episeerr():
    """Initialize episeerr components."""
    app.logger.debug("Entering initialize_episeerr()")

    # Migrate any pending request JSON files into SQLite (one-time, idempotent)
    try:
        migrated = migrate_pending_requests_from_files(REQUESTS_DIR)
        if migrated:
            app.logger.info(f"✓ Migrated {migrated} pending request(s) from files to DB")
    except Exception as e:
        app.logger.error(f"Error migrating pending requests: {e}")

    # Existing code
    try:
        episeerr_utils.check_and_cancel_unmonitored_downloads()
    except Exception as e:
        app.logger.error(f"Error in initial download check: {str(e)}")
    
    # NEW: Comprehensive tag reconciliation (create, migrate, drift, orphaned)
    try:
        app.logger.info("🏷️  Starting comprehensive tag reconciliation...")
        
        config = load_config()
        
        # Step 1: Create/verify all rule tags exist in Sonarr
        created, failed = migrate_create_rule_tags()
        if created > 0 or failed > 0:
            app.logger.info(f"  Tag creation: {created} verified, {failed} failed")
        
        # Step 2: One-time bulk sync (migrate existing series to have tags)
        if not config.get('tag_migration_complete', False):
            app.logger.info("  First-time migration - syncing all series tags...")
            synced, failed, not_found = sync_all_series_tags()
            app.logger.info(f"  Series tag sync: {synced} synced, {failed} failed, {not_found} not found")
            
            # Mark migration as complete
            config['tag_migration_complete'] = True
            save_config(config)
            app.logger.info("  ✓ Tag migration marked as complete")
        
        # Step 3: Drift detection + orphaned recovery for all series
        all_series_ids = [
            int(sid)
            for rule_details in config['rules'].values()
            for sid in list(rule_details.get('series', {}).keys())
        ]
        # Also check Sonarr series not in config (orphaned tag recovery)
        all_sonarr_series = get_sonarr_series()
        config_series_ids = {
            sid
            for rule_details in config['rules'].values()
            for sid in rule_details.get('series', {}).keys()
        }
        orphaned_ids = [
            s['id'] for s in all_sonarr_series
            if str(s['id']) not in config_series_ids
        ]

        modified = False
        reconciled = 0
        for series_id in all_series_ids + orphaned_ids:
            try:
                _, changed = episeerr_utils.reconcile_series_drift(series_id, config)
                if changed:
                    modified = True
                    reconciled += 1
            except Exception as e:
                app.logger.debug(f"Error reconciling series {series_id}: {e}")

        if modified:
            save_config(config)

        app.logger.info(f"✓ Tag reconciliation complete: {reconciled} corrections made")
            
    except requests.exceptions.ConnectionError:
        app.logger.warning("Sonarr not ready - tags will be created when Sonarr becomes available")
    except Exception as e:
        app.logger.error(f"Error during tag reconciliation: {str(e)}")

    # NEW: Ensure delay profile has control tags ONLY (default, select, delay)
    try:
        updated = episeerr_utils.update_delay_profile_with_control_tags()
        if updated:
            app.logger.info("✓ Delay profile updated with control tags (default, select, delay)")
        else:
            app.logger.warning("Delay profile update skipped or failed (check logs)")
    except requests.exceptions.ConnectionError:
        app.logger.warning("Sonarr not ready yet - will retry delay profile sync later")
    except Exception as e:
        app.logger.error(f"Error updating delay profile with control tags: {str(e)}")

    # NEW: Create Radarr tags for movie rules
    try:
        from movie_processor import ensure_movie_rule_tags
        config = load_config()
        movie_rules = config.get('movie_rules', {})
        if movie_rules:
            tag_ids = ensure_movie_rule_tags(movie_rules)
            app.logger.info(f"✓ Movie rule tags ensured in Radarr: {len(tag_ids)} tags")
    except requests.exceptions.ConnectionError:
        app.logger.warning("Radarr not ready — movie rule tags will be created when available")
    except Exception as e:
        app.logger.warning(f"Could not ensure movie rule tags: {e}")
    
    

# Run initialization (after function is defined!)
initialize_episeerr()

# Create scheduler instance
cleanup_scheduler = OCDarrScheduler()
app.logger.info("✓ OCDarrScheduler instantiated successfully")
cleanup_scheduler.start_scheduler()

# Initialize notification config 
notification_config = get_notification_config()
NOTIFICATIONS_ENABLED = notification_config['NOTIFICATIONS_ENABLED']
DISCORD_WEBHOOK_URL = notification_config['DISCORD_WEBHOOK_URL']
EPISEERR_URL = notification_config['EPISEERR_URL']

# NEW: Initialize notifications module
import notifications
notifications.init_notifications(NOTIFICATIONS_ENABLED, DISCORD_WEBHOOK_URL, EPISEERR_URL, SONARR_URL)

if __name__ == '__main__':
    cleanup_config_rules()
    app.logger.info("🚀 Enhanced Episeerr starting")
    app.run(host='0.0.0.0', port=5002, debug=os.getenv('FLASK_DEBUG', 'false').lower() == 'true')
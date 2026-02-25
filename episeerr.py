__version__ = "3.5.5"
from flask import Flask, render_template, request, redirect, url_for, jsonify
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
from episeerr_utils import EPISEERR_DEFAULT_TAG_ID, EPISEERR_SELECT_TAG_ID, normalize_url
import pending_deletions
from dashboard import dashboard_bp
import media_processor
from settings_db import (
    save_service, get_service, delete_service,
    update_service_test_result, get_all_services,
    set_setting, get_setting
)
from logging_config import main_logger as logger
# Import plugin system
from integrations import get_integration, get_all_integrations
from integrations import register_integration_blueprints
app = Flask(__name__)

register_integration_blueprints(app)
app.register_blueprint(dashboard_bp)



# Load environment variables
load_dotenv()
BASE_DIR = os.getcwd()

# Initialize settings database
from settings_db import get_sonarr_config, init_settings_db
init_settings_db()

# Sonarr variables (with DB support)
sonarr_config = get_sonarr_config()
SONARR_URL = normalize_url(sonarr_config.get('url')) if sonarr_config else None
SONARR_API_KEY = sonarr_config.get('api_key') if sonarr_config else None

# Jellyseerr/Overseerr variables (keep as-is for now)
JELLYSEERR_URL = normalize_url(os.getenv('JELLYSEERR_URL', ''))
JELLYSEERR_API_KEY = os.getenv('JELLYSEERR_API_KEY')
OVERSEERR_URL = normalize_url(os.getenv('OVERSEERR_URL'))
OVERSEERR_API_KEY = os.getenv('OVERSEERR_API_KEY')
SEERR_ENABLED = bool((JELLYSEERR_URL and JELLYSEERR_API_KEY) or (OVERSEERR_URL and OVERSEERR_API_KEY))

# TMDB API Key - check database first
tmdb_service = get_service('tmdb', 'default')
TMDB_API_KEY = tmdb_service['api_key'] if tmdb_service else os.getenv('TMDB_API_KEY')
app.config['TMDB_API_KEY'] = TMDB_API_KEY
if app.config['TMDB_API_KEY']:
    app.logger.info("TMDB_API_KEY is set - request system will function normally")
else:
    app.logger.warning("TMDB_API_KEY is missing - you may encounter issues fetching series details and seasons")

# Request storage
REQUESTS_DIR = os.path.join(os.getcwd(), 'data', 'requests')
os.makedirs(REQUESTS_DIR, exist_ok=True)

LAST_PROCESSED_FILE = os.path.join(os.getcwd(), 'data', 'last_processed.json')
os.makedirs(os.path.dirname(LAST_PROCESSED_FILE), exist_ok=True)

LAST_PROCESSED_JELLYFIN_EPISODES = {}
LAST_PROCESSED_LOCK = Lock()


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

def auto_add_quick_link(name, url, icon, open_in_iframe=False):
    """Automatically add or update a service quick link"""
    from settings_db import get_all_quick_links, add_quick_link, delete_quick_link
    
    # Check if link already exists (by URL)
    existing_links = get_all_quick_links()
    
    # Normalize URLs for comparison
    normalized_url = url.rstrip('/').lower()
    
    for link in existing_links:
        link_url = link['url'].rstrip('/').lower()
        
        if link_url == normalized_url:
            app.logger.debug(f"Quick link for {name} already exists (ID: {link['id']})")
            
            # UPDATE: Check if open_in_iframe setting changed
            if link.get('open_in_iframe') != open_in_iframe:
                # Need to update the quick link with new iframe setting
                delete_quick_link(link['id'])
                new_id = add_quick_link(name, url, icon, open_in_iframe)
                app.logger.info(f"Updated quick link {name} - iframe: {open_in_iframe} (ID: {new_id})")
            
            return  # Already exists (and updated if needed)
    
    # Add new link
    add_quick_link(name, url, icon, open_in_iframe)
    app.logger.info(f"Auto-added {name} to quick links - iframe: {open_in_iframe}")

@app.before_request
def check_first_run():
    # Skip check for setup page itself, static files, and API routes
    if request.endpoint in ['setup', 'static', 'test_connection', 'save_service_config', 
                            'manage_quick_links', 'delete_quick_link_route', 'handle_emby_webhook',
                            'iframe_view', 'services_sidebar', 'iframe_service_view']:  # ADD THESE TWO
        return
    
    # Check if Sonarr is configured (required service)
    from settings_db import get_sonarr_config
    sonarr = get_sonarr_config()
    
    if not sonarr or not sonarr.get('url'):
        # Not configured - redirect to setup
        return redirect(url_for('setup'))

@app.route('/setup')
def setup():
    """Service setup page"""
    # Get all service configurations (existing)
    sonarr = get_service('sonarr', 'default')
    jellyfin = get_service('jellyfin', 'default')
    emby = get_service('emby', 'default')
    tautulli = get_service('tautulli', 'default')
    jellyseerr = get_service('jellyseerr', 'default')
    tmdb = get_service('tmdb', 'default')
    
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
            'url': saved_values.get('url'),
            'apikey': saved_values.get('apikey'),
            'integration': integration,
            'setup_fields': setup_fields,
            'saved_values': saved_values  # for template pre-fill
        }
    
    # Check if setup is complete
    setup_complete = (sonarr and (jellyfin or emby or tautulli))
    
    # Quick links
    from settings_db import get_all_quick_links
    quick_links = get_all_quick_links()
    
    return render_template('setup.html',
        setup_complete=setup_complete,
        sonarr_connected=sonarr is not None,
        sonarr_url=sonarr['url'] if sonarr else None,
        sonarr_apikey=sonarr['api_key'] if sonarr else None,
        jellyfin_connected=jellyfin is not None,
        jellyfin_url=jellyfin['url'] if jellyfin else None,
        jellyfin_apikey=jellyfin['api_key'] if jellyfin else None,
        jellyfin_userid=jellyfin['config'].get('user_id') if jellyfin and jellyfin.get('config') else None,
        jellyfin_method=jellyfin['config'].get('method', 'polling') if jellyfin and jellyfin.get('config') else 'polling',
        jellyfin_trigger_min=jellyfin['config'].get('trigger_min', 50.0) if jellyfin and jellyfin.get('config') else 50.0,
        jellyfin_trigger_max=jellyfin['config'].get('trigger_max', 55.0) if jellyfin and jellyfin.get('config') else 55.0,
        jellyfin_poll_interval=jellyfin['config'].get('poll_interval', 900) if jellyfin and jellyfin.get('config') else 900,
        jellyfin_trigger_percent=jellyfin['config'].get('trigger_percentage', 50.0) if jellyfin and jellyfin.get('config') else 50.0,emby_connected=emby is not None,
        emby_url=emby['url'] if emby else None,
        emby_apikey=emby['api_key'] if emby else None,
        emby_userid=emby['config'].get('user_id') if emby and emby.get('config') else None,
        emby_poll_interval=emby['config'].get('poll_interval', 900) if emby and emby.get('config') else 900,emby_trigger_percent=emby['config'].get('trigger_percentage', 50.0) if emby and emby.get('config') else 50.0,
        tautulli_connected=tautulli is not None,
        tautulli_url=tautulli['url'] if tautulli else None,
        tautulli_apikey=tautulli['api_key'] if tautulli else None,
        jellyseerr_connected=jellyseerr is not None,
        jellyseerr_url=jellyseerr['url'] if jellyseerr else None,
        jellyseerr_apikey=jellyseerr['api_key'] if jellyseerr else None,
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
            
            if api_key:  # Only test if we have an API key
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
            
            response = requests.get(f"{url}/api/v3/system/status", 
                                  headers={'X-Api-Key': api_key}, timeout=10)
            response.raise_for_status()
            
            update_service_test_result('sonarr', 'default', 'success')
            return jsonify({'status': 'success', 'message': 'Connected to Sonarr successfully'})
        
        elif service == 'tautulli':
            url = data.get('tautulli-url')
            api_key = data.get('tautulli-apikey')
            
            if not url or not api_key:
                return jsonify({'status': 'error', 'message': 'URL and API key are required'}), 400
            
            response = requests.get(f"{url}/api/v2",
                                  params={'apikey': api_key, 'cmd': 'get_server_info'},
                                  timeout=10)
            response.raise_for_status()
            
            update_service_test_result('tautulli', 'default', 'success')
            return jsonify({'status': 'success', 'message': 'Connected to Tautulli successfully'})
        
        elif service == 'jellyfin':
            url = data.get('jellyfin-url')
            api_key = data.get('jellyfin-apikey')
            
            if not url or not api_key:
                return jsonify({'status': 'error', 'message': 'URL and API key are required'}), 400
            
            response = requests.get(f"{url}/System/Info",
                                  headers={'X-Emby-Token': api_key},
                                  timeout=10)
            response.raise_for_status()
            
            update_service_test_result('jellyfin', 'default', 'success')
            return jsonify({'status': 'success', 'message': 'Connected to Jellyfin successfully'})
        
        # Add other legacy services as needed...
        
        else:
            return jsonify({'status': 'error', 'message': f'Unknown service: {service}'}), 400
            
    except Exception as e:
        app.logger.error(f"Test connection error for {service}: {e}")
        update_service_test_result(service, 'default', 'failed')
        return jsonify({'status': 'error', 'message': f'Connection failed: {str(e)}'}), 400

# Replace save_service_config in episeerr.py with this:

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
                # No integration fields found, this might be a legacy service
                # Fall through to legacy handlers
                pass
            elif not api_key:
                # Integration data exists but no API key - this is an error
                return jsonify({
                    'status': 'error',
                    'message': 'API key/token/path is required'
                }), 400
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
                        # Get the open_in_iframe setting from normalized_data
                        open_in_iframe = normalized_data.get('open_in_iframe', False)
                        
                        auto_add_quick_link(
                            integration.display_name,
                            url,
                            integration.icon,
                            open_in_iframe  # Pass the iframe setting
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

            if not url or not apikey:
                return jsonify({'status': 'error', 'message': 'URL and API key required'}), 400
            
            save_service('sonarr', 'default', url, apikey)
            auto_add_quick_link('Sonarr', url, 
                              'https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/sonarr.png', open_in_iframe)
            
            return jsonify({
                'status': 'success',
                'message': 'Sonarr saved successfully'
            })
        
        elif service == 'jellyfin':
            url = data.get('jellyfin-url')
            apikey = data.get('jellyfin-apikey')
            open_in_iframe = data.get('jellyfin-open_in_iframe', False)
            if not url or not apikey:
                return jsonify({'status': 'error', 'message': 'URL and API key required'}), 400
            
            method = data.get('jellyfin-method', 'polling')  # Changed: read 'jellyfin-method' and default to 'polling'
            
            config = {
                'user_id': data.get('jellyfin-userid'),
                'method': method
            }
            
            # Only save relevant fields based on method
            if method == 'polling':
                config.update({
                    'poll_interval': int(data.get('jellyfin-poll-interval', 900)),
                    'trigger_percentage': float(data.get('jellyfin-trigger-percent', 50.0))
                })
            else:  # progress
                config.update({
                    'trigger_min': float(data.get('jellyfin-trigger-min', 50.0)),
                    'trigger_max': float(data.get('jellyfin-trigger-max', 55.0))
                })
            
            save_service('jellyfin', 'default', url, apikey, config)
            auto_add_quick_link('Jellyfin', url,
                              'https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/jellyfin.png', open_in_iframe)
            
            return jsonify({
                'status': 'success',
                'message': 'Jellyfin saved successfully'
            })
        
        elif service == 'emby':
            url = data.get('emby-url')
            apikey = data.get('emby-apikey')
            open_in_iframe = data.get('emby-open_in_iframe', False)

            if not url or not apikey:
                return jsonify({'status': 'error', 'message': 'URL and API key required'}), 400
            
            config = {
                'user_id': data.get('emby-userid'),
                'poll_interval': int(data.get('emby-poll-interval', 5)),
                'trigger_percentage': float(data.get('emby-trigger-percent', 50.0))
            }
            save_service('emby', 'default', url, apikey, config)
            auto_add_quick_link('Emby', url,
                              'https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/emby.png', open_in_iframe)
            
            return jsonify({
                'status': 'success',
                'message': 'Emby saved successfully'
            })
        
        elif service == 'tautulli':
            url = data.get('tautulli-url')
            apikey = data.get('tautulli-apikey')
            open_in_iframe = data.get('tautulli-open_in_iframe', False)

            if not url or not apikey:
                return jsonify({'status': 'error', 'message': 'URL and API key required'}), 400
            
            save_service('tautulli', 'default', url, apikey)
            auto_add_quick_link('Tautulli', url,
                              'https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/tautulli.png', open_in_iframe)
            
            return jsonify({
                'status': 'success',
                'message': 'Tautulli saved successfully'
            })
        
        elif service == 'jellyseerr':
            url = data.get('jellyseerr-url')
            apikey = data.get('jellyseerr-apikey')
            open_in_iframe = data.get('jellyseerr-open_in_iframe', False)

            if not url or not apikey:
                return jsonify({'status': 'error', 'message': 'URL and API key required'}), 400
            
            save_service('jellyseerr', 'default', url, apikey)
            auto_add_quick_link('Jellyseerr', url,
                              'https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/jellyseerr.png', open_in_iframe)
            
            return jsonify({
                'status': 'success',
                'message': 'Jellyseerr saved successfully'
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
        return jsonify({'status': 'success', 'links': links})
    
    else:  # POST
        data = request.json
        link_id = add_quick_link(
            data.get('name'),
            data.get('url'),
            data.get('icon', 'fas fa-link'),
            data.get('open_in_iframe', False)  # NEW: Accept iframe flag
        )
        return jsonify({'status': 'success', 'id': link_id})

# 2. ADD this NEW route after delete_quick_link_route (around line 586)

@app.route('/iframe/<int:service_id>')
def iframe_view(service_id):
    """Display a service in an iframe"""
    from settings_db import get_quick_link_by_id
    
    service = get_quick_link_by_id(service_id)
    
    if not service:
        return "Service not found", 404
    
    if not service.get('open_in_iframe'):
        # If not configured for iframe, redirect to external URL
        return redirect(service['url'])
    
    return render_template('iframe_view.html', service=service)


@app.route('/api/services-sidebar')
def services_sidebar():
    """Get all configured services for sidebar display with iframe settings
    Also auto-manages quick links for integrations"""
    from settings_db import get_service, get_all_quick_links, add_quick_link, delete_quick_link
    from integrations import get_all_integrations
    
    services = []
    existing_links = get_all_quick_links()
    integration_urls = set()
    
    # Get all integrations
    for integration in get_all_integrations():
        config = get_service(integration.service_name, 'default')
        if config:  # Only include if configured
            # Check if open_in_iframe is in config
            open_in_iframe = False
            if config.get('config'):
                open_in_iframe = config['config'].get('open_in_iframe', False)
            
            service_url = config['url'].rstrip('/')
            integration_urls.add(service_url.lower())
            
            # Check if a quick link already exists for this service
            existing_link = None
            for link in existing_links:
                if link['url'].rstrip('/').lower() == service_url.lower():
                    existing_link = link
                    break
            
            if existing_link:
                # Use the existing quick link ID
                services.append({
                    'id': existing_link['id'],
                    'name': integration.display_name,
                    'url': config['url'],
                    'icon': integration.icon,
                    'service_type': integration.service_name,
                    'open_in_iframe': open_in_iframe
                })
            else:
                # Auto-create a quick link for this integration
                link_id = add_quick_link(
                    name=integration.display_name,
                    url=config['url'],
                    icon=integration.icon,
                    open_in_iframe=open_in_iframe
                )
                
                services.append({
                    'id': link_id,
                    'name': integration.display_name,
                    'url': config['url'],
                    'icon': integration.icon,
                    'service_type': integration.service_name,
                    'open_in_iframe': open_in_iframe
                })
                
                app.logger.info(f"Auto-created quick link for {integration.display_name}")
    
    # Clean up orphaned quick links that match integration URLs but service is no longer configured
    for link in existing_links:
        link_url = link['url'].rstrip('/').lower()
        # If this link's URL matches an integration URL but wasn't processed above, delete it
        # (This happens when you unconfigure a service)
        if link_url in integration_urls:
            # Check if this link was included in services
            found = False
            for service in services:
                if service.get('id') == link['id']:
                    found = True
                    break
            
            if not found:
                # Orphaned integration link - remove it
                delete_quick_link(link['id'])
                app.logger.info(f"Removed orphaned quick link: {link['name']}")
    
    return jsonify({'status': 'success', 'services': services})

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
    
    if not open_in_iframe:
        # If not configured for iframe, redirect to external URL
        return redirect(service['url'])
    
    return render_template('iframe_view.html', service={
        'name': service_type.replace('_', ' ').title(),
        'url': service['url']
    })

@app.route('/api/quick-links/<int:link_id>', methods=['DELETE'])
def delete_quick_link_route(link_id):
    """Delete a quick link"""
    from settings_db import delete_quick_link
    delete_quick_link(link_id)
    return jsonify({'status': 'success'})

# ============================================================
# EMBY WEBHOOK HANDLER
# ============================================================
# Emby field mapping (vs Jellyfin):
#   Event                         → "playback.start / pause / stop"
#   Item.Type                     → "Episode"
#   Item.SeriesName               → series name
#   Item.ParentIndexNumber        → season number
#   Item.IndexNumber              → episode number
#   Item.RunTimeTicks             → total runtime
#   PlaybackInfo.PositionTicks    → current position
#   Session.Id                    → session ID (NOT PlaySessionId!)
#   User.Name                     → username
#   Item.ProviderIds.sonarr       → Sonarr series ID (bonus - skips lookup)
#
# Emby setup:
#   User Preferences → Notifications → Add Notification → Webhooks
#   URL: http://<episeerr-host>:5002/emby-webhook
#   Events: playback.start, playback.stop
# ============================================================

@app.route('/emby-webhook', methods=['POST'])
def handle_emby_webhook():
    """Handle Emby webhooks - maps Emby event structure to Episeerr's watched logic."""
    app.logger.info("Received webhook from Emby")
    data = request.json
    if not data:
        return jsonify({'status': 'error', 'message': 'No data received'}), 400

    try:
        # ── Extract Emby fields ──
        event = data.get('Event', '')
        item = data.get('Item', {})
        user = data.get('User', {})
        session = data.get('Session', {})
        playback_info = data.get('PlaybackInfo', {})

        item_type = item.get('Type')
        series_name = item.get('SeriesName')
        season_number = item.get('ParentIndexNumber')
        episode_number = item.get('IndexNumber')
        runtime_ticks = item.get('RunTimeTicks', 0)
        position_ticks = playback_info.get('PositionTicks', 0)
        session_id = session.get('Id', '')  # Use Session.Id - Emby API doesn't populate PlayState.PlaySessionId
        user_name = user.get('Name', 'Unknown')

        # Bonus: Emby includes Sonarr ID directly — skips name-based lookup
        provider_ids = item.get('ProviderIds', {})
        sonarr_id = provider_ids.get('sonarr')
        tvdb_id = provider_ids.get('Tvdb')

        app.logger.info(f"Emby event: {event} | Type: {item_type} | User: {user_name} | Session: {session_id}")

        # Only process Episodes
        if item_type != 'Episode':
            app.logger.debug(f"Emby: skipping non-episode type '{item_type}'")
            return jsonify({'status': 'success', 'message': 'Not an episode'}), 200

        if not all([series_name, season_number is not None, episode_number is not None]):
            app.logger.warning(f"Emby: missing fields - series={series_name}, season={season_number}, ep={episode_number}")
            return jsonify({'status': 'error', 'message': 'Missing episode fields'}), 400

        app.logger.info(f"📺 Emby: {series_name} S{season_number}E{episode_number} [{event}] (User: {user_name}, sonarr_id: {sonarr_id})")

        # ============================================================
        # playback.start — user check + start polling session
        # ============================================================
        if event == 'playback.start':
            try:
                from media_processor import start_emby_polling, check_emby_user

                if not check_emby_user(user_name):
                    app.logger.info(f"Emby: User '{user_name}' not configured, skipping")
                    return jsonify({'status': 'success', 'message': 'User not configured'}), 200

                app.logger.info(f"▶️  Emby started: {series_name} S{season_number}E{episode_number}")

                episode_info = {
                    'user_name': user_name,
                    'series_name': series_name,
                    'season_number': int(season_number),
                    'episode_number': int(episode_number),
                    'progress_percent': 0.0,
                    'is_paused': False,
                    'sonarr_id': sonarr_id,
                    'tvdb_id': tvdb_id
                }

                polling_started = start_emby_polling(session_id, episode_info)
                if polling_started:
                    app.logger.info(f"✅ Emby: Started polling for {series_name} S{season_number}E{episode_number}")

            except Exception as e:
                app.logger.error(f"Emby: Error on playback.start: {e}")

            return jsonify({'status': 'success', 'message': 'Playback started'}), 200

        # ============================================================
        # playback.pause — ignore (polling handles progress tracking)
        # ============================================================
        elif event == 'playback.pause':
            app.logger.debug(f"⏸️  Emby paused: {series_name} S{season_number}E{episode_number}")
            return jsonify({'status': 'success', 'message': 'Pause acknowledged'}), 200

        # ============================================================
        # playback.stop — final progress check + process if threshold met
        # ============================================================
        elif event == 'playback.stop':
            progress_percent = (position_ticks / runtime_ticks * 100) if runtime_ticks > 0 else 0
            app.logger.info(f"⏹️  Emby stopped: {series_name} S{season_number}E{episode_number} — {progress_percent:.1f}% watched")

            # Stop polling for this session
            try:
                from media_processor import stop_emby_polling
                stopped = stop_emby_polling(session_id)
                if stopped:
                    app.logger.info(f"🛑 Emby: Stopped polling for session {session_id}")
            except Exception as e:
                app.logger.debug(f"Emby: No polling to stop: {e}")

            try:
                from media_processor import (
                    get_episode_tracking_key,
                    processed_jellyfin_episodes,
                    check_emby_user,
                    EMBY_TRIGGER_PERCENTAGE
                )

                if not check_emby_user(user_name):
                    app.logger.info(f"Emby: User '{user_name}' not configured, skipping")
                    return jsonify({'status': 'success', 'message': 'User not configured'}), 200

                # Already processed via polling? Skip.
                tracking_key = get_episode_tracking_key(series_name, season_number, episode_number, user_name)
                if tracking_key in processed_jellyfin_episodes:
                    app.logger.info(f"✅ Emby: Already processed via polling — skipping")
                    processed_jellyfin_episodes.discard(tracking_key)
                    return jsonify({'status': 'success', 'message': 'Already processed'}), 200

                # Check against EMBY_TRIGGER_PERCENTAGE
                if progress_percent >= EMBY_TRIGGER_PERCENTAGE:
                    app.logger.info(f"🎯 Emby: Processing at {progress_percent:.1f}% (threshold: {EMBY_TRIGGER_PERCENTAGE}%)")

                    # ── Tag sync & drift correction ──
                    from media_processor import get_series_id, move_series_in_config
                    from episeerr_utils import validate_series_tag, sync_rule_tag_to_sonarr

                    # Use Sonarr ID from Emby ProviderIds if available, else look up by name
                    series_id = int(sonarr_id) if sonarr_id else get_series_id(series_name, tvdb_id)

                    if series_id:
                        config = load_config()
                        config_rule = None
                        series_id_str = str(series_id)

                        for rule_name, rule_details in config['rules'].items():
                            if series_id_str in rule_details.get('series', {}):
                                config_rule = rule_name
                                break

                        if config_rule:
                            matches, actual_tag_rule = validate_series_tag(series_id, config_rule)
                            if not matches:
                                if actual_tag_rule:
                                    app.logger.warning(f"EMBY DRIFT - config: {config_rule} → tag: {actual_tag_rule}")
                                    move_series_in_config(series_id, config_rule, actual_tag_rule)
                                else:
                                    app.logger.warning(f"Emby: No episeerr tag on {series_id} → restoring episeerr_{config_rule}")
                                    sync_rule_tag_to_sonarr(series_id, config_rule)

                    # ── Write temp file and run media_processor ──
                    temp_dir = os.path.join(os.getcwd(), 'temp')
                    os.makedirs(temp_dir, exist_ok=True)

                    plex_data = {
                        "server_title": series_name,
                        "server_season_num": int(season_number),
                        "server_ep_num": int(episode_number),
                        "thetvdb_id": tvdb_id,
                        "themoviedb_id": None,
                        "sonarr_series_id": int(sonarr_id) if sonarr_id else series_id,
                        "source": "emby"
                    }

                    temp_file_path = os.path.join(temp_dir, 'data_from_server.json')
                    with open(temp_file_path, 'w') as f:
                        json.dump(plex_data, f)

                    result = subprocess.run(
                        ["python3", os.path.join(os.getcwd(), "media_processor.py")],
                        capture_output=True,
                        text=True
                    )

                    if result.returncode != 0:
                        app.logger.error(f"Emby: media_processor failed (rc={result.returncode}): {result.stderr}")
                        return jsonify({'status': 'error', 'message': 'Processor failed'}), 500
                    else:
                        app.logger.info(f"✅ Emby: Processed {series_name} S{season_number}E{episode_number}")
                else:
                    app.logger.info(f"⏭️  Emby: {progress_percent:.1f}% watched (need {EMBY_TRIGGER_PERCENTAGE}%) — skipping")

            except Exception as e:
                app.logger.error(f"Emby: Error on playback.stop: {e}")
                return jsonify({'status': 'error', 'message': str(e)}), 500

            return jsonify({'status': 'success'}), 200

        else:
            app.logger.debug(f"Emby: Unhandled event '{event}'")
            return jsonify({'status': 'success', 'message': f'Unhandled event: {event}'}), 200

    except Exception as e:
        app.logger.error(f"Emby webhook error: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

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
        
        response = requests.get(base_url, params=params, headers=headers)
        
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
            response = requests.get(f"{sonarr_url}/api/v3/queue", headers=headers, timeout=5)
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
            response = requests.get(f"{sonarr_url}/api/v3/wanted/missing", headers=headers, timeout=5)
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
            
            response = requests.get(
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
    all_series = get_sonarr_series()
    
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
        response = requests.get(f'{sonarr_url}/api/v3/series', headers=headers)
        
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
            series_count = len(rule_details.get('series', []))
            
            # Create display name (title case with spaces)
            display_name = rule_name.replace('_', ' ').title()
            
            rules_list.append({
                'name': rule_name,
                'display_name': display_name,
                'description': rule_details.get('description', ''),
                'series_count': series_count,
                'is_default': is_default
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

@app.route('/pending-deletions')
def view_pending_deletions():
    """View all pending deletions"""
    import pending_deletions
    summary = pending_deletions.get_pending_deletions_summary()
    return render_template('pending_deletions.html', summary=summary)


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
    summary = pending_deletions.get_pending_deletions_summary()
    return jsonify({
        'count': summary['total_episodes'],
        'series_count': summary['total_series'],
        'size_gb': summary['total_size_gb']
    })
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

def load_config():
    """Load configuration with simplified migration."""
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
        
        # Save the config
        with open(config_path, 'w') as file:
            json.dump(config, file, indent=4)
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
        response = requests.get(
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
        response = requests.get(f"{sonarr_url}/api/v3/series", headers=headers)
        if not response.ok:
            app.logger.error(f"Failed to fetch series from Sonarr: {response.status_code}")
            return []
        
        all_series = response.json()
        
        # Get all tags to find 'watched' tag ID
        tags_response = requests.get(f"{sonarr_url}/api/v3/tag", headers=headers)
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

@app.route('/series')  # or whatever you want to call it
def series_management():  # Changed from index to avoid confusion
    """Main series/rules management page."""
    config = load_config()
    all_series = get_sonarr_series()
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
    
    return render_template('rules_index.html', 
                         config=config,
                         all_series=all_series,
                         sonarr_stats=sonarr_stats,
                         SONARR_URL=sonarr_url,
                         SONARR_API_KEY=sonarr_preferences['SONARR_API_KEY'],
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
            'always_have': request.form.get('always_have', '').strip(),
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
            'always_have': request.form.get('always_have', '').strip()
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
            series_response = requests.get(f"{SONARR_URL}/api/v3/series", headers=headers)
            if series_response.ok:
                all_series = series_response.json()
                removed_from_count = 0
                
                for series in all_series:
                    tags = series.get('tags', [])
                    if tag_id in tags:
                        tags.remove(tag_id)
                        series['tags'] = tags
                        update_resp = requests.put(
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
                get_resp = requests.get(f"{SONARR_URL}/api/v3/delayprofile/{profile_id}", headers=headers)
                if get_resp.ok:
                    profile = get_resp.json()
                    current_tags = profile.get('tags', [])
                    if tag_id in current_tags:
                        current_tags.remove(tag_id)
                        profile['tags'] = current_tags
                        put_resp = requests.put(
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
            delete_resp = requests.delete(
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

@app.template_filter('sonarr_url')
def sonarr_series_url(series):
    """Generate Sonarr series URL from series object."""
    # Use titleSlug if available, otherwise fallback to ID
    if isinstance(series, dict):
        if 'titleSlug' in series:
            return f"{SONARR_URL}/series/{series['titleSlug']}"
        else:
            return f"{SONARR_URL}/series/{series.get('id', '')}"
    else:
        # If it's just an ID passed directly
        return f"{SONARR_URL}/series/{series}"

@app.route('/delete-rule/<rule_name>', methods=['POST'])



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
    
    return {'services': services}

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
    except Exception as e:
        app.logger.error(f"Error getting series stats: {str(e)}")
        return jsonify({'error': str(e)}), 500

def cleanup_config_rules():
    """Clean up config by removing non-existent series AND comprehensive tag reconciliation."""
    try:
        config = load_config()
        existing_series = get_sonarr_series()
        existing_series_ids = set(str(series['id']) for series in existing_series)
        changes_made = False
        
        # EXISTING: Remove series that don't exist in Sonarr
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
        
        
        
        # NEW: Comprehensive tag reconciliation (create, drift, orphaned)
        app.logger.info("Starting tag reconciliation during cleanup...")
        
        # Step 1: Create/verify all rule tags exist in Sonarr
        created, failed = migrate_create_rule_tags()
        if created > 0 or failed > 0:
            app.logger.info(f"  Tag creation: {created} verified, {failed} failed")
        
        # Step 2: Drift detection - fix mismatched tags
        drift_fixed = 0
        drift_synced = 0
        series_deleted = 0
        
        for rule_name, rule_details in config['rules'].items():
            for series_id_str in list(rule_details.get('series', {}).keys()):
                try:
                    series_id = int(series_id_str)
                    
                    # First check if series still exists
                    try:
                        series_data = episeerr_utils.get_series_from_sonarr(series_id)
                        if not series_data:
                            # Series deleted - remove from config
                            del rule_details['series'][series_id_str]
                            series_deleted += 1
                            app.logger.info(f"  Removed deleted series {series_id} from config (no longer in Sonarr)")
                            changes_made = True
                            continue
                    except Exception as fetch_error:
                        if "404" in str(fetch_error):
                            # Definite 404 - series deleted
                            del rule_details['series'][series_id_str]
                            series_deleted += 1
                            app.logger.info(f"  Removed series {series_id} from config (404 - deleted from Sonarr)")
                            changes_made = True
                            continue
                        else:
                            # Other error - log and continue
                            app.logger.error(f"  Error fetching series {series_id}: {str(fetch_error)}")
                            continue
                    
                    # Now check tag drift
                    matches, actual_tag_rule = episeerr_utils.validate_series_tag(series_id, rule_name)
                    
                    if not matches:
                        if actual_tag_rule:
                            # Find actual rule name (case-insensitive)
                            actual_rule_name = None
                            for rn in config['rules'].keys():
                                if rn.lower() == actual_tag_rule.lower():
                                    actual_rule_name = rn
                                    break
                            
                            if actual_rule_name:
                                # Move series to new rule
                                series_data = rule_details['series'][series_id_str]
                                del rule_details['series'][series_id_str]
                                
                                target_rule = config['rules'][actual_rule_name]
                                target_rule.setdefault('series', {})[series_id_str] = series_data
                                
                                drift_fixed += 1
                                app.logger.info(f"  Drift: Moved series {series_id} to '{actual_rule_name}'")
                                changes_made = True
                            else:
                                app.logger.error(f"Target rule '{actual_tag_rule}' not found")
                        else:
                            # No tag found: sync from config
                            episeerr_utils.sync_rule_tag_to_sonarr(series_id, rule_name)
                            drift_synced += 1
                
                except Exception as e:
                    app.logger.error(f"Error checking drift for series {series_id_str}: {str(e)}")
        
        # Step 3: Orphaned tags - find shows tagged in Sonarr but not in config
        # Build set of series IDs in config
        config_series_ids = set()
        for rule_details in config['rules'].values():
            config_series_ids.update(rule_details.get('series', {}).keys())
        
        orphaned = 0
        for series in existing_series:
            series_id = str(series['id'])
            
            # Skip if already in config
            if series_id in config_series_ids:
                continue
            
            # Check if has episeerr tag
            tag_mapping = episeerr_utils.get_tag_mapping()
            for tag_id in series.get('tags', []):
                tag_name = tag_mapping.get(tag_id, '').lower()
                
                # Found episeerr rule tag (not default/select)
                if tag_name.startswith('episeerr_'):
                    rule_name = tag_name.replace('episeerr_', '')
                    if rule_name not in ['default', 'select']:
                        # Find actual rule name (case-insensitive)
                        actual_rule_name = None
                        for rn in config['rules'].keys():
                            if rn.lower() == rule_name:
                                actual_rule_name = rn
                                break
                        
                        if actual_rule_name:
                            # Add to config
                            config['rules'][actual_rule_name].setdefault('series', {})[series_id] = {}
                            orphaned += 1
                            app.logger.info(f"  Orphaned: Added {series['title']} to '{actual_rule_name}'")
                            changes_made = True
                            break
        
        # Save if any changes made
        if changes_made or drift_fixed > 0 or drift_synced > 0 or orphaned > 0 or series_deleted > 0:
            save_config(config)
            if series_deleted > 0:
                app.logger.info(f"✓ Tag reconciliation complete: {drift_fixed} moved, {drift_synced} synced, {orphaned} orphaned, {series_deleted} deleted")
            else:
                app.logger.info(f"✓ Tag reconciliation complete: {drift_fixed} moved, {drift_synced} synced, {orphaned} orphaned")
        else:
            app.logger.info("✓ Tag reconciliation complete: No changes needed")
            
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
        
        # Add configuration summary
        config = load_config()
        rule_summary = {
            "total_rules": len(config['rules']),
            "cleanup_rules": 0,
            "protected_rules": 0
        }
        
        for rule_name, rule in config['rules'].items():
            has_cleanup = rule.get('grace_watched') or rule.get('grace_unwatched') or rule.get('dormant_days')
            if has_cleanup:
                rule_summary["cleanup_rules"] += 1
            else:
                rule_summary["protected_rules"] += 1
        
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
            app.logger.info(f"Form data received: {dict(request.form)}")
            config = load_config()
            
            for rule_name in config.get('rules', {}).keys():
                rule_dry_run_key = f'rule_dry_run_{rule_name}'
                rule_dry_run = rule_dry_run_key in request.form
                app.logger.info(f"Setting {rule_name} dry_run to: {rule_dry_run}")
                config['rules'][rule_name]['dry_run'] = rule_dry_run
            
            save_config(config)
            app.logger.info("save_config() called")
            
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

@app.route('/api/send-to-selection/<int:series_id>')
def send_to_selection(series_id):
    """Create a pending selection request for a Sonarr series and redirect to season selection."""
    try:
        # Check if a pending request already exists for this series — reuse it if so
        os.makedirs(REQUESTS_DIR, exist_ok=True)
        for filename in os.listdir(REQUESTS_DIR):
            if not filename.endswith('.json'):
                continue
            try:
                with open(os.path.join(REQUESTS_DIR, filename), 'r') as f:
                    existing = json.load(f)
                if str(existing.get('series_id')) == str(series_id) and existing.get('tmdb_id'):
                    config = load_config()
                    current_rule = ''
                    for rule_name_iter, rule_data in config.get('rules', {}).items():
                        if str(series_id) in rule_data.get('series', {}):
                            current_rule = rule_name_iter
                            break
                    app.logger.info(f"Reusing existing pending request for series {series_id}")
                    return redirect(url_for('select_seasons', tmdb_id=existing['tmdb_id'], current_rule=current_rule))
            except Exception:
                continue

        sonarr_preferences = sonarr_utils.load_preferences()
        headers = {
            'X-Api-Key': sonarr_preferences['SONARR_API_KEY'],
            'Content-Type': 'application/json'
        }
        sonarr_url = sonarr_preferences['SONARR_URL']

        # Get series info from Sonarr
        resp = requests.get(f"{sonarr_url}/api/v3/series/{series_id}", headers=headers, timeout=10)
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

        # Create a selection request file (same format as the Sonarr webhook handler)
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
        os.makedirs(REQUESTS_DIR, exist_ok=True)
        with open(os.path.join(REQUESTS_DIR, f"{request_id}.json"), 'w') as f:
            json.dump(pending_request, f, indent=2)

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
            for filename in os.listdir(REQUESTS_DIR):
                if not filename.endswith('.json'):
                    continue
                try:
                    with open(os.path.join(REQUESTS_DIR, filename), 'r') as f:
                        req_data = json.load(f)
                    if str(req_data.get('tmdb_id')) == str(tmdb_id):
                        request_id = req_data.get('id', '')
                        break
                except Exception:
                    continue
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
        request_file_path = None
        
        for filename in os.listdir(REQUESTS_DIR):
            if filename.endswith('.json'):
                filepath = os.path.join(REQUESTS_DIR, filename)
                try:
                    with open(filepath, 'r') as f:
                        request_data = json.load(f)
                        if str(request_data.get('tmdb_id')) == str(tmdb_id):
                            series_id = request_data.get('series_id')
                            request_id = request_data.get('id')
                            request_file_path = filepath
                            break
                except Exception:
                    continue
        
        if not series_id:
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

                _eps_resp = requests.get(
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
                        _n = _get_count or 1
                        _to_monitor = [
                            ep['id'] for ep in _all_eps
                            if _starting_season <= ep.get('seasonNumber', 0) < (_starting_season + _n)
                        ]
                    else:  # episodes
                        _n = _get_count or 1
                        _to_monitor = [ep['id'] for ep in _season_eps[:_n]]

                    if _to_monitor:
                        _mon_resp = requests.put(
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
                                _srch_resp = requests.post(
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
        
        # Clean up the pending request file
        if request_file_path and os.path.exists(request_file_path):
            try:
                os.remove(request_file_path)
                app.logger.info(f"✓ Removed pending request {request_id}")
            except Exception:
                pass
        
        # Remove episeerr_select tag (keep rule tag)
        try:
            tag_resp = requests.get(f"{SONARR_URL}/api/v3/tag", headers=headers)
            if tag_resp.ok:
                tag_map = {t['label'].lower(): t['id'] for t in tag_resp.json()}
                select_tag_id = tag_map.get('episeerr_select')
                
                if select_tag_id:
                    series_resp = requests.get(f"{SONARR_URL}/api/v3/series/{series_id}", headers=headers)
                    if series_resp.ok:
                        series_data = series_resp.json()
                        current_tags = series_data.get('tags', [])
                        if select_tag_id in current_tags:
                            current_tags.remove(select_tag_id)
                            series_data['tags'] = current_tags
                            requests.put(f"{SONARR_URL}/api/v3/series", headers=headers, json=series_data)
        except Exception as e:
            app.logger.debug(f"Tag cleanup: {e}")
        
        app.logger.info(f"Applied rule '{rule_name}' to {request_data.get('title', 'series')}")
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
        series_id = None
        request_id = None
        
        for filename in os.listdir(REQUESTS_DIR):
            if filename.endswith('.json'):
                try:
                    with open(os.path.join(REQUESTS_DIR, filename), 'r') as f:
                        request_data = json.load(f)
                        if str(request_data.get('tmdb_id')) == str(tmdb_id):
                            series_id = request_data.get('series_id')
                            request_id = request_data.get('id')
                            app.logger.info(f"Found matching request: series_id={series_id}, request_id={request_id}")
                            break
                except Exception as e:
                    app.logger.error(f"Error reading request file {filename}: {str(e)}")
        
        if not series_id or not request_id:
            return render_template('error.html', message="No pending request found for this series")
        
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
        app.logger.info(f"Raw form data: {dict(request.form)}")
        app.logger.info(f"Form lists: episodes={request.form.getlist('episodes')}")
        
        # Get form data
        request_id = request.form.get('request_id')
        episodes = request.form.getlist('episodes')  # Gets ALL values with name 'episodes'
        action = request.form.get('action')
        
        app.logger.info(f"Processing: request_id={request_id}, action={action}, episodes={episodes}")
        
        if action == 'cancel':
            # Delete the request file
            if request_id:
                request_file = os.path.join(REQUESTS_DIR, f"{request_id}.json")
                if os.path.exists(request_file):
                    os.remove(request_file)
                    app.logger.info(f"Cancelled and removed request {request_id}")
            
            return redirect(url_for('rules_page'))
        
        elif action == 'process':
            # Load request data
            request_file = os.path.join(REQUESTS_DIR, f"{request_id}.json")
            if not os.path.exists(request_file):
                return redirect(url_for('rules_page'))
            
            with open(request_file, 'r') as f:
                request_data = json.load(f)
            
            series_id = request_data['series_id']
            
            if not episodes:
                return redirect(url_for('rules_page'))
            
            app.logger.info(f"DEBUG: Processing {len(episodes)} episodes: {episodes}")
            
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
            
            # Clean up request file
            try:
                os.remove(request_file)
                app.logger.info(f"Removed request file: {request_id}.json")
            except Exception as e:
                app.logger.error(f"Error removing request file: {str(e)}")
            
            # Prepare result message
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
        pending_requests = []
        for filename in os.listdir(REQUESTS_DIR):
            if filename.endswith('.json'):
                try:
                    with open(os.path.join(REQUESTS_DIR, filename), 'r') as f:
                        request_data = json.load(f)
                        pending_requests.append(request_data)
                except Exception as e:
                    app.logger.error(f"Error reading request file {filename}: {str(e)}")
        pending_requests.sort(key=lambda x: x.get('created_at', 0), reverse=True)
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
        filename = f"{request_id}.json"
        filepath = os.path.join(REQUESTS_DIR, filename)

        if os.path.exists(filepath):
            os.remove(filepath)
            app.logger.info(f"Deleted pending request file {filename}")
            return jsonify({"status": "success", "message": "Request deleted successfully"}), 200
        else:
            app.logger.warning(f"Pending request file {filename} not found")
            return jsonify({"status": "error", "message": "Request not found"}), 404

    except Exception as e:
        app.logger.error(f"Error deleting request {request_id}: {str(e)}")
        return jsonify({"status": "error", "message": "Failed to delete request"}), 500

def process_watch_event(series_id: int, user_name: str = None):
    """
    Shared logic for any watch event (Tautulli or Jellyfin).
    - Find current config rule
    - Validate tag vs config
    - Auto-correct drift if needed
    - Apply GET + KEEP for the (possibly updated) rule
    - Update activity_date
    """
    config = load_config()
    
    # 1. Find current assignment in config
    config_rule = None
    series_id_str = str(series_id)
    for rule_name, rule_data in config['rules'].items():
        if series_id_str in rule_data.get('series', {}):
            config_rule = rule_name
            break
    
    if not config_rule:
        app.logger.info(f"Series {series_id} not assigned to any rule → skipping watch processing")
        return False, "Not assigned"
    
    # 2. Check what Sonarr currently says
    matches, actual_tag_rule = episeerr_utils.validate_series_tag(series_id, config_rule)
    
    if matches:
        app.logger.debug(f"Tag matches config: {config_rule}")
        final_rule = config_rule
    
    else:
        if actual_tag_rule:
            # Drift: tag was changed manually → move config to match tag
            app.logger.warning(f"DRIFT DETECTED — config: {config_rule}, Sonarr tag: {actual_tag_rule}")
            episeerr_utils.move_series_in_config(series_id, config_rule, actual_tag_rule)
            final_rule = actual_tag_rule
            app.logger.info(f"Series moved to rule '{final_rule}' to match Sonarr tag")
        
        else:
            # No episeerr rule tag at all → restore from config
            app.logger.warning(f"No episeerr rule tag found → restoring episeerr_{config_rule}")
            episeerr_utils.sync_rule_tag_to_sonarr(series_id, config_rule)
            final_rule = config_rule
    
    # 3. Apply GET + KEEP logic for final_rule
    rule_config = config['rules'][final_rule]
    
    # ─── Your GET logic here ───
    # monitor new episodes, search, etc. based on rule['get_type'], ['get_count'], etc.
    # Example:
    # apply_get_settings(series_id, rule_config)
    
    # ─── Your KEEP logic here ───
    # delete old episodes based on grace periods, keep_count, etc.
    # Example:
    # apply_keep_cleanup(series_id, rule_config)
    
    # 4. Update activity tracking
    series_data = config['rules'][final_rule]['series'][series_id_str]
    series_data['activity_date'] = int(time.time())
    # Optional: update last_season / last_episode if you track them
    
    save_config(config)
    
    app.logger.info(f"Watch processed for series {series_id} under rule '{final_rule}'")
    return True, final_rule
# ============================================================================
# WEBHOOK ROUTES
# ============================================================================

@app.route('/sonarr-webhook', methods=['POST'])
def process_sonarr_webhook():
    """Handle incoming Sonarr webhooks for series additions with enhanced tag-based assignment."""
    app.logger.info("Received Sonarr webhook")
    
    try:
        json_data = request.json
        
        event_type = json_data.get('eventType')
        app.logger.info(f"Sonarr webhook event type: {event_type}")
        
        if event_type == 'Grab':
            return handle_episode_grab(json_data)
        
        series = json_data.get('series', {})
        series_id = series.get('id')
        tvdb_id = series.get('tvdbId')
        tmdb_id = series.get('tmdbId')
        series_title = series.get('title')
        
        app.logger.info(f"Processing series addition: {series_title} (ID: {series_id}, TVDB: {tvdb_id})")
        
        # Sonarr connection setup
        sonarr_preferences = sonarr_utils.load_preferences()
        headers = {
            'X-Api-Key': sonarr_preferences['SONARR_API_KEY'],
            'Content-Type': 'application/json'
        }
        SONARR_URL = sonarr_preferences['SONARR_URL']

        # ────────────────────────────────────────────────────────────────
        # Jellyseerr request cleanup (moved up)
        # ────────────────────────────────────────────────────────────────
        jellyseerr_request_id = None
        jellyseerr_requested_seasons = None
        tvdb_id_str = str(tvdb_id) if tvdb_id else None

        if tvdb_id_str:
            request_file = os.path.join(REQUESTS_DIR, f"jellyseerr-{tvdb_id_str}.json")
            if os.path.exists(request_file):
                try:
                    with open(request_file, 'r') as f:
                        request_data = json.load(f)
                    jellyseerr_request_id = request_data.get('request_id')
                    jellyseerr_requested_seasons = request_data.get('requested_seasons')
                    app.logger.info(f"✓ Found Jellyseerr request file: {jellyseerr_request_id}")
                    
                    app.logger.info(f"Cancelling Jellyseerr request {jellyseerr_request_id}")
                    episeerr_utils.delete_overseerr_request(jellyseerr_request_id)
                    
                    try:
                        from activity_storage import save_request_event
                        save_request_event(request_data)
                    except Exception as e:
                        app.logger.error(f"Failed to log request to activity: {e}")
                    
                    os.remove(request_file)
                    app.logger.info(f"✓ Removed Jellyseerr request file")
                except Exception as e:
                    app.logger.error(f"Error processing Jellyseerr request file: {str(e)}")

        # ────────────────────────────────────────────────────────────────
        # Enhanced tag detection - supports all rule tags
        # ────────────────────────────────────────────────────────────────
        tags_response = requests.get(f"{SONARR_URL}/api/v3/tag", headers=headers)
        if not tags_response.ok:
            app.logger.error(f"Failed to get Sonarr tags: {tags_response.status_code}")
            return jsonify({"status": "error", "message": "Failed to get tags"}), 500
        
        tags = tags_response.json()
        tag_mapping = {tag['id']: tag['label'].lower() for tag in tags}

        series_tags = series.get('tags', [])
        app.logger.info(f"Series tags (IDs): {series_tags}")
        app.logger.info(f"Tag mapping: {tag_mapping}")

        assigned_rule = None
        is_select_request = False

        config = None  # lazy load
        
        # Create reverse mapping (label -> id) for webhook format compatibility
        reverse_tag_mapping = {label.lower(): tag_id for tag_id, label in tag_mapping.items()}
        app.logger.debug(f"Reverse tag mapping created: {len(reverse_tag_mapping)} tags")

        for tag_id in series_tags:
            # Handle both formats: integer IDs and string labels
            original_tag = tag_id
            tag_label = None
            
            if isinstance(tag_id, int):
                # Standard format: integer ID
                tag_label = tag_mapping.get(tag_id, '').lower()
            elif isinstance(tag_id, str):
                # Webhook format: string label
                # Check if it looks like a tag label
                tag_label = tag_id.lower()
                # Try to get the actual ID from reverse mapping
                actual_tag_id = reverse_tag_mapping.get(tag_label)
                if actual_tag_id:
                    tag_id = actual_tag_id
                    app.logger.debug(f"Converted tag label '{original_tag}' to ID {tag_id}")
                else:
                    app.logger.warning(f"Tag label '{original_tag}' not found in Sonarr tags")
                    continue
            else:
                app.logger.error(f"Unexpected tag type: {original_tag} (type: {type(original_tag)})")
                continue
            
            if not tag_label:
                app.logger.warning(f"Could not determine label for tag: {original_tag}")
                continue
                
            if not tag_label.startswith('episeerr_'):
                continue
                
            rule_name = tag_label.replace('episeerr_', '')
            app.logger.info(f"Processing episeerr tag: {tag_label} (rule_name: {rule_name})")
            
            if rule_name == 'select':
                is_select_request = True
                app.logger.info("Detected episeerr_select tag → selection workflow")
                break
                
            else:
                # Direct rule tag - case-insensitive lookup
                if config is None:
                    config = load_config()
                
                actual_rule_name = None
                for rn in config.get('rules', {}).keys():
                    if rn.lower() == rule_name.lower():
                        actual_rule_name = rn
                        break
                
                if actual_rule_name:
                    assigned_rule = actual_rule_name
                    app.logger.info(f"✓ Detected direct rule tag: episeerr_{rule_name} → matched rule '{actual_rule_name}'")
                    break
                else:
                    app.logger.warning(f"Ignoring unknown rule tag: episeerr_{rule_name}")
                    if config:
                        app.logger.warning(f"Available rules: {list(config.get('rules', {}).keys())}")
                    else:
                        app.logger.warning("Config not yet loaded")

        # ────────────────────────────────────────────────────────────────
        # No episeerr tag → auto-assign fallback
        # ────────────────────────────────────────────────────────────────
        app.logger.info(f"=== TAG DETECTION SUMMARY ===")
        app.logger.info(f"assigned_rule: {assigned_rule}")
        app.logger.info(f"is_select_request: {is_select_request}")        
        if not assigned_rule and not is_select_request:
            import media_processor
            global_settings = media_processor.load_global_settings()
            app.logger.info(f"Auto-assign check: enabled={global_settings.get('auto_assign_new_series', False)}")
            
            if global_settings.get('auto_assign_new_series', False):
                if config is None:
                    config = load_config()
                default_rule_name = config.get('default_rule', 'default')
                
                if default_rule_name not in config['rules']:
                    return jsonify({"status": "error", "message": f"Default rule '{default_rule_name}' missing"}), 500
                
                series_id_str = str(series_id)
                target_rule = config['rules'][default_rule_name]
                target_rule.setdefault('series', {})
                series_dict = target_rule['series']
                
                if series_id_str not in series_dict:
                    series_dict[series_id_str] = {'activity_date': None}
                    save_config(config)
                    try:
                        episeerr_utils.sync_rule_tag_to_sonarr(series_id, default_rule_name)
                    except Exception as e:
                        app.logger.error(f"Auto-assign tag sync failed: {e}")
                    app.logger.info(f"Auto-assigned to default rule '{default_rule_name}'")
                
                # Set assigned_rule and continue to processing
                assigned_rule = default_rule_name
            else:
                # ONLY return if auto-assign is OFF
                app.logger.info("No episeerr tags + auto-assign off → no action")
                return jsonify({"status": "success", "message": "No processing needed"}), 200

        # ────────────────────────────────────────────────────────────────
        # We have action → unmonitor + cleanup tags + cancel downloads
        # ────────────────────────────────────────────────────────────────
        app.logger.info(f"Unmonitoring episodes for {series_title}")
        episeerr_utils.unmonitor_series(series_id, headers)
        
        # Remove ONLY episeerr_select/episeerr_default/episeerr_delay (keep rule tags)
        updated_tags = []
        removed = []
        had_delay_tag = False
        
        # Create reverse mapping for string labels
        reverse_tag_mapping = {label.lower(): tag_id for tag_id, label in tag_mapping.items()}
        
        for tag_item in series_tags:
            # Handle both integer IDs and string labels
            if isinstance(tag_item, int):
                # Integer ID format
                tag_id = tag_item
                label = tag_mapping.get(tag_id, '').lower()
            elif isinstance(tag_item, str):
                # String label format (from webhook)
                label = tag_item.lower()
                tag_id = reverse_tag_mapping.get(label)
                if not tag_id:
                    app.logger.warning(f"Unknown tag label in removal: {tag_item}")
                    continue
            else:
                app.logger.warning(f"Unexpected tag type in removal: {tag_item}")
                continue
            
            if label in ['episeerr_select', 'episeerr_delay']:
                removed.append(label)
                if label == 'episeerr_delay':
                    had_delay_tag = True
            else:
                # Keep this tag - use integer ID
                updated_tags.append(tag_id)
        
        if removed:
            # Get fresh series data from Sonarr
            series_resp = requests.get(
                f"{SONARR_URL}/api/v3/series/{series_id}",
                headers=headers
            )
            
            if series_resp.ok:
                update_payload = series_resp.json()
                update_payload['tags'] = updated_tags
                
                resp = requests.put(
                    f"{SONARR_URL}/api/v3/series",
                    headers=headers,
                    json=update_payload
                )
                
                if resp.ok:
                    app.logger.info(f"Removed control tag(s): {removed}")
                else:
                    app.logger.error(f"Tag removal failed: {resp.text}")
            else:
                app.logger.error(f"Failed to fetch series data: {series_resp.status_code}")
        
        try:
            episeerr_utils.check_and_cancel_unmonitored_downloads()
        except Exception as e:
            app.logger.error(f"Download cancel failed: {e}")

        # ────────────────────────────────────────────────────────────────
        # Branch: selection vs rule processing
        # ────────────────────────────────────────────────────────────────
        if is_select_request:
            app.logger.info(f"Processing {series_title} with episeerr_select tag - creating selection request")
            
            # Ensure we have a TMDB ID for the UI
            if not tmdb_id:
                try:
                    external_ids = get_external_ids(tvdb_id, 'tv')
                    if external_ids and external_ids.get('tmdb_id'):
                        tmdb_id = external_ids['tmdb_id']
                    else:
                        search_results = search_tv_shows(series_title)
                        if search_results.get('results'):
                            tmdb_id = search_results['results'][0]['id']
                except Exception as e:
                   app.logger.error(f"Error finding TMDB ID: {str(e)}")
            
            # Create a selection request
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
                "jellyseerr_request_id": jellyseerr_request_id,
                "created_at": int(time.time())
            }
            
            os.makedirs(REQUESTS_DIR, exist_ok=True)
            with open(os.path.join(REQUESTS_DIR, f"{request_id}.json"), 'w') as f:
                json.dump(pending_request, f, indent=2)
            
            app.logger.info(f"✓ Created episode selection request for {series_title}")

            try:
                from notifications import send_notification
                send_notification(
                    "selection_pending",
                    series=series_title,
                    series_id=series_id
                )
            except Exception as e:
                app.logger.error(f"Failed to send selection pending notification: {e}")
            
            return jsonify({"status": "success", "message": "Selection request created"}), 200

        # ─── Rule processing ─────────────────────────────────────────────
        if config is None:
            config = load_config()

        app.logger.info(f"Applying rule: {assigned_rule}")

        # Determine starting season
        starting_season = 1
        if jellyseerr_requested_seasons:
            try:
                seasons = [int(s.strip()) for s in str(jellyseerr_requested_seasons).split(',')]
                if seasons:
                    starting_season = min(seasons)
                    app.logger.info(f"✓ Using requested season {starting_season} from Jellyseerr")
            except Exception as e:
                app.logger.warning(f"Could not parse requested seasons: {e}")

        # Add to config + sync tag
        series_id_str = str(series_id)
        target_rule = config['rules'][assigned_rule]
        target_rule.setdefault('series', {})
        series_dict = target_rule['series']
        
        if series_id_str not in series_dict:
            series_dict[series_id_str] = {'activity_date': None}
            save_config(config)
            try:
                episeerr_utils.sync_rule_tag_to_sonarr(series_id, assigned_rule)
                app.logger.info(f"Synced tag episeerr_{assigned_rule}")
            except Exception as e:
                app.logger.error(f"Tag sync failed: {e}")

        # Execute rule logic
        try:
            rule_config = config['rules'][assigned_rule]
            get_type = rule_config.get('get_type', 'episodes')
            get_count = rule_config.get('get_count', 1)
            action_option = rule_config.get('action_option', 'monitor')

            # Process always_have FIRST (additive on top of get_type, runs after unmonitor)
            always_have = rule_config.get('always_have', '')
            if always_have:
                try:
                    media_processor.process_always_have(series_id, always_have)
                except Exception as e:
                    app.logger.error(f"always_have processing failed for series {series_id}: {e}")

            app.logger.info(f"Executing rule '{assigned_rule}' with get_type '{get_type}', get_count '{get_count}' starting from Season {starting_season}")
            
            # Get all episodes for the series
            episodes_response = requests.get(
                f"{SONARR_URL}/api/v3/episode?seriesId={series_id}",
                headers=headers
            )
            
            if episodes_response.ok:
                all_episodes = episodes_response.json()
                
                # Get episodes from the requested season
                requested_season_episodes = sorted(
                    [ep for ep in all_episodes if ep.get('seasonNumber') == starting_season],
                    key=lambda x: x.get('episodeNumber', 0)
                )
                
                if not requested_season_episodes:
                    app.logger.warning(f"No Season {starting_season} episodes found for {series_title}")
                else:
                    # Determine which episodes to monitor based on get settings
                    episodes_to_monitor = []
                    
                    if get_type == 'all':
                        episodes_to_monitor = [
                            ep['id'] for ep in all_episodes 
                            if ep.get('seasonNumber') >= starting_season
                        ]
                        app.logger.info(f"Monitoring all episodes from Season {starting_season} onward")
                        
                    elif get_type == 'seasons':
                        num_seasons = get_count or 1
                        episodes_to_monitor = [
                            ep['id'] for ep in all_episodes 
                            if starting_season <= ep.get('seasonNumber') < (starting_season + num_seasons)
                        ]
                        app.logger.info(f"Monitoring {num_seasons} season(s) starting from Season {starting_season} ({len(episodes_to_monitor)} episodes)")
                        
                    else:  # episodes
                        try:
                            num_episodes = get_count or 1
                            episodes_to_monitor = [ep['id'] for ep in requested_season_episodes[:num_episodes]]
                            app.logger.info(f"Monitoring first {len(episodes_to_monitor)} episodes of Season {starting_season}")
                        except (ValueError, TypeError):
                            episodes_to_monitor = [requested_season_episodes[0]['id']] if requested_season_episodes else []
                            app.logger.warning(f"Invalid get_count, defaulting to first episode")
                    
                    if episodes_to_monitor:
                        # Monitor the selected episodes
                        monitor_response = requests.put(
                            f"{SONARR_URL}/api/v3/episode/monitor",
                            headers=headers,
                            json={"episodeIds": episodes_to_monitor, "monitored": True}
                        )
                        
                        if monitor_response.ok:
                            app.logger.info(f"✓ Monitored {len(episodes_to_monitor)} episodes for {series_title}")
                            
                            # Search for episodes if action_option is 'search'
                            if action_option == 'search':
                                if get_type == 'seasons':
                                    # Use SeasonSearch for season-based rules
                                    first_ep_response = requests.get(
                                        f"{SONARR_URL}/api/v3/episode/{episodes_to_monitor[0]}",
                                        headers=headers
                                    )
                                    if first_ep_response.ok:
                                        first_ep = first_ep_response.json()
                                        season_number = first_ep.get('seasonNumber')
                                        
                                        app.logger.info(f"Searching for season pack for Season {season_number}")
                                        search_json = {
                                            "name": "SeasonSearch",
                                            "seriesId": series_id,
                                            "seasonNumber": season_number
                                        }
                                    else:
                                        search_json = {"name": "EpisodeSearch", "episodeIds": episodes_to_monitor}
                                else:
                                    # Individual episode search
                                    search_json = {"name": "EpisodeSearch", "episodeIds": episodes_to_monitor}
                                
                                search_response = requests.post(
                                    f"{SONARR_URL}/api/v3/command",
                                    headers=headers,
                                    json=search_json
                                )
                                
                                if search_response.ok:
                                    search_type = "season pack" if get_type == 'seasons' else "episodes"
                                    app.logger.info(f"✓ Started search for {search_type}")
                                else:
                                    app.logger.error(f"Failed to search: {search_response.text}")
                        else:
                            app.logger.error(f"Failed to monitor episodes: {monitor_response.text}")
                    else:
                        app.logger.warning(f"No episodes to monitor for {series_title}")
                        
                    # ────────────────────────────────────────────────────────────────
                    # Remove episeerr_delay tag to allow immediate downloads
                    # ────────────────────────────────────────────────────────────────
                    # Now that we've monitored the correct episodes and triggered searches,
                    # remove the delay tag so downloads can proceed immediately
                    
                    try:
                        delay_tag_id = episeerr_utils.get_or_create_rule_tag_id('delay')
                        if delay_tag_id:
                            # Get fresh series data
                            series_refresh_resp = requests.get(
                                f"{SONARR_URL}/api/v3/series/{series_id}",
                                headers=headers
                            )
                            
                            if series_refresh_resp.ok:
                                fresh_series = series_refresh_resp.json()
                                current_tags = fresh_series.get('tags', [])
                                
                                if delay_tag_id in current_tags:
                                    # Remove delay tag
                                    current_tags.remove(delay_tag_id)
                                    fresh_series['tags'] = current_tags
                                    
                                    update_resp = requests.put(
                                        f"{SONARR_URL}/api/v3/series",
                                        headers=headers,
                                        json=fresh_series
                                    )
                                    
                                    if update_resp.ok:
                                        app.logger.info(f"✓ Removed episeerr_delay tag - downloads can proceed immediately")
                                    else:
                                        app.logger.error(f"Failed to remove delay tag: {update_resp.text}")
                                else:
                                    app.logger.debug("episeerr_delay tag not present (already removed or never added)")
                            else:
                                app.logger.error(f"Failed to refresh series data: {series_refresh_resp.status_code}")
                        else:
                            app.logger.warning("Could not get delay tag ID")
                            
                    except Exception as e:
                        app.logger.error(f"Error removing delay tag: {str(e)}")
            else:
                app.logger.error(f"Failed to get episodes: {episodes_response.text}")
        except Exception as e:
            app.logger.error(f"Error executing rule: {str(e)}", exc_info=True)

        return jsonify({"status": "success", "message": "Processing completed"}), 200

    except Exception as e:
        app.logger.error(f"Error processing Sonarr webhook: {str(e)}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500
    
def handle_episode_grab(json_data):
    """
    Handle episode grab:
    1. Mark series as cleaned (stops grace checking)
    2. Log download for dashboard
    3. Delete pending Discord notifications
    """
    try:
        series = json_data.get('series', {})
        series_id = series.get('id')
        series_title = series.get('title', 'Unknown')
        episodes = json_data.get('episodes', [])
        
        if not episodes:
            app.logger.warning(f"Grab webhook for {series_title} has no episodes")
            return jsonify({"status": "success", "message": "No episodes in grab"}), 200
        
        episode_info = episodes[0]
        season_num = episode_info.get('seasonNumber')
        episode_num = episode_info.get('episodeNumber')
        episode_id = episode_info.get('id')
        
        app.logger.info(f"✅ Episode grabbed: {series_title} S{season_num}E{episode_num}")
        
        # ──────────────────────────────────────────────────────
        # 1. MARK AS CLEANED (stops grace checking)
        # ──────────────────────────────────────────────────────
        config = load_config()
        
        for rule_name, rule in config['rules'].items():
            if str(series_id) in rule.get('series', {}):
                series_data = rule['series'][str(series_id)]
                
                if isinstance(series_data, dict):
                    series_data['grace_cleaned'] = True
                    save_config(config)
                    app.logger.info(f"✓ Marked as cleaned in rule '{rule_name}'")
                break
        
        # ──────────────────────────────────────────────────────
        # 2. LOG DOWNLOAD FOR DASHBOARD (7-day rolling window)
        # ──────────────────────────────────────────────────────
        try:
            from datetime import datetime, timedelta
            
            download_event = {
                'series_title': series_title,
                'series_id': series_id,
                'season': season_num,
                'episode': episode_num,
                'episode_title': episode_info.get('title', ''),
                'timestamp': datetime.now().isoformat()
            }
            
            downloads_file = os.path.join(os.getcwd(), 'data', 'recent_downloads.json')
            os.makedirs(os.path.dirname(downloads_file), exist_ok=True)
            
            if os.path.exists(downloads_file):
                with open(downloads_file, 'r') as f:
                    downloads = json.load(f)
            else:
                downloads = []
            
            # Auto-cleanup: keep only last 7 days
            cutoff = datetime.now() - timedelta(days=7)
            downloads = [
                d for d in downloads 
                if datetime.fromisoformat(d['timestamp']) > cutoff
            ]

            # Remove any existing entry for this episode before adding new one
            episode_key = (download_event['series_id'], download_event['season'], download_event['episode'])
            downloads = [
                d for d in downloads 
                if (d['series_id'], d['season'], d['episode']) != episode_key
            ]

            # Add new download at the front
            downloads.insert(0, download_event)

            # Keep only 50 most recent (optional limit)
            downloads = downloads[:50]

            with open(downloads_file, 'w') as f:
                json.dump(downloads, f, indent=2)
                
            app.logger.info(f"📥 Logged download for dashboard: {series_title} S{season_num}E{episode_num}")
            
        except Exception as e:
            app.logger.error(f"Error logging download for dashboard: {e}")
        
        # ──────────────────────────────────────────────────────
        # 3. DELETE PENDING DISCORD NOTIFICATION
        # ──────────────────────────────────────────────────────
        try:
            from notification_storage import get_and_remove_notification
            from notifications import delete_discord_message
            
            message_id = get_and_remove_notification(episode_id)
            
            if message_id:
                app.logger.info(f"🗑️ Deleting pending search notification for episode {episode_id}")
                if delete_discord_message(message_id):
                    app.logger.info(f"✅ Successfully deleted notification message {message_id}")
                else:
                    app.logger.warning(f"⚠️ Failed to delete notification message {message_id}")
        except ImportError:
            # Notification modules not available, skip
            pass
        except Exception as e:
            app.logger.error(f"Error deleting notification: {e}")
        
        return jsonify({"status": "success", "message": "Grab processed"}), 200
                
    except Exception as e:
        app.logger.error(f"Error handling grab webhook: {str(e)}")
        import traceback
        app.logger.error(f"Traceback: {traceback.format_exc()}")
        return jsonify({"status": "error", "message": str(e)}), 500
                        

@app.route('/seerr-webhook', methods=['POST'])
def process_seerr_webhook():
    """Handle incoming Jellyseerr webhooks."""
    try:
        app.logger.info("=== JELLYSEERR WEBHOOK RECEIVED ===")
        json_data = request.json
        
        # Log the full webhook data for debugging
        app.logger.info(f"Jellyseerr webhook data: {json.dumps(json_data, indent=2)}")
        
        # Get the request ID
        request_info = json_data.get('request', {})
        request_id = (
            request_info.get('request_id') or 
            request_info.get('id') or 
            json_data.get('request_id') or
            json_data.get('id')
        )
        
        # Get media info
        media_info = json_data.get('media', {})
        media_type = media_info.get('media_type')
        
        app.logger.info(f"Request ID: {request_id}, Media Type: {media_type}")
        
        # Only process TV show requests
        if media_type != 'tv':
            app.logger.info(f"Request is not a TV show (media_type={media_type}), skipping")
            return jsonify({"status": "success", "message": "Not a TV request"}), 200
        
        # Get identifiers
        tvdb_id = media_info.get('tvdbId')
        tmdb_id = media_info.get('tmdbId')
        title = json_data.get('subject', 'Unknown Show')
        
        app.logger.info(f"TVDB ID: {tvdb_id}, TMDB ID: {tmdb_id}, Title: {title}")
        
        # ADDED: Extract requested seasons from webhook
        requested_seasons_str = None
        extra = json_data.get('extra', [])
        for item in extra:
            if item.get('name') == 'Requested Seasons':
                requested_seasons_str = item.get('value')
                break
        
        if tvdb_id and request_id:
            tvdb_id_str = str(tvdb_id)
            request_file = os.path.join(REQUESTS_DIR, f"jellyseerr-{tvdb_id_str}.json")
            
            # Get poster path from TMDB
            poster_path = get_tmdb_poster_path(tmdb_id) if tmdb_id else None

            request_data = {
                'request_id': request_id,
                'title': title,
                'tmdb_id': poster_path or str(tmdb_id),  # Use poster path if available, fallback to ID
                'tvdb_id': tvdb_id,
                'requested_seasons': requested_seasons_str,
                'timestamp': int(time.time())
}
            
            os.makedirs(REQUESTS_DIR, exist_ok=True)
            with open(request_file, 'w') as f:
                json.dump(request_data, f)
            
            app.logger.info(f"✓ Stored Jellyseerr request {request_id} for TVDB ID {tvdb_id_str} ({title}) - Seasons: {requested_seasons_str}")
        else:
            app.logger.warning(f"Missing required data - TVDB ID: {tvdb_id}, Request ID: {request_id}")

        return jsonify({"status": "success"}), 200
        
    except Exception as e:
        app.logger.error(f"Error processing Jellyseerr webhook: {str(e)}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/webhook', methods=['POST'])
def handle_server_webhook():
    app.logger.info("Received webhook from Tautulli")
    data = request.json
    if not data:
        return jsonify({'status': 'error', 'message': 'No data received'}), 400
    
    try:
        # Extract identifiers (original)
        series_title = data.get('plex_title') or data.get('server_title') or 'Unknown'
        season_number = data.get('plex_season_num') or data.get('server_season_num')
        episode_number = data.get('plex_ep_num') or data.get('server_ep_num')
        thetvdb_id = data.get('thetvdb_id')
        themoviedb_id = data.get('themoviedb_id')

        # ─── NEW: Tag sync & drift correction BEFORE processing ───
        # Late import the needed functions from media_processor.py
        from media_processor import get_series_id, better_partial_match, validate_series_tag, sync_rule_tag_to_sonarr, move_series_in_config

        series_id = get_series_id(series_title, thetvdb_id, themoviedb_id)
        if not series_id:
            app.logger.warning(f"Could not find Sonarr series ID for '{series_title}'")
            # Proceed with temp file anyway (original behavior)
        else:
            # Tag sync/drift logic (what we added today)
            config = load_config()
            config_rule = None
            series_id_str = str(series_id)
            
            for rule_name, rule_details in config['rules'].items():
                if series_id_str in rule_details.get('series', {}):
                    config_rule = rule_name
                    break
            
            if config_rule:
                matches, actual_tag_rule = validate_series_tag(series_id, config_rule)
                
                if not matches:
                    if actual_tag_rule:
                        app.logger.warning(f"DRIFT DETECTED - config: {config_rule} → tag: {actual_tag_rule}")
                        move_series_in_config(series_id, config_rule, actual_tag_rule)
                        final_rule = actual_tag_rule
                    else:
                        app.logger.warning(f"No episeerr tag on series {series_id} → restoring episeerr_{config_rule}")
                        sync_rule_tag_to_sonarr(series_id, config_rule)
                        final_rule = config_rule
                else:
                    final_rule = config_rule
                    app.logger.debug(f"Tag matches config rule: {final_rule}")
            if config_rule:
                matches, actual_tag_rule = validate_series_tag(series_id, config_rule)
                
                if not matches:
                    if actual_tag_rule:
                        app.logger.warning(f"DRIFT DETECTED - config: {config_rule} → tag: {actual_tag_rule}")
                        move_series_in_config(series_id, config_rule, actual_tag_rule)
                        final_rule = actual_tag_rule
                    else:
                        app.logger.warning(f"No episeerr tag on series {series_id} → restoring episeerr_{config_rule}")
                        sync_rule_tag_to_sonarr(series_id, config_rule)
                        final_rule = config_rule
                else:
                    final_rule = config_rule
                    app.logger.debug(f"Tag matches config rule: {final_rule}")
            else:
                # ORPHANED TAG DETECTION: Series not in config but might have episeerr tag in Sonarr
                series = episeerr_utils.get_series_from_sonarr(series_id)
                if series:
                    tag_mapping = episeerr_utils.get_tag_mapping()
                    found_orphaned_tag = False
                    
                    for tag_id in series.get('tags', []):
                        tag_name = tag_mapping.get(tag_id, '').lower()
                        
                        if tag_name.startswith('episeerr_'):
                            rule_name = tag_name.replace('episeerr_', '')
                            
                            # Skip special tags
                            if rule_name in ['default', 'select']:
                                continue
                            
                            # Find the rule (case-insensitive)
                            actual_rule_name = None
                            for rn in config['rules'].keys():
                                if rn.lower() == rule_name:
                                    actual_rule_name = rn
                                    break
                            
                            if actual_rule_name:
                                # Add series to config
                                import time
                                config['rules'][actual_rule_name].setdefault('series', {})[series_id_str] = {
                                    'activity_date': int(time.time())
                                }
                                save_config(config)
                                app.logger.info(f"🏷️ ORPHANED: Added series {series_id} ('{series_title}') to '{actual_rule_name}' based on Sonarr tag")
                                final_rule = actual_rule_name
                                found_orphaned_tag = True
                                break
                            else:
                                app.logger.warning(f"Found episeerr tag '{tag_name}' on series {series_id} but rule '{rule_name}' doesn't exist in config")
                                final_rule = None
                                found_orphaned_tag = True
                                break
                    
                    if not found_orphaned_tag:
                        # No episeerr tags found
                        final_rule = None
                        app.logger.info(f"Series {series_id} not assigned to any rule → skipping tag sync")
                else:
                    final_rule = None
                    app.logger.warning(f"Could not fetch series {series_id} from Sonarr")

        # ─── Original temp file creation ───
        temp_dir = os.path.join(os.getcwd(), 'temp')
        os.makedirs(temp_dir, exist_ok=True)
        
        plex_data = {
            "server_title": series_title,
            "server_season_num": season_number,
            "server_ep_num": episode_number,
            "thetvdb_id": thetvdb_id,
            "themoviedb_id": themoviedb_id,
            "sonarr_series_id": series_id,          # Added: useful for media_processor
            "rule": final_rule                      # Added: pass final corrected rule
        }
        
        temp_file_path = os.path.join(temp_dir, 'data_from_server.json')
        with open(temp_file_path, 'w') as f:
            json.dump(plex_data, f)
        
        # ─── Original subprocess call ───
        result = subprocess.run(
            ["python3", os.path.join(os.getcwd(), "media_processor.py")],
            capture_output=True,
            text=True
        )
        
        if result.returncode != 0:
            app.logger.error(f"media_processor.py failed with return code {result.returncode}")
            if result.stderr:
                app.logger.error(f"Error output: {result.stderr}")
        else:
            app.logger.info("media_processor.py completed successfully")
            if result.stderr:
                app.logger.info(f"Processor output: {result.stderr}")
        
        app.logger.info("Webhook processing completed - activity tracked, next content processed")
        return jsonify({'status': 'success'}), 200
        
    except Exception as e:
        app.logger.error(f"Failed to process Tautulli webhook: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

# Replace your webhook handler with this version that uses SessionStart

@app.route('/jellyfin-webhook', methods=['POST'])
def handle_jellyfin_webhook():
    """Handle Jellyfin webhooks - Supports polling (SessionStart) OR real-time (PlaybackProgress)."""
    app.logger.info("Received webhook from Jellyfin")
    data = request.json
    if not data:
        return jsonify({'status': 'error', 'message': 'No data received'}), 400

    try:
        notification_type = data.get('NotificationType')
        app.logger.info(f"Jellyfin webhook type: {notification_type}")

        # ============================================================================
        # REAL-TIME MODE: PlaybackProgress (NEW)
        # ============================================================================
        if notification_type == 'PlaybackProgress':
            item_type = data.get('ItemType')
            if item_type == 'Episode':
                series_name = data.get('SeriesName')
                season = data.get('SeasonNumber')
                episode = data.get('EpisodeNumber')
                
                # Calculate progress percentage
                progress_ticks = data.get('PlaybackPositionTicks', 0)
                runtime_ticks = data.get('RunTimeTicks', 1)
                progress_percent = (progress_ticks / runtime_ticks * 100) if runtime_ticks > 0 else 0
                
                session_id = data.get('SessionId') or data.get('PlaySessionId') or data.get('Id')
                user_name = data.get('NotificationUsername', 'Unknown')
                
                if all([series_name, season is not None, episode is not None]):
                    from media_processor import (
                        process_jellyfin_progress_webhook,
                        check_jellyfin_user,
                        JELLYFIN_TRIGGER_MIN
                    )
                    
                    if not check_jellyfin_user(user_name):
                        return jsonify({'status': 'success', 'message': 'User not configured'}), 200
                    
                    if progress_percent >= JELLYFIN_TRIGGER_MIN:
                        success = process_jellyfin_progress_webhook(
                            session_id=session_id,
                            series_name=series_name,
                            season_number=season,
                            episode_number=episode,
                            progress_percent=progress_percent,
                            user_name=user_name
                        )
                        
                        if success:
                            app.logger.info(f"✅ Processed {series_name} S{season}E{episode}")
                    
                    return jsonify({'status': 'success'}), 200
                    
                else:
                    app.logger.warning("Missing episode data in PlaybackProgress")
                    return jsonify({'status': 'error', 'message': 'Missing episode data'}), 400
            else:
                return jsonify({'status': 'success', 'message': 'Not an episode'}), 200

        # ============================================================================
        # POLLING MODE: SessionStart or PlaybackStart (OLD - backward compatible)
        # ============================================================================
        elif notification_type in ['SessionStart', 'PlaybackStart']:
            item_type = data.get('ItemType')
            if item_type == 'Episode':
                series_name = data.get('SeriesName')
                season = data.get('SeasonNumber')
                episode = data.get('EpisodeNumber')
                webhook_id = data.get('Id')
                user_name = data.get('NotificationUsername', 'Unknown')

                if all([series_name, season is not None, episode is not None]):
                    app.logger.info(f"📺 Jellyfin session started: {series_name} S{season}E{episode} (User: {user_name})")
                    
                    try:
                        from media_processor import start_jellyfin_polling
                        
                        episode_info = {
                            'user_name': user_name,
                            'series_name': series_name,
                            'season_number': int(season),
                            'episode_number': int(episode),
                            'progress_percent': 0.0,
                            'is_paused': False
                        }
                        
                        polling_started = start_jellyfin_polling(webhook_id, episode_info)
                        
                        if polling_started:
                            app.logger.info(f"✅ Started polling for {series_name} S{season}E{episode}")
                            return jsonify({'status': 'success', 'message': 'Started polling'}), 200
                        else:
                            app.logger.warning(f"⚠️ Failed to start polling (may already be active)")
                            return jsonify({'status': 'warning', 'message': 'Polling may already be active'}), 200
                            
                    except Exception as e:
                        app.logger.error(f"Error starting Jellyfin polling: {str(e)}")
                        return jsonify({'status': 'error', 'message': f'Failed to start polling: {str(e)}'}), 500
                else:
                    app.logger.warning("Missing required fields for SessionStart")
                    return jsonify({'status': 'error', 'message': 'Missing fields'}), 400
            else:
                app.logger.info(f"Item type '{item_type}' is not an episode")
                return jsonify({'status': 'success', 'message': 'Not an episode'}), 200

        # ============================================================================
        # CLEANUP: PlaybackStop
        # ============================================================================
        elif notification_type == 'PlaybackStop':
            webhook_id = data.get('Id')
            series_name = data.get('SeriesName', 'Unknown')
            season = data.get('SeasonNumber')
            episode = data.get('EpisodeNumber')
            user_name = data.get('NotificationUsername', 'Unknown')
            
            app.logger.info(f"📺 Jellyfin playback stopped: {series_name} S{season}E{episode} (User: {user_name})")
            
            # Stop polling if active
            try:
                from media_processor import stop_jellyfin_polling
                stopped = stop_jellyfin_polling(webhook_id)
                if stopped:
                    app.logger.info(f"🛑 Stopped polling for {series_name}")
            except Exception as e:
                app.logger.debug(f"No polling to stop: {e}")
            
            # Clean up progress tracking
            try:
                from media_processor import cleanup_jellyfin_tracking
                # cleanup_jellyfin_tracking()  # DISABLED: Clears shared set used by Emby polling
                app.logger.debug("Tracking cleanup skipped (shared with Emby)")
            except Exception as e:
                app.logger.debug(f"No tracking to clean: {e}")
            
            # Fallback processing if watched enough
            if all([series_name, season is not None, episode is not None]):
                try:
                    from media_processor import (
                        get_episode_tracking_key,
                        processed_jellyfin_episodes,
                        process_jellyfin_episode,
                        check_jellyfin_user,
                        JELLYFIN_TRIGGER_PERCENTAGE
                    )
                    
                    if not check_jellyfin_user(user_name):
                        return jsonify({'status': 'success'}), 200
                    
                    progress_ticks = data.get('PlaybackPositionTicks', 0)
                    runtime_ticks = data.get('RunTimeTicks', 1)
                    progress_percent = (progress_ticks / runtime_ticks * 100) if runtime_ticks > 0 else 0
                    
                    app.logger.info(f"Final progress: {progress_percent:.1f}%")
                    
                    tracking_key = get_episode_tracking_key(series_name, season, episode, user_name)
                    if tracking_key in processed_jellyfin_episodes:
                        app.logger.info(f"Already processed via PlaybackProgress")
                        processed_jellyfin_episodes.clear()
                        return jsonify({'status': 'success', 'message': 'Already processed'}), 200
                    
                    if progress_percent >= JELLYFIN_TRIGGER_PERCENTAGE:
                        app.logger.info(f"🎯 Processing on stop at {progress_percent:.1f}%")
                        
                        # ─── NEW: Tag sync & drift correction BEFORE processing ───
                        series_id = get_series_id(series_name)
                        final_rule = None
                        
                        if series_id:
                            config = load_config()
                            config_rule = None
                            series_id_str = str(series_id)
                            
                            for rule_name, rule_details in config['rules'].items():
                                if series_id_str in rule_details.get('series', {}):
                                    config_rule = rule_name
                                    break
                            
                            if config_rule:
                                matches, actual_tag_rule = episeerr_utils.validate_series_tag(series_id, config_rule)
                                
                                if not matches:
                                    if actual_tag_rule:
                                        logger.warning(f"JELLYFIN STOP DRIFT - config: {config_rule} → tag: {actual_tag_rule}")
                                        episeerr_utils.move_series_in_config(series_id, config_rule, actual_tag_rule)
                                        final_rule = actual_tag_rule
                                    else:
                                        logger.warning(f"No tag on {series_id} → restoring episeerr_{config_rule}")
                                        episeerr_utils.sync_rule_tag_to_sonarr(series_id, config_rule)
                                        final_rule = config_rule
                        
                        # ─── Pass to processing ───
                        episode_info = {
                            'user_name': user_name,
                            'series_name': series_name,
                            'season_number': int(season),
                            'episode_number': int(episode),
                            'progress_percent': progress_percent,
                            'sonarr_series_id': series_id,  # NEW
                            'rule': final_rule             # NEW
                        }
                        
                        process_jellyfin_episode(episode_info)
                        return jsonify({'status': 'success', 'message': 'Processed on stop'}), 200
                    else:
                        app.logger.info(f"Skipped - only watched {progress_percent:.1f}%")
                        
                except Exception as e:
                    app.logger.error(f"Error processing on stop: {e}")
            
            return jsonify({'status': 'success', 'message': 'Playback stopped'}), 200

        else:
            app.logger.info(f"Jellyfin notification type '{notification_type}' not handled")
            return jsonify({'status': 'success', 'message': 'Event not handled'}), 200

    except Exception as e:
        app.logger.error(f"Error handling Jellyfin webhook: {str(e)}")
        import traceback
        app.logger.error(f"Traceback: {traceback.format_exc()}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

# Add a debug endpoint to check polling status
@app.route('/api/jellyfin-polling-status')
def jellyfin_polling_status():
    """Get current Jellyfin polling status for debugging."""
    try:
        from media_processor import get_jellyfin_polling_status
        status = get_jellyfin_polling_status()
        return jsonify({
            'status': 'success',
            'polling_status': status
        })
    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

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
# ADD this new API endpoint to episeerr.py:
@app.route('/api/jellyfin-active-polling-status')
def jellyfin_active_polling_status():
    """Get Jellyfin active polling status."""
    try:
        from media_processor import get_jellyfin_active_polling_status
        status = get_jellyfin_active_polling_status()
        return jsonify({
            'status': 'success',
            'polling_status': status
        })
    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': str(e)
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
                    app.logger.error(f"✗ Failed to create tag for rule: {rule_name}")
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
        
        # Step 3: Drift detection - fix mismatched tags
        drift_fixed = 0
        drift_synced = 0
        
        for rule_name, rule_details in config['rules'].items():
            for series_id_str in list(rule_details.get('series', {}).keys()):
                try:
                    series_id = int(series_id_str)
                    matches, actual_tag_rule = episeerr_utils.validate_series_tag(series_id, rule_name)
                    
                    if not matches:
                        if actual_tag_rule:
                            # Find actual rule name (case-insensitive)
                            actual_rule_name = None
                            for rn in config['rules'].keys():
                                if rn.lower() == actual_tag_rule.lower():
                                    actual_rule_name = rn
                                    break
                            
                            if actual_rule_name:
                                # Move series to new rule
                                series_data = rule_details['series'][series_id_str]
                                del rule_details['series'][series_id_str]
                                
                                target_rule = config['rules'][actual_rule_name]
                                target_rule.setdefault('series', {})[series_id_str] = series_data
                                
                                drift_fixed += 1
                                app.logger.info(f"  Drift: Moved series {series_id} to '{actual_rule_name}'")
                        else:
                            # Sync missing tag
                            episeerr_utils.sync_rule_tag_to_sonarr(series_id, rule_name)
                            drift_synced += 1
                
                except Exception as e:
                    app.logger.debug(f"Error checking series {series_id_str}: {str(e)}")
        
        # Step 4: Orphaned tags - find shows tagged in Sonarr but not in config
        all_series = get_sonarr_series()
        
        # Build set of series IDs in config
        config_series_ids = set()
        for rule_details in config['rules'].values():
            config_series_ids.update(rule_details.get('series', {}).keys())
        
        orphaned = 0
        for series in all_series:
            series_id = str(series['id'])
            
            # Skip if already in config
            if series_id in config_series_ids:
                continue
            
            # Check if has episeerr tag
            tag_mapping = episeerr_utils.get_tag_mapping()
            for tag_id in series.get('tags', []):
                tag_name = tag_mapping.get(tag_id, '').lower()
                
                # Found episeerr rule tag (not default/select)
                if tag_name.startswith('episeerr_'):
                    rule_name = tag_name.replace('episeerr_', '')
                    if rule_name not in ['default', 'select']:
                        # Find actual rule name (case-insensitive)
                        actual_rule_name = None
                        for rn in config['rules'].keys():
                            if rn.lower() == rule_name:
                                actual_rule_name = rn
                                break
                        
                        if actual_rule_name:
                            # Add to config
                            config['rules'][actual_rule_name].setdefault('series', {})[series_id] = {}
                            orphaned += 1
                            app.logger.info(f"  Orphaned: Added {series['title']} to '{actual_rule_name}'")
                            break
        
        # Save if any changes made
        if drift_fixed > 0 or drift_synced > 0 or orphaned > 0:
            save_config(config)
        
        # Summary
        app.logger.info(f"✓ Tag reconciliation complete: {drift_fixed} moved, {drift_synced} synced, {orphaned} orphaned")
            
    except Exception as e:
        app.logger.error(f"Error during tag reconciliation: {str(e)}")
    
    # NEW: Ensure delay profile has control tags ONLY (default, select, delay)
    try:
        updated = episeerr_utils.update_delay_profile_with_control_tags()
        if updated:
            app.logger.info("✓ Delay profile updated with control tags (default, select, delay)")
        else:
            app.logger.warning("Delay profile update skipped or failed (check logs)")
    except Exception as e:
        app.logger.error(f"Error updating delay profile with control tags: {str(e)}")
    
    # NEW: Add Jellyfin active polling
    try:
        from media_processor import start_jellyfin_active_polling
        started = start_jellyfin_active_polling()
        if started:
            app.logger.info("✅ Jellyfin active polling started (every 15 minutes)")
        else:
            app.logger.info("⏭️ Jellyfin not configured - active polling disabled")
    except Exception as e:
        app.logger.error(f"Error starting Jellyfin active polling: {str(e)}")
    
    app.logger.debug("Exiting initialize_episeerr()")

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
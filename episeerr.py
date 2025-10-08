__version__ = "2.5.6"
from flask import Flask, render_template, request, redirect, url_for, jsonify
import subprocess
import os
import atexit
import re
import time
import logging
import json
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

app = Flask(__name__)

# Load environment variables
load_dotenv()
BASE_DIR = os.getcwd()

# Sonarr variables
SONARR_URL = normalize_url(os.getenv('SONARR_URL'))
SONARR_API_KEY = os.getenv('SONARR_API_KEY')

# Jellyseerr/Overseerr variables
JELLYSEERR_URL = normalize_url(os.getenv('JELLYSEERR_URL', ''))
JELLYSEERR_API_KEY = os.getenv('JELLYSEERR_API_KEY')
OVERSEERR_URL = normalize_url(os.getenv('OVERSEERR_URL'))
OVERSEERR_API_KEY = os.getenv('OVERSEERR_API_KEY')
SEERR_ENABLED = bool((JELLYSEERR_URL and JELLYSEERR_API_KEY) or (OVERSEERR_URL and OVERSEERR_API_KEY))

# TMDB API Key
TMDB_API_KEY = os.getenv('TMDB_API_KEY')
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

# Setup logging
log_file = os.getenv('LOG_PATH', os.path.join(os.getcwd(), 'logs', 'app.log'))
log_level = logging.INFO
os.makedirs(os.path.dirname(log_file), exist_ok=True)

file_handler = RotatingFileHandler(
    log_file,
    maxBytes=1*1024*1024,
    backupCount=2,
    encoding='utf-8'
)
file_handler.setLevel(log_level)
file_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
file_handler.setFormatter(file_formatter)

logging.basicConfig(
    level=log_level,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[file_handler]
)

stream_handler = logging.StreamHandler()
stream_handler.setLevel(logging.DEBUG if os.getenv('FLASK_DEBUG', 'false').lower() == 'true' else logging.INFO)
formatter = logging.Formatter('%(asctime)s:%(levelname)s:%(message)s')
stream_handler.setFormatter(formatter)
app.logger.addHandler(stream_handler)

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

# ============================================================================
# SIMPLIFIED CONFIG MANAGEMENT
# ============================================================================

def load_config():
    """Load configuration with simplified migration."""
    try:
        with open(config_path, 'r') as file:
            config = json.load(file)
        if 'rules' not in config:
            config['rules'] = {}
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
    """Save configuration to JSON file."""
    try:
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, 'w') as file:
            json.dump(config, file, indent=4)
        app.logger.debug("Config saved successfully")
    except Exception as e:
        app.logger.error(f"Save failed: {str(e)}")
        raise

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

@app.route('/')
def index():
    """Main rules management page."""
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
                         SONARR_URL=sonarr_url,  # ADD THIS
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
# Update your create_rule route to handle the default rule checkbox
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
        
        grace_watched = None if not grace_watched else int(grace_watched)
        grace_unwatched = None if not grace_unwatched else int(grace_unwatched)
        dormant_days = None if not dormant_days else int(dormant_days)
        
        # Save rule
        config['rules'][rule_name] = {
            'get_type': get_type,
            'get_count': get_count,
            'keep_type': keep_type,
            'keep_count': keep_count,
            'action_option': request.form.get('action_option', 'monitor'),
            'monitor_watched': 'monitor_watched' in request.form,
            'grace_watched': grace_watched,
            'grace_unwatched': grace_unwatched,
            'dormant_days': dormant_days,
            'series': {},
            'dry_run': False
        }
        
        # Handle default rule setting
        if 'set_as_default' in request.form:
            config['default_rule'] = rule_name
        
        save_config(config)
        
        message = f"Rule '{rule_name}' created successfully"
        if 'set_as_default' in request.form:
            message += " and set as default"
        
        return redirect(url_for('index', message=message))
    return render_template('create_rule.html')

# Update your edit_rule route to handle the default rule checkbox
@app.route('/edit-rule/<rule_name>', methods=['GET', 'POST'])
def edit_rule(rule_name):
    """Edit an existing rule."""
    config = load_config()
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
        
        grace_watched = None if not grace_watched else int(grace_watched)
        grace_unwatched = None if not grace_unwatched else int(grace_unwatched)
        dormant_days = None if not dormant_days else int(dormant_days)
        
        # Update rule
        config['rules'][rule_name].update({
            'get_type': get_type,
            'get_count': get_count,
            'keep_type': keep_type,
            'keep_count': keep_count,
            'action_option': request.form.get('action_option', 'monitor'),
            'monitor_watched': 'monitor_watched' in request.form,
            'grace_watched': grace_watched,
            'grace_unwatched': grace_unwatched,
            'dormant_days': dormant_days
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
        
        message = f"Rule '{rule_name}' updated successfully"
        if 'set_as_default' in request.form and config.get('default_rule') == rule_name:
            message += " and set as default"
        
        return redirect(url_for('index', message=message))
    
    rule = config['rules'][rule_name]
    return render_template('edit_rule.html', rule_name=rule_name, rule=rule, config=config)

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

# Alternative: If you prefer a simple ID-based approach
@app.template_filter('sonarr_url_simple')
def sonarr_series_url_simple(series_id):
    """Generate Sonarr series URL from series ID."""
    return f"{SONARR_URL}/series/{series_id}"
@app.route('/delete-rule/<rule_name>', methods=['POST'])
def delete_rule(rule_name):
    """Delete a rule."""
    config = load_config()
    if rule_name not in config['rules']:
        return redirect(url_for('index', message=f"Rule '{rule_name}' not found"))
    if rule_name == config.get('default_rule'):
        return redirect(url_for('index', message="Cannot delete the default rule"))
    del config['rules'][rule_name]
    save_config(config)
    return redirect(url_for('index', message=f"Rule '{rule_name}' deleted successfully"))

@app.route('/assign-rules', methods=['POST'])
def assign_rules():
    """Assign series to rules while preserving activity data."""
    config = load_config()
    rule_name = request.form.get('rule_name')
    series_ids = request.form.getlist('series_ids')
    if not rule_name or rule_name not in config['rules']:
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
    
    message = f"Assigned {len(series_ids)} series to rule '{rule_name}'"
    if preserved_count > 0:
        message += f" (preserved activity data for {preserved_count} series)"
    
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
        if key.endswith('_URL') and value:
            # Extract service ID (e.g., 'SONARR' from 'SONARR_URL')
            service_id = key[:-len('_URL')].lower()
            
            # Get name: Use corresponding _NAME variable, default_names, or capitalize service_id
            name_key = f'{key[:-len("_URL")]}_NAME'
            service_name = os.getenv(name_key, default_names.get(service_id, service_id.capitalize()))
            
            # Get icon: Use corresponding _ICON variable, default_icons, or generic icon
            icon_key = f'{key[:-len("_URL")]}_ICON'
            service_icon = os.getenv(icon_key, default_icons.get(service_id, 'fas fa-link'))
            
            # Add to services dictionary
            services[service_id] = {
                'name': service_name,
                'url': value,
                'icon': service_icon
            }
    
    return {'services': services}

@app.route('/set-default-rule', methods=['POST'])
def set_default_rule():
    """Set the default rule."""
    config = load_config()
    rule_name = request.form.get('rule_name')
    if rule_name not in config['rules']:
        return redirect(url_for('index', message="Invalid rule selected"))
    config['default_rule'] = rule_name
    save_config(config)
    return redirect(url_for('index', message=f"Set '{rule_name}' as default rule"))

@app.route('/unassign-series', methods=['POST'])
def unassign_series():
    """Unassign series from all rules."""
    config = load_config()
    series_ids = request.form.getlist('series_ids')
    total_removed = 0
    for rule_name, details in config['rules'].items():
        original_count = len(details.get('series', {}))
        series_dict = details.get('series', {})
        for series_id in series_ids:
            if series_id in series_dict:
                del series_dict[series_id]
        total_removed += original_count - len(details['series'])
    save_config(config)
    return redirect(url_for('index', message=f"Unassigned {len(series_ids)} series from all rules"))

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
        rules_mapping = {}
        for rule_name, details in config['rules'].items():
            for series_id in details.get('series', {}):
                rules_mapping[str(series_id)] = rule_name
        stats = {
            'total_series': len(all_series),
            'assigned_series': len(rules_mapping),
            'unassigned_series': len(all_series) - len(rules_mapping),
            'total_rules': len(config['rules']),
            'rule_breakdown': {}
        }
        for rule_name, details in config['rules'].items():
            stats['rule_breakdown'][rule_name] = len(details.get('series', {}))
        return jsonify(stats)
    except Exception as e:
        app.logger.error(f"Error getting series stats: {str(e)}")
        return jsonify({'error': str(e)}), 500

def cleanup_config_rules():
    """Clean up config by removing non-existent series."""
    try:
        config = load_config()
        existing_series = get_sonarr_series()
        existing_series_ids = set(str(series['id']) for series in existing_series)
        changes_made = False
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
        config['rules'] = {
            rule: details for rule, details in config['rules'].items()
            if details.get('series') or rule == config.get('default_rule', 'default')
        }
        if changes_made:
            save_config(config)
            app.logger.info("Configuration cleanup completed")
    except Exception as e:
        app.logger.error(f"Error during config cleanup: {str(e)}")

@app.route('/cleanup')
def cleanup():
    """Clean up configuration."""
    cleanup_config_rules()
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
    return render_template('scheduler_admin.html')

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
        auto_assign_new_series = data.get('auto_assign_new_series', False)  # ADD THIS LINE
        
        # Validate inputs
        if storage_min_gb is not None:
            storage_min_gb = int(storage_min_gb) if storage_min_gb else None
        
        settings = {
            'global_storage_min_gb': storage_min_gb,
            'cleanup_interval_hours': int(cleanup_interval_hours),
            'dry_run_mode': bool(dry_run_mode),
            'auto_assign_new_series': bool(auto_assign_new_series)  # ADD THIS LINE
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
    """Episeerr main page."""
    return render_template('episeerr_index.html')

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
        
        # Add seasons (skip season 0 - specials)
        for season in show_data.get('seasons', []):
            if season.get('season_number', 0) > 0:
                formatted_show['seasons'].append({
                    'seasonNumber': season['season_number'],
                    'episodeCount': season.get('episode_count', '?')
                })
        
        return render_template('season_selection.html', 
                             show=formatted_show, 
                             tmdb_id=tmdb_id)
    
    except Exception as e:
        app.logger.error(f"Error in select_seasons: {str(e)}", exc_info=True)
        return render_template('error.html', message=f"Error loading season selection: {str(e)}")

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
        
        return render_template('episode_selection.html', 
                             show=formatted_show, 
                             request_id=request_id,
                             series_id=series_id,
                             selected_seasons=selected_seasons)
    
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
            
            return redirect(url_for('episeerr_index', message="Request cancelled"))
        
        elif action == 'process':
            # Load request data
            request_file = os.path.join(REQUESTS_DIR, f"{request_id}.json")
            if not os.path.exists(request_file):
                return redirect(url_for('episeerr_index', message="Error: Request not found"))
            
            with open(request_file, 'r') as f:
                request_data = json.load(f)
            
            series_id = request_data['series_id']
            
            if not episodes:
                return redirect(url_for('episeerr_index', message="Error: No episodes selected"))
            
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
                return redirect(url_for('episeerr_index', message="Error: No valid episodes found"))
            
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
            
            # Clean up request file
            try:
                os.remove(request_file)
                app.logger.info(f"Removed request file: {request_id}.json")
            except Exception as e:
                app.logger.error(f"Error removing request file: {str(e)}")
            
            # Prepare result message
            if failed_seasons:
                message = f"Processed {total_processed} episodes. Failed seasons: {failed_seasons}"
                return redirect(url_for('episeerr_index', message=message))
            else:
                seasons_list = list(episodes_by_season.keys())
                message = f"Successfully processed {total_processed} episodes across {len(seasons_list)} seasons"
                return redirect(url_for('episeerr_index', message=message))
        
        else:
            return redirect(url_for('episeerr_index', message="Invalid action"))
            
    except Exception as e:
        app.logger.error(f"Error processing episode selection: {str(e)}", exc_info=True)
        return redirect(url_for('episeerr_index', message="An error occurred while processing episodes"))

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

@app.route('/api/delete-request/<tmdb_id>', methods=['POST'])
def delete_request(tmdb_id):
    """Delete a pending request."""
    try:
        # Look for request files that contain this tmdb_id
        deleted = False
        for filename in os.listdir(REQUESTS_DIR):
            if filename.endswith('.json'):
                filepath = os.path.join(REQUESTS_DIR, filename)
                try:
                    with open(filepath, 'r') as f:
                        request_data = json.load(f)
                    
                    # Check if this request matches the tmdb_id
                    if str(request_data.get('tmdb_id')) == str(tmdb_id):
                        os.remove(filepath)
                        app.logger.info(f"Deleted pending request file {filename} for TMDB ID {tmdb_id}")
                        deleted = True
                        break
                except Exception as e:
                    app.logger.error(f"Error reading request file {filename}: {str(e)}")
                    continue
        
        if deleted:
            return jsonify({"status": "success", "message": "Request deleted successfully"}), 200
        else:
            app.logger.warning(f"Pending request for TMDB ID {tmdb_id} not found")
            return jsonify({"status": "error", "message": "Request not found"}), 404
            
    except Exception as e:
        app.logger.error(f"Error deleting request for TMDB ID {tmdb_id}: {str(e)}")
        return jsonify({"status": "error", "message": "Failed to delete request"}), 500

# ============================================================================
# WEBHOOK ROUTES
# ============================================================================

@app.route('/sonarr-webhook', methods=['POST'])
def process_sonarr_webhook():
    """Handle incoming Sonarr webhooks for series additions with enhanced Jellyseerr integration."""
    app.logger.info("Received Sonarr webhook")
    
    try:
        json_data = request.json
        
        # Get important data from the webhook
        series = json_data.get('series', {})
        series_id = series.get('id')
        tvdb_id = series.get('tvdbId')
        tmdb_id = series.get('tmdbId')
        series_title = series.get('title')
        
        app.logger.info(f"Processing series addition: {series_title} (ID: {series_id}, TVDB: {tvdb_id})")
        
        # Setup Sonarr connection
        sonarr_preferences = sonarr_utils.load_preferences()
        headers = {
            'X-Api-Key': sonarr_preferences['SONARR_API_KEY'],
            'Content-Type': 'application/json'
        }
        sonarr_url = sonarr_preferences['SONARR_URL']

        # Get all tags from Sonarr to map IDs to labels
        tags_response = requests.get(f"{SONARR_URL}/api/v3/tag", headers=headers)
        if not tags_response.ok:
            app.logger.error(f"Failed to get tags from Sonarr: {tags_response.status_code}")
            return jsonify({"status": "error", "message": "Failed to get tags"}), 500
            
        tags = tags_response.json()
        tag_mapping = {tag['id']: tag['label'].lower() for tag in tags}

        # Check series tags
        series_tags = series.get('tags', [])
        app.logger.info(f"Series tags: {series_tags}")
        app.logger.info(f"Tag mapping: {tag_mapping}")

        # Check for episeerr tags
        has_episeerr_default = False
        has_episeerr_select = False

        for tag in series_tags:
            if isinstance(tag, int):
                # Tag is an ID - check against stored global IDs
                if tag == EPISEERR_DEFAULT_TAG_ID:
                    has_episeerr_default = True
                    break
                elif tag == EPISEERR_SELECT_TAG_ID:
                    has_episeerr_select = True
                    break
            else:
                # Tag is a string name - check the name directly
                if str(tag).lower() == 'episeerr_default':
                    has_episeerr_default = True
                    break
                elif str(tag).lower() == 'episeerr_select':
                    has_episeerr_select = True
                    break

        # If no episeerr tags, check if auto-assign is enabled
        if not has_episeerr_default and not has_episeerr_select:
            # Check for auto-assign setting from GLOBAL SETTINGS
            import media_processor
            global_settings = media_processor.load_global_settings()
            auto_assign_enabled = global_settings.get('auto_assign_new_series', False)
            
            if auto_assign_enabled:
                app.logger.info(f"Auto-assign enabled: Adding {series_title} to default rule (no processing)")
                
                # Add to default rule (same logic as episeerr_default but without episode processing)
                try:
                    config = load_config()
                    default_rule_name = config.get('default_rule', 'default')
                    
                    if default_rule_name not in config['rules']:
                        app.logger.error(f"Default rule '{default_rule_name}' not found in config!")
                        return jsonify({"status": "error", "message": f"Default rule '{default_rule_name}' not found"}), 500
                    
                    series_id_str = str(series_id)
                    target_rule = config['rules'][default_rule_name]
                    
                    if 'series' not in target_rule:
                        target_rule['series'] = {}
                    
                    series_dict = target_rule['series']
                    if not isinstance(series_dict, dict):
                        series_dict = {}
                        target_rule['series'] = series_dict
                    
                    if series_id_str not in series_dict:
                        series_dict[series_id_str] = {'activity_date': None}
                        save_config(config)
                        app.logger.info(f"✓ Auto-assigned {series_title} to default rule '{default_rule_name}' (no episode processing)")
                    else:
                        app.logger.info(f"Series {series_id_str} already in rule '{default_rule_name}'")
                    
                    return jsonify({"status": "success", "message": f"Auto-assigned to default rule"}), 200
                    
                except Exception as e:
                    app.logger.error(f"Error auto-assigning series: {str(e)}", exc_info=True)
                    return jsonify({"status": "error", "message": f"Failed to auto-assign: {str(e)}"}), 500
            else:
                app.logger.info(f"Series {series_title} has no episeerr tags, doing nothing")
                return jsonify({"status": "success", "message": "Series has no episeerr tags, no processing needed"}), 200
        
        # Check for pending Jellyseerr request
        jellyseerr_request_id = None
        tvdb_id_str = str(tvdb_id) if tvdb_id else None

        app.logger.info(f"Looking for Jellyseerr request with TVDB ID: {tvdb_id_str}")

        if tvdb_id_str:
            request_file = os.path.join(REQUESTS_DIR, f"jellyseerr-{tvdb_id_str}.json")
            if os.path.exists(request_file):
                try:
                    with open(request_file, 'r') as f:
                        request_data = json.load(f)
                    jellyseerr_request_id = request_data.get('request_id')
                    app.logger.info(f"✓ Found Jellyseerr request file: {jellyseerr_request_id}")
                    
                    # Cancel the Jellyseerr request
                    app.logger.info(f"Cancelling Jellyseerr request {jellyseerr_request_id}")
                    cancel_result = episeerr_utils.delete_overseerr_request(jellyseerr_request_id)
                    app.logger.info(f"Jellyseerr cancellation result: {cancel_result}")
                    
                    # Delete the file after processing
                    os.remove(request_file)
                    app.logger.info(f"✓ Removed Jellyseerr request file for TVDB ID {tvdb_id_str}")
                    
                except Exception as e:
                    app.logger.error(f"Error processing Jellyseerr request file: {str(e)}")
            else:
                app.logger.info(f"No Jellyseerr request file found for TVDB ID: {tvdb_id_str}")
                
        # ALWAYS: Unmonitor all episodes and remove episeerr tags
        app.logger.info(f"Unmonitoring all episodes for {series_title}")
        unmonitor_success = episeerr_utils.unmonitor_series(series_id, headers)
        if not unmonitor_success:
            app.logger.warning(f"Failed to unmonitor episodes for {series_title}")
        
        # Remove episeerr tags from the series
        app.logger.info(f"Removing episeerr tags from {series_title}")
        updated_tags = []
        removed_tags = []
        
        for tag_id in series_tags:
            tag_label = tag_mapping.get(tag_id, '').lower()
            if tag_label in ['episeerr_default', 'episeerr_select']:
                removed_tags.append(tag_label)
            else:
                updated_tags.append(tag_id)
        
        if removed_tags:
            # Update series with removed tags
            update_payload = series.copy()
            update_payload['tags'] = updated_tags
            
            update_response = requests.put(f"{SONARR_URL}/api/v3/series", headers=headers, json=update_payload)
            if update_response.ok:
                app.logger.info(f"✓ Removed tags {removed_tags} from {series_title}")
            else:
                app.logger.error(f"Failed to remove tags from {series_title}: {update_response.text}")
        
        # Cancel any active downloads
        app.logger.info(f"Checking for downloads to cancel for {series_title}")
        try:
            episeerr_utils.check_and_cancel_unmonitored_downloads()
        except Exception as e:
            app.logger.error(f"Error cancelling downloads: {str(e)}")
        
        # Process based on tag type
        if has_episeerr_default:
            app.logger.info(f"Processing {series_title} with episeerr_default tag")
            
            # Add to default rule
            try:
                config = load_config()
                default_rule_name = config.get('default_rule', 'default')
                
                app.logger.info(f"Adding {series_title} to default rule '{default_rule_name}'")
                
                if default_rule_name not in config['rules']:
                    app.logger.error(f"Default rule '{default_rule_name}' not found in config!")
                    return jsonify({"status": "error", "message": f"Default rule '{default_rule_name}' not found"}), 500
                
                series_id_str = str(series_id)
                
                # Get the specific rule we're modifying
                target_rule = config['rules'][default_rule_name]
                
                # Ensure series dict structure exists
                if 'series' not in target_rule:
                    target_rule['series'] = {}
                
                series_dict = target_rule['series']
                if not isinstance(series_dict, dict):
                    app.logger.warning(f"Converting series from {type(series_dict)} to dict for rule '{default_rule_name}'")
                    series_dict = {}
                    target_rule['series'] = series_dict
                
                # Add series if not already present
                if series_id_str not in series_dict:
                    series_dict[series_id_str] = {'activity_date': None}
                    app.logger.info(f"Added series {series_id_str} to rule '{default_rule_name}'")
                    
                    # Validate config before saving
                    if len(config['rules']) < 1:
                        app.logger.error(f"CONFIG CORRUPTION DETECTED! Only {len(config['rules'])} rules found")
                        return jsonify({"status": "error", "message": "Config corruption detected, save aborted"}), 500
                    
                    save_config(config)
                    app.logger.info(f"✓ Successfully added {series_title} to default rule '{default_rule_name}'")
                else:
                    app.logger.info(f"Series {series_id_str} already in rule '{default_rule_name}'")

            except Exception as e:
                app.logger.error(f"Error adding series to default rule: {str(e)}", exc_info=True)
                return jsonify({"status": "error", "message": f"Failed to add series to rule: {str(e)}"}), 500

            # Execute default rule immediately
            try:
                rule_config = config['rules'][default_rule_name]
                get_type = rule_config.get('get_type', 'episodes')
                get_count = rule_config.get('get_count', 1)
                action_option = rule_config.get('action_option', 'monitor')
                
                app.logger.info(f"Executing default rule with get_type '{get_type}', get_count '{get_count}' for {series_title}")
                
                # Get all episodes for the series
                episodes_response = requests.get(
                    f"{SONARR_URL}/api/v3/episode?seriesId={series_id}",
                    headers=headers
                )
                
                if episodes_response.ok:
                    all_episodes = episodes_response.json()
                    
                    # Get Season 1 episodes, sorted by episode number
                    season1_episodes = sorted(
                        [ep for ep in all_episodes if ep.get('seasonNumber') == 1],
                        key=lambda x: x.get('episodeNumber', 0)
                    )
                    
                    if not season1_episodes:
                        app.logger.warning(f"No Season 1 episodes found for {series_title}")
                    else:
                        # Determine which episodes to monitor based on get settings
                        episodes_to_monitor = []
                        
                        if get_type == 'all':
                            # Get all episodes from all seasons
                            episodes_to_monitor = [ep['id'] for ep in all_episodes]
                            app.logger.info(f"Monitoring all episodes for {series_title}")
                            
                        elif get_type == 'seasons':
                            # Get all episodes from Season 1
                            episodes_to_monitor = [ep['id'] for ep in season1_episodes]
                            app.logger.info(f"Monitoring Season 1 ({len(episodes_to_monitor)} episodes) for {series_title}")
                            
                        else:  # episodes
                            try:
                                # Get specific number of episodes from Season 1
                                num_episodes = get_count or 1
                                episodes_to_monitor = [ep['id'] for ep in season1_episodes[:num_episodes]]
                                app.logger.info(f"Monitoring first {len(episodes_to_monitor)} episodes for {series_title}")
                            except (ValueError, TypeError):
                                # Fallback to pilot episode
                                episodes_to_monitor = [season1_episodes[0]['id']] if season1_episodes else []
                                app.logger.warning(f"Invalid get_count, defaulting to pilot episode for {series_title}")
                        
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
                                    search_response = requests.post(
                                        f"{SONARR_URL}/api/v3/command",
                                        headers=headers,
                                        json={"name": "EpisodeSearch", "episodeIds": episodes_to_monitor}
                                    )
                                    
                                    if search_response.ok:
                                        app.logger.info(f"✓ Started search for {len(episodes_to_monitor)} episodes of {series_title}")
                                    else:
                                        app.logger.error(f"Failed to search for episodes: {search_response.text}")
                            else:
                                app.logger.error(f"Failed to monitor episodes: {monitor_response.text}")
                        else:
                            app.logger.warning(f"No episodes to monitor for {series_title}")
                else:
                    app.logger.error(f"Failed to get episodes for series: {episodes_response.text}")
            except Exception as e:
                app.logger.error(f"Error executing default rule for {series_title}: {str(e)}", exc_info=True)
        
        elif has_episeerr_select:
            app.logger.info(f"Processing {series_title} with episeerr_select tag - creating selection request")
            
            # Ensure we have a TMDB ID for the UI
            if not tmdb_id:
                try:
                    # Try to get TMDB ID from TVDB ID using TMDB API
                    external_ids = get_external_ids(tvdb_id, 'tv')
                    if external_ids and external_ids.get('tmdb_id'):
                        tmdb_id = external_ids['tmdb_id']
                    else:
                        # Search by title as fallback
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
            
            return jsonify({
                "status": "success",
                "message": "Episode selection request created"
            }), 200
        
        return jsonify({
            "status": "success",
            "message": "Processing completed"
        }), 200
            
    except Exception as e:
        app.logger.error(f"Error processing Sonarr webhook: {str(e)}", exc_info=True)
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
        
        if tvdb_id and request_id:
            tvdb_id_str = str(tvdb_id)
            request_file = os.path.join(REQUESTS_DIR, f"jellyseerr-{tvdb_id_str}.json")
            
            request_data = {
                'request_id': request_id,
                'title': title,
                'tmdb_id': tmdb_id,
                'tvdb_id': tvdb_id,
                'timestamp': int(time.time())
            }
            
            os.makedirs(REQUESTS_DIR, exist_ok=True)
            with open(request_file, 'w') as f:
                json.dump(request_data, f)
            
            app.logger.info(f"✓ Stored Jellyseerr request {request_id} for TVDB ID {tvdb_id_str} ({title})")
        else:
            app.logger.warning(f"Missing required data - TVDB ID: {tvdb_id}, Request ID: {request_id}")

        return jsonify({"status": "success"}), 200
        
    except Exception as e:
        app.logger.error(f"Error processing Jellyseerr webhook: {str(e)}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/webhook', methods=['POST'])
def handle_server_webhook():
    """Handle Tautulli webhook."""
    app.logger.info("Received webhook from Tautulli")
    data = request.json
    if data:
        try:
            temp_dir = os.path.join(os.getcwd(), 'temp')
            os.makedirs(temp_dir, exist_ok=True)
            plex_data = {
                "server_title": data.get('plex_title') or data.get('server_title'),
                "server_season_num": data.get('plex_season_num') or data.get('server_season_num'),
                "server_ep_num": data.get('plex_ep_num') or data.get('server_ep_num'),
                "thetvdb_id": data.get('thetvdb_id'),
                "themoviedb_id": data.get('themoviedb_id')
            }
            with open(os.path.join(temp_dir, 'data_from_server.json'), 'w') as f:
                json.dump(plex_data, f)
            result = subprocess.run(["python3", os.path.join(os.getcwd(), "media_processor.py")], capture_output=True, text=True)
            
            # FIXED: Check return code instead of just stderr
            if result.returncode != 0:
                app.logger.error(f"media_processor.py failed with return code {result.returncode}")
                if result.stderr:
                    app.logger.error(f"Error output: {result.stderr}")
            else:
                # Success - log as info instead of error
                if result.stderr:
                    app.logger.info(f"media_processor.py output: {result.stderr}")
                    
            app.logger.info("Webhook processing completed - activity tracked, next content processed")
            return jsonify({'status': 'success'}), 200
        except Exception as e:
            app.logger.error(f"Failed to process Tautulli webhook: {str(e)}")
            return jsonify({'status': 'error', 'message': str(e)}), 500
    return jsonify({'status': 'error', 'message': 'No data received'}), 400

# Replace your webhook handler with this version that uses SessionStart

@app.route('/jellyfin-webhook', methods=['POST'])
def handle_jellyfin_webhook():
    """Handle Jellyfin webhook - Using SessionStart + PlaybackStop."""
    app.logger.info("Received webhook from Jellyfin")
    data = request.json
    if not data:
        return jsonify({'status': 'error', 'message': 'No data received'}), 400

    try:
        notification_type = data.get('NotificationType')
        app.logger.info(f"Jellyfin webhook type: {notification_type}")

        if notification_type == 'SessionStart':  # CHANGED: Use SessionStart instead of PlaybackStart
            # Start polling for this session
            item_type = data.get('ItemType')
            if item_type == 'Episode':
                series_name = data.get('SeriesName')
                season = data.get('SeasonNumber')
                episode = data.get('EpisodeNumber')
                webhook_id = data.get('Id')
                user_name = data.get('NotificationUsername', 'Unknown')

                if all([series_name, season is not None, episode is not None]):
                    app.logger.info(f"📺 Jellyfin session started: {series_name} S{season}E{episode} (User: {user_name})")
                    app.logger.info(f"🔄 This handles both NEW episodes and RESUMED episodes")
                    
                    # Import and start polling
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
                            return jsonify({
                                'status': 'success', 
                                'message': f'Started polling for {series_name} S{season}E{episode}'
                            }), 200
                        else:
                            app.logger.warning(f"⚠️ Failed to start polling (may already be active)")
                            return jsonify({'status': 'warning', 'message': 'Polling may already be active'}), 200
                            
                    except Exception as e:
                        app.logger.error(f"Error starting Jellyfin polling: {str(e)}")
                        return jsonify({'status': 'error', 'message': f'Failed to start polling: {str(e)}'}), 500
                else:
                    missing_fields = []
                    if not series_name: missing_fields.append('SeriesName')
                    if season is None: missing_fields.append('SeasonNumber')  
                    if episode is None: missing_fields.append('EpisodeNumber')
                    
                    app.logger.warning(f"Missing required fields: {missing_fields}")
                    return jsonify({'status': 'error', 'message': f'Missing fields: {missing_fields}'}), 400
            else:
                app.logger.info(f"Item type '{item_type}' is not an episode, ignoring session start")
                return jsonify({'status': 'success', 'message': 'Not an episode'}), 200

        elif notification_type == 'PlaybackStop':
            # Stop polling for this episode
            webhook_id = data.get('Id')
            series_name = data.get('SeriesName', 'Unknown')
            season = data.get('SeasonNumber')
            episode = data.get('EpisodeNumber')
            user_name = data.get('NotificationUsername', 'Unknown')
            
            app.logger.info(f"📺 Jellyfin playback stopped: {series_name} S{season}E{episode} (User: {user_name})")
            
            if all([series_name, season is not None, episode is not None]):
                try:
                    from media_processor import stop_jellyfin_polling
                    
                    episode_info = {
                        'user_name': user_name,
                        'series_name': series_name,
                        'season_number': int(season),
                        'episode_number': int(episode)
                    }
                    
                    stopped = stop_jellyfin_polling(webhook_id, episode_info)
                    
                    if stopped:
                        app.logger.info(f"🛑 Stopped polling for {series_name} S{season}E{episode}")
                        return jsonify({'status': 'success', 'message': f'Stopped polling for {series_name}'}), 200
                    else:
                        app.logger.info(f"ℹ️ No active polling found for {series_name} S{season}E{episode}")
                        return jsonify({'status': 'success', 'message': 'No active polling found'}), 200
                        
                except Exception as e:
                    app.logger.error(f"Error stopping Jellyfin polling: {str(e)}")
                    return jsonify({'status': 'error', 'message': f'Failed to stop polling: {str(e)}'}), 500
            else:
                app.logger.warning("📺 Jellyfin playback stopped but missing episode info")
                return jsonify({'status': 'success', 'message': 'Playback stopped (missing episode info)'}), 200

        else:
            app.logger.info(f"Jellyfin notification type '{notification_type}' not handled")
            return jsonify({'status': 'success', 'message': f'Notification type {notification_type} not handled'}), 200

    except Exception as e:
        app.logger.error(f"Failed to process Jellyfin webhook: {str(e)}", exc_info=True)
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

@app.route('/debug-series/<int:series_id>')
def debug_series(series_id):
    """Debug series information."""
    try:
        from media_processor import get_activity_date_with_hierarchy, load_config
        config = load_config()
        rule = None
        rule_name = None
        for r_name, r_details in config['rules'].items():
            if str(series_id) in r_details.get('series', {}):
                rule = r_details
                rule_name = r_name
                break
        if not rule:
            return jsonify({"error": f"Series {series_id} not found in any rule"})
        
        # Get series data from config
        series_data = rule.get('series', {}).get(str(series_id), {})
        activity_date = get_activity_date_with_hierarchy(series_id)
        
        return jsonify({
            "series_id": series_id,
            "rule_name": rule_name,
            "rule_config": rule,
            "series_data": series_data,
            "activity_date": activity_date,
            "current_time": int(time.time()),
            "days_since_activity": (int(time.time()) - activity_date) / (24*60*60) if activity_date else "No activity date"
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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

@app.route('/add-test-activity/<int:series_id>', methods=['POST'])
def add_test_activity(series_id):
    """Add test activity data for debugging."""
    try:
        days_ago = int(request.form.get('days_ago', 16))
        season = int(request.form.get('season', 1))
        episode = int(request.form.get('episode', 1))
        current_time = int(time.time())
        watch_time = current_time - (days_ago * 24 * 60 * 60)
        
        # Update config.json directly
        config = load_config()
        for rule_name, rule_details in config['rules'].items():
            series_dict = rule_details.get('series', {})
            if str(series_id) in series_dict:
                series_dict[str(series_id)] = {
                    'activity_date': watch_time,
                    'last_season': season,
                    'last_episode': episode
                }
                break
        
        save_config(config)
        
        return jsonify({
            "status": "success",
            "message": f"Added activity for series {series_id}: watched S{season}E{episode} {days_ago} days ago",
            "watch_timestamp": watch_time,
            "days_ago": days_ago
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

# ============================================================================
# INITIALIZATION
# ============================================================================

def initialize_episeerr():
    """Initialize episeerr components."""
    app.logger.debug("Entering initialize_episeerr()")
    
    # Existing code...
    try:
        episeerr_utils.check_and_cancel_unmonitored_downloads()
    except Exception as e:
        app.logger.error(f"Error in initial download check: {str(e)}")
    
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

# Create scheduler instance
cleanup_scheduler = OCDarrScheduler()
app.logger.info("✓ OCDarrScheduler instantiated successfully")
cleanup_scheduler.start_scheduler()

if __name__ == '__main__':
    cleanup_config_rules()
    initialize_episeerr()
    app.logger.info("🚀 Enhanced Episeerr starting")
    app.run(host='0.0.0.0', port=5002, debug=os.getenv('FLASK_DEBUG', 'false').lower() == 'true')
